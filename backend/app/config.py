"""模型配置：免费优先的提供商预设 + 用户设置存取。"""
import os
from . import db

# 各预设默认模型（均为免费档或可免费使用）
PRESETS = {
    "siliconflow_free": {
        "label": "硅基流动（免费额度，推荐）",
        "base_url": "https://api.siliconflow.cn/v1",
        # 默认用非推理模型：回答生成与图谱抽取（JSON）都不需要思考链，
        # 推理模型会先产出一大段 reasoning，首字延迟高且更费 token。
        "llm_model": "THUDM/GLM-4-9B-0414",
        "llm_models": [
            "THUDM/GLM-4-9B-0414",                 # 非推理，最快最省，默认
            "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B",  # 推理模型，复杂问题更强，较慢
            "Qwen/Qwen3-8B",                       # 推理模型，中文能力好，较慢
        ],
        "embed_model": "BAAI/bge-m3",
        "embed_models": ["BAAI/bge-m3", "netease-youdao/bce-embedding-base_v1"],
        "rerank_model": "BAAI/bge-reranker-v2-m3",
        "rerank_models": ["BAAI/bge-reranker-v2-m3"],
        "ocr_model": "PaddlePaddle/PaddleOCR-VL-1.5",
        "ocr_models": ["PaddlePaddle/PaddleOCR-VL-1.5", "deepseek-ai/DeepSeek-OCR"],
        "note": "注册 siliconflow.cn 创建 API Key；上列 LLM 与 bge 系列长期免费",
        "key_url": "https://cloud.siliconflow.cn/account/ak",
    },
    "ollama_local": {
        "label": "Ollama 本地部署（完全免费离线）",
        "base_url": "http://localhost:11434/v1",
        "llm_model": "qwen3:8b",
        "llm_models": ["qwen3:8b", "qwen3:14b", "qwen2.5:7b", "llama3.1:8b", "glm4:9b"],
        "embed_model": "bge-m3",
        "embed_models": ["bge-m3", "nomic-embed-text", "bge-m3:567m", "quentinz/bge-large-zh-v1.5"],
        "rerank_model": "",
        "rerank_models": [],
        # 本地 OCR：专用文档解析模型优于通用视觉模型（后者会把版面「描述」成一句话）
        "ocr_model": "glm-ocr",
        "ocr_models": [
            "glm-ocr",                        # 智谱 0.9B，2.7GB，官方库，per-page 精度最好，推荐
            "glm-ocr:q8_0",                   # 同上 8bit 量化，1.8GB，省显存
            "richardyoung/unlimited-ocr",     # 百度 3.3B 社区 GGUF，2.8GB，长文档/带坐标
            "qwen2.5vl:7b",                   # 通用视觉，版面复杂时兜底，非专用 OCR
            "minicpm-v",                      # 轻量通用视觉，低配机器兜底
        ],
        "note": (
            "需先安装 Ollama 并拉取模型：ollama pull qwen3:8b && ollama pull bge-m3 && ollama pull glm-ocr"
            "（Qwen3 默认输出思考链，觉得慢可换 qwen2.5:7b）。"
            "OCR 用专用模型更准：glm-ocr 为智谱 0.9B 文档解析模型，"
            "richardyoung/unlimited-ocr 为百度 3.3B 长文档模型（输出带坐标，已自动清理）"
        ),
        "key_url": "https://ollama.com",
    },
    "custom": {
        "label": "自定义（任意 OpenAI 兼容 API）",
        "base_url": "",
        "llm_model": "",
        "llm_models": [],
        "embed_model": "",
        "embed_models": [],
        "rerank_model": "",
        "rerank_models": [],
        "ocr_model": "",
        "ocr_models": [],
        "note": "vLLM / Xinference / LM Studio / DeepSeek / 通义等，任何 OpenAI 兼容接口",
        "key_url": "",
    },
    "mock": {
        "label": "Mock 模型（无 Key 体验，--mock 自动启用）",
        "base_url": "http://127.0.0.1:9999/v1",
        "llm_model": "mock",
        "llm_models": ["mock"],
        "embed_model": "mock-embed",
        "embed_models": ["mock-embed"],
        "rerank_model": "mock-rerank",
        "rerank_models": ["mock-rerank"],
        "note": "本地规则式伪模型，仅用于跑通流程；真实效果请配置其他提供商",
        "key_url": "",
    },
}

