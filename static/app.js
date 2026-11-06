/* ============ MarketAI Studio 前端 ============ */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = {
  providers: [],
  products: [],
  pid: null,
  product: null,
  angles: null,          // {dimensions:[{id,label,values:[{id,label,prompt_fragment}]}], style_notes}
  selected: {},          // dimId -> Set(valId)
  plan: null,
  runs: [],
  assets: [],
  gen: { provider: null, model: null, ar: "1:1", size: "1K", variants: 1, useRef: true },
  filters: { tags: {}, provider: "", rating: "all", verdict: "all" },  // rating=质量审核 · verdict=投放判定
  tab: "runs",
  pollTimer: null,
};

/* ---------------- api ---------------- */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body instanceof FormData ? {} : { "Content-Type": "application/json" },
    ...opts,
    body: opts.body instanceof FormData ? opts.body : opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let msg = `${res.status}`;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("json") ? res.json() : res;
}

function toast(msg, isErr = false) {
  const el = document.createElement("div");
  el.className = "toast-item" + (isErr ? " err" : "");
  el.textContent = msg;
  $("#toast").appendChild(el);
  setTimeout(() => el.remove(), isErr ? 5200 : 2800);
}

/* ---------------- init ---------------- */
async function init() {
  state.providers = await api("/api/providers");
  renderProvDots();
  renderProvGrid();
  state.products = await api("/api/products");
  renderProductSelect();
  if (state.products.length) {
    await selectProduct(state.products[0].id);
  } else {
    newProductModal();
  }
  bindStatic();
  startPolling();
}

/* ---------------- header ---------------- */
function renderProvDots() {
  $("#provDots").innerHTML = state.providers
    .filter((p) => p.name !== "mock")
    .map((p) => `<span><i class="pdot ${p.available ? "on" : ""}"></i>${esc(p.label.split("·")[0].trim())}</span>`)
    .join("");
}

function renderProductSelect() {
  $("#productSelect").innerHTML =
    state.products.map((p) => `<option value="${p.id}" ${p.id === state.pid ? "selected" : ""}>${esc(p.name)}</option>`).join("") ||
    `<option value="">（暂无产品）</option>`;
}

async function selectProduct(pid) {
  state.pid = pid;
  state.product = await api(`/api/products/${pid}`);
  state.angles = await api(`/api/products/${pid}/angles`);
  state.plan = await api(`/api/products/${pid}/plan`);
  state.runs = await api(`/api/products/${pid}/runs`);
  state.assets = await api(`/api/products/${pid}/assets`);
  state.selected = {};
  if (state.angles) for (const d of state.angles.dimensions) state.selected[d.id] = new Set(d.values.map((v) => v.id));
  renderProductSelect();
  fillProductForm();
  renderRefs();
  renderDims();
  renderComboPanel();
  renderComboResult();
  renderRuns();
  renderGallery();
  updateEstimate();
}

/* ---------------- ① 产品 ---------------- */
function fillProductForm() {
  const p = state.product || {};
  $("#p_name").value = p.name || "";
  $("#p_desc").value = p.description || "";
  $("#p_points").value = (p.selling_points || []).join(", ");
  $("#p_aud").value = p.audience || "";
  $("#p_brand").value = p.brand_notes || "";
}

function productBody() {
  return {
    name: $("#p_name").value.trim(),
    description: $("#p_desc").value.trim(),
    selling_points: $("#p_points").value.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
    audience: $("#p_aud").value.trim(),
    brand_notes: $("#p_brand").value.trim(),
  };
}

async function saveProduct() {
  const body = productBody();
  if (!body.name) return toast("先填产品名称", true);
  try {
    if (state.pid && state.product) {
      state.product = await api(`/api/products/${state.pid}`, { method: "PUT", body });
    } else {
      state.product = await api("/api/products", { method: "POST", body });
      state.pid = state.product.id;
    }
    state.products = await api("/api/products");
    renderProductSelect();
    toast("产品已保存 ✓");
  } catch (e) { toast("保存失败: " + e.message, true); }
}

