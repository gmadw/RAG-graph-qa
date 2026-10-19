"""FastAPI 入口：文档、图谱、聊天（SSE 流式）、设置等全部路由。"""
import asyncio
import hashlib
import json
import os
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import db, config, documents, graph, rag
from .providers import test_section, ProviderError

ROOT = Path(__file__).resolve().parent.parent.parent
FRONTEND = ROOT / "frontend"

app = FastAPI(title="RAG Graph QA", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def frontend_no_cache(request, call_next):
    """前端资源强制协商缓存。

    Starlette 的 FileResponse / StaticFiles 默认只发 etag + last-modified、不发
    Cache-Control，浏览器会走「启发式缓存」直接用旧副本，导致改了前端刷新页面
    看到仍是旧界面。这里显式要求每次回源校验：文件未变则 304（几乎无开销），
    变了立即拿到新版本。
    """
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/") or path.endswith(
        (".html", ".js", ".css", ".map")
    ):
        if "cache-control" not in {k.lower() for k in response.headers}:
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


ALLOWED_SUFFIXES = {
    ".pdf", ".docx", ".md", ".markdown", ".txt",
    # 图片：走 OCR 识别文字（需在设置页启用 OCR）
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff",
}


@app.on_event("startup")
def startup():
    db.init_db()


# ---------------- 前端 ----------------

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


# ---------------- 设置 ----------------

@app.get("/api/presets")
def presets():
    return config.PRESETS


@app.get("/api/settings")
def get_settings():
    return config.mask_settings(config.get_settings())


class SettingsIn(BaseModel):
    llm: dict | None = None
    embedding: dict | None = None
    rerank: dict | None = None
    ocr: dict | None = None
    graph: dict | None = None
    retrieval: dict | None = None
    ontology: dict | None = None


@app.put("/api/settings")
def put_settings(cfg: SettingsIn):
    saved = config.save_settings(cfg.model_dump(exclude_none=True))
    return {"ok": True, "settings": config.mask_settings(saved)}


@app.post("/api/settings/test/{section}")
async def test_conn(section: str):
    if section not in ("llm", "embedding", "rerank", "ocr"):
        raise HTTPException(400, "section 必须是 llm/embedding/rerank/ocr")
    return await test_section(section)


# ---------------- 知识库 ----------------

class KbIn(BaseModel):
    name: str
    description: str | None = ""


@app.get("/api/kbs")
def list_kbs():
    return db.list_kbs()


@app.post("/api/kbs")
def create_kb(body: KbIn):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "知识库名称不能为空")
    if len(name) > 60:
        raise HTTPException(400, "知识库名称过长（最多 60 字）")
    kb_id = db.create_kb(name, body.description or "")
    return {"ok": True, "id": kb_id}


@app.put("/api/kbs/{kb_id}")
def update_kb(kb_id: int, body: KbIn):
    if not db.kb_exists(kb_id):
        raise HTTPException(404, "知识库不存在")
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "知识库名称不能为空")
    db.rename_kb(kb_id, name, body.description)
    return {"ok": True}


@app.delete("/api/kbs/{kb_id}")
def delete_kb(kb_id: int):
    if not db.kb_exists(kb_id):
        raise HTTPException(404, "知识库不存在")
    if db.count_kbs() <= 1:
        raise HTTPException(400, "至少需要保留一个知识库")
    removed = db.delete_kb(kb_id)
    return {"ok": True, "removed_docs": removed}


# ---------------- 文档 ----------------

@app.post("/api/documents/upload")
async def upload(files: list[UploadFile] = File(...), kb_id: int | None = None,
                 background: BackgroundTasks = None):
    if kb_id is None:
        kbs = db.list_kbs()
        kb_id = kbs[0]["id"] if kbs else None
    elif not db.kb_exists(kb_id):
        raise HTTPException(400, "指定的知识库不存在")
    saved_ids = []
    skipped = []
    for f in files:
        # 仅取文件名，剥离任何路径成分，避免 ../ 路径穿越
        safe_name = Path((f.filename or "upload").replace("\\", "/")).name or "upload"
        suffix = Path(safe_name).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise HTTPException(
                400, f"不支持的文件类型 {suffix}，仅支持 pdf/docx/md/txt 与常见图片格式"
            )
        doc_id = db.insert_document(safe_name, suffix, 0, "", kb_id=kb_id)
        dest = db.UPLOAD_DIR / f"{doc_id}_{safe_name}"
        size = 0
        hasher = hashlib.sha256()
        with open(dest, "wb") as out:
            while True:
                buf = await f.read(1024 * 256)
                if not buf:
                    break
                hasher.update(buf)
                out.write(buf)
                size += len(buf)
        content_hash = hasher.hexdigest()
        dup = db.find_document_by_hash(content_hash, kb_id=kb_id)
        if dup:
            # 同一知识库内内容已存在：回滚刚创建的记录与文件
            db.delete_document(doc_id)
            try:
                dest.unlink()
            except OSError:
                pass
            skipped.append({"filename": safe_name, "existing_id": dup["id"]})
            continue
        db.update_document(doc_id, size=size, content_hash=content_hash)
        saved_ids.append(doc_id)
    for doc_id in saved_ids:
        background.add_task(_run_pipeline, doc_id)
    return {"ok": True, "doc_ids": saved_ids, "skipped": skipped}


