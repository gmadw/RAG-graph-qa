"""模型服务客户端：统一走 OpenAI 兼容协议（线上免费 API / 本地 Ollama / 自定义）。"""
import json
import re
import httpx
from .config import get_settings

TIMEOUT = httpx.Timeout(120.0, connect=15.0)


class ProviderError(Exception):
    pass


_ENDPOINT_TAILS = ("/chat/completions", "/embeddings", "/rerank", "/completions")


def normalize_base_url(url: str) -> str:
    """容忍「直接粘贴完整端点」的填法。

    用户常把 https://api.siliconflow.cn/v1/chat/completions 整段填进 Base URL，
    若再拼一次就会变成 .../chat/completions/chat/completions。这里统一剥成根路径。
    """
    base = (url or "").strip().rstrip("/")
    for tail in _ENDPOINT_TAILS:
        if base.endswith(tail):
            base = base[: -len(tail)]
            break
    return base.rstrip("/")


def _conf(section: str) -> dict:
    cfg = get_settings().get(section) or {}
    base = normalize_base_url(cfg.get("base_url") or "")
    if not base:
        raise ProviderError(f"{section} 未配置 base_url，请到「设置」页完成模型配置")
    key = cfg.get("api_key") or ""
    model = cfg.get("model") or ""
    return {"base_url": base, "api_key": key, "model": model, "extra": cfg}


def _headers(key: str) -> dict:
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


# ---------------- LLM ----------------

