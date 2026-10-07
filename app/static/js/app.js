/* OpenAPI Contract Evolution Workbench — vanilla JS frontend */
"use strict";

const state = {
  report: null,
  analysisId: null,
  files: { baseline: null, candidate: null },
  view: "endpoints",
  severities: new Set(["breaking", "undetermined"]),
  text: "",
  findingsById: new Map(),
  coverageSide: "baseline",
  coverageDocs: { baseline: null, candidate: null },
  coverageFile: null,
  gapDocs: { baseline: null, candidate: null },
  migrationDoc: null,
  migrationFile: null,
  releaseDoc: null,
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const sevLabel = { breaking: "破坏性", undetermined: "无法判定", compatible: "兼容" };
const sevClass = {
  breaking: "sev-breaking", undetermined: "sev-undetermined",
  compatible: "sev-compatible",
};
const dotClass = {
  breaking: "dot-breaking", undetermined: "dot-undetermined",
  compatible: "dot-compatible",
};

// ------------------------------------------------------------- init
window.addEventListener("DOMContentLoaded", () => {
  bindUpload("baseline");
  bindUpload("candidate");
  $("#btn-analyze").addEventListener("click", runAnalysis);
  $$('input[type=checkbox][value]').forEach((cb) => {
    cb.addEventListener("change", () => {
      state.severities = new Set(
        $$('input[type=checkbox][value]').filter((x) => x.checked)
          .map((x) => x.value));
      render();
    });
  });
  $("#filter-text").addEventListener("input", (e) => {
    state.text = e.target.value.trim().toLowerCase();
    render();
  });
  $$(".tab").forEach((b) => b.addEventListener("click", () => {
    $$(".tab").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    state.view = b.dataset.view;
    render();
  }));
  $$(".cov-side").forEach((b) => b.addEventListener("click", () => {
    $$(".cov-side").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    state.coverageSide = b.dataset.side;
    renderCoverage();
  }));
  $("#coverage-file").addEventListener("change", (e) => {
    state.coverageFile = e.target.files[0] || null;
    $("#btn-coverage-upload").disabled = !state.coverageFile;
  });
  $("#btn-coverage-upload").addEventListener("click", uploadCoverage);
  $("#btn-coverage-export").addEventListener("click", exportCoverage);
  $("#btn-gap-samples").addEventListener("click", downloadGapSamples);
  $("#btn-coverage-delete").addEventListener("click", removeCoverage);
  $("#migration-file").addEventListener("change", (e) => {
    state.migrationFile = e.target.files[0] || null;
    $("#btn-migration-upload").disabled = !state.migrationFile;
  });
  $("#btn-migration-upload").addEventListener("click", uploadMigration);
  $("#btn-migration-export").addEventListener("click", exportMigration);
  $("#btn-migration-delete").addEventListener("click", removeMigration);
  $$("[data-export]").forEach((b) =>
    b.addEventListener("click", () => exportReport(b.dataset.export)));
  $("#btn-history").addEventListener("click", openHistory);
  $("#history-close").addEventListener("click", closeHistory);
  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#drawer-backdrop").addEventListener("click", closeDrawer);
  refreshHealth();
});

// ------------------------------------------------------------- upload
function bindUpload(side) {
  const input = $(`#file-${side}`);
  const card = $(`#drop-${side}`);
  input.addEventListener("change", () => {
    if (input.files[0]) setFile(side, input.files[0]);
  });
  ["dragenter", "dragover"].forEach((ev) =>
    card.addEventListener(ev, (e) => {
      e.preventDefault();
      card.classList.add("dragover");
    }));
  ["dragleave", "drop"].forEach((ev) =>
    card.addEventListener(ev, (e) => {
      e.preventDefault();
      card.classList.remove("dragover");
    }));
  card.addEventListener("drop", (e) => {
    const f = e.dataTransfer.files[0];
    if (f) {
      input.files = e.dataTransfer.files;
      setFile(side, f);
    }
  });
}

function setFile(side, file) {
  state.files[side] = file;
  const meta = $(`#meta-${side}`);
  meta.hidden = false;
  const sizeKb = (file.size / 1024).toFixed(1);
  meta.innerHTML = `已选择 <b>${escapeHtml(file.name)}</b> · ${sizeKb} KB`;
  $("#btn-analyze").disabled = !(state.files.baseline && state.files.candidate);
  $("#upload-error").textContent = "";
}

async function runAnalysis() {
  const btn = $("#btn-analyze");
  btn.disabled = true;
  btn.textContent = "比对中…";
  $("#upload-error").textContent = "";
  try {
    const fd = new FormData();
    fd.append("baseline", state.files.baseline);
    fd.append("candidate", state.files.candidate);
    const resp = await fetch("/api/analyses", { method: "POST", body: fd });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    const doc = await resp.json();
    loadReport(doc);
  } catch (exc) {
    $("#upload-error").textContent = "导入/比对失败：" + exc.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "开始比对";
  }
}

function loadReport(doc) {
  state.report = doc;
  state.analysisId = doc.analysis_id;
  state.findingsById = new Map(doc.findings.map((f) => [f.id, f]));
  state.coverageDocs = { baseline: null, candidate: null };
  state.gapDocs = { baseline: null, candidate: null };
  state.migrationDoc = null;
  state.releaseDoc = null;
  $("#panel-result").hidden = false;
  $("#analysis-id").textContent = "#" + doc.analysis_id;
  $("#fingerprint").textContent = doc.fingerprint || "";
  render();
  loadCoverageSummaries();
  $("#panel-result").scrollIntoView({ behavior: "smooth", block: "start" });
}

// ------------------------------------------------------------- render
function render() {
  const doc = state.report;
  if (!doc) return;
  const s = doc.summary;
  $("#stat-breaking").querySelector(".stat-num").textContent = s.breaking;
  $("#stat-undetermined").querySelector(".stat-num").textContent =
    s.undetermined;
  $("#stat-compatible").querySelector(".stat-num").textContent =
    s.compatible;
  $("#stat-engine").querySelector(".stat-num").textContent =
    s.engine_stats.cycles_terminated || 0;

  const findings = filteredFindings();
  const panels = {
    endpoints: $("#list-endpoints"),
    findings: $("#list-findings"),
    release: $("#list-release"),
    coverage: $("#list-coverage"),
    migration: $("#list-migration"),
  };
  for (const [name, el] of Object.entries(panels)) {
    el.hidden = name !== state.view;
  }
  if (state.view === "endpoints") {
    renderEndpoints(findings);
  } else if (state.view === "findings") {
    renderFlat(findings);
  } else if (state.view === "release") {
    renderRelease();
  } else if (state.view === "coverage") {
    renderCoverage();
  } else if (state.view === "migration") {
    renderMigration();
  }
}

function filteredFindings() {
  return state.report.findings.filter((f) => {
    if (!state.severities.has(f.severity)) return false;
    if (!state.text) return true;
    const hay = [f.rule, f.message, f.endpoint.path, f.endpoint.method,
      f.media_type || "", f.status_code || "",
      f.pointer.baseline || "", f.pointer.candidate || ""]
      .join(" ").toLowerCase();
    return hay.includes(state.text);
  });
}

function renderEndpoints(findings) {
  const box = $("#list-endpoints");
  $("#list-findings").hidden = true;
  box.hidden = false;
  const byEp = new Map();
  for (const f of findings) {
    const key = `${f.endpoint.method} ${f.endpoint.path}`;
    if (!byEp.has(key)) byEp.set(key, []);
    byEp.get(key).push(f);
  }
  // severity ordering
  const rank = { breaking: 0, undetermined: 1, compatible: 2 };
  const epMeta = new Map(state.report.endpoints.map(
    (e) => [`${e.method} ${e.path}`, e]));
  const keys = Array.from(byEp.keys()).sort((a, b) => {
    const fa = byEp.get(a)[0], fb = byEp.get(b)[0];
    const sa = fa.severity, sb = fb.severity;
    if (rank[sa] !== rank[sb]) return rank[sa] - rank[sb];
    return a.localeCompare(b);
  });
  box.innerHTML = "";
  if (!keys.length) {
    box.innerHTML = `<p class="hint">当前筛选条件下没有结果。</p>`;
    return;
  }
  for (const key of keys) {
    const [method, ...rest] = key.split(" ");
    const path = rest.join(" ");
    const fs = byEp.get(key);
    const meta = epMeta.get(key) || {};
    const worst = fs.some((f) => f.severity === "breaking") ? "breaking"
      : fs.some((f) => f.severity === "undetermined") ? "undetermined"
        : "compatible";
    const row = document.createElement("div");
    row.className = "ep-row";
    row.innerHTML = `
      <div class="ep-main">
        <span class="ep-chevron">▶</span>
        <span class="method ${methodClass(method)}">${method}</span>
        <span class="ep-path">${escapeHtml(path)}</span>
        ${meta.operation_id ? `<span class="ep-opid">${escapeHtml(meta.operation_id)}</span>` : ""}
        <span class="count-pill ${sevClass[worst]}">${fs.length}</span>
      </div>
      <div class="ep-findings"></div>`;
    const body = row.querySelector(".ep-findings");
    for (const f of fs) body.appendChild(findingRow(f));
    row.querySelector(".ep-main").addEventListener("click", () =>
      row.classList.toggle("open"));
    box.appendChild(row);
  }
}

function renderFlat(findings) {
  const box = $("#list-findings");
  $("#list-endpoints").hidden = true;
  box.hidden = false;
  box.innerHTML = "";
  if (!findings.length) {
    box.innerHTML = `<p class="hint">当前筛选条件下没有结果。</p>`;
    return;
  }
  for (const f of findings) {
    const row = document.createElement("div");
    row.className = "finding-row";
    const loc = f.pointer.candidate || f.pointer.baseline || "—";
    row.innerHTML = `
      <span><span class="sev-dot ${dotClass[f.severity]}"></span>${sevLabel[f.severity]}</span>
      <span class="flat-endpoint"><span class="method ${methodClass(f.endpoint.method)}">${f.endpoint.method}</span> ${escapeHtml(f.endpoint.path)}</span>
      <span class="finding-msg">${escapeHtml(f.message)}</span>
      <span class="flat-loc" title="${escapeHtml(loc)}">${escapeHtml(loc)}</span>`;
    row.addEventListener("click", () => openDrawer(f.id));
    box.appendChild(row);
  }
}

function findingRow(f) {
  const row = document.createElement("div");
  row.className = "finding-row";
  const loc = f.media_type
    ? `${f.media_type}${f.status_code ? " · " + f.status_code : ""}`
    : (f.location || "");
  row.innerHTML = `
    <span><span class="sev-dot ${dotClass[f.severity]}"></span>${sevLabel[f.severity]}</span>
    <span class="finding-msg" title="${escapeHtml(f.rule)}">${escapeHtml(f.message)}</span>
    <span class="finding-loc" title="${escapeHtml(f.pointer.candidate || f.pointer.baseline || "")}">${escapeHtml(loc)}</span>`;
  row.addEventListener("click", () => openDrawer(f.id));
  return row;
}

function methodClass(m) {
  return ({ GET: "m-get", POST: "m-post", PUT: "m-put",
    PATCH: "m-patch", DELETE: "m-delete" })[m] || "m-other";
}

// ------------------------------------------------------------- drawer
async function openDrawer(id) {
  const f = state.findingsById.get(id);
  if (!f) return;
  const w = f.witness || {};
  const isEndpoint = w.kind === "endpoint";
  const isStructural = ["parameter", "body", "response", "media"]
    .includes(w.kind);
  const sc = w.self_check || {};
  // schema 级三态自检：baseline_state/candidate_state 为
  // pass/fail/pending（旧报告仅含布尔字段时按布尔回退）。
  const scState = (key, boolKey) => sc[key]
    || (sc[boolKey] === true ? "pass"
      : sc[boolKey] === false ? "fail" : null);
  const scPending = isStructural === false && isEndpoint === false
    && (scState("baseline_state", "baseline_valid") === "pending"
        || scState("candidate_state", "candidate_valid") === "pending");
  $("#drawer-title").textContent = f.rule_title + " (" + f.rule + ")";
  const body = $("#drawer-body");
  const witnessHeading = isEndpoint
    ? "最小 JSON 请求反例（端点级，可编辑后重放）"
    : isStructural
      ? "结构化反例（可编辑后按 finding 语义重放）"
      : "最小 JSON 反例（仅单侧校验通过）";
  body.innerHTML = `
    <div class="detail-section">
      <span class="count-pill ${sevClass[f.severity]}">${sevLabel[f.severity]}</span>
      ${isEndpoint && w.verifiable === false
        ? `<span class="count-pill sev-undetermined" style="margin-left:8px">反例不可验证</span>`
        : ""}
      ${scPending
        ? `<span class="count-pill sev-undetermined" style="margin-left:8px">反例待自检重放确认</span>`
        : ""}
    </div>
    <div class="detail-section">
      <h4>命中位置</h4>
      <dl class="kv">
        <dt>端点</dt><dd><span class="method ${methodClass(f.endpoint.method)}">${f.endpoint.method}</span> ${escapeHtml(f.endpoint.path)}</dd>
        <dt>位置</dt><dd>${escapeHtml(locationText(f))}</dd>
        <dt>JSON Pointer（基线）</dt><dd class="pointer-link">${escapeHtml(formatPtr(f, "baseline"))}</dd>
        <dt>JSON Pointer（候选）</dt><dd class="pointer-link">${escapeHtml(formatPtr(f, "candidate"))}</dd>
        <dt>关键字</dt><dd><code>${escapeHtml(f.facet || "—")}</code></dd>
      </dl>
    </div>
    <div class="detail-section">
      <h4>规则解释</h4>
      <div class="rule-explain"><b>${escapeHtml(f.message)}</b><br><br>${escapeHtml(f.rule_detail)}</div>
    </div>
    <div class="detail-section">
      <h4>${witnessHeading}</h4>
      ${isEndpoint
        ? `<p class="hint">字段：<code>method</code> / <code>path</code>（已代入具体路径参数值）/
             <code>query</code> / <code>header</code> / <code>cookie</code>（必填参数）/
             <code>body</code>（存在必填请求体时给出最小 JSON）。编辑后点重放，将按基线契约重新核对。</p>`
        : isStructural
          ? `<p class="hint">${structuralHint(w)}</p>`
          : ""}
      <textarea id="witness-instance" class="replay-box" style="width:100%;min-height:170px"></textarea>
      <div id="replay-output"></div>
      <button class="btn btn-primary btn-sm btn-replay" id="btn-replay">
        ▶ ${isEndpoint || isStructural ? "按 finding 语义重放（双侧）" : "重放校验（双侧）"}</button>
    </div>`;
  const ta = $("#witness-instance");
  ta.value = JSON.stringify(w.instance ?? null, null, 2);
  if (isEndpoint && w.verifiable === false && w.unverifiable_reason) {
    const warn = document.createElement("p");
    warn.className = "replay-warn";
    warn.textContent = "⚠ 反例不可验证：" + w.unverifiable_reason
      + "。破坏性结论保留，但不会宣称单侧校验通过；可手动构造请求后重放。";
    ta.parentElement.insertBefore(warn, $("#replay-output"));
  }
  if (w.note) {
    const note = document.createElement("p");
    note.className = "hint";
    note.style.marginTop = "6px";
    note.textContent = w.note;
    ta.parentElement.insertBefore(note, $("#replay-output"));
  }
  // 生成时自检的待定原因（含 not / pattern 等关键字的模式位置）
  const scReasons = sc.pending_reasons || [];
  if (scPending && scReasons.length) {
    const pending = document.createElement("div");
    pending.className = "replay-result replay-pending";
    pending.innerHTML = `？ 自检存在<b>待定</b>结果，未标记为已验证的单侧通过：
      <ul class="replay-errors replay-pending-reasons">${scReasons
        .slice(0, 8).map((e) => `<li>${escapeHtml(e)}</li>`).join("")}</ul>`;
    ta.parentElement.insertBefore(pending, $("#replay-output"));
  }
  $("#btn-replay").addEventListener("click", () => replayFinding(f.id));
  $("#drawer").hidden = false;
  $("#drawer-backdrop").hidden = false;
  // auto replay once
  replayFinding(f.id);
}

function structuralHint(w) {
  if (w.kind === "parameter") {
    return `字段：<code>method</code> / <code>path</code> / <code>parameter</code> /
      <code>in</code>（query/header/cookie/path）/ <code>value</code>。
      重放会分别核对基线是否声明（或作为可选项接受）该参数、候选是否缺失或把它收紧为必填。`;
  }
  if (w.kind === "body") {
    return `字段：<code>method</code> / <code>path</code> / <code>media_type</code> /
      <code>body</code>（最小 JSON 请求体）。重放会核对基线 requestBody 的声明/必填与
      载荷 schema，以及候选是否删除 requestBody 或将其收紧为必填。`;
  }
  if (w.kind === "response") {
    return `字段：<code>method</code> / <code>path</code> / <code>status_code</code> /
      <code>media_type</code> / <code>body</code>（该状态码下的最小响应体，可空）。
      重放会核对基线是否声明该状态码（并在有 schema 时校验响应体）、候选是否已删除该状态码。`;
  }
  if (w.kind === "media") {
    return `字段：<code>method</code> / <code>path</code> / <code>location</code> /
      <code>status_code</code>（响应媒体类型时）/ <code>media_type</code> /
      <code>body</code>。重放会核对基线是否声明并接受/返回该媒体类型、候选是否已删除。`;
  }
  return "结构化反例，可编辑后重放。";
}

function closeDrawer() {
  $("#drawer").hidden = true;
  $("#drawer-backdrop").hidden = true;
}

async function replayFinding(id) {
  const out = $("#replay-output");
  out.innerHTML = `<p class="hint">重放中…</p>`;
  let instance = null;
  try {
    instance = JSON.parse($("#witness-instance").value);
  } catch (exc) {
    out.innerHTML = `<div class="replay-result replay-bad">JSON 解析失败：${escapeHtml(exc.message)}</div>`;
    return;
  }
  try {
    const resp = await fetch(
      `/api/analyses/${state.analysisId}/findings/${id}/replay`,
      { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ instance }) });
    if (!resp.ok) {
      const e = await resp.json().catch(() => ({}));
      throw new Error(e.detail || `HTTP ${resp.status}`);
    }
    const r = await resp.json();
    renderReplay(r, out);
  } catch (exc) {
    out.innerHTML = `<div class="replay-result replay-bad">重放失败：${escapeHtml(exc.message)}</div>`;
  }
}