function renderRefs() {
  const p = state.product;
  $("#refThumbs").innerHTML = (p?.ref_images || [])
    .map((n) => `<div class="ref"><img src="/files/refs/${p.id}/${n}"><div class="x" data-ref="${n}">✕</div></div>`)
    .join("");
}

async function uploadRefs(files) {
  if (!state.pid) return toast("先保存产品", true);
  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  try {
    state.product = await api(`/api/products/${state.pid}/refs`, { method: "POST", body: fd });
    renderRefs();
    toast(`已上传 ${files.length} 张参考图 ✓`);
  } catch (e) { toast("上传失败: " + e.message, true); }
}

/* ---------------- ② 角度维度 ---------------- */
async function genAngles() {
  if (!state.pid) return toast("先保存产品", true);
  const btn = $("#btnGenAngles");
  btn.disabled = true; btn.textContent = "生成中…";
  try {
    state.angles = await api(`/api/products/${state.pid}/angles/generate`, { method: "POST" });
    state.selected = {};
    for (const d of state.angles.dimensions) state.selected[d.id] = new Set(d.values.map((v) => v.id));
    renderDims();
    renderComboPanel();
    toast(state.angles.source === "llm" ? "AI 维度已生成 ✓" : "未配置 LLM key，使用了通用模板维度");
  } catch (e) { toast("生成失败: " + e.message, true); }
  btn.disabled = false; btn.textContent = "✨ AI 生成维度";
}

function renderDims() {
  const a = state.angles;
  $("#angleSrc").innerHTML = a ? `<span class="src-badge">${{ llm: "AI 生成", template: "通用模板", manual: "手动" }[a.source] || ""}</span>` : "";
  $("#styleNotes").value = a?.style_notes || "";
  $("#dimList").innerHTML = !a ? "" : a.dimensions.map((d, di) => `
    <div class="dim-card" data-di="${di}">
      <div class="dim-head">
        <input type="text" value="${esc(d.label)}" data-role="dimlabel" data-di="${di}" style="flex:1">
        <span class="del" data-role="deldim" data-di="${di}" title="删除维度" style="cursor:pointer;color:var(--muted)">🗑</span>
      </div>
      ${d.values.map((v, vi) => `
        <div class="val-row">
          <input type="text" value="${esc(v.label)}" placeholder="取值名" data-role="vlabel" data-di="${di}" data-vi="${vi}">
          <input type="text" value="${esc(v.prompt_fragment)}" placeholder="英文 prompt 片段" data-role="vfrag" data-di="${di}" data-vi="${vi}">
          <span class="del" data-role="delval" data-di="${di}" data-vi="${vi}">✕</span>
        </div>`).join("")}
      <span class="addv" data-role="addval" data-di="${di}">＋ 添加取值</span>
    </div>`).join("");
}

function ensureAngles() {
  if (!state.angles) state.angles = { product_id: state.pid, dimensions: [], style_notes: "", source: "manual", updated_at: 0 };
  return state.angles;
}

async function saveAngles() {
  const a = ensureAngles();
  a.style_notes = $("#styleNotes").value.trim();
  a.dimensions = a.dimensions.filter((d) => d.label.trim() && d.values.filter((v) => v.label.trim()).length >= 1);
  a.dimensions.forEach((d) => { d.values = d.values.filter((v) => v.label.trim()); });
  if (!a.dimensions.length) return toast("至少要有一个维度和取值", true);
  try {
    state.angles = await api(`/api/products/${state.pid}/angles`, { method: "PUT", body: a });
    for (const d of state.angles.dimensions) if (!state.selected[d.id]) state.selected[d.id] = new Set(d.values.map((v) => v.id));
    renderDims();
    renderComboPanel();
    toast("维度已保存 ✓");
  } catch (e) { toast("保存失败: " + e.message, true); }
}

