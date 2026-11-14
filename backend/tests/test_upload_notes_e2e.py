"""端到端：真实上传 Markdown → 入库管线 → 双链落库 → 反链接接口。

覆盖两类场景：
  1. 图谱可用（关闭构建）时应解析双链；
  2. 图谱不可用（LLM 调用失败）时，**双链仍必须建立**——否则知识图谱故障会连带
     让 Obsidian 反链接全部消失。
"""
import pytest
from fastapi.testclient import TestClient

import app.providers as providers
from app import db, notes
from app.main import app


def _fake_embed(texts, return_usage=False):
    vecs = [[float(len(t) % 7), 1.0, 0.5] for t in texts]
    if return_usage:
        return vecs, {"prompt_tokens": 1, "completion_tokens": 0,
                      "total_tokens": 1, "estimated": False}
    return vecs


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


@pytest.fixture(autouse=True)
def _no_network_embedding(monkeypatch):
    """向量化走假实现，不依赖外部模型；LLM 保持不可用，模拟「图谱服务故障」。"""
    monkeypatch.setattr(providers, "embed_texts", _fake_embed)


def _upload(client, kb_id, entries):
    files = [("files", (name, text.encode("utf-8"), "text/markdown")) for name, text in entries]
    return client.post(f"/api/documents/upload?kb_id={kb_id}", files=files)


def test_upload_md_builds_backlinks_even_when_graph_fails(kb_id):
    with TestClient(app) as client:
        r = _upload(client, kb_id, [("分布式共识.md", NOTE_A), ("Raft.md", NOTE_B)])
        assert r.status_code == 200
        assert r.json()["doc_ids"], "两个文件都应入库"

        notes_api = client.get("/api/notes", params={"kb_id": kb_id}).json()
        assert notes_api["stats"]["notes"] == 2
        assert notes_api["stats"]["links"] == 2
        assert notes_api["stats"]["resolved"] == 2, "两篇都已上传，链接应全部解析"

        a = [n for n in notes_api["notes"] if n["title"] == "分布式共识"][0]
        backlinks = client.get(f"/api/notes/{a['doc_id']}/backlinks").json()
        assert [x["title"] for x in backlinks["backlinks"]] == ["Raft"]


def test_upload_order_does_not_matter(kb_id):
    """先传目标后传源、或反过来，最终都应解析成功。"""
    with TestClient(app) as client:
        _upload(client, kb_id, [("Raft.md", NOTE_B)])
        first = client.get("/api/notes", params={"kb_id": kb_id}).json()
        assert first["stats"]["resolved"] == 0, "此时目标笔记还不存在"

        _upload(client, kb_id, [("分布式共识.md", NOTE_A)])
        final = client.get("/api/notes", params={"kb_id": kb_id}).json()
        assert final["stats"]["resolved"] == 2


def test_relink_backfills_documents_uploaded_before_upgrade(kb_id):
    """存量 .md（改造前上传，没有 note_title）点「重建双链」后应被补解析。"""
    doc = db.insert_document("旧笔记.md", ".md", 0, "", kb_id=kb_id)
    path = db.UPLOAD_DIR / f"{doc}_旧笔记.md"
    path.write_text("# 旧笔记\n这段是 [[分布式共识]] 的补充说明。", encoding="utf-8")
    assert db.get_note_meta(doc)["title"] == "", "模拟改造前的存量文档"

    with TestClient(app) as client:
        r = client.post("/api/notes/relink", json={"kb_id": kb_id}).json()
        assert r["backfilled"] == 1, "应补解析 1 篇存量笔记"

    meta = db.get_note_meta(doc)
    assert meta["title"] == "旧笔记"
    assert db.note_outlinks(doc)[0]["dst_title"] == "分布式共识"