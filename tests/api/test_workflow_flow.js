"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const {
  buildModel,
  formatDuration,
  render,
  toneForStatus,
} = require("../../src/flyboard/api/static/workflow-flow.js");

function timing(phase, scopeType, scopeId, status, durationMs) {
  return {
    phase,
    scope_type: scopeType,
    scope_id: scopeId,
    status,
    duration_ms: durationMs,
  };
}

function runSummary() {
  return {
    status: "running",
    duration_ms: 81234,
    branch_status: {
      pdf_image: "completed",
      xlsx: "running",
      gerber: "queued",
    },
    phase_timings: [
      timing("input_snapshot", "phase", null, "completed", 21),
      timing("branch_analysis", "branch", "pdf_image", "completed", 54500),
      timing("branch_analysis", "branch", "xlsx", "running", 18750),
      timing("gerber_preparation", "gerber_job", "mat_gerber", "completed", 2700),
    ],
  };
}

test("buildModel maps the stable run summary into the real workflow order", () => {
  const model = buildModel(runSummary());
  assert.equal(model.input.title, "输入快照");
  assert.deepEqual(model.branches.map((node) => node.code), ["pdf_image", "xlsx", "gerber"]);
  assert.equal(model.branches[0].tone, "completed");
  assert.equal(model.branches[1].tone, "running");
  assert.equal(model.branches[1].durationMs, 18750);
  assert.equal(model.branches[2].statusLabel, "等待中");
  assert.equal(model.branches[2].detail, "1 个 job 已进入准备");
  assert.equal(model.integration.statusLabel, "未开始");
  assert.equal(model.result.statusLabel, "未开始");
});

test("error-bearing and stopped states use the error tone", () => {
  assert.equal(toneForStatus("completed_with_errors"), "error");
  assert.equal(toneForStatus("completed_with_warnings"), "error");
  assert.equal(toneForStatus("failed"), "error");
  assert.equal(toneForStatus("stopped"), "error");
});

test("terminal result shows total duration and escaped workflow markup", () => {
  const summary = runSummary();
  summary.status = "completed_with_errors";
  summary.duration_ms = 125000;
  summary.branch_status.gerber = "failed";
  summary.phase_timings.push(
    timing("branch_analysis", "branch", "gerber", "failed", 1200),
    timing("integration", "phase", null, "completed_with_errors", 3400),
  );
  const container = { innerHTML: "" };
  render(container, summary);
  assert.match(container.innerHTML, /运行流程与耗时|workflow-flow/);
  assert.match(container.innerHTML, /PDF \/ 图片分析/);
  assert.match(container.innerHTML, /is-running/);
  assert.match(container.innerHTML, /is-error/);
  assert.equal(buildModel(summary).result.durationMs, 125000);
  assert.equal(buildModel(summary).result.durationLabel, "总运行时长");
  assert.match(container.innerHTML, /总运行时长/);
  assert.equal(formatDuration(125000), "2 分 05 秒");
});

test("empty workflow gives a clear start instruction", () => {
  const container = { innerHTML: "" };
  render(container, null);
  assert.match(container.innerHTML, /尚未创建运行/);
  assert.match(container.innerHTML, /启动分析后/);
});