function renderReplay(r, out) {
  if (r.ok === false) {
    out.innerHTML = `<div class="replay-result replay-bad">重放失败：${escapeHtml(r.error || "未知错误")}</div>`;
    return;
  }
  if (r.kind === "endpoint") {
    renderEndpointReplay(r, out);
    return;
  }
  if (r.kind === "structural") {
    renderStructuralReplay(r, out);
    return;
  }
  const rb = r.root_validation.baseline;
  const rc = r.root_validation.candidate;
  const exact = r.exactly_one_side;
  // 三态：valid 为 true/false 或 null（待定：仍依赖 not/pattern 等未支持
  // 关键字、循环或不可解析节点）；旧报告重放后也会带 state。
  const sideState = (side) => side.state
    || (side.valid === true ? "pass"
      : side.valid === false ? "fail" : "pending");
  const stateMeta = {
    pass: { icon: "✓", text: "通过", cls: "replay-ok" },
    fail: { icon: "✗", text: "失败", cls: "replay-bad" },
    pending: { icon: "？", text: "待定（无法判定）", cls: "replay-pending" },
  };
  const pendingList = (side) => (side.pending && side.pending.length
    ? `<ul class="replay-errors replay-pending-reasons">${side.pending
        .slice(0, 8).map((e) => `<li>${escapeHtml(e)}</li>`).join("")}</ul>`
    : "");
  const card = (name, side) => {
    const st = sideState(side);
    const meta = stateMeta[st];
    return `<div class="replay-result ${meta.cls}">
      ${meta.icon} ${name}校验：<b>${meta.text}</b>
      ${st === "fail" && side.errors && side.errors.length
        ? `<ul class="replay-errors">${side.errors.slice(0, 5)
            .map((e) => `<li>${escapeHtml(e)}</li>`).join("")}</ul>` : ""}
      ${st === "pending" ? pendingList(side) : ""}
    </div>`;
  };
  const pendingReasons = r.pending_reasons || [];
  let verdict;
  if (exact) {
    verdict = `<div class="replay-result replay-ok">
      ✓ 该反例恰好只被一侧接受（可用于演示破坏性）</div>`;
  } else if (pendingReasons.length
             || sideState(rb) === "pending" || sideState(rc) === "pending") {
    verdict = `<div class="replay-result replay-pending">
      ？ 反例存在<b>待定</b>校验结果，<b>不能宣称已验证的单侧通过</b>：
      某一侧能否通过仍取决于下列未决因素（破坏性结论保留；
      可手动编辑实例消除未决因素后再次重放）
      <ul class="replay-errors replay-pending-reasons">${pendingReasons
        .slice(0, 8).map((e) => `<li>${escapeHtml(e)}</li>`).join("")}</ul>
    </div>`;
  } else {
    verdict = `<div class="replay-result replay-bad">
      ⚠ 该反例并非恰好单侧通过（结构复杂时为近似示例，可手动编辑后重放）</div>`;
  }
  let facet = "";
  if (r.facet_validation) {
    const fv = r.facet_validation;
    facet = `<h4 style="margin:14px 0 6px;font-size:12px;color:var(--muted)">
      关键字路径 <code>${escapeHtml(fv.path)}</code> 处局部校验</h4>`;
    facet += ["baseline", "candidate"].map((k) => fv[k]
      ? card(k === "baseline" ? "基线" : "候选", fv[k])
      : "").join("");
  }
  out.innerHTML = `
    ${verdict}
    ${card("基线", rb)}
    ${card("候选", rc)}
    ${facet}`;
}

