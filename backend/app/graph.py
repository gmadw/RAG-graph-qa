"""知识图谱：LLM 实体关系抽取、入库合并、查询时的实体链接与子图检索。"""
import itertools
import json

import networkx as nx

from . import db
from .config import get_settings
from .providers import llm_json, llm_chat, ProviderError

# 内置默认本体（未启用自定义本体时使用）
DEFAULT_ENTITY_TYPES = "人物/组织/公司/地点/技术/产品/概念/事件/时间等"
DEFAULT_RELATION_HINT = "简洁的动词或名词短语（如：任职于、位于、属于、开发了、支持）"

EXTRACT_TEMPLATE = """你是一个严谨的知识图谱信息抽取专家。请从给定文本中抽取知识三元组。

规则：
1. 每个三元组包含字段：head(头实体)、head_type(头实体类型)、relation(关系)、tail(尾实体)、tail_type(尾实体类型)
2. 实体名称必须简洁规范（如"张三"而非"张先生"）；{entity_rule}
3. 只抽取文本中明确表达的事实，禁止编造或推测
4. {relation_rule}
5. 抽取最重要的 3~12 个三元组；若文本无实质事实则返回 []
6. 严格只输出 JSON 数组，不要任何解释文字

示例输出：
[{{"head":"张三","head_type":"人物","relation":"任职于","tail":"阿里巴巴","tail_type":"公司"}},
 {{"head":"阿里巴巴","head_type":"公司","relation":"总部位于","tail":"杭州","tail_type":"地点"}}]

待抽取文本：
"""


def build_extract_prompt() -> str:
    """按自定义本体配置生成抽取 prompt（未启用则用内置默认本体）。"""
    onto = get_settings().get("ontology", {}) or {}
    ent = (onto.get("entity_types") or "").strip()
    rel = (onto.get("relation_types") or "").strip()
    if onto.get("enabled") and ent:
        entity_rule = f"head_type/tail_type 必须从以下类型中选择：{ent}"
    else:
        entity_rule = f"类型可为：{DEFAULT_ENTITY_TYPES}"
    if onto.get("enabled") and rel:
        relation_rule = f"relation 必须从以下关系中选择：{rel}"
    else:
        relation_rule = f"relation 使用{DEFAULT_RELATION_HINT}"
    return EXTRACT_TEMPLATE.format(entity_rule=entity_rule, relation_rule=relation_rule)

LINK_PROMPT = """从用户问题中提取用于知识图谱检索的关键实体或关键词。
规则：返回 JSON 数组，元素为字符串；最多 6 个；只输出 JSON，不要解释。
若问题中没有具体实体，返回最核心的 1-2 个关键词。

用户问题："""

ALIAS_PROMPT = """你是实体消解专家。下面是一批从文档中抽取的实体（JSON 数组，含 id/name/type/mention_count），
请找出指向同一个真实对象的不同写法（别名 / 缩写 / 中英文 / 全称简称）。

规则：
1. 只合并你高度确信是同一对象的名称，如 "阿里巴巴"/"阿里"/"Alibaba"
2. 类型不同或明显是不同对象的不要合并，如 "Java" 与 "JavaScript"
3. 输出 JSON 数组，每个元素为一组等价名称的列表（字符串），把最规范的名称放第一位
4. 若无等价组，返回 []
5. 严格只输出 JSON，不要解释

实体列表：
"""

DESCRIBE_PROMPT = """你是知识图谱构建助手。下面给出若干实体及其在文档中出现的相关文本片段（JSON 数组），
请为每个实体生成一句简洁的中文描述（不超过 60 字），概括它是什么、与什么相关。

规则：
1. 只依据给定文本片段总结，不要引入外部知识，不要编造
2. 输出 JSON 数组，元素为 {"name": 实体名, "description": "描述"}
3. 严格只输出 JSON，不要解释

实体与资料：
"""


