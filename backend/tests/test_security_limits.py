"""安全修复回归：CORS 收紧、上传硬限制、query 长度限制、路径穿越防护。

扫描器报了 60 个 Critical「SQL 注入」，人工甄别后均为误报（提示词 f-string /
SQL 常量子查询 / 参数化占位符）；本文件只覆盖真实修复的行为。
"""
import io

import pytest
from fastapi.testclient import TestClient

from app import db, main
from app.main import app


def _md_file(name: str, content: bytes = b"# note\ncontent [[target]]"):
    return ("files", (name, io.BytesIO(content), "text/markdown"))


# ---------------- CORS：默认只放行本机来源 ----------------

def test_cors_blocks_foreign_origin():
    with TestClient(app) as client:
        r = client.get("/api/kbs", headers={"Origin": "http://evil.example"})
        assert r.status_code == 200
        assert "access-control-allow-origin" not in {k.lower() for k in r.headers}, \
            "任意网页不得获得跨域读取本服务响应的能力"


def test_cors_allows_localhost_any_port():
    with TestClient(app) as client:
        for origin in ("http://localhost:5173", "http://127.0.0.1:8000"):
            r = client.get("/api/kbs", headers={"Origin": origin})
            assert r.headers.get("access-control-allow-origin") == origin


def test_cors_preflight_rejects_foreign_origin():
    with TestClient(app) as client:
        r = client.options(
            "/api/documents/upload",
            headers={
                "Origin": "http://evil.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


# ---------------- 上传硬限制 ----------------

def test_upload_file_count_limit(kb_id, monkeypatch):
    with TestClient(app) as client:
        files = [_md_file(f"n{i}.md") for i in range(main.MAX_UPLOAD_FILES + 1)]
        r = client.post(f"/api/documents/upload?kb_id={kb_id}", files=files)
    assert r.status_code == 413


def test_upload_size_limit_rolls_back(kb_id, monkeypatch):
    """超限文件必须 413，且半截文件与文档记录都要回滚（不残留 pending）。"""
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 1024)
    big = b"x" * 4096
    with TestClient(app) as client:
        r = client.post(
            f"/api/documents/upload?kb_id={kb_id}",
            files=[_md_file("big.md", big)],
        )
        assert r.status_code == 413
        assert db.list_documents(kb_id) == [], "超限文档不能残留记录"
    uploads = list(db.UPLOAD_DIR.glob("*_big.md"))
    assert uploads == [], "半截文件不能残留在磁盘上"


def test_upload_path_traversal_never_escapes_upload_dir(kb_id):
    """回归：../../ 类文件名必须被剥成纯文件名，且后缀不合法时直接拒绝。"""
    with TestClient(app) as client:
        r = client.post(
            f"/api/documents/upload?kb_id={kb_id}",
            files=[("files", ("../../etc/passwd", io.BytesIO(b"x"), "application/octet-stream"))],
        )
        assert r.status_code == 400  # .passwd 不在白名单 → 整体拒绝，不落盘
        assert all(not p.name.endswith("passwd") for p in db.UPLOAD_DIR.iterdir())
        # 合法扩展名 + 穿越前缀：应只剩文件名本体
        r2 = client.post(
            f"/api/documents/upload?kb_id={kb_id}",
            files=[("files", ("../evil.md", io.BytesIO(b"# ok"), "text/markdown"))],
        )
        assert r2.status_code == 200
        doc = db.list_documents(kb_id)[0]
        assert doc["filename"] == "evil.md", "入库文件名必须剥离路径成分"
        assert db.delete_document(doc["id"]) is None or True


# ---------------- 请求体长度限制 ----------------

def test_chat_query_length_limit(kb_id):
    with TestClient(app) as client:
        r = client.post("/api/chat", json={"query": "好" * 8001, "kb_id": kb_id})
        assert r.status_code == 422
        r2 = client.post("/api/chat", json={"query": "正常问题", "kb_id": kb_id})
        assert r2.status_code in (200, 404)  # 通过校验（kb 无文档时端点内部有分支）


def test_retrieve_preview_query_length_limit(kb_id):
    with TestClient(app) as client:
        r = client.post("/api/retrieve-preview", json={"query": "问" * 8001, "kb_id": kb_id})
        assert r.status_code == 422