function renderEndpointReplay(r, out) {
  const ec = r.endpoint_check || {};
  const bc = r.baseline_check || {};
  const existenceCard = (name, exists, detail) => `
    <div class="replay-result ${exists ? "replay-ok" : "replay-bad"}">
      ${exists ? "✓" : "✗"} ${name}端点：<b>${exists ? "存在" : "不存在"}</b>
      <div class="replay-sub">${escapeHtml(detail || "")}</div>
    </div>`;
  const errorsList = (errors) => (errors && errors.length
    ? `<ul class="replay-errors">${errors.slice(0, 8)
        .map((e) => `<li>${escapeHtml(e)}</li>`).join("")}</ul>` : "");
  const caveats = (bc.caveats && bc.caveats.length)
    ? `<ul class="replay-errors replay-caveats">${bc.caveats
        .map((e) => `<li>${escapeHtml(e)}</li>`).join("")}</ul>` : "";
  const verdict = r.verifiable
    ? `<div class="replay-result replay-ok">
         ✓ 反例可验证：<b>基线接受该请求，候选无此端点</b>（端点删除的破坏性成立）
       </div>`
    : `<div class="replay-result replay-bad">
         ⚠ 反例不可验证：<b>${escapeHtml(r.unverifiable_reason
           || "未能证明基线接受 / 候选缺失，不宣称单侧通过")}</b><br>
         端点删除的破坏性结论仍然保留；可在编辑器中修正请求后再次重放。
       </div>`;
  out.innerHTML = `
    ${verdict}
    ${existenceCard("基线", ec.baseline, ec.baseline_detail)}
    ${existenceCard("候选", ec.candidate, ec.candidate_detail)}
    <div class="replay-result ${bc.valid ? "replay-ok" : "replay-bad"}">
      ${bc.valid ? "✓" : "✗"} 基线请求约束核对：<b>${bc.valid ? "请求满足基线契约" : "请求违反基线契约"}</b>
      ${errorsList(bc.errors)}
      ${caveats}
    </div>`;
  // if the server rebuilt a missing request from the baseline, reflect it
  // back into the editor so the user can inspect/edit the concrete payload
  const ta = $("#witness-instance");
  if (ta && r.instance && JSON.stringify(r.instance) !== ta.value.trim()
      && (ta.value.trim() === "null" || ta.value.trim() === "")) {
    ta.value = JSON.stringify(r.instance, null, 2);
  }
}