# 已下线的提供商 → 迁移目标预设。
# 只填一处硅基流动 Key 即可覆盖 LLM/Embedding/Rerank/OCR，无需第二家服务商。
RETIRED_PROVIDERS = {"zhipu_free": "siliconflow_free"}

# 各配置节对应的「模型名」字段（迁移时按此取目标预设的默认模型）
_SECTION_MODEL_FIELD = {
    "llm": "llm_model",
    "embedding": "embed_model",
    "rerank": "rerank_model",
    "ocr": "ocr_model",
}


def _migrate_retired(cfg: dict) -> dict:
    """把已下线提供商的存量配置迁到目标预设。

    老用户库里可能还存着 provider="zhipu_free"，而该预设已从 PRESETS 移除。
    若不迁移，设置页该节的提供商下拉会选不中（显示空白），保存后模型名与
    base_url 会错配，调用直接失败。
    """
    for section, field in _SECTION_MODEL_FIELD.items():
        c = cfg.get(section)
        if not isinstance(c, dict):
            continue
        target = RETIRED_PROVIDERS.get(c.get("provider", ""))
        if not target:
            continue
        preset = PRESETS[target]
        c["provider"] = target
        c["base_url"] = preset["base_url"]
        c["model"] = preset[field]
        c["api_key"] = ""   # 原 Key 属于已下线服务商，不能用于新服务商
    return cfg


_KEY_SECTIONS = ("embedding", "llm", "rerank", "ocr")


def _reuse_provider_keys(cfg: dict) -> dict:
    """同一提供商只需填一次 Key：某节 Key 为空时，复用其他同提供商节的 Key。

    典型场景：LLM / Embedding / Rerank / OCR 全用硅基流动，用户只填了一处，
    其余留空即可正常工作，不必把同一把 Key 粘四遍。显式填写的 Key 不会被覆盖。
    """
    for section in _KEY_SECTIONS:
        c = cfg.get(section)
        if not isinstance(c, dict) or c.get("api_key"):
            continue
        prov = c.get("provider")
        if not prov:
            continue
        for other in _KEY_SECTIONS:
            o = cfg.get(other) or {}
            if other != section and o.get("provider") == prov and o.get("api_key"):
                c["api_key"] = o["api_key"]
                break
    return cfg


def default_settings() -> dict:
    """默认设置。当环境变量 RAG_MOCK=1（start.sh --mock）时，三项模型默认指向本地
    Mock 服务，免去在「模型设置」页手动填写三遍 Base URL。已保存过设置的用户不受影响。"""
    ds = {
        "llm": {
            "provider": "siliconflow_free",
            "base_url": PRESETS["siliconflow_free"]["base_url"],
            "api_key": "",
            "model": PRESETS["siliconflow_free"]["llm_model"],
            "temperature": 0.3,
        },
        "embedding": {
            "provider": "siliconflow_free",
            "base_url": PRESETS["siliconflow_free"]["base_url"],
            "api_key": "",
            "model": PRESETS["siliconflow_free"]["embed_model"],
        },
        "rerank": {
            "enabled": False,
            "provider": "siliconflow_free",
            "base_url": PRESETS["siliconflow_free"]["base_url"],
            "api_key": "",
            "model": PRESETS["siliconflow_free"]["rerank_model"],
        },
        "ocr": {
            "enabled": False,              # 扫描件 PDF / 图片的文字识别（多模态模型）
            "provider": "siliconflow_free",
            "base_url": PRESETS["siliconflow_free"]["base_url"],
            "api_key": "",
            "model": PRESETS["siliconflow_free"]["ocr_model"],
            "dpi": 150,                    # 页面渲染分辨率，越高越准也越慢
            "max_pages": 50,               # 单份 PDF 最多 OCR 页数，防止超长文档耗时过久
            "min_chars": 50,               # 单页文字少于该值即视为扫描页，需要 OCR
        },
        "graph": {
            "build_on_upload": True,
            "max_chunks_per_run": 500,
            "completion": False,           # 图谱补全：LLM 推理隐含关系（离线确认 + 在线补充）
        },
        "ontology": {
            "enabled": False,              # 启用自定义本体（约束抽取的实体/关系类型）
            "entity_types": "",            # 换行或逗号分隔，如：人物\n公司\n技术
            "relation_types": "",          # 换行或逗号分隔，如：任职于\n投资\n竞争
            "filter_search": False,        # 图谱检索时仅匹配上述类型
        },
        "retrieval": {
            "mode": "hybrid",     # vector / graph / hybrid
            "top_k": 6,
            "graph_hops": 2,
        },
    }
    if os.environ.get("RAG_MOCK") == "1":
        mk = PRESETS["mock"]
        ds["llm"] = {"provider": "mock", "base_url": mk["base_url"], "api_key": "mock",
                     "model": mk["llm_model"], "temperature": 0.3}
        ds["embedding"] = {"provider": "mock", "base_url": mk["base_url"], "api_key": "mock",
                           "model": mk["embed_model"]}
        ds["rerank"] = {"provider": "mock", "base_url": mk["base_url"], "api_key": "mock",
                        "model": mk["rerank_model"], "enabled": True}
    return ds

