"""向量矩阵缓存正确性：写入失效、缓存命中一致、kb 隔离、embed_tag SQL 过滤。

这是检索热路径的缓存，语义错了会直接导致「上传了搜不到 / 删掉了还能搜到」。
"""
import numpy as np

from app import db
from helpers import add_doc

TAG_A = "modelA::v1"
TAG_B = "modelB::v1"


def _add_vec_doc(kb_id, filename, texts, tag=TAG_A):
    did = db.insert_document(filename, ".md", 10, "", kb_id=kb_id)
    vecs = [np.full(8, float(i) + 1, dtype=np.float32) for i in range(len(texts))]
    db.insert_chunks(did, tag, list(zip(range(len(texts)), texts, vecs)))
    return did


def test_first_load_then_cache_hit_is_consistent(kb_id):
    _add_vec_doc(kb_id, "a.md", ["块一", "块二"])
    first = db.all_vectors(TAG_A)
    second = db.all_vectors(TAG_A)
    # 缓存命中：两次结果完全一致，且是同一对象（没有反复查库）
    assert [int(x) for x in first[0]] == [int(x) for x in second[0]]
    assert first[0] is second[0]
    assert first[3] is second[3]
    assert first[3].shape == (2, 8)


def test_cache_rebuilds_after_new_upload(kb_id):
    _add_vec_doc(kb_id, "a.md", ["块一"])
    assert db.all_vectors(TAG_A)[3].shape[0] == 1
    # insert_chunks 内部触发失效：新文档必须立即可检索
    _add_vec_doc(kb_id, "b.md", ["块三"])
    ids, texts, docs, mat = db.all_vectors(TAG_A)
    assert mat.shape[0] == 2
    assert "块三" in texts


def test_delete_document_invalidates_cache(kb_id):
    did = _add_vec_doc(kb_id, "a.md", ["块一", "块二"])
    db.delete_document(did)
    assert db.all_vectors(TAG_A)[3] is None or db.all_vectors(TAG_A)[3].shape[0] == 0


def test_kb_isolation_with_cache(kb_id):
    from app import db as _db
    kb2 = _db.create_kb("第二知识库")
    _add_vec_doc(kb_id, "a.md", ["kb1的块"])
    _add_vec_doc(kb2, "b.md", ["kb2的块"])
    _, texts1, _, m1 = db.all_vectors(TAG_A, kb_id=kb_id)
    _, texts2, _, m2 = db.all_vectors(TAG_A, kb_id=kb2)
    assert texts1 == ["kb1的块"] and m1.shape[0] == 1
    assert texts2 == ["kb2的块"] and m2.shape[0] == 1


def test_embed_tag_filter_pushed_down_to_sql(kb_id):
    """换模型后旧向量必须被排除：过滤在 SQL 层完成，不在 Python 侧丢弃。"""
    _add_vec_doc(kb_id, "a.md", ["模型A的块"], tag=TAG_A)
    _add_vec_doc(kb_id, "b.md", ["模型B的块"], tag=TAG_B)
    _, texts_a, _, m_a = db.all_vectors(TAG_A)
    _, texts_b, _, m_b = db.all_vectors(TAG_B)
    assert texts_a == ["模型A的块"] and m_a.shape[0] == 1
    assert texts_b == ["模型B的块"] and m_b.shape[0] == 1


def test_reset_reprocess_invalidates(kb_id):
    did = _add_vec_doc(kb_id, "a.md", ["旧块"])
    assert db.all_vectors(TAG_A)[3].shape[0] == 1
    db.reset_document_for_reprocess(did)  # 清空分块等待重解析
    ids, texts, docs, mat = db.all_vectors(TAG_A)
    assert mat is None or mat.shape[0] == 0


def test_vector_search_uses_cache_after_insert(kb_id, monkeypatch):
    """端到端：入库后立刻检索应能命中（缓存失效链路完整）。

    vector_search 按当前配置的 embed_tag 签名检索，插入必须用同一签名；
    embedding 走进程内假实现（conftest 已断网），本用例只验证缓存与相似度链路。
    """
    import app.providers as providers
    from app.config import embed_tag, get_settings
    from app.vectorstore import vector_search

    tag = embed_tag(get_settings())
    _add_vec_doc(kb_id, "a.md", ["分布式共识算法的原理与实现"], tag=tag)

    async def _fake_embed(texts, return_usage=False):
        vecs = [[1.0] * 8 for _ in texts]  # 与插入向量同向，必然命中该块
        if return_usage:
            return vecs, {"prompt_tokens": 0, "completion_tokens": 0,
                          "total_tokens": 0, "estimated": True}
        return vecs

    monkeypatch.setattr(providers, "embed_texts", _fake_embed)
    hits, _, _ = _run(vector_search("分布式共识算法", top_k=3, kb_id=kb_id))
    assert hits, "入库后应立即能检索到"
    assert hits[0]["text"].startswith("分布式共识算法")


def _run(coro):
    import asyncio
    return asyncio.run(coro)