async def _run_pipeline(doc_id: int):
    """串行执行后台管线，避免并发写库冲突。"""
    async with _pipeline_lock:
        await documents.process_document(doc_id)
        await _post_graph_tasks(doc_id)


async def _post_graph_tasks(doc_id: int):
    """图谱构建后处理：实体消解合并 + 实体描述生成 +（可选）图谱补全。失败不影响主流程。"""
    try:
        merged = await graph.detect_aliases_for_doc(doc_id)
        if merged:
            print(f"[graph] 文档#{doc_id} 实体消解：合并 {merged} 个别名实体")
        described = await graph.describe_entities_for_doc(doc_id)
        if described:
            print(f"[graph] 文档#{doc_id} 实体描述：生成 {described} 条")
        if config.get_settings().get("graph", {}).get("completion"):
            added = await graph.complete_graph_for_doc(doc_id)
            if added:
                print(f"[graph] 文档#{doc_id} 图谱补全：新增 {added} 条推断关系")
    except Exception as e:
        print(f"[graph] 文档#{doc_id} 图谱后处理失败（已忽略）：{e}")


_pipeline_lock = asyncio.Lock()


@app.get("/api/documents")
def list_docs(kb_id: int | None = None):
    return db.list_documents(kb_id)


@app.delete("/api/documents/{doc_id}")
def del_doc(doc_id: int):
    db.delete_document(doc_id)
    return {"ok": True}


@app.post("/api/documents/{doc_id}/reprocess")
async def reprocess_doc(doc_id: int, background: BackgroundTasks):
    """清空分块与图谱关系后重新解析。

    典型用途：扫描件 PDF 初次上传时未启用 OCR 导致解析失败，开启 OCR 后重跑，
    无需删除重传（原始文件仍在 uploads 下）。
    """
    doc = db.get_document(doc_id)
    if not doc:
        raise HTTPException(404, "文档不存在")
    path = db.UPLOAD_DIR / f"{doc_id}_{doc['filename']}"
    if not path.exists():
        raise HTTPException(400, "原始文件已丢失，请重新上传")
    db.reset_document_for_reprocess(doc_id)
    background.add_task(_run_pipeline, doc_id)
    return {"ok": True}


@app.post("/api/documents/{doc_id}/graph")
async def rebuild_graph(doc_id: int, background: BackgroundTasks):
    doc = db.get_document(doc_id)
    if not doc:
        raise HTTPException(404, "文档不存在")
    if doc["status"] != "done":
        raise HTTPException(400, "文档尚未处理完成，无法构建图谱")
    db.update_document(doc_id, status="processing", stage="构建知识图谱", error="")
    background.add_task(_run_graph_build, doc_id)
    return {"ok": True}


async def _run_graph_build(doc_id: int):
    async with _pipeline_lock:
        try:
            count = await graph.build_graph_for_doc(doc_id, force=True)
            await _post_graph_tasks(doc_id)
            db.update_document(doc_id, status="done", stage="", triple_count=count)
        except Exception as e:
            db.update_document(doc_id, status="done", error=f"图谱构建失败：{e}")


# ---------------- 知识图谱 ----------------

@app.get("/api/graph")
def get_graph(limit: int = 300, q: str = "", kb_id: int | None = None):
    return db.graph_data(limit=limit, query=q.strip() or None, kb_id=kb_id)


@app.get("/api/graph/stats")
def graph_stats_api(kb_id: int | None = None):
    return db.graph_stats(kb_id)


@app.get("/api/graph/entity/{entity_id}")
def entity_api(entity_id: int):
    d = db.entity_detail(entity_id)
    if not d:
        raise HTTPException(404, "实体不存在")
    return d


