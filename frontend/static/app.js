/* ================= 工具 ================= */
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = j.detail || j.error || msg;  // 业务错误（如检索 502 返回 {error}）也要透出真实原因
    } catch (e) {}
    throw new Error(msg);
  }
  return res.json();
}

function toast(msg, ok = true) {
  const el = document.createElement("div");
  el.className = "toast" + (ok ? "" : " err");
  // 多条通知纵向堆叠，避免互相遮挡
  el.style.top = 18 + document.querySelectorAll(".toast").length * 48 + "px";
  el.setAttribute("role", "status");
  el.textContent = msg;
  el.title = "点击关闭";
  el.addEventListener("click", () => el.remove());
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3200);
}

function fmtSize(n) {
  if (n > 1048576) return (n / 1048576).toFixed(1) + " MB";
  if (n > 1024) return (n / 1024).toFixed(1) + " KB";
  return n + " B";
}

/* ================= 知识库 ================= */
let KBS = [];
let currentKbId = null;

function kbNameOf(id) {
  const k = KBS.find((x) => x.id === id);
  return k ? k.name : "未选择";
}

async function loadKbs(selectId = null) {
  KBS = await api("/api/kbs");
  if (selectId == null) {
    const saved = parseInt(localStorage.getItem("kb_id") || "", 10);
    if (KBS.some((k) => k.id === saved)) currentKbId = saved;
  }
  if (!KBS.some((k) => k.id === currentKbId)) {
    currentKbId = KBS.length ? KBS[0].id : null;
  }
  if (selectId != null) currentKbId = selectId;
  if (currentKbId != null) localStorage.setItem("kb_id", currentKbId);
  renderKbSelectors();
  renderKbTabs();
  const tip = $("#upload-kb-name");
  if (tip) tip.textContent = kbNameOf(currentKbId);
}

function kbOptions() {
  return KBS.map(
    (k) => `<option value="${k.id}">${escapeHtml(k.name)}（${k.doc_count}）</option>`
  ).join("");
}

function renderKbSelectors() {
  for (const sel of ["#chat-kb", "#graph-kb"]) {
    const el = $(sel);
    if (!el) continue;
    el.innerHTML = kbOptions();
    if (currentKbId != null) el.value = String(currentKbId);
  }
}

function renderKbTabs() {
  const box = $("#kb-tabs");
  if (!box) return;
  box.innerHTML = KBS.map(
    (k) => `<div class="kb-tab ${k.id === currentKbId ? "active" : ""}" data-id="${k.id}" title="${escapeHtml(k.name)}">
      <span class="n">${escapeHtml(k.name)}</span><span class="c">${k.doc_count}</span></div>`
  ).join("");
}

async function setKb(kbId, opts = {}) {
  if (kbId == null) return;
  const changed = kbId !== currentKbId;
  currentKbId = kbId;
  localStorage.setItem("kb_id", kbId);
  renderKbSelectors();
  renderKbTabs();
  const tip = $("#upload-kb-name");
  if (tip) tip.textContent = kbNameOf(kbId);
  if ($("#view-docs").classList.contains("active")) loadDocs();
  if ($("#view-graph").classList.contains("active")) refreshGraph($("#graph-search").value.trim());
  loadKbStatus();
  // 知识库即问答语境：一旦切换就开启新对话，避免沿用旧库的会话绑定
  if (changed || opts.resetChat) newChat();
}

$("#chat-kb").addEventListener("change", (e) => {
  setKb(parseInt(e.target.value, 10));
});
$("#graph-kb").addEventListener("change", (e) => setKb(parseInt(e.target.value, 10)));
$("#kb-tabs").addEventListener("click", (e) => {
  const tab = e.target.closest(".kb-tab");
  if (tab) setKb(parseInt(tab.dataset.id, 10));
});

$("#btn-new-kb").addEventListener("click", async () => {
  const name = prompt("新知识库名称（如：产品手册 / 法规库 / 项目资料）：");
  if (name == null) return;
  if (!name.trim()) return toast("名称不能为空", false);
  try {
    const r = await api("/api/kbs", { method: "POST", body: JSON.stringify({ name: name.trim() }) });
    await loadKbs();
    await setKb(r.id);
    toast("知识库已创建");
  } catch (e) {
    toast(e.message, false);
  }
});

$("#btn-rename-kb").addEventListener("click", async () => {
  if (currentKbId == null) return;
  const cur = KBS.find((k) => k.id === currentKbId);
  const name = prompt("重命名知识库：", cur ? cur.name : "");
  if (name == null || !name.trim()) return;
  try {
    await api("/api/kbs/" + currentKbId, {
      method: "PUT",
      body: JSON.stringify({ name: name.trim() }),
    });
    await loadKbs(currentKbId);
    toast("已重命名");
  } catch (e) {
    toast(e.message, false);
  }
});

$("#btn-del-kb").addEventListener("click", async () => {
  if (currentKbId == null) return;
  const cur = KBS.find((k) => k.id === currentKbId);
  if (!confirm(`确定删除知识库「${cur ? cur.name : ""}」？\n其下全部文档、分块与图谱关系将一并删除，且不可恢复。`)) return;
  try {
    const r = await api("/api/kbs/" + currentKbId, { method: "DELETE" });
    toast(`已删除知识库（含 ${r.removed_docs} 份文档）`);
    await loadKbs();
    await setKb(currentKbId, { resetChat: true });
  } catch (e) {
    toast(e.message, false);
  }
});

/* ================= 视图路由 ================= */
let graphChart = null;
$("#nav").addEventListener("click", (e) => {
  const item = e.target.closest(".nav-item");
  if (!item) return;
  $$(".nav-item").forEach((n) => n.classList.remove("active"));
  item.classList.add("active");
  const view = item.dataset.view;
  $$(".view").forEach((v) => v.classList.remove("active"));
  $("#view-" + view).classList.add("active");
  if (view === "docs") loadDocs();
  if (view === "graph") initGraph();
  if (view === "settings") loadSettings();
});

/* ================= 聊天 ================= */
let currentChatId = null;
let sending = false;
let sendSeq = 0;          // 流式请求序号：切换会话/新对话后，旧响应自动失效
let currentAbort = null;  // 当前流式请求的 AbortController

/* 主动中止进行中的流式回答，避免旧回答继续往已切换的会话里写数据 */
function stopActiveStream() {
  sendSeq++;
  if (currentAbort) {
    currentAbort.abort();
    currentAbort = null;
  }
  sending = false;
  $("#btn-send").disabled = false;
}

