"""向量检索：内存矩阵 + cosine 相似度（中小规模知识库零额外依赖）。"""
import numpy as np
from . import db
from .config import get_settings, embed_tag


async def embed_query(query: str) -> np.ndarray:
    from .providers import embed_texts
    vec = (await embed_texts([query]))[0]
    return np.asarray(vec, dtype=np.float32)


async def vector_search(query: str, top_k: int = 6, kb_id: int = None):
    """返回 [{chunk_id, doc_id, text, score}]，按 cosine 降序。
    指定 kb_id 时仅在该知识库的文档分块中检索。"""
    cfg = get_settings()
    tag = embed_tag(cfg)
    ids, texts, docs, mat = db.all_vectors(tag, kb_id=kb_id)
    if mat is None or not ids:
        return [], tag
    qvec = await embed_query(query)
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
    return out, tag