# ---------------- 聊天 ----------------

class ChatIn(BaseModel):
    chat_id: int | None = None
    query: str
    mode: str | None = None   # 临时覆盖检索模式 vector/graph/hybrid
    kb_id: int | None = None  # 本次问答限定的知识库


@app.post("/api/chat")
async def chat(body: ChatIn):
    query = body.query.strip()
    if not query:
        raise HTTPException(400, "问题不能为空")
    # 已有会话沿用其绑定的知识库；新会话使用传入的知识库
    kb_id = body.kb_id
    if body.chat_id:
        chat_row = db.get_chat(body.chat_id)
        if not chat_row:
            body.chat_id = None
        else:
            kb_id = chat_row.get("kb_id") or kb_id
    if kb_id is not None and not db.kb_exists(kb_id):
        kb_id = None
    if kb_id is None:
        kbs = db.list_kbs()
        kb_id = kbs[0]["id"] if kbs else None
    if not body.chat_id:
        body.chat_id = db.create_chat(query[:24], kb_id=kb_id)
    history = db.get_messages(body.chat_id)
    db.add_message(body.chat_id, "user", query)

    async def gen():
        answer_parts = []
        refs = []
        msg_id = None
        try:
            async for ev in rag.answer_stream_events(query, history, mode_override=body.mode, kb_id=kb_id):
                if ev["event"] == "meta":
                    refs = ev["data"].get("refs", [])
                if ev["event"] == "delta":
                    answer_parts.append(ev["data"]["text"])
                payload = json.dumps(ev["data"], ensure_ascii=False)
                yield f"event: {ev['event']}\ndata: {payload}\n\n"
        except Exception as e:  # 兜底：不让连接悬挂
            yield f"event: error\ndata: {json.dumps({'message': str(e)[:300]}, ensure_ascii=False)}\n\n"
        finally:
            if answer_parts:
                msg_id = db.add_message(body.chat_id, "assistant", "".join(answer_parts), refs)
            else:
                msg_id = db.add_message(
                    body.chat_id, "assistant",
                    "（生成失败，请检查「设置」中的模型配置）", [],
                )
        yield f"event: end\ndata: {json.dumps({'chat_id': body.chat_id, 'message_id': msg_id, 'kb_id': kb_id}, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/chats")
def chats():
    return db.list_chats()


class FeedbackIn(BaseModel):
    message_id: int
    vote: int          # 1 赞 / -1 踩


@app.post("/api/feedback")
async def feedback(body: FeedbackIn):
    """回答反馈：把赞/踩记录到该回答引用的每个 chunk，供检索加权学习。"""
    msg = db.get_message(body.message_id)
    if not msg or msg["role"] != "assistant":
        raise HTTPException(400, "消息不存在或不是助手回答")
    query = db.prev_user_query(msg["chat_id"], body.message_id)
    vote = 1 if body.vote >= 0 else -1
    recorded = 0
    for r in msg["refs"]:
        cid = r.get("chunk_id") if isinstance(r, dict) else None
        if cid:
            db.add_feedback(msg["chat_id"], body.message_id, cid, query, vote)
            recorded += 1
    return {"ok": True, "recorded": recorded}


@app.get("/api/chats/{chat_id}/messages")
def chat_messages(chat_id: int):
    return db.get_messages(chat_id)


@app.delete("/api/chats/{chat_id}")
def del_chat(chat_id: int):
    db.delete_chat(chat_id)
    return {"ok": True}


# ---------------- 检索调试 ----------------

class RetrieveIn(BaseModel):
    query: str
    mode: str | None = None
    kb_id: int | None = None


@app.post("/api/retrieve-preview")
async def retrieve_preview(body: RetrieveIn):
    try:
        chunks, gctx = await rag.retrieve(body.query, mode_override=body.mode, kb_id=body.kb_id)
    except ProviderError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return {
        "chunks": [
            {"n": i, "doc": _doc_name(c["doc_id"]), "score": round(float(c.get("score", 0)), 4), "text": c["text"][:600]}
            for i, c in enumerate(chunks, 1)
        ],
        "graph_entities": gctx.get("entities", []),
        "graph_triples": gctx.get("triples", [])[:30],
    }


def _doc_name(doc_id: int) -> str:
    d = db.get_document(doc_id)
    return d["filename"] if d else f"文档#{doc_id}"


app.mount("/static", StaticFiles(directory=str(FRONTEND / "static")), name="static")