function newChat() {
  stopActiveStream();
  currentChatId = null;
  $("#msg-wrap").innerHTML = `
    <div class="empty" id="chat-empty">
      <div class="eicon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg></div>
      <div class="etitle">向知识库提问吧</div>
      <div class="ehint">当前知识库：<b>${escapeHtml(kbNameOf(currentKbId))}</b> · 回答基于该库已上传文档</div>
      <div class="sugs">
        <button class="sug" data-q="这份资料的核心内容是什么？">这份资料的核心内容是什么？</button>
        <button class="sug" data-q="总结文档里的关键实体和它们的关系">总结关键实体和关系</button>
        <button class="sug" data-q="用知识图谱梳理一下主要脉络">用知识图谱梳理主要脉络</button>
        <button class="sug" data-q="文档中提到了哪些流程或方法？">文档中提到哪些流程或方法？</button>
      </div>
    </div>`;
  loadChatList();
}
$("#btn-new-chat").addEventListener("click", newChat);

$("#chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendQuery();
  }
});
$("#btn-send").addEventListener("click", sendQuery);

function escapeHtml(s) {
  // 引号也必须转义：知识库名/文件名/错误信息会被拼进 title="..." 这类属性里，
  // 只转义 &<> 时一个双引号就能闭合属性并注入事件处理器。
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/* 极简 markdown 渲染 + [n] 引用转上标 */
function renderMd(text) {
  let t = escapeHtml(text);
  const codeBlocks = [];
  t = t.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    codeBlocks.push(`<pre><code>${code}</code></pre>`);
    return `\x00CB${codeBlocks.length - 1}\x00`;
  });
  t = t.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  t = t.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
  t = t.replace(/^### (.+)$/gm, "<h4>$1</h4>");
  t = t.replace(/^## (.+)$/gm, "<h3>$1</h3>");
  t = t.replace(/^\s*[-•] (.+)$/gm, "<li>$1</li>");
  t = t.replace(/(<li>[\s\S]*?<\/li>)(?!\s*<li>)/g, "<ul>$1</ul>");
  t = t.replace(/\[(\d{1,2})\]/g, '<sup class="cite" data-n="$1">[$1]</sup>');
  t = t.replace(/\x00CB(\d+)\x00/g, (_, i) => codeBlocks[+i]);
  return t;
}

/* ===== 模型速率与 token 用量 ===== */
/* 与后端 providers.estimate_tokens 同口径：CJK ≈1 token/字，其余 ≈1/4 token/字符 */
function estimateTokens(text) {
  if (!text) return 0;
  let cjk = 0;
  for (const ch of text) if (ch >= "\u3400" && ch <= "\u9fff") cjk++;
  return Math.max(1, Math.round(cjk + (text.length - cjk) / 4));
}
const fmtInt = (n) => (n || 0).toLocaleString("en-US");

/* 流式过程中的实时速率（按字符估算，最终被 stats 事件的精确值替换） */
function updateLiveUsage(msgEl, acc, firstDeltaAt) {
  const el = msgEl.querySelector(".usage-line");
  if (!el) return;
  const toks = estimateTokens(acc);
  const secs = Math.max((performance.now() - firstDeltaAt) / 1000, 0.05);
  el.style.display = "block";
  el.textContent = `正在生成… 约 ${fmtInt(toks)} tok · ${(toks / secs).toFixed(1)} tok/s（实时估算）`;
}

function hideUsage(msgEl) {
  const el = msgEl && msgEl.querySelector(".usage-line");
  if (el) el.style.display = "none";
}

function showUsage(msgEl, s) {
  if (!msgEl || !s) return;
  const el = msgEl.querySelector(".usage-line");
  if (!el) return;
  const parts = [];
  if (s.model) parts.push("⚡ " + s.model);
  if (s.ttft_ms != null) parts.push("首字 " + (s.ttft_ms / 1000).toFixed(2) + "s");
  if (s.tps) parts.push((+s.tps).toFixed(1) + " tok/s");
  parts.push("↑" + fmtInt(s.prompt_tokens) + " ↓" + fmtInt(s.completion_tokens));
  const total = s.total_tokens || (s.prompt_tokens || 0) + (s.completion_tokens || 0);
  let line = parts.join(" · ") + " · 共 " + fmtInt(total) + " tokens";
  if (s.aux_tokens) line += "（另检索辅助 " + fmtInt(s.aux_tokens) + "）";
  if (s.estimated) line += " · 估算值";
  if (s.latency_ms) line += " · 总耗时 " + (s.latency_ms / 1000).toFixed(1) + "s";
  el.textContent = line;
  el.title = "↑ 提示 tokens · ↓ 生成 tokens；「估算值」表示模型服务未返回精确用量，按字符数粗略折算";
  el.style.display = "block";
}

function appendMsg(role, content = "", refs = null, msgId = null) {
  const empty = $("#chat-empty");
  if (empty) empty.remove();
  const wrap = document.createElement("div");
  wrap.className = "msg " + (role === "user" ? "user" : "bot");
  const avatar = role === "user" ? "我" : "AI";
  wrap.innerHTML = `
    <div class="avatar">${avatar}</div>
    <div class="bubble">
      <div class="who">${role === "user" ? "你" : "知识库助手"}</div>
      <div class="content"></div>
      <div class="refs-panel" style="display:none"></div>
      <div class="usage-line" style="display:none"></div>
    </div>`;
  if (role === "bot") {
    wrap.dataset.msgId = msgId || "";
    const bar = document.createElement("div");
    bar.className = "vote-bar";
    bar.innerHTML = `
      <button class="vote-btn" data-vote="1" title="回答有帮助">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 10v12"/><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z"/></svg>
        有用</button>
      <button class="vote-btn" data-vote="-1" title="回答有待改进">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 14V2"/><path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z"/></svg>
        待改进</button>
      <span class="vote-done" style="display:none">✓ 已反馈</span>`;
    wrap.querySelector(".bubble").appendChild(bar);
  }
  const contentEl = wrap.querySelector(".content");
  if (role === "user") contentEl.textContent = content;
  else contentEl.innerHTML = renderMd(content);
  $("#msg-wrap").appendChild(wrap);
  scrollChat(role === "user");
  return wrap;
}

function scrollChat(force = false) {
  const box = $("#chat-messages");
  // 用户上翻阅读历史时不强行拉回底部：仅贴近底部或新消息入列时才滚动
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 90;
  if (force || nearBottom) box.scrollTop = box.scrollHeight;
}

function showRefs(msgEl, refs, triples = [], entities = [], searchQuery = null, web = null, extra = null) {
  const panel = msgEl.querySelector(".refs-panel");
  const webInfo = !!(web && web.enabled);
  const narrative = (extra && extra.narrative) || "";
  const noteCtx = (extra && extra.notes) || "";
  if ((!refs || !refs.length) && !triples.length && !webInfo && !narrative && !noteCtx) return;
  let html = "";
  if (webInfo) {
    html += web.error
      ? `<div class="rp-line rp-web err">联网搜索失败：${escapeHtml(web.error)}（已回退到知识库）</div>`
      : `<div class="rp-line rp-web">联网搜索：获取 ${web.count || 0} 条网页结果</div>`;
  }
  if (webInfo && web.query) {
    html += `<div class="rp-line">联网检索词：<b>${escapeHtml(web.query)}</b></div>`;
  }
  if (searchQuery) {
    html += `<div class="rp-line">检索词（已做多轮指代消解）：<b>${escapeHtml(searchQuery)}</b></div>`;
  }
  if (narrative) {
    html += `<details class="graph-read"><summary class="rp-title">图谱解读（AI 读到的结构）</summary>` +
      `<div style="white-space:pre-wrap;font-size:12px;line-height:1.75;color:var(--text-2);margin-top:6px">${escapeHtml(narrative)}</div></details>`;
  }
  if (noteCtx) {
    html += `<details class="graph-read"><summary class="rp-title">笔记链接（反链接与知识网络）</summary>` +
      `<div style="white-space:pre-wrap;font-size:12px;line-height:1.75;color:var(--text-2);margin-top:6px">${escapeHtml(noteCtx)}</div></details>`;
  }
  if (refs && refs.length) {
    html += `<div class="rp-title">引用来源（${refs.length}）</div>`;
    html += refs
      .map((r) => {
        const badge = r.source === "web" ? '<span class="ref-badge">网页</span>' : "";
        const doc = r.url
          ? `<a class="ref-doc ref-link" href="${escapeHtml(r.url)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(r.url)}">${escapeHtml(r.doc)}</a>`
          : `<span class="ref-doc">${escapeHtml(r.doc)}</span>`;
        return `<div class="ref-item" data-n="${r.n}"><span class="ref-n">${r.n}</span>
          <div>${doc}${badge}
          <span class="ref-score">${r.score}</span>
          <div class="ref-text">${escapeHtml(r.text)}</div></div></div>`;
      })
      .join("");
  }
  if (triples && triples.length) {
    html += `<div class="triples"><span class="rp-title">图谱三元组（${triples.length}）</span>` +
      triples.map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join("") + "</div>";
  }
  if (entities && entities.length) {
    html += `<div class="rp-line" style="margin-top:8px"><span class="rp-title">命中实体</span>` + entities.map((e) => `<span class="tag">${escapeHtml(e)}</span>`).join("") + `</div>`;
  }
  if (html) {
    panel.innerHTML = html;
    panel.style.display = "block";
  }
}

async function sendQuery() {
  const input = $("#chat-input");
  const query = input.value.trim();
  if (!query || sending) return;
  sending = true;
  $("#btn-send").disabled = true;
  input.value = "";
  input.style.height = "auto";
  appendMsg("user", query);
  const botEl = appendMsg("bot", "", null);
  const contentEl = botEl.querySelector(".content");
  contentEl.innerHTML = '<span class="pending-note">正在检索并生成回答…</span>';

  let acc = "";
  let statsData = null;
  let firstDeltaAt = null;
  /* 流式渲染合并：SSE 每 token 一次 delta，直接 innerHTML 全量重渲是 O(n²) 成本，
     长回答会越写越卡。用 rAF 把同一帧内的多个 delta 合并成一次渲染。 */
  let renderQueued = false;
  function flushRender() {
    renderQueued = false;
    if (seq !== sendSeq) return;  // 已切到其他会话：丢弃积压渲染，防止旧内容闪回
    contentEl.innerHTML = renderMd(acc);
    updateLiveUsage(botEl, acc, firstDeltaAt);
    scrollChat();
  }
  function queueRender() {
    if (renderQueued) return;
    renderQueued = true;
    requestAnimationFrame(flushRender);
  }
  const seq = ++sendSeq;
  const controller = new AbortController();
  currentAbort = controller;
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: controller.signal,
      body: JSON.stringify({
        chat_id: currentChatId,
        query,
        mode: $("#chat-mode").value,
        kb_id: currentKbId,
        web: $("#web-search").checked,
      }),
    });
    if (!res.ok) {
      let msg = "HTTP " + res.status;
      try { const j = await res.json(); if (j && j.detail) msg = j.detail; } catch (e) {}
      throw new Error(msg);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (seq !== sendSeq) return;  // 已切到新对话/其他会话，丢弃旧响应
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop();
      for (const part of parts) {
        const evMatch = part.match(/^event: (.+)$/m);
        const dataMatch = part.match(/^data: (.+)$/m);
        if (!evMatch || !dataMatch) continue;
        const ev = evMatch[1];
        let data = {};
        try { data = JSON.parse(dataMatch[1]); } catch (e) {}
        if (ev === "meta") {
          showRefs(botEl, data.refs, data.graph_triples, data.graph_entities, data.search_query, data.web,
            { narrative: data.graph_narrative, paths: data.graph_paths, notes: data.notes });
        } else if (ev === "delta") {
          if (firstDeltaAt === null) firstDeltaAt = performance.now();
          acc += data.text;
          queueRender();
        } else if (ev === "stats") {
          statsData = data;
          showUsage(botEl, data);
        } else if (ev === "error") {
          acc += (acc ? "\n\n" : "") + "**生成失败：" + (data.message || "请检查模型配置") + "**";
          contentEl.innerHTML = renderMd(acc);
        } else if (ev === "end") {
          if (data.chat_id && data.chat_id !== currentChatId) {
            currentChatId = data.chat_id;
            loadChatList();
          }
          if (data.message_id) botEl.dataset.msgId = data.message_id;
          if (!statsData) hideUsage(botEl);  // 无用量数据（如知识库为空的固定回复）时隐藏行
        }
      }
    }
    if (seq !== sendSeq) return;
    if (!acc) contentEl.innerHTML = '<span style="color:var(--muted)">（无输出，请检查设置中的模型配置）</span>';
  } catch (e) {
    if (seq !== sendSeq || e.name === "AbortError") return;  // 用户主动切换导致的取消
    hideUsage(botEl);
    contentEl.innerHTML = `<span style="color:var(--red)">请求失败：${escapeHtml(e.message)}。请确认模型设置正确。</span>`;
  } finally {
    if (seq === sendSeq) {
      sending = false;
      $("#btn-send").disabled = false;
      if (currentAbort === controller) currentAbort = null;
      input.focus();
    }
  }
}

