"""RAG 编排：检索（向量/图谱/混合）→ 重排 → 带引用的流式生成。"""
import json
from . import db
from .config import get_settings
from .providers import llm_chat, rerank, ProviderError
from .vectorstore import vector_search
from . import graph as kg

SYSTEM_PROMPT = """你是「知识库问答助手」，必须严格依据下面提供的参考资料回答用户问题。

要求：
1. 优先使用参考资料中的信息；在句末用 [编号] 标注引用来源，如：……属于核心模块 [2]
2. 参考资料中包含知识图谱三元组（形式为 A --关系--> B），它们是文档中抽取的事实，可用于推理和补充
3. 若参考资料不足以回答，请明确说明"知识库中暂无相关信息"，不要编造
4. 用简体中文回答，结构清晰；涉及多个要点时使用列表
"""

NATURAL_SYSTEM_PROMPT = """你是「知识库问答助手」。当前为纯模型问答模式：未使用知识库检索。

要求：
1. 基于你自身的知识直接回答用户问题，用简体中文，结构清晰
2. 回答末尾注明：（本条回答未使用知识库检索，仅供参考）
3. 不确定的内容要明确说明，不要编造
"""

REWRITE_PROMPT = """你是查询改写助手。你的任务：根据对话历史，把用户的当前问题改写成一个独立完整、适合知识库检索的问题。

规则：
1. 补全指代（"它"、"这个"、"上述"、"该公司"等）与省略成分，使其脱离上下文也能看懂
2. 保持原意，不增加新信息，不要尝试回答
3. 若当前问题本身已独立完整，原样返回
4. 只输出改写后的问题本身，不要任何解释

【对话历史】
{history}

【当前问题】
{query}

改写后的问题："""


async def rewrite_query(query: str, history: list) -> str:
    """多轮对话指代消解：结合历史把当前问题改写为可独立检索的问句。失败时返回原文。"""
    recent = [
        h for h in (history or [])
        if h.get("role") in ("user", "assistant") and (h.get("content") or "").strip()
    ]
    if not recent:
        return query
    lines = []
    for h in recent[-6:]:
        role = "用户" if h["role"] == "user" else "助手"
        lines.append(f"{role}：{h['content'][:300]}")
    try:
        text = await llm_chat(
            [{
                "role": "user",
                "content": REWRITE_PROMPT.format(history="\n".join(lines), query=query),
            }],
            stream=False,
            temperature=0.0,
            max_tokens=200,
        )
    except ProviderError:
        return query
    text = (text or "").strip().strip('"“”').strip()
    # 异常输出（过长/空/换行堆叠）时退回原问题
    if not text or len(text) > 200 or text.count("\n") > 2:
        return query
    return text


def _rrf_merge(result_lists, k=60):
    """Reciprocal Rank Fusion 合并多路检索结果。"""
    scores = {}
    meta = {}
    for results in result_lists:
        for rank, item in enumerate(results):
            cid = item["chunk_id"]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
            if cid not in meta:
                meta[cid] = item
    merged = sorted(scores.items(), key=lambda x: -x[1])
    out = []
    for cid, sc in merged:
        item = dict(meta[cid])
        item["score"] = sc
        out.append(item)
    return out


async def retrieve(query: str, mode_override: str = None, kb_id: int = None):
    """检索 + 可选重排。返回 (chunks, graph_ctx)。
    模式：vector 纯向量 / graph 纯图谱 / hybrid 混合（RRF 融合）/
          insights 图谱洞察（社群+中心性分析）/ natural 纯模型（不检索）。
    指定 kb_id 时仅在该知识库范围内检索。
    """
    cfg = get_settings()
    rcfg = cfg.get("retrieval", {})
    mode = mode_override or rcfg.get("mode", "hybrid")
    top_k = int(rcfg.get("top_k", 6))
    hops = int(rcfg.get("graph_hops", 2))

    graph_ctx = {"triples": [], "entities": []}
    if mode == "natural":
        return [], graph_ctx
    if mode == "vector":
        vec_hits, _ = await vector_search(query, top_k=top_k, kb_id=kb_id)
        chunks = vec_hits
    elif mode == "graph":
        g = await kg.graph_search(query, hops=hops, top_k=top_k, kb_id=kb_id)
        graph_ctx = {"triples": g["triples"], "entities": g["entities"]}
        chunks = g["chunks"]
    elif mode == "insights":
        g = await kg.graph_insights(query, hops=hops, top_k=top_k, kb_id=kb_id)
        graph_ctx = {"triples": g["triples"], "entities": g["entities"]}
        chunks = g["chunks"]
    else:
        vec_hits, _ = await vector_search(query, top_k=top_k, kb_id=kb_id)
        g = await kg.graph_search(query, hops=hops, top_k=top_k, kb_id=kb_id)
        graph_ctx = {"triples": g["triples"], "entities": g["entities"]}
        chunks = _rrf_merge([vec_hits, g["chunks"]])[: top_k + 3]

    # 在线图谱补全：为图谱上下文补充 LLM 推理出的隐含关系
    if cfg.get("graph", {}).get("completion") and mode in ("graph", "hybrid", "insights"):
        inferred = await kg.complete_subgraph(query, graph_ctx["triples"])
        if inferred:
            graph_ctx["triples"] = graph_ctx["triples"] + inferred

    # 反馈学习：被点赞的 chunk 加分、被点踩的减分（重排前生效）
    votes = db.chunk_vote_scores()
    if votes:
        for c in chunks:
            v = votes.get(c.get("chunk_id"))
            if v:
                c["score"] = float(c.get("score", 0.0)) + 0.5 * v
        chunks.sort(key=lambda x: -float(x.get("score", 0.0)))

    rr = cfg.get("rerank", {})
    if rr.get("enabled") and rr.get("model") and len(chunks) > 1:
        try:
            ranked = await rerank(query, [c["text"] for c in chunks], top_k=max(top_k, len(chunks)))
            if ranked:
                by_idx = {i: sc for i, sc in ranked}
                enriched = []
                for i, c in enumerate(chunks):
                    c2 = dict(c)
                    c2["rerank_score"] = by_idx.get(i, -1e9)
                    enriched.append(c2)
                enriched.sort(key=lambda x: -x["rerank_score"])
                chunks = enriched[:top_k]
        except ProviderError:
            pass
    else:
        chunks = chunks[:top_k]
    return chunks, graph_ctx


