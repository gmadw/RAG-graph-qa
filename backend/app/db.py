"""SQLite 存储层：文档、分块向量、知识图谱、会话、设置。"""
import sqlite3
import json
import threading
import time
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "app.db"

_local = threading.local()
# 全局写锁：串行化后台处理任务，避免 SQLite 写冲突。
# 必须是 RLock：upsert_entity 持锁调用 bump_entity_mention 等同线程嵌套获取，Lock 会死锁。
write_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_bases(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  description TEXT DEFAULT '',
  created_at REAL
);
CREATE TABLE IF NOT EXISTS documents(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kb_id INTEGER REFERENCES knowledge_bases(id) ON DELETE SET NULL,
  filename TEXT NOT NULL,
  filetype TEXT DEFAULT '',
  size INTEGER DEFAULT 0,
  status TEXT DEFAULT 'pending',      -- pending/processing/done/error
  stage TEXT DEFAULT '',              -- 当前处理阶段描述
  error TEXT DEFAULT '',
  chunk_count INTEGER DEFAULT 0,
  triple_count INTEGER DEFAULT 0,
  embed_tag TEXT DEFAULT '',          -- 向量模型签名（provider/model）
  content_hash TEXT DEFAULT '',       -- 文件内容 SHA256，用于上传去重
  created_at REAL
);
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
  chunk_index INTEGER DEFAULT 0,
  text TEXT DEFAULT '',
  vector BLOB,
  embed_tag TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS entities(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE,
  norm TEXT,                          -- 归一化名称（小写去空白）用于合并
  type TEXT DEFAULT '',
  description TEXT DEFAULT '',
  mention_count INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS relations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  head_id INTEGER REFERENCES entities(id) ON DELETE CASCADE,
  tail_id INTEGER REFERENCES entities(id) ON DELETE CASCADE,
  relation TEXT DEFAULT '',
  chunk_id INTEGER,
  doc_id INTEGER,
  inferred INTEGER DEFAULT 0          -- 1 表示图谱补全推断出的关系（非原文直述）
);
CREATE TABLE IF NOT EXISTS feedback(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER,
  message_id INTEGER,
  chunk_id INTEGER,
  query TEXT DEFAULT '',
  vote INTEGER DEFAULT 0,             -- 1 赞 / -1 踩
  created_at REAL
);
CREATE TABLE IF NOT EXISTS entity_aliases(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_id INTEGER REFERENCES entities(id) ON DELETE CASCADE,
  alias TEXT NOT NULL,
  norm TEXT,                          -- 归一化别名（小写去空白）用于匹配
  UNIQUE(entity_id, alias)
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS chats(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kb_id INTEGER REFERENCES knowledge_bases(id) ON DELETE SET NULL,
  title TEXT DEFAULT '新对话',
  created_at REAL
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER REFERENCES chats(id) ON DELETE CASCADE,
  role TEXT,
  content TEXT DEFAULT '',
  refs TEXT DEFAULT '[]',
  created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash);
CREATE INDEX IF NOT EXISTS idx_documents_kb ON documents(kb_id);
CREATE INDEX IF NOT EXISTS idx_chats_kb ON chats(kb_id);
CREATE INDEX IF NOT EXISTS idx_entities_norm ON entities(norm);
CREATE INDEX IF NOT EXISTS idx_aliases_norm ON entity_aliases(norm);
CREATE INDEX IF NOT EXISTS idx_relations_head ON relations(head_id);
CREATE INDEX IF NOT EXISTS idx_relations_tail ON relations(tail_id);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id);
"""


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def init_db():
    with write_lock:
        conn = get_conn()
        # 老库先补列：SCHEMA 里的 CREATE INDEX 依赖新列，必须在 executescript 之前
        _migrate(conn)
        conn.executescript(SCHEMA)
        _seed_default_kb(conn)
        conn.commit()


def _migrate(conn):
    """老版本数据库的列迁移（CREATE TABLE IF NOT EXISTS 不会给已存在的表加列）。"""
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    if "documents" in tables:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)").fetchall()}
        if "content_hash" not in cols:
            conn.execute("ALTER TABLE documents ADD COLUMN content_hash TEXT DEFAULT ''")
        if "kb_id" not in cols:
            conn.execute("ALTER TABLE documents ADD COLUMN kb_id INTEGER")
    if "relations" in tables:
        rcols = {r["name"] for r in conn.execute("PRAGMA table_info(relations)").fetchall()}
        if "inferred" not in rcols:
            conn.execute("ALTER TABLE relations ADD COLUMN inferred INTEGER DEFAULT 0")
    if "chats" in tables:
        ccols = {r["name"] for r in conn.execute("PRAGMA table_info(chats)").fetchall()}
        if "kb_id" not in ccols:
            conn.execute("ALTER TABLE chats ADD COLUMN kb_id INTEGER")
    conn.commit()


def _seed_default_kb(conn):
    """确保至少存在一个知识库，并让历史遗留数据（kb_id 为空）归入默认知识库。"""
    row = conn.execute("SELECT id FROM knowledge_bases ORDER BY id LIMIT 1").fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO knowledge_bases(name,description,created_at) VALUES(?,?,?)",
            ("默认知识库", "系统自动创建，用于归集历史文档", time.time()),
        )
        kb_id = cur.lastrowid
    else:
        kb_id = row["id"]
    conn.execute("UPDATE documents SET kb_id=? WHERE kb_id IS NULL", (kb_id,))
    conn.execute("UPDATE chats SET kb_id=? WHERE kb_id IS NULL", (kb_id,))
    conn.commit()


# ---------------- settings ----------------

def get_setting(key: str, default=None):
    row = get_conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return default


def set_setting(key: str, value):
    with write_lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        conn.commit()


# ---------------- 知识库 ----------------

def create_kb(name: str, description: str = "") -> int:
    with write_lock:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO knowledge_bases(name,description,created_at) VALUES(?,?,?)",
            ((name or "未命名知识库").strip()[:60], (description or "").strip()[:200], time.time()),
        )
        conn.commit()
        return cur.lastrowid


def get_kb(kb_id: int):
    row = get_conn().execute("SELECT * FROM knowledge_bases WHERE id=?", (kb_id,)).fetchone()
    return dict(row) if row else None


def kb_exists(kb_id) -> bool:
    if not kb_id:
        return False
    return get_conn().execute(
        "SELECT 1 FROM knowledge_bases WHERE id=?", (kb_id,)
    ).fetchone() is not None


def list_kbs():
    rows = get_conn().execute(
        "SELECT kb.id, kb.name, kb.description, kb.created_at, "
        " (SELECT COUNT(*) FROM documents d WHERE d.kb_id=kb.id) AS doc_count, "
        " (SELECT COALESCE(SUM(d.chunk_count),0) FROM documents d WHERE d.kb_id=kb.id) AS chunk_count "
        "FROM knowledge_bases kb ORDER BY kb.id"
    ).fetchall()
    return [dict(r) for r in rows]


def rename_kb(kb_id: int, name: str, description: str = None):
    with write_lock:
        conn = get_conn()
        if description is None:
            conn.execute(
                "UPDATE knowledge_bases SET name=? WHERE id=?",
                ((name or "").strip()[:60], kb_id),
            )
        else:
            conn.execute(
                "UPDATE knowledge_bases SET name=?, description=? WHERE id=?",
                ((name or "").strip()[:60], (description or "").strip()[:200], kb_id),
            )
        conn.commit()


def count_kbs() -> int:
    return get_conn().execute("SELECT COUNT(*) c FROM knowledge_bases").fetchone()["c"]


def delete_kb(kb_id: int) -> int:
    """删除知识库及其全部文档/分块/图谱关系，返回删除的文档数。
    该库的会话改挂到剩余的第一个知识库（或置空）。"""
    with write_lock:
        conn = get_conn()
        doc_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM documents WHERE kb_id=?", (kb_id,)
        ).fetchall()]
    for did in doc_ids:
        delete_document(did)
    with write_lock:
        conn = get_conn()
        row = conn.execute(
            "SELECT id FROM knowledge_bases WHERE id!=? ORDER BY id LIMIT 1", (kb_id,)
        ).fetchone()
        fallback = row["id"] if row else None
        conn.execute("UPDATE chats SET kb_id=? WHERE kb_id=?", (fallback, kb_id))
        conn.execute("DELETE FROM knowledge_bases WHERE id=?", (kb_id,))
        conn.commit()
    return len(doc_ids)


def entity_ids_in_kb(kb_id) -> set:
    """参与该知识库任何关系的实体 id 集合（用于图谱实体链接过滤）。"""
    if not kb_id:
        return set()
    rows = get_conn().execute(
        "SELECT DISTINCT head_id AS id FROM relations WHERE doc_id IN "
        "(SELECT id FROM documents WHERE kb_id=?) "
        "UNION SELECT DISTINCT tail_id AS id FROM relations WHERE doc_id IN "
        "(SELECT id FROM documents WHERE kb_id=?)",
        (kb_id, kb_id),
    ).fetchall()
    return {r["id"] for r in rows}


# ---------------- documents ----------------

def insert_document(filename: str, filetype: str, size: int, content_hash: str = "",
                    kb_id: int = None) -> int:
    with write_lock:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO documents(kb_id,filename,filetype,size,status,content_hash,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (kb_id, filename, filetype, size, "pending", content_hash, time.time()),
        )
        conn.commit()
        return cur.lastrowid


def find_document_by_hash(content_hash: str, kb_id: int = None):
    """按内容 hash 查同一知识库内已存在的文档（用于上传去重）。"""
    if not content_hash:
        return None
    if kb_id is None:
        row = get_conn().execute(
            "SELECT id,filename,status FROM documents WHERE content_hash=? AND status!='error' "
            "ORDER BY id DESC LIMIT 1",
            (content_hash,),
        ).fetchone()
    else:
        row = get_conn().execute(
            "SELECT id,filename,status FROM documents WHERE content_hash=? AND kb_id=? "
            "AND status!='error' ORDER BY id DESC LIMIT 1",
            (content_hash, kb_id),
        ).fetchone()
    return dict(row) if row else None


def update_document(doc_id: int, **fields):
    if not fields:
        return
    cols = ",".join(f"{k}=?" for k in fields)
    with write_lock:
        conn = get_conn()
        conn.execute(f"UPDATE documents SET {cols} WHERE id=?", (*fields.values(), doc_id))
        conn.commit()


def list_documents(kb_id: int = None):
    if kb_id is None:
        rows = get_conn().execute(
            "SELECT id,kb_id,filename,filetype,size,status,stage,error,chunk_count,triple_count,embed_tag,created_at "
            "FROM documents ORDER BY id DESC"
        ).fetchall()
    else:
        rows = get_conn().execute(
            "SELECT id,kb_id,filename,filetype,size,status,stage,error,chunk_count,triple_count,embed_tag,created_at "
            "FROM documents WHERE kb_id=? ORDER BY id DESC",
            (kb_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_document(doc_id: int):
    row = get_conn().execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    return dict(row) if row else None


def clear_doc_relations(doc_id: int):
    """清空某文档的全部三元组与孤儿实体（用于图谱重建，防止数据叠加）。"""
    with write_lock:
        conn = get_conn()
        conn.execute("DELETE FROM relations WHERE doc_id=?", (doc_id,))
        conn.execute(
            "DELETE FROM entities WHERE id NOT IN "
            "(SELECT head_id FROM relations UNION SELECT tail_id FROM relations)"
        )
        conn.commit()


def reset_document_for_reprocess(doc_id: int):
    """清空分块与图谱关系并重置状态，用于重新解析（例如启用 OCR 后重跑扫描件）。"""
    with write_lock:
        conn = get_conn()
        conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
        conn.execute("DELETE FROM relations WHERE doc_id=?", (doc_id,))
        conn.execute(
            "DELETE FROM entities WHERE id NOT IN "
            "(SELECT head_id FROM relations UNION SELECT tail_id FROM relations)"
        )
        conn.execute(
            "UPDATE documents SET status='pending', stage='', error='', chunk_count=0, "
            "triple_count=0, embed_tag='' WHERE id=?",
            (doc_id,),
        )
        conn.commit()


def delete_document(doc_id: int):
    with write_lock:
        conn = get_conn()
        conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
        conn.execute("DELETE FROM relations WHERE doc_id=?", (doc_id,))
        conn.execute(
            "DELETE FROM entities WHERE id NOT IN "
            "(SELECT head_id FROM relations UNION SELECT tail_id FROM relations)"
        )
        conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        conn.commit()


def insert_chunks(doc_id: int, embed_tag: str, items):
    """items: [(chunk_index, text, vector_np)]"""
    with write_lock:
        conn = get_conn()
        for idx, text, vec in items:
            conn.execute(
                "INSERT INTO chunks(doc_id,chunk_index,text,vector,embed_tag) VALUES(?,?,?,?,?)",
                (doc_id, idx, text, vec.tobytes() if vec is not None else None, embed_tag),
            )
        conn.commit()


def all_vectors(embed_tag: str, kb_id: int = None):
    """返回 (ids, texts, docs, matrix)。仅加载与当前向量模型签名一致的块。
    指定 kb_id 时只加载该知识库下文档的分块，实现知识隔离。"""
    import numpy as np
    if kb_id is None:
        rows = get_conn().execute(
            "SELECT c.id, c.text, c.doc_id, c.vector, c.embed_tag FROM chunks c "
            "WHERE c.vector IS NOT NULL"
        ).fetchall()
    else:
        rows = get_conn().execute(
            "SELECT c.id, c.text, c.doc_id, c.vector, c.embed_tag FROM chunks c "
            "JOIN documents d ON c.doc_id=d.id "
            "WHERE c.vector IS NOT NULL AND d.kb_id=?",
            (kb_id,),
        ).fetchall()
    rows = [r for r in rows if r["embed_tag"] == embed_tag]
    if not rows:
        return [], [], [], None
    ids = [r["id"] for r in rows]
    texts = [r["text"] for r in rows]
    docs = [r["doc_id"] for r in rows]
    dim = len(np.frombuffer(rows[0]["vector"], dtype=np.float32))
    mat = np.zeros((len(rows), dim), dtype=np.float32)
    for i, r in enumerate(rows):
        mat[i] = np.frombuffer(r["vector"], dtype=np.float32)
    return ids, texts, docs, mat


# ---------------- knowledge graph ----------------

def bump_entity_mention(entity_id: int, etype: str = ""):
    """已存在实体：提及数 +1，类型补全（不覆盖已有类型）。"""
    with write_lock:
        conn = get_conn()
        conn.execute(
            "UPDATE entities SET mention_count=mention_count+1, "
            "type=CASE WHEN ?!='' AND type='' THEN ? ELSE type END WHERE id=?",
            (etype, etype, entity_id),
        )
        conn.commit()


def upsert_entity(name: str, etype: str) -> int:
    norm = name.strip().lower()
    conn = get_conn()
    with write_lock:
        row = conn.execute("SELECT id FROM entities WHERE norm=?", (norm,)).fetchone()
        if row:
            bump_entity_mention(row["id"], etype)
            return row["id"]
        cur = conn.execute(
            "INSERT INTO entities(name,norm,type,description) VALUES(?,?,?,'')",
            (name.strip(), norm, etype),
        )
        conn.commit()
        return cur.lastrowid


def insert_relation(head_id: int, tail_id: int, relation: str, chunk_id: int, doc_id: int,
                    inferred: int = 0):
    with write_lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO relations(head_id,tail_id,relation,chunk_id,doc_id,inferred) "
            "VALUES(?,?,?,?,?,?)",
            (head_id, tail_id, relation, chunk_id, doc_id, 1 if inferred else 0),
        )
        conn.commit()


def find_entity_by_name(name: str):
    """按名称精确查找实体（归一化匹配），返回 id 或 None。"""
    norm = (name or "").strip().lower()
    if not norm:
        return None
    row = get_conn().execute("SELECT id FROM entities WHERE norm=?", (norm,)).fetchone()
    return row["id"] if row else None


def relation_exists(head_id: int, tail_id: int, relation: str) -> bool:
    row = get_conn().execute(
        "SELECT 1 FROM relations WHERE head_id=? AND tail_id=? AND relation=? LIMIT 1",
        (head_id, tail_id, relation),
    ).fetchone()
    return row is not None


def processed_chunk_ids(doc_id: int) -> set:
    """该文档中已抽取过三元组的 chunk id（图谱增量构建时跳过）。"""
    rows = get_conn().execute(
        "SELECT DISTINCT chunk_id FROM relations WHERE doc_id=? AND chunk_id IS NOT NULL",
        (doc_id,),
    ).fetchall()
    return {r["chunk_id"] for r in rows}


def delete_doc_chunks(doc_id: int):
    """删除文档全部分块（更换向量模型重新入库时用）。"""
    with write_lock:
        conn = get_conn()
        conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
        conn.commit()


# ---------------- 实体消解（别名） ----------------

def resolve_entity_by_alias(name: str):
    """通过别名表查找规范实体，命中返回 {id,name,type}。"""
    norm = (name or "").strip().lower()
    if not norm:
        return None
    row = get_conn().execute(
        "SELECT e.id, e.name, e.type FROM entity_aliases a "
        "JOIN entities e ON a.entity_id=e.id WHERE a.norm=? LIMIT 1",
        (norm,),
    ).fetchone()
    return dict(row) if row else None


def add_entity_alias(entity_id: int, alias: str):
    alias = (alias or "").strip()
    if not alias:
        return
    with write_lock:
        conn = get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO entity_aliases(entity_id,alias,norm) VALUES(?,?,?)",
            (entity_id, alias, alias.lower()),
        )
        conn.commit()


def merge_entities(canonical_id: int, alias_ids: list):
    """把 alias_ids 指向的实体合并进 canonical_id：关系迁移、提及数累加、别名登记、删除旧实体。"""
    if not alias_ids:
        return
    with write_lock:
        conn = get_conn()
        for aid in alias_ids:
            if aid == canonical_id:
                continue
            row = conn.execute(
                "SELECT name, mention_count FROM entities WHERE id=?", (aid,)
            ).fetchone()
            if not row:
                continue
            conn.execute("UPDATE relations SET head_id=? WHERE head_id=?", (canonical_id, aid))
            conn.execute("UPDATE relations SET tail_id=? WHERE tail_id=?", (canonical_id, aid))
            conn.execute(
                "UPDATE entities SET mention_count=mention_count+?, "
                "type=CASE WHEN type='' THEN (SELECT type FROM entities WHERE id=?) ELSE type END, "
                "description=CASE WHEN description='' "
                "THEN (SELECT description FROM entities WHERE id=?) ELSE description END "
                "WHERE id=?",
                (row["mention_count"], aid, aid, canonical_id),
            )
            # 旧实体的别名记录迁到规范实体（冲突的忽略，由 CASCADE 清理）
            conn.execute(
                "UPDATE OR IGNORE entity_aliases SET entity_id=? WHERE entity_id=?",
                (canonical_id, aid),
            )
            conn.execute(
                "INSERT OR IGNORE INTO entity_aliases(entity_id,alias,norm) VALUES(?,?,?)",
                (canonical_id, row["name"], row["name"].strip().lower()),
            )
            conn.execute("DELETE FROM entities WHERE id=?", (aid,))
        conn.commit()


def entity_aliases_list(entity_id: int) -> list:
    rows = get_conn().execute(
        "SELECT alias FROM entity_aliases WHERE entity_id=? ORDER BY id", (entity_id,)
    ).fetchall()
    return [r["alias"] for r in rows]


def update_entity_description(entity_id: int, description: str):
    with write_lock:
        conn = get_conn()
        conn.execute(
            "UPDATE entities SET description=? WHERE id=?", ((description or "")[:500], entity_id)
        )
        conn.commit()


def doc_entity_ids(doc_id: int, limit: int = 200) -> list:
    """某文档图谱涉及的全部实体 id。"""
    rows = get_conn().execute(
        "SELECT DISTINCT id FROM entities WHERE id IN "
        "(SELECT head_id FROM relations WHERE doc_id=? "
        " UNION SELECT tail_id FROM relations WHERE doc_id=?) LIMIT ?",
        (doc_id, doc_id, limit),
    ).fetchall()
    return [r["id"] for r in rows]


def entity_evidence(entity_id: int, limit: int = 5) -> list:
    """实体证据文本：关系关联的 chunk + 名称命中的 chunk（用于生成描述）。"""
    conn = get_conn()
    row = conn.execute("SELECT name FROM entities WHERE id=?", (entity_id,)).fetchone()
    if not row:
        return []
    texts, seen = [], set()
    rows = conn.execute(
        "SELECT DISTINCT c.id, c.text FROM relations r JOIN chunks c ON r.chunk_id=c.id "
        "WHERE (r.head_id=? OR r.tail_id=?) LIMIT ?",
        (entity_id, entity_id, limit),
    ).fetchall()
    for r in rows:
        if r["id"] not in seen:
            seen.add(r["id"])
            texts.append(r["text"][:400])
    if len(texts) < limit:
        rows = conn.execute(
            "SELECT id, text FROM chunks WHERE text LIKE ? LIMIT ?",
            (f"%{row['name']}%", limit),
        ).fetchall()
        for r in rows:
            if r["id"] not in seen:
                seen.add(r["id"])
                texts.append(r["text"][:400])
    return texts[:limit]


def graph_stats(kb_id: int = None):
    conn = get_conn()
    if kb_id is None:
        n = conn.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"]
        r = conn.execute("SELECT COUNT(*) c FROM relations").fetchone()["c"]
        return {"entities": n, "relations": r}
    sub = "(SELECT id FROM documents WHERE kb_id=?)"
    r = conn.execute(
        f"SELECT COUNT(*) c FROM relations WHERE doc_id IN {sub}", (kb_id,)
    ).fetchone()["c"]
    n = conn.execute(
        f"SELECT COUNT(*) c FROM ("
        f"SELECT head_id AS id FROM relations WHERE doc_id IN {sub} "
        f"UNION SELECT tail_id AS id FROM relations WHERE doc_id IN {sub})",
        (kb_id, kb_id),
    ).fetchone()["c"]
    return {"entities": n, "relations": r}


def graph_data(limit=300, query=None, kb_id: int = None):
    """返回 ECharts graph 格式的节点和边，按度数取 top。query 时仅返回相关子图。
    指定 kb_id 时仅返回该知识库的实体关系子图。"""
    conn = get_conn()
    sub = "(SELECT id FROM documents WHERE kb_id=?)"
    if query:
        ents = search_entities(query, limit=30)
        ent_ids = {e["id"] for e in ents}
        if kb_id is not None:
            ent_ids &= entity_ids_in_kb(kb_id)
        # 一跳扩展
        neigh = set()
        for eid in ent_ids:
            if kb_id is None:
                rows = conn.execute(
                    "SELECT head_id,tail_id FROM relations WHERE head_id=? OR tail_id=?", (eid, eid)
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT head_id,tail_id FROM relations WHERE (head_id=? OR tail_id=?) "
                    f"AND doc_id IN {sub}", (eid, eid, kb_id)
                ).fetchall()
            for r in rows:
                neigh.add(r["head_id"]); neigh.add(r["tail_id"])
        ent_ids |= neigh
    else:
        if kb_id is None:
            rows = conn.execute(
                "SELECT e.id, COUNT(r.id)+0 AS deg FROM entities e "
                "LEFT JOIN relations r ON r.head_id=e.id OR r.tail_id=e.id "
                "GROUP BY e.id ORDER BY deg DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT e.id, COUNT(r.id)+0 AS deg FROM entities e "
                "JOIN relations r ON r.head_id=e.id OR r.tail_id=e.id "
                f"WHERE r.doc_id IN {sub} "
                "GROUP BY e.id ORDER BY deg DESC LIMIT ?",
                (kb_id, limit),
            ).fetchall()
        ent_ids = {r["id"] for r in rows}

    if not ent_ids:
        return {"nodes": [], "links": [], "categories": []}

    marks = ",".join("?" * len(ent_ids))
    ents = conn.execute(
        f"SELECT id,name,type,mention_count,description FROM entities WHERE id IN ({marks})",
        tuple(ent_ids),
    ).fetchall()
    ent_map = {r["id"]: r for r in ents}

    if kb_id is None:
        links = conn.execute(
            f"SELECT head_id,tail_id,relation,inferred FROM relations "
            f"WHERE head_id IN ({marks}) AND tail_id IN ({marks}) LIMIT 1500",
            tuple(ent_ids) * 2,
        ).fetchall()
    else:
        links = conn.execute(
            f"SELECT head_id,tail_id,relation,inferred FROM relations "
            f"WHERE head_id IN ({marks}) AND tail_id IN ({marks}) "
            f"AND doc_id IN {sub} LIMIT 1500",
            tuple(ent_ids) * 2 + (kb_id,),
        ).fetchall()

    # 类别
    cats = sorted({(r["type"] or "其他") for r in ents})
    cat_index = {c: i for i, c in enumerate(cats)}

    deg = {}
    for l in links:
        deg[l["head_id"]] = deg.get(l["head_id"], 0) + 1
        deg[l["tail_id"]] = deg.get(l["tail_id"], 0) + 1

    nodes = [
        {
            "id": r["id"],
            "name": r["name"],
            "category": cat_index.get(r["type"] or "其他", 0),
            "symbolSize": min(20 + deg.get(r["id"], 0) * 6, 60),
            "value": deg.get(r["id"], 0),
            "desc": (r["description"] or "")[:80],
        }
        for r in ents
    ]
    elinks = [
        {
            "source": ent_map[l["head_id"]]["name"],
            "target": ent_map[l["tail_id"]]["name"],
            "value": l["relation"],
            # 推断关系用紫色虚线区分
            **({"lineStyle": {"type": "dashed", "color": "#a78bfa"}} if l["inferred"] else {}),
        }
        for l in links
        if l["head_id"] in ent_map and l["tail_id"] in ent_map
    ]
    return {
        "nodes": nodes,
        "links": elinks,
        "categories": [{"name": c} for c in cats],
    }


def entity_detail(entity_id: int):
    conn = get_conn()
    e = conn.execute("SELECT id,name,type,description,mention_count FROM entities WHERE id=?", (entity_id,)).fetchone()
    if not e:
        return None
    rels = conn.execute(
        "SELECT r.relation, r.chunk_id, r.inferred, he.name hname, te.name tname "
        "FROM relations r JOIN entities he ON r.head_id=he.id JOIN entities te ON r.tail_id=te.id "
        "WHERE r.head_id=? OR r.tail_id=? LIMIT 100",
        (entity_id, entity_id),
    ).fetchall()
    return {
        "entity": dict(e),
        "relations": [dict(r) for r in rels],
        "aliases": entity_aliases_list(entity_id),
    }


def search_entities(query: str, limit=30):
    """按名称/别名匹配实体（双向子串：实体名包含关键词，或关键词包含实体名）。"""
    conn = get_conn()
    q = query.strip()
    if not q:
        return []
    rows = conn.execute(
        "SELECT DISTINCT e.id, e.name, e.type, e.mention_count FROM entities e "
        "LEFT JOIN entity_aliases a ON a.entity_id=e.id "
        "WHERE e.name LIKE ? OR ? LIKE '%' || e.name || '%' "
        "   OR a.alias LIKE ? OR ? LIKE '%' || a.alias || '%' "
        "ORDER BY e.mention_count DESC LIMIT ?",
        (f"%{q}%", q, f"%{q}%", q, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------- chats ----------------

def create_chat(title="新对话", kb_id: int = None) -> int:
    with write_lock:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO chats(kb_id,title,created_at) VALUES(?,?,?)", (kb_id, title, time.time())
        )
        conn.commit()
        return cur.lastrowid


def get_chat(chat_id: int):
    row = get_conn().execute(
        "SELECT id,kb_id,title,created_at FROM chats WHERE id=?", (chat_id,)
    ).fetchone()
    return dict(row) if row else None


def list_chats():
    rows = get_conn().execute(
        "SELECT c.id,c.kb_id,c.title,c.created_at,kb.name AS kb_name FROM chats c "
        "LEFT JOIN knowledge_bases kb ON c.kb_id=kb.id ORDER BY c.id DESC LIMIT 100"
    ).fetchall()
    return [dict(r) for r in rows]


def delete_chat(chat_id: int):
    with write_lock:
        conn = get_conn()
        conn.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
        conn.execute("DELETE FROM chats WHERE id=?", (chat_id,))
        conn.commit()


def add_message(chat_id: int, role: str, content: str, refs=None) -> int:
    with write_lock:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO messages(chat_id,role,content,refs,created_at) VALUES(?,?,?,?,?)",
            (chat_id, role, content, json.dumps(refs or [], ensure_ascii=False), time.time()),
        )
        conn.commit()
        return cur.lastrowid


def get_messages(chat_id: int):
    rows = get_conn().execute(
        "SELECT id,role,content,refs,created_at FROM messages WHERE chat_id=? ORDER BY id", (chat_id,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["refs"] = json.loads(d["refs"] or "[]")
        except Exception:
            d["refs"] = []
        out.append(d)
    return out


def get_message(message_id: int):
    row = get_conn().execute(
        "SELECT id,chat_id,role,content,refs FROM messages WHERE id=?", (message_id,)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["refs"] = json.loads(d["refs"] or "[]")
    except Exception:
        d["refs"] = []
    return d


def prev_user_query(chat_id: int, message_id: int) -> str:
    """某条助手消息之前最近的一条用户问题（反馈归属用）。"""
    row = get_conn().execute(
        "SELECT content FROM messages WHERE chat_id=? AND role='user' AND id<? "
        "ORDER BY id DESC LIMIT 1",
        (chat_id, message_id),
    ).fetchone()
    return row["content"] if row else ""


# ---------------- 反馈学习 ----------------

def add_feedback(chat_id: int, message_id: int, chunk_id: int, query: str, vote: int):
    with write_lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO feedback(chat_id,message_id,chunk_id,query,vote,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (chat_id, message_id, chunk_id, query[:200], 1 if vote >= 0 else -1, time.time()),
        )
        conn.commit()


def chunk_vote_scores() -> dict:
    """每个 chunk 的累计净投票（赞-踩），检索时用于加权。"""
    rows = get_conn().execute(
        "SELECT chunk_id, SUM(vote) s FROM feedback WHERE chunk_id IS NOT NULL GROUP BY chunk_id"
    ).fetchall()
    return {r["chunk_id"]: r["s"] for r in rows if r["s"]}


def rename_chat(chat_id: int, title: str):
    update = get_conn()
    with write_lock:
        update.execute("UPDATE chats SET title=? WHERE id=?", (title, chat_id))
        update.commit()
