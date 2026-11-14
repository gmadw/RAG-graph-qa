"""测试辅助：快速造文档 / 分块 / 笔记，避免每个用例重复样板代码。"""
from app import db, notes

EMBED_TAG = "test::tag"


def add_doc(kb_id, filename, text, chunk_texts=None, filetype=".md"):
    """插入文档并按给定文本造分块（向量为 None，仅测结构化逻辑）。"""
    did = db.insert_document(filename, filetype, len(text or ""), "", kb_id=kb_id)
    chunks = chunk_texts if chunk_texts is not None else [text or ""]
    db.insert_chunks(did, EMBED_TAG, [(i, t, None) for i, t in enumerate(chunks)])
    return did


def add_note(kb_id, filename, text, path=None, chunk_texts=None):
    """插入 Markdown 笔记：文档 + 分块 + frontmatter/双链解析。

    分块默认用「剥掉 frontmatter 的正文」，与真实入库管线保持一致。
    """
    _, body = notes.parse_frontmatter(text)
    if chunk_texts is None:
        chunk_texts = [body]
    did = add_doc(kb_id, filename, text, chunk_texts)
    notes.sync_note(did, path or f"/vault/{filename}", text, body=body)
    return did


def add_triple(head, head_type, relation, tail, tail_type, doc_id=None):
    """建实体与一条关系，返回 (head_id, tail_id)。"""
    hid = db.upsert_entity(head, head_type)
    tid = db.upsert_entity(tail, tail_type)
    db.insert_relation(hid, tid, relation, None, doc_id)
    return hid, tid