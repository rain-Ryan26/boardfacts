"use strict";

const state = {
  projects: [], project: null, materials: [], ruleSets: [], run: null,
  data: null, findings: [], findingsState: "idle", preparations: [], definitions: [], gerberReads: GerberWorkspace.emptyReadState(),
  dataFilter: "all", selectedJob: null, poller: null, gerberRenderKey: null,
  view: { scale: 1, x: 0, y: 0, dragging: false, startX: 0, startY: 0 },
  evidenceView: { zoom: 1, objectUrl: null },
  selectedMaterialIds: new Set(), materialSelectionProjectId: null,
  uploading: false,
};

const $ = (id) => document.getElementById(id);
const terminal = new Set(["completed", "completed_with_errors", "failed", "stopped"]);
const palette = [["green", "#5f8f78"], ["red", "#b86468"], ["blue", "#5c82a6"], ["yellow", "#c5a456"], ["orange", "#bd7d4f"], ["cyan", "#5b9694"], ["purple", "#7f72a4"], ["magenta", "#a66f91"], ["white", "#f6f7f3"], ["gray", "#77868b"]];
const roles = LayerDefinitionProfile.roles;
const maxLayerProfileBytes = 1024 * 1024;
const { flatUploadSelection, folderPickerSelection, droppedUploadSelection } = MaterialUploadSelection;
const { initialMaterialSelection, preserveMaterialSelection, mergeUploadedMaterialSelection } = MaterialSelectionState;
const { groupProjectDataRows } = ProjectDataSections;
const { emptyReadState, resolveReads, resolveSelectedJob, definitionReadOnly, readProblems } = GerberWorkspace;
const dataFilterLabels = { all: "全部", confirmed: "正常", conflict: "冲突", unknown: "未知", missing: "缺失" };
let projectDataPrintPreviousTitle = null;

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...options, headers });
  const type = response.headers.get("content-type") || "";
  if (!response.ok) {
    const problem = type.includes("json") ? await response.json() : { code: "http_error", message: await response.text() };
    throw problem;
  }
  if (type.includes("json")) return response.json();
  return response;
}

function showNotice(message, error = false) {
  const notice = $("notice");
  notice.textContent = message;
  notice.className = `notice${error ? " error" : ""}`;
  notice.hidden = false;
  clearTimeout(showNotice.timer);
  showNotice.timer = setTimeout(() => { notice.hidden = true; }, 7000);
}

function showProblem(problem) {
  showNotice(`${problem.code || "request_failed"}: ${problem.message || "请求失败"}`, true);
}

function uploadProblem(code, message) {
  return { code, message };
}

function setUploadFeedback(message, error = false) {
  const feedback = $("upload-feedback");
  feedback.textContent = message;
  feedback.className = `upload-feedback${error ? " error" : ""}`;
}

function setUploading(uploading) {
  state.uploading = uploading;
  ["select-material-files", "select-material-folder", "material-files", "material-folder"].forEach((id) => {
    $(id).disabled = uploading;
  });
  document.querySelectorAll("[data-project]").forEach((button) => { button.disabled = uploading; });
}

function postMaterialUpload(projectId, selection) {
  return new Promise((resolve, reject) => {
    const body = new FormData();
    body.append("input_mode", selection.mode);
    selection.entries.forEach((entry) => {
      body.append("files", entry.file, entry.file.name);
      body.append("relative_paths", entry.relativePath);
    });
    const request = new XMLHttpRequest();
    request.open("POST", `/projects/${projectId}/materials/upload`);
    request.upload.addEventListener("progress", (event) => {
      const progress = event.lengthComputable ? `${Math.round(event.loaded * 100 / event.total)}%` : "…";
      setUploadFeedback(`正在上传 ${selection.entries.length} 个文件：${progress}`);
    });
    request.upload.addEventListener("load", () => {
      setUploadFeedback("上传完成，正在解压并识别资料…");
    });
    request.addEventListener("load", () => {
      let payload;
      try { payload = JSON.parse(request.responseText || "{}"); }
      catch (_) { return reject(uploadProblem("http_error", "服务器返回了无法识别的响应。")); }
      if (request.status < 200 || request.status >= 300) return reject(payload);
      resolve(payload);
    });
    request.addEventListener("error", () => reject(uploadProblem("network_error", "资料上传连接失败。")));
    request.addEventListener("abort", () => reject(uploadProblem("upload_aborted", "资料上传已取消。")));
    request.send(body);
  });
}