def _norm_triples(raw):
    out = []
    if not isinstance(raw, list):
        return out
    for t in raw:
        if not isinstance(t, dict):
            continue
        head = str(t.get("head", "")).strip().strip('"“”').strip()
        tail = str(t.get("tail", "")).strip().strip('"“”').strip()
        rel = str(t.get("relation", "")).strip().strip('"“”').strip()
        if not head or not tail or not rel or len(head) > 60 or len(tail) > 60:
            continue
        out.append({
            "head": head,
            "head_type": str(t.get("head_type", "")).strip()[:20],
            "relation": rel[:40],
            "tail": tail,
            "tail_type": str(t.get("tail_type", "")).strip()[:20],
        })
    return out


async def resolve_entity(name: str, etype: str) -> int:
    """实体归并入库：先查别名表命中规范实体，再按名称精确匹配，最后新建。"""
    hit = db.resolve_entity_by_alias(name)
    if hit:
        db.bump_entity_mention(hit["id"], etype)
        return hit["id"]
    return db.upsert_entity(name, etype)


async def build_graph_for_doc(doc_id: int, force: bool = False) -> int:
    """对文档 chunk 抽取三元组并入库。返回本次新增的三元组数量。

    force=False（默认）：跳过已抽取过的 chunk，实现图谱增量构建；
    force=True：先清空该文档旧图谱再全量重建。
    """
    if force:
        db.clear_doc_relations(doc_id)  # 先清理旧数据，防止重建叠加
    conn = db.get_conn()
    prompt = build_extract_prompt()  # 每次构建只生成一次（可能含自定义本体）
    rows = conn.execute(
        "SELECT id, text FROM chunks WHERE doc_id=? ORDER BY chunk_index", (doc_id,)
    ).fetchall()
    done_ids = db.processed_chunk_ids(doc_id)
    total = 0
    for r in rows:
        chunk_id, text = r["id"], r["text"]
        if len(text) < 20 or chunk_id in done_ids:
            continue
        try:
            raw = await llm_json(
                [{"role": "user", "content": prompt + text}],
                max_tokens=1500,
            )
        except ProviderError:
            raise
        for t in _norm_triples(raw):
            hid = await resolve_entity(t["head"], t["head_type"])
            tid = await resolve_entity(t["tail"], t["tail_type"])
            db.insert_relation(hid, tid, t["relation"], chunk_id, doc_id)
            total += 1
    return total


async def detect_aliases_for_doc(doc_id: int) -> int:
    """LLM 识别同义实体并合并。返回被合并的实体数量。"""
    ids = db.doc_entity_ids(doc_id, limit=200)
    if len(ids) < 2:
        return 0
    conn = db.get_conn()
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id,name,type,mention_count FROM entities WHERE id IN ({marks})", tuple(ids)
    ).fetchall()
    names = [dict(r) for r in rows]
    try:
        raw = await llm_json(
            [{"role": "user", "content": ALIAS_PROMPT + json.dumps(names, ensure_ascii=False)}],
            max_tokens=1500,
        )
    except ProviderError:
        return 0
    if not isinstance(raw, list):
        return 0
    by_name = {n["name"]: n for n in names}
    merged = 0
    for group in raw:
        if not isinstance(group, list) or len(group) < 2:
            continue
        members = [by_name[g] for g in group if isinstance(g, str) and g in by_name]
        if len(members) < 2:
            continue
        # 规范实体取提及次数最多的；别名实体不得与规范实体同类不同物（已在 prompt 约束）
        canonical = max(members, key=lambda m: m.get("mention_count", 0))
        others = [m["id"] for m in members if m["id"] != canonical["id"]]
        db.merge_entities(canonical["id"], others)
        merged += len(others)
    return merged


async def describe_entities_for_doc(doc_id: int, limit: int = 60) -> int:
    """为文档相关且暂无描述的实体批量生成 LLM 描述。返回更新数量。"""
    conn = db.get_conn()
    ids = db.doc_entity_ids(doc_id, limit=limit)
    todo = []
    for eid in ids:
        row = conn.execute(
            "SELECT id,name,type,description FROM entities WHERE id=?", (eid,)
        ).fetchone()
        if row and not (row["description"] or "").strip():
            todo.append(dict(row))
    if not todo:
        return 0
    updated = 0
    batch = 8
    for i in range(0, len(todo), batch):
        part = todo[i : i + batch]
        payload = [
            {"name": e["name"], "type": e["type"], "texts": db.entity_evidence(e["id"], limit=3)}
            for e in part
        ]
        try:
            raw = await llm_json(
                [{"role": "user", "content": DESCRIBE_PROMPT + json.dumps(payload, ensure_ascii=False)}],
                max_tokens=1200,
            )
        except ProviderError:
            continue
        if not isinstance(raw, list):
            continue
        desc_map = {}
        for item in raw:
            if isinstance(item, dict) and item.get("name"):
                desc_map[str(item["name"]).strip()] = str(item.get("description", "")).strip()
        for e in part:
            d = desc_map.get(e["name"])
            if d:
                db.update_entity_description(e["id"], d)
                updated += 1
    return updated


