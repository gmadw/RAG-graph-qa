"""API 路由测试：笔记列表/反链接/重建双链/图解读/统计（进程内 TestClient）。

不依赖外部模型服务：conftest 已把模型端点置空，图解读会自动走规则兜底。
"""
from fastapi.testclient import TestClient

from app import db, notes
from app.main import app
from helpers import add_doc, add_note

NOTE_A = """---
title: 分布式共识
aliases: [Consensus]
tags: [分布式]
---
# 分布式共识
共识算法让多个节点达成一致，代表实现是 [[Raft]]。
"""

NOTE_B = """---
tags: [共识]
---
# Raft
Raft 是 [[分布式共识]] 的工程实现。
"""


def _seed(kb_id):
    a = add_note(kb_id, "分布式共识.md", NOTE_A)
    b = add_note(kb_id, "Raft.md", NOTE_B)
    return a, b


def test_notes_api_lists_notes_and_stats(kb_id):
    _seed(kb_id)
    with TestClient(app) as client:
        r = client.get("/api/notes", params={"kb_id": kb_id})
    assert r.status_code == 200
    body = r.json()
    assert body["stats"]["notes"] == 2
    assert {n["title"] for n in body["notes"]} == {"分布式共识", "Raft"}

    # 未建边前两条链接都处于未解析状态
    assert body["stats"]["unresolved"] == 2


def test_relink_api_creates_edges_and_is_repeatable(kb_id):
    _seed(kb_id)
    with TestClient(app) as client:
        first = client.post("/api/notes/relink", json={"kb_id": kb_id}).json()
        second = client.post("/api/notes/relink", json={"kb_id": kb_id}).json()
    assert first["edges"] == 2
    assert second["edges"] == 2          # 幂等：重复重建不会让边翻倍
    assert second["stats"]["resolved"] == 2


def test_backlinks_api_returns_meta_and_links(kb_id):
    a, b = _seed(kb_id)
    notes.link_notes(kb_id)
    with TestClient(app) as client:
        r = client.get(f"/api/notes/{a}/backlinks")
    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["title"] == "分布式共识"
    assert body["meta"]["aliases"] == ["Consensus"]
    assert [x["doc_id"] for x in body["backlinks"]] == [b]
    assert body["outlinks"][0]["dst_title"] == "Raft"


def test_backlinks_api_rejects_non_note_document(kb_id):
    doc = add_doc(kb_id, "spec.pdf", "PDF 正文", filetype=".pdf")
    with TestClient(app) as client:
        r = client.get(f"/api/notes/{doc}/backlinks")
    assert r.status_code == 400


def test_backlinks_api_404_for_missing_doc(kb_id):
    with TestClient(app) as client:
        r = client.get("/api/notes/99999/backlinks")
    assert r.status_code == 404


def test_graph_read_api_returns_narrative_and_stats(kb_id):
    doc = add_doc(kb_id, "公司.md", "张三任职于阿里巴巴，阿里巴巴总部在杭州。")
    db.upsert_entity("张三", "人物")
    h = db.upsert_entity("阿里巴巴", "公司")
    t = db.upsert_entity("杭州", "地点")
    db.insert_relation(h, t, "总部位于", None, doc)
    with TestClient(app) as client:
        r = client.get("/api/graph/read", params={"kb_id": kb_id})
    assert r.status_code == 200
    body = r.json()
    assert "阿里巴巴 --总部位于--> 杭州" in body["narrative"]
    assert body["stats"]["entities"] >= 1


def test_graph_stats_api_merges_note_counts(kb_id):
    _seed(kb_id)
    with TestClient(app) as client:
        r = client.get("/api/graph/stats", params={"kb_id": kb_id})
    body = r.json()
    assert "entities" in body and "notes" in body
    assert body["notes"] == 2