async function uploadMaterialSelection(selection) {
  if (!state.project) throw uploadProblem("project_required", "请先选择项目。");
  if (state.uploading) throw uploadProblem("upload_in_progress", "已有资料正在上传。");
  const projectId = state.project.project_id;
  const previousMaterials = state.materials;
  setUploading(true);
  setUploadFeedback("准备上传…");
  try {
    let result;
    try {
      result = await postMaterialUpload(projectId, selection);
    } catch (problem) {
      setUploadFeedback(`${problem.code || "request_failed"}: ${problem.message || "资料上传失败"}`, true);
      throw problem;
    }
    if (state.project?.project_id === projectId) {
      state.selectedMaterialIds = mergeUploadedMaterialSelection(previousMaterials, result.materials, state.selectedMaterialIds);
      state.materialSelectionProjectId = projectId;
      state.materials = result.materials;
      renderMaterials();
      updateRunControls();
    }
    const failureText = result.failures.length ? `；${result.failures.length} 项登记失败` : "";
    const runText = state.run ? "。已存在的运行快照不变，新资料可用于下次运行" : "";
    const completionText = `资料登记完成${failureText}${runText}。`;
    setUploadFeedback(completionText);
    try {
      await loadProjects(state.project?.project_id === projectId ? projectId : state.project?.project_id);
    } catch (problem) {
      setUploadFeedback(`${completionText} 项目列表刷新失败，可稍后重新选择项目刷新。`, true);
    }
  } finally {
    setUploading(false);
  }
}
function setDropOverlay(visible) {
  const overlay = $("material-drop-overlay");
  overlay.hidden = !visible;
  overlay.setAttribute("aria-hidden", String(!visible));
}

