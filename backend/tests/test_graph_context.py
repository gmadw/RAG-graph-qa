"""图谱可读化测试：多跳路径、叙述渲染、无 LLM 时的图谱解读端到端。"""
import asyncio

import networkx as nx

from app import db, graph_context
from helpers import add_doc, add_triple


def _names(G, mapping):
    return {n: mapping.get(n, str(n)) for n in G.nodes()}


def test_relation_paths_generates_multi_hop_chain():
    G = nx.DiGraph()
    G.add_edge(1, 2, relations=["任职于"])
    G.add_edge(2, 3, relations=["总部位于"])
    names = _names(G, {1: "张三", 2: "阿里巴巴", 3: "杭州"})
    paths = graph_context._relation_paths(G, [1], names, max_paths=5, max_len=3)
    assert paths, "应至少产出一条路径"
    assert paths[0]["hops"] == 2
    assert paths[0]["text"] == "张三 --任职于--> 阿里巴巴 --总部位于--> 杭州"


def test_relation_paths_respects_max_len():
    G = nx.DiGraph()
    for i in range(5):
        G.add_edge(i, i + 1, relations=["r"])
    names = _names(G, {i: f"E{i}" for i in range(6)})
    paths = graph_context._relation_paths(G, [0], names, max_paths=10, max_len=2)
    assert all(p["hops"] <= 2 for p in paths)


def test_render_puts_entities_paths_and_notes_together():
    cards = [{"name": "张三", "type": "人物", "description": "工程师", "degree": 3, "aliases": ["张工"]}]
    paths = [{"text": "张三 --任职于--> 阿里巴巴", "hops": 1}]
    communities = [{"size": 5, "core": ["阿里巴巴"], "members": ["阿里巴巴", "张三"]}]
    text = graph_context._render(["张三"], cards, paths, communities, "【笔记网络】3 篇笔记", {"entities": 2, "relations": 1})
    assert "张三（人物，关联 3 条关系）：工程师" in text
    assert "别名：张工" in text
    assert "张三 --任职于--> 阿里巴巴" in text
    assert "社群1" in text
    assert "【笔记网络】3 篇笔记" in text
    # 没有实体卡片和路径时不产出空叙述，避免把空段落塞进 prompt
    assert graph_context._render([], [], [], [], "", None) == ""


def test_read_graph_without_llm_uses_top_entities(kb_id):
    """LLM 不可用时走兜底：按图上最活跃实体做种子，仍能产出可读叙述。"""
    doc = add_doc(kb_id, "公司.md", "张三任职于阿里巴巴，阿里巴巴总部在杭州。")
    add_triple("张三", "人物", "任职于", "阿里巴巴", "公司", doc_id=doc)
    add_triple("阿里巴巴", "公司", "总部位于", "杭州", "地点", doc_id=doc)
    result = asyncio.run(graph_context.read_graph("", kb_id=kb_id))
    assert "张三" in result["narrative"]
    assert "阿里巴巴 --总部位于--> 杭州" in result["narrative"]
    assert result["triples"], "应携带结构化三元组供前端展示"
    assert result["usage"] is None


def test_read_graph_returns_empty_on_empty_kb(kb_id):
    result = asyncio.run(graph_context.read_graph("", kb_id=kb_id))
    assert result["narrative"] == ""
    assert result["chunks"] == []


def test_communities_are_skipped_for_tiny_graph():
    G = nx.DiGraph()
    G.add_edge(1, 2, relations=["r"])
    G.add_edge(2, 3, relations=["r"])
    assert graph_context._communities(G, {1: "A", 2: "B", 3: "C"}, {1}) == []


def test_render_subgraph_brief_without_db_lookup(kb_id):
    """hybrid 模式复用 graph_search 的边：不重跑 BFS，只做叙述化。"""
    doc = add_doc(kb_id, "技术.md", "Raft 是共识算法。")
    h1, t1 = add_triple("Raft", "技术", "属于", "共识算法", "概念", doc_id=doc)
    edges = [{"head_id": h1, "tail_id": t1, "relation": "属于", "chunk_id": None, "kind": "extracted"}]
    brief = graph_context.render_subgraph_brief(edges, [h1, t1], kb_id)
    assert "Raft --属于--> 共识算法" in brief


def test_graph_stats_includes_note_counts(kb_id):
    from app import notes
    add_note_doc = add_doc(kb_id, "笔记.md", "[[目标]]")
    db.set_note_meta(add_note_doc, "笔记", aliases=[])
    stats = db.note_link_stats(kb_id)
    assert stats["notes"] == 1
    assert notes.graph_notes_summary(kb_id).startswith("【笔记网络】")