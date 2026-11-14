# 与 Cognee 的功能差距分析 & 轻部署演进方案

> 对照对象：Cognee（AI memory engine，ECL pipeline：`add → cognify → search`）
> 本项目：RAG-graph-qa（FastAPI + SQLite + numpy + NetworkX，单进程、零外部服务）
> 结论先行：**能在不增加部署组件的前提下，把「图理解、笔记双链、检索策略」三块能力拉到接近 Cognee 的水平**；
> 剩余差距集中在多租户/权限、存储可插拔、任务编排与评测体系，属于「要不要上生产」的问题，而不是「能不能用」的问题。

---

## 一、现状盘点

| 层 | 本项目现状 | 代码位置 |
|---|---|---|
| 数据接入 | PDF/Word/Markdown/TXT/图片，扫描件 OCR（PaddleOCR-VL / GLM-OCR / DeepSeek-OCR） | `documents.py`、`providers.py` |
| 存储 | SQLite 单文件（文档/分块/向量/实体/关系/会话/反馈/笔记双链） | `db.py` |
| 向量检索 | BLOB 存 float32，内存矩阵 + cosine，按 `embed_tag` 隔离模型 | `vectorstore.py` |
| 图谱构建 | 逐 chunk LLM 抽三元组、别名消解、实体描述、共同邻居补全 | `graph.py` |
| 图谱检索 | LLM 实体链接 → 2 跳子图 → 关联 chunk；图谱洞察（度中心性/社群/桥接） | `graph.py` |
| 编排 | 向量/图谱/混合（RRF）/洞察/纯模型/联网 6 种模式，SSE 流式 + 引用 | `rag.py`、`main.py` |
| 反馈 | 引用块赞/踩累计，检索时加权 | `db.py`、`rag.py` |
| 部署 | `start.sh` / `start.bat` + `pip install`，`--mock` 无 Key 跑通全流程 | `start.sh`、`mock_server.py` |

---

## 二、与 Cognee 的能力差距

评级：✅ 已具备　🟡 部分具备　❌ 缺失　⚡ 本项目更优

| # | 能力维度 | Cognee | 本项目（改造前） | 差距判断 |
|---|---|---|---|---|
| 1 | **图 → 自然语言** | `GRAPH_COMPLETION`：把子图叙述化后交给模型，支持多跳推理 | 只把 `A --关系--> B` 平铺进 prompt | ❌ 关键差距（本次已补） |
| 2 | **笔记/文档结构** | 无原生 Obsidian 概念，靠 `add()` 文件 | 只做文本分块，双链是正文符号 | ❌ 关键差距（本次已补） |
| 3 | **检索策略谱系** | `GRAPH_COMPLETION`/`RAG_COMPLETION`/`SUMMARIES`/`CHUNKS`/`INSIGHTS`/`TEMPORAL`/`GRAPH_SUMMARY`/`FEELING_LUCKY` 等 | 6 种模式，缺少「图解读」「摘要」「时间」 | 🟡 |
| 4 | **图质量治理** | 实体消解、摘要节点、图补全、可自定义 graph model | 别名 LLM 合并、实体描述、共同邻居补全 | 🟡 接近 |
| 5 | **本体约束** | RDF/OWL 级 ontology，可校验 | 提示词层面的类型枚举 + 可选检索过滤 | 🟡 够用但无校验 |
| 6 | **增量/幂等** | pipeline 状态化、可断点重跑 | chunk 级增量已做；后台任务单锁、重启丢失 | 🟡 |
| 7 | **存储可插拔** | 向量：LanceDB/Qdrant/Chroma/PGVector/Weaviate；图：Kuzu/Neo4j/FalkorDB/NetworkX | 硬绑 SQLite + numpy + NetworkX | ❌ 但也是**部署优势** |
| 8 | **多租户/权限** | User / tenant / dataset 权限模型 | 仅知识库逻辑隔离，无用户与鉴权 | ❌ 生产化差距 |
| 9 | **记忆生命周期** | `memify`（记忆富化）、遗忘/过期 | 反馈加权；无摘要层、无衰减、无遗忘 | ❌ |
| 10 | **接口形态** | Python SDK / CLI / REST / MCP server | REST + Web UI | 🟡 缺 SDK 与 MCP |
| 11 | **多模态** | 文本 + 图片 + 音频转录 | 文本 + 扫描件 OCR | 🟡 缺音频 |
| 12 | **可观测/评测** | 日志、trace、评测示例 | 文档 stage/用量统计，无检索评测 | ❌ |
| 13 | **部署成本** | 默认可全本地，但常规要拉起 LanceDB + Kuzu + 关系库 | 一个 Python 进程 + 一个 `.db` 文件 | ⚡ 本项目明显更轻 |

