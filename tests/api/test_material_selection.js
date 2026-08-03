"use strict";

const assert = require("node:assert/strict");
const {
  initialMaterialSelection,
  preserveMaterialSelection,
  mergeUploadedMaterialSelection,
} = require("../../src/flyboard/api/static/material-selection.js");

function material(materialId, kind = "pdf") {
  return { material_id: materialId, kind };
}

const initialMaterials = [material("mat_pdf"), material("mat_xlsx", "spreadsheet"), material("mat_note", "unsupported")];
assert.deepEqual([...initialMaterialSelection(initialMaterials)], ["mat_pdf", "mat_xlsx"]);

const preserved = preserveMaterialSelection(initialMaterials, new Set(["mat_pdf", "mat_missing", "mat_note"]));
assert.deepEqual([...preserved], ["mat_pdf"]);

const afterUpload = mergeUploadedMaterialSelection(
  initialMaterials,
  [...initialMaterials, material("mat_image", "image"), material("mat_unknown", "unsupported")],
  new Set(["mat_pdf"]),
);
assert.deepEqual([...afterUpload], ["mat_pdf", "mat_image"]);

const afterRefresh = preserveMaterialSelection(initialMaterials, new Set(["mat_pdf"]));
assert.deepEqual([...afterRefresh], ["mat_pdf"]);
