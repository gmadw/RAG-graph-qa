"""Obsidian 笔记解析纯函数测试：frontmatter、双链、标签、标题与别名。"""
from app import notes


def test_parse_frontmatter_extracts_title_aliases_tags():
    text = "---\ntitle: 分布式共识\naliases: [Consensus, 共识算法]\ntags:\n  - 分布式\n  - 算法\n---\n正文内容"
    fm, body = notes.parse_frontmatter(text)
    assert fm["title"] == "分布式共识"
    assert fm["aliases"] == ["Consensus", "共识算法"]
    assert fm["tags"] == ["分布式", "算法"]
    assert body == "正文内容"


def test_parse_frontmatter_without_delimiter_returns_original():
    text = "# 标题\n没有 frontmatter"
    fm, body = notes.parse_frontmatter(text)
    assert fm == {}
    assert body == text


def test_parse_frontmatter_unclosed_returns_original():
    """frontmatter 没闭合时不能把整篇当元信息吃掉，否则正文全部丢失。"""
    text = "---\ntitle: 坏掉的头部\n正文其实在这里"
    fm, body = notes.parse_frontmatter(text)
    assert fm == {}
    assert body == text


def test_extract_links_basic_alias_anchor():
    text = "参见 [[Raft]] 与 [[Paxos#Basic Paxos|Paxos 基础协议]]。"
    links = notes.extract_links(text)
    assert [l["target"] for l in links] == ["Raft", "Paxos"]
    assert links[1]["anchor"] == "Basic Paxos"
    assert links[1]["display"] == "Paxos 基础协议"


def test_extract_links_embed_kind():
    links = notes.extract_links("![[Raft#选主流程]]")
    assert len(links) == 1
    assert links[0]["kind"] == "embed"
    assert links[0]["target"] == "Raft"
    assert links[0]["anchor"] == "选主流程"


def test_extract_links_pure_anchor_skipped():
    """[[#本页锚点]] 指向本文，不产生跨笔记边。"""
    assert notes.extract_links("[[#本章小结]]") == []


def test_extract_links_table_escaped_pipe():
    text = "| [[A\\|别名A]] | 说明 |"
    links = notes.extract_links(text)
    assert links[0]["target"] == "A"
    assert links[0]["display"] == "别名A"


def test_extract_links_records_line_and_context():
    text = "第一行\n分布式共识的工程实现是 [[Raft]]。\n第三行"
    link = notes.extract_links(text)[0]
    assert link["line_no"] == 2
    assert "分布式共识的工程实现是" in link["context"]


def test_extract_links_dedup():
    """同一目标 + 同一锚点只保留一条，但不同锚点算不同链接。"""
    text = "[[Raft]] 与 [[Raft]]，以及 [[Raft#选举]]"
    links = notes.extract_links(text)
    assert len(links) == 2


def test_extract_tags_ignores_heading_and_code_fence():
    text = "# 这是一级标题\n正文提到 #分布式 与 #algorithms/raft。\n```\n#不应被识别\n```"
    tags = notes.extract_tags(text)
    assert "分布式" in tags
    assert "algorithms/raft" in tags
    assert "不应被识别" not in tags


def test_note_title_prefers_frontmatter_title():
    path = "/vault/文件名.md"
    assert notes.note_title(path, {"title": "显式标题"}) == "显式标题"
    assert notes.note_title(path, {}) == "文件名"


def test_note_aliases_multiple_keys():
    aliases = notes.note_aliases({"aliases": ["A"], "alias": "B", "别名": ["C"]})
    assert aliases == ["A", "B", "C"]


def test_sync_note_strips_frontmatter_from_links():
    """frontmatter 里的 [[...]] 不应被当成正文双链。"""
    text = "---\ntitle: T\nrelated: '[[Ghost]]'\n---\n正文没有链接"
    links = notes.extract_links(notes.parse_frontmatter(text)[1])
    assert links == []

def test_extract_mdlink_parses_relative_note():
    """标准 Markdown 相对链接也能被识别，不强制使用 Obsidian 语法。"""
    links = notes.extract_links("参见 [Raft 实现](笔记/Raft.md#选举) 与 [Paxos](Paxos.md)。")
    assert [l["target"] for l in links] == ["Raft.md", "Paxos.md"]
    assert links[0]["anchor"] == "选举"
    assert links[0]["kind"] == "mdlink"


def test_extract_mdlink_skips_external_images_and_anchors():
    text = "[官网](https://example.com/a.md) ![图](img/a.png) [本页](#小节) [邮件](mailto:a@b.com)"
    assert notes.extract_links(text) == []


def test_extract_links_merges_wikilink_and_mdlink():
    text = "[[Raft]] 与 [Raft](Raft.md) 指向同一篇笔记"
    links = notes.extract_links(text)
    # 两种写法各记一条（kind 不同），建边时按目标去重，不会产生重复边
    assert {l["kind"] for l in links} == {"wikilink", "mdlink"}
    assert {db_norm(l["target"]) for l in links} == {"raft"}


def db_norm(value):
    from app import db
    return db.note_norm(value)