_env_cache = None


def _apply_env(cfg):
    """环境变量兜底：SILICONFLOW_API_KEY / OLLAMA_BASE_URL 等。"""
    env_map = {
        "siliconflow_free": os.environ.get("SILICONFLOW_API_KEY", ""),
        "deepseek": os.environ.get("DEEPSEEK_API_KEY", ""),
        "custom": os.environ.get("CUSTOM_API_KEY", ""),
    }
    for section in ("llm", "embedding", "rerank", "ocr"):
        c = cfg.get(section) or {}
        key = env_map.get(c.get("provider", ""), "")
        if key and not c.get("api_key"):
            c["api_key"] = key
    obu = os.environ.get("OLLAMA_BASE_URL", "")
    if obu:
        for section in ("llm", "embedding", "rerank", "ocr"):
            c = cfg.get(section) or {}
            if c.get("provider") == "ollama_local":
                c["base_url"] = obu.rstrip("/") + "/v1" if not obu.endswith("/v1") else obu
    return cfg


def _merge_defaults(saved: dict) -> dict:
    out = {}
    for k, v in default_settings().items():
        if isinstance(v, dict):
            out[k] = {**v, **(saved.get(k) or {})}
        else:
            out[k] = saved.get(k, v)
    return out


def get_settings() -> dict:
    saved = db.get_setting("settings", {}) or {}
    cfg = _migrate_retired(_merge_defaults(saved))
    return _reuse_provider_keys(_apply_env(cfg))


def save_settings(new_cfg: dict) -> dict:
    current = get_settings()
    # 合并基底用数据库里的原值，而不是默认值：只提交部分配置节时，
    # 未提交的节必须保持原样，否则会被悄悄重置成默认（如 OCR 配置被 LLM 设置冲掉）。
    stored = db.get_setting("settings", {}) or {}
    merged = {}
    for k, v in default_settings().items():
        if isinstance(v, dict):
            merged[k] = {**v, **(stored.get(k) or {}), **(new_cfg.get(k) or {})}
        else:
            merged[k] = new_cfg.get(k, stored.get(k, v))
    # 已下线提供商（如历史上的智谱）在保存时同样迁移，避免脏配置被写回
    merged = _reuse_provider_keys(_migrate_retired(merged))
    # api_key 打码处理：前端回传 "____" 前缀的占位值时保留原值
    for section in ("llm", "embedding", "rerank", "ocr"):
        old_key = (current.get(section) or {}).get("api_key", "")
        new_key = (merged.get(section) or {}).get("api_key", "")
        # 打码回传（含 ••）或清空时，保留原 key 不变
        if "••" in new_key or new_key.strip() == "":
            merged[section]["api_key"] = old_key
    db.set_setting("settings", merged)
    return merged


def mask_settings(cfg: dict) -> dict:
    """API key 打码后返回给前端。"""
    out = {}
    for k, v in cfg.items():
        if isinstance(v, dict):
            v = dict(v)
            if v.get("api_key"):
                key = v["api_key"]
                v["api_key"] = (key[:3] + "••" + key[-4:]) if len(key) > 8 else "••••"
            out[k] = v
        else:
            out[k] = v
    return out


def embed_tag(cfg: dict) -> str:
    e = cfg.get("embedding", {})
    return f"{e.get('provider','?')}::{e.get('model','?')}"