async def link_entities(query: str, kb_id: int = None) -> list:
    """问题 → 实体链接（图数据库中匹配到的实体 id 列表）。
    指定 kb_id 时仅保留参与该知识库关系网络的实体，避免跨库串味。"""
    raw = await llm_json(
        [{"role": "user", "content": LINK_PROMPT + query}],
        max_tokens=200,
    )
    keywords = []
    if isinstance(raw, list):
        keywords = [str(k).strip() for k in raw if str(k).strip()][:6]
    if not keywords:
        # 退化：直接用问题做模糊匹配
        keywords = [query.strip()[:20]]
    matched_ids = {}
    for kw in keywords:
        for e in db.search_entities(kw, limit=8):
            matched_ids[e["id"]] = e
    matched = list(matched_ids.values())
    if kb_id is not None:
        in_kb = db.entity_ids_in_kb(kb_id)
        matched = [e for e in matched if e["id"] in in_kb]
    # 自定义本体开启"检索按类型过滤"时，仅保留配置类型内的实体
    onto = get_settings().get("ontology", {}) or {}
    if onto.get("enabled") and onto.get("filter_search"):
        types = _split_types(onto.get("entity_types"))
        if types:
            filtered = [e for e in matched if (e.get("type") or "") in types]
            if filtered:
                matched = filtered
    return matched


def _split_types(text: str) -> list:
    """本体类型字符串（换行/逗号分隔）→ 列表。"""
    return [t.strip() for t in (text or "").replace("，", ",").replace(",", "\n").split("\n") if t.strip()]


