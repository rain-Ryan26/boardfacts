"use strict";

(function exposeWorkflowFlow(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.WorkflowFlow = api;
})(typeof globalThis !== "undefined" ? globalThis : window, () => {
  const errorStatuses = new Set(["completed_with_errors", "completed_with_warnings", "failed", "stopped", "interrupted"]);
  const statusLabels = {
    pending: "未开始",
    queued: "等待中",
    running: "运行中",
    completed: "已完成",
    completed_with_errors: "有错误",
    completed_with_warnings: "有警告",
    failed: "运行失败",
    stopped: "已停止",
    interrupted: "已中断",
    skipped: "已跳过",
  };

  function escapeHtml(value) {
    return String(value ?? "—").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
  }

  function formatDuration(value) {
    if (value === null || value === undefined) return "—";
    if (value < 1000) return `${value} ms`;
    if (value < 60000) return `${(value / 1000).toFixed(1)} s`;
    const minutes = Math.floor(value / 60000);
    const seconds = Math.floor((value % 60000) / 1000);
    return `${minutes} 分 ${String(seconds).padStart(2, "0")} 秒`;
  }

  function toneForStatus(status) {
    if (status === "running") return "running";
    if (status === "completed") return "completed";
    if (errorStatuses.has(status)) return "error";
    return "pending";
  }

  function timingFor(run, phase, scopeType, scopeId = null) {
    return (run?.phase_timings || []).find((timing) => (
      timing.phase === phase
      && timing.scope_type === scopeType
      && (scopeId === null || timing.scope_id === scopeId)
    ));
  }

  function makeNode({ id, title, code, timing, fallbackStatus = "pending", duration = null, durationLabel = "处理用时", detail = "" }) {
    const status = timing?.status || fallbackStatus || "pending";
    return {
      id,
      title,
      code,
      status,
      statusLabel: statusLabels[status] || status,
      tone: toneForStatus(status),
      durationMs: timing?.duration_ms ?? duration,
      durationLabel,
      detail,
    };
  }

  function buildModel(run) {
    if (!run) return null;
    const branchStatus = run.branch_status || {};
    const gerberJobs = (run.phase_timings || []).filter((timing) => timing.scope_type === "gerber_job");
    const branchNodes = [
      makeNode({
        id: "pdf-image",
        title: "PDF / 图片分析",
        code: "pdf_image",
        timing: timingFor(run, "branch_analysis", "branch", "pdf_image"),
        fallbackStatus: branchStatus.pdf_image,
      }),
      makeNode({
        id: "xlsx",
        title: "XLSX 分析",
        code: "xlsx",
        timing: timingFor(run, "branch_analysis", "branch", "xlsx"),
        fallbackStatus: branchStatus.xlsx,
      }),
      makeNode({
        id: "gerber",
        title: "Gerber 分析",
        code: "gerber",
        timing: timingFor(run, "branch_analysis", "branch", "gerber"),
        fallbackStatus: branchStatus.gerber,
        detail: gerberJobs.length ? `${gerberJobs.length} 个 job 已进入准备` : "",
      }),
    ];
    const resultStatus = ["completed", "completed_with_errors", "failed", "stopped"].includes(run.status)
      ? run.status
      : "pending";
    return {
      input: makeNode({
        id: "input",
        title: "输入快照",
        code: "input_snapshot",
        timing: timingFor(run, "input_snapshot", "phase"),
        fallbackStatus: run.status === "queued" ? "running" : "pending",
      }),
      branches: branchNodes,
      integration: makeNode({
        id: "integration",
        title: "多来源整合",
        code: "integration",
        timing: timingFor(run, "integration", "phase"),
      }),
      rule: makeNode({
        id: "rule",
        title: "规则检查",
        code: "rule_check",
        timing: timingFor(run, "rule_check", "phase"),
      }),
      evidence: makeNode({
        id: "evidence",
        title: "证据索引",
        code: "evidence_index",
        timing: timingFor(run, "evidence_index", "phase"),
      }),
      result: makeNode({
        id: "result",
        title: "运行结果",
        code: "run",
        fallbackStatus: resultStatus,
        duration: resultStatus === "pending" ? null : run.duration_ms,
        durationLabel: "总运行时长",
      }),
    };
  }

  function nodeMarkup(node) {
    const aria = `${node.title}，${node.statusLabel}，${node.durationLabel} ${formatDuration(node.durationMs)}`;
    return `
      <div class="flow-node is-${node.tone}" role="group" aria-label="${escapeHtml(aria)}">
        <div class="flow-node-head"><span class="flow-node-code">${escapeHtml(node.code)}</span><span class="flow-status"><i aria-hidden="true"></i>${escapeHtml(node.statusLabel)}</span></div>
        <strong class="flow-node-title">${escapeHtml(node.title)}</strong>
        ${node.detail ? `<span class="flow-node-detail">${escapeHtml(node.detail)}</span>` : ""}
        <div class="flow-node-duration"><span>${escapeHtml(node.durationLabel)}</span><strong>${formatDuration(node.durationMs)}</strong></div>
      </div>`;
  }

  function linkClass(node) {
    return `flow-link is-${node.tone}`;
  }

  function splitMarkup(branches) {
    return `<svg class="flow-connector flow-connector-split" viewBox="0 0 64 300" preserveAspectRatio="none" aria-hidden="true">
      <path class="${linkClass(branches[0])}" d="M2 150 C30 150 31 50 62 50" />
      <path class="${linkClass(branches[1])}" d="M2 150 C31 150 33 150 62 150" />
      <path class="${linkClass(branches[2])}" d="M2 150 C30 150 31 250 62 250" />
    </svg>`;
  }

  function mergeMarkup(branches) {
    return `<svg class="flow-connector flow-connector-merge" viewBox="0 0 64 300" preserveAspectRatio="none" aria-hidden="true">
      <path class="${linkClass(branches[0])}" d="M2 50 C33 50 34 150 62 150" />
      <path class="${linkClass(branches[1])}" d="M2 150 C31 150 33 150 62 150" />
      <path class="${linkClass(branches[2])}" d="M2 250 C33 250 34 150 62 150" />
    </svg>`;
  }

  function straightMarkup(node) {
    return `<svg class="flow-connector flow-connector-straight" viewBox="0 0 44 100" preserveAspectRatio="none" aria-hidden="true"><path class="${linkClass(node)}" d="M2 50 C16 50 28 50 42 50" /></svg>`;
  }

  function render(container, run) {
    if (!container) return;
    const model = buildModel(run);
    if (!model) {
      container.innerHTML = '<div class="flow-empty"><span aria-hidden="true">◇</span><strong>尚未创建运行</strong><small>启动分析后，这里会实时显示各节点状态与处理用时。</small></div>';
      return;
    }
    const statusSummary = [...model.branches, model.input, model.integration, model.rule, model.evidence, model.result]
      .map((node) => `${node.title}${node.statusLabel}`).join("，");
    container.innerHTML = `
      <div class="flow-scroll">
        <div class="workflow-flow" role="img" aria-label="运行流程：${escapeHtml(statusSummary)}">
          <div class="flow-stage flow-stage-single">${nodeMarkup(model.input)}</div>
          ${splitMarkup(model.branches)}
          <div class="flow-branches">${model.branches.map(nodeMarkup).join("")}</div>
          ${mergeMarkup(model.branches)}
          <div class="flow-stage flow-stage-single">${nodeMarkup(model.integration)}</div>
          ${straightMarkup(model.rule)}
          <div class="flow-stage flow-stage-single">${nodeMarkup(model.rule)}</div>
          ${straightMarkup(model.evidence)}
          <div class="flow-stage flow-stage-single">${nodeMarkup(model.evidence)}</div>
          ${straightMarkup(model.result)}
          <div class="flow-stage flow-stage-single">${nodeMarkup(model.result)}</div>
        </div>
      </div>
      <div class="flow-legend" aria-label="流程状态图例">
        <span><i class="is-pending"></i>未开始 / 等待</span>
        <span><i class="is-running"></i>运行中</span>
        <span><i class="is-completed"></i>已完成</span>
        <span><i class="is-error"></i>错误 / 失败</span>
      </div>`;
  }

  return { buildModel, formatDuration, render, toneForStatus };
});