/* ---------------- ③ 组合 ---------------- */
function renderComboPanel() {
  const a = state.angles;
  $("#comboDims").innerHTML = !a ? `<p class="hint">先在第 2 步生成/保存维度</p>` : a.dimensions.map((d) => `
    <div class="combo-dim">
      <div class="cd-label">${esc(d.label)}</div>
      <div class="chips">
        ${d.values.map((v) => `<span class="chip ${state.selected[d.id]?.has(v.id) ? "on" : ""}" data-dim="${d.id}" data-val="${v.id}">${esc(v.label)}</span>`).join("")}
      </div>
    </div>`).join("");
  updateComboCount();
}

function comboMath() {
  const a = state.angles;
  if (!a) return { full: 0, out: 0 };
  let full = 1, active = 0;
  for (const d of a.dimensions) {
    const n = [...(state.selected[d.id] || [])].length;
    if (n > 0) { full *= n; active++; }
  }
  if (!active) return { full: 0, out: 0 };
  const mode = $("#planMode").value;
  const cap = parseInt($("#planCap").value || "0", 10);
  const out = mode === "coverage" && cap && cap < full ? cap : full;
  return { full, out };
}

function updateComboCount() {
  const { full, out } = comboMath();
  $("#comboCount").innerHTML = full
    ? `全组合 <b>${full}</b> 个 → 本轮将生成 <b>${out}</b> 个组合`
    : "—";
  updateEstimate();
}

async function buildPlan() {
  if (!state.angles) return toast("先完成第 2 步", true);
  const selected = {};
  for (const [k, set] of Object.entries(state.selected)) if (set.size) selected[k] = [...set];
  if (!Object.keys(selected).length) return toast("至少勾选一个维度的取值", true);
  try {
    state.plan = await api(`/api/products/${state.pid}/plan`, {
      method: "POST",
      body: { selected, mode: $("#planMode").value, cap: parseInt($("#planCap").value || "0", 10) },
    });
    renderComboResult();
    updateEstimate();
    toast(`组合方案已生成：${state.plan.combos.length} 个组合 ✓`);
  } catch (e) { toast(e.message, true); }
}

function renderComboResult() {
  const plan = state.plan;
  $("#comboList").innerHTML = !plan ? "" : plan.combos.map((c) =>
    `<div class="combo-item"><b>${c.id}</b><span>${Object.entries(c.tags).map(([k, v]) => `${esc(k)}:${esc(v)}`).join(" · ")}</span></div>`
  ).join("");
}

/* ---------------- ④ 生成 ---------------- */
function renderProvGrid() {
  const grid = $("#provGrid");
  if (!state.gen.provider) {
    const firstOk = state.providers.find((p) => p.available && p.name !== "mock") || state.providers.find((p) => p.name === "mock");
    state.gen.provider = firstOk?.name || "mock";
  }
  grid.innerHTML = state.providers.map((p) => `
    <div class="prov-card ${p.name === state.gen.provider ? "sel" : ""} ${p.available ? "" : "off"}" data-prov="${p.name}">
      <div class="pc-name"><i class="pdot ${p.available ? "on" : ""}"></i>${esc(p.label)}</div>
      <div class="pc-sub">${p.available ? `≈$${p.est_cost_per_image}/张` : "未配置 key"}</div>
    </div>`).join("");
  renderModelSelect();
}

function renderModelSelect() {
  const p = state.providers.find((x) => x.name === state.gen.provider);
  $("#modelSelect").innerHTML = (p?.models || []).map((m) => `<option ${m === (state.gen.model || p.default_model) ? "selected" : ""}>${m}</option>`).join("");
  state.gen.model = $("#modelSelect").value;
  updateEstimate();
}