**一句话总结**：Cognee 强在「图的表达能力 + 可插拔存储 + 平台化能力」，本项目强在「零依赖、开箱即用」。
把差距拆开后，真正影响「回答是否聪明」的只有 #1、#2、#3、#4、#9；其余是工程平台化问题。

---

## 三、本次已落地的能力（不改部署方式）

### 1. 让 AI 真正「读懂」知识图谱 —— `backend/app/graph_context.py`

不是再塞一遍三元组，而是把子图翻译成模型能顺着推理的结构：

- **实体档案**：名称、类型、度数、LLM 生成的描述、别名
- **多跳关系路径**：`张三 --任职于--> 阿里巴巴 --总部位于--> 杭州`（有向 DFS，优先走高度数主干）
- **概念社群**：无向化后贪心模块度聚类，输出核心实体与成员
- **笔记网络摘要**：连接度最高的笔记 + 未创建的链接目标（知识缺口）
- **引用溯源**：路径上的边回连原始 chunk，回答可以带 `[n]`

新增模式 `graph_read`（≈ Cognee `GRAPH_COMPLETION`）：向量兜底 + 子图叙述化，两者 RRF 融合。
`hybrid`/`graph` 模式也复用 `render_subgraph_brief()`，用已有的边做叙述化，**不额外增加 LLM 调用**。

### 2. Obsidian 双链与反链接 —— `backend/app/notes.py`

**不依赖 Obsidian 客户端**：只要 Markdown 正文里有链接语法，任意编辑器（Obsidian / Logseq / Foam / Typora / VS Code）导出的文件都能解析。
支持两种写法，可在同一篇里混用：

| 写法 | kind | 说明 |
|---|---|---|
| `[[笔记]]` | wikilink | Obsidian / Logseq 原生双链 |
| `[[笔记\|展示]]` | wikilink | 带展示文本 |
| `[[笔记#锚点]]` | wikilink | 指向小节 |
| `![[笔记]]` | embed | 嵌入（图上关系为「嵌入」） |
| `[文本](笔记.md)` | mdlink | 标准 Markdown 相对链接（`../` 与 `#锚点` 均可） |
| `#标签` / frontmatter | — | 标签与 title/aliases/tags 元信息 |

- 外部 URL、图片、页内锚点（`[x](#小节)`）不会误建边
- 双链落成图谱真实边（`kind='wikilink'`），并回连原文 chunk
- **上传顺序无关**：A 先入库、B 后入库，`resolve_note_links` 在每次入库后补齐，无需手动干预
- **存量补解析**：升级前入库的 `.md` 没有 `note_title`，`backfill_notes()` 从 `uploads/` 读原文件重新解析（不动分块与向量），由「笔记双链」按钮触发
- **反链接 / 出链**：`GET /api/notes/{id}/backlinks`，问答时注入「谁在什么语境下引用了这条笔记」
- **幂等**：每次重建先清旧双链边，重复上传/重解析不会让边叠加
- 笔记标题/别名直接映射成 `类型=笔记` 的实体，与 LLM 抽出的实体共用同一张图 —— 这是「知识结构更紧密」的关键

### 3. 引用与上下文编排 —— `rag.py`

上下文顺序固定为：**图谱解读 → 带编号原文 → 三元组 → 笔记反链接**。
图谱解读单独占预算（≤1800 字），不挤占原文引用；系统提示词要求区分「原文直述」与「多跳推出」。

