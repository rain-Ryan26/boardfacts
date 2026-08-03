"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const {
  buildDraftLayers,
  LayerDefinitionProfileError,
} = require("../../src/flyboard/api/static/layer-definition-profile.js");

function preparation() {
  return {
    job_material_id: "mat_gerber",
    layer_inputs: [
      { layer_id: "layer_top", file_path: "board/top.gbr", detected_type: "gerber" },
      { layer_id: "layer_bottom", file_path: "board/bottom.gbr", detected_type: "gerber" },
      { layer_id: "layer_drill", file_path: "drill/holes.drl", detected_type: "drill" },
    ],
  };
}

function profile() {
  return {
    schema_version: "1.0",
    matching_key: "file_path",
    confirmation: "explicit",
    layers: [
      { file_path: "drill/holes.drl", role: "drill", copper_layer_index: null },
      { file_path: "board/bottom.gbr", role: "bottom_copper", copper_layer_index: 2 },
      { file_path: "board/top.gbr", role: "top_copper", copper_layer_index: 1 },
    ],
  };
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function expectProfileError(mutator, code) {
  const value = profile();
  mutator(value);
  assert.throws(
    () => buildDraftLayers(value, preparation()),
    (error) => error instanceof LayerDefinitionProfileError && error.code === code,
  );
}

test("profile maps file_path to current layer_id and returns preparation order", () => {
  assert.deepEqual(buildDraftLayers(profile(), preparation()), [
    { layer_id: "layer_top", file_path: "board/top.gbr", role: "top_copper", copper_layer_index: 1 },
    { layer_id: "layer_bottom", file_path: "board/bottom.gbr", role: "bottom_copper", copper_layer_index: 2 },
    { layer_id: "layer_drill", file_path: "drill/holes.drl", role: "drill", copper_layer_index: null },
  ]);
});

test("profile rejects missing preparation layers", () => {
  expectProfileError((value) => value.layers.pop(), "layer_profile_file_mismatch");
});

test("profile rejects extra files", () => {
  expectProfileError((value) => value.layers.push({ file_path: "board/extra.gbr", role: "unknown", copper_layer_index: null }), "layer_profile_file_mismatch");
});

test("profile rejects duplicate file_path", () => {
  expectProfileError((value) => { value.layers[1].file_path = value.layers[0].file_path; }, "layer_profile_duplicate_file_path");
});

test("profile rejects unknown roles", () => {
  expectProfileError((value) => { value.layers[0].role = "plated_drill"; }, "layer_profile_role_invalid");
});

test("profile rejects extra root and layer fields", async (context) => {
  await context.test("root", () => {
    expectProfileError((value) => { value.comment = "not allowed"; }, "layer_profile_fields_invalid");
  });
  await context.test("layer", () => {
    expectProfileError((value) => { value.layers[0].layer_id = "client_supplied"; }, "layer_profile_layer_fields_invalid");
  });
});

test("profile rejects implicit confirmation", () => {
  expectProfileError((value) => { value.confirmation = "automatic"; }, "layer_profile_confirmation_invalid");
});

test("profile enforces copper index rules before PUT", async (context) => {
  await context.test("top index", () => {
    expectProfileError((value) => { value.layers[2].copper_layer_index = 3; }, "layer_profile_copper_order_invalid");
  });
  await context.test("duplicate index", () => {
    expectProfileError((value) => { value.layers[1].copper_layer_index = 1; }, "layer_profile_copper_order_invalid");
  });
  await context.test("non-copper index", () => {
    expectProfileError((value) => { value.layers[0].copper_layer_index = 4; }, "layer_profile_copper_index_invalid");
  });
});

test("profile rejects a malformed preparation before reading profile content", () => {
  const malformed = clone(preparation());
  malformed.layer_inputs[1].file_path = malformed.layer_inputs[0].file_path;
  assert.throws(
    () => buildDraftLayers(profile(), malformed),
    (error) => error instanceof LayerDefinitionProfileError && error.code === "layer_profile_preparation_invalid",
  );
});
