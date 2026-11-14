"""Obsidian 风格 Markdown 笔记：frontmatter、双链（wikilink）、标签与反链接索引。

设计目标（对齐 Obsidian 的「知识网络」体验）：
  1. 解析 `[[笔记]]` / `[[笔记|展示]]` / `[[笔记#锚点]]` / `![[嵌入]]`
  2. 把双链落成知识图谱里的真实边（kind='wikilink'），让图检索能用上笔记结构
  3. 提供反链接（backlink）与出链（outlink），并在问答时注入「谁引用了这条笔记」
  4. 目标笔记尚未上传时保留为未解析链接，重新入库后自动补齐（上传顺序无关）
"""
import re
from pathlib import Path

from . import db

NOTE_SUFFIXES = {".md", ".markdown"}
NOTE_ENTITY_TYPE = "笔记"
MAX_LINKS_PER_NOTE = 500

_FM_FENCE = "---"
_WIKILINK_RE = re.compile(r"(!?)\[\[([^\[\]]{1,300})\]\]")
# 标准 Markdown 内部链接：[显示](笔记.md) / [显示](../笔记.md#锚点)
# 排除图片 ![]()、外链、页内锚点：这些不是笔记引用
_MDLINK_RE = re.compile(r"(?<!!)\[([^\]\n]{1,120})\]\(\s*<?([^)\s>]{1,300})>?(?:\s+\"[^\"]*\")?\s*\)")
# URL 锚点（https://a/#section）不是 Obsidian 标签，前缀是 `/` 时排除
_TAG_RE = re.compile(r"(?<![#\w/])#([A-Za-z0-9_\u4e00-\u9fff][\w\u4e00-\u9fff/\-]{0,40})")
_FENCE_RE = re.compile(r"```.*?```|~~~.*?~~~", re.S)
_LIST_ITEM_RE = re.compile(r"^\s*-\s+(.+?)\s*$")


def is_note(path) -> bool:
    """只有 Markdown 走双链解析（PDF/Word 里的 [[...]] 是正文符号，不该建边）。"""
    return Path(str(path)).suffix.lower() in NOTE_SUFFIXES


# ---------------- frontmatter ----------------

def _clean_scalar(value: str) -> str:
    v = (value or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v.strip()


def _split_inline_list(value: str) -> list:
    """支持 `aliases: [A, B]` 与 `tags: a, b` 两种写法。"""
    v = value.strip()
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1]
    return [_clean_scalar(x) for x in re.split(r"[,，]", v) if _clean_scalar(x)]


def _parse_frontmatter_block(lines) -> dict:
    """极简 YAML 子集解析：标题行、列表项、内联列表、标量。

    不引入 PyYAML：只需要 title / aliases / tags 三个字段，且必须容忍用户写坏的 YAML。
    """
    meta, current_key, current_list = {}, None, False
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        item = _LIST_ITEM_RE.match(raw)
        if item and current_key:
            if not current_list:
                meta[current_key] = []
                current_list = True
            meta[current_key].append(_clean_scalar(item.group(1)))
            continue
        if ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        key, value = key.strip(), value.strip()
        if not key:
            continue
        current_key, current_list = key, False
        if value == "":
            meta[key] = []          # 后面可能跟 `- item` 列表
            continue
        if value.startswith("[") or "," in value:
            meta[key] = _split_inline_list(value)
        else:
            meta[key] = _clean_scalar(value)
    return meta


def parse_frontmatter(text: str):
    """返回 (frontmatter dict, 正文)。文档不以 `---` 开头时视为无 frontmatter。"""
    if not (text or "").startswith(_FM_FENCE):
        return {}, text or ""
    lines = (text or "").splitlines()
    if lines[0].strip() != _FM_FENCE:
        return {}, text
    end = None
    for i in range(1, min(len(lines), 300)):
        if lines[i].strip() == _FM_FENCE:
            end = i
            break
    if end is None:
        return {}, text
    return _parse_frontmatter_block(lines[1:end]), "\n".join(lines[end + 1:])


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return _split_inline_list(value) if ("," in value or "，" in value) else [value.strip()]
    return []


def note_title(path, frontmatter: dict = None) -> str:
    """笔记标题：frontmatter.title 优先，否则文件名（去 .md）。"""
    fm = frontmatter or {}
    title = _clean_scalar(str(fm.get("title") or ""))
    if title:
        return title[:200]
    return Path(str(path)).stem[:200]