function structuralVerdictText(r) {
  switch (r.rule) {
    case "PARAMETER_MADE_REQUIRED":
      return "基线允许省略该参数 / requestBody，候选将其收紧为必填";
    case "PARAMETER_ADDED_REQUIRED":
      return "基线不要求该参数 / requestBody，候选新增了必填要求";
    case "PARAMETER_REMOVED":
      return r.location === "requestBody"
        ? "基线声明并接受该请求体，候选已删除 requestBody"
        : "基线声明并接受该参数，候选已删除该参数";
    case "STATUS_REMOVED":
      return "基线声明了该响应状态码，候选已删除该状态码";
    case "MEDIA_REMOVED":
      return r.direction === "response"
        ? "基线声明并返回该媒体类型，候选已删除该媒体类型"
        : "基线声明并接受该媒体类型，候选已删除该媒体类型";
    default:
      return "按 finding 语义双侧核对完成";
  }
}

function renderStructuralReplay(r, out) {
  const sc = r.structural_check || {};
  const checkCard = (name, chk) => {
    if (!chk) return "";
    const rows = [];
    if (typeof chk.declared !== "undefined") {
      rows.push(["声明情况", chk.declared ? "已声明" : "未声明"]);
    }
    if (typeof chk.required !== "undefined") {
      rows.push(["required", chk.required ? "必填（true）" : "可选（false）"]);
    }
    if (chk.media_type) rows.push(["媒体类型", chk.media_type]);
    const errs = chk.errors || [];
    return `<div class="replay-result ${chk.ok ? "replay-ok" : "replay-bad"}">
      ${chk.ok ? "✓" : "✗"} ${name}：<b>${escapeHtml(chk.detail || "")}</b>
      ${rows.length ? `<dl class="kv replay-kv">${rows.map(([k, v]) =>
        `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`).join("")}</dl>` : ""}
      ${errs.length
        ? `<ul class="replay-errors">${errs.slice(0, 8)
            .map((e) => `<li>${escapeHtml(e)}</li>`).join("")}</ul>` : ""}
    </div>`;
  };
  const cv = r.carried_value;
  let carried = "";
  if (cv) {
    const sideCard = (title, s) => {
      if (!s) return "";
      if (s.applicable === false) {
        return `<div class="replay-result replay-na">— ${title}：${escapeHtml(s.detail || "该侧无对应契约")}</div>`;
      }
      return `<div class="replay-result ${s.valid ? "replay-ok" : "replay-bad"}">
        ${s.valid ? "✓" : "✗"} ${title}载荷校验：<b>${s.valid ? "通过" : "失败"}</b>
        ${s.media_type ? `（<code>${escapeHtml(s.media_type)}</code>）` : ""}
        ${(s.errors || []).length
          ? `<ul class="replay-errors">${s.errors.slice(0, 8)
              .map((e) => `<li>${escapeHtml(e)}</li>`).join("")}</ul>` : ""}
      </div>`;
    };
    carried = `<h4 style="margin:14px 0 6px;font-size:12px;color:var(--muted)">
      ${escapeHtml(cv.label || "携带值 / 载荷核对")}</h4>
      ${sideCard("基线", cv.baseline)}${sideCard("候选", cv.candidate)}`;
  }
  const verdict = r.verifiable
    ? `<div class="replay-result replay-ok">
         ✓ 反例可验证：<b>${escapeHtml(structuralVerdictText(r))}</b>
       </div>`
    : `<div class="replay-result replay-bad">
         ⚠ 反例不可验证：<b>${escapeHtml(r.unverifiable_reason
           || "未能按 finding 语义证明两侧的声明/收紧差异")}</b><br>
         破坏性结论仍然保留；可在编辑器中修正结构化反例后再次重放。
       </div>`;
  out.innerHTML = `
    ${verdict}
    ${checkCard("基线核对", sc.baseline)}
    ${checkCard("候选核对", sc.candidate)}
    ${carried}`;
}

function locationText(f) {
  const parts = [];
  if (f.location) parts.push({
    parameter: "参数", requestBody: "请求体", response: "响应",
  }[f.location] || f.location);
  if (f.status_code) parts.push("HTTP " + f.status_code);
  if (f.media_type) parts.push(f.media_type);
  return parts.join(" · ") || "端点";
}

function formatPtr(f, side) {
  const file = f.file[side];
  const pointer = f.pointer[side];
  if (!pointer) return "—";
  const sameFile = !file || file === state.report.packages[side]?.entry;
  return sameFile ? "#" + pointer : `${file}#${pointer}`;
}

// ----------------------------------------------------- release gate
const releaseMeta = {
  blocked: { label: "阻断", cls: "release-blocked" },
  pending_review: { label: "待核", cls: "release-pending" },
  releasable: { label: "可放行", cls: "release-ok" },
};
const treatmentMeta = {
  blocked: { label: "剩余破坏性 · 阻断", cls: "release-blocked" },
  pending_review: { label: "剩余无法判定 · 待核", cls: "release-pending" },
  waived: { label: "例外生效 · 已放行", cls: "release-waived" },
  not_gating: { label: "兼容 · 不参与裁决", cls: "release-na" },
};