async def graph_search(query: str, hops: int = 2, top_k: int = 8, kb_id: int = None):
    """图检索：实体链接 → 子图扩展 → 收集关联 chunk 与三元组路径。
    指定 kb_id 时子图扩展与分块补充都限制在该知识库内。

    返回：
      chunks: [{chunk_id, doc_id, text, score}]
      triples: ["A --关系--> B", ...]
      entities: 匹配到的实体名列表
    """
    ents = await link_entities(query, kb_id=kb_id)
    if not ents:
        return {"chunks": [], "triples": [], "entities": []}

    conn = db.get_conn()
    kb_doc_sub = "(SELECT id FROM documents WHERE kb_id=?)"
    frontier = {e["id"]: e for e in ents}
    visited = set(frontier.keys())
    chunk_score = {}   # chunk_id -> score
    triples = []
    ent_names = {e["name"] for e in ents}

    for hop in range(max(1, min(hops, 3))):
        if not frontier:
            break
        marks = ",".join("?" * len(frontier))
        ids = tuple(frontier.keys())
        if kb_id is None:
            rows = conn.execute(
                f"SELECT head_id, tail_id, relation, chunk_id FROM relations "
                f"WHERE head_id IN ({marks}) OR tail_id IN ({marks}) LIMIT 400",
                ids + ids,
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT head_id, tail_id, relation, chunk_id FROM relations "
                f"WHERE (head_id IN ({marks}) OR tail_id IN ({marks})) "
                f"AND doc_id IN {kb_doc_sub} LIMIT 400",
                ids + ids + (kb_id,),
            ).fetchall()
        next_frontier = {}
        for r in rows:
            for eid, name_key in ((r["head_id"], "h"), (r["tail_id"], "t")):
                if eid not in visited:
                    row = conn.execute("SELECT id,name,type FROM entities WHERE id=?", (eid,)).fetchone()
                    if row:
                        next_frontier[eid] = dict(row)
                        visited.add(eid)
            h = conn.execute("SELECT name FROM entities WHERE id=?", (r["head_id"],)).fetchone()
            t = conn.execute("SELECT name FROM entities WHERE id=?", (r["tail_id"],)).fetchone()
            if h and t:
                triple_txt = f"{h['name']} --{r['relation']}--> {t['name']}"
                if triple_txt not in triples:
                    triples.append(triple_txt)
            if r["chunk_id"] is not None:
                # 起点实体的 chunk 权重高，扩展跳数衰减
                base = 1.0 if hop == 0 else 0.5 / hop
                chunk_score[r["chunk_id"]] = chunk_score.get(r["chunk_id"], 0.0) + base
        frontier = next_frontier
        if hop == 0 and len(chunk_score) >= top_k * 3:
            break

    # 补充：匹配实体（含别名）自身出现的 chunk（文本包含实体名或别名）
    for e in ents:
        names = [e["name"]] + db.entity_aliases_list(e["id"])
        for nm in names:
            if kb_id is None:
                rows = conn.execute(
                    "SELECT id FROM chunks WHERE text LIKE ? LIMIT 20", (f"%{nm}%",)
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT id FROM chunks WHERE text LIKE ? AND doc_id IN {kb_doc_sub} LIMIT 20",
                    (f"%{nm}%", kb_id),
                ).fetchall()
            for r in rows:
                chunk_score[r["id"]] = chunk_score.get(r["id"], 0.0) + 0.6

    if not chunk_score:
        return {"chunks": [], "triples": triples, "entities": sorted(ent_names)}

    ranked = sorted(chunk_score.items(), key=lambda x: -x[1])[:top_k]
    marks = ",".join("?" * len(ranked))
    rows = conn.execute(
        f"SELECT id, doc_id, text FROM chunks WHERE id IN ({marks})", tuple(c for c, _ in ranked)
    ).fetchall()
    row_map = {r["id"]: r for r in rows}
    chunks = []
    for cid, sc in ranked:
        r = row_map.get(cid)
        if r:
            chunks.append({"chunk_id": cid, "doc_id": r["doc_id"], "text": r["text"], "score": float(sc)})
    return {"chunks": chunks, "triples": triples, "entities": sorted(ent_names)}


# ---------------- 图谱补全（graph completion） ----------------

COMPLETE_PROMPT = """你是知识图谱补全专家。下面给出若干实体对及它们的共同邻居（JSON 数组），
请判断哪些实体对之间存在合理的隐含关系（原文未明说但可依据共同邻居推出的关系）。

规则：
1. 只补全你有充分把握的关系，不确定就跳过
2. 输出 JSON 数组，元素为 {"head": 实体A, "relation": 关系, "tail": 实体B}
3. 最多输出 10 条；若无合适关系返回 []
4. 严格只输出 JSON

候选实体对：
"""

COMPLETE_QUERY_PROMPT = """你是知识图谱补全专家。下面是用户问题与已检索到的知识三元组，
请推理出与问题相关、但三元组中未直接体现的隐含关系，用于补充回答上下文。

规则：
1. 只推理与问题相关的高置信度关系
2. 输出 JSON 数组，元素为 {{"head": 实体A, "relation": 关系, "tail": 实体B}}
3. 最多输出 6 条；若无合适关系返回 []
4. 严格只输出 JSON

【用户问题】
{query}

【已有三元组】
{triples}
"""


