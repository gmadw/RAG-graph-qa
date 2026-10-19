#!/usr/bin/env python3
"""Mock OpenAI 兼容模型服务（用于无 API Key 的本地体验与端到端测试）。

提供：
  POST /v1/chat/completions   （支持 stream SSE；能识别图谱抽取/实体链接/普通问答三类请求）
  POST /v1/embeddings         （词袋 hash 向量，相似文本向量相近）
  POST /v1/rerank             （词重叠启发式打分）

用法：python3 mock_server.py [--port 9999]
然后在系统「设置」页选「自定义」，Base URL 填 http://localhost:9999/v1 即可。
"""
import argparse
import hashlib
import json
import math
import re
import time
import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI(title="Mock OpenAI-Compatible Server")

DIM = 256
CN_WORD = re.compile(r"[\u4e00-\u9fa5]{2,6}|[A-Za-z][A-Za-z0-9_-]{2,20}")


def tokenize(text: str):
    toks = set(CN_WORD.findall(text))
    # 中文再加单字 bigram，增强相似度判断
    cn = re.findall(r"[\u4e00-\u9fa5]", text)
    for a, b in zip(cn, cn[1:]):
        toks.add(a + b)
    return toks


def embed_one(text: str):
    vec = [0.0] * DIM
    for tok in tokenize(text):
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        vec[h % DIM] += 1.0
        vec[(h >> 8) % DIM] += 0.5
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [round(v / norm, 6) for v in vec]


STOPWORDS = set("的了是在于与和并由其该此为并将把被从对")

def _clean_words(text: str):
    """按标点与常见虚词切分出名词性短语，模拟 LLM 抽出的实体。"""
    parts = re.split(r"[，。；、！？（）「」《》\s的了是在于与和并由该此为并将把被从对]", text)
    words = []
    for p in parts:
        p = p.strip()
        if 2 <= len(p) <= 12 and not any(c in STOPWORDS for c in p[:1]):
            words.append(p)
    seen, out = set(), []
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


def extract_triples_mock(text: str):
    """规则式伪抽取：全词表跳跃配对生成三元组（仅用于无 Key 演示与测试）。"""
    words = _clean_words(text)
    pairs = list(zip(words[::2], words[1::2])) + list(zip(words[1::2], words[2::2]))
    triples = []
    for a, b in pairs[:8]:
        triples.append({
            "head": a, "head_type": "概念",
            "relation": "关联",
            "tail": b, "tail_type": "概念",
        })
    return triples


def fake_answer(messages):
    user = ""
    for m in reversed(messages):
        if m["role"] == "user":
            user = m["content"]
            break
    # 从参考资料里摘取相关句子
    m = re.search(r"【参考资料】\n(.+?)\n\n【用户问题】", user, re.S)
    ctx = m.group(1) if m else ""
    sents = re.split(r"(?<=[。！？\n])", ctx)
    sents = [s.strip().lstrip("[]0123456789 ").strip() for s in sents if len(s.strip()) > 15][:3]
    if sents:
        body = "\n".join(f"- {s[:120]}" for s in sents)
        return (
            "根据知识库中的资料，回答如下：\n" + body +
            "\n\n以上信息来自知识库检索结果 [1][2]。"
            "（当前为 Mock 模型演示输出；在「模型设置」中配置真实的免费 LLM 后即可获得正常回答。）"
        )
    return (
        "知识库中暂无与该问题直接相关的信息。（Mock 模型提示：请先在「文档库」上传文档，"
        "或在「模型设置」中配置真实的免费 LLM，如硅基流动的 GLM-4-9B-0414 / Qwen3-8B。）"
    )


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    user = ""
    for m in reversed(messages):
        if m["role"] == "user":
            user = m["content"]
            break

    if "三元组" in user or "知识图谱信息抽取" in user:
        text = user.split("待抽取文本：")[-1]
        content = json.dumps(extract_triples_mock(text), ensure_ascii=False)
    elif "关键实体" in user or "知识图谱检索" in user:
        text = user.split("用户问题：")[-1]
        # 按常见虚词/标点切分后取短语，更接近真实 LLM 的实体链接效果
        parts = [p.strip() for p in re.split(r"[的了吗呢吧，。；、！？\s]", text) if len(p.strip()) >= 2]
        words = list(dict.fromkeys(parts or CN_WORD.findall(text)))[:5] or [text.strip()[:6]]
        content = json.dumps(words, ensure_ascii=False)
    else:
        content = fake_answer(messages)

    model = body.get("model", "mock")
    if not body.get("stream"):
        return {
            "id": "chatcmpl-mock", "object": "chat.completion", "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    async def gen():
        chunk_size = 24
        for i in range(0, len(content), chunk_size):
            piece = content[i : i + chunk_size]
            data = {
                "id": "chatcmpl-mock", "object": "chat.completion.chunk", "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            await asyncio.sleep(0.02)
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    body = await request.json()
    inp = body.get("input", [])
    if isinstance(inp, str):
        inp = [inp]
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": i, "embedding": embed_one(t)} for i, t in enumerate(inp)
        ],
        "model": body.get("model", "mock-embed"),
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    }


@app.post("/v1/rerank")
async def rerank(request: Request):
    body = await request.json()
    query = body.get("query", "")
    docs = body.get("documents", [])
    qtoks = tokenize(query)
    scored = []
    for i, d in enumerate(docs):
        dtoks = tokenize(d)
        overlap = len(qtoks & dtoks) / (len(qtoks) or 1)
        scored.append({"index": i, "relevance_score": round(overlap, 4)})
    scored.sort(key=lambda x: -x["relevance_score"])
    top_n = body.get("top_n") or len(scored)
    return {"results": scored[:top_n], "model": body.get("model", "mock-rerank")}


if __name__ == "__main__":
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9999)
    parser.add_argument(
        "--allow-public", action="store_true",
        help="允许监听 0.0.0.0（暴露于局域网/公网）。默认仅 127.0.0.1，避免误暴露。",
    )
    args = parser.parse_args()
    if args.host in ("0.0.0.0", "") and not args.allow_public:
        print("[*] 安全护栏：Mock 服务默认仅监听 127.0.0.1，已在本地回环可用。")
        print("    如需在局域网内被其他设备访问，请显式添加 --allow-public 参数，")
        print("    并仅在可信网络中启用（Mock 服务无鉴权）。")
        args.host = "127.0.0.1"
    print(f"Mock 模型服务: http://{args.host}:{args.port}/v1 （OpenAI 兼容）")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