def build_context(chunks, graph_ctx, max_chars=6000):
    """构建带编号的参考资料上下文。"""
    parts = []
    used = 0
    for i, c in enumerate(chunks, 1):
        text = c["text"][:1200]
        if used + len(text) > max_chars:
            text = text[: max_chars - used]
        parts.append(f"[{i}] {text}")
        used += len(text)
        if used >= max_chars:
            break
    ctx = "\n\n".join(parts)
    if graph_ctx.get("triples"):
        trip = "\n".join(f"- {t}" for t in graph_ctx["triples"][:40])
        ctx += f"\n\n【知识图谱三元组（文档中抽取的事实）】\n{trip}"
    return ctx


def refs_payload(chunks, graph_ctx):
    """引用来源元数据（前端展示用）。"""
    conn = db.get_conn()
    refs = []
    for i, c in enumerate(chunks, 1):
        doc_id = c.get("doc_id")
        if doc_id:
            d = conn.execute(
                "SELECT filename FROM documents WHERE id=?", (doc_id,)
            ).fetchone()
            doc = d["filename"] if d else f"文档#{doc_id}"
        else:
            doc = "图谱洞察"
        refs.append({
            "n": i,
            "doc": doc,
            "score": round(float(c.get("score", 0)), 4),
            "text": c["text"][:600],
            "chunk_id": c.get("chunk_id"),
        })
    return refs


async def answer_stream_events(query: str, history: list, mode_override: str = None,
                              kb_id: int = None):
    """生成器：依次 yield SSE 事件 dict。
    事件：meta（引用/模式）→ 多个 delta → done / error
    kb_id 用于限定本次问答检索的知识库范围。
    """
    cfg = get_settings()
    mode = mode_override or cfg.get("retrieval", {}).get("mode", "hybrid")

    # 纯模型问答：不走检索，直接用模型自身知识回答
    if mode == "natural":
        yield {
            "event": "meta",
            "data": {
                "mode": mode,
                "refs": [],
                "graph_entities": [],
                "graph_triples": [],
                "search_query": None,
            },
        }
        messages = [{"role": "system", "content": NATURAL_SYSTEM_PROMPT}]
        for h in history[-6:]:
            messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": query})
        try:
            stream = await llm_chat(messages, stream=True, temperature=0.3)
            async for piece in stream:
                yield {"event": "delta", "data": {"text": piece}}
        except ProviderError as e:
            yield {"event": "error", "data": {"message": f"LLM 调用失败：{e}"}}
            return
        yield {"event": "done", "data": {}}
        return

    # 多轮对话：先把当前问题改写成可独立检索的问句（指代消解）
    search_query = await rewrite_query(query, history)
    try:
        chunks, graph_ctx = await retrieve(search_query, mode_override=mode_override, kb_id=kb_id)
    except ProviderError as e:
        yield {"event": "error", "data": {"message": f"检索失败：{e}"}}
        return

    refs = refs_payload(chunks, graph_ctx)
    yield {
        "event": "meta",
        "data": {
            "mode": mode,
            "refs": refs,
            "graph_entities": graph_ctx.get("entities", []),
            "graph_triples": graph_ctx.get("triples", [])[:20],
            "search_query": search_query if search_query != query else None,
        },
    }

    if not chunks and not graph_ctx.get("triples"):
        msg = "知识库中暂无与该问题相关的信息。请先上传文档，或尝试更换检索模式 / 关键词。"
        yield {"event": "delta", "data": {"text": msg}}
        yield {"event": "done", "data": {}}
        return

    ctx = build_context(chunks, graph_ctx)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for h in history[-6:]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({
        "role": "user",
        "content": f"【参考资料】\n{ctx}\n\n【用户问题】\n{query}",
    })

    full = []
    try:
        stream = await llm_chat(messages, stream=True, temperature=0.3)
        async for piece in stream:
            full.append(piece)
            yield {"event": "delta", "data": {"text": piece}}
    except ProviderError as e:
        if full:
            yield {"event": "error", "data": {"message": f"生成中断：{e}"}}
        else:
            yield {"event": "error", "data": {"message": f"LLM 调用失败：{e}"}}
        return
    yield {"event": "done", "data": {}}
