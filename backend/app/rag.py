"""RAG 编排：检索（向量/图谱/图谱解读/混合/联网）→ 重排 → 带引用的流式生成。"""
import asyncio
import json
import re
import time
from . import db
from . import notes as note_mod
from . import graph_context as gc
from .config import get_settings
from .providers import llm_chat, rerank, ProviderError, estimate_tokens, estimate_messages_tokens
from .vectorstore import vector_search
from . import graph as kg

SYSTEM_PROMPT = """你是「知识库问答助手」，必须严格依据下面提供的参考资料回答用户问题。

要求：
1. 优先使用参考资料中的信息；在句末用 [编号] 标注引用来源，如：……属于核心模块 [2]
2. 参考资料中可能包含「知识图谱解读」：实体档案、多跳关系路径与概念社群。
   请沿路径做多跳推理，推断性结论要写明推理链（如：据 A→B→C 可推知），
   并与原文直述区分开，不要编造图中不存在的边
3. 参考资料中可能包含「反向链接」：其他笔记引用本笔记的语境。可据此说明该知识点
   在知识网络中的位置与相互影响
4. 参考资料中可能包含联网搜索结果（会标注网页地址），请综合使用并注意信息时效性
5. 若参考资料不足以回答，请明确说明"参考资料中暂无相关信息"，不要编造
6. 用简体中文回答，结构清晰；涉及多个要点时使用列表
"""

WEB_SYSTEM_PROMPT = """你是「联网问答助手」。请严格依据下面提供的联网搜索结果回答用户问题。

要求：
1. 综合多条网页结果，在句末用 [编号] 标注引用来源
2. 网页信息可能过时或相互矛盾，请优先采用更具体、更新的内容，并说明不确定性
3. 若搜索结果不足以回答，请明确说明"联网搜索未找到足够信息"，不要编造
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


async def rewrite_query(query: str, history: list):
    """多轮对话指代消解：结合历史把当前问题改写为可独立检索的问句。
    返回 (改写后问题, usage)；失败时返回原文。"""
    recent = [
        h for h in (history or [])
        if h.get("role") in ("user", "assistant") and (h.get("content") or "").strip()
    ]
    if not recent:
        return query, None
    lines = []
    for h in recent[-6:]:
        role = "用户" if h["role"] == "user" else "助手"
        lines.append(f"{role}：{h['content'][:300]}")
    try:
        text, usage = await llm_chat(
            [{
                "role": "user",
                "content": REWRITE_PROMPT.format(history="\n".join(lines), query=query),
            }],
            stream=False,
            temperature=0.0,
            max_tokens=200,
            return_usage=True,
        )
    except ProviderError:
        return query, None
    text = (text or "").strip().strip('"“”').strip()
    # 异常输出（过长/空/换行堆叠）时退回原问题（调用本身已消耗，usage 照常返回）
    if not text or len(text) > 200 or text.count("\n") > 2:
        return query, usage
    return text, usage


WEB_QUERY_PROMPT = """你是联网搜索查询构造助手。请把用户问题改写成适合搜索引擎的中文关键词查询。

规则：
1. 若问题中有“这篇文章/它/上述内容”等指代，请结合【检索到的资料】判断具体主题（文章标题、专有名词），并把主题写进查询
2. 输出 3~8 个关键词，用空格分隔；不要问号、标点、序号或解释
3. 保留专有名词原文（人名、产品名、作品名）
4. 问题在问来源/出处/仿写时，保留“仿写/出处”等意图词
5. 只输出查询本身

【用户问题】
{query}

【检索到的资料】
{context}