/* 历史会话 / 建议问题 / 引用跳转（事件委托） */
$("#msg-wrap").addEventListener("click", async (e) => {
  // 空态建议问题：点一下即填入输入框
  const sug = e.target.closest(".sug");
  if (sug) {
    const inp = $("#chat-input");
    inp.value = sug.dataset.q || "";
    inp.dispatchEvent(new Event("input"));
    inp.focus();
    return;
  }
  // 引用上标 [n] → 滚动到下方对应来源并高亮
  const cite = e.target.closest("sup.cite");
  if (cite) {
    const n = cite.dataset.n;
    // 只在当前这条回答的引用面板里找：全局查找会跳到历史消息的同号引用
    const msgEl = cite.closest(".msg");
    const ref = msgEl && msgEl.querySelector(`.refs-panel .ref-item[data-n="${n}"]`);
    if (ref) {
      ref.scrollIntoView({ behavior: "smooth", block: "nearest" });
      ref.classList.remove("flash");
      void ref.offsetWidth; // 重启动画
      ref.classList.add("flash");
    }
    return;
  }
  const btn = e.target.closest(".vote-btn");
  if (!btn) return;
  const msgEl = btn.closest(".msg");
  const msgId = msgEl && msgEl.dataset.msgId;
  if (!msgId) {
    toast("回答完成后才能评价", false);
    return;
  }
  try {
    const r = await api("/api/feedback", {
      method: "POST",
      body: JSON.stringify({ message_id: parseInt(msgId, 10), vote: parseInt(btn.dataset.vote, 10) }),
    });
    const bar = btn.closest(".vote-bar");
    bar.querySelectorAll(".vote-btn").forEach((b) => (b.style.display = "none"));
    bar.querySelector(".vote-done").style.display = "inline";
    toast(r.recorded ? "感谢反馈，将用于改进后续检索" : "已记录（该回答无引用分块）");
  } catch (err) {
    toast(err.message, false);
  }
});