function updateEstimate() {
  const combos = state.plan?.combos?.length || comboMath().out || 0;
  const variants = parseInt($("#variantSelect").value, 10);
  const p = state.providers.find((x) => x.name === state.gen.provider);
  const n = combos * variants;
  const cost = (n * (p?.est_cost_per_image || 0)).toFixed(2);
  $("#estLine").innerHTML = n ? `本次将生成 <b>${n}</b> 张 · 预估成本 <b>≈ $${cost}</b>` : "—";
}

async function startRun() {
  if (!state.plan) return toast("先在第 3 步生成组合方案", true);
  const p = state.providers.find((x) => x.name === state.gen.provider);
  if (!p?.available) return toast(`${p?.label || "该模型"} 未配置 API key，请在 .env 里填入后重启`, true);
  const body = {
    product_id: state.pid,
    provider: state.gen.provider,
    model: $("#modelSelect").value,
    aspect_ratio: state.gen.ar,
    image_size: $("#sizeSelect").value,
    variants: parseInt($("#variantSelect").value, 10),
    use_ref: $("#refSwitch").classList.contains("on"),
  };
  const btn = $("#btnGenerate");
  btn.disabled = true; btn.textContent = "启动中…";
  try {
    await api("/api/runs", { method: "POST", body });
    toast("生成任务已启动 🚀");
    switchTab("runs");
    await refreshRuns();
  } catch (e) { toast(e.message, true); }
  btn.disabled = false; btn.textContent = "🚀 开始生成";
}

/* ---------------- 任务视图 ---------------- */
async function refreshRuns() {
  if (!state.pid) return;
  state.runs = await api(`/api/products/${state.pid}/runs`);
  renderRuns();
}

function renderRuns() {
  const v = $("#view-runs");
  if (!state.runs.length) {
    v.innerHTML = `<div class="empty"><div class="big">🎬</div>还没有生成任务<br>完成左侧 1→4 步后点「开始生成」</div>`;
    return;
  }
  v.innerHTML = state.runs.map((r) => {
    const pct = r.total ? Math.round(((r.done + r.failed) / r.total) * 100) : 0;
    return `
    <div class="run-card">
      <div class="run-head">
        <span class="rh-title">${esc(r.provider)} · ${esc(r.model)}</span>
        <span class="status-pill ${r.status}">${{ queued: "排队", running: "生成中", done: "完成", cancelled: "已取消", failed: "失败" }[r.status]}</span>
        <span class="rh-meta">${r.aspect_ratio} · ${r.image_size} · ${r.use_ref ? "参考图保真" : "纯文生图"} · ${new Date(r.created_at * 1000).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}</span>
        <span class="spacer"></span>
        <span class="rh-meta">${r.done}/${r.total}${r.failed ? ` · <span style="color:var(--bad)">失败${r.failed}</span>` : ""}</span>
        ${r.status === "running" || r.status === "queued" ? `<button class="btn mini danger" data-cancel="${r.id}">停止</button>` : ""}
      </div>
      <div class="pbar"><div style="width:${pct}%"></div></div>
      <div class="thumb-grid">
        ${r.assets.map((a) => {
          if (a.status === "ok") return `<div class="thumb"><img loading="lazy" src="/files/runs/${r.product_id}/${r.id}/${a.filename}" data-lb="${a.id}"><span class="tcode">${a.combo_id}</span></div>`;
          if (a.status === "failed") return `<div class="thumb failed" title="${esc(a.error)}">✕ ${a.combo_id}<br>${esc(a.error.slice(0, 40))}</div>`;
          return `<div class="thumb pending"><span class="tcode">${a.combo_id}</span></div>`;
        }).join("")}
      </div>
    </div>`;
  }).join("");
}

function startPolling() {
  clearInterval(state.pollTimer);
  state.pollTimer = setInterval(async () => {
    const active = state.runs.some((r) => r.status === "running" || r.status === "queued");
    if (!active || !state.pid) return;
    await refreshRuns();
    state.assets = await api(`/api/products/${state.pid}/assets`);
    if (state.tab === "gallery") renderGallery();
    else updateGalCount();
  }, 2000);
}