function defaultExpiresInput() {
  const d = new Date(Date.now() + 7 * 24 * 60 * 60 * 1000);
  d.setSeconds(0, 0);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}T`
    + `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}

async function renderRelease() {
  const box = $("#list-release");
  box.hidden = false;
  box.innerHTML = `<p class="hint">正在读取完整 findings 并计算发布裁决…</p>`;
  let doc;
  try {
    const resp = await fetch(
      `/api/analyses/${state.analysisId}/release-decision`);
    if (!resp.ok) {
      const e = await resp.json().catch(() => ({}));
      throw new Error(e.detail || `HTTP ${resp.status}`);
    }
    doc = await resp.json();
    state.releaseDoc = doc;
  } catch (exc) {
    box.innerHTML = `<div class="replay-result replay-bad">发布裁决加载失败：${escapeHtml(exc.message)}</div>`;
    return;
  }
  box.innerHTML = releaseHtml(doc);
  bindReleaseForms();
}

function releaseHtml(doc) {
  const meta = releaseMeta[doc.decision] || releaseMeta.pending_review;
  const s = doc.summary || {};
  const cards = [
    ["完整 findings", s.total_findings ?? 0],
    ["破坏性", s.breaking ?? 0],
    ["无法判定", s.undetermined ?? 0],
    ["剩余破坏性", s.remaining_breaking ?? 0],
    ["剩余无法判定", s.remaining_undetermined ?? 0],
    ["生效例外", s.exceptions_active ?? 0],
  ].map(([label, num]) => `
    <div class="cov-stat">
      <span class="cov-stat-num">${num}</span>
      <span class="cov-stat-label">${label}</span>
    </div>`).join("");

  const rows = (doc.items || []).map((item) => {
    const tm = treatmentMeta[item.treatment] || treatmentMeta.not_gating;
    const active = item.active_exception;
    const expired = item.current_version_exception;
    const stale = item.stale_exceptions || [];
    const formNeeded = item.severity === "breaking"
      || item.severity === "undetermined";
    const staleHtml = stale.length
      ? `<div class="release-exception-note">${stale.slice(0, 2).map((x) =>
          escapeHtml(x.reason)).join("<br>")}</div>`
      : "";
    const expiredHtml = expired && !active
      ? `<div class="release-exception-note">${escapeHtml(expired.reason)}</div>`
      : "";
    const activeHtml = active
      ? `<div class="release-exception-note release-active-note">
           当前例外生效：${escapeHtml(active.registered_reason || "")}<br>
           依据：${escapeHtml(active.reason)}<br>
           UTC 到期：${escapeHtml(active.expires_at)}
         </div>`
      : "";
    return `<tr>
      <td><span class="release-badge ${tm.cls}">${tm.label}</span></td>
      <td><span class="sev-dot ${dotClass[item.severity]}"></span>${sevLabel[item.severity] || item.severity}</td>
      <td><span class="method ${methodClass(item.endpoint?.method || "")}">${escapeHtml(item.endpoint?.method || "")}</span>
          <code>${escapeHtml(item.endpoint?.path || "")}</code><br>
          <small>${escapeHtml(item.rule || "")} · <code>${escapeHtml(item.finding_id)}</code></small></td>
      <td class="release-message">${escapeHtml(item.message || "")}
        <div class="release-reason">${escapeHtml(item.reason || "")}</div>
        ${activeHtml}${expiredHtml}${staleHtml}
      </td>
      <td>${formNeeded ? `
        <form class="release-exception-form" data-fid="${escapeHtml(item.finding_id)}">
          <textarea name="reason" rows="2" placeholder="登记放行理由（必填）" required>${active ? escapeHtml(active.registered_reason || "") : ""}</textarea>
          <label class="utc-label">UTC 到期时间
            <input type="datetime-local" name="expires_at" required value="${active ? escapeHtml(active.expires_at.slice(0, 16)) : defaultExpiresInput()}" />
          </label>
          <button class="btn btn-primary btn-sm" type="submit">${active ? "更新例外" : "登记例外"}</button>
          <div class="release-form-msg"></div>
        </form>` : "—"}
      </td>
    </tr>`;
  }).join("");

  const orphanRows = (doc.exceptions || [])
    .filter((x) => x.state === "missing_finding")
    .map((x) => `<li><code>${escapeHtml(x.finding_id)}</code>：${escapeHtml(x.reason)}</li>`)
    .join("");
  const audit = orphanRows
    ? `<h4 class="cov-section-title">已失效/不可用的登记记录</h4><ul class="release-audit">${orphanRows}</ul>`
    : "";

  return `
    <div class="release-hero ${meta.cls}">
      <div>
        <h3>发布裁决：${meta.label}</h3>
        <p>${escapeHtml(doc.reason || "")}</p>
        <p class="hint">判定时间 UTC <code>${escapeHtml(doc.decided_at || "")}</code>
          · 当前报告版本 <code title="${escapeHtml(doc.report_version || "")}">${escapeHtml((doc.report_version || "").slice(0, 16))}</code></p>
      </div>
    </div>
    <div class="release-source-note">
      裁决固定读取服务端完整 <code>report.findings</code>；不使用端点汇总、页面筛选结果，
      也不把反例自检的 <code>pending</code> 状态当作 finding 严重度。
      例外绑定键为 <code>analysis_id + report_version + finding_id</code>。
    </div>
    <div class="cov-stat-grid">${cards}</div>
    <h4 class="cov-section-title">逐项依据与例外登记</h4>
    <div class="release-table-wrap">
      <table class="release-table">
        <thead><tr><th>裁决项</th><th>严重度</th><th>端点 / 规则</th><th>依据与原因</th><th>例外（理由 + UTC 到期）</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    ${audit}`;
}

function bindReleaseForms() {
  $$(".release-exception-form").forEach((form) => {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fid = form.dataset.fid;
      const msg = form.querySelector(".release-form-msg");
      const btn = form.querySelector("button");
      const reason = new FormData(form).get("reason").trim();
      const local = new FormData(form).get("expires_at");
      if (!reason) {
        msg.textContent = "请填写理由。";
        msg.className = "release-form-msg error-text";
        return;
      }
      if (!local) {
        msg.textContent = "请填写 UTC 到期时间。";
        msg.className = "release-form-msg error-text";
        return;
      }
      btn.disabled = true;
      msg.textContent = "提交中…";
      msg.className = "release-form-msg hint";
      try {
        const resp = await fetch(
          `/api/analyses/${state.analysisId}/release-exceptions/${fid}`,
          { method: "PUT", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ reason, expires_at: local + ":00Z" }) });
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({}));
          throw new Error(err.detail || `HTTP ${resp.status}`);
        }
        const body = await resp.json();
        state.releaseDoc = body.decision;
        const box = $("#list-release");
        box.innerHTML = releaseHtml(body.decision);
        bindReleaseForms();
      } catch (exc) {
        msg.textContent = "登记失败：" + exc.message;
        msg.className = "release-form-msg error-text";
        btn.disabled = false;
      }
    });
  });
}

// ----------------------------------------------------- call coverage
const covOutcomeMeta = {
  covered: { label: "已覆盖", cls: "cov-covered" },
  validation_failed: { label: "校验失败", cls: "cov-failed" },
  undetermined: { label: "无法判定", cls: "cov-undetermined" },
  unmatched: { label: "未匹配", cls: "cov-unmatched" },
  ambiguous: { label: "未归属", cls: "cov-ambiguous" },
  excluded_media: { label: "非 JSON 排除", cls: "cov-excluded" },
  invalid: { label: "行格式错误", cls: "cov-invalid" },
};

