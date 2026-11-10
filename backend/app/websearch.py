"""免费联网搜索与网页抓取（无需 API Key）。

- Web Search：中文问题优先 360 搜索（对中文关键词命中更稳），
  Bing RSS / DuckDuckGo HTML 作为兜底；自动识别并丢弃「只匹配首词」的退化结果。
- Web Fetch：直接抓取网页并抽取正文，支持 HTML / 纯文本 / PDF。

安全约束（服务可能被 --host 0.0.0.0 暴露到局域网）：
- 只允许 http/https、标准端口 80/443；
- 拒绝回环、内网、链路本地、保留地址（含 DNS 解析结果）；
- 手动跟随重定向并逐跳校验，最多 3 次；
- 限制响应体大小与超时，避免被大文件拖死。
"""
import asyncio
import html as html_lib
import ipaddress
import re
import socket
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from io import BytesIO
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import httpx

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
_TIMEOUT = httpx.Timeout(12.0, connect=8.0)
_MAX_BYTES = 2_000_000
_MAX_REDIRECTS = 3
_ALLOWED_TYPES = (
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "text/xml",
    "application/xml",
    "application/pdf",
)


class WebError(Exception):
    """联网搜索 / 抓取失败（对调用方是可降级的错误）。"""


# ---------------- URL 与网络校验 ----------------


def _url_ok(url: str) -> bool:
    try:
        p = urlparse(url)
        port = p.port  # 非法端口（如 :abc）会在这里抛 ValueError
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    if not p.hostname or p.username or p.password:
        return False
    if port and port not in (80, 443):
        return False
    return True


def _host_public_sync(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for info in infos:
        ip = str(info[4][0]).split("%", 1)[0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if not addr.is_global:  # 回环/内网/链路本地/保留/CGNAT 全部拒绝
            return False
    return True


async def _ensure_public(url: str):
    if not _url_ok(url):
        raise WebError("不支持的网页地址")
    host = urlparse(url).hostname or ""
    if not await asyncio.to_thread(_host_public_sync, host):
        raise WebError("拒绝访问内网或本机地址")


def _content_allowed(ctype: str, url: str) -> bool:
    ctype = (ctype or "").lower()
    if any(ctype.startswith(t) for t in _ALLOWED_TYPES):
        return True
    # 有些站点把 PDF 标成 octet-stream
    if ctype.startswith("application/octet-stream") and urlparse(url).path.lower().endswith(".pdf"):
        return True
    return False


async def _fetch_raw(url: str, timeout: float = None) -> tuple:
    """抓取 URL，返回 (字节, content-type, 最终 URL)；逐跳校验重定向目标。"""
    current = url
    timeout = timeout or 12.0
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=8.0),
        headers=_HEADERS,
        follow_redirects=False,
    ) as client:
        for _ in range(_MAX_REDIRECTS + 1):
            await _ensure_public(current)
            try:
                async with client.stream("GET", current) as r:
                    if r.status_code in (301, 302, 303, 307, 308):
                        loc = r.headers.get("location")
                        if not loc:
                            raise WebError(f"HTTP {r.status_code} 未提供跳转地址")
                        current = urljoin(current, loc)
                        continue
                    if r.status_code != 200:
                        raise WebError(f"HTTP {r.status_code}")
                    ctype = r.headers.get("content-type") or ""
                    if not _content_allowed(ctype, current):
                        raise WebError(f"不支持的内容类型：{ctype.split(';')[0] or '未知'}")
                    low = ctype.lower()
                    is_pdf = low.startswith("application/pdf") or (
                        low.startswith("application/octet-stream")
                        and urlparse(current).path.lower().endswith(".pdf")
                    )
                    limit = 8_000_000 if is_pdf else _MAX_BYTES
                    buf = bytearray()
                    async for chunk in r.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > limit:
                            if is_pdf:  # PDF 截断后无法解析，直接放弃
                                raise WebError("PDF 文件过大（超过 8MB）")
                            del buf[limit:]  # HTML 只取前 2MB，仍能抽取正文
                            break
                    return bytes(buf), ctype, current
            except httpx.HTTPError as e:
                raise WebError(f"网页请求失败：{e}") from e
    raise WebError("重定向次数过多")