/* ---------------- 画廊 ---------------- */
function updateGalCount() {
  const ok = state.assets.filter((a) => a.status === "ok").length;
  const badge = $("#galCount");
  badge.style.display = ok ? "" : "none";
  badge.textContent = ok;
}

function galleryFiltered() {
  return state.assets.filter((a) => {
    if (a.status !== "ok") return false;
    if (state.filters.provider && a.provider !== state.filters.provider) return false;
    if (state.filters.rating === "good" && a.quality !== "approved") return false;
    if (state.filters.rating === "nc" && a.quality !== "needs_changes") return false;
    if (state.filters.rating === "bad" && a.quality !== "rejected") return false;
    if (state.filters.rating === "none" && a.quality) return false;
    if (state.filters.verdict !== "all") {
      if (state.filters.verdict === "none" ? !!a.verdict : a.verdict !== state.filters.verdict) return false;
    }
    for (const [dim, set] of Object.entries(state.filters.tags)) {
      if (set.size && !set.has(a.tags[dim])) return false;
    }
    return true;
  });
}

function renderGallery() {
  updateGalCount();
  // 过滤条：维度取值来自实际素材的 tags（跨 run 聚合）
  const dims = {};
  for (const a of state.assets) if (a.status === "ok") for (const [k, v] of Object.entries(a.tags)) (dims[k] ??= new Set()).add(v);
  const providers = [...new Set(state.assets.filter((a) => a.status === "ok").map((a) => a.provider))];

  $("#galleryFilters").innerHTML = Object.keys(dims).length ? `
    ${Object.entries(dims).map(([dim, vals]) => `
      <div class="filter-bar"><span class="fb-label">${esc(dim)}</span>
        ${[...vals].map((v) => `<span class="chip ${state.filters.tags[dim]?.has(v) ? "on" : ""}" data-fdim="${esc(dim)}" data-fval="${esc(v)}">${esc(v)}</span>`).join("")}
      </div>`).join("")}
    <div class="filter-bar">
      <span class="fb-label">模型</span>
      <span class="chip ${!state.filters.provider ? "on" : ""}" data-fprov="">全部</span>
      ${providers.map((p) => `<span class="chip ${state.filters.provider === p ? "on" : ""}" data-fprov="${p}">${p}</span>`).join("")}
      <span class="fb-label" style="margin-left:14px">质量审核</span>
      ${[["all", "全部"], ["none", "未审"], ["good", "✅ Approved"], ["nc", "✎ Needs Changes"], ["bad", "⛔ Rejected"]].map(([k, t]) =>
        `<span class="chip ${state.filters.rating === k ? "on" : ""}" data-frate="${k}">${t}</span>`).join("")}
      <span class="fb-label" style="margin-left:14px">投放判定</span>
      ${[["all", "全部"], ["win", "🏆 Winner"], ["learn", "🎓 Learning"], ["lose", "❌ Loser"], ["none", "未投放"]].map(([k, t]) =>
        `<span class="chip ${state.filters.verdict === k ? "on" : ""}" data-fverd="${k}">${t}</span>`).join("")}
    </div>` : "";

  const list = galleryFiltered();
  const VCHIP = { win: ["🏆 投放 Winner", "var(--ok)"], learn: ["🎓 投放 Learning", "var(--warn)"], lose: ["❌ 投放 Loser", "var(--bad)"] };
  $("#galGrid").innerHTML = list.map((a) => `
    <div class="gcard ${a.quality === "approved" ? "approved" : ""} ${a.quality === "needs_changes" ? "needschanges" : ""} ${a.quality === "rejected" ? "rejected" : ""}">
      <img loading="lazy" src="${a.url}" data-lb="${a.id}">
      <div class="gmeta">
        <div class="gtags">${Object.entries(a.tags).map(([k, v]) => `<span class="gtag">${esc(v)}</span>`).join("")}${a.verdict ? `<span class="gtag" style="color:${VCHIP[a.verdict][1]};border-color:currentColor" title="投放判定：来自 Meta 阶段的数据结论，与质量审核分离">${VCHIP[a.verdict][0]}</span>` : ""}</div>
        <div class="gact">
          <span class="prov">${a.provider} · ${a.combo_id}</span>
          <span class="iconbtn ${a.quality === "approved" ? "on-good" : ""}" data-q="approved" data-aid="${a.id}" title="✅ Approved：质量过关，可进投放">✓</span>
          <span class="iconbtn ${a.quality === "needs_changes" ? "on-nc" : ""}" data-q="needs_changes" data-aid="${a.id}" title="✎ Needs Changes：有救但要改">✎</span>
          <span class="iconbtn ${a.quality === "rejected" ? "on-bad" : ""}" data-q="rejected" data-aid="${a.id}" title="⛔ Rejected：质量不合格，弃用">✕</span>
        </div>
      </div>
    </div>`).join("");
  $("#galEmpty").innerHTML = list.length ? "" : `<div class="empty"><div class="big">🖼</div>没有符合筛选条件的素材</div>`;

  const approved = state.assets.filter((a) => a.quality === "approved" && a.status === "ok").length;
  $("#exportBar").style.display = approved && state.tab === "gallery" ? "" : "none";
  $("#approvedCount").textContent = approved;
}