function escapeHtml(value) {
  return String(value ?? "—").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

function formatDuration(value) {
  return WorkflowFlow.formatDuration(value);
}

async function loadProjects(preferredId = null) {
  state.projects = await api("/projects");
  renderProjects();
  const target = preferredId || state.project?.project_id;
  const project = state.projects.find((item) => item.project_id === target);
  if (project) await selectProject(project.project_id);
  else if (!state.projects.length) clearProject();
}

function renderProjects() {
  $("project-list").innerHTML = state.projects.map((project) => `
    <button class="project-item ${state.project?.project_id === project.project_id ? "active" : ""}" data-project="${escapeHtml(project.project_id)}" ${state.uploading ? "disabled" : ""}>
      <span>${escapeHtml(project.project_name)}</span><small>${project.material_count}</small>
    </button>`).join("") || '<p class="muted-copy">暂无项目</p>';
}

function activateTab(tabName) {
  document.querySelectorAll(".tab").forEach((item) => item.classList.toggle("active", item.dataset.tab === tabName));
  document.querySelectorAll(".tab-panel").forEach((item) => item.classList.toggle("active", item.id === `tab-${tabName}`));
}

function clearProject() {
  state.project = null; state.run = null; state.materials = []; state.data = null;
  state.selectedMaterialIds = new Set(); state.materialSelectionProjectId = null;
  $("empty-state").hidden = false; $("project-workspace").hidden = true;
  $("workspace-title").textContent = "请选择项目";
  setRunPill(null); stopPolling(); setDropOverlay(false); setUploadFeedback("");
}

async function selectProject(projectId) {
  const preserveExistingSelection = state.materialSelectionProjectId === projectId;
  stopPolling();
  state.project = state.projects.find((item) => item.project_id === projectId) || null;
  if (!state.project) return clearProject();
  activateTab("overview");
  state.run = null; state.data = null; state.findings = []; state.findingsState = "idle"; state.preparations = []; state.definitions = []; state.gerberReads = emptyReadState(); state.selectedJob = null; state.gerberRenderKey = null;
  resetView(); renderGerber(true);
  $("empty-state").hidden = true; $("project-workspace").hidden = false;
  $("workspace-title").textContent = state.project.project_name;
  renderProjects();
  try {
    const [materials, ruleSets] = await Promise.all([api(`/projects/${projectId}/materials`), api("/rule-sets")]);
    state.materials = materials; state.ruleSets = ruleSets;
    state.selectedMaterialIds = preserveExistingSelection
      ? preserveMaterialSelection(materials, state.selectedMaterialIds)
      : initialMaterialSelection(materials);
    state.materialSelectionProjectId = projectId;
    renderMaterials(); renderRuleSets();
    if (state.project.latest_run_id) await loadRun(); else { state.run = null; renderRun(); }
  } catch (problem) { showProblem(problem); }
}

async function loadRun() {
  if (!state.project?.latest_run_id) return;
  try {
    state.run = await api(`/projects/${state.project.project_id}/runs/${state.project.latest_run_id}`);
    renderRun();
    await loadRunResources();
    if (!terminal.has(state.run.status)) startPolling(); else stopPolling();
  } catch (problem) { showProblem(problem); stopPolling(); }
}

async function loadRunResources() {
  const base = `/projects/${state.project.project_id}/runs/${state.run.run_id}`;
  const jobs = await Promise.allSettled([api(`${base}/tool-findings`), api(`${base}/gerber-preparations`), api(`${base}/layer-definitions`)]);
  if (jobs[0].status === "fulfilled") {
    state.findings = jobs[0].value;
    state.findingsState = "ready";
  } else {
    state.findings = [];
    state.findingsState = jobs[0].reason?.code || "tool_findings_unavailable";
  }
  const gerber = resolveReads(
    { preparations: state.preparations, definitions: state.definitions },
    { preparations: jobs[1], definitions: jobs[2] },
  );
  state.preparations = gerber.preparations;
  state.definitions = gerber.definitions;
  state.gerberReads = gerber.readState;
  readProblems(state.gerberReads).forEach(showProblem);
  if (terminal.has(state.run.status)) {
    try { state.data = await api(`${base}/data-list`); } catch (problem) { showProblem(problem); }
  } else {
    try { state.data = await api(`${base}/data-list`); } catch (_) { state.data = null; }
  }
  renderRunProblems(); renderData(); renderGerber();
}

function startPolling() {
  if (state.poller) return;
  state.poller = setInterval(loadRun, 1600);
}

function stopPolling() { if (state.poller) clearInterval(state.poller); state.poller = null; }

function renderMaterials() {
  $("material-list").innerHTML = state.materials.map((item) => `
    <label class="material-item">
      <input type="checkbox" data-material="${escapeHtml(item.material_id)}" ${item.kind === "unsupported" ? "disabled" : ""} ${state.selectedMaterialIds.has(item.material_id) ? "checked" : ""}>
      <span><strong>${escapeHtml(item.display_name)}</strong><br><small class="muted">${escapeHtml(item.material_id)}</small></span>
      <em class="badge">${escapeHtml(item.kind)}</em>
    </label>`).join("") || '<p class="muted-copy">尚未登记资料。</p>';
}

function renderRuleSets(preferredId = null) {
  const select = $("rule-set-select");
  const selectedId = preferredId ?? select.value;
  select.innerHTML = '<option value="">不选择规则库</option>' + state.ruleSets.map((item) => `<option value="${escapeHtml(item.rule_set_id)}">${escapeHtml(item.rule_set_name)} · ${escapeHtml(item.rule_set_version)}</option>`).join("");
  if (state.ruleSets.some((item) => item.rule_set_id === selectedId)) select.value = selectedId;
}

function setRuleSetFeedback(message, success = false) {
  const feedback = $("rule-set-feedback");
  feedback.textContent = message;
  feedback.className = `dialog-feedback${success ? " success" : ""}`;
}

function formatRuleSetMessages(messages) {
  return (messages || []).map((item) => `${item.path ? `${item.path}: ` : ""}${item.message} (${item.code})`).join("\n");
}

async function openRuleSetDialog() {
  const dialog = $("rule-set-dialog");
  setRuleSetFeedback("");
  dialog.showModal();
  $("rule-set-path").focus();
  try {
    state.ruleSets = await api("/rule-sets");
    renderRuleSets();
  } catch (problem) {
    setRuleSetFeedback(`${problem.code || "request_failed"}: ${problem.message || "规则库列表读取失败"}`);
  }
}

function setRunPill(run) {
  const pill = $("run-pill");
  pill.className = `run-pill ${run?.status || "muted"}`;
  pill.textContent = run ? `${run.status} · ${run.run_id}` : "尚未运行";
}

function updateRunControls() {
  const active = Boolean(state.run && ["queued", "running"].includes(state.run.status));
  $("start-run").disabled = active || state.selectedMaterialIds.size === 0;
  $("stop-run").disabled = !active || Boolean(state.run?.stop_requested_at);
  return active;
}

function renderRun() {
  setRunPill(state.run);
  $("tab-overview").classList.toggle("has-run", Boolean(state.run));
  const active = updateRunControls();
  $("delete-project").disabled = active;
  if (!state.run) {
    $("run-summary").textContent = "尚未创建运行。";
    $("flow-total").textContent = "总耗时 —";
    WorkflowFlow.render($("timing-list"), null);
    renderRunProblems(); return;
  }
  const rule = state.run.rule_check_summary;
  $("run-summary").innerHTML = `
    <strong>${state.run.stop_requested_at && state.run.status === "running" ? "正在停止" : escapeHtml(state.run.status)}</strong><br>
    当前阶段：${escapeHtml(state.run.current_phase)}<br>
    总耗时：${formatDuration(state.run.duration_ms)}<br>
    规则阶段：${escapeHtml(rule.status)}${rule.rule_set_id ? ` · ${escapeHtml(rule.rule_set_id)} ${escapeHtml(rule.rule_set_version)}` : ""}`;
  $("flow-total").textContent = `总耗时 ${formatDuration(state.run.duration_ms)}`;
  WorkflowFlow.render($("timing-list"), state.run);
}

function renderRunProblems() {
  const card = $("run-problems-card");
  const target = $("run-problem-list");
  if (!state.run) {
    card.hidden = true;
    target.innerHTML = "";
    return;
  }

  const seen = new Set();
  const items = [
    ...(state.run.error_summary || []).map((item) => ({
      key: `error:${item.error_id}`,
      level: item.severity,
      kind: "运行错误",
      title: item.code,
      body: item.message,
    })),
    ...state.findings.map((item) => ({
      key: `finding:${item.finding_id}`,
      level: "warning",
      kind: "工具告警",
      title: item.code,
      body: item.message,
    })),
  ].filter((item) => {
    if (seen.has(item.key)) return false;
    seen.add(item.key);
    return true;
  });

  const findingsPending = state.findingsState === "tool_findings_not_ready" && !terminal.has(state.run.status);
  if (!items.length && (state.findingsState === "ready" || findingsPending)) {
    card.hidden = true;
    target.innerHTML = "";
    return;
  }

  card.hidden = false;
  const pendingMessage = state.findingsState === "idle"
    ? "正在读取工具告警。"
    : state.findingsState === "ready"
      ? ""
      : findingsPending
        ? "工具告警将在所有分析分支完成后汇总。"
        : `工具告警当前不可读取：${state.findingsState}`;
  target.innerHTML = items.map((item) => `
    <div class="run-problem-item ${item.level}">
      <span class="badge">${escapeHtml(item.kind)}</span>
      <strong>${escapeHtml(item.title)}</strong>
      <span>${escapeHtml(item.body)}</span>
    </div>`).join("") + (pendingMessage ? `<p class="muted-copy">${escapeHtml(pendingMessage)}</p>` : "");
}

function evidenceButtons(ids) {
  const uniqueIds = [...new Set(ids || [])];
  const buttons = uniqueIds.map((id, index) => `<button class="evidence-button" data-evidence="${escapeHtml(id)}" title="${escapeHtml(id)}" aria-label="查看证据 ${index + 1}">查看证据 ${index + 1}</button>`).join("") || "—";
  return `<span class="evidence-actions">${buttons}</span>`;
}

function renderDataRow(row) {
  const candidates = row.conflict_candidates?.length ? `<div>${row.conflict_candidates.map((candidate, index) => `<div class="candidate"><strong>${index === 0 ? "主显示候选" : `候选 ${index + 1}`}</strong> · ${escapeHtml(candidate.field_value)}<br>${escapeHtml(candidate.source_ref)}<br class="evidence-actions">${evidenceButtons(candidate.evidence_ids)}</div>`).join("")}</div>` : "";
  return `<tr><td><span class="state-light ${row.field_state}"></span><strong>${escapeHtml(row.field_name)}</strong><br><small class="muted">${escapeHtml(row.field_code)} · ${escapeHtml(row.field_state)}</small></td><td>${escapeHtml(row.field_value)}<br><small class="muted">${escapeHtml(row.source)}</small>${candidates}</td><td>${escapeHtml(row.check_note)}</td><td>${escapeHtml(row.check_conclusion)}</td><td class="evidence-column">${evidenceButtons(row.evidence_ids)}</td></tr>`;
}

function renderDataSection(section) {
  const content = section.rows.length
    ? `<div class="data-section-table"><table class="data-table"><thead><tr><th>状态 / 字段</th><th>值与来源</th><th>检查备注</th><th>检查结论</th><th class="evidence-column">证据</th></tr></thead><tbody>${section.rows.map(renderDataRow).join("")}</tbody></table></div>`
    : `<p class="data-section-empty">当前筛选下暂无${escapeHtml(section.title)}。</p>`;
  return `<section class="data-section" aria-labelledby="data-section-${section.key}"><div class="data-section-heading"><div><span class="kicker">${escapeHtml(section.category)}</span><h3 id="data-section-${section.key}">${escapeHtml(section.title)}</h3></div><span class="data-section-count">${section.rows.length} 项</span></div>${content}</section>`;
}

function updateProjectDataExportAvailability() {
  $("export-project-data").disabled = !state.project || !state.run || !Array.isArray(state.data);
}

function renderData() {
  const target = $("data-list");
  updateProjectDataExportAvailability();
  if (!Array.isArray(state.data)) {
    const unavailable = state.data?.kind === "data_list_unavailable" ? `当前不可读取：${state.data.run_status}${state.data.reason_code ? ` / ${state.data.reason_code}` : ""}` : "运行完成后显示项目数据。";
    target.innerHTML = `<p class="muted-copy">${escapeHtml(unavailable)}</p>`; return;
  }
  target.innerHTML = groupProjectDataRows(state.data, state.dataFilter).map(renderDataSection).join("");
}

function finishProjectDataPrint() {
  document.body.classList.remove("printing-project-data");
  if (projectDataPrintPreviousTitle !== null) {
    document.title = projectDataPrintPreviousTitle;
    projectDataPrintPreviousTitle = null;
  }
  updateProjectDataExportAvailability();
}

function printProjectData() {
  if (!state.project || !state.run || !Array.isArray(state.data)) {
    showNotice("项目数据尚未准备好，无法导出 PDF。", true);
    return;
  }
  const projectName = state.project.project_name || state.project.project_id;
  const filterLabel = dataFilterLabels[state.dataFilter] || state.dataFilter;
  $("data-print-title").textContent = `${projectName} · 项目数据列表`;
  $("data-print-run").textContent = `run_id：${state.run.run_id}`;
  $("data-print-filter").textContent = `筛选：${filterLabel}`;
  $("data-print-time").textContent = `导出时间：${new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "medium", hour12: false }).format(new Date())}`;
  projectDataPrintPreviousTitle = document.title;
  document.title = `${projectName}-项目数据`;
  document.body.classList.add("printing-project-data");
  $("export-project-data").disabled = true;
  try {
    window.print();
  } catch (_) {
    finishProjectDataPrint();
    showNotice("浏览器未能打开打印窗口。", true);
  }
}

function currentPreparation() { return state.preparations.find((item) => item.job_material_id === state.selectedJob); }
function currentDefinition() { return state.definitions.find((item) => item.job_material_id === state.selectedJob); }

function gerberRenderKey() {
  return JSON.stringify([state.selectedJob, terminal.has(state.run?.status), state.preparations, state.definitions, state.gerberReads]);
}

function renderGerber(force = false) {
  const selectedJob = resolveSelectedJob(state.selectedJob, state.preparations);
  if (selectedJob !== state.selectedJob) {
    state.selectedJob = selectedJob;
    resetView();
  }
  const renderKey = gerberRenderKey();
  if (!force && renderKey === state.gerberRenderKey) return;
  const select = $("gerber-job-select");
  select.innerHTML = '<option value="">选择 Gerber job</option>' + state.preparations.map((item) => {
    const material = state.materials.find((candidate) => candidate.material_id === item.job_material_id);
    return `<option value="${escapeHtml(item.job_material_id)}">${escapeHtml(material?.display_name || item.job_material_id)}</option>`;
  }).join("");
  if (state.selectedJob) select.value = state.selectedJob;
  renderLayers();
  state.gerberRenderKey = renderKey;
}

function gerberProblemMarkup() {
  return readProblems(state.gerberReads).map((problem) =>
    `<p class="muted-copy">读取失败：${escapeHtml(problem.code)} · ${escapeHtml(problem.message)}</p>`
  ).join("");
}

function renderLayers() {
  const preparation = currentPreparation(); const definition = currentDefinition();
  const problemMarkup = gerberProblemMarkup();
  if (!preparation) {
    const loading = Boolean(state.run) && state.gerberReads.preparations.status === "idle";
    const emptyMessage = state.run ? "本次运行暂无可用 preparation。" : "尚未创建运行。";
    $("layer-list").innerHTML = problemMarkup || `<p class="muted-copy">${loading ? "正在读取 Gerber preparation…" : emptyMessage}</p>`;
    $("import-layer-profile").disabled = true; $("save-layers").disabled = true; $("confirm-layers").disabled = true;
    $("gerber-stage").innerHTML = "";
    $("viewer-empty").textContent = problemMarkup ? "Gerber 预览资源读取失败" : loading ? "正在读取 Gerber 预览…" : state.run ? "本次运行暂无可用 Gerber 预览" : "尚未创建运行";
    $("viewer-empty").hidden = false;
    return;
  }
  $("viewer-empty").textContent = "打开图层以查看预览";
  const existing = new Map((definition?.layers || []).map((item) => [item.layer_id, item]));
  const readOnly = definitionReadOnly(definition, terminal.has(state.run?.status), state.gerberReads);
  const layerMarkup = preparation.layer_inputs.map((layer, index) => {
    const saved = existing.get(layer.layer_id);
    return `<div class="layer-row" data-layer-row="${escapeHtml(layer.layer_id)}"><div class="layer-row-head"><input type="checkbox" class="layer-visible" data-layer="${escapeHtml(layer.layer_id)}"><strong title="${escapeHtml(layer.file_path)}">${escapeHtml(layer.file_path.split("/").pop())}</strong><span class="badge">${escapeHtml(layer.detected_type)}</span></div><div class="layer-controls"><select class="layer-role" ${readOnly ? "disabled" : ""}><option value="">选择 role</option>${roles.map((role) => `<option value="${role}" ${saved?.role === role ? "selected" : ""}>${role}</option>`).join("")}</select><input class="copper-index" type="number" min="1" placeholder="铜层序号" value="${saved?.copper_layer_index || ""}" ${readOnly ? "disabled" : ""}><select class="color-input">${palette.map(([name, color], paletteIndex) => `<option value="${color}" ${paletteIndex === index % palette.length ? "selected" : ""}>${name}</option>`).join("")}</select></div></div>`;
  }).join("");
  $("layer-list").innerHTML = problemMarkup + layerMarkup;
  $("import-layer-profile").disabled = readOnly;
  $("save-layers").disabled = readOnly;
  $("confirm-layers").disabled = !definition || readOnly;
  renderViewer();
}

function layerRows() { return [...document.querySelectorAll("[data-layer-row]")]; }

function assignVisibleColor(row) {
  const others = layerRows().filter((item) => item !== row && item.querySelector(".layer-visible").checked);
  const used = new Set(others.map((item) => item.querySelector(".color-input").value));
  const available = palette.find(([, color]) => !used.has(color));
  row.querySelector(".color-input").value = (available || palette[others.length % palette.length])[1];
}

function renderViewer() {
  const preparation = currentPreparation(); if (!preparation) return;
  const definition = currentDefinition(); const roleById = new Map((definition?.layers || []).map((item) => [item.layer_id, item]));
  const order = ["bottom_silkscreen", "bottom_paste", "bottom_solder_mask", "bottom_copper", "inner_copper", "top_copper", "top_solder_mask", "top_paste", "top_silkscreen", "mechanical", "board_outline", "drill", "route", "unknown"];
  const visible = layerRows().filter((row) => row.querySelector(".layer-visible").checked).map((row) => {
    const id = row.dataset.layerRow; const layer = preparation.layer_inputs.find((item) => item.layer_id === id); const role = roleById.get(id)?.role || row.querySelector(".layer-role").value || "unknown";
    return { id, layer, role, color: row.querySelector(".color-input").value, copperIndex: roleById.get(id)?.copper_layer_index || Number(row.querySelector(".copper-index").value) || 0, preparationIndex: preparation.layer_inputs.indexOf(layer) };
  }).filter((item) => item.role !== "ignored").sort((a, b) => { const roleOrder = order.indexOf(a.role) - order.indexOf(b.role); if (roleOrder) return roleOrder; if (a.role === "inner_copper") return b.copperIndex - a.copperIndex; return a.preparationIndex - b.preparationIndex; });
  const base = `/projects/${state.project.project_id}/runs/${state.run.run_id}/gerber-preparations/${preparation.job_material_id}/layers`;
  $("gerber-stage").innerHTML = visible.map((item) => `<div class="gerber-layer" style="--layer-color:${item.color};--layer-url:url('${base}/${item.id}/preview')"></div>`).join("");
  $("viewer-empty").hidden = visible.length > 0; applyView();
}

function applyView() { $("gerber-stage").style.transform = `translate(${state.view.x}px, ${state.view.y}px) scale(${state.view.scale})`; }
function resetView() { Object.assign(state.view, { scale: 1, x: 0, y: 0 }); applyView(); }

function layerDraftFromRows(preparation) {
  return layerRows().map((row) => {
    const source = preparation.layer_inputs.find((item) => item.layer_id === row.dataset.layerRow);
    const role = row.querySelector(".layer-role").value;
    const copper = row.querySelector(".copper-index").value;
    return { layer_id: source.layer_id, file_path: source.file_path, role, copper_layer_index: copper ? Number(copper) : null };
  });
}

async function saveLayerDefinitionDraft(preparation, layers, successMessage) {
  const definition = await api(`/projects/${state.project.project_id}/runs/${state.run.run_id}/layer-definitions/${preparation.job_material_id}`, {
    method: "PUT",
    body: JSON.stringify({
      project_id: state.project.project_id,
      run_id: state.run.run_id,
      job_material_id: preparation.job_material_id,
      layers,
    }),
  });
  state.definitions = state.definitions.filter((item) => item.job_material_id !== definition.job_material_id).concat(definition);
  renderGerber(true);
  showNotice(successMessage);
}

function releaseEvidenceObjectUrl() {
  if (!state.evidenceView.objectUrl) return;
  URL.revokeObjectURL(state.evidenceView.objectUrl);
  state.evidenceView.objectUrl = null;
}

function closeEvidenceDialog() {
  releaseEvidenceObjectUrl();
  $("evidence-dialog").close();
}

function applyEvidenceZoom() {
  const image = $("evidence-image");
  if (!image) return;
  const zoom = state.evidenceView.zoom;
  image.style.width = zoom === 1 ? "auto" : `${Math.round(zoom * 100)}%`;
  image.style.maxWidth = zoom === 1 ? "100%" : "none";
  $("evidence-zoom-value").textContent = zoom === 1 ? "适合窗口" : `${Math.round(zoom * 100)}%`;
  $("evidence-zoom-out").disabled = zoom <= .5;
  $("evidence-zoom-in").disabled = zoom >= 4;
}

function setEvidenceZoom(zoom) {
  state.evidenceView.zoom = Math.max(.5, Math.min(4, zoom));
  applyEvidenceZoom();
}

async function openEvidence(evidenceId) {
  const base = `/projects/${state.project.project_id}/runs/${state.run.run_id}/evidence/${evidenceId}`;
  try {
    const [metadata, content] = await Promise.all([api(`${base}?view=metadata`), api(`${base}?view=content`)]);
    const type = content.headers.get("content-type") || "";
    const body = $("evidence-body");
    releaseEvidenceObjectUrl();
    state.evidenceView.zoom = 1;
    body.innerHTML = `<h2>证据详情</h2><p>${escapeHtml(metadata.type)} · ${escapeHtml(metadata.file_path)}${metadata.page_index ? ` · 第 ${metadata.page_index} 页` : ""}</p><small class="evidence-id">证据编号：${escapeHtml(metadata.evidence_id)}</small>`;

    if (type.startsWith("image/")) {
      const blob = await content.blob();
      state.evidenceView.objectUrl = URL.createObjectURL(blob);
      body.insertAdjacentHTML("beforeend", `<div class="evidence-image-stage"><img id="evidence-image" src="${state.evidenceView.objectUrl}" alt="证据内容"></div>`);
      $("evidence-controls").hidden = false;
    } else {
      $("evidence-controls").hidden = true;
      body.insertAdjacentHTML("beforeend", `<pre>${escapeHtml(await content.text())}</pre>`);
    }

    $("evidence-dialog").showModal();
    applyEvidenceZoom();
  } catch (problem) {
    releaseEvidenceObjectUrl();
    showProblem(problem);
  }
}

document.addEventListener("click", async (event) => {
  const projectButton = event.target.closest("[data-project]"); if (projectButton) return selectProject(projectButton.dataset.project);
  const tab = event.target.closest(".tab"); if (tab) activateTab(tab.dataset.tab);
  const filter = event.target.closest(".filter"); if (filter) { state.dataFilter = filter.dataset.state; document.querySelectorAll(".filter").forEach((item) => item.classList.toggle("active", item === filter)); renderData(); }
  const evidence = event.target.closest("[data-evidence]"); if (evidence) await openEvidence(evidence.dataset.evidence);
});

$("create-project-form").addEventListener("submit", async (event) => { event.preventDefault(); try { const created = await api("/projects", { method: "POST", body: JSON.stringify({ project_name: $("project-name").value }) }); $("project-name").value = ""; await loadProjects(created.project_id); } catch (problem) { showProblem(problem); } });
$("open-rule-set").addEventListener("click", openRuleSetDialog);
$("close-rule-set").addEventListener("click", () => $("rule-set-dialog").close());
$("cancel-rule-set").addEventListener("click", () => $("rule-set-dialog").close());
$("rule-set-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const path = $("rule-set-path").value.trim();
  const submit = $("register-rule-set");
  submit.disabled = true; submit.textContent = "登记中…"; setRuleSetFeedback("");
  try {
    const result = await api("/rule-sets", { method: "POST", body: JSON.stringify({ rule_set_path: path }) });
    if (result.validation_status === "invalid") {
      setRuleSetFeedback(formatRuleSetMessages(result.errors) || "规则库校验失败。");
      return;
    }
    const summary = result.rule_set_summary;
    const existingIndex = state.ruleSets.findIndex((item) => item.rule_set_id === summary.rule_set_id);
    if (existingIndex === -1) state.ruleSets.push(summary); else state.ruleSets.splice(existingIndex, 1, summary);
    renderRuleSets(summary.rule_set_id);
    $("rule-set-path").value = "";
    const warningText = formatRuleSetMessages(result.warnings);
    setRuleSetFeedback(`“${summary.rule_set_name}”已登记，可在运行控制中选择。${warningText ? `\n${warningText}` : ""}`, true);
  } catch (problem) {
    setRuleSetFeedback(`${problem.code || "request_failed"}: ${problem.message || "规则库登记失败"}`);
  } finally {
    submit.disabled = false; submit.textContent = "登记规则库";
  }
});
$("select-material-files").addEventListener("click", () => { $("material-files").value = ""; $("material-files").click(); });
$("select-material-folder").addEventListener("click", () => { $("material-folder").value = ""; $("material-folder").click(); });
$("material-files").addEventListener("change", async (event) => {
  try { await uploadMaterialSelection(flatUploadSelection([...event.target.files])); }
  catch (problem) { if (!state.uploading) showProblem(problem); }
  finally { event.target.value = ""; }
});
$("material-folder").addEventListener("change", async (event) => {
  try { await uploadMaterialSelection(folderPickerSelection([...event.target.files])); }
  catch (problem) { if (!state.uploading) showProblem(problem); }
  finally { event.target.value = ""; }
});
let materialDragDepth = 0;
window.addEventListener("dragenter", (event) => {
  if (![...(event.dataTransfer?.types || [])].includes("Files")) return;
  event.preventDefault(); materialDragDepth += 1; setDropOverlay(true);
});
window.addEventListener("dragover", (event) => {
  if (![...(event.dataTransfer?.types || [])].includes("Files")) return;
  event.preventDefault(); event.dataTransfer.dropEffect = state.project && !state.uploading ? "copy" : "none";
});
window.addEventListener("dragleave", (event) => {
  if (![...(event.dataTransfer?.types || [])].includes("Files")) return;
  materialDragDepth = Math.max(0, materialDragDepth - 1);
  if (!materialDragDepth) setDropOverlay(false);
});
window.addEventListener("drop", async (event) => {
  if (![...(event.dataTransfer?.types || [])].includes("Files")) return;
  event.preventDefault(); materialDragDepth = 0; setDropOverlay(false);
  try { await uploadMaterialSelection(await droppedUploadSelection(event.dataTransfer)); }
  catch (problem) { if (!state.uploading) showProblem(problem); }
});
$("export-project-data").addEventListener("click", printProjectData);
window.addEventListener("afterprint", finishProjectDataPrint);
$("material-list").addEventListener("change", (event) => {
  const input = event.target.closest("[data-material]");
  if (!input || input.disabled) return;
  if (input.checked) state.selectedMaterialIds.add(input.dataset.material);
  else state.selectedMaterialIds.delete(input.dataset.material);
  updateRunControls();
});
$("start-run").addEventListener("click", async () => { const ids = [...state.selectedMaterialIds]; try { state.run = await api(`/projects/${state.project.project_id}/runs`, { method: "POST", body: JSON.stringify({ material_ids: ids, rule_set_id: $("rule-set-select").value || null }) }); state.project.latest_run_id = state.run.run_id; await loadProjects(state.project.project_id); } catch (problem) { if (problem.code === "active_run_exists" && problem.details?.active_project_id) { await loadProjects(problem.details.active_project_id); } showProblem(problem); } });
$("stop-run").addEventListener("click", async () => { try { state.run = await api(`/projects/${state.project.project_id}/runs/${state.run.run_id}/stop`, { method: "POST", body: "{}" }); renderRun(); startPolling(); } catch (problem) { showProblem(problem); } });
$("delete-project").addEventListener("click", async () => { if (!confirm(`确定删除项目“${state.project.project_name}”？\n选择“取消”可保留项目。`)) return; const deleteArtifacts = confirm("同时删除受管资料和运行文件？\n选择“取消”将保留文件，仅删除活动项目记录。"); try { const result = await api(`/projects/${state.project.project_id}`, { method: "DELETE", body: JSON.stringify({ delete_artifacts: deleteArtifacts }) }); showNotice(`项目已删除；文件清理：${result.artifact_cleanup.status}`); state.project = null; await loadProjects(); } catch (problem) { showProblem(problem); } });
$("gerber-job-select").addEventListener("change", (event) => { state.selectedJob = event.target.value || null; resetView(); renderGerber(true); });
$("layer-list").addEventListener("change", (event) => { if (event.target.matches(".layer-visible") && event.target.checked) assignVisibleColor(event.target.closest("[data-layer-row]")); if (event.target.matches(".layer-visible, .color-input, .layer-role")) renderViewer(); });
$("import-layer-profile").addEventListener("click", () => {
  if ($("import-layer-profile").disabled) return;
  $("layer-profile-file").value = "";
  $("layer-profile-file").click();
});
$("layer-profile-file").addEventListener("change", async (event) => {
  const file = event.target.files?.[0];
  if (!file) return;
  const button = $("import-layer-profile");
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "导入中…";
  try {
    const preparation = currentPreparation();
    if (!preparation) throw { code: "layer_profile_preparation_invalid", message: "当前没有可导入的 Gerber preparation。" };
    if (file.size > maxLayerProfileBytes) throw { code: "layer_profile_too_large", message: "层定义 profile 不能超过 1 MiB。" };
    let profile;
    try { profile = JSON.parse(await file.text()); }
    catch (_) { throw { code: "layer_profile_json_invalid", message: "层定义 profile 不是合法 JSON。" }; }
    const layers = LayerDefinitionProfile.buildDraftLayers(profile, preparation);
    await saveLayerDefinitionDraft(preparation, layers, `已从 ${file.name} 导入并保存 ${layers.length} 层草稿；请复核后显式确认。`);
  } catch (problem) {
    showProblem(problem);
  } finally {
    event.target.value = "";
    button.textContent = label;
    renderLayers();
  }
});
$("save-layers").addEventListener("click", async () => {
  const preparation = currentPreparation();
  const layers = layerDraftFromRows(preparation);
  if (layers.some((item) => !item.role)) return showNotice("请为所有层选择 role；unknown 必须显式选择。", true);
  try { await saveLayerDefinitionDraft(preparation, layers, "层定义草稿已保存。"); }
  catch (problem) { showProblem(problem); }
});
$("confirm-layers").addEventListener("click", async () => { const preparation = currentPreparation(); if (!confirm("确认后本次 run 的该 job 层定义将只读，并可能启动完整 Gerber 分析。继续？")) return; try { const definition = await api(`/projects/${state.project.project_id}/runs/${state.run.run_id}/layer-definitions/${preparation.job_material_id}/confirm`, { method: "POST", body: "{}" }); state.definitions = state.definitions.filter((item) => item.job_material_id !== definition.job_material_id).concat(definition); renderGerber(true); await loadRun(); showNotice("层定义已确认。"); } catch (problem) { showProblem(problem); } });
$("zoom-in").addEventListener("click", () => { state.view.scale = Math.min(8, state.view.scale * 1.25); applyView(); });
$("zoom-out").addEventListener("click", () => { state.view.scale = Math.max(.25, state.view.scale / 1.25); applyView(); });
$("reset-view").addEventListener("click", resetView);
$("gerber-viewer").addEventListener("pointerdown", (event) => { state.view.dragging = true; state.view.startX = event.clientX - state.view.x; state.view.startY = event.clientY - state.view.y; event.currentTarget.setPointerCapture(event.pointerId); });
$("gerber-viewer").addEventListener("pointermove", (event) => { if (!state.view.dragging) return; state.view.x = event.clientX - state.view.startX; state.view.y = event.clientY - state.view.startY; applyView(); });
$("gerber-viewer").addEventListener("pointerup", () => { state.view.dragging = false; });
$("gerber-viewer").addEventListener("wheel", (event) => { event.preventDefault(); state.view.scale = Math.max(.25, Math.min(8, state.view.scale * (event.deltaY < 0 ? 1.1 : .9))); applyView(); }, { passive: false });
$("evidence-zoom-in").addEventListener("click", () => setEvidenceZoom(state.evidenceView.zoom * 1.25));
$("evidence-zoom-out").addEventListener("click", () => setEvidenceZoom(state.evidenceView.zoom / 1.25));
$("evidence-zoom-reset").addEventListener("click", () => setEvidenceZoom(1));
$("close-evidence").addEventListener("click", closeEvidenceDialog);
$("evidence-dialog").addEventListener("cancel", releaseEvidenceObjectUrl);

loadProjects().catch(showProblem);
