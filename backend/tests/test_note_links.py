"""笔记双链入库测试：跨笔记解析、反链接、幂等建边与删除解绑。"""
from app import db, notes
from helpers import add_doc, add_note

NOTE_A = """---
title: 分布式共识
aliases: [Consensus]
tags: [分布式]
---
# 分布式共识
共识算法让多个节点达成一致，代表实现是 [[Raft]]，与 [[Paxos]] 常被比较。
"""

NOTE_B = """---
aliases: [Raft 协议]
---
# Raft
Raft 是 [[分布式共识]] 的工程实现。
"""


def _seed(kb_id):
    a = add_note(kb_id, "分布式共识.md", NOTE_A)
    b = add_note(kb_id, "Raft.md", NOTE_B)
    return a, b


def test_sync_note_stores_meta_and_links(kb_id):
    a = add_note(kb_id, "分布式共识.md", NOTE_A)
    meta = db.get_note_meta(a)
    assert meta["title"] == "分布式共识"
    assert meta["aliases"] == ["Consensus"]
    assert meta["tags"] == ["分布式"]
    out = db.note_outlinks(a)
    assert {o["dst_title"] for o in out} == {"Raft", "Paxos"}


def test_link_notes_creates_wikilink_edges(kb_id):
    a, b = _seed(kb_id)
    edges = notes.link_notes(kb_id)
    # A→Raft 与 B→分布式共识 都可解析；A→Paxos 目标不存在，不建边
    assert edges == 2
    conn = db.get_conn()
    rows = conn.execute("SELECT relation,kind,chunk_id FROM relations ORDER BY id").fetchall()
    assert {r["kind"] for r in rows} == {"wikilink"}
    assert {r["relation"] for r in rows} == {"链接到"}
    # 边能回溯到原文分块，回答时可用于引用溯源
    assert all(r["chunk_id"] for r in rows)


def test_link_notes_resolves_out_of_order_upload(kb_id):
    """先传 A（链接 B）后传 B：第二次 link_notes 必须把 A→B 接上。"""
    a = add_note(kb_id, "分布式共识.md", NOTE_A)
    first = notes.link_notes(kb_id)
    assert first == 0  # 此时 Raft 还不存在
    b = add_note(kb_id, "Raft.md", NOTE_B)
    notes.link_notes(kb_id)
    backlinks = db.note_backlinks(a)
    assert [x["doc_id"] for x in backlinks] == [b]


def test_link_notes_is_idempotent(kb_id):
    _seed(kb_id)
    notes.link_notes(kb_id)
    count_once = db.graph_stats(kb_id)["relations"]
    notes.link_notes(kb_id)
    notes.link_notes(kb_id)
    assert db.graph_stats(kb_id)["relations"] == count_once


def test_backlinks_carry_context_and_anchor(kb_id):
    a = add_note(kb_id, "分布式共识.md", NOTE_A)
    add_note(kb_id, "Raft.md", "链接到 [[分布式共识#共识算法|共识]] 的具体实现。")
    notes.link_notes(kb_id)
    bl = db.note_backlinks(a)
    assert len(bl) == 1
    assert bl[0]["anchor"] == "共识算法"
    assert "具体实现" in bl[0]["context"]


def test_delete_document_unbinds_backlinks(kb_id):
    a, b = _seed(kb_id)
    notes.link_notes(kb_id)
    db.delete_document(b)
    # 指向被删笔记的链接记录保留但解除绑定，重新上传同名笔记可自动恢复
    assert db.note_backlinks(a) == []
    assert db.note_link_stats(kb_id)["unresolved"] >= 1


def test_note_link_stats_counts_unresolved(kb_id):
    a = add_note(kb_id, "分布式共识.md", NOTE_A)
    notes.link_notes(kb_id)
    stats = db.note_link_stats(kb_id)
    assert stats["notes"] == 1
    assert stats["links"] == 2
    assert stats["unresolved"] == 2  # Raft 与 Paxos 都还不存在


def test_link_notes_skips_self_link(kb_id):
    add_note(kb_id, "自引用.md", "本文链接自己 [[自引用]]")
    assert notes.link_notes(kb_id) == 0


def test_note_context_includes_backlinks_and_outlinks(kb_id):
    a, b = _seed(kb_id)
    notes.link_notes(kb_id)
    ctx = notes.note_context(a)
    assert "《分布式共识》" in ctx
    assert "本文链接到：《Raft》" in ctx
    assert "反向链接" in ctx
    assert "《Raft》" in ctx


def test_note_entity_gets_excerpt_description(kb_id):
    a = add_note(kb_id, "分布式共识.md", NOTE_A)
    notes.link_notes(kb_id)
    meta = db.get_note_meta(a)
    eid = db.find_entity_by_name(meta["title"])
    row = db.get_conn().execute("SELECT description FROM entities WHERE id=?", (eid,)).fetchone()
    assert "共识算法让多个节点达成一致" in row["description"]

def test_title_uses_original_filename_not_disk_path(kb_id):
    """回归：磁盘文件名带 `{doc_id}_` 前缀，标题必须取用户上传时的原始文件名。"""
    text = "# 领导者选举\n候选者通过投票成为领导者。"
    did = add_doc(kb_id, "领导者选举.md", text)
    notes.sync_note(did, "/data/uploads/7_领导者选举.md", text, source_name="领导者选举.md")
    assert db.get_note_meta(did)["title"] == "领导者选举"
