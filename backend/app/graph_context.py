"""知识图谱可读化：把子图渲染成 AI 能直接理解的自然语言叙述。

对标 Cognee 的 GRAPH_COMPLETION：单纯把三元组平铺给模型，模型只能「看到边」；
本模块把图结构翻译成 实体卡片 + 多跳关系路径 + 概念社群，让模型能沿路径推理，
并在回答里区分「原文直述」与「多跳推出」。
"""
import networkx as nx

from . import db
from . import graph as kg
from .providers import ProviderError

MAX_NARRATIVE_CHARS = 3200
MAX_TRIPLES = 40
MAX_PATHS = 8
MAX_ENTITIES = 18
MAX_COMMUNITIES = 4


def _load_entities(ids) -> dict:
    """批量取实体详情，避免逐条查询。"""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = db.get_conn().execute(
        f"SELECT id,name,type,description,mention_count FROM entities WHERE id IN ({marks})",
        tuple(ids),
    ).fetchall()
    return {r["id"]: dict(r) for r in rows}


def _fetch_subgraph(seed_ids, kb_id, hops: int):
    """从种子实体出发逐跳收集边与节点，返回 (edges, node_ids)。

    与 graph_search 的差别：这里保留方向与边上的多条关系，用于生成可读路径。
    """
    conn = db.get_conn()
    kb_sub = "(SELECT id FROM documents WHERE kb_id=?)"
    frontier = set(seed_ids)
    visited = set(seed_ids)
    edges, node_ids = [], set(seed_ids)
    for _ in range(max(1, min(hops, 3))):
        if not frontier:
            break
        marks = ",".join("?" * len(frontier))
        args = tuple(frontier) * 2
        sql = (
            f"SELECT head_id,tail_id,relation,chunk_id,kind,inferred FROM relations "
            f"WHERE (head_id IN ({marks}) OR tail_id IN ({marks}))"
        )
        if kb_id is not None:
            sql += f" AND doc_id IN {kb_sub}"
            args = args + (kb_id,)
        rows = conn.execute(sql + " LIMIT 1200", args).fetchall()
        next_frontier = set()
        for r in rows:
            edges.append(dict(r))
            for eid in (r["head_id"], r["tail_id"]):
                node_ids.add(eid)
                if eid not in visited:
                    next_frontier.add(eid)
        visited |= next_frontier
        frontier = next_frontier
    return edges, node_ids


def _build_digraph(edges) -> nx.DiGraph:
    G = nx.DiGraph()
    for e in edges:
        h, t = e["head_id"], e["tail_id"]
        if h == t:
            continue
        if G.has_edge(h, t):
            bucket = G[h][t].setdefault("relations", [])
            if e["relation"] not in bucket:
                bucket.append(e["relation"])
            if e.get("chunk_id") and e["chunk_id"] not in G[h][t].setdefault("chunks", []):
                G[h][t]["chunks"].append(e["chunk_id"])
            continue
        G.add_edge(
            h, t,
            relations=[e["relation"]],
            kind=e.get("kind") or "extracted",
            chunks=[e["chunk_id"]] if e.get("chunk_id") else [],
        )
    return G


def _chain_text(chain, names: dict) -> str:
    """[(a, rel, b), ...] → "A --rel--> B --rel--> C"。"""
    if not chain:
        return ""
    parts = [names.get(chain[0][0], "?")]
    for _src, rel, dst in chain:
        parts.append(f"--{rel}-->")
        parts.append(names.get(dst, "?"))
    return " ".join(parts)


def _relation_paths(G: nx.DiGraph, seeds, names: dict, max_paths: int = MAX_PATHS,
                    max_len: int = 3) -> list:
    """从种子实体沿出边展开多跳路径，优先走度数高的节点（信息量更大）。"""
    paths, seen = [], set()
    for s in seeds:
        if s not in G:
            continue
        stack = [(s, [], frozenset([s]))]
        while stack and len(paths) < max_paths:
            node, chain, visited = stack.pop()
            succs = [(n, d) for _u, n, d in G.out_edges(node, data=True) if n not in visited]
            if not succs or len(chain) >= max_len:
                text = _chain_text(chain, names)
                key = text
                if text and key not in seen:
                    seen.add(key)
                    paths.append({"text": text, "hops": len(chain), "nodes": [c[0] for c in chain] + ([chain[-1][2]] if chain else [])})
                continue
            # 列表尾部先弹出 → 先走度数高的分支，保证有限预算下拿到主干路径
            succs.sort(key=lambda x: G.out_degree(x[0]))
            for n, d in succs[-3:]:
                rels = d.get("relations") or ["关联"]
                stack.append((n, chain + [(node, rels[0], n)], visited | {n}))
    paths.sort(key=lambda p: -p["hops"])
    return paths[:max_paths]