def _decode(raw: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w\-]+)", content_type or "", re.I)
    encodings = [m.group(1)] if m else []
    encodings += ["utf-8-sig", "gb18030"]
    for enc in encodings:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="ignore")


# ---------------- HTML 正文抽取 ----------------


class _TextExtractor(HTMLParser):
    SKIP = {
        "script", "style", "noscript", "template", "svg", "canvas",
        "iframe", "object", "embed", "form", "nav", "footer", "header", "aside",
    }
    BLOCK = {
        "p", "div", "br", "hr", "li", "ul", "ol", "tr", "table", "section",
        "article", "main", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre",
        "dl", "dt", "dd",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.title_parts = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self.SKIP:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
            return
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag.lower() in ("br", "hr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.SKIP:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
            return
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._skip_depth or not data.strip():
            return
        self.parts.append(data)


def _html_to_text(raw: str) -> tuple:
    parser = _TextExtractor()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        pass
    text = "".join(parser.parts)
    text = re.sub(r"[\u00a0 \t\r\f\v]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    title = re.sub(r"\s+", " ", "".join(parser.title_parts)).strip()
    return title, text


def _clean(text: str, limit: int = 600) -> str:
    text = html_lib.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


# ---------------- Web Search ----------------


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# 词典/百科类站点：Bing 只匹配首个词时，返回的几乎都是这类单字/单词词条
_DICT_HOSTS = (
    "baike.baidu.com", "zdic.net", "hgcha.com", "hanzipi.com",
    "hanyuguoxue.com", "cidian", "zidian", "dict.",
)


def _cjk_count(text: str) -> int:
    return len(_CJK_RE.findall(text or ""))


def _best_overlap(title: str, query: str) -> int:
    """标题与查询的最长公共子串长度（忽略空白），用于识别「只搜了首词」的退化结果。"""
    q = re.sub(r"\s+", "", query or "")[:60]
    t = re.sub(r"\s+", "", title or "")
    if not q or not t:
        return 0
    best = 0
    for i in range(len(q)):
        for j in range(i + best + 1, len(q) + 1):
            if q[i:j] in t:
                best = j - i
            else:
                break
    return best


def _looks_degenerate(query: str, results: list) -> bool:
    """识别搜索引擎「只匹配首个词」的退化结果。

    Bing 的 RSS/HTML 接口对较长的中文问句会退化成只检索第一个词：例如搜
    「这篇文章是仿写哪篇文章」返回「这（汉语文字）_百度百科」，搜「人工智能
    最新进展」返回「人工（汉语词汇）_百度百科」。这类结果同时满足：
    标题与问题的最长公共片段 ≤2 字，且多数来自词典/百科站点。
    判定为无效后换下一个引擎，避免把词典页塞进回答上下文。
    """
    if _cjk_count(query) < 4 or not results:
        return False
    if max(_best_overlap(r.get("title") or "", query) for r in results) > 2:
        return False
    dict_hits = 0
    for r in results:
        host = (urlparse(r.get("url") or "").hostname or "").lower()
        if any(h in host for h in _DICT_HOSTS):
            dict_hits += 1
    return dict_hits >= max(2, len(results) * 0.6)


async def _so_search(query: str, limit: int) -> list:
    """360 搜索：中文关键词命中率明显好于 Bing 的免费接口，作为中文主力引擎。"""
    url = "https://www.so.com/s?q=" + quote_plus(query)
    raw, ctype, _ = await _fetch_raw(url, timeout=10.0)
    text = _decode(raw, ctype)
    out, seen = [], set()
    for block in re.findall(r'<li class="res-list".*?</li>', text, re.S):
        m = re.search(r'<h3[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        title = _clean(m.group(2), 200)
        if not title:
            continue
        # 360 的链接是跳转地址，data-mdurl 才是网页真实地址（引用与抓取都用它）
        mu = re.search(r'data-mdurl="([^"]+)"', block)
        link = html_lib.unescape(mu.group(1)) if mu else html_lib.unescape(m.group(1))
        if not _url_ok(link) or link in seen:
            continue
        seen.add(link)
        sm = re.search(r'<span class="res-list-summary"[^>]*>(.*?)</span>', block, re.S)
        if not sm:
            sm = re.search(r'<p class="res-desc"[^>]*>(.*?)</p>', block, re.S)
        out.append({
            "title": title,
            "url": link,
            "snippet": _clean(sm.group(1), 400) if sm else "",
        })
        if len(out) >= limit:
            break
    if not out:
        raise WebError("360 搜索无结果")
    return out


async def _bing_search(query: str, limit: int) -> list:
    urls = [
        "https://cn.bing.com/search?q=" + quote_plus(query) + "&format=rss",
        "https://www.bing.com/search?q=" + quote_plus(query) + "&format=rss&setlang=zh-CN",
    ]
    last = None
    for url in urls:
        try:
            raw, ctype, _ = await _fetch_raw(url, timeout=10.0)
            root = ET.fromstring(_decode(raw, ctype))
            out = []
            for item in root.findall(".//item"):
                title = _clean(item.findtext("title") or "", 200)
                link = (item.findtext("link") or "").strip()
                desc = _clean(item.findtext("description") or "", 400)
                if not title or not _url_ok(link):
                    continue
                if "bing.com" in (urlparse(link).hostname or ""):
                    continue
                out.append({"title": title, "url": link, "snippet": desc})
                if len(out) >= limit:
                    break
            if out:
                return out
        except (WebError, ET.ParseError) as e:
            last = e
    if last:
        raise WebError(f"Bing 搜索失败：{last}")
    return []


class _DDGParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results = []
        self._cur = None
        self._field = None
        self._buf = []

    def _finish(self):
        if self._cur and self._cur.get("url") and self._cur.get("title"):
            self.results.append(self._cur)
        self._cur = None
        self._field = None
        self._buf = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "a" and "result__a" in cls:
            self._finish()
            self._cur = {"url": a.get("href") or "", "title": "", "snippet": ""}
            self._field, self._buf = "title", []
        elif "result__snippet" in cls:
            self._field, self._buf = "snippet", []

    def handle_data(self, data):
        if self._field:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if not self._field:
            return
        text = _clean("".join(self._buf), 400)
        if self._cur is not None:
            self._cur[self._field] = text
        self._field, self._buf = None, []


def _ddg_real_url(href: str) -> str:
    if href.startswith("//"):
        href = "https:" + href
    p = urlparse(href)
    if p.hostname and "duckduckgo.com" in p.hostname and p.path.startswith("/l/"):
        room = parse_qs(p.query).get("uddg")
        if room:
            return unquote(room[0])
    return href


async def _ddg_search(query: str, limit: int) -> list:
    url = "https://html.duckduckgo.com/html/?q=" + quote_plus(query)
    raw, ctype, _ = await _fetch_raw(url, timeout=8.0)
    parser = _DDGParser()
    parser.feed(_decode(raw, ctype))
    parser.close()
    parser._finish()
    out = []
    for item in parser.results:
        link = _ddg_real_url(item["url"])
        if not _url_ok(link) or "duckduckgo.com" in (urlparse(link).hostname or ""):
            continue
        out.append({"title": item["title"], "url": link, "snippet": item["snippet"]})
        if len(out) >= limit:
            break
    return out


async def search(query: str, limit: int = 5) -> list:
    """免费 Web Search：中文优先 360，Bing RSS / DuckDuckGo 兜底。

    每个引擎的结果都会做一次相关性校验：标题与查询几乎没有公共内容
    （典型是 Bing 只搜了首个词返回词典页）时视为无效，继续换下一个引擎。
    """
    query = (query or "").strip()
    if not query:
        return []
    engines = (
        (("so360", _so_search), ("bing", _bing_search), ("duckduckgo", _ddg_search))
        if _cjk_count(query) >= 2
        else (("bing", _bing_search), ("duckduckgo", _ddg_search), ("so360", _so_search))
    )
    errors = []
    for name, func in engines:
        try:
            results = await func(query, limit)
        except Exception as e:
            errors.append(f"{name}: {e}")
            continue
        if results and not _looks_degenerate(query, results):
            return results[:limit]
        if results:
            errors.append(f"{name}: 结果与查询不相关（疑似只匹配首个词）")
    raise WebError("；".join(errors) or "没有找到搜索结果")


# ---------------- Web Fetch ----------------


def _pdf_text(raw: bytes, max_chars: int) -> str:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(raw))
    parts = []
    for page in reader.pages[:10]:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            parts.append("")
        if sum(len(p) for p in parts) >= max_chars:
            break
    return "\n".join(parts)


async def fetch_page(url: str, max_chars: int = 1500) -> dict:
    """抓取网页并抽取正文；PDF 用 pypdf 解析。"""
    raw, ctype, final_url = await _fetch_raw(url)
    low = (ctype or "").lower()
    if low.startswith("application/pdf") or (
        low.startswith("application/octet-stream") and urlparse(final_url).path.lower().endswith(".pdf")
    ):
        text = await asyncio.to_thread(_pdf_text, raw, max_chars)
        title = urlparse(final_url).path.rsplit("/", 1)[-1] or final_url
    else:
        decoded = _decode(raw, ctype)
        if low.startswith("text/plain"):
            title, text = urlparse(final_url).hostname or final_url, decoded
        else:
            title, text = _html_to_text(decoded)
        title = title or urlparse(final_url).hostname or final_url
    text = re.sub(r"\s+", " ", text or "").strip()
    return {
        "url": final_url,
        "title": _clean(title, 200),
        "text": text[:max_chars],
    }


async def search_and_fetch(query: str, limit: int = 5, pages: int = 3,
                           max_chars: int = 1500) -> dict:
    """搜索 + 并发抓取前 `pages` 个网页，返回可直接拼接进 RAG 上下文的 chunk。"""
    results = await search(query, limit=limit)
    if not results:
        return {"chunks": [], "results": [], "error": ""}

    pages = max(0, min(int(pages or 0), len(results)))
    fetched = {}
    if pages:
        sem = asyncio.Semaphore(3)

        async def one(item):
            async with sem:
                try:
                    page = await fetch_page(item["url"], max_chars=max_chars)
                    return item["url"], page
                except Exception:
                    return item["url"], None

        for url, page in await asyncio.gather(*(one(r) for r in results[:pages])):
            if page:
                fetched[url] = page

    chunks = []
    for idx, item in enumerate(results):
        page = fetched.get(item["url"])
        title = (page or {}).get("title") or item["title"]
        page_text = ((page or {}).get("text") or "").strip()
        # JS 渲染站点抓不到正文时，搜索摘要往往比几行导航文字更有用
        text = page_text if len(page_text) >= 40 else (item["snippet"] or page_text)
        if not title and not text:
            continue
        # 抓取成功后用最终地址（搜索结果的跳转链接会被替换成真实网页地址）
        link = (page or {}).get("url") or item["url"]
        chunks.append({
            "chunk_id": None,
            "doc_id": None,
            "text": f"{title}\n{text}",
            "score": round(0.9 - idx * 0.1, 3),
            "source": "web",
            "url": link,
            "title": title,
        })
    return {"chunks": chunks, "results": results, "error": ""}
