# Security Policy

## 项目定位

这是一个**自托管（self-hosted）个人/小团队**的本地知识库问答系统，默认仅监听
`127.0.0.1`，按"个人内网工具"的威胁模型设计，**不包含内置的用户认证与授权**。

## 支持的版本

| 版本 | 支持状态 |
| ---- | -------- |
| main 分支 | ✅ 接受漏洞报告 |
| 其余分支 | 仅做尽力维护，请先合入 main 再发 PR |

## 已知风险（Important）

请在部署前逐条阅读。下面这些不是 bug，是设计取舍，但**误用即可造成安全事故**：

### 1. 默认监听本机，但 `--host 0.0.0.0` 后完全无认证
`python backend/run.py --host 0.0.0.0` 一旦启用，后端所有 API
（上传、删除、读取所有聊天记录、读取图谱、用你配置的 LLM 提问）将暴露在
该网段上。**任何能访问到端口的人 = 完全控制权**。
- 缓解方案：套反向代理（nginx / Caddy）并在前面加 BasicAuth 或 OIDC；
  或使用 VPN / Tailscale / SSH 隧道限定访问来源。

### 2. CORS 全开
后端使用 `CORSMiddleware(allow_origins=["*"])`，无任何来源校验。
默认 `127.0.0.1` 时无实际风险；公网部署会被任意网站跨域调用，叠加 #1 影响严重。

### 3. 上传路径使用客户端原始 filename
`POST /api/documents/upload` 会把客户端 `UploadFile.filename` 原样拼入
`uploads/<doc_id>_<filename>`。已通过扩展名白名单（pdf/docx/md/txt）限制可上传
类型，但**未对文件名做路径净化**。建议自行加上"强制重命名为安全文件名"的
patch，或在反向代理层强制拒绝路径段。

### 4. 上传大小无限制
单文件可无限大，会撑爆磁盘。在 nginx 层加 `client_max_body_size`，
或在 FastAPI 层加中间件限制 `Content-Length`。

### 5. Provider base_url 由用户配置，无 SSRF 防护
在「设置」页可以填任意 URL，后端会直接发起请求。
理论上可探测内网（127.0.0.1 / 192.168.x.x / 169.254.169.254）。
若你被诱导填入恶意 URL 即可被 SSRF。**默认仅本人使用，风险可控**；
若对外开放请加白名单或代理。

### 6. API Key 以明文存入本地 SQLite
`backend/data/app.db` → `settings` 表。
本机部署默认安全；但若该目录被泄露或备份到公网，Key 会一并泄露。

### 7. 错误信息可能暴露内部细节
LLM 调用失败时，错误文本会被截断后写入文档 `error` 字段并暴露在
`/api/documents` 接口。下游调用者可借此推断你用的 base_url / 模型名。
属于低危信息泄露。

### 8. SQLite 拼接 SQL 字符串
多处使用 `f"... WHERE id IN ({marks})"` + 参数化占位符，**当前没有 SQL 注入**，
但模式高风险。修改时务必保留参数化写法。

## 报告漏洞

- 通过 GitHub Issues 公开报告（适合已修复或低危问题）
- 通过 GitHub Security Advisories 私下报告（适合敏感 / 未修复漏洞）

报告内容请包含：
1. 复现步骤（命令 / 请求 / 响应）
2. 影响面（谁能利用、能做什么）
3. 你的环境（commit、Python 版本、OS）

修复与响应时间不构成任何承诺，作者保留自行决定是否处理、修复优先级与处理方式的权利。

## 安全部署建议（最低门槛）

```bash
# 1. 默认仅监听本机
python backend/run.py --host 127.0.0.1 --port 8000

# 2. 若要局域网访问，加 nginx 反向代理 + BasicAuth：
# /etc/nginx/conf.d/rag.conf
#   auth_basic "RAG";
#   auth_basic_user_file /etc/nginx/.htpasswd;

# 3. 备份前排除：
#   - 不要把 backend/data/ 目录上传到任何云端
#   - 若必须备份，加密后再上传

# 5. 定期清理 uploads/ 中不再需要的原始文件
```