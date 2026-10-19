# 知识库问答系统（RAG + 知识图谱）

一个自托管的智能知识库问答系统：**向量检索（RAG）+ 知识图谱（GraphRAG）双引擎**，
支持多轮对话、流式回答、引用溯源、图谱可视化。

- 后端：FastAPI + SQLite（零外部数据库依赖）+ numpy + NetworkX
- 前端：原生 JS 单页应用 + ECharts（本地内置，离线可用）
- 模型层：统一 OpenAI 兼容协议，**免费模型优先**，支持线上免费 API / 本地 Ollama / 任意自定义端点

## ✨ 功能特性

| 功能 | 说明 |
|---|---|
| 多知识库 | 可创建多个自定义知识库，文档分库归档、分库提问，检索与图谱按库隔离 |
| 文档入库 | PDF / Word(docx) / Markdown / TXT / 图片(PNG/JPG)，自动解析、分块、向量化 |
| 扫描件 OCR | 扫描件 PDF 与图片走多模态模型识别文字，支持 PaddleOCR-VL、DeepSeek-OCR，以及本地 Ollama 的 glm-ocr / unlimited-ocr；文字型 PDF 自动跳过 OCR |
| 三种检索模式 | 向量检索 / 知识图谱检索 / 混合检索（RRF 融合排序） |
| 知识图谱 | LLM 自动抽取实体与关系三元组，ECharts 力导向图可视化 |
| 流式问答 | SSE 流式输出，句末 `[编号]` 引用标注，可展开查看原文 |
| 检索调试 | 单独查看检索命中内容，便于调参与排错 |
| 重排序 | 可选 rerank 模型，提升检索精度 |
| 多会话 | 会话历史持久化，随时回看 |
| Mock 模式 | 无 API Key 也能完整体验全流程 |

## 🚀 快速开始

### Windows

在 PowerShell 里执行（或直接在文件管理器里双击 `start.bat`）：

```bat
.\start.bat              :: 默认 8000 端口
.\start.bat --mock       :: 无 Key 体验：同时启动主服务 + Mock 模型服务
.\start.bat --help       :: 查看用法
```

脚本会自动定位 Python、检查依赖（首次运行会 `pip install -r backend/requirements.txt`）。

> **注意**：PowerShell / CMD 原生没有 `bash`，直接敲 `bash start.sh` 或 `start.sh` 会报
> "无法将 bash 项识别为 cmdlet"。请用 `start.bat`。若你装了 Git Bash，也可以
> `& "C:\Program Files\Git\bin\bash.exe" start.sh`。

### macOS / Linux

```bash
chmod +x start.sh        # 首次需要
./start.sh               # 默认 8000 端口
./start.sh --mock        # 无 Key 体验模式
```

### 打开界面

浏览器访问 **http://localhost:8000** 。

### 无 Key 体验（Mock 模式）

以 `--mock` 参数启动后（Windows: `.\start.bat --mock`，macOS/Linux: `./start.sh --mock`），**主服务会自动将 LLM / Embedding / Rerank 三项默认指向本地 Mock 服务**（`http://127.0.0.1:9999/v1`），打开网页即可直接使用，无需手动填写。

如需手动验证或自定义，也可在「模型设置」页把三项 Base URL 都填 `http://127.0.0.1:9999/v1`、API Key 随意（如 `mock`）。保存后到「文档库」上传 `demo_docs/` 里的示例文档即可体验。

> Mock 模式使用规则式伪抽取与词袋向量，仅用于跑通流程；真实效果请配置下列免费模型。

## 🆓 免费模型配置（推荐顺序）

### 1. 硅基流动 SiliconFlow（推荐，一套配齐）

注册 **https://cloud.siliconflow.cn** 创建API Key，且以下模型**长期免费**：

| 用途 | 模型 | 费用 |
|---|---|---|
| LLM | `THUDM/GLM-4-9B-0414`（默认）、`deepseek-ai/DeepSeek-R1-0528-Qwen3-8B`、`Qwen/Qwen3-8B` | 免费 |
| Embedding | `BAAI/bge-m3` | 免费 |
| Rerank | `BAAI/bge-reranker-v2-m3` | 免费 |
| OCR（可选） | `PaddlePaddle/PaddleOCR-VL-1.5`、`deepseek-ai/DeepSeek-OCR`（本地 Ollama 可用 `glm-ocr`） | 免费 |

> **一把硅基流动 Key 就够了**：LLM / Embedding / Rerank / OCR 全部走同一家，
> Key 填一次即可（其余节留空会自动复用同提供商的 Key）。
>
> 三个 LLM 的取舍：`THUDM/GLM-4-9B-0414` 是**非推理模型**，响应最快、最省 token，
> 适合默认使用；`DeepSeek-R1-0528-Qwen3-8B` 与 `Qwen/Qwen3-8B` 是**推理模型**，
> 会先输出一段思考链（实测简单问题也要 200～300 tokens），首字延迟明显更高。
> 图谱抽取这类要 JSON 输出的任务，思考链容易挤占 `max_tokens` 导致结果为空，
> 建议保持默认模型。