async function loadCoverageSummaries() {
  try {
    const data = await (await fetch(
      `/api/analyses/${state.analysisId}/coverage`)).json();
    for (const s of data.sides || []) {
      if (s.available) {
        // keep any already-loaded full doc; otherwise carry summary stub
        state.coverageDocs[s.side] = state.coverageDocs[s.side] || {
          summary: s.summary, source_file: s.source_file, __stub: true,
        };
      }
    }
    if (state.view === "coverage") renderCoverage();
  } catch { /* coverage is optional; ignore summary fetch errors */ }
}

async function uploadCoverage() {
  const side = state.coverageSide;
  const file = state.coverageFile;
  if (!file) return;
  const btn = $("#btn-coverage-upload");
  const err = $("#coverage-error");
  err.textContent = "";
  btn.disabled = true;
  btn.textContent = "计算中…";
  try {
    const fd = new FormData();
    fd.append("file", file);
    const resp = await fetch(
      `/api/analyses/${state.analysisId}/coverage/${side}`,
      { method: "POST", body: fd });
    if (!resp.ok) {
      const e = await resp.json().catch(() => ({}));
      throw new Error(e.detail || `HTTP ${resp.status}`);
    }
    const doc = await resp.json();
    state.coverageDocs[side] = doc;
    state.gapDocs[side] = null;
    renderCoverage();
  } catch (exc) {
    err.textContent = "上传/计算失败：" + exc.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "上传 NDJSON 计算覆盖";
  }
}

async function ensureCoverageDoc(side) {
  const existing = state.coverageDocs[side];
  if (existing && !existing.__stub) return existing;
  const resp = await fetch(
    `/api/analyses/${state.analysisId}/coverage/${side}`);
  if (!resp.ok) return null;
  const doc = await resp.json();
  state.coverageDocs[side] = doc;
  return doc;
}

async function renderCoverage() {
  const side = state.coverageSide;
  $$(".cov-side").forEach((b) =>
    b.classList.toggle("active", b.dataset.side === side));
  const body = $("#coverage-body");
  body.innerHTML = `<p class="hint">加载中…</p>`;
  const doc = await ensureCoverageDoc(side);
  if (!doc) {
    body.innerHTML = `<p class="hint">尚未为${side === "baseline" ? "基线" : "候选"}上传调用记录（NDJSON）。
      选择文件后点击「上传 NDJSON 计算覆盖」。</p>`;
  } else {
    body.innerHTML = coverageHtml(doc);
  }
  const has = !!doc;
  $("#btn-coverage-export").hidden = !has;
  $("#btn-gap-samples").hidden = !has;
  $("#btn-coverage-delete").hidden = !has;
  renderGapSamples(has);
}

async function ensureGapDoc(side) {
  if (state.gapDocs[side]) return state.gapDocs[side];
  const resp = await fetch(
    `/api/analyses/${state.analysisId}/coverage/${side}/gap-samples`);
  if (!resp.ok) {
    const e = await resp.json().catch(() => ({}));
    return { __error: e.detail || `HTTP ${resp.status}` };
  }
  const doc = await resp.json();
  state.gapDocs[side] = doc;
  return doc;
}

async function renderGapSamples(available) {
  const box = $("#coverage-gap-samples");
  if (!available) {
    box.innerHTML = "";
    return;
  }
  const side = state.coverageSide;
  const doc = await ensureGapDoc(side);
  if (doc && doc.__error) {
    box.innerHTML = `<div class="replay-result replay-bad">缺口补测样本不可用：${escapeHtml(doc.__error)}</div>`;
    return;
  }
  box.innerHTML = gapSamplesHtml(doc);
}

function gapSamplesHtml(doc) {
  const s = doc.summary || {};
  const sideZh = doc.analysis_side === "baseline" ? "基线" : "候选";
  let hero;
  if (s.gaps_total === 0) {
    hero = `<div class="replay-result replay-ok" style="margin:0 0 12px">
      ✓ 当前${sideZh}侧已无覆盖缺口，补测样本 NDJSON 为空文件。</div>`;
  } else {
    hero = `<div class="cov-gap-hero">
      <div><b>覆盖缺口补测样本</b>：共 ${s.gaps_total} 个未覆盖格，
        可生成 <span class="cov-gap-ok">${s.samples_generated}</span> 个补测调用，
        <span class="cov-gap-na">${s.samples_unavailable}</span> 个无法可靠生成（逐格原因见下）。</div>
      <div class="hint">下载的 NDJSON 与上传调用格式完全一致；样本状态码已避开精确码/范围键抢占，
        路径已复核唯一归属，并经现有三态覆盖判定确认确实补上目标格。</div>
    </div>`;
  }
  const cells = (doc.cells || []).map((c) => {
    const media = c.kind === "no_content" ? "无实体响应"
      : escapeHtml(c.media_type || "—");
    const head = `<span class="method ${methodClass(c.method || "")}">${escapeHtml(c.method || "")}</span>
      <code>${escapeHtml(c.path || "")}</code> ·
      声明 <code>${escapeHtml(c.declared_status)}</code> · ${media}`;
    if (c.generated) {
      const preempt = c.resolved_status !== c.declared_status
        ? ` → 实际用状态码 <code>${c.request_status}</code>（归属 <code>${escapeHtml(c.resolved_status)}</code>，避开抢占）`
        : "";
      const sample = (doc.samples || []).find((x) => x.ordinal === c.sample_ordinal);
      return `<div class="cov-gap-row cov-gap-ok-row">
        <div class="cov-gap-head">${head}
          <span class="cov-badge cov-covered">✓ 可补测 · 第 ${c.sample_ordinal} 行</span>${preempt}</div>
        <pre class="cov-gap-call">${escapeHtml(JSON.stringify(sample ? sample.call : {}))}</pre>
      </div>`;
    }
    return `<div class="cov-gap-row cov-gap-na-row">
      <div class="cov-gap-head">${head}
        <span class="cov-badge cov-undetermined">无法生成</span></div>
      <ul class="cov-gap-reasons">${(c.reasons || []).map((r) =>
        `<li>${escapeHtml(r)}</li>`).join("")}</ul>
    </div>`;
  }).join("");
  return `${hero}<div class="cov-gap-list">${cells}</div>`;
}