async function setQuality(assetId, q) {
  const a = state.assets.find((x) => x.id === assetId);
  if (!a) return;
  const newQ = a.quality === q ? null : q;   // 再点一次取消
  a.quality = newQ;
  renderGallery();
  try { await api(`/api/products/${state.pid}/assets/${assetId}/quality`, { method: "POST", body: { quality: newQ } }); }
  catch (e) { toast(e.message, true); }
}

/* ---------------- lightbox ---------------- */
function openLightbox(assetId) {
  const list = state.tab === "gallery" ? galleryFiltered() : state.assets.filter((a) => a.status === "ok");
  const idx = list.findIndex((a) => a.id === assetId);
  if (idx < 0) return;
  const draw = (i) => {
    const a = list[i];
    $("#lightboxRoot").innerHTML = `
      <div class="lightbox" data-lbroot>
        <div class="lb-close" data-lbclose>✕</div>
        <div class="lb-nav" style="left:8px" data-lbnav="-1">‹</div>
        <div class="lb-img"><img src="${a.url}"></div>
        <div class="lb-nav" style="right:338px" data-lbnav="1">›</div>
        <div class="lb-side">
          <h4>组合 ${a.combo_id}（${a.combo_code}）</h4>
          <div class="gtags">${Object.entries(a.tags).map(([k, v]) => `<span class="gtag">${esc(k)}:${esc(v)}</span>`).join("")}</div>
          <h4>模型</h4><div style="font-size:13px">${a.provider} · ${a.model}</div>
          <h4>质量审核（人 · 能不能用）</h4>
          <div class="row">
            <button class="btn ${a.quality === "approved" ? "primary" : ""}" data-lbq="approved">✅ Approved</button>
            <button class="btn ${a.quality === "needs_changes" ? "primary" : ""}" data-lbq="needs_changes">✎ Needs Changes</button>
            <button class="btn ${a.quality === "rejected" ? "danger" : ""}" data-lbq="rejected">⛔ Rejected</button>
          </div>
          ${a.verdict ? `<h4>投放判定（数据 · 好不好卖）</h4>
          <div style="font-size:13px">${{ win: "🏆 Winner", learn: "🎓 Learning", lose: "❌ Loser" }[a.verdict]}
            <span style="color:var(--muted);font-size:12px;margin-left:8px">来自 Meta 阶段回写 · 与质量审核分离</span></div>` : ""}
          <h4>Prompt</h4>
          <div class="prompt-box">${esc(a.prompt)}</div>
          <button class="btn mini" style="margin-top:8px" data-lbcopy>复制 prompt</button>
        </div>
      </div>`;
    const root = $("[data-lbroot]");
    root.addEventListener("click", async (e) => {
      const t = e.target;
      if (t.dataset.lbclose !== undefined || t === root) return close();
      if (t.dataset.lbnav) return draw((i + parseInt(t.dataset.lbnav, 10) + list.length) % list.length);
      if (t.dataset.lbq) { await setQuality(a.id, t.dataset.lbq); draw(i); }
      if (t.dataset.lbcopy !== undefined) { navigator.clipboard.writeText(a.prompt); toast("已复制"); }
    });
  };
  const close = () => { $("#lightboxRoot").innerHTML = ""; document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  draw(idx);
}

/* ---------------- modal：新建产品 ---------------- */
function newProductModal() {
  $("#modalRoot").innerHTML = `
    <div class="modal-mask">
      <div class="modal">
        <h3>新建产品</h3>
        <p class="hint">先起个名字，详细信息在左侧第 1 步里补充。</p>
        <input type="text" id="npName" placeholder="例：便携式电动车充电机" style="margin:10px 0 16px">
        <div class="row">
          <button class="btn" id="npCancel">取消</button>
          <button class="btn primary" id="npOk">创建</button>
        </div>
      </div>
    </div>`;
  $("#npName").focus();
  $("#npCancel").onclick = () => ($("#modalRoot").innerHTML = "");
  $("#npOk").onclick = async () => {
    const name = $("#npName").value.trim();
    if (!name) return toast("请输入产品名称", true);
    const p = await api("/api/products", { method: "POST", body: { name } });
    state.products = await api("/api/products");
    $("#modalRoot").innerHTML = "";
    await selectProduct(p.id);
    toast("产品已创建，请补充信息并上传参考图");
  };
  $("#npName").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#npOk").click(); });
}

/* ---------------- tab ---------------- */
function switchTab(tab) {
  state.tab = tab;
  $$(".tab").forEach((t) => t.classList.toggle("on", t.dataset.tab === tab));
  $("#view-runs").style.display = tab === "runs" ? "" : "none";
  $("#view-gallery").style.display = tab === "gallery" ? "" : "none";
  if (tab === "gallery") api(`/api/products/${state.pid}/assets`).then((a) => { state.assets = a; renderGallery(); });
  else $("#exportBar").style.display = "none";
}

/* ---------------- 事件绑定 ---------------- */
function bindStatic() {
  // 折叠面板
  $$("[data-toggle]").forEach((h) => h.addEventListener("click", () => $("#" + h.dataset.toggle).classList.toggle("open")));
  // header
  $("#productSelect").addEventListener("change", (e) => e.target.value && selectProduct(e.target.value));
  $("#btnNewProduct").addEventListener("click", newProductModal);
  // ① 产品
  $("#btnSaveProduct").addEventListener("click", saveProduct);
  $("#uploadBox").addEventListener("click", () => $("#refInput").click());
  $("#refInput").addEventListener("change", (e) => { if (e.target.files.length) uploadRefs([...e.target.files]); e.target.value = ""; });
  $("#refThumbs").addEventListener("click", async (e) => {
    const name = e.target.dataset.ref;
    if (!name) return;
    state.product = await api(`/api/products/${state.pid}/refs/${name}`, { method: "DELETE" });
    renderRefs();
  });
  // ② 维度
  $("#btnGenAngles").addEventListener("click", genAngles);
  $("#btnAddDim").addEventListener("click", () => {
    const a = ensureAngles();
    a.dimensions.push({ id: "d_" + Math.random().toString(36).slice(2, 12), label: "新维度", values: [{ id: "v_" + Math.random().toString(36).slice(2, 12), label: "", prompt_fragment: "" }] });
    renderDims();
  });
  $("#btnSaveAngles").addEventListener("click", saveAngles);
  $("#dimList").addEventListener("input", (e) => {
    const t = e.target, a = state.angles;
    if (!a) return;
    const di = +t.dataset.di, vi = +t.dataset.vi;
    if (t.dataset.role === "dimlabel") a.dimensions[di].label = t.value;
    if (t.dataset.role === "vlabel") a.dimensions[di].values[vi].label = t.value;
    if (t.dataset.role === "vfrag") a.dimensions[di].values[vi].prompt_fragment = t.value;
  });
  $("#dimList").addEventListener("click", (e) => {
    const t = e.target, a = state.angles;
    if (!a || !t.dataset.role) return;
    const di = +t.dataset.di, vi = +t.dataset.vi;
    if (t.dataset.role === "deldim") { a.dimensions.splice(di, 1); renderDims(); }
    if (t.dataset.role === "delval") { a.dimensions[di].values.splice(vi, 1); renderDims(); }
    if (t.dataset.role === "addval") { a.dimensions[di].values.push({ id: "v_" + Math.random().toString(36).slice(2, 12), label: "", prompt_fragment: "" }); renderDims(); }
  });
  // ③ 组合
  $("#comboDims").addEventListener("click", (e) => {
    const t = e.target;
    if (!t.dataset.dim) return;
    const set = state.selected[t.dataset.dim] ??= new Set();
    set.has(t.dataset.val) ? set.delete(t.dataset.val) : set.add(t.dataset.val);
    t.classList.toggle("on");
    updateComboCount();
  });
  $("#planMode").addEventListener("change", updateComboCount);
  $("#planCap").addEventListener("input", updateComboCount);
  $("#btnBuildPlan").addEventListener("click", buildPlan);
  // ④ 生成
  $("#provGrid").addEventListener("click", (e) => {
    const card = e.target.closest("[data-prov]");
    if (!card) return;
    state.gen.provider = card.dataset.prov;
    state.gen.model = null;
    renderProvGrid();
  });
  $("#modelSelect").addEventListener("change", (e) => (state.gen.model = e.target.value));
  $("#arSeg").addEventListener("click", (e) => {
    if (!e.target.dataset.ar) return;
    state.gen.ar = e.target.dataset.ar;
    $$("#arSeg button").forEach((b) => b.classList.toggle("on", b === e.target));
  });
  $("#variantSelect").addEventListener("change", updateEstimate);
  $("#refSwitch").addEventListener("click", (e) => e.currentTarget.classList.toggle("on"));
  $("#btnGenerate").addEventListener("click", startRun);
  // 右侧
  $$(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));
  $("#view-runs").addEventListener("click", (e) => {
    if (e.target.dataset.cancel) api(`/api/runs/${e.target.dataset.cancel}/cancel`, { method: "POST" }).then(refreshRuns).catch((err) => toast(err.message, true));
    if (e.target.dataset.lb) openLightbox(e.target.dataset.lb);
  });
  $("#view-gallery").addEventListener("click", (e) => {
    const t = e.target;
    if (t.dataset.lb) return openLightbox(t.dataset.lb);
    if (t.dataset.aid && t.dataset.q) return setQuality(t.dataset.aid, t.dataset.q);
    if (t.dataset.fprov !== undefined) { state.filters.provider = t.dataset.fprov; return renderGallery(); }
    if (t.dataset.frate) { state.filters.rating = t.dataset.frate; return renderGallery(); }
    if (t.dataset.fverd) { state.filters.verdict = t.dataset.fverd; return renderGallery(); }
    if (t.dataset.fdim) {
      const set = state.filters.tags[t.dataset.fdim] ??= new Set();
      set.has(t.dataset.fval) ? set.delete(t.dataset.fval) : set.add(t.dataset.fval);
      return renderGallery();
    }
  });
  $("#btnExport").addEventListener("click", () =>
    window.open(`/api/products/${state.pid}/export?scope=${$("#exportScope")?.value || "approved"}`, "_blank"));
}

init().catch((e) => toast("初始化失败: " + e.message, true));
