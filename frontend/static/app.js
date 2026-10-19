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
    try { const j = await res.json(); msg = j.detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  return res.json();
}

function toast(msg, ok = true) {
  const el = document.createElement("div");
  el.className = "toast";
  el.style.borderColor = ok ? "var(--green)" : "var(--red)";
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2600);
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

function newChat() {
  currentChatId = null;
  $("#msg-wrap").innerHTML = `
    <div class="empty" id="chat-empty">
      <div class="icon">💬</div>向知识库提问吧<br>
      <span style="font-size:12px">当前知识库：<b style="color:var(--accent)">${escapeHtml(kbNameOf(currentKbId))}</b>　回答基于该库已上传文档</span>
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
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
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
    </div>`;
  if (role === "bot") {
    wrap.dataset.msgId = msgId || "";
    const bar = document.createElement("div");
    bar.className = "vote-bar";
    bar.innerHTML = `
      <button class="vote-btn" data-vote="1" title="回答有帮助">👍 有用</button>
      <button class="vote-btn" data-vote="-1" title="回答有待改进">👎 待改进</button>
      <span class="vote-done" style="display:none">✓ 已反馈</span>`;
    wrap.querySelector(".bubble").appendChild(bar);
  }
  const contentEl = wrap.querySelector(".content");
  if (role === "user") contentEl.textContent = content;
  else contentEl.innerHTML = renderMd(content);
  $("#msg-wrap").appendChild(wrap);
  scrollChat();
  return wrap;
}

function scrollChat() {
  const box = $("#chat-messages");
  box.scrollTop = box.scrollHeight;
}

function showRefs(msgEl, refs, triples = [], entities = [], searchQuery = null) {
  const panel = msgEl.querySelector(".refs-panel");
  if (!refs || !refs.length) {
    if (!triples.length) return;
  }
  let html = "";
  if (searchQuery) {
    html += `<div style="margin-bottom:4px;color:var(--muted)">🔍 检索词（已做多轮指代消解）：<b style="color:var(--accent)">${escapeHtml(searchQuery)}</b></div>`;
  }
  if (refs && refs.length) {
    html += `<div style="margin-bottom:4px">📎 引用来源（${refs.length}）</div>`;
    html += refs
      .map(
        (r) => `<div class="ref-item"><span class="ref-n">${r.n}</span>
          <div><span class="ref-doc">${escapeHtml(r.doc)}</span>
          <span style="color:var(--accent);margin-left:6px">${r.score}</span>
          <div class="ref-text">${escapeHtml(r.text)}</div></div></div>`
      )
      .join("");
  }
  if (triples && triples.length) {
    html += `<div class="triples" style="margin-top:6px">🕸️ 图谱三元组（${triples.length}）：<br>` +
      triples.map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join("") + "</div>";
  }
  if (entities && entities.length) {
    html += `<div style="margin-top:4px">🎯 命中实体：` + entities.map((e) => `<span class="tag">${escapeHtml(e)}</span>`).join("") + `</div>`;
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
  contentEl.innerHTML = '<span class="typing"><span></span><span></span><span></span></span>';

  let acc = "";
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: currentChatId,
        query,
        mode: $("#chat-mode").value,
        kb_id: currentKbId,
      }),
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
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
          showRefs(botEl, data.refs, data.graph_triples, data.graph_entities, data.search_query);
        } else if (ev === "delta") {
          acc += data.text;
          contentEl.innerHTML = renderMd(acc);
          scrollChat();
        } else if (ev === "error") {
          acc += (acc ? "\n\n⚠️ " : "⚠️ ") + (data.message || "生成失败");
          contentEl.innerHTML = renderMd(acc);
        } else if (ev === "end") {
          if (data.chat_id && data.chat_id !== currentChatId) {
            currentChatId = data.chat_id;
            loadChatList();
          }
          if (data.message_id) botEl.dataset.msgId = data.message_id;
        }
      }
    }
    if (!acc) contentEl.innerHTML = '<span style="color:var(--muted)">（无输出，请检查设置中的模型配置）</span>';
  } catch (e) {
    contentEl.innerHTML = `<span style="color:var(--red)">请求失败：${escapeHtml(e.message)}。请确认模型设置正确。</span>`;
  } finally {
    sending = false;
    $("#btn-send").disabled = false;
    input.focus();
  }
}

