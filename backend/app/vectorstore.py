"""向量检索：内存矩阵 + cosine 相似度（中小规模知识库零额外依赖）。"""
import numpy as np
from . import db
from .config import get_settings, embed_tag

_ZERO_USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "estimated": False}


async def embed_query(query: str):
    """查询向量化，返回 (向量, usage)。"""
    from .providers import embed_texts
    vecs, usage = await embed_texts([query], return_usage=True)
    return np.asarray(vecs[0], dtype=np.float32), usage


async def vector_search(query: str, top_k: int = 6, kb_id: int = None):
    """返回 ([{chunk_id, doc_id, text, score}], 向量模型签名, usage)，按 cosine 降序。
    指定 kb_id 时仅在该知识库的文档分块中检索。"""
    cfg = get_settings()
    tag = embed_tag(cfg)
    ids, texts, docs, mat = db.all_vectors(tag, kb_id=kb_id)
    if mat is None or not ids:
        return [], tag, dict(_ZERO_USAGE)
    qvec, eusage = await embed_query(query)
    qn = np.linalg.norm(qvec) + 1e-10
    matn = np.linalg.norm(mat, axis=1) + 1e-10
    sims = (mat @ qvec) / (matn * qn)
    order = np.argsort(-sims)[:top_k]
    out = []
    for i in order:
        out.append({
            "chunk_id": int(ids[i]),
            "doc_id": int(docs[i]),
            "text": texts[i],
            "score": float(sims[i]),
        })
    return out, tag, eusage