function downloadGapSamples() {
  const side = state.coverageSide;
  const a = document.createElement("a");
  a.href = `/api/analyses/${state.analysisId}/coverage/${side}/gap-samples.ndjson`;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function coverageHtml(doc) {
  const s = doc.summary || {};
  const denomNote = s.cells_total === 0
    ? `<span class="cov-na-pill">无适用格</span>` : "";
  const rate = s.cells_total
    ? Math.round((s.cells_covered / s.cells_total) * 100) : 0;
  const cards = [
    ["已覆盖格 / 总格", `${s.cells_covered} / ${s.cells_total}`, rate],
    ["覆盖缺口", s.cells_gap, null],
    ["覆盖调用", s.calls_covered, null],
    ["校验失败", s.calls_validation_failed, null],
    ["无法判定", s.calls_undetermined, null],
    ["未匹配", s.calls_unmatched, null],
    ["未归属(多模板)", s.calls_ambiguous, null],
    ["非 JSON 排除", s.calls_excluded_media, null],
    ["行格式错误", s.calls_invalid, null],
  ];
  const cardsHtml = cards.map(([label, num, pct]) => `
    <div class="cov-stat">
      <span class="cov-stat-num">${num ?? 0}${pct !== null ? `<small>${pct}%</small>` : ""}</span>
      <span class="cov-stat-label">${label}</span>
    </div>`).join("");

  const ops = (doc.operations || []).map((op) => {
    if (!op.applicable) {
      return `<div class="cov-op">
        <div class="cov-op-head">
          <span class="method ${methodClass(op.method)}">${op.method}</span>
          <span class="ep-path">${escapeHtml(op.path)}</span>
          ${op.operation_id ? `<span class="ep-opid">${escapeHtml(op.operation_id)}</span>` : ""}
          <span class="cov-na-pill">无适用格</span>
        </div>
        ${(op.excluded_media || []).length
          ? `<div class="cov-op-note">非 JSON 媒体不生成格：${op.excluded_media
              .map((m) => `<code>${m.status} ${escapeHtml(m.media_type)}</code>`)
              .join("、")}</div>` : ""}
      </div>`;
    }
    const cell = (c, isGap) => `
      <div class="cov-cell ${isGap ? "cov-cell-gap" : "cov-cell-ok"}"
           title="${isGap ? "缺口（未覆盖）" : `由第 ${c.covered_by_line} 行调用覆盖`}">
        <span class="cov-cell-status">${escapeHtml(c.status)}</span>
        <span class="cov-cell-mt">${c.kind === "no_content"
          ? "无实体响应" : escapeHtml(c.media_type)}</span>
        <span class="cov-cell-mark">${isGap ? "○ 缺口" : `● 第${c.covered_by_line}行`}</span>
      </div>`;
    const covered = (op.cells || []).map((c) => cell(c, false)).join("");
    const gaps = (op.gaps || []).map((c) => cell(c, true)).join("");
    return `<div class="cov-op">
      <div class="cov-op-head">
        <span class="method ${methodClass(op.method)}">${op.method}</span>
        <span class="ep-path">${escapeHtml(op.path)}</span>
        ${op.operation_id ? `<span class="ep-opid">${escapeHtml(op.operation_id)}</span>` : ""}
        <span class="cov-op-progress">${op.covered} / ${op.total} 格</span>
      </div>
      <div class="cov-cells">${covered}${gaps}</div>
      ${(op.excluded_media || []).length
        ? `<div class="cov-op-note">非 JSON 媒体不生成格：${op.excluded_media
            .map((m) => `<code>${m.status} ${escapeHtml(m.media_type)}</code>`)
            .join("、")}</div>` : ""}
    </div>`;
  }).join("");

  const rows = (doc.rows || []).map((r) => {
    const meta = covOutcomeMeta[r.outcome] || { label: r.outcome, cls: "" };
    return `<tr>
      <td class="cov-line">${r.line}</td>
      <td><span class="cov-badge ${meta.cls}">${meta.label}</span></td>
      <td><span class="method ${methodClass(r.method || "")}">${escapeHtml(r.method || "—")}</span>
          <code>${escapeHtml(r.path || "—")}</code></td>
      <td>${r.status ? escapeHtml(r.status) : "—"}</td>
      <td>${r.operation && r.matched_template && r.matched_template !== r.path
        ? `→ <code>${escapeHtml(r.matched_template)}</code>`
        : r.cell ? `<code>${escapeHtml(r.cell.replace("|", " · "))}</code>` : ""}</td>
      <td class="cov-reasons">${(r.reasons || []).map(escapeHtml)
          .join("<br>")}</td>
    </tr>`;
  }).join("");

  return `
    <div class="cov-meta">
      来源文件 <code>${escapeHtml(doc.source_file || "calls.ndjson")}</code>
      · 操作 ${s.operations_applicable}/${s.operations_total} 有适用格
      ${denomNote}
    </div>
    <div class="cov-stat-grid">${cardsHtml}</div>
    <h4 class="cov-section-title">覆盖矩阵与缺口</h4>
    <div class="cov-ops">${ops || `<p class="hint">该文档没有任何操作。</p>`}</div>
    <h4 class="cov-section-title">逐行判定（未匹配 / 失败 / 无法判定均含原因）</h4>
    <div class="cov-rows-wrap">
      <table class="cov-rows">
        <thead><tr><th>行</th><th>结局</th><th>调用</th><th>状态码</th><th>归属</th><th>原因</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function exportCoverage() {
  const side = state.coverageSide;
  const a = document.createElement("a");
  a.href = `/api/analyses/${state.analysisId}/coverage/${side}/export`;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

async function removeCoverage() {
  const side = state.coverageSide;
  if (!confirm(`确定删除${side === "baseline" ? "基线" : "候选"}侧的调用覆盖结果？`)) return;
  const resp = await fetch(
    `/api/analyses/${state.analysisId}/coverage/${side}`,
    { method: "DELETE" });
  if (resp.ok) {
    state.coverageDocs[side] = null;
    state.gapDocs[side] = null;
    renderCoverage();
  }
}

// ----------------------------------------------------- request migration
const migClassMeta = {
  affected: { label: "受影响", cls: "cov-failed" },
  unaffected: { label: "未受影响", cls: "cov-covered" },
  not_applicable: { label: "不适用", cls: "cov-excluded" },
  pending: { label: "待核", cls: "cov-undetermined" },
  invalid: { label: "行格式错误", cls: "cov-invalid" },
};
const migStateMeta = {
  pass: { label: "✓ 通过", cls: "cov-covered" },
  fail: { label: "✗ 失败", cls: "cov-failed" },
  pending: { label: "？ 待定", cls: "cov-undetermined" },
};

async function ensureMigrationDoc() {
  if (state.migrationDoc) return state.migrationDoc;
  const resp = await fetch(
    `/api/analyses/${state.analysisId}/migration`);
  if (!resp.ok) return null;
  const doc = await resp.json();
  state.migrationDoc = doc;
  return doc;
}

async function renderMigration() {
  const body = $("#migration-body");
  body.innerHTML = `<p class="hint">加载中…</p>`;
  const doc = await ensureMigrationDoc();
  if (!doc) {
    body.innerHTML = `<p class="hint">尚未上传基线时期的请求记录（NDJSON）。
      选择文件后点击「上传 NDJSON 评估迁移」。</p>`;
  } else {
    body.innerHTML = migrationHtml(doc);
  }
  const has = !!doc;
  $("#btn-migration-export").hidden = !has;
  $("#btn-migration-delete").hidden = !has;
}

async function uploadMigration() {
  const file = state.migrationFile;
  if (!file) return;
  const btn = $("#btn-migration-upload");
  const err = $("#migration-error");
  err.textContent = "";
  btn.disabled = true;
  btn.textContent = "评估中…";
  try {
    const fd = new FormData();
    fd.append("file", file);
    const resp = await fetch(
      `/api/analyses/${state.analysisId}/migration`,
      { method: "POST", body: fd });
    if (!resp.ok) {
      const e = await resp.json().catch(() => ({}));
      throw new Error(e.detail || `HTTP ${resp.status}`);
    }
    state.migrationDoc = await resp.json();
    renderMigration();
  } catch (exc) {
    err.textContent = "上传/评估失败：" + exc.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "上传 NDJSON 评估迁移";
  }
}

function migrationHtml(doc) {
  const s = doc.summary || {};
  const cards = [
    ["受影响", s.affected],
    ["未受影响", s.unaffected],
    ["待核", s.pending],
    ["不适用", s.not_applicable],
    ["行格式错误", s.invalid],
    ["受影响端点", `${s.endpoints_affected ?? 0} / ${s.endpoints_total ?? 0}`],
  ].map(([label, num]) => `
    <div class="cov-stat">
      <span class="cov-stat-num">${num ?? 0}</span>
      <span class="cov-stat-label">${label}</span>
    </div>`).join("");

  const sideCell = (side) => {
    if (!side) return "—";
    const meta = migStateMeta[side.state] || { label: side.state, cls: "" };
    const op = side.operation
      ? ` <code>${escapeHtml(side.operation)}</code>`
      : "";
    const reasons = (side.reasons || []).length
      ? `<div class="cov-reasons">${side.reasons.map(escapeHtml)
          .join("<br>")}</div>`
      : "";
    return `<span class="cov-badge ${meta.cls}">${meta.label}</span>${op}${reasons}`;
  };

  const endpoints = (doc.endpoints || []).map((ep) => {
    const cand = (ep.candidate_operations || []).length
      ? ep.candidate_operations.map((o) => `<code>${escapeHtml(o)}</code>`)
          .join("、")
      : `<span class="cov-na-pill">候选无对应端点</span>`;
    const lines = (ep.lines || []).map((ln) => {
      const meta = migClassMeta[ln.classification]
        || { label: ln.classification, cls: "" };
      return `<tr>
        <td class="cov-line">${ln.line}</td>
        <td><span class="cov-badge ${meta.cls}">${meta.label}</span></td>
        <td>${ln.candidate_operation
          ? `<code>${escapeHtml(ln.candidate_operation)}</code>` : "—"}</td>
        <td class="cov-reasons">${(ln.reasons || []).map(escapeHtml)
            .join("<br>") || "—"}</td>
      </tr>`;
    }).join("");
    return `<div class="cov-op">
      <div class="cov-op-head">
        <span class="method ${methodClass(ep.method)}">${escapeHtml(ep.method)}</span>
        <span class="ep-path">${escapeHtml(ep.path)}</span>
        <span class="cov-op-progress">受影响 ${ep.affected} / ${ep.requests_total} 请求</span>
      </div>
      <div class="cov-op-note">候选侧归属：${cand}</div>
      <div class="cov-rows-wrap" style="margin-top:8px">
        <table class="cov-rows">
          <thead><tr><th>行</th><th>迁移结论</th><th>候选端点</th><th>原因</th></tr></thead>
          <tbody>${lines}</tbody>
        </table>
      </div>
    </div>`;
  }).join("");

  const rows = (doc.rows || []).map((r) => {
    const meta = migClassMeta[r.classification]
      || { label: r.classification, cls: "" };
    return `<tr>
      <td class="cov-line">${r.line}</td>
      <td><span class="cov-badge ${meta.cls}">${meta.label}</span></td>
      <td><span class="method ${methodClass(r.method || "")}">${escapeHtml(r.method || "—")}</span>
          <code>${escapeHtml(r.path || "—")}</code></td>
      <td>${sideCell(r.baseline)}</td>
      <td>${sideCell(r.candidate)}</td>
    </tr>`;
  }).join("");

  return `
    <div class="cov-meta">
      来源文件 <code>${escapeHtml(doc.source_file || "requests.ndjson")}</code>
      · 请求 ${s.requests_total ?? 0} 行 · 受影响端点
      ${s.endpoints_affected ?? 0} / ${s.endpoints_total ?? 0}
    </div>
    <div class="cov-stat-grid">${cards}</div>
    <h4 class="cov-section-title">按端点汇总（受影响请求逐行含原因）</h4>
    <div class="cov-ops">${endpoints || `<p class="hint">没有可归入基线端点的请求行。</p>`}</div>
    <h4 class="cov-section-title">逐行双侧判定（通过 / 失败 / 待定均含原因）</h4>
    <div class="cov-rows-wrap">
      <table class="cov-rows">
        <thead><tr><th>行</th><th>迁移结论</th><th>请求</th><th>基线侧</th><th>候选侧</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function exportMigration() {
  const a = document.createElement("a");
  a.href = `/api/analyses/${state.analysisId}/migration/export`;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

async function removeMigration() {
  if (!confirm("确定删除该分析的请求迁移评估结果？")) return;
  const resp = await fetch(
    `/api/analyses/${state.analysisId}/migration`,
    { method: "DELETE" });
  if (resp.ok) {
    state.migrationDoc = null;
    renderMigration();
  }
}

// ------------------------------------------------------------- export
function exportReport(kind) {
  const map = {
    all: "",
    breaking: "severity=breaking",
    undetermined: "severity=undetermined",
    compatible: "severity=compatible",
  };
  const qs = map[kind];
  const url = `/api/analyses/${state.analysisId}/export${qs ? "?" + qs : ""}`;
  const a = document.createElement("a");
  a.href = url;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// ------------------------------------------------------------- history
async function openHistory() {
  $("#modal-history").hidden = false;
  const list = $("#history-list");
  list.innerHTML = `<p class="hint" style="padding:8px 18px">加载中…</p>`;
  try {
    const resp = await fetch("/api/analyses");
    const data = await resp.json();
    if (!data.items.length) {
      list.innerHTML = `<p class="hint" style="padding:8px 18px">暂无历史记录。</p>`;
      return;
    }
    list.innerHTML = "";
    for (const a of data.items) {
      const item = document.createElement("div");
      item.className = "history-item";
      const st = a.stats || {};
      item.innerHTML = `
        <div class="history-names">
          <b>${escapeHtml(a.baseline_name)}</b> → ${escapeHtml(a.candidate_name)}
          <small>${a.created_at ? new Date(a.created_at).toLocaleString() : ""}
            · <span style="color:var(--breaking)">${st.finding_breaking || 0} 破坏</span>
            · <span style="color:var(--undetermined)">${st.finding_undetermined || 0} 待定</span>
            · <code>${a.id}</code></small>
        </div>
        <div>
          <button class="btn btn-sm">打开</button>
        </div>`;
      item.querySelector("button").addEventListener("click", () =>
        loadHistory(a.id));
      list.appendChild(item);
    }
  } catch (exc) {
    list.innerHTML = `<p class="error-text" style="padding:8px 18px">加载失败：${escapeHtml(exc.message)}</p>`;
  }
}

async function loadHistory(id) {
  const resp = await fetch(`/api/analyses/${id}`);
  if (!resp.ok) return;
  const doc = await resp.json();
  loadReport(doc);
  closeHistory();
}

function closeHistory() {
  $("#modal-history").hidden = true;
}

// ------------------------------------------------------------- health
async function refreshHealth() {
  try {
    const h = await (await fetch("/api/health")).json();
    const el = $("#storage-badge");
    if (h.storage.available) {
      el.className = "badge badge-ok";
      el.textContent = "存储：PostgreSQL";
    } else {
      el.className = "badge badge-warn";
      el.textContent = "存储：内存降级模式";
      el.title = h.storage.error || "";
    }
  } catch {
    const el = $("#storage-badge");
    el.className = "badge badge-warn";
    el.textContent = "存储：不可用";
  }
}

// ------------------------------------------------------------- utils
function escapeHtml(s) {
  if (s === null || s === undefined) return "";
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}
