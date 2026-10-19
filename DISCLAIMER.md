# 免责声明 / Disclaimer

> 本节同时提供中英文版本。中文版与英文版如有冲突，以英文版为准。
> If there is any conflict between the Chinese and English versions, the English
> version shall prevail.

---

## 中文版

本项目（以下简称"本软件"）按"现状（AS IS）"提供，作者与贡献者不对软件
的适用性、可靠性、可用性、准确性、合规性作任何明示或暗示的担保，亦不承担
因使用本软件而产生的任何直接或间接损失，包括但不限于：

### 一、模型输出免责
1. 本软件使用第三方大语言模型（LLM）API（如 SiliconFlow、OpenAI、Ollama、
   以及任何用户自行配置的 OpenAI 兼容服务）生成回答。**所有模型输出均为模型
   推断结果，不保证真实性、准确性、合法性、合规性或时效性**。
2. 任何基于本软件输出所作的决策、行动、法律或商业后果，由**使用者自行承担**。
3. 在医疗、法律、金融、工程等关键领域使用本软件前，请由专业人士人工核验
   全部输出内容。

### 二、数据与隐私免责
1. 本软件默认将全部数据（包括但不限于上传文档原文、知识图谱、聊天历史、
   API Key、向量索引）存储于本机 SQLite 数据库 `backend/data/app.db` 与
   `backend/data/uploads/` 目录下。**这些数据未经加密**，请按本地敏感数据管理。
2. 上传文档的版权、合规性、隐私合法性由**文档上传者负责**。请勿上传未经授权
   的第三方文件或机密文件。
4. 在「设置」页填入的第三方 API Key 由您持有；本软件不收集、不上传这些 Key，
   但您需自行承担第三方服务对 API 调用的记录与计费。
5. 启用 `--host 0.0.0.0` 后，本软件将暴露在网络上。**此时所有数据可被任何
   能访问到该端口的人读取、删除或滥用**。请参见 `SECURITY.md` 第 1 条。

### 三、服务可用性免责
1. 本软件依赖第三方 LLM / Embedding / Rerank 服务的可用性，**作者不保证**
   这些第三方服务持续可用、不限速、不变更接口。
2. 作者不承担因第三方服务变更、计费调整、停服而造成的任何损失。
3. 作者不提供任何形式的商业技术支持、运维服务、SLA 保障。

### 四、责任限制
在适用法律允许的最大范围内，无论基于何种法律理论（合同、侵权、过失、
严格责任或其他），作者与贡献者对您承担的全部责任上限为您为本软件实际
支付的金额（如未支付，则为零元）。

---

## English Version

This project (the "Software") is provided "AS IS", without warranty of any kind,
express or implied, including but not limited to the warranties of
merchantability, fitness for a particular purpose, and noninfringement. In no
event shall the authors or contributors be liable for any claim, damages, or
other liability arising from, out of, or in connection with the Software or the
use or other dealings in the Software.

### 1. Model Output Disclaimer
1. The Software calls third-party Large Language Model (LLM) APIs (including
   but not limited to SiliconFlow, Zhipu, OpenAI, Ollama, and any OpenAI-compatible
   service configured by the user). **All model outputs are inferred and are NOT
   guaranteed to be true, accurate, legal, compliant, or up-to-date.**
3. **You are solely responsible** for any decisions, actions, legal or business
   consequences taken based on the Software's output.
4. Do not use the Software in medical, legal, financial, engineering, or other
   critical domains without professional human verification of all outputs.

### 2. Data & Privacy Disclaimer
1. By default, the Software stores all data — including but not limited to
   uploaded document content, knowledge graph, chat history, API keys, and vector
   indexes — in the local SQLite database `backend/data/app.db` and the upload
   directory `backend/data/uploads/`. **These data are NOT encrypted**. Please
   handle them as local sensitive data.
2. **The uploader is solely responsible** for the copyright, compliance, and
   privacy legality of any uploaded document. Do not upload unauthorized or
   confidential documents.
3. API keys you enter in the "Settings" page belong to you. The Software does
   not collect or upload these keys, but you are responsible for the billing and
   usage records held by the third-party service providers.
4. Running the Software with `--host 0.0.0.0` exposes it to a network where
   **anyone able to reach it can read, delete, or abuse all data**. See
   `SECURITY.md` item 1.

### 3. Service Availability Disclaimer
1. The Software depends on third-party LLM / Embedding / Rerank services.
   **The authors do not guarantee** the continued availability, rate limits, or
   interface stability of these third-party services.
3. The authors are not liable for any damages arising from third-party service
   changes, billing adjustments, or service termination.
4. The authors do not provide commercial technical support, operations, or SLA.

### 4. Limitation of Liability
To the maximum extent permitted by applicable law, in no event and under no
legal theory (contract, tort, negligence, strict liability, or otherwise) shall
the authors or contributors be liable to you for any amount exceeding the fees
actually paid by you for the Software (which is zero if you have not paid any).

---

## 开源许可 / Open Source License

本软件采用 MIT 许可证发布，详见 `LICENSE` 文件。
This Software is released under the MIT License, see the `LICENSE` file for
details.