def note_aliases(frontmatter: dict) -> list:
    """兼容 aliases / alias / 别名 三种常见写法。"""
    fm = frontmatter or {}
    out = []
    for key in ("aliases", "alias", "别名"):
        out.extend(_as_list(fm.get(key)))
    seen, uniq = set(), []
    for a in out:
        k = db.note_norm(a)
        if k and k not in seen:
            seen.add(k)
            uniq.append(a)
    return uniq[:20]


# ---------------- 双链与标签 ----------------

def _line_at(text: str, pos: int) -> tuple:
    """返回 (行号, 行文本)。链接引文取整行，便于反链接展示上下文。"""
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    if end == -1:
        end = len(text)
    return text.count("\n", 0, pos) + 1, text[start:end]


def _strip_link_syntax(line: str) -> str:
    """引文里的链接语法还原为可读文本：[[A|B]] → B，![[A]] → A。"""
    def repl(m):
        inner = m.group(2)
        target, _, display = inner.partition("|")
        label = display or target.split("#")[0]
        return label.strip()
    return _WIKILINK_RE.sub(repl, line).strip()


def _split_unescaped_pipe(inner: str):
    """`[[A|B]]` 按竖线切分。

    表格里的写法是 `[[A\\|B]]`：这个反斜杠只是 Markdown 表格的转义，语义上仍是分隔符，
    因此先统一还原再切分，否则 target 会变成 "A|B"。
    """
    inner = inner.replace("\\|", "|")
    target, _, display = inner.partition("|")
    return target.strip(), display.strip()


def extract_links(text: str) -> list:
    """抽取笔记内部链接。返回 [{target, anchor, display, kind, context, line_no}]。

    - `[[笔记]]` / `[[笔记|展示]]` / `[[笔记#锚点]]` → kind=wikilink
    - `![[嵌入]]` → kind=embed
    - `[展示](笔记.md)` → kind=mdlink（标准 Markdown 也能用，不强制 Obsidian）
    - 纯锚点链接（`[[#标题]]`、`[x](#标题)`）与外部 URL 跳过
    """
    links, seen = [], set()
    body = text or ""
    for m in _WIKILINK_RE.finditer(body):
        embed = bool(m.group(1))
        inner = (m.group(2) or "").strip()
        if not inner:
            continue
        target_part, display = _split_unescaped_pipe(inner)
        target, _, anchor = target_part.partition("#")
        target, anchor = target.strip(), anchor.strip()
        if not target:
            continue
        line_no, line = _line_at(body, m.start())
        context = _strip_link_syntax(line)
        if len(context) > 300:
            context = context[:300]
        key = (db.note_norm(target), anchor, "embed" if embed else "wikilink")
        if key in seen:
            continue
        seen.add(key)
        links.append({
            "target": target,
            "anchor": anchor,
            "display": display,
            "kind": "embed" if embed else "wikilink",
            "context": context,
            "line_no": line_no,
        })
        if len(links) >= MAX_LINKS_PER_NOTE:
            return links

    for m in _MDLINK_RE.finditer(body):
        display, href = (m.group(1) or "").strip(), (m.group(2) or "").strip()
        if re.match(r"^(https?:|mailto:|tel:|#)", href, re.I):
            continue
        path_part, _, anchor = href.partition("#")
        if not path_part.lower().endswith((".md", ".markdown")):
            continue
        target = path_part.replace("\\", "/").split("/")[-1]
        if not target:
            continue
        line_no, line = _line_at(body, m.start())
        context = re.sub(r"\[([^\]]*)\]\([^)]*\)", lambda mm: mm.group(1), line).strip()
        key = (db.note_norm(target), anchor, "mdlink")
        if key in seen:
            continue
        seen.add(key)
        links.append({
            "target": target,
            "anchor": anchor,
            "display": display,
            "kind": "mdlink",
            "context": context[:300],
            "line_no": line_no,
        })
        if len(links) >= MAX_LINKS_PER_NOTE:
            break
    return links


def extract_tags(text: str) -> list:
    """抽取 `#标签`。跳过代码围栏与 Markdown 标题行，避免把 `# 标题` 当标签。"""
    body = _FENCE_RE.sub(" ", text or "")
    out, seen = [], set()
    for m in _TAG_RE.finditer(body):
        line_no, line = _line_at(body, m.start())
        if line.lstrip().startswith("#"):
            continue
        tag = m.group(1).strip()
        if not tag or tag.isdigit():
            continue
        if tag.lower() in seen:
            continue
        seen.add(tag.lower())
        out.append(tag)
    return out[:50]


# ---------------- 入库与链接解析 ----------------

