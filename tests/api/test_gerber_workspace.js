"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const {
  emptyReadState,
  resolveReads,
  resolveSelectedJob,
  readsReady,
  definitionReadOnly,
  readProblems,
} = require("../../src/flyboard/api/static/gerber-workspace.js");

test("Gerber job recovery keeps an available choice or selects the first preparation", () => {
  const preparations = [{ job_material_id: "job_a" }, { job_material_id: "job_b" }];

  assert.equal(resolveSelectedJob("job_b", preparations), "job_b");
  assert.equal(resolveSelectedJob("missing", preparations), "job_a");
  assert.equal(resolveSelectedJob(null, []), null);
});

test("successful Gerber reads replace resources and allow draft editing", () => {
  const result = resolveReads(
    { preparations: [{ job_material_id: "old" }], definitions: [{ job_material_id: "old" }] },
    {
      preparations: { status: "fulfilled", value: [{ job_material_id: "job_a" }] },
      definitions: { status: "fulfilled", value: [] },
    },
  );

  assert.deepEqual(result.preparations, [{ job_material_id: "job_a" }]);
  assert.deepEqual(result.definitions, []);
  assert.equal(readsReady(result.readState), true);
  assert.equal(definitionReadOnly(null, false, result.readState), false);
  assert.deepEqual(readProblems(result.readState), []);
});

test("failed Gerber reads preserve stable resources and fail closed", () => {
  const previous = {
    preparations: [{ job_material_id: "job_a" }],
    definitions: [{ job_material_id: "job_a", confirmed_at: null }],
  };
  const preparationProblem = { code: "artifact_index_invalid", message: "索引不可读" };
  const result = resolveReads(previous, {
    preparations: { status: "rejected", reason: preparationProblem },
    definitions: { status: "rejected", reason: { code: "definition_read_failed", message: "定义不可读" } },
  });

  assert.deepEqual(result.preparations, previous.preparations);
  assert.deepEqual(result.definitions, previous.definitions);
  assert.equal(readsReady(result.readState), false);
  assert.equal(definitionReadOnly(result.definitions[0], false, result.readState), true);
  assert.deepEqual(readProblems(result.readState).map((problem) => problem.code), [
    "artifact_index_invalid",
    "definition_read_failed",
  ]);
});

test("confirmed and terminal definitions remain read-only after successful reads", () => {
  const ready = resolveReads(
    { preparations: [], definitions: [] },
    {
      preparations: { status: "fulfilled", value: [] },
      definitions: { status: "fulfilled", value: [] },
    },
  ).readState;

  assert.equal(definitionReadOnly({ confirmed_at: "2026-07-21T00:00:00Z" }, false, ready), true);
  assert.equal(definitionReadOnly({ confirmed_at: null }, true, ready), true);
  assert.equal(definitionReadOnly({ confirmed_at: null }, false, emptyReadState()), true);
});