async function loadChatList() {
  const chats = await api("/api/chats");
  const box = $("#chat-list");
  box.innerHTML = '<div class="label">历史会话</div>';
  if (!chats.length) {
    box.innerHTML += '<div style="font-size:12px;color:var(--muted);padding:8px 10px">暂无会话</div>';
    return;
  }
  for (const c of chats) {
    const el = document.createElement("div");
    el.className = "chat-item" + (c.id === currentChatId ? " active" : "");
    const kbMini = c.kb_name ? `<span class="kb-mini">${escapeHtml(c.kb_name)}</span>` : "";
    el.innerHTML = `<span class="t">${escapeHtml(c.title || "对话")}${kbMini}</span><span class="x" title="删除">✕</span>`;
    el.querySelector(".t").addEventListener("click", () => openChat(c.id, c.kb_id));
    el.querySelector(".x").addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        await api("/api/chats/" + c.id, { method: "DELETE" });
      } catch (err) {
        toast(err.message, false);
        return;
      }
      if (currentChatId === c.id) $("#btn-new-chat").click();
      else loadChatList();
    });
    box.appendChild(el);
  }
}

async function openChat(chatId, kbId) {
  stopActiveStream();  // 切换历史会话时中止仍在进行的回答
  currentChatId = chatId;
  // 会话绑定某个知识库：打开时同步切换当前知识库
  if (kbId != null && kbId !== currentKbId && KBS.some((k) => k.id === kbId)) {
    currentKbId = kbId;
    localStorage.setItem("kb_id", kbId);
    renderKbSelectors();
    renderKbTabs();
    const tip = $("#upload-kb-name");
    if (tip) tip.textContent = kbNameOf(kbId);
  }
  const msgs = await api(`/api/chats/${chatId}/messages`);
  $("#msg-wrap").innerHTML = "";
  for (const m of msgs) {
    const el = appendMsg(m.role === "user" ? "user" : "bot", m.content, m.refs, m.id);
    if (m.role !== "user" && m.refs && m.refs.length) showRefs(el, m.refs);
    if (m.role !== "user" && m.stats) showUsage(el, m.stats);  // 历史回答的用量与速率
  }
  loadChatList();
  scrollChat(true);
}

/* ================= 文档库 ================= */
let docsTimer = null;

async function loadDocs() {
  const q = currentKbId != null ? `?kb_id=${currentKbId}` : "";
  const docs = await api("/api/documents" + q);
  const tbody = $("#docs-tbody");
  tbody.innerHTML = "";
  $("#docs-empty").style.display = docs.length ? "none" : "block";
  let totalChunks = 0, done = 0;
  for (const d of docs) {
    totalChunks += d.chunk_count;
    if (d.status === "done") done++;
    const tr = document.createElement("tr");
    const statusMap = {
      done: '<span class="badge done"><i class="dot"></i>已完成</span>',
      processing: `<span class="badge processing"><i class="dot"></i>处理中·${escapeHtml(d.stage || "")}</span>`,
      pending: '<span class="badge pending"><i class="dot"></i>等待中</span>',
      error: `<span class="badge error"><i class="dot"></i>失败</span>`,
    };
    const noteBadge = d.note_title
      ? `<span class="note-badge" title="Markdown 笔记标题（双链解析依据）">[[${escapeHtml(d.note_title)}]]</span>`
      : "";
    tr.innerHTML = `
      <td class="doc-name" title="${escapeHtml(d.filename)}">${noteBadge}${escapeHtml(d.filename)}</td>
      <td class="num">${fmtSize(d.size)}</td>
      <td>${statusMap[d.status] || d.status}${d.error ? `<div class="err-tip" title="${escapeHtml(d.error)}">${escapeHtml(d.error)}</div>` : ""}</td>
      <td class="num">${d.chunk_count || "-"}</td>
      <td class="num">${d.triple_count || "-"}</td>
      <td style="font-size:11px;color:var(--muted)">${escapeHtml((d.embed_tag || "").split("::")[1] || "-")}</td>
      <td class="actions">
        ${d.note_title ? `<button class="small" data-act="links" data-id="${d.id}" title="查看反链接与出链">双链</button>` : ""}
        <button class="small" data-act="reparse" data-id="${d.id}" title="清空分块后重新解析（启用 OCR 后可识别扫描件）">重解析</button>
        <button class="small" data-act="graph" data-id="${d.id}" ${d.status !== "done" ? "disabled" : ""} title="重新用 LLM 抽取实体关系">建图谱</button>
        <button class="small danger" data-act="del" data-id="${d.id}">删除</button>
      </td>`;
    tbody.appendChild(tr);
  }
  $("#docs-sub").textContent = docs.length
    ? `${kbNameOf(currentKbId)} · ${docs.length} 份文档 · ${done} 份就绪 · ${totalChunks} 个分块`
    : `${kbNameOf(currentKbId)} · 暂无文档`;
  const hasProcessing = docs.some((d) => d.status === "processing" || d.status === "pending");
  clearTimeout(docsTimer);
  if (hasProcessing) docsTimer = setTimeout(loadDocs, 2000);
}