def sync_note(doc_id: int, path, text: str, body: str = None, source_name: str = None) -> dict:
    """解析并落库笔记元信息与出链（不做跨笔记解析）。返回摘要。

    body 为已剥掉 frontmatter 的正文（入库分块用），避免 YAML 头部混进向量与引用。
    source_name 是用户原始文件名：磁盘上的文件带 `{doc_id}_` 前缀，不能用来推导标题，
    否则所有没写 frontmatter.title 的笔记都会变成「12_笔记名」这种脏标题。
    """
    fm, parsed_body = parse_frontmatter(text)
    body = parsed_body if body is None else body
    title = note_title(source_name or path, fm)
    aliases = note_aliases(fm)
    tags = extract_tags(body)
    for t in _as_list(fm.get("tags")):
        if t.lstrip("#") not in tags:
            tags.append(t.lstrip("#"))
    links = extract_links(body)
    db.set_note_meta(doc_id, title, aliases=aliases, tags=tags, frontmatter=fm)
    db.replace_note_links(doc_id, links)
    return {"title": title, "aliases": aliases, "tags": tags, "links": len(links)}


def _entity_for(title: str) -> int:
    """笔记标题映射为实体。已存在则复用，避免每次重建链接都把 mention_count 刷高。"""
    hit = db.find_entity_by_name(title)
    return hit or db.upsert_entity(title, NOTE_ENTITY_TYPE)


def _plain_excerpt(text: str, limit: int = 80) -> str:
    """笔记首段 → 实体描述：跳过标题行、剥掉 Markdown 标记与链接语法，只留可读文字。"""
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("---"):
            continue
        s = _strip_link_syntax(s)
        s = re.sub(r"^[>\-\*\s]+", "", s)
        s = re.sub(r"[`*_]{1,3}", "", s)
        s = re.sub(r"\s+", " ", s).strip()
        if s:
            return s[:limit]
    return ""


def resync_document(doc) -> dict:
    """从 uploads 里的原始文件重新解析一篇笔记（不动分块与向量）。

    用途：改造前入库的 .md 没有 note_title/note_links，点一次「重建双链」即可补齐，
    不必逐篇「重解析」（重解析会重新向量化，成本高）。
    """
    from .documents import extract_text  # 延迟导入：documents 依赖 notes，避免循环

    filename = doc.get("filename") or ""
    if not is_note(filename):
        return None
    path = db.UPLOAD_DIR / f"{doc['id']}_{Path(filename).name}"
    if not path.exists():
        return None
    text = extract_text(path)
    _, body = parse_frontmatter(text)
    return sync_note(doc["id"], path, text, body=body, source_name=filename)


def backfill_notes(kb_id: int = None) -> int:
    """为「已入库但没有笔记元信息」的 Markdown 文档补解析，返回补解析篇数。

    只补 note_title 为空的：正常上传流程已经解析过，重复解析没有意义。
    单篇失败（文件丢失/编码异常）跳过，不影响其它笔记。
    """
    done = 0
    for doc in db.list_documents(kb_id):
        if doc.get("note_title") or not is_note(doc.get("filename") or ""):
            continue
        try:
            if resync_document(doc):
                done += 1
        except Exception:
            continue
    return done


def link_notes(kb_id: int = None) -> int:
    """把已解析的双链写成图谱边（kind='wikilink'），返回新建边数。

    幂等：先清掉范围内旧的双链边再重建，保证重解析/重复上传不会让边叠加。
    """
    db.resolve_note_links(kb_id)
    db.clear_wikilink_relations(kb_id)
    added = 0
    seen = set()
    eid_cache = {}

    def eid_of(title: str) -> int:
        if title not in eid_cache:
            eid_cache[title] = _entity_for(title)
        return eid_cache[title]

    pending = []
    for note in db.notes_in_kb(kb_id):
        src_doc = note["doc_id"]
        src_title = note["title"]
        for out in db.note_outlinks(src_doc, limit=MAX_LINKS_PER_NOTE):
            if not out.get("dst_doc_id"):
                continue
            dst_title = out.get("dst_doc_title") or out.get("dst_title") or ""
            if not dst_title or db.note_norm(dst_title) == db.note_norm(src_title):
                continue
            relation = "嵌入" if out.get("kind") == "embed" else "链接到"
            key = (src_title, dst_title, relation)
            if key in seen:
                continue
            seen.add(key)
            # 引用溯源：按 `[[目标` 能在原文里稳定命中（context 已剥掉链接语法，直接 LIKE 会失败）
            chunk_id = (
                db.chunk_id_for_text(src_doc, f"[[{out.get('dst_title') or ''}")
                or db.chunk_id_for_text(src_doc, (out.get("context") or "")[:60])
            )
            pending.append((eid_of(src_title), eid_of(dst_title), relation,
                            chunk_id, src_doc, "wikilink"))
            added += 1
    db.insert_relations_bulk(pending)

    # 笔记实体也要有描述，否则图解读里的实体档案只有名字，模型无法判断笔记讲什么。
    # 只在描述为空时用首段兜底，绝不覆盖 LLM 抽取阶段生成的摘要。
    for note in db.notes_in_kb(kb_id):
        chunks = db.note_chunks(note["doc_id"], limit=1)
        if chunks:
            excerpt = _plain_excerpt(chunks[0]["text"])
            if excerpt:
                db.set_entity_description_if_empty(eid_of(note["title"]), excerpt)
    return added