/* 历史会话 */
$("#msg-wrap").addEventListener("click", async (e) => {
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
      await api("/api/chats/" + c.id, { method: "DELETE" });
      if (currentChatId === c.id) $("#btn-new-chat").click();
      else loadChatList();
    });
    box.appendChild(el);
  }
}

async function openChat(chatId, kbId) {
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
  }
  loadChatList();
  scrollChat();
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
      done: '<span class="badge done">已完成</span>',
      processing: `<span class="badge processing">处理中·${escapeHtml(d.stage || "")}</span>`,
      pending: '<span class="badge pending">等待中</span>',
      error: `<span class="badge error">失败</span>`,
    };
    tr.innerHTML = `
      <td class="doc-name" title="${escapeHtml(d.filename)}">${escapeHtml(d.filename)}</td>
      <td>${fmtSize(d.size)}</td>
      <td>${statusMap[d.status] || d.status}${d.error ? `<div class="err-tip" title="${escapeHtml(d.error)}">${escapeHtml(d.error)}</div>` : ""}</td>
      <td>${d.chunk_count || "-"}</td>
      <td>${d.triple_count || "-"}</td>
      <td style="font-size:11px;color:var(--muted)">${escapeHtml((d.embed_tag || "").split("::")[1] || "-")}</td>
      <td style="white-space:nowrap">
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

tbodyEvents();
function tbodyEvents() {
  $("#docs-tbody").addEventListener("click", async (e) => {
    const btn = e.target.closest("button");
    if (!btn || btn.disabled) return;
    const id = btn.dataset.id;
    if (btn.dataset.act === "del") {
      if (!confirm("删除该文档及其全部分块与图谱关系？")) return;
      await api("/api/documents/" + id, { method: "DELETE" });
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
      await api(`/api/documents/${id}/graph`, { method: "POST" });
      toast("已开始构建知识图谱（后台运行）");
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
    let msg = `已上传 ${(j.doc_ids || []).length} 个文件到「${kbNameOf(currentKbId)}」，后台处理中…`;
    if (skipped.length) msg += `；跳过 ${skipped.length} 个重复文档（内容已存在）`;
    toast(msg);
    loadDocs();
    loadKbs();
  } catch (e) {
    toast(e.message, false);
  }
}

/* ================= 知识图谱 ================= */
let graphInited = false;

async function initGraph() {
  if (!graphChart) {
    graphChart = echarts.init($("#graph-chart"));
    graphChart.on("click", (params) => {
      if (params.dataType === "node") openEntity(params.data.id);
    });
    window.addEventListener("resize", () => graphChart && graphChart.resize());
  }
  graphChart.resize();
  await refreshGraph();
}

async function refreshGraph(q = "") {
  const kbq = currentKbId != null ? "&kb_id=" + currentKbId : "";
  const [stats, data] = await Promise.all([
    api("/api/graph/stats" + (currentKbId != null ? "?kb_id=" + currentKbId : "")),
    api("/api/graph?limit=300" + (q ? "&q=" + encodeURIComponent(q) : "") + kbq),
  ]);
  $("#graph-stats").innerHTML = `${kbNameOf(currentKbId)} · 实体 ${stats.entities} · 关系 ${stats.relations}` + (q ? ` · 筛选"${escapeHtml(q)}"` : "");
  if (!data.nodes.length) {
    graphChart.clear();
    graphChart.setOption({
      title: { text: `知识库「${kbNameOf(currentKbId)}」暂无图谱数据：请先在「文档库」上传文档（会自动构建），或点击「建图谱」`, left: "center", top: "middle", textStyle: { color: "#8b949e", fontSize: 13 } },
    });
    return;
  }
  graphChart.setOption(
    {
      tooltip: {
        formatter: (p) => {
          if (p.dataType === "node") {
            return `${p.name}（度数 ${p.value || 0}）` + (p.data && p.data.desc ? `<br/>📖 ${p.data.desc}` : "");
          }
          return `${p.data.source} —${p.data.value || ""}→ ${p.data.target}`;
        },
      },
      legend: [{ data: data.categories.map((c) => c.name), textStyle: { color: "#8b949e" }, type: "scroll", right: 10, bottom: 10 }],
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
          label: { show: true, position: "right", color: "#e6edf3", fontSize: 11 },
          labelLayout: { hideOverlap: true },
          lineStyle: { color: "source", curveness: 0.15, opacity: 0.6 },
          emphasis: { focus: "adjacency", lineStyle: { width: 3 } },
          itemStyle: { borderColor: "#0d1117", borderWidth: 1 },
        },
      ],
      color: ["#4f8cff", "#22c55e", "#facc15", "#f472b6", "#a78bfa", "#fb923c", "#2dd4bf", "#e879f9"],
    },
    true
  );
}

$("#graph-refresh").addEventListener("click", () => refreshGraph($("#graph-search").value.trim()));
$("#graph-search").addEventListener("keydown", (e) => {
  if (e.key === "Enter") refreshGraph(e.target.value.trim());
});

async function openEntity(entityId) {
  const d = await api("/api/graph/entity/" + entityId);
  $("#entity-name").textContent = d.entity.name;
  $("#entity-type").textContent = `${d.entity.type || "未分类"} · 提及 ${d.entity.mention_count} 次`;
  const descEl = $("#entity-desc");
  if (d.entity.description) {
    descEl.textContent = "📖 " + d.entity.description;
    descEl.style.display = "block";
  } else {
    descEl.style.display = "none";
  }
  const aliasEl = $("#entity-aliases");
  if (d.aliases && d.aliases.length) {
    aliasEl.innerHTML = "🔁 别名：" + d.aliases.map((a) => `<span class="tag">${escapeHtml(a)}</span>`).join("");
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
  if (clearKey && p.base_url) $("#" + pfx + "-base-url").value = p.base_url;
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
  if (defModel && clearKey) modelField.value = defModel;

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
    note.innerHTML = "ℹ️ " + (p.note || "");
    if (p.key_url) {
      note.innerHTML += `　<a href="${p.key_url}" target="_blank" style="color:var(--accent)">获取 API Key ↗</a>`;
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
    el.textContent = (r.ok ? "✅ " : "❌ ") + r.detail;
    el.className = "test-result " + (r.ok ? "ok" : "fail");
  } catch (e) {
    el.textContent = "❌ " + e.message;
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
async function runDebug() {
  const q = $("#debug-query").value.trim();
  if (!q) return;
  const box = $("#debug-result");
  box.innerHTML = '<div style="color:var(--muted);padding:20px;text-align:center">检索中…</div>';
  try {
    const r = await api("/api/retrieve-preview", {
      method: "POST",
      body: JSON.stringify({ query: q, mode: $("#chat-mode").value }),
    });
    if (r.error) throw new Error(r.error);
    let html = "";
    if (r.graph_entities && r.graph_entities.length) {
      html += `<div style="margin-bottom:8px">🎯 命中实体：` + r.graph_entities.map((e) => `<span class="tag">${escapeHtml(e)}</span>`).join("") + `</div>`;
    }
    if (r.graph_triples && r.graph_triples.length) {
      html += `<div style="margin-bottom:10px">🕸️ 图谱路径：<br>` + r.graph_triples.map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join("") + `</div>`;
    }
    if (r.chunks && r.chunks.length) {
      html += r.chunks
        .map(
          (c) => `<div class="debug-chunk">
            <div class="head"><span>[${c.n}] ${escapeHtml(c.doc)}</span><span class="score">score ${c.score}</span></div>
            ${escapeHtml(c.text)}
          </div>`
        )
        .join("");
    } else {
      html += '<div style="color:var(--muted);padding:16px;text-align:center">未命中任何内容</div>';
    }
    box.innerHTML = html;
  } catch (e) {
    box.innerHTML = `<div style="color:var(--red);padding:16px">${escapeHtml(e.message)}</div>`;
  }
}

/* ================= 状态栏 ================= */
async function loadKbStatus() {
  try {
    const kbq = currentKbId != null ? "?kb_id=" + currentKbId : "";
    const docs = await api("/api/documents" + kbq);
    const stats = await api("/api/graph/stats" + kbq);
    const ready = docs.filter((d) => d.status === "done").length;
    $("#kb-status").textContent = `知识库「${kbNameOf(currentKbId)}」：${ready}/${docs.length} 份文档就绪 · 图谱 ${stats.entities} 实体/${stats.relations} 关系`;
  } catch (e) {}
}

/* ================= 初始化 ================= */
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
  loadChatList();
  loadKbStatus();
  loadOcrTip();
  setInterval(loadKbStatus, 8000);
})();