API Key 申请：https://cloud.siliconflow.cn/account/ak
在「模型设置」页选「硅基流动（免费额度，推荐）」预设 → 填入 Key → 保存 → 点「测试」。

### 2. Ollama 本地部署（完全免费 + 数据不出本机）

```bash
# 安装：https://ollama.com/download
ollama pull qwen3:8b            # LLM（约 5.2GB）
ollama pull bge-m3              # Embedding（约 1.2GB）
ollama pull glm-ocr             # OCR（约 2.7GB，扫描件识别，可选）
ollama serve                    # 默认端口 11434
```

「模型设置」页选「Ollama 本地」预设：
- Base URL：`http://localhost:11434/v1`
- LLM 模型：`qwen3:8b`；Embedding 模型：`bge-m3`；OCR 模型：`glm-ocr`（启用 OCR 时）
- API Key 留空即可

> Qwen3 是推理模型，默认会先输出思考链，首字较慢；想更快可改用列表里的 `qwen2.5:7b`。
> 本地 Rerank 可用 `ollama pull qwen2.5-reranker` 或直接关闭重排开关。

### 3. 自定义端点

任何 OpenAI 兼容服务（vLLM / LM Studio / One-API / 其它云厂商）都能接入：
填 Base URL + API Key + 模型名即可。

## 📖 使用流程

1. **模型设置**：选择提供商预设 → 填 Key → 保存 → 分别点「测试」确认三个绿灯
2. **文档库**：拖入或选择文件上传（支持多选）→ 等待状态变为「完成」
   - 默认自动构建知识图谱（可在设置页关闭以加速入库）
   - 可对单个文档重建图谱、重新解析、或删除
   - **扫描件 PDF / 图片**需先在「模型设置 → 文档 OCR」启用 OCR；未启用时会明确提示「没有文字层」。
     已上传但解析失败的文档点「重解析」即可重跑，不必删掉重传
3. **问答**：输入问题，流式获得带引用的回答；顶部可切换检索模式
4. **知识图谱**：查看全库实体关系图，支持拖拽、缩放、点击实体查看关系
5. **检索调试**：只看检索命中不看生成，用于验证知识库内容是否被正确召回

## 🔍 扫描件 OCR

扫描件 PDF 是「图片型 PDF」——页面里没有可提取的文字层，`pypdf` 抽出来是空的，
所以和对话模型无关：**不接 OCR，扫描件永远进不了知识库**。

开启方式：**模型设置 → 文档 OCR → 启用 OCR**，选提供商预设（云端 / Ollama）后会自动带出
Base URL 与模型名，点「测试」验证连通性。

**云端：硅基流动**（走 OpenAI 兼容的 `chat/completions` 多模态接口）

| 模型 | 特点 |
|---|---|
| `PaddlePaddle/PaddleOCR-VL-1.5` | 通用文字识别，中英混排、竖排、手写较稳，输出纯文本 |
| `deepseek-ai/DeepSeek-OCR` | 面向文档，按 grounding 转 Markdown，表格与版面结构保留更好。实测在硅基流动上输出不够稳定（偶发乱码/幻觉），**建议优先用 PaddleOCR-VL** |

**本地：Ollama**（预设选「Ollama 本地部署」，Base URL 自动填 `http://localhost:11434/v1`，Key 留空）

| 模型 | 体积 | 特点 |
|---|---|---|
| `glm-ocr` | 2.7 GB（0.9B） | 智谱官方文档解析模型，OmniDocBench V1.5 得分 94.62 排名第一，**首选**。另有 `glm-ocr:q8_0`（1.8 GB）省显存 |
| `richardyoung/unlimited-ocr` | 2.8 GB（3.3B） | 百度 Unlimited-OCR 的社区 GGUF，超长文档可整篇连续解析；输出带版面坐标，本项目会自动剥掉坐标只留文字 |
| `qwen2.5vl:7b` / `minicpm-v` | 6 GB / 5 GB | 通用视觉模型，版面复杂或特殊语种时兜底，不是专用 OCR 模型 |

```bash
ollama pull glm-ocr
# 或
ollama pull richardyoung/unlimited-ocr
```

> **提示词是硬约束**：GLM-OCR 只认 `Text Recognition:` 这类任务前缀，Unlimited-OCR 只认
> `Free OCR.`——提示词写错会直接返回空文本，不是模型坏了。本项目按模型名自动选用对应提示词
> （`providers._ocr_prompt`），无需手动修改。
>
> Ollama 的 OpenAI 兼容层对视觉输入支持不完整，`glm-ocr` 可能返回 502 或空 body。此时程序会
> 自动改用 Ollama 原生 `/api/generate` 重试一次（`providers._ocr_ollama_native`），无需干预。

解析策略（`backend/app/documents.py`）：

- **文字型 PDF**：只走文字层，零 OCR 开销，不会被送去识别。
- **扫描件 PDF**：整份判定无文字层 → 逐页渲染成图片 → 并发 OCR（3 路）。
- **混合型 PDF**：只对文字层过少的页做 OCR，其余页保留原文字。
- **图片文件**（PNG/JPG/WEBP/BMP/TIFF）：整张 OCR。
- 单页失败只跳过该页，不中断整份文档；全部失败才报错。
- 进度实时显示在文档状态列（`OCR 识别 3/10 页`）。

可调参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `dpi` | 150 | 页面渲染分辨率。字小/模糊可提到 200，越高越慢、请求越大 |
| `max_pages` | 50 | 单份 PDF 最多 OCR 页数，防止超长文档耗时过久、消耗过多额度 |
| `min_chars` | 50 | 单页去空白后少于该字符数即视为需要 OCR |

> 扫描件识别质量取决于原件清晰度与模型能力，OCR 会引入错字，重要信息建议人工核对。

## 🏗️ 架构说明

```
用户提问
   │
   ├─ 向量检索 ──┐
   │  (query嵌入 → cosine topK)          混合模式：RRF 融合
   ├─ 图谱检索 ──┤ → (可选 rerank) → 构建带编号上下文 → LLM 流式生成
   │  (LLM实体链接 → 子图扩展 → 关联chunk)
   │
   └─ 引用映射：回答中的 [n] ↔ 原文分块
```

- **分块策略**：按中文句子边界切分，目标 500 字、重叠 80 字
- **图谱构建**：逐 chunk 调 LLM 抽取 `(头实体, 关系, 尾实体)` 三元组，实体合并去重
- **图检索**：LLM 从问题提取关键词 → 双向子串匹配实体 → N 跳子图扩展 → 关联原文分块
- **向量存储**：SQLite 存向量 + 内存矩阵计算，万级分块内性能良好，无需外部向量库

## 📁 目录结构

```
rag-graph-qa/
├── start.sh                # 一键启动（macOS / Linux）
├── start.bat               # 一键启动（Windows）
├── mock_server.py          # Mock OpenAI 兼容服务（无 Key 体验）
├── backend/
│   ├── run.py              # 启动入口
│   ├── requirements.txt
│   ├── app/
│   │   ├── main.py         # FastAPI 路由
│   │   ├── db.py           # SQLite 存储层
│   │   ├── config.py       # 设置与提供商预设
│   │   ├── providers.py    # LLM/Embedding/Rerank 客户端
│   │   ├── documents.py    # 解析与分块管线
│   │   ├── vectorstore.py  # 向量检索
│   │   ├── graph.py        # 知识图谱构建与检索
│   │   └── rag.py          # RAG 编排与流式生成
│   └── data/               # 运行时生成：DB + 上传文件
├── frontend/
│   ├── index.html
│   └── static/             # app.js / style.css / echarts.min.js
└── demo_docs/              # 示例文档
```

## ❓ 常见问题

**Q: 上传后一直显示「处理中」？**
首见于模型服务不通。到「模型设置」点「测试」；另看后端日志中的错误详情。

**Q: 图谱检索为空？**
图谱依赖实体名匹配，问题中需包含文档里出现过的实体名；或切换到混合模式（向量兜底）。

**Q: 换了 Embedding 模型后检索不到旧文档？**
向量与模型绑定（embed_tag 标记）。重新上传文档，或删除后重新入库。

**Q: 如何重置全部数据？**
停止服务后删除 `backend/data/` 目录即可。

**Q: 终端显示 `http://0.0.0.0:8000` 打不开？**
`0.0.0.0` 是服务器绑定地址，浏览器请访问 **http://localhost:8000** 。默认仅监听本机（127.0.0.1）；如需局域网/远程访问，启动时加参数：`python backend/run.py --host 0.0.0.0`。

**Q: Windows 上执行 `bash start.sh` 报「无法将 bash 项识别为 cmdlet」？**
PowerShell / CMD 不带 bash。请改用 `.\start.bat`（等价功能），或安装 Git for Windows 后用
`& "C:\Program Files\Git\bin\bash.exe" start.sh`。

**Q: `start.bat` 提示 Python not found？**
Python 未加入 PATH。重新安装 Python 并勾选 "Add Python to PATH"，或改用 `py` 启动器，脚本会自动尝试。

**Q: 启动报 `[Errno 10048] error while attempting to bind on address` / 端口已被占用？**
不是代码问题，是 8000 端口上已有一个实例在跑（多半是上一次没关掉的窗口）。先用浏览器打开
http://127.0.0.1:8000 ，能打开就说明旧实例还在正常服务，直接用即可。要跑新实例，先结束旧进程：

```bat
netstat -ano | findstr :8000
taskkill /PID <上面查到的PID> /F
```

或者换个端口启动：`set PORT=8010 && start.bat`（macOS / Linux：`PORT=8010 ./start.sh`）。
`start.bat` / `start.sh` 现在会先检测端口，被占用时直接给出提示，不再抛异常堆栈。
