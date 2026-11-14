"""RAG 上下文组装测试：图谱解读优先级、三元组与笔记反链接注入、字符预算。"""
from app import rag


def _chunk(text, doc_id=1, cid=1):
    return {"chunk_id": cid, "doc_id": doc_id, "text": text, "score": 0.9}


def test_build_context_puts_narrative_first():
    ctx = rag.build_context(
        [_chunk("原文段落")],
        {"narrative": "【知识图谱解读】张三 --任职于--> 阿里巴巴", "triples": []},
    )
    assert ctx.startswith("【知识图谱解读】")
    assert ctx.index("【知识图谱解读】") < ctx.index("[1]")


def test_build_context_includes_triples_and_notes():
    ctx = rag.build_context(
        [_chunk("原文段落")],
        {
            "narrative": "",
            "triples": ["A --关系--> B"],
            "notes": "【反向链接】《某笔记》提到本笔记",
        },
    )
    assert "【知识图谱三元组" in ctx
    assert "A --关系--> B" in ctx
    assert "【反向链接】" in ctx


def test_build_context_truncates_chunks_by_budget():
    ctx = rag.build_context([_chunk("长" * 500), _chunk("短", cid=2)], {"triples": []}, max_chars=300)
    # 第一个分块被截到预算内，第二个不再进入上下文
    assert "[1]" in ctx
    assert "[2]" not in ctx
    assert len(ctx) <= 320


def test_build_context_handles_missing_graph_ctx():
    """graph_ctx 缺键（历史调用/外部集成）时不能抛异常。"""
    ctx = rag.build_context([_chunk("内容")], {})
    assert "[1] 内容" in ctx


def test_build_context_marks_web_source_url():
    ctx = rag.build_context(
        [{"chunk_id": None, "doc_id": None, "text": "网页正文", "score": 1.0,
          "source": "web", "url": "https://example.com/a"}],
        {"triples": []},
    )
    assert "https://example.com/a" in ctx


def test_build_context_limits_narrative_and_notes():
    ctx = rag.build_context(
        [_chunk("x")],
        {"narrative": "叙" * 5000, "notes": "链" * 5000, "triples": []},
    )
    assert len(ctx) < 9000