"use strict";

const assert = require("node:assert/strict");
const { groupProjectDataRows } = require("../../src/flyboard/api/static/project-data-sections.js");

function row(index, fieldCode, category, fieldState = "confirmed") {
  return { index, field_code: fieldCode, category, field_state: fieldState };
}

const rows = [
  row(3, "project_name", "项目参数"),
  row(7, "layer_count", "板件设计参数", "missing"),
  row(8, "unknown_note_001", "板件设计参数", "unknown"),
];

const sections = groupProjectDataRows(rows);
assert.deepEqual(sections.map((section) => [section.key, section.category, section.title]), [
  ["project", "项目参数", "项目信息"],
  ["design", "板件设计参数", "板件设计信息"],
]);
assert.deepEqual(sections[0].rows.map((item) => item.field_code), ["project_name"]);
assert.deepEqual(sections[1].rows.map((item) => item.field_code), ["layer_count", "unknown_note_001"]);

const missingSections = groupProjectDataRows(rows, "missing");
assert.deepEqual(missingSections[0].rows, []);
assert.deepEqual(missingSections[1].rows.map((item) => item.field_code), ["layer_count"]);