/* ---------- 笔记双链：反链接面板 + 存量补解析 ---------- */
async function openNoteLinks(docId) {
  const panel = $("#note-panel");
  $("#note-panel-title").textContent = "加载中…";
  $("#note-panel-meta").textContent = "";
  $("#note-panel-body").innerHTML = "";
  panel.classList.add("show");
  let d;
  try {
    d = await api(`/api/notes/${docId}/backlinks`);
  } catch (e) {
    $("#note-panel-title").textContent = "读取失败";
    $("#note-panel-body").innerHTML = `<div class="note-empty">${escapeHtml(e.message)}</div>`;
    return;
  }
  $("#note-panel-title").textContent = `《${d.meta.title || d.meta.filename}》`;
  const meta = [];
  if (d.meta.tags && d.meta.tags.length) meta.push("标签 " + d.meta.tags.map((t) => "#" + t).join(" "));
  if (d.meta.aliases && d.meta.aliases.length) meta.push("别名 " + d.meta.aliases.join("、"));
  $("#note-panel-meta").textContent = meta.join(" · ") || "Markdown 笔记";

  let html = "";
  if (d.backlinks && d.backlinks.length) {
    html += `<div class="note-sec"><h4>反向链接（${d.backlinks.length}）：谁引用了本笔记</h4>` +
      d.backlinks.map((b) => `<div class="note-item"><span class="src">《${escapeHtml(b.title)}》</span>` +
        (b.anchor ? `（锚点：${escapeHtml(b.anchor)}）` : "") +
        (b.context ? `<div class="quote">${escapeHtml(b.context)}</div>` : "") + `</div>`).join("") +
      `</div>`;
  } else {
    html += `<div class="note-sec"><h4>反向链接</h4><div class="note-empty">暂无其他笔记引用它；上传引用本笔记的 Markdown 后会自动出现</div></div>`;
  }
  if (d.outlinks && d.outlinks.length) {
    html += `<div class="note-sec"><h4>出链（${d.outlinks.length}）：本笔记指向</h4>` +
      d.outlinks.map((o) => `<div class="note-item"><span class="src">` +
        (o.dst_doc_title ? `《${escapeHtml(o.dst_doc_title)}》` : escapeHtml(o.dst_title)) + `</span>` +
        (o.dst_doc_id ? "" : ` <span style="color:var(--yellow)">尚未创建</span>`) +
        (o.anchor ? ` <span class="quote">#${escapeHtml(o.anchor)}</span>` : "") + `</div>`).join("") +
      `</div>`;
  }
  $("#note-panel-body").innerHTML = html;
}
$("#note-panel-close").addEventListener("click", () => $("#note-panel").classList.remove("show"));

// 存量补解析：升级前上传的 .md 没有 note_title，点一次即可补齐双链（不重新向量化）
$("#btn-notes").addEventListener("click", async () => {
  const btn = $("#btn-notes");
  btn.disabled = true;
  btn.textContent = "解析中…";
  try {
    const r = await api("/api/notes/relink", {
      method: "POST",
      body: JSON.stringify({ kb_id: currentKbId }),
    });
    toast(`双链已重建：补解析 ${r.backfilled} 篇笔记，解析 ${r.resolved} 条链接，图谱边 ${r.edges} 条`);
    await loadDocs();
    loadKbStatus();
  } catch (e) {
    toast(e.message, false);
  } finally {
    btn.disabled = false;
    btn.textContent = "笔记双链";
  }
});

tbodyEvents();
function tbodyEvents() {
  $("#docs-tbody").addEventListener("click", async (e) => {
    const btn = e.target.closest("button");
    if (!btn || btn.disabled) return;
    const id = btn.dataset.id;
    if (btn.dataset.act === "links") {
      openNoteLinks(id);
      return;
    }
    if (btn.dataset.act === "del") {
      if (!confirm("删除该文档及其全部分块与图谱关系？")) return;
      try {
        await api("/api/documents/" + id, { method: "DELETE" });
      } catch (err) {
        toast(err.message, false);
        return;
      }
      loadDocs();
      loadKbs();
    } else if (btn.dataset.act === "reparse") {
      if (!confirm("重新解析会清空该文档现有的分块与图谱关系，再重新处理一遍。\n启用 OCR 后可用它重跑扫描件。继续？")) return;
      btn.disabled = true;
      btn.textContent = "解析中…";
      try {
        await api(`/api/documents/${id}/reprocess`, { method: "POST" });
        toast("已开始重新解析（后台运行）");
      } catch (err) {
        toast(err.message, false);
      }
      setTimeout(loadDocs, 800);
    } else if (btn.dataset.act === "graph") {
      btn.disabled = true;
      btn.textContent = "构建中…";
      try {
        await api(`/api/documents/${id}/graph`, { method: "POST" });
        toast("已开始构建知识图谱（后台运行）");
      } catch (err) {
        toast(err.message, false);
        btn.disabled = false;
        btn.textContent = "建图谱";
        return;
      }
      setTimeout(loadDocs, 800);
    }
  });
}

/* 上传 */
const zone = $("#upload-zone");
zone.addEventListener("click", () => $("#file-input").click());
zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("drag"); });
zone.addEventListener("dragleave", () => zone.classList.remove("drag"));
zone.addEventListener("drop", (e) => {
  e.preventDefault();
  zone.classList.remove("drag");
  handleFiles(e.dataTransfer.files);
});
$("#file-input").addEventListener("change", (e) => {
  handleFiles(e.target.files);
  e.target.value = "";
});

async function handleFiles(files) {
  if (!files || !files.length) return;
  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  try {
    const url = "/api/documents/upload" + (currentKbId != null ? "?kb_id=" + currentKbId : "");
    const res = await fetch(url, { method: "POST", body: fd });
    const j = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(j.detail || "上传失败");
    const skipped = j.skipped || [];
    const rejected = j.rejected || [];
    let msg = `已上传 ${(j.doc_ids || []).length} 个文件到「${kbNameOf(currentKbId)}」，后台处理中…`;
    if (skipped.length) msg += `；跳过 ${skipped.length} 个重复文档（内容已存在）`;
    if (rejected.length) msg += `；${rejected.length} 个文件类型不支持`;
    toast(msg);
    loadDocs();
    loadKbs();
  } catch (e) {
    toast(e.message, false);
  }
}

/* ================= 知识图谱 ================= */
let graphInited = false;

/* ECharts（约 1MB）只有图谱视图用得到，首屏不加载：
   切到图谱视图时才注入 <script>，重复调用复用同一个加载 Promise。 */
let echartsPromise = null;
function loadECharts() {
  if (window.echarts) return Promise.resolve();
  if (!echartsPromise) {
    echartsPromise = new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = "/static/echarts.min.js";
      s.onload = () => resolve();
      s.onerror = () => {
        echartsPromise = null;  // 允许网络恢复后重试
        reject(new Error("图表库 echarts.min.js 加载失败，请刷新重试"));
      };
      document.head.appendChild(s);
    });
  }
  return echartsPromise;
}

async function initGraph() {
  if (!graphChart) {
    try {
      await loadECharts();
    } catch (e) {
      $("#graph-stats").innerHTML = `<span style="color:var(--yellow)">${escapeHtml(e.message)}</span>`;
      return;
    }
    graphChart = echarts.init($("#graph-chart"));
    graphChart.on("click", (params) => {
      if (params.dataType === "node") openEntity(params.data.id);
    });
    // resize 高频触发，每帧合并一次；直接调会强制同步重排
    let resizeRaf = 0;
    window.addEventListener("resize", () => {
      cancelAnimationFrame(resizeRaf);
      resizeRaf = requestAnimationFrame(() => graphChart && graphChart.resize());
    });
  }
  graphChart.resize();
  await refreshGraph();
}