async def complete_graph_for_doc(doc_id: int, max_new: int = 20) -> int:
    """离线图谱补全：用共同邻居启发式找候选缺失边，交 LLM 确认后入库（inferred=1）。"""
    conn = db.get_conn()
    ids = db.doc_entity_ids(doc_id, limit=300)
    if len(ids) < 3:
        return 0
    kb_id = (db.get_document(doc_id) or {}).get("kb_id")
    marks = ",".join("?" * len(ids))
    if kb_id is None:
        rows = conn.execute(
            f"SELECT head_id,tail_id FROM relations WHERE head_id IN ({marks}) OR tail_id IN ({marks})",
            tuple(ids) * 2,
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT head_id,tail_id FROM relations WHERE (head_id IN ({marks}) OR tail_id IN ({marks})) "
            f"AND doc_id IN (SELECT id FROM documents WHERE kb_id=?)",
            tuple(ids) * 2 + (kb_id,),
        ).fetchall()
    G = nx.Graph()
    for r in rows:
        G.add_edge(r["head_id"], r["tail_id"])
    if G.number_of_nodes() < 3:
        return 0

    existing = {(min(r["head_id"], r["tail_id"]), max(r["head_id"], r["tail_id"])) for r in rows}
    candidates, seen = [], set()
    for n in list(G.nodes())[:80]:
        for u, v in itertools.combinations(sorted(G.neighbors(n)), 2):
            pair = (min(u, v), max(u, v))
            if pair in existing or pair in seen or u == v:
                continue
            seen.add(pair)
            candidates.append(pair)
        if len(candidates) >= 40:
            break
    if not candidates:
        return 0

    # 名称映射需覆盖全图节点：共同邻居可能不在候选实体对中
    node_ids = sorted(G.nodes())
    marks2 = ",".join("?" * len(node_ids))
    erows = conn.execute(
        f"SELECT id,name FROM entities WHERE id IN ({marks2})", tuple(node_ids)
    ).fetchall()
    name_map = {r["id"]: r["name"] for r in erows}
    payload = [
        {
            "head": name_map[a],
            "tail": name_map[b],
            "common": [name_map[c] for c in list(nx.common_neighbors(G, a, b))[:5]],
        }
        for a, b in candidates
        if a in name_map and b in name_map
    ]
    if not payload:
        return 0
    try:
        raw = await llm_json(
            [{"role": "user", "content": COMPLETE_PROMPT + json.dumps(payload, ensure_ascii=False)}],
            max_tokens=1500,
        )
    except ProviderError:
        return 0
    if not isinstance(raw, list):
        return 0

    added = 0
    for item in raw:
        if not isinstance(item, dict):
            continue
        h, rel, t = str(item.get("head", "")).strip(), str(item.get("relation", "")).strip(), str(item.get("tail", "")).strip()
        if not (h and rel and t):
            continue
        hid, tid = db.find_entity_by_name(h), db.find_entity_by_name(t)
        if not hid or not tid or hid == tid or db.relation_exists(hid, tid, rel):
            continue
        db.insert_relation(hid, tid, rel[:40], None, doc_id, inferred=1)
        added += 1
        if added >= max_new:
            break
    return added


async def complete_subgraph(query: str, triples: list) -> list:
    """在线图谱补全：基于检索到的三元组，推理与问题相关的隐含关系（不写入库）。"""
    if not triples:
        return []
    try:
        raw = await llm_json(
            [{
                "role": "user",
                "content": COMPLETE_QUERY_PROMPT.format(
                    query=query, triples="\n".join(triples[:40])
                ),
            }],
            max_tokens=800,
        )
    except ProviderError:
        return []
    out = []
    for item in raw or []:
        if isinstance(item, dict):
            h, rel, t = item.get("head"), item.get("relation"), item.get("tail")
            if h and rel and t:
                out.append(f"{h} --{rel}--> {t}（推断）")
        elif isinstance(item, str) and "--" in item:
            out.append(item.strip()[:120] + "（推断）")
    return out[:6]


# ---------------- 图谱洞察（insights） ----------------

