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
from pydantic import BaseModel, Field

from . import db, config, documents, graph, notes, rag
from .providers import test_section, ProviderError

ROOT = Path(__file__).resolve().parent.parent.parent
FRONTEND = ROOT / "frontend"

app = FastAPI(title="RAG Graph QA", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    # 默认只放行本机来源（任意端口，便于开发/预览）：服务可能被 --host 0.0.0.0
    # 暴露到局域网，CORS 全开会让任意网页从访问者的浏览器静默调用
    # 删除文档 / 改设置等破坏性接口（drive-by localhost 攻击）。
    # 需要跨机调试时用环境变量 RAG_CORS_ORIGINS 显式列出允许的来源（逗号分隔）。
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$",
    allow_origins=[o.strip() for o in os.environ.get("RAG_CORS_ORIGINS", "").split(",") if o.strip()],
    allow_credentials=False,
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
    # 默认 None：PUT 时前端只传 name，不应把已有描述冲成空串
    description: str | None = None


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
    if len(name) > 60:
        raise HTTPException(400, "知识库名称过长（最多 60 字）")
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

# 上传硬限制：服务无鉴权，磁盘是「匿名可写」资源，必须有硬上限防填盘 DoS
MAX_UPLOAD_FILES = 20
MAX_UPLOAD_MB = int(os.environ.get("RAG_MAX_UPLOAD_MB", "100"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024


@app.post("/api/documents/upload")
async def upload(files: list[UploadFile] = File(...), kb_id: int | None = None,
                 background: BackgroundTasks = None):
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(413, f"单次最多上传 {MAX_UPLOAD_FILES} 个文件，请分批导入")
    if kb_id is None:
        kbs = db.list_kbs()
        kb_id = kbs[0]["id"] if kbs else None
    elif not db.kb_exists(kb_id):
        raise HTTPException(400, "指定的知识库不存在")
    # 先整体校验文件类型再落盘：避免批量上传里前面的文件已保存、
    # 后面一个非法文件触发 400 时，已保存的文件卡在 pending 永不处理
    rejected = []
    prepared = []
    for f in files:
        # 仅取文件名，剥离任何路径成分，避免 ../ 路径穿越
        safe_name = Path((f.filename or "upload").replace("\\", "/")).name or "upload"
        suffix = Path(safe_name).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            rejected.append(safe_name)
            continue
        prepared.append((f, safe_name))
    if rejected and not prepared:
        raise HTTPException(
            400, f"不支持的文件类型：{ '、'.join(rejected[:5]) }{'…' if len(rejected) > 5 else ''}，"
                 "仅支持 pdf/docx/md/txt 与常见图片格式"
        )
    saved_ids = []
    skipped = []
    for f, safe_name in prepared:
        suffix = Path(safe_name).suffix.lower()
        doc_id = db.insert_document(safe_name, suffix, 0, "", kb_id=kb_id)
        dest = db.UPLOAD_DIR / f"{doc_id}_{safe_name}"
        size = 0
        hasher = hashlib.sha256()
        try:
            with open(dest, "wb") as out:
                while True:
                    buf = await f.read(1024 * 256)
                    if not buf:
                        break
                    size += len(buf)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(
                            413,
                            f"文件 {safe_name} 超过单文件上限 {MAX_UPLOAD_MB}MB，已中止保存",
                        )
                    hasher.update(buf)
                    out.write(buf)
        except HTTPException:
            # 大小超限：与写盘失败同样回滚，原样透传 413
            db.delete_document(doc_id)
            try:
                dest.unlink()
            except OSError:
                pass
            raise
        except Exception:
            # 写盘失败：回滚刚创建的记录与半截文件，避免残留 pending 文档
            db.delete_document(doc_id)
            try:
                dest.unlink()
            except OSError:
                pass
            raise HTTPException(500, f"文件 {safe_name} 保存失败")
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
    return {"ok": True, "doc_ids": saved_ids, "skipped": skipped, "rejected": rejected}


async def _run_pipeline(doc_id: int):
    """串行执行后台管线，避免并发写库冲突。"""
    async with _pipeline_lock:
        await documents.process_document(doc_id)
        await _post_graph_tasks(doc_id)


async def _post_graph_tasks(doc_id: int):
    """图谱构建后处理：实体消解合并 + 实体描述生成 +（可选）图谱补全 + 笔记双链。

    双链放在最后：先有实体与关系，再让笔记链接挂到同一张图上，形成「笔记 ↔ 概念」的闭环。
    失败不影响主流程。
    """
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
        kb_id = (db.get_document(doc_id) or {}).get("kb_id")
        if db.note_link_stats(kb_id)["notes"]:
            edges = notes.link_notes(kb_id)
            if edges:
                print(f"[notes] 知识库#{kb_id} 双链边：{edges} 条")
    except Exception as e:
        print(f"[graph] 文档#{doc_id} 图谱后处理失败（已忽略）：{e}")


_pipeline_lock = asyncio.Lock()


@app.get("/api/documents")
def list_docs(kb_id: int | None = None):
    return db.list_documents(kb_id)


@app.delete("/api/documents/{doc_id}")
def del_doc(doc_id: int):
    if not db.get_document(doc_id):
        raise HTTPException(404, "文档不存在")
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
    path = db.UPLOAD_DIR / f"{doc_id}_{Path(doc['filename']).name}"
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
            # error 必须清空：否则上一次构建失败的错误提示会一直挂在已成功的文档上
            db.update_document(doc_id, status="done", stage="", error="", triple_count=count)
        except Exception as e:
            db.update_document(doc_id, status="done", error=f"图谱构建失败：{e}")


# ---------------- 知识图谱 ----------------

@app.get("/api/graph")
def get_graph(limit: int = 300, q: str = "", kb_id: int | None = None):
    return db.graph_data(limit=limit, query=q.strip() or None, kb_id=kb_id)


@app.get("/api/graph/stats")
def graph_stats_api(kb_id: int | None = None):
    """图谱规模 + 笔记双链规模（前端用于展示「知识连接度」）。"""
    out = db.graph_stats(kb_id)
    out.update(db.note_link_stats(kb_id))
    return out


@app.get("/api/graph/read")
async def graph_read_api(q: str = "", kb_id: int | None = None, hops: int = 2):
    """图谱解读：把子图渲染成自然语言（实体档案/关系路径/概念社群）。

    不生成回答，供图谱页展示与调试「AI 看到的图长什么样」。
    """
    from . import graph_context
    try:
        g = await graph_context.read_graph(q.strip(), kb_id=kb_id, hops=max(1, min(hops, 3)))
    except ProviderError as e:
        raise HTTPException(502, f"实体链接失败：{e}")
    return {
        "narrative": g["narrative"],
        "entities": g["entities"],
        "paths": g["paths"],
        "communities": g["communities"],
        "triples": g["triples"][:30],
        "stats": g["stats"],
    }


# ---------------- Obsidian 笔记与双链 ----------------

@app.get("/api/notes")
def list_notes(kb_id: int | None = None):
    """笔记列表（按入链数降序）+ 双链统计 + 未解析链接（知识缺口）。"""
    return {
        "notes": db.notes_in_kb(kb_id),
        "stats": db.note_link_stats(kb_id),
        "unresolved": db.unresolved_links(kb_id, limit=20),
    }


@app.get("/api/notes/{doc_id}/backlinks")
def note_backlinks_api(doc_id: int):
    """单篇笔记的反链接 / 出链 / 标签 / 别名。"""
    meta = db.get_note_meta(doc_id)
    if not meta:
        raise HTTPException(404, "文档不存在")
    if not meta.get("title"):
        raise HTTPException(400, "该文档不是 Markdown 笔记，没有双链信息")
    return {
        "meta": meta,
        "backlinks": db.note_backlinks(doc_id, limit=50),
        "outlinks": db.note_outlinks(doc_id, limit=100),
    }


class RelinkIn(BaseModel):
    kb_id: int | None = None


@app.post("/api/notes/relink")
def relink_notes(body: RelinkIn):
    """重建双链：补解析存量笔记 + 解析未绑定链接 + 刷新图谱边。

    典型场景：
      1. 先传了 A（链接 B），后传 B，需要重跑一次才能把 A→B 接上；
      2. 本次升级前上传的 .md 没有双链信息，点一次即可补齐（不需要重新向量化）。
    上传流程已自动触发，这里提供手动入口便于批量导入后在界面上立即刷新。
    """
    backfilled = notes.backfill_notes(body.kb_id)
    resolved = db.resolve_note_links(body.kb_id)
    edges = notes.link_notes(body.kb_id)
    return {"ok": True, "backfilled": backfilled, "resolved": resolved, "edges": edges,
            "stats": db.note_link_stats(body.kb_id)}


@app.get("/api/graph/entity/{entity_id}")
def entity_api(entity_id: int, kb_id: int | None = None):
    d = db.entity_detail(entity_id, kb_id=kb_id)
    if not d:
        raise HTTPException(404, "实体不存在")
    return d


# ---------------- 聊天 ----------------

class ChatIn(BaseModel):
    chat_id: int | None = None
    # 长度上限：超大 payload 会直接打爆 LLM 上下文并放大内存占用，
    # 正常问答远用不到 8000 字，超限直接 422 拒绝
    query: str = Field(..., min_length=1, max_length=8000)
    mode: str | None = None   # 临时覆盖检索模式 vector/graph/graph_read/hybrid/insights
    kb_id: int | None = None  # 本次问答限定的知识库
    web: bool | None = False  # 本次问答是否启用免费联网搜索


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
        stats = None
        try:
            async for ev in rag.answer_stream_events(
                query, history, mode_override=body.mode, kb_id=kb_id, web=bool(body.web)
            ):
                if ev["event"] == "meta":
                    refs = ev["data"].get("refs", [])
                if ev["event"] == "stats":
                    stats = ev["data"]
                if ev["event"] == "delta":
                    answer_parts.append(ev["data"]["text"])
                payload = json.dumps(ev["data"], ensure_ascii=False)
                yield f"event: {ev['event']}\ndata: {payload}\n\n"
        except Exception as e:  # 兜底：不让连接悬挂
            yield f"event: error\ndata: {json.dumps({'message': str(e)[:300]}, ensure_ascii=False)}\n\n"
        finally:
            if answer_parts:
                msg_id = db.add_message(body.chat_id, "assistant", "".join(answer_parts), refs, stats=stats)
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
    query: str = Field(..., min_length=1, max_length=8000)  # 同 ChatIn：防超大 payload
    mode: str | None = None
    kb_id: int | None = None
    web: bool = False


@app.post("/api/retrieve-preview")
async def retrieve_preview(body: RetrieveIn):
    try:
        chunks, gctx, _aux, web_ctx = await rag.retrieve(
            body.query, mode_override=body.mode, kb_id=body.kb_id, web=bool(body.web)
        )
    except ProviderError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return {
        "chunks": [
            {
                "n": i,
                "doc": (c.get("title") or c.get("url") or "网页")
                       if c.get("source") == "web" else _doc_name(c["doc_id"]),
                "score": round(float(c.get("score", 0)), 4),
                "text": c["text"][:600],
                "url": c.get("url"),
                "source": c.get("source"),
            }
            for i, c in enumerate(chunks, 1)
        ],
        "graph_entities": gctx.get("entities", []),
        "graph_triples": gctx.get("triples", [])[:30],
        "graph_narrative": (gctx.get("narrative") or "")[:2000],
        "graph_paths": [p.get("text") for p in (gctx.get("paths") or [])][:8],
        "notes": (gctx.get("notes") or "")[:1500],
        "web": web_ctx,
    }


def _doc_name(doc_id: int) -> str:
    # 图谱洞察产生的分块没有 doc_id，展示成「文档#None」会让人困惑
    if not doc_id:
        return "图谱洞察"
    d = db.get_document(doc_id)
    return d["filename"] if d else f"文档#{doc_id}"


app.mount("/static", StaticFiles(directory=str(FRONTEND / "static")), name="static")