async function refreshGraph(q = "") {
  if (!graphChart) return;  // 图表库尚未加载成功时（如 initGraph 失败），直接跳过渲染
  const kbq = currentKbId != null ? "&kb_id=" + currentKbId : "";
  const [stats, data] = await Promise.all([
    api("/api/graph/stats" + (currentKbId != null ? "?kb_id=" + currentKbId : "")),
    api("/api/graph?limit=300" + (q ? "&q=" + encodeURIComponent(q) : "") + kbq),
  ]);
  $("#graph-stats").innerHTML = `${escapeHtml(kbNameOf(currentKbId))} · 实体 ${stats.entities} · 关系 ${stats.relations}` +
    (stats.notes ? ` · 笔记 ${stats.notes} · 双链 ${stats.links}（未解析 ${stats.unresolved}）` : "") +
    (q ? ` · 筛选"${escapeHtml(q)}"` : "");
  if (!data.nodes.length) {
    graphChart.clear();
    graphChart.setOption({
      title: { text: `知识库「${kbNameOf(currentKbId)}」暂无图谱数据：请先在「文档库」上传文档（会自动构建），或点击「建图谱」`, left: "center", top: "middle", textStyle: { color: "#8f9bab", fontSize: 13 } },
    });
    return;
  }
  graphChart.setOption(
    {
      tooltip: {
        backgroundColor: "rgba(16,20,28,.94)",
        borderColor: "#2e3950",
        textStyle: { color: "#e3e9f2", fontSize: 12 },
        formatter: (p) => {
          if (p.dataType === "node") {
            return `${p.name}（度数 ${p.value || 0}）` + (p.data && p.data.desc ? `<br/>${p.data.desc}` : "");
          }
          return `${p.data.source} —${p.data.value || ""}→ ${p.data.target}`;
        },
      },
      legend: [{ data: data.categories.map((c) => c.name), textStyle: { color: "#8f9bab" }, type: "scroll", right: 10, bottom: 10, pageIconColor: "#8f9bab", pageTextStyle: { color: "#8f9bab" } }],
      series: [
        {
          type: "graph",
          layout: "force",
          roam: true,
          draggable: true,
          categories: data.categories,
          data: data.nodes,
          links: data.links,
          force: { repulsion: 220, edgeLength: [40, 120], gravity: 0.08, layoutAnimation: true },
          label: { show: true, position: "right", color: "#dbe3ef", fontSize: 11 },
          labelLayout: { hideOverlap: true },
          lineStyle: { color: "source", curveness: 0.15, opacity: 0.55 },
          emphasis: { focus: "adjacency", lineStyle: { width: 3 } },
          itemStyle: { borderColor: "#0b0e14", borderWidth: 1 },
        },
      ],
      color: ["#4f8cff", "#3dd68c", "#e7c25f", "#f472b6", "#a78bfa", "#fb923c", "#2dd4bf", "#e879f9"],
    },
    true
  );
}

$("#graph-refresh").addEventListener("click", () => refreshGraph($("#graph-search").value.trim()));
$("#graph-search").addEventListener("keydown", (e) => {
  if (e.key === "Enter") refreshGraph(e.target.value.trim());
});

// 图解读：展示「AI 实际读到的图」——实体档案、多跳路径与社群，
// 用于调参和解释「为什么回答里会提到某条间接关系」。
$("#graph-read-btn").addEventListener("click", async () => {
  const out = $("#graph-read-out");
  const q = $("#graph-read-q").value.trim();
  const kbq = currentKbId != null ? "&kb_id=" + currentKbId : "";
  out.style.display = "block";
  out.textContent = "解读中…";
  try {
    const r = await api(`/api/graph/read?q=${encodeURIComponent(q)}${kbq}`);
    out.textContent = r.narrative || "暂无图谱结构可解读（可先上传文档并构建图谱）";
  } catch (e) {
    out.textContent = "解读失败：" + e.message;
  }
});
$("#graph-read-q").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("#graph-read-btn").click();
});

// 重建双链：批量导入笔记后手动补挂反链接（上传流程已自动触发，这里给个显式入口）
$("#graph-relink").addEventListener("click", async () => {
  const r = await api("/api/notes/relink", {
    method: "POST",
    body: JSON.stringify({ kb_id: currentKbId }),
  });
  await refreshGraph($("#graph-search").value.trim());
  $("#graph-read-out").style.display = "block";
  $("#graph-read-out").textContent =
    `双链重建完成：新解析 ${r.resolved} 条，图谱边 ${r.edges} 条，` +
    `共 ${r.stats.notes} 篇笔记 / ${r.stats.links} 条链接（未解析 ${r.stats.unresolved}）`;
});

async function openEntity(entityId) {
  const d = await api("/api/graph/entity/" + entityId);
  $("#entity-name").textContent = d.entity.name;
  $("#entity-type").textContent = `${d.entity.type || "未分类"} · 提及 ${d.entity.mention_count} 次`;
  const descEl = $("#entity-desc");
  if (d.entity.description) {
    descEl.textContent = d.entity.description;
    descEl.style.display = "block";
  } else {
    descEl.style.display = "none";
  }
  const aliasEl = $("#entity-aliases");
  if (d.aliases && d.aliases.length) {
    aliasEl.innerHTML = "别名：" + d.aliases.map((a) => `<span class="tag">${escapeHtml(a)}</span>`).join("");
    aliasEl.style.display = "block";
  } else {
    aliasEl.style.display = "none";
  }
  const rels = d.relations
    .map((r) => {
      const isHead = r.hname === d.entity.name;
      const other = isHead ? r.tname : r.hname;
      const rel = r.inferred ? `${r.relation}（推断）` : r.relation;
      const arrow = isHead ? `—${rel}→` : `←${rel}—`;
      return `<div class="rel">${escapeHtml(d.entity.name)} ${escapeHtml(arrow)} <b style="color:var(--accent)">${escapeHtml(other)}</b></div>`;
    })
    .join("");
  $("#entity-rels").innerHTML = rels || '<div class="rel">暂无关联关系</div>';
  $("#entity-panel").classList.add("show");
}
$("#entity-close").addEventListener("click", () => $("#entity-panel").classList.remove("show"));

/* ================= 设置 ================= */
let PRESETS = {};

function fillProviderSelect(sel, presets) {
  sel.innerHTML = Object.entries(presets)
    .map(([k, v]) => `<option value="${k}">${v.label}</option>`)
    .join("");
}