async def graph_insights(query: str, hops: int = 2, top_k: int = 8, kb_id: int = None) -> dict:
    """图谱洞察：围绕问题实体做社群发现与中心性分析，产出结构化分析作为检索上下文。
    指定 kb_id 时仅分析该知识库的图谱子图。

    返回结构与 graph_search 一致（chunks 为洞察文本，chunk_id/doc_id 为 None/0）。
    """
    conn = db.get_conn()
    kb_doc_sub = "(SELECT id FROM documents WHERE kb_id=?)"
    ents = await link_entities(query, kb_id=kb_id)
    if ents:
        seed = {e["id"] for e in ents}
        for _ in range(max(1, min(hops, 3))):
            marks = ",".join("?" * len(seed))
            if kb_id is None:
                rows = conn.execute(
                    f"SELECT head_id,tail_id FROM relations WHERE head_id IN ({marks}) OR tail_id IN ({marks})",
                    tuple(seed) * 2,
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT head_id,tail_id FROM relations WHERE (head_id IN ({marks}) OR tail_id IN ({marks})) "
                    f"AND doc_id IN {kb_doc_sub}",
                    tuple(seed) * 2 + (kb_id,),
                ).fetchall()
            seed |= {r["head_id"] for r in rows} | {r["tail_id"] for r in rows}
        marks = ",".join("?" * len(seed))
        if kb_id is None:
            rows = conn.execute(
                f"SELECT head_id,tail_id,relation,inferred FROM relations "
                f"WHERE head_id IN ({marks}) AND tail_id IN ({marks}) LIMIT 800",
                tuple(seed) * 2,
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT head_id,tail_id,relation,inferred FROM relations "
                f"WHERE head_id IN ({marks}) AND tail_id IN ({marks}) "
                f"AND doc_id IN {kb_doc_sub} LIMIT 800",
                tuple(seed) * 2 + (kb_id,),
            ).fetchall()
    else:
        seed = set()
        if kb_id is None:
            rows = conn.execute(
                "SELECT head_id,tail_id,relation,inferred FROM relations LIMIT 800"
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT head_id,tail_id,relation,inferred FROM relations "
                f"WHERE doc_id IN {kb_doc_sub} LIMIT 800", (kb_id,)
            ).fetchall()
    if not rows:
        return {"chunks": [], "triples": [], "entities": []}

    G = nx.Graph()
    for r in rows:
        G.add_edge(r["head_id"], r["tail_id"])
    node_ids = list(G.nodes())
    marks = ",".join("?" * len(node_ids))
    erows = conn.execute(
        f"SELECT id,name,type FROM entities WHERE id IN ({marks})", tuple(node_ids)
    ).fetchall()
    name_map = {r["id"]: r["name"] for r in erows}
    type_map = {r["id"]: r["type"] for r in erows}

    # 中心性：度数中心性（大规模子图上近似）
    try:
        centrality = nx.degree_centrality(G)
    except Exception:
        centrality = {}
    top_central = sorted(centrality.items(), key=lambda x: -x[1])[:10]

    # 社群发现：贪心模块度（图太小时跳过）
    communities = []
    if G.number_of_nodes() >= 6:
        try:
            communities = list(nx.community.greedy_modularity_communities(G))[:8]
        except Exception:
            communities = []

    # 桥接分析：介数中心性 top（连接不同社群的关键节点）
    bridges = []
    if G.number_of_nodes() >= 8:
        try:
            bc = nx.betweenness_centrality(G)
            bridges = sorted(bc.items(), key=lambda x: -x[1])[:5]
        except Exception:
            bridges = []

    ent_names = sorted({name_map[e["id"]] for e in ents if e["id"] in name_map})
    insights = []
    if top_central:
        lines = [
            f"{i+1}. {name_map.get(nid, '?')}（{type_map.get(nid, '') or '未分类'}）"
            for i, (nid, _) in enumerate(top_central)
        ]
        insights.append("【核心实体（按度数中心性排序）】\n" + "\n".join(lines))
    if communities:
        blocks = []
        for ci, comm in enumerate(communities, 1):
            members = [name_map.get(n, "?") for n in list(comm)[:8]]
            blocks.append(f"社群{ci}（{len(comm)} 个实体）：{'、'.join(members)}")
        insights.append("【实体社群（模块度聚类）】\n" + "\n".join(blocks))
    if bridges:
        lines = [
            f"{name_map.get(nid, '?')}（中介中心性 {round(v, 3)}）" for nid, v in bridges
        ]
        insights.append("【关键桥接实体（连接不同社群）】\n" + "\n".join(lines))
    if not insights:
        return {"chunks": [], "triples": [], "entities": ent_names}

    triples = []
    for r in rows[:60]:
        h, t = name_map.get(r["head_id"]), name_map.get(r["tail_id"])
        if h and t:
            suffix = "（推断）" if r["inferred"] else ""
            triples.append(f"{h} --{r['relation']}--> {t}{suffix}")

    chunks = [
        {"chunk_id": None, "doc_id": None, "text": text, "score": float(len(insights) - i)}
        for i, text in enumerate(insights)
    ]
    return {"chunks": chunks, "triples": triples, "entities": ent_names}