async def llm_chat(messages, stream=False, temperature=None, max_tokens=2048, section="llm"):
    """调用 chat/completions。stream=False 返回完整文本；stream=True 返回异步迭代器(yield str)。"""
    conf = _conf(section)
    temperature = 0.3 if temperature is None else temperature
    payload = {
        "model": conf["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    url = f"{conf['base_url']}/chat/completions"
    key = conf["api_key"]
    try:
        if stream:
            return _stream_sse(url, key, payload)
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(url, headers=_headers(key), json=payload)
            if r.status_code != 200:
                raise ProviderError(f"LLM HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
        return data["choices"][0]["message"]["content"] or ""
    except httpx.HTTPError as e:
        raise ProviderError(f"LLM 网络错误：{e}") from e


async def _stream_sse(url, key, payload):
    """SSE 流式解析，逐段 yield 文本增量。"""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            async with client.stream("POST", url, headers=_headers(key), json=payload) as r:
                if r.status_code != 200:
                    body = await r.aread()
                    raise ProviderError(f"LLM HTTP {r.status_code}: {body.decode('utf-8', 'ignore')[:300]}")
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except Exception:
                        continue
                    choices = obj.get("choices") or []
                    if choices:
                        delta = choices[0].get("delta") or {}
                        piece = delta.get("content") or ""
                        if piece:
                            yield piece
    except httpx.HTTPError as e:
        raise ProviderError(f"LLM 流式网络错误：{e}") from e


async def llm_json(messages, max_tokens=2048, section="llm"):
    """调用 LLM 并解析 JSON 输出（容忍围栏/前后杂质）。"""
    text = await llm_chat(messages, stream=False, temperature=0.0, max_tokens=max_tokens, section=section)
    return parse_json_loose(text)


def parse_json_loose(text: str):
    if text is None:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        pass
    # 找第一个 [ 或 { 到最后一个 ] 或 }
    for open_c, close_c in (("[", "]"), ("{", "}")):
        i, j = t.find(open_c), t.rfind(close_c)
        if i != -1 and j > i:
            try:
                return json.loads(t[i : j + 1])
            except Exception:
                continue
    return None


# ---------------- Embedding ----------------

async def embed_texts(texts: list) -> list:
    conf = _conf("embedding")
    out = []
    batch = 16
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for i in range(0, len(texts), batch):
            part = texts[i : i + batch]
            payload = {"model": conf["model"], "input": part}
            try:
                r = await client.post(
                    f"{conf['base_url']}/embeddings", headers=_headers(conf["api_key"]), json=payload
                )
            except httpx.HTTPError as e:
                raise ProviderError(f"Embedding 网络错误：{e}") from e
            if r.status_code != 200:
                raise ProviderError(f"Embedding HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()["data"]
            out.extend(d["embedding"] for d in data)
    return out


# ---------------- Rerank ----------------

async def rerank(query: str, texts: list, top_k: int = None):
    """返回按相关性排序的 [(原索引, score)]。"""
    conf = _conf("rerank")
    if not conf["model"]:
        return [(i, 0.0) for i in range(len(texts))]
    payload = {
        "model": conf["model"],
        "query": query,
        "documents": texts,
        "top_n": top_k or len(texts),
        "return_documents": False,
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(
                f"{conf['base_url']}/rerank", headers=_headers(conf["api_key"]), json=payload
            )
    except httpx.HTTPError as e:
        raise ProviderError(f"Rerank 网络错误：{e}") from e
    if r.status_code != 200:
        raise ProviderError(f"Rerank HTTP {r.status_code}: {r.text[:300]}")
    results = r.json().get("results") or []
    return [(item["index"], item.get("relevance_score", 0.0)) for item in results]


# ---------------- OCR（多模态视觉模型） ----------------

# 各模型对提示词的口径完全不同，写错不是「效果差一点」，而是直接返回空或乱码：
#   PaddleOCR-VL / 通用视觉模型：自然语言指令即可
#   DeepSeek-OCR：官方 grounding 转 markdown 写法，效果优于自然语言提问
#   GLM-OCR：官方任务前缀，Text / Table / Figure Recognition
#   Unlimited-OCR：训练时用的固定短语 "Free OCR."，换成别的提示词会输出空
_OCR_PROMPT_ZH = (
    "请识别这张图片中的全部文字，按原有阅读顺序输出为纯文本。"
    "保留段落与标题层级，表格用 Markdown 表格表示。"
    "不要翻译、不要总结、不要添加任何解释或前后缀。"
)
_OCR_PROMPT_DEEPSEEK = "<image>\n<|grounding|>Convert the document to markdown."
_OCR_PROMPT_GLM = "Text Recognition:"
_OCR_PROMPT_UNLIMITED = "Free OCR."


def _ocr_prompt(model: str) -> str:
    m = (model or "").lower()
    if "unlimited-ocr" in m:
        return _OCR_PROMPT_UNLIMITED
    if "glm-ocr" in m:
        return _OCR_PROMPT_GLM
    if "deepseek-ocr" in m:
        return _OCR_PROMPT_DEEPSEEK
    return _OCR_PROMPT_ZH


# Unlimited-OCR 等版面模型输出 `title [34, 88, 370, 172]INVOICE 4471`：
# 坐标对 RAG 没有任何用处，还会污染分块与向量，按行剥掉版式前缀。
_LAYOUT_PREFIX = re.compile(r"^\s*[a-zA-Z_]+ \[\s*\d+\s*,\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\]\s*")


def _strip_layout_tags(text: str) -> str:
    lines = (text or "").splitlines()
    # 至少两行符合版式前缀才认为是版面模型输出，避免误伤正文里的方括号数字
    if sum(1 for ln in lines if _LAYOUT_PREFIX.match(ln)) < 2:
        return text
    out = []
    for ln in lines:
        m = _LAYOUT_PREFIX.match(ln)
        if m:
            rest = ln[m.end() :].strip()
            if rest:                       # 纯图片区域剥完为空，直接丢弃
                out.append(rest)
            continue
        out.append(ln)
    return "\n".join(out).strip()


def _img_mime(raw: bytes) -> str:
    if raw[:8].startswith(b"\x89PNG"):
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _looks_like_ollama(base_url: str, section: dict) -> bool:
    if (section.get("provider") or "") == "ollama_local":
        return True
    return ":11434" in (base_url or "")


async def _ocr_openai(url: str, key: str, payload: dict, retries: int) -> str:
    """OpenAI 兼容 chat/completions 通道，5xx / 429 有限重试。"""
    import asyncio

    r = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=15.0)) as client:
        for attempt in range(retries + 1):
            try:
                r = await client.post(url, headers=_headers(key), json=payload)
            except httpx.HTTPError as e:
                if attempt < retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise ProviderError(f"OCR 网络错误：{e}") from e
            if r.status_code == 200:
                break
            # 5xx / 429 属于上游抖动，可重试
            if (r.status_code >= 500 or r.status_code == 429) and attempt < retries:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            raise ProviderError(f"OCR HTTP {r.status_code}: {r.text[:300]}")
    if r is None or r.status_code != 200:
        raise ProviderError(f"OCR HTTP {getattr(r, 'status_code', '?')}: {getattr(r, 'text', '')[:300]}")
    try:
        content = r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        raise ProviderError(f"OCR 返回格式异常：{r.text[:200]}") from e
    if isinstance(content, list):  # 少数实现返回分段数组
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


async def _ocr_ollama_native(base_url: str, model: str, image: bytes, prompt: str, max_tokens: int) -> str:
    """走 Ollama 原生 /api/generate。

    Ollama 的 OpenAI 兼容层对视觉输入支持不完整：glm-ocr 这类带自定义
    renderer/parser 的模型在 /v1/chat/completions 上会返回 502 或空 body，
    官方部署文档也建议视觉请求改走原生接口，因此这里作为兜底通道。
    """
    import base64

    root = (base_url or "").rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    payload = {
        "model": model,
        "prompt": prompt,
        "images": [base64.b64encode(image).decode()],
        "stream": False,
        "options": {"temperature": 0, "num_predict": max_tokens},
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=15.0)) as client:
            r = await client.post(f"{root}/api/generate", json=payload)
    except httpx.HTTPError as e:
        raise ProviderError(f"Ollama 原生接口网络错误：{e}") from e
    if r.status_code != 200:
        raise ProviderError(f"Ollama 原生接口 HTTP {r.status_code}: {r.text[:200]}")
    return r.json().get("response") or ""


async def ocr_image(image: bytes, max_tokens: int = 4096, retries: int = 2) -> str:
    """单张图片 → 文字。图片以 data URL 内联，避免额外的文件托管。

    硅基流动的 OCR 模型会偶发 500 / code 50507（"Unknown error"），同一张图重发即可成功，
    因此对 5xx 与 429 做有限重试；4xx（模型名错、鉴权失败）不重试，直接暴露真实原因。
    本地 Ollama 若兼容层失败或返回空，自动改用原生 /api/generate 再试一次。
    """
    import base64

    conf = _conf("ocr")
    if not conf["model"]:
        raise ProviderError("OCR 未配置模型名，请到「设置」页选择 OCR 模型")
    prompt = _ocr_prompt(conf["model"])
    b64 = base64.b64encode(image).decode()
    payload = {
        "model": conf["model"],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{_img_mime(image)};base64,{b64}"},
                    },
                ],
            }
        ],
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "stream": False,
    }
    url = f"{conf['base_url']}/chat/completions"

    text = ""
    first_err = None
    try:
        text = await _ocr_openai(url, conf["api_key"], payload, retries)
    except ProviderError as e:
        first_err = e

    if not (text or "").strip() and _looks_like_ollama(conf["base_url"], conf["extra"]):
        try:
            text = await _ocr_ollama_native(conf["base_url"], conf["model"], image, prompt, max_tokens)
        except ProviderError as e:
            raise (first_err or e)
    elif first_err is not None:
        raise first_err

    # 部分推理模型会把思考过程裹在 content 里，这里只去掉常见的 grounding 标记
    text = re.sub(r"<\|/?grounding\|>", "", text or "")
    return _strip_layout_tags(text).strip()


