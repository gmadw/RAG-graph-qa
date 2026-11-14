"""pytest 公共夹具：隔离临时数据目录，每个用例前重置业务数据。

必须在导入 `app.db` 之前设置 RAG_DATA_DIR——db 模块在 import 时就确定了数据库路径。
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["RAG_DATA_DIR"] = tempfile.mkdtemp(prefix="rag-graph-qa-test-")

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402
from app import db  # noqa: E402

_TABLES = (
    "note_links", "relations", "entities", "chunks", "documents",
    "feedback", "messages", "chats", "knowledge_bases", "settings",
)


@pytest.fixture(scope="session", autouse=True)
def _init_db():
    db.init_db()


@pytest.fixture(autouse=True)
def _clean_db():
    conn = db.get_conn()
    for table in _TABLES:
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    # 测试直接改表不经过 db 写函数，向量矩阵缓存必须手动失效，否则用例间相互污染
    db.invalidate_search_cache()
    db._seed_default_kb(conn)
    # 把模型端点置空：ProviderError 会立刻回落到规则逻辑，单元测试不依赖外部服务
    db.set_setting("settings", {
        "llm": {"base_url": ""},
        "embedding": {"base_url": ""},
        "rerank": {"enabled": False},
    })
    yield


@pytest.fixture
def kb_id():
    return db.list_kbs()[0]["id"]