def _communities(G: nx.DiGraph, names: dict, seeds: set) -> list:
    """无向化后做贪心模块度聚类，返回与问题相关的社群（含核心实体）。"""
    UG = nx.Graph(G)
    if UG.number_of_nodes() < 6:
        return []
    try:
        raw = list(nx.community.greedy_modularity_communities(UG))
    except Exception:
        return []
    raw.sort(key=lambda c: -len(c))
    out = []
    for comm in raw[:MAX_COMMUNITIES]:
        members = list(comm)
        if not any(m in seeds for m in members) and seeds:
            # 与问题无关的社群不进入上下文，避免噪声
            continue
        core = sorted(members, key=lambda n: -UG.degree(n))[:3]
        out.append({
            "size": len(members),
            "core": [names.get(n, "?") for n in core],
            "members": [names.get(n, "?") for n in members[:10]],
            "related": any(m in seeds for m in members),
        })
    return out


def _render(seed_names, cards, paths, communities, note_block, stats) -> str:
    """把结构化结果渲染为自然语言。模型对「有主语的句子」比裸三元组更敏感。

    小标题不用「一、二、」编号：某些区块（如社群）可能为空，
    固定编号会出现跳号，反而让模型误以为内容缺失。
    """
    if not cards and not paths:
        return ""
    lines = ["【知识图谱解读】"]
    if seed_names:
        lines.append("问题直接涉及的实体：" + "、".join(seed_names[:8]) + "。")
    if cards:
        lines.append("◆ 实体档案")
        for c in cards[:MAX_ENTITIES]:
            desc = (c.get("description") or "").strip()
            line = f"- {c['name']}（{c['type'] or '未分类'}，关联 {c['degree']} 条关系）"
            if desc:
                line += f"：{desc}"
            if c.get("aliases"):
                line += f"（别名：{'、'.join(c['aliases'][:3])}）"
            lines.append(line)
    if paths:
        lines.append("◆ 可推理的关系路径")
        for p in paths:
            lines.append(f"- {p['text']}" + ("（多跳）" if p["hops"] > 1 else ""))
    if communities:
        lines.append("◆ 概念社群")
        for i, c in enumerate(communities, 1):
            lines.append(
                f"- 社群{i}（{c['size']} 个实体，核心：{'、'.join(c['core'])}）："
                f"{'、'.join(c['members'])}"
            )
    if note_block:
        lines.append(note_block)
    if stats:
        lines.append(
            f"（图谱规模：{stats.get('entities', 0)} 个实体 / {stats.get('relations', 0)} 条关系）"
        )
    text = "\n".join(lines)
    return text[:MAX_NARRATIVE_CHARS]


def _fallback_seeds(query: str, kb_id):
    """LLM 不可用时的实体链接兜底：按名称/别名做子串匹配。"""
    hits = []
    for kw in [query.strip()[:20]]:
        if kw:
            hits.extend(db.search_entities(kw, limit=8))
    uniq = {}
    for h in hits:
        uniq[h["id"]] = h
    if kb_id is not None:
        in_kb = db.entity_ids_in_kb(kb_id)
        uniq = {k: v for k, v in uniq.items() if k in in_kb}
    return list(uniq.values())