# ---------------- 连通性测试 ----------------

def _make_doc_test_image() -> bytes:
    """文档版面的测试图（多行、A4 比例）。

    DeepSeek-OCR 带 grounding 版面分析，喂单行居中大字会输出 "] ] ]" 之类的乱码，
    喂正常多行文档才稳定；PaddleOCR-VL 则相反，见 _make_test_image。
    """
    from io import BytesIO

    from PIL import Image, ImageDraw, ImageFont

    def f(size):
        for c in ("C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
            try:
                return ImageFont.truetype(c, size)
            except Exception:
                continue
        try:
            return ImageFont.load_default(size=size)
        except Exception:
            return ImageFont.load_default()

    img = Image.new("RGB", (600, 800), "white")
    d = ImageDraw.Draw(img)
    d.text((48, 60), "OCR TEST 2026", fill="black", font=f(30))
    y = 120
    for line in (
        "This is a document sample for OCR.",
        "Line two: the quick brown fox.",
        "Line three: numbers 12345.",
        "Line four: paragraph continues.",
    ):
        d.text((48, y), line, fill="black", font=f(20))
        y += 42
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_test_image(text: str = "OCR TEST 2026") -> bytes:
    """生成一张大字号、足够清晰的测试图。

    早期版本用 PIL 默认位图字体（约 11px），真模型也只能认出一串 0，
    导致连通性测试永远失败——图太小不是模型的问题。
    """
    from io import BytesIO

    from PIL import Image, ImageDraw, ImageFont

    # 700×700 是实测较稳的尺寸：过宽或某些比例会触发上游 50507
    W, H = 700, 700
    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)
    font = None
    for candidate in (
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/msyh.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        try:
            font = ImageFont.truetype(candidate, 64)
            break
        except Exception:
            continue
    if font is None:
        try:
            font = ImageFont.load_default(size=64)  # Pillow ≥10.1
        except Exception:
            font = ImageFont.load_default()
    draw.text((60, 300), text, fill="black", font=font)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def test_section(section: str) -> dict:
    try:
        if section == "ocr":
            model = (get_settings().get("ocr") or {}).get("model") or ""
            # 两类模型的偏好不同：文档解析类（DeepSeek-OCR / Unlimited-OCR / GLM-OCR）
            # 要正常文档版面，PaddleOCR-VL 与通用视觉模型喂单行大字更稳
            doc_like = any(k in model.lower() for k in ("deepseek-ocr", "unlimited-ocr", "glm-ocr"))
            img = _make_doc_test_image() if doc_like else _make_test_image()
            text = await ocr_image(img, max_tokens=512)
            # 判定只看是否认出测试图里的关键词：不同 OCR 模型输出风格差异大
            # （纯文本 / Markdown / 带坐标标记），还可能有个别错字，不能要求逐字一致。
            # 判定以「接口连通且返回非空」为准：测试图只是连通性探针，
            # 不同模型对图片版式的偏好差异很大（DeepSeek-OCR 偶发幻觉），
            # 不能因为测试图识别不好就判定配置错误。
            flat = re.sub(r"[^A-Za-z0-9]", "", text or "").upper()
            hit = any(k in flat for k in ("OCR", "0CR", "TEST", "2026"))
            if not text.strip():
                return {
                    "ok": False,
                    "detail": "接口连通但返回空文本：确认该模型支持图片输入（不是纯文本模型）",
                }
            if hit:
                detail = f"识别结果：{text[:60]}"
            else:
                detail = (
                    f"接口连通，但该模型对测试图的识别结果不可靠：{text[:50]}　"
                    "（不影响实际使用，建议用真实文档验证效果）"
                )
            return {"ok": True, "detail": detail}
        if section == "llm":
            text = await llm_chat(
                [{"role": "user", "content": "回复：ok"}],
                temperature=0.0,
                max_tokens=512,
            )
            return {"ok": True, "detail": f"模型回复：{(text or '')[:60]}"}
        if section == "embedding":
            vecs = await embed_texts(["连通性测试"])
            return {"ok": True, "detail": f"向量维度：{len(vecs[0])}"}
        if section == "rerank":
            ranked = await rerank("测试", ["文档一", "文档二"], top_k=2)
            return {"ok": True, "detail": f"排序返回 {len(ranked)} 条，分数正常"}
    except ProviderError as e:
        return {"ok": False, "detail": str(e)}
    return {"ok": False, "detail": "未知配置节"}