async function loadSettings() {
  PRESETS = await api("/api/presets");
  const cfg = await api("/api/settings");
  const secs = [
    ["llm", "llm"],
    ["emb", "embedding"],
    ["rr", "rerank"],
    ["ocr", "ocr"],
  ];
  for (const [pfx, sec] of secs) {
    fillProviderSelect($("#" + pfx + "-provider"), PRESETS);
    const c = cfg[sec] || {};
    $("#" + pfx + "-provider").value = c.provider || "custom";
    $("#" + pfx + "-base-url").value = c.base_url || "";
    $("#" + pfx + "-api-key").value = c.api_key || "";
    $("#" + pfx + "-model").value = c.model || "";
    applyPreset(pfx, c.provider, false);
  }
  $("#rr-enabled").checked = !!(cfg.rerank && cfg.rerank.enabled);
  const oc = cfg.ocr || {};
  $("#ocr-enabled").checked = !!oc.enabled;
  $("#ocr-dpi").value = oc.dpi || 150;
  $("#ocr-max-pages").value = oc.max_pages || 50;
  updateOcrTip();
  const rc = cfg.retrieval || {};
  $("#cfg-mode").value = rc.mode || "hybrid";
  $("#cfg-topk").value = rc.top_k || 6;
  $("#cfg-hops").value = rc.graph_hops || 2;
  $("#cfg-build-graph").checked = !(cfg.graph && cfg.graph.build_on_upload === false);
  $("#cfg-completion").checked = !!(cfg.graph && cfg.graph.completion);
  $("#upload-graph-tip").style.display = $("#cfg-build-graph").checked ? "inline" : "none";
  const onto = cfg.ontology || {};
  $("#onto-enabled").checked = !!onto.enabled;
  $("#onto-entity-types").value = onto.entity_types || "";
  $("#onto-relation-types").value = onto.relation_types || "";
  $("#onto-filter-search").checked = !!onto.filter_search;
}

function applyPreset(pfx, providerKey, clearKey = true) {
  const p = PRESETS[providerKey];
  if (!p) return;
  // 只在用户主动切换预设时覆盖 Base URL：页面加载时的 applyPreset(..., false)
  // 不能把用户手填的地址（如 Ollama 跑在别的机器上）冲回默认值
  // 切到「自定义」时预设的 base_url / model 是空串，也要清空，否则会残留上一家的值
  if (clearKey) $("#" + pfx + "-base-url").value = p.base_url || "";
  const modelField = $("#" + pfx + "-model");
  const listId = (pfx === "rr" ? "rr" : pfx) + "-models";
  const dl = document.getElementById(listId);
  const MF = {
    llm: ["llm_models", "llm_model"],
    emb: ["embed_models", "embed_model"],
    rr: ["rerank_models", "rerank_model"],
    ocr: ["ocr_models", "ocr_model"],
  };
  const [listKey, defKey] = MF[pfx] || MF.rr;
  dl.innerHTML = (p[listKey] || []).map((m) => `<option value="${m}">`).join("");
  const defModel = p[defKey];
  if (clearKey) modelField.value = defModel || "";

  // 快速填入按钮随预设重建：原来写死硅基流动的模型名，切到 Ollama 就全点错了
  const quickBox = document.getElementById(pfx + "-quick");
  if (quickBox) {
    const label = document.getElementById(pfx + "-quick-label");
    if (label) label.textContent = p.label || "";
    quickBox.innerHTML = "";
    (p[listKey] || []).forEach((m, i) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "small";
      b.textContent = (m.includes("/") ? m.split("/").pop() : m) + (i === 0 ? "（推荐）" : "");
      b.addEventListener("click", () => {
        modelField.value = m;
        modelField.focus();
      });
      quickBox.appendChild(b);
    });
  }

  const note = $("#" + pfx + "-note");
  if (note) {
    note.innerHTML = p.note || "";
    if (p.key_url) {
      note.innerHTML += `　<a href="${p.key_url}" target="_blank">获取 API Key ↗</a>`;
    }
  }
}

// ocr 曾经漏在这份名单外，导致 OCR 卡片切换提供商时 Base URL 与模型名都不跟着变
["llm", "emb", "rr", "ocr"].forEach((pfx) => {
  $("#" + pfx + "-provider").addEventListener("change", (e) => applyPreset(pfx, e.target.value, true));
});

async function testConn(pfx, section) {
  const el = $("#" + pfx + "-test-result");
  el.textContent = "测试中…";
  el.className = "test-result";
  await saveSettings(false);
  try {
    const r = await api(`/api/settings/test/${section}`, { method: "POST" });
    el.textContent = (r.ok ? "✓ 通过：" : "✗ 失败：") + r.detail;
    el.className = "test-result " + (r.ok ? "ok" : "fail");
  } catch (e) {
    el.textContent = "✗ 失败：" + e.message;
    el.className = "test-result fail";
  }
}
$("#llm-test").addEventListener("click", () => testConn("llm", "llm"));
$("#emb-test").addEventListener("click", () => testConn("emb", "embedding"));
$("#rr-test").addEventListener("click", () => testConn("rr", "rerank"));
$("#ocr-test").addEventListener("click", () => testConn("ocr", "ocr"));

/* 模型名快捷填入按钮：由 applyPreset 按当前预设的模型列表动态生成，
   不再写死具体模型名（否则切到 Ollama / 自定义时会点到别的服务商的模型） */

/* OCR 状态影响上传提示：关闭时明确告知扫描件无法解析 */
function updateOcrTip() {
  const on = $("#ocr-enabled").checked;
  const maxPages = $("#ocr-max-pages").value || 50;
  const tip = $("#ocr-upload-tip");
  if (!tip) return;
  tip.textContent = on
    ? `已启用 OCR：扫描件 PDF 与图片会自动识别文字，单份最多 ${maxPages} 页`
    : "未启用 OCR：扫描件 PDF 与图片无文字层，无法提取文字（可在「模型设置 → 文档 OCR」开启）";
  tip.style.color = on ? "var(--muted)" : "var(--red)";
}
$("#ocr-enabled").addEventListener("change", updateOcrTip);
$("#ocr-max-pages").addEventListener("input", updateOcrTip);

async function loadOcrTip() {
  try {
    const s = await api("/api/settings");
    $("#ocr-enabled").checked = !!(s.ocr && s.ocr.enabled);
    $("#ocr-max-pages").value = (s.ocr && s.ocr.max_pages) || 50;
    updateOcrTip();
  } catch (e) {}
}