搜索查询："""

# 兜底去词：疑问词、指代词和虚词，避免把整句口语直接丢给搜索引擎
_WEB_STOPWORDS = re.compile(
    r"(请问|帮我|一下|这篇文章|该文章|本文|这篇|这段|这个|那个|这些|那些|"
    r"是什么|是哪个|是哪些|为什么|怎么样|怎么|如何|是否|是|的|了|吗|呢|吧|啊|呀|"
    r"与|和|及|或|在|有|我|你|他|她|它|们|"
    r"[？?！!，,。.、；;：:\"“”《》()（）\s]+)"
)


def _query_context(chunks, limit: int = 2, chars: int = 400) -> str:
    """取本地检索到的前几段资料，用于消解问题中的指代。"""
    parts = []
    for c in chunks or []:
        if c.get("source") == "web":
            continue
        text = (c.get("text") or "").strip()
        if text:
            parts.append(text[:chars])
        if len(parts) >= limit:
            break
    return "\n".join(parts)


def _clean_search_query(text: str) -> str:
    """清洗模型输出的搜索词：只取一行、去标点包装、拒绝异常输出。"""
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    line = line.strip().strip('"“”『』「」`').strip()
    line = re.sub(r"^(搜索查询|查询|关键词)[:：]\s*", "", line)
    line = re.sub(r"\s+", " ", line).strip()
    if not line or len(line) > 80:
        return ""
    if any(w in line for w in ("抱歉", "无法", "不能", "作为AI")):
        return ""
    return line


def _fallback_web_query(question: str, chunks) -> str:
    """模型不可用时的兜底：去掉疑问/指代词；若问题有指代，补上资料里的小标题。"""
    q = _WEB_STOPWORDS.sub(" ", question or "")
    q = re.sub(r"\s+", " ", q).strip() or (question or "").strip()
    title = ""
    if re.search(r"(这篇文章|该文章|本文|这篇|这段|它)", question or ""):
        for line in _query_context(chunks, limit=1, chars=300).splitlines():
            s = line.strip()
            if (2 <= len(s) <= 20 and not re.search(r"[，。！？!?：:；;]", s)
                    and "引用" not in s and "思考" not in s):
                title = s
                break
    if title and title not in q:
        q = f"{title} {q}".strip()
    return (q or (question or "").strip())[:80]


async def build_web_query(query: str, chunks) -> tuple:
    """构造联网搜索关键词：结合本地检索资料，把「这篇文章」等指代换成具体主题。

    返回 (搜索词, usage)；模型不可用或输出异常时退回规则兜底。
    """
    ctx = _query_context(chunks)
    if not ctx:
        return _fallback_web_query(query, chunks), None
    try:
        text, usage = await asyncio.wait_for(
            llm_chat(
                [{"role": "user", "content": WEB_QUERY_PROMPT.format(query=query, context=ctx)}],
                stream=False,
                temperature=0.0,
                max_tokens=60,
                return_usage=True,
            ),
            timeout=15,  # 查询改写不能拖慢联网搜索；超时直接用规则兜底
        )
    except (ProviderError, asyncio.TimeoutError):
        return _fallback_web_query(query, chunks), None
    return (_clean_search_query(text) or _fallback_web_query(query, chunks)), usage


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


def _add_usage(dst: dict, src: dict | None):
    """把一次模型调用的 usage 累加进汇总字典。"""
    if not src:
        return
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        dst[k] = dst.get(k, 0) + int(src.get(k) or 0)
    if src.get("estimated"):
        dst["estimated"] = True


def _new_usage() -> dict:
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "estimated": False}


async def retrieve(query: str, mode_override: str = None, kb_id: int = None, web: bool = False):
    """检索 + 可选重排。返回 (chunks, graph_ctx, aux_usage, web_ctx)。
    模式：vector 纯向量 / graph 纯图谱 / graph_read 图谱解读（实体卡片+路径+社群）/
          hybrid 混合（RRF 融合）/ insights 图谱洞察（社群+中心性分析）/
          natural 纯模型（不检索）/ web 仅联网搜索（由「纯模型问答 + 联网搜索」组合进入）。
    指定 kb_id 时仅在该知识库范围内检索；web=True 时额外抓取网页结果并追加到 chunks。
    graph_ctx 额外携带 narrative（图谱自然语言解读）与 notes（笔记反链接上下文）。
    """
    cfg = get_settings()
    rcfg = cfg.get("retrieval", {})
    mode = mode_override or rcfg.get("mode", "hybrid")
    top_k = int(rcfg.get("top_k", 6))
    hops = int(rcfg.get("graph_hops", 2))

    graph_ctx = {"triples": [], "entities": [], "narrative": "", "notes": "", "paths": []}
    web_ctx = {"enabled": bool(web), "count": 0, "error": "", "query": ""}
    aux = _new_usage()
    if mode == "natural" and not web:
        return [], graph_ctx, aux, web_ctx
    if mode == "natural":
        mode = "web"
    if mode == "web":
        chunks = []
    elif mode == "vector":
        vec_hits, _, u = await vector_search(query, top_k=top_k, kb_id=kb_id)
        _add_usage(aux, u)
        chunks = vec_hits
    elif mode == "graph":
        g = await kg.graph_search(query, hops=hops, top_k=top_k, kb_id=kb_id)
        _add_usage(aux, g.get("usage"))
        graph_ctx = {
            "triples": g["triples"], "entities": g["entities"],
            "narrative": gc.render_subgraph_brief(
                g.get("edges") or [], g.get("node_ids") or [], kb_id
            ),
            "paths": [], "notes": "",
        }
        chunks = g["chunks"]
    elif mode == "graph_read":
        # 图谱解读：向量兜底 + 子图叙述化。只做一次实体链接（在 read_graph 内部）
        vec_hits, _, u = await vector_search(query, top_k=top_k, kb_id=kb_id)
        _add_usage(aux, u)
        g = await gc.read_graph(query, kb_id=kb_id, hops=hops, max_chunks=top_k + 2)
        _add_usage(aux, g.get("usage"))
        graph_ctx = {
            "triples": g["triples"], "entities": g["entities"],
            "narrative": g.get("narrative") or "",
            "paths": g.get("paths") or [], "notes": "",
        }
        chunks = _rrf_merge([vec_hits, g.get("chunks") or []])[: top_k + 2]
    elif mode == "insights":
        g = await kg.graph_insights(query, hops=hops, top_k=top_k, kb_id=kb_id)
        _add_usage(aux, g.get("usage"))
        graph_ctx = {
            "triples": g["triples"], "entities": g["entities"],
            "narrative": "", "paths": [], "notes": "",
        }
        chunks = g["chunks"]
    else:
        vec_hits, _, u = await vector_search(query, top_k=top_k, kb_id=kb_id)
        _add_usage(aux, u)
        g = await kg.graph_search(query, hops=hops, top_k=top_k, kb_id=kb_id)
        _add_usage(aux, g.get("usage"))
        graph_ctx = {
            "triples": g["triples"], "entities": g["entities"],
            "narrative": gc.render_subgraph_brief(
                g.get("edges") or [], g.get("node_ids") or [], kb_id
            ),
            "paths": [], "notes": "",
        }
        chunks = _rrf_merge([vec_hits, g["chunks"]])[: top_k + 3]

    # 在线图谱补全：为图谱上下文补充 LLM 推理出的隐含关系
    if cfg.get("graph", {}).get("completion") and mode in ("graph", "graph_read", "hybrid", "insights"):
        inferred, u = await kg.complete_subgraph(query, graph_ctx["triples"])
        _add_usage(aux, u)
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

    # 联网搜索放在本地检索与重排之后：网页结果不参与向量/图谱排序，
    # 但会作为额外引用追加进上下文。搜索词先结合本地资料消解「这篇文章」等指代，
    # 避免把整句口语丢给搜索引擎导致只命中首个词（如只搜「这」）。
    if web:
        from . import websearch
        web_query, wq_usage = await build_web_query(query, chunks)
        _add_usage(aux, wq_usage)
        web_ctx["query"] = web_query
        try:
            got = await asyncio.wait_for(
                websearch.search_and_fetch(web_query, limit=5, pages=3, max_chars=1500),
                timeout=25,
            )
            web_chunks = got.get("chunks") or []
            chunks = chunks + web_chunks
            web_ctx["count"] = len(web_chunks)
        except Exception as e:  # 网络不可用/被限流时降级为仅知识库回答
            web_ctx["error"] = (str(e).strip() or type(e).__name__)[:200]

    # Obsidian 反链接：命中的笔记要带上「谁引用了它、它链接到谁」，
    # 让模型理解知识点在知识网络中的位置，而不是孤立段落。
    if mode != "web":
        graph_ctx["notes"] = note_mod.context_for_chunks(chunks, kb_id)
    return chunks, graph_ctx, aux, web_ctx


def build_context(chunks, graph_ctx, max_chars=6000):
    """构建带编号的参考资料上下文。

    顺序即优先级：图谱解读在最前（模型先读到结构化事实，多跳推理更稳），
    其次是带编号的原文分块（引用编号与前端一一对应），最后是三元组与笔记反链接。
    """
    parts = []
    narrative = (graph_ctx or {}).get("narrative") or ""
    if narrative:
        parts.append(narrative[:1800])
    used = 0
    for i, c in enumerate(chunks, 1):
        is_web = c.get("source") == "web"
        text = c["text"][:1500 if is_web else 1200]
        if is_web and c.get("url"):
            text = f"（网页来源：{c['url']}）\n{text}"
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
    if (graph_ctx or {}).get("notes"):
        ctx += "\n\n" + graph_ctx["notes"][:2000]
    return ctx


def refs_payload(chunks, graph_ctx):
    """引用来源元数据（前端展示用）。"""
    conn = db.get_conn()
    refs = []
    for i, c in enumerate(chunks, 1):
        doc_id = c.get("doc_id")
        if c.get("source") == "web":
            doc = c.get("title") or c.get("url") or "网页"
        elif doc_id:
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
            "url": c.get("url"),
            "source": c.get("source"),
        })
    return refs


def _compose_stats(model_name: str, messages, answer_text: str, usage: dict | None,
                   t0: float, ttft_ms) -> dict:
    """汇总回答生成的 token 用量与速率。上游未返回 usage 时按字符数估算。"""
    if usage:
        p = int(usage.get("prompt_tokens") or 0)
        c = int(usage.get("completion_tokens") or 0)
        est = bool(usage.get("estimated"))
    else:
        p, c, est = estimate_messages_tokens(messages), estimate_tokens(answer_text), True
    now = time.perf_counter()
    latency_ms = round((now - t0) * 1000)
    gen_ms = None if ttft_ms is None else max(1, latency_ms - ttft_ms)
    tps = None
    if gen_ms and c > 0:
        tps = round(c / (gen_ms / 1000), 1)
    return {
        "model": model_name,
        "prompt_tokens": p,
        "completion_tokens": c,
        "total_tokens": p + c,
        "estimated": est,
        "ttft_ms": ttft_ms,
        "gen_ms": gen_ms,
        "latency_ms": latency_ms,
        "tps": tps,
    }


async def answer_stream_events(query: str, history: list, mode_override: str = None,
                              kb_id: int = None, web: bool = False):
    """生成器：依次 yield SSE 事件 dict。
    事件：meta（引用/模式）→ 多个 delta → stats（模型用量与速率）→ done / error
    kb_id 用于限定本次问答检索的知识库范围；web=True 时启用免费联网搜索。
    """
    cfg = get_settings()
    mode = mode_override or cfg.get("retrieval", {}).get("mode", "hybrid")
    model_name = (cfg.get("llm") or {}).get("model") or "?"
    t_total0 = time.perf_counter()

    # 纯模型问答：不走检索，直接用模型自身知识回答。
    # 若同时开启联网搜索，则退化为「仅联网模式」，把网页结果作为参考资料。
    if mode == "natural" and not web:
        yield {
            "event": "meta",
            "data": {
                "mode": mode,
                "refs": [],
                "graph_entities": [],
                "graph_triples": [],
                "graph_narrative": "",
                "graph_paths": [],
                "notes": "",
                "search_query": None,
            },
        }
        messages = [{"role": "system", "content": NATURAL_SYSTEM_PROMPT}]
        for h in history[-6:]:
            messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": query})
        t0 = time.perf_counter()
        ttft_ms, usage, full = None, None, []
        try:
            stream = await llm_chat(messages, stream=True, temperature=0.3)
            async for piece, u in stream:
                # 只在真正收到正文时记首字延迟：仅含 usage 的空分块不算首字
                if ttft_ms is None and piece:
                    ttft_ms = round((time.perf_counter() - t0) * 1000)
                if u:
                    usage = u
                full.append(piece)
                yield {"event": "delta", "data": {"text": piece}}
        except ProviderError as e:
            yield {"event": "error", "data": {"message": f"LLM 调用失败：{e}"}}
            return
        stats = _compose_stats(model_name, messages, "".join(full), usage, t0, ttft_ms)
        yield {"event": "stats", "data": stats}
        yield {"event": "done", "data": {}}
        return

    # 「纯模型问答 + 联网搜索」：只用网页结果，不查本地知识库
    effective_mode = "web" if mode == "natural" else mode

    # 多轮对话：先把当前问题改写成可独立检索的问句（指代消解）
    aux = _new_usage()
    search_query, rw_usage = await rewrite_query(query, history)
    _add_usage(aux, rw_usage)
    try:
        chunks, graph_ctx, ret_usage, web_ctx = await retrieve(
            search_query, mode_override=effective_mode, kb_id=kb_id, web=web
        )
        _add_usage(aux, ret_usage)
    except ProviderError as e:
        yield {"event": "error", "data": {"message": f"检索失败：{e}"}}
        return

    refs = refs_payload(chunks, graph_ctx)
    yield {
        "event": "meta",
        "data": {
            "mode": effective_mode,
            "refs": refs,
            "graph_entities": graph_ctx.get("entities", []),
            "graph_triples": graph_ctx.get("triples", [])[:20],
            "graph_narrative": (graph_ctx.get("narrative") or "")[:1600],
            "graph_paths": [p.get("text") for p in (graph_ctx.get("paths") or [])][:6],
            "notes": (graph_ctx.get("notes") or "")[:1200],
            "search_query": search_query if search_query != query else None,
            "web": web_ctx,
        },
    }

    if not chunks and not graph_ctx.get("triples") and not graph_ctx.get("narrative"):
        if web:
            msg = "知识库与联网搜索均未获取到有效结果"
            if web_ctx.get("error"):
                msg += f"（{web_ctx['error']}）"
            msg += "。请更换关键词，或关闭联网搜索后重试。"
        else:
            msg = "知识库中暂无与该问题相关的信息。请先上传文档，或尝试更换检索模式 / 关键词。"
        yield {"event": "delta", "data": {"text": msg}}
        # 无生成调用，但检索辅助调用的开销照常呈现
        yield {"event": "stats", "data": {
            "model": model_name, "prompt_tokens": 0, "completion_tokens": 0,
            "total_tokens": 0, "aux_tokens": aux["total_tokens"],
            "estimated": aux["estimated"], "ttft_ms": None, "gen_ms": None,
            "tps": None, "latency_ms": round((time.perf_counter() - t_total0) * 1000),
        }}
        yield {"event": "done", "data": {}}
        return

    # 联网结果通常需要更多上下文篇幅
    max_ctx = 9000 if any(c.get("source") == "web" for c in chunks) else 6000
    ctx = build_context(chunks, graph_ctx, max_chars=max_ctx)
    system_prompt = WEB_SYSTEM_PROMPT if effective_mode == "web" else SYSTEM_PROMPT
    messages = [{"role": "system", "content": system_prompt}]
    for h in history[-6:]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({
        "role": "user",
        "content": f"【参考资料】\n{ctx}\n\n【用户问题】\n{query}",
    })

    t0 = time.perf_counter()
    ttft_ms, usage, full = None, None, []
    try:
        stream = await llm_chat(messages, stream=True, temperature=0.3)
        async for piece, u in stream:
            # 只在真正收到正文时记首字延迟：仅含 usage 的空分块不算首字
            if ttft_ms is None and piece:
                ttft_ms = round((time.perf_counter() - t0) * 1000)
            if u:
                usage = u
            full.append(piece)
            yield {"event": "delta", "data": {"text": piece}}
    except ProviderError as e:
        if full:
            yield {"event": "error", "data": {"message": f"生成中断：{e}"}}
        else:
            yield {"event": "error", "data": {"message": f"LLM 调用失败：{e}"}}
        return
    stats = _compose_stats(model_name, messages, "".join(full), usage, t0, ttft_ms)
    stats["aux_tokens"] = aux["total_tokens"]
    if aux["estimated"]:
        stats["estimated"] = True
    yield {"event": "stats", "data": stats}
    yield {"event": "done", "data": {}}
