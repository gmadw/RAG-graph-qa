"""SQLite 存储层：文档、分块向量、知识图谱、笔记双链、会话、设置。"""
import os
import sqlite3
import json
import threading
import time
from pathlib import Path

# 数据目录支持环境变量覆盖：单元测试隔离临时库、容器部署挂载持久卷都靠它。
DATA_DIR = Path(os.environ.get("RAG_DATA_DIR") or (Path(__file__).resolve().parent.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
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
  note_title TEXT DEFAULT '',         -- Obsidian/ Markdown 笔记标题（非笔记为空）
  note_meta TEXT DEFAULT '',          -- 笔记元信息 JSON：aliases/tags/frontmatter
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
  inferred INTEGER DEFAULT 0,         -- 1 表示图谱补全推断出的关系（非原文直述）
  kind TEXT DEFAULT 'extracted'       -- extracted 抽取 / inferred 推断 / wikilink 笔记双链
);
CREATE TABLE IF NOT EXISTS note_links(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  src_doc_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
  dst_title TEXT DEFAULT '',          -- 原始链接目标（不含锚点与展示文本）
  dst_norm TEXT DEFAULT '',           -- 归一化目标（小写去空白），用于跨笔记解析
  dst_doc_id INTEGER,                 -- 解析到的目标笔记文档 id，NULL 表示尚未创建
  kind TEXT DEFAULT 'wikilink',       -- wikilink 普通链接 / embed 嵌入
  anchor TEXT DEFAULT '',             -- [[笔记#标题]] 的锚点
  display TEXT DEFAULT '',            -- [[笔记|展示文本]] 的展示文本
  context TEXT DEFAULT '',            -- 链接所在段落（反链接引文，回答时作为上下文）
  line_no INTEGER DEFAULT 0,
  created_at REAL
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
  stats TEXT DEFAULT '',              -- 模型用量统计 JSON（token/速率），空串表示无
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
CREATE INDEX IF NOT EXISTS idx_relations_doc ON relations(doc_id);
CREATE INDEX IF NOT EXISTS idx_note_links_src ON note_links(src_doc_id);
CREATE INDEX IF NOT EXISTS idx_note_links_dst ON note_links(dst_doc_id);
CREATE INDEX IF NOT EXISTS idx_note_links_norm ON note_links(dst_norm);
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
        if "note_title" not in cols:
            conn.execute("ALTER TABLE documents ADD COLUMN note_title TEXT DEFAULT ''")
        if "note_meta" not in cols:
            conn.execute("ALTER TABLE documents ADD COLUMN note_meta TEXT DEFAULT ''")
    if "relations" in tables:
        rcols = {r["name"] for r in conn.execute("PRAGMA table_info(relations)").fetchall()}
        if "inferred" not in rcols:
            conn.execute("ALTER TABLE relations ADD COLUMN inferred INTEGER DEFAULT 0")
        if "kind" not in rcols:
            conn.execute("ALTER TABLE relations ADD COLUMN kind TEXT DEFAULT 'extracted'")
    if "chats" in tables:
        ccols = {r["name"] for r in conn.execute("PRAGMA table_info(chats)").fetchall()}
        if "kb_id" not in ccols:
            conn.execute("ALTER TABLE chats ADD COLUMN kb_id INTEGER")
    if "messages" in tables:
        mcols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "stats" not in mcols:
            conn.execute("ALTER TABLE messages ADD COLUMN stats TEXT DEFAULT ''")
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
    invalidate_search_cache()  # 文档归属映射变化，向量缓存内的 kb 过滤需重建
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
            "SELECT id,kb_id,filename,filetype,size,status,stage,error,chunk_count,triple_count,embed_tag,note_title,created_at "
            "FROM documents ORDER BY id DESC"
        ).fetchall()
    else:
        rows = get_conn().execute(
            "SELECT id,kb_id,filename,filetype,size,status,stage,error,chunk_count,triple_count,embed_tag,note_title,created_at "
            "FROM documents WHERE kb_id=? ORDER BY id DESC",
            (kb_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_document(doc_id: int):
    row = get_conn().execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    return dict(row) if row else None


def clear_doc_relations(doc_id: int):
    """清空某文档的全部三元组与孤儿实体（用于图谱重建，防止数据叠加）。

    笔记双链边（kind='wikilink'）也一并清掉，随后由 notes.link_notes 重建。
    """
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
        # 分块重建后 chunk id 全部失效，对应的反馈记录也要一并清掉，避免脏数据长期影响排序
        conn.execute(
            "DELETE FROM feedback WHERE chunk_id IN (SELECT id FROM chunks WHERE doc_id=?)",
            (doc_id,),
        )
        conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
        conn.execute("DELETE FROM relations WHERE doc_id=?", (doc_id,))
        # 重新解析会重新生成出链；入链（别的笔记指向本笔记）保留，重解析后仍然有效
        conn.execute("DELETE FROM note_links WHERE src_doc_id=?", (doc_id,))
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
    invalidate_search_cache()


def delete_document(doc_id: int):
    filename = None
    with write_lock:
        conn = get_conn()
        row = conn.execute("SELECT filename FROM documents WHERE id=?", (doc_id,)).fetchone()
        filename = row["filename"] if row else None
        conn.execute(
            "DELETE FROM feedback WHERE chunk_id IN (SELECT id FROM chunks WHERE doc_id=?)",
            (doc_id,),
        )
        conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
        conn.execute("DELETE FROM relations WHERE doc_id=?", (doc_id,))
        # 只删「本笔记作为源」的链接；指向本笔记的链接记录保留但解除绑定，
        # 这样重新上传同名笔记后能自动恢复反链接，不会留下悬空外键。
        conn.execute("DELETE FROM note_links WHERE src_doc_id=?", (doc_id,))
        conn.execute("UPDATE note_links SET dst_doc_id=NULL WHERE dst_doc_id=?", (doc_id,))
        conn.execute(
            "DELETE FROM entities WHERE id NOT IN "
            "(SELECT head_id FROM relations UNION SELECT tail_id FROM relations)"
        )
        conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        conn.commit()
    invalidate_search_cache()
    # 同步删除 uploads 下的原始文件，否则删除文档/知识库后文件会永久残留
    if filename:
        try:
            (UPLOAD_DIR / f"{doc_id}_{Path(filename).name}").unlink(missing_ok=True)
        except OSError:
            pass


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
    invalidate_search_cache()


# ---------------- 向量矩阵缓存 ----------------
# all_vectors 要从 SQLite 读出全部分块 BLOB 再拼矩阵，2 万分块约 80ms 且随规模线性增长，
# 而检索是最高频的读路径。进程内按 embed_tag 缓存矩阵（2 万 × 256 维 float32 ≈ 20MB），
# chunks/documents 发生增删时由写路径主动失效，下次检索自动重建。
_VECTOR_CACHE = {}   # embed_tag -> (ids: np.ndarray, texts: list, docs: np.ndarray, mat: np.ndarray)
_DOC_KB = None       # doc_id -> kb_id 映射（随 documents 增删失效）


def invalidate_search_cache():
    """向量矩阵与文档归属映射已过期（chunks/documents 增删后调用）。"""
    global _DOC_KB
    _VECTOR_CACHE.clear()
    _DOC_KB = None


def _doc_kb_map():
    global _DOC_KB
    if _DOC_KB is None:
        rows = get_conn().execute("SELECT id, kb_id FROM documents").fetchall()
        _DOC_KB = {r["id"]: r["kb_id"] for r in rows}
    return _DOC_KB


def all_vectors(embed_tag: str, kb_id: int = None):
    """返回 (ids, texts, docs, matrix)。仅加载与当前向量模型签名一致的块。

    embed_tag 过滤必须下推到 SQL：此前在 Python 侧过滤会把换模型前的历史向量
    也整表读出来再丢弃，多模型历史的库检索一次能拖到数百毫秒。
    指定 kb_id 时在缓存矩阵内按文档归属过滤，实现知识隔离。
    """
    import numpy as np
    cached = _VECTOR_CACHE.get(embed_tag)
    if cached is None:
        rows = get_conn().execute(
            "SELECT id, text, doc_id, vector FROM chunks "
            "WHERE vector IS NOT NULL AND embed_tag=?",
            (embed_tag,),
        ).fetchall()
        if not rows:
            return [], [], [], None
        dim = len(np.frombuffer(rows[0]["vector"], dtype=np.float32))
        n = len(rows)
        ids = np.empty(n, dtype=np.int64)
        docs = np.empty(n, dtype=np.int64)
        mat = np.empty((n, dim), dtype=np.float32)
        texts = [None] * n
        for i, r in enumerate(rows):
            ids[i] = r["id"]
            docs[i] = r["doc_id"]
            texts[i] = r["text"]
            mat[i] = np.frombuffer(r["vector"], dtype=np.float32)
        cached = (ids, texts, docs, mat)
        _VECTOR_CACHE[embed_tag] = cached
    ids, texts, docs, mat = cached
    if kb_id is None:
        return ids, texts, docs, mat
    doc_kb = _doc_kb_map()
    idx = np.nonzero(np.array([doc_kb.get(int(d)) == kb_id for d in docs], dtype=bool))[0]
    if idx.size == 0:
        return [], [], [], None
    return (
        ids[idx],
        [texts[i] for i in idx],
        docs[idx],
        mat[idx],
    )


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
                    inferred: int = 0, kind: str = ""):
    """插入三元组。kind 未指定时按 inferred 推断为 extracted / inferred。"""
    kind = kind or ("inferred" if inferred else "extracted")
    with write_lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO relations(head_id,tail_id,relation,chunk_id,doc_id,inferred,kind) "
            "VALUES(?,?,?,?,?,?,?)",
            (head_id, tail_id, relation, chunk_id, doc_id, 1 if inferred else 0, kind),
        )
        conn.commit()


def insert_relations_bulk(items):
    """批量插入三元组（单事务）。

    笔记双链重建时可能有上千条边，逐条 commit 会让批量导入笔记明显变慢。
    items: [(head_id, tail_id, relation, chunk_id, doc_id, kind)]
    """
    if not items:
        return 0
    with write_lock:
        conn = get_conn()
        conn.executemany(
            "INSERT INTO relations(head_id,tail_id,relation,chunk_id,doc_id,inferred,kind) "
            "VALUES(?,?,?,?,?,0,?)",
            items,
        )
        conn.commit()
    return len(items)


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
    invalidate_search_cache()


# ---------------- Obsidian 笔记与双链 ----------------

_NOTE_EXT = (".md", ".markdown")


def note_norm(name: str) -> str:
    """笔记链接归一化：去 .md 后缀、去首尾空白、压缩连续空白、统一小写。

    Obsidian 的 `[[笔记名]]` 不带扩展名，而文件名可能带 .md；两者必须归一到同一键，
    否则「先传 A 后传 B」时 A 里指向 B 的链接永远解析不到。
    """
    n = (name or "").strip().replace("\\", "/").split("/")[-1]
    for ext in _NOTE_EXT:
        if n.lower().endswith(ext):
            n = n[: -len(ext)]
            break
    return " ".join(n.split()).lower()


def set_note_meta(doc_id: int, title: str, aliases=None, tags=None, frontmatter=None):
    """写入笔记标题与元信息（别名/标签/frontmatter）。"""
    meta = {
        "aliases": [str(a).strip() for a in (aliases or []) if str(a).strip()],
        "tags": [str(t).strip().lstrip("#") for t in (tags or []) if str(t).strip()],
        "frontmatter": frontmatter or {},
    }
    with write_lock:
        conn = get_conn()
        conn.execute(
            "UPDATE documents SET note_title=?, note_meta=? WHERE id=?",
            ((title or "").strip()[:200], json.dumps(meta, ensure_ascii=False), doc_id),
        )
        conn.commit()


def get_note_meta(doc_id: int) -> dict:
    row = get_conn().execute(
        "SELECT id,kb_id,filename,note_title,note_meta FROM documents WHERE id=?", (doc_id,)
    ).fetchone()
    if not row:
        return {}
    try:
        meta = json.loads(row["note_meta"] or "{}")
    except Exception:
        meta = {}
    return {
        "doc_id": row["id"],
        "kb_id": row["kb_id"],
        "filename": row["filename"],
        "title": row["note_title"] or "",
        "aliases": meta.get("aliases") or [],
        "tags": meta.get("tags") or [],
        "frontmatter": meta.get("frontmatter") or {},
    }


def replace_note_links(doc_id: int, links):
    """以本次解析结果整体替换某笔记的出链（幂等，重解析不会累加）。

    links: [{target, anchor, display, kind, context, line_no}]
    """
    with write_lock:
        conn = get_conn()
        conn.execute("DELETE FROM note_links WHERE src_doc_id=?", (doc_id,))
        now = time.time()
        for ln in links or []:
            target = (ln.get("target") or "").strip()
            if not target:
                continue
            conn.execute(
                "INSERT INTO note_links(src_doc_id,dst_title,dst_norm,kind,anchor,display,context,line_no,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    doc_id,
                    target[:200],
                    note_norm(target),
                    (ln.get("kind") or "wikilink")[:20],
                    (ln.get("anchor") or "")[:200],
                    (ln.get("display") or "")[:200],
                    (ln.get("context") or "")[:500],
                    int(ln.get("line_no") or 0),
                    now,
                ),
            )
        conn.commit()


def note_norm_map(kb_id: int = None) -> dict:
    """笔记标题与别名 → 文档 id 的映射。

    同名笔记冲突时保留先创建的（setdefault），避免后上传的笔记把既有反链接整体吸走。
    """
    if kb_id is None:
        rows = get_conn().execute(
            "SELECT id,note_title,note_meta FROM documents WHERE note_title!='' ORDER BY id"
        ).fetchall()
    else:
        rows = get_conn().execute(
            "SELECT id,note_title,note_meta FROM documents "
            "WHERE note_title!='' AND kb_id=? ORDER BY id",
            (kb_id,),
        ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(note_norm(r["note_title"]), r["id"])
        try:
            meta = json.loads(r["note_meta"] or "{}")
        except Exception:
            meta = {}
        for alias in meta.get("aliases") or []:
            key = note_norm(alias)
            if key:
                out.setdefault(key, r["id"])
    return out


def resolve_note_links(kb_id: int = None) -> int:
    """把未解析的双链目标绑定到实际笔记文档，返回本次新解析成功的链接数。

    上传顺序不确定（A 链接 B，但 A 先入库），因此每次入库后都要重跑一遍全量解析。
    """
    norm_map = note_norm_map(kb_id)
    if not norm_map:
        return 0
    with write_lock:
        conn = get_conn()
        if kb_id is None:
            rows = conn.execute(
                "SELECT id,dst_norm FROM note_links WHERE dst_doc_id IS NULL"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT l.id,l.dst_norm FROM note_links l JOIN documents d ON d.id=l.src_doc_id "
                "WHERE l.dst_doc_id IS NULL AND d.kb_id=?",
                (kb_id,),
            ).fetchall()
        fixed = 0
        for r in rows:
            dst = norm_map.get(r["dst_norm"])
            if dst:
                conn.execute("UPDATE note_links SET dst_doc_id=? WHERE id=?", (dst, r["id"]))
                fixed += 1
        conn.commit()
    return fixed


def note_backlinks(doc_id: int, limit: int = 20):
    """指向该笔记的链接（反链接），带链接所在段落作为引文上下文。"""
    rows = get_conn().execute(
        "SELECT l.id, l.src_doc_id, l.anchor, l.display, l.context, l.line_no, l.kind, "
        "       d.note_title AS src_title, d.filename AS src_filename "
        "FROM note_links l JOIN documents d ON d.id=l.src_doc_id "
        "WHERE l.dst_doc_id=? ORDER BY l.id LIMIT ?",
        (doc_id, limit),
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "doc_id": r["src_doc_id"],
            "title": r["src_title"] or r["src_filename"],
            "filename": r["src_filename"],
            "anchor": r["anchor"],
            "display": r["display"],
            "context": r["context"],
            "line_no": r["line_no"],
            "kind": r["kind"],
        })
    return out


def note_outlinks(doc_id: int, limit: int = 50):
    """该笔记指向其他笔记的链接（出链），同时标记是否已解析。"""
    rows = get_conn().execute(
        "SELECT l.id, l.dst_title, l.dst_doc_id, l.anchor, l.display, l.kind, l.line_no, "
        "       d.note_title AS dst_doc_title "
        "FROM note_links l LEFT JOIN documents d ON d.id=l.dst_doc_id "
        "WHERE l.src_doc_id=? ORDER BY l.id LIMIT ?",
        (doc_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def notes_in_kb(kb_id: int = None):
    """知识库内的笔记列表（按入链数降序，体现「被引用最多」的知识节点）。"""
    if kb_id is None:
        rows = get_conn().execute(
            "SELECT d.id AS doc_id, d.note_title, d.filename, d.note_meta, d.status, "
            " (SELECT COUNT(*) FROM note_links l WHERE l.src_doc_id=d.id) AS outlinks, "
            " (SELECT COUNT(*) FROM note_links l WHERE l.dst_doc_id=d.id) AS inlinks "
            "FROM documents d WHERE d.note_title!='' ORDER BY inlinks DESC, d.id"
        ).fetchall()
    else:
        rows = get_conn().execute(
            "SELECT d.id AS doc_id, d.note_title, d.filename, d.note_meta, d.status, "
            " (SELECT COUNT(*) FROM note_links l WHERE l.src_doc_id=d.id) AS outlinks, "
            " (SELECT COUNT(*) FROM note_links l WHERE l.dst_doc_id=d.id) AS inlinks "
            "FROM documents d WHERE d.note_title!='' AND d.kb_id=? ORDER BY inlinks DESC, d.id",
            (kb_id,),
        ).fetchall()
    out = []
    for r in rows:
        try:
            meta = json.loads(r["note_meta"] or "{}")
        except Exception:
            meta = {}
        out.append({
            "doc_id": r["doc_id"],
            "title": r["note_title"],
            "filename": r["filename"],
            "status": r["status"],
            "tags": meta.get("tags") or [],
            "aliases": meta.get("aliases") or [],
            "inlinks": r["inlinks"],
            "outlinks": r["outlinks"],
        })
    return out


def unresolved_links(kb_id: int = None, limit: int = 50):
    """指向「尚不存在笔记」的链接（Obsidian 里显示为未创建节点）。"""
    if kb_id is None:
        rows = get_conn().execute(
            "SELECT l.id,l.dst_title,d.note_title AS src_title,d.id AS src_doc_id "
            "FROM note_links l JOIN documents d ON d.id=l.src_doc_id "
            "WHERE l.dst_doc_id IS NULL ORDER BY l.id LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        rows = get_conn().execute(
            "SELECT l.id,l.dst_title,d.note_title AS src_title,d.id AS src_doc_id "
            "FROM note_links l JOIN documents d ON d.id=l.src_doc_id "
            "WHERE l.dst_doc_id IS NULL AND d.kb_id=? ORDER BY l.id LIMIT ?",
            (kb_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def note_link_stats(kb_id: int = None) -> dict:
    """笔记与双链规模统计（前端图谱页展示「知识连接度」）。"""
    conn = get_conn()
    if kb_id is None:
        notes = conn.execute("SELECT COUNT(*) c FROM documents WHERE note_title!=''").fetchone()["c"]
        links = conn.execute("SELECT COUNT(*) c FROM note_links").fetchone()["c"]
        resolved = conn.execute(
            "SELECT COUNT(*) c FROM note_links WHERE dst_doc_id IS NOT NULL"
        ).fetchone()["c"]
    else:
        notes = conn.execute(
            "SELECT COUNT(*) c FROM documents WHERE note_title!='' AND kb_id=?", (kb_id,)
        ).fetchone()["c"]
        links = conn.execute(
            "SELECT COUNT(*) c FROM note_links l JOIN documents d ON d.id=l.src_doc_id WHERE d.kb_id=?",
            (kb_id,),
        ).fetchone()["c"]
        resolved = conn.execute(
            "SELECT COUNT(*) c FROM note_links l JOIN documents d ON d.id=l.src_doc_id "
            "WHERE l.dst_doc_id IS NOT NULL AND d.kb_id=?",
            (kb_id,),
        ).fetchone()["c"]
    return {
        "notes": notes,
        "links": links,
        "resolved": resolved,
        "unresolved": max(0, links - resolved),
    }


def note_chunks(doc_id: int, limit: int = 3):
    """笔记的前若干分块文本（反链接上下文与回答引用用）。"""
    rows = get_conn().execute(
        "SELECT id,chunk_index,text FROM chunks WHERE doc_id=? ORDER BY chunk_index LIMIT ?",
        (doc_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def chunk_id_for_text(doc_id: int, needle: str):
    """在文档分块里定位包含指定文本的块，用于把笔记链接边关联回原文（引用溯源）。"""
    if not needle:
        return None
    escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    row = get_conn().execute(
        "SELECT id FROM chunks WHERE doc_id=? AND text LIKE ? ESCAPE '\\' "
        "ORDER BY chunk_index LIMIT 1",
        (doc_id, f"%{escaped}%"),
    ).fetchone()
    return row["id"] if row else None


def clear_wikilink_relations(kb_id: int = None):
    """清空双链边（重建链接前调用），并清理因此产生的孤儿笔记实体。"""
    with write_lock:
        conn = get_conn()
        if kb_id is None:
            conn.execute("DELETE FROM relations WHERE kind='wikilink'")
        else:
            conn.execute(
                "DELETE FROM relations WHERE kind='wikilink' AND doc_id IN "
                "(SELECT id FROM documents WHERE kb_id=?)",
                (kb_id,),
            )
        conn.execute(
            "DELETE FROM entities WHERE id NOT IN "
            "(SELECT head_id FROM relations UNION SELECT tail_id FROM relations)"
        )
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


def set_entity_description_if_empty(entity_id: int, description: str):
    """只在实体尚无描述时写入（笔记首段兜底描述，不覆盖 LLM 生成的摘要）。"""
    if not (description or "").strip():
        return
    with write_lock:
        conn = get_conn()
        conn.execute(
            "UPDATE entities SET description=? WHERE id=? AND (description IS NULL OR description='')",
            (description.strip()[:500], entity_id),
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


def top_entity_ids(kb_id: int = None, limit: int = 5) -> list:
    """图上最活跃的实体 id（按提及次数）。用于无明确问题时给图谱解读一个起点。"""
    if kb_id is None:
        rows = get_conn().execute(
            "SELECT id FROM entities WHERE id IN "
            "(SELECT head_id FROM relations UNION SELECT tail_id FROM relations) "
            "ORDER BY mention_count DESC, id LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        sub = "(SELECT id FROM documents WHERE kb_id=?)"
        rows = get_conn().execute(
            "SELECT id FROM entities WHERE id IN ("
            "  SELECT head_id FROM relations WHERE doc_id IN " + sub +
            "  UNION SELECT tail_id FROM relations WHERE doc_id IN " + sub +
            ") ORDER BY mention_count DESC, id LIMIT ?",
            (kb_id, kb_id, limit),
        ).fetchall()
    return [r["id"] for r in rows]


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
            f"SELECT head_id,tail_id,relation,inferred,kind FROM relations "
            f"WHERE head_id IN ({marks}) AND tail_id IN ({marks}) LIMIT 1500",
            tuple(ent_ids) * 2,
        ).fetchall()
    else:
        links = conn.execute(
            f"SELECT head_id,tail_id,relation,inferred,kind FROM relations "
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
            "kind": l["kind"] or "extracted",
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


def entity_detail(entity_id: int, kb_id: int = None):
    """实体详情。指定 kb_id 时关系只取该知识库内文档产生的，避免多库之间串数据。"""
    conn = get_conn()
    e = conn.execute("SELECT id,name,type,description,mention_count FROM entities WHERE id=?", (entity_id,)).fetchone()
    if not e:
        return None
    sql = (
        "SELECT r.relation, r.chunk_id, r.inferred, he.name hname, te.name tname "
        "FROM relations r JOIN entities he ON r.head_id=he.id JOIN entities te ON r.tail_id=te.id "
        "WHERE (r.head_id=? OR r.tail_id=?)"
    )
    params = [entity_id, entity_id]
    if kb_id is not None:
        sql += " AND r.doc_id IN (SELECT id FROM documents WHERE kb_id=?)"
        params.append(kb_id)
    rels = conn.execute(sql + " LIMIT 100", tuple(params)).fetchall()
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
        # 反馈是全局学习的：会话删除后其赞/踩不应继续影响后续检索
        conn.execute("DELETE FROM feedback WHERE chat_id=?", (chat_id,))
        conn.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
        conn.execute("DELETE FROM chats WHERE id=?", (chat_id,))
        conn.commit()


def add_message(chat_id: int, role: str, content: str, refs=None, stats=None) -> int:
    with write_lock:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO messages(chat_id,role,content,refs,stats,created_at) VALUES(?,?,?,?,?,?)",
            (chat_id, role, content,
             json.dumps(refs or [], ensure_ascii=False),
             json.dumps(stats, ensure_ascii=False) if stats else "",
             time.time()),
        )
        conn.commit()
        return cur.lastrowid


def _parse_message_row(d: dict) -> dict:
    """refs / stats JSON 字段反序列化（脏数据时回退默认值）。"""
    try:
        d["refs"] = json.loads(d.get("refs") or "[]")
    except Exception:
        d["refs"] = []
    try:
        d["stats"] = json.loads(d["stats"]) if d.get("stats") else None
    except Exception:
        d["stats"] = None
    return d


def get_messages(chat_id: int):
    rows = get_conn().execute(
        "SELECT id,role,content,refs,stats,created_at FROM messages WHERE chat_id=? ORDER BY id",
        (chat_id,),
    ).fetchall()
    return [_parse_message_row(dict(r)) for r in rows]


def get_message(message_id: int):
    row = get_conn().execute(
        "SELECT id,chat_id,role,content,refs,stats FROM messages WHERE id=?", (message_id,)
    ).fetchone()
    if not row:
        return None
    return _parse_message_row(dict(row))


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