async def read_graph(query: str = "", kb_id: int = None, hops: int = 2,
                     max_chunks: int = 6) -> dict:
    """图谱可读化检索：返回叙述文本 + 三元组 + 引用分块，结构与 graph_search 兼容。

    与 graph_search 的分工：graph_search 负责「找到原文分块」，本函数负责
    「让模型读懂图」，两者在 hybrid 模式下互补。
    """
    usage = None
    ents = []
    if query:
        try:
            ents, usage = await kg.link_entities(query, kb_id=kb_id)
        except ProviderError:
            ents = _fallback_seeds(query, kb_id)
    if not ents and query:
        ents = _fallback_seeds(query, kb_id)

    seed_ids = [e["id"] for e in ents]
    if not seed_ids:
        # 无明确实体时给「全库图景」一个起点：取图上最活跃的实体，
        # 这样「总结一下这张知识图谱」这类问题也能得到结构化叙述。
        seed_ids = db.top_entity_ids(kb_id, limit=5)
    edges, node_ids = _fetch_subgraph(seed_ids, kb_id, hops)
    if not edges:
        return {
            "chunks": [], "triples": [], "entities": [], "paths": [],
            "communities": [], "narrative": "", "usage": usage,
        }

    G = _build_digraph(edges)
    UG = nx.Graph(G)
    ent_map = _load_entities(node_ids)
    names = {i: e["name"] for i, e in ent_map.items()}

    # 度数取无向图：有向图里「被引用的实体」出度可能为 0，会漏掉重要枢纽
    degree = dict(UG.degree())
    ordered = sorted(G.nodes(), key=lambda n: (-degree.get(n, 0), n))
    focus = [n for n in seed_ids if n in G] or ordered
    card_ids = []
    for n in focus + ordered:
        if n not in card_ids and n in ent_map:
            card_ids.append(n)
        if len(card_ids) >= MAX_ENTITIES:
            break

    cards = []
    for n in card_ids:
        e = ent_map[n]
        cards.append({
            "id": n,
            "name": e["name"],
            "type": e.get("type") or "",
            "description": e.get("description") or "",
            "degree": degree.get(n, 0),
            "aliases": db.entity_aliases_list(n),
        })

    paths = _relation_paths(G, focus, names, max_paths=MAX_PATHS)
    communities = _communities(G, names, set(focus))

    triples = []
    for h, t, d in G.edges(data=True):
        if len(triples) >= MAX_TRIPLES:
            break
        rel = (d.get("relations") or ["关联"])[0]
        suffix = "（推断）" if d.get("kind") == "inferred" else ""
        if d.get("kind") == "wikilink":
            suffix = "（笔记双链）"
        triples.append(f"{names.get(h, '?')} --{rel}--> {names.get(t, '?')}{suffix}")

    # 路径上的原文分块：优先种子实体直接关联的块，其次路径命中的块
    chunk_ids, seen = [], set()
    for n in focus:
        for _u, _v, d in list(G.out_edges(n, data=True)) + list(G.in_edges(n, data=True)):
            for cid in d.get("chunks") or []:
                if cid not in seen:
                    seen.add(cid)
                    chunk_ids.append(cid)
    chunks = []
    if chunk_ids:
        marks = ",".join("?" * len(chunk_ids[:max_chunks]))
        rows = db.get_conn().execute(
            f"SELECT id,doc_id,text FROM chunks WHERE id IN ({marks})",
            tuple(chunk_ids[:max_chunks]),
        ).fetchall()
        row_map = {r["id"]: r for r in rows}
        for rank, cid in enumerate(chunk_ids[:max_chunks]):
            r = row_map.get(cid)
            if r:
                chunks.append({
                    "chunk_id": r["id"], "doc_id": r["doc_id"],
                    "text": r["text"], "score": float(max_chunks - rank),
                })

    from . import notes as note_mod
    note_block = note_mod.graph_notes_summary(kb_id, limit=8)
    stats = db.graph_stats(kb_id)
    seed_names = [ent_map[i]["name"] for i in [e["id"] for e in ents] if i in ent_map]
    narrative = _render(seed_names, cards, paths, communities, note_block, stats)
    return {
        "chunks": chunks,
        "triples": triples,
        "entities": seed_names,
        "paths": paths,
        "communities": communities,
        "cards": cards,
        "narrative": narrative,
        "stats": stats,
        "usage": usage,
    }


def render_subgraph_brief(edges, node_ids, kb_id=None, max_chars: int = 1200) -> str:
    """把给定边集合渲染成简短叙述（供已有 graph_search 结果复用，不额外查库）。

    hybrid 模式下 graph_search 已经拿到了子图边，无需重跑一遍 BFS/LLM，
    这里只做「结构化 → 自然语言」的转换。
    """
    G = _build_digraph(edges)
    if not G.number_of_nodes():
        return ""
    ent_map = _load_entities(set(node_ids))
    names = {i: e["name"] for i, e in ent_map.items()}
    UG = nx.Graph(G)
    degree = dict(UG.degree())
    ordered = sorted(G.nodes(), key=lambda n: -degree.get(n, 0))[:8]
    cards = [
        {
            "name": ent_map[n]["name"],
            "type": ent_map[n].get("type") or "",
            "description": ent_map[n].get("description") or "",
            "degree": degree.get(n, 0),
            "aliases": db.entity_aliases_list(n),
        }
        for n in ordered if n in ent_map
    ]
    paths = _relation_paths(G, ordered[:3], names, max_paths=5)
    return _render([], cards, paths, [], "", None)[:max_chars]