# ---------------- 问答上下文 ----------------

def _titles_of(doc_ids) -> dict:
    return {d: (db.get_note_meta(d) or {}).get("title", "") for d in doc_ids}


def backlink_context(doc_id: int, limit: int = 5) -> str:
    """单条笔记的反链接上下文（谁引用了它、在什么语境下引用）。"""
    rows = db.note_backlinks(doc_id, limit=limit)
    if not rows:
        return ""
    lines = []
    for r in rows:
        anchor = f"（锚点：{r['anchor']}）" if r.get("anchor") else ""
        quote = (r.get("context") or "").strip()
        if quote:
            lines.append(f"- 《{r['title']}》{anchor}提到：{quote[:160]}")
        else:
            lines.append(f"- 《{r['title']}》{anchor}链接到本笔记")
    return "【反向链接：引用本笔记的其他笔记】\n" + "\n".join(lines)


def note_context(doc_id: int, backlink_limit: int = 5) -> str:
    """笔记的完整结构上下文：标题/标签/出链/反链接。用于让 AI 理解知识结构。"""
    meta = db.get_note_meta(doc_id)
    if not meta or not meta.get("title"):
        return ""
    parts = [f"笔记：《{meta['title']}》"]
    if meta.get("aliases"):
        parts.append("别名：" + "、".join(meta["aliases"]))
    if meta.get("tags"):
        parts.append("标签：" + "、".join("#" + t for t in meta["tags"]))
    outs = [o for o in db.note_outlinks(doc_id, limit=20) if o.get("dst_doc_id")]
    if outs:
        # 同一目标可能同时被「链接」和「嵌入」，展示时合并，避免《A》、《A》重复
        titles, seen = [], set()
        for o in outs:
            t = o.get("dst_doc_title") or o["dst_title"]
            if t and t not in seen:
                seen.add(t)
                titles.append(f"《{t}》")
        parts.append("本文链接到：" + "、".join(titles))
    blk = backlink_context(doc_id, limit=backlink_limit)
    if blk:
        parts.append(blk)
    return "\n".join(parts)


def context_for_chunks(chunks, kb_id: int = None, max_notes: int = 3) -> str:
    """检索命中笔记分块时，补充这些笔记的结构信息（反链接 + 出链）。

    只对命中 chunk 所属的笔记生效：论文/PDF 没有笔记结构，不会产生噪声。
    """
    doc_ids = []
    for c in chunks or []:
        did = c.get("doc_id")
        if did and did not in doc_ids:
            doc_ids.append(did)
        if len(doc_ids) >= max_notes:
            break
    blocks = [note_context(d, backlink_limit=4) for d in doc_ids]
    return "\n\n".join(b for b in blocks if b)[:2500]


def graph_notes_summary(kb_id: int = None, limit: int = 12) -> str:
    """知识库层面的笔记网络摘要：连接度最高的笔记 + 未解析链接（知识缺口）。"""
    notes = db.notes_in_kb(kb_id)[:limit]
    stats = db.note_link_stats(kb_id)
    if not stats["notes"]:
        return ""
    lines = [
        f"共 {stats['notes']} 篇笔记、{stats['links']} 条双链（已解析 {stats['resolved']} 条）。"
    ]
    hubs = [n for n in notes if n["inlinks"] or n["outlinks"]][:8]
    if hubs:
        lines.append("连接度最高的笔记：")
        for n in hubs:
            lines.append(f"- 《{n['title']}》入链 {n['inlinks']} / 出链 {n['outlinks']}")
    unresolved = db.unresolved_links(kb_id, limit=6)
    if unresolved:
        lines.append("尚未创建的链接目标（知识缺口）：" + "、".join(
            f"《{u['dst_title']}》" for u in unresolved
        ))
    return "【笔记网络】\n" + "\n".join(lines)