### 4. 新增接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/notes?kb_id=` | 笔记列表（入链/出链/标签）+ 双链统计 + 知识缺口 |
| GET | `/api/notes/{doc_id}/backlinks` | 单篇反链接/出链 |
| POST | `/api/notes/relink` | 手动重建双链（批量导入后用） |
| GET | `/api/graph/read?q=&kb_id=` | 查看「AI 实际读到的图」 |
| GET | `/api/graph/stats` | 图谱规模 + 笔记/双链规模 |

前端：新增「图谱解读」检索模式、图谱页「图解读 / 重建双链」、引用面板折叠展示图解读与笔记网络。

---

## 四、更易部署的演进路线

原则：**默认单进程 + 单数据目录；一切增强都是「可选开关」，不引入必须常驻的服务。**

### P0 —— 已完成（本次）

- 图叙述化 + 多跳路径 + 社群
- Obsidian 双链 / 反链接 / 上传顺序无关的解析
- `graph_read` 检索模式
- 45 个单元 + API 测试，覆盖解析、入库、幂等、上下文组装

### P1 —— 不增加部署组件的能力补齐（建议下一步）

| 能力 | 做法 | 成本 |
|---|---|---|
| **摘要层（≈SUMMARIES/GRAPH_SUMMARY）** | 新增 `summaries` 表（doc / 社区 / 全局），lazy 生成并缓存；检索命中摘要直接作为高优先级上下文 | 1 张表 + 1 个模块 |
| **时间维度（≈TEMPORAL）** | `relations` 加 `valid_from/valid_to`，从文档元信息与正文时间抽取；检索支持「最近/某年」过滤与时效加权 | 2 列 + 解析规则 |
| **检索评测** | 内置 golden set（问题→期望 chunk），`scripts/eval_retrieval.py` 输出 Recall@k / nDCG，调参有据 | 1 个脚本 |
| **任务持久化** | `jobs` 表记录待处理文档，启动时恢复 `processing` 状态，避免重启后卡死 | 1 张表 + 启动钩子 |
| **存储抽象** | `VectorStore` / `GraphStore` 协议，默认实现仍是 SQLite+numpy；需要时接 pgvector（复用同一 PostgreSQL 实例） | 接口层重构 |

### P2 ——「上生产」才需要

- 用户体系与 dataset ACL（单机可用 token + 知识库授权表）
- MCP server：把 `add/search/notes` 暴露成工具，供 Agent 直接调用
- 记忆衰减与遗忘（按访问频次/时间给 chunk 打权重，定期降权归档）
- Query-aware 子图：用 Personalized PageRank 替代当前度数排序，进一步提纯长尾实体

---

## 五、为什么没有直接内嵌 Cognee

| 方案 | 部署代价 | 结论 |
|---|---|---|
| 直接依赖 Cognee | 常规要拉起 LanceDB（pyarrow 依赖）+ Kuzu（平台二进制）+ 关系库；Windows/ARM 上装轮子容易翻车 | ❌ 与「双击 `start.bat` 就能用」的定位冲突 |
| 抄 Cognee 的存储架构 | 同上，只是自己维护 | ❌ |
| **保持单库、借鉴其能力设计（本次）** | 仍是 `pip install -r requirements.txt` + 一个 `.db` | ✅ |

真正的取舍是：**用「能力对齐」换「部署复杂度不变」**。
代价是超大图谱（百万级边）会受 SQLite + 内存矩阵限制 —— 这种情况本来也不该跑在这个定位的产品里。

---

## 六、验证

```bash
cd backend && python3 -m pytest tests -q
# 45 passed
```

覆盖点：
- frontmatter/双链/标签解析边界（未闭合 frontmatter、表格转义 `\|`、纯锚点、代码围栏、去重）
- 跨笔记解析与上传顺序无关、幂等重建、删除后反链接解绑
- 多跳路径生成、叙述渲染、空图/小图降级、无 LLM 时的规则兜底
- 上下文顺序与字符预算、笔记反链接注入
- API 路由（笔记列表、反链接、relink、图解读、统计）

已知限制：
- 双链解析只对 `.md` / `.markdown` 生效（PDF/Word 里的 `[[...]]` 是正文符号）
- 同名笔记跨库会共用一个实体节点（与现有实体策略一致）
- 反链接上下文默认只取命中笔记的前 4 条，避免 prompt 膨胀