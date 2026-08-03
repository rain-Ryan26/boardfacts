"use strict";

(function exposeMaterialSelectionState(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.MaterialSelectionState = api;
})(typeof globalThis === "object" ? globalThis : this, function createMaterialSelectionState() {
  function selectableMaterialIds(materials) {
    return new Set(materials.filter((item) => item.kind !== "unsupported").map((item) => item.material_id));
  }

  function initialMaterialSelection(materials) {
    return selectableMaterialIds(materials);
  }

  function preserveMaterialSelection(materials, selectedIds) {
    const selectable = selectableMaterialIds(materials);
    return new Set([...selectedIds].filter((materialId) => selectable.has(materialId)));
  }

  function mergeUploadedMaterialSelection(previousMaterials, nextMaterials, selectedIds) {
    const previousIds = new Set(previousMaterials.map((item) => item.material_id));
    const selected = preserveMaterialSelection(nextMaterials, selectedIds);
    for (const materialId of selectableMaterialIds(nextMaterials)) {
      if (!previousIds.has(materialId)) selected.add(materialId);
    }
    return selected;
  }

  return Object.freeze({
    initialMaterialSelection,
    preserveMaterialSelection,
    mergeUploadedMaterialSelection,
  });
});