async function saveSettings(showToast = true) {
  const body = {
    llm: {
      provider: $("#llm-provider").value,
      base_url: $("#llm-base-url").value.trim(),
      api_key: $("#llm-api-key").value,
      model: $("#llm-model").value.trim(),
    },
    embedding: {
      provider: $("#emb-provider").value,
      base_url: $("#emb-base-url").value.trim(),
      api_key: $("#emb-api-key").value,
      model: $("#emb-model").value.trim(),
    },
    rerank: {
      enabled: $("#rr-enabled").checked,
      provider: $("#rr-provider").value,
      base_url: $("#rr-base-url").value.trim(),
      api_key: $("#rr-api-key").value,
      model: $("#rr-model").value.trim(),
    },
    ocr: {
      enabled: $("#ocr-enabled").checked,
      provider: $("#ocr-provider").value,
      base_url: $("#ocr-base-url").value.trim(),
      api_key: $("#ocr-api-key").value,
      model: $("#ocr-model").value.trim(),
      dpi: parseInt($("#ocr-dpi").value) || 150,
      max_pages: parseInt($("#ocr-max-pages").value) || 50,
    },
    graph: {
      build_on_upload: $("#cfg-build-graph").checked,
      completion: $("#cfg-completion").checked,
    },
    ontology: {
      enabled: $("#onto-enabled").checked,
      entity_types: $("#onto-entity-types").value.trim(),
      relation_types: $("#onto-relation-types").value.trim(),
      filter_search: $("#onto-filter-search").checked,
    },
    retrieval: {
      mode: $("#cfg-mode").value,
      top_k: parseInt($("#cfg-topk").value) || 6,
      graph_hops: parseInt($("#cfg-hops").value) || 2,
    },
  };
  await api("/api/settings", { method: "PUT", body: JSON.stringify(body) });
  if (showToast) {
    toast("设置已保存");
    loadSettings();
    loadKbStatus();
  }
}
$("#btn-save-settings").addEventListener("click", () => saveSettings(true));

/* ================= 检索调试 ================= */
$("#btn-debug").addEventListener("click", () => {
  $("#debug-modal").classList.add("show");
  $("#debug-query").focus();
});
$("#debug-close").addEventListener("click", () => $("#debug-modal").classList.remove("show"));
$("#debug-modal").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) $("#debug-modal").classList.remove("show");
});
$("#debug-run").addEventListener("click", runDebug);
$("#debug-query").addEventListener("keydown", (e) => {
  if (e.key === "Enter") runDebug();
});
/* ESC：关闭最上层的调试弹窗 / 实体面板 */
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  const modal = $("#debug-modal");
  if (modal.classList.contains("show")) {
    modal.classList.remove("show");
    return;
  }
  $("#entity-panel").classList.remove("show");
});
async function runDebug() {
  const q = $("#debug-query").value.trim();
  if (!q) return;
  const box = $("#debug-result");
  box.innerHTML = '<div style="color:var(--muted);padding:20px;text-align:center">检索中…</div>';
  try {
    const r = await api("/api/retrieve-preview", {
      method: "POST",
      body: JSON.stringify({ query: q, mode: $("#chat-mode").value, kb_id: currentKbId, web: $("#web-search").checked }),
    });
    if (r.error) throw new Error(r.error);
    let html = "";
    if (r.graph_narrative) {
      html += `<div style="margin-bottom:10px"><span class="rp-title">图谱解读</span><div style="white-space:pre-wrap;font-size:12px;line-height:1.75;color:var(--text-2);margin-top:6px">${escapeHtml(r.graph_narrative)}</div></div>`;
    }
    if (r.notes) {
      html += `<div style="margin-bottom:10px"><span class="rp-title">笔记链接</span><div style="white-space:pre-wrap;font-size:12px;line-height:1.75;color:var(--text-2);margin-top:6px">${escapeHtml(r.notes)}</div></div>`;
    }
    if (r.graph_entities && r.graph_entities.length) {
      html += `<div style="margin-bottom:8px"><span class="rp-title">命中实体</span> ` + r.graph_entities.map((e) => `<span class="tag">${escapeHtml(e)}</span>`).join("") + `</div>`;
    }
    if (r.graph_triples && r.graph_triples.length) {
      html += `<div style="margin-bottom:10px"><span class="rp-title">图谱路径</span><br>` + r.graph_triples.map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join("") + `</div>`;
    }
    if (r.web && r.web.enabled) {
      html += r.web.error
        ? `<div style="color:var(--yellow);margin-bottom:8px">联网搜索失败：${escapeHtml(r.web.error)}</div>`
        : `<div style="color:var(--muted);margin-bottom:8px">联网搜索：${r.web.count || 0} 条网页结果</div>`;
      if (r.web.query) {
        html += `<div style="color:var(--muted);margin-bottom:8px">联网检索词：${escapeHtml(r.web.query)}</div>`;
      }
    }
    if (r.chunks && r.chunks.length) {
      html += r.chunks
        .map((c) => {
          const doc = c.url
            ? `<a class="ref-link" href="${escapeHtml(c.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(c.doc)}</a>`
            : escapeHtml(c.doc);
          return `<div class="debug-chunk">
            <div class="head"><span>[${c.n}] ${doc}${c.source === "web" ? ' <span class="ref-badge">网页</span>' : ""}</span><span class="score">score ${c.score}</span></div>
            ${escapeHtml(c.text)}
          </div>`;
        })
        .join("");
    } else {
      html += '<div class="debug-empty">未命中任何内容</div>';
    }
    box.innerHTML = html;
  } catch (e) {
    box.innerHTML = `<div class="debug-empty" style="color:var(--red)">${escapeHtml(e.message)}</div>`;
  }
}

/* ================= 状态栏 ================= */
async function loadKbStatus() {
  try {
    const kbq = currentKbId != null ? "?kb_id=" + currentKbId : "";
    const docs = await api("/api/documents" + kbq);
    const stats = await api("/api/graph/stats" + kbq);
    const ready = docs.filter((d) => d.status === "done").length;
    $("#kb-status").textContent = `知识库「${kbNameOf(currentKbId)}」：${ready}/${docs.length} 份文档就绪 · 图谱 ${stats.entities} 实体/${stats.relations} 关系` +
      (stats.notes ? ` · ${stats.notes} 篇笔记/${stats.links} 条双链` : "");
  } catch (e) {}
}

/* ================= 初始化 ================= */
/* 联网搜索开关：记住上次状态 */
const webSearchEl = $("#web-search");
webSearchEl.checked = localStorage.getItem("web_search") === "1";
webSearchEl.addEventListener("change", () => {
  localStorage.setItem("web_search", webSearchEl.checked ? "1" : "0");
});

$("#chat-input").addEventListener("input", function () {
  this.style.height = "auto";
  this.style.height = Math.min(this.scrollHeight, 160) + "px";
});
(async function boot() {
  try {
    await loadKbs();
  } catch (e) {
    toast("加载知识库失败：" + e.message, false);
  }
  loadKbStatus();
  // 会话列表 / OCR 提示不是首屏必需内容，延到主线程空闲再取，
  // 避免启动阶段与首帧渲染挤在一起形成长任务
  const idle = window.requestIdleCallback || ((fn) => setTimeout(fn, 120));
  idle(() => {
    loadChatList();
    loadOcrTip();
  });
  setInterval(loadKbStatus, 8000);
})();
