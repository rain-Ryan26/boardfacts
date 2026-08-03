"use strict";

(function exposeLayerDefinitionProfile(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.LayerDefinitionProfile = api;
})(typeof globalThis === "object" ? globalThis : this, function createLayerDefinitionProfile() {
  const roles = Object.freeze([
    "top_silkscreen",
    "bottom_silkscreen",
    "top_paste",
    "bottom_paste",
    "top_solder_mask",
    "bottom_solder_mask",
    "top_copper",
    "inner_copper",
    "bottom_copper",
    "mechanical",
    "board_outline",
    "drill",
    "route",
    "unknown",
    "ignored",
  ]);
  const roleSet = new Set(roles);
  const copperRoles = new Set(["top_copper", "inner_copper", "bottom_copper"]);
  const rootFields = new Set(["schema_version", "matching_key", "confirmation", "layers"]);
  const layerFields = new Set(["file_path", "role", "copper_layer_index"]);

  class LayerDefinitionProfileError extends Error {
    constructor(code, message) {
      super(message);
      this.name = "LayerDefinitionProfileError";
      this.code = code;
    }
  }

  function fail(code, message) {
    throw new LayerDefinitionProfileError(code, message);
  }

  function isPlainObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function requireExactFields(value, expected, code, location) {
    const actual = Object.keys(value);
    const unexpected = actual.filter((key) => !expected.has(key));
    const missing = [...expected].filter((key) => !Object.hasOwn(value, key));
    if (unexpected.length || missing.length) {
      const details = [
        unexpected.length ? `多余字段：${unexpected.join(", ")}` : "",
        missing.length ? `缺少字段：${missing.join(", ")}` : "",
      ].filter(Boolean).join("；");
      fail(code, `${location}字段不符合封闭格式（${details}）。`);
    }
  }

  function validatePreparation(preparation) {
    if (!isPlainObject(preparation) || !Array.isArray(preparation.layer_inputs) || !preparation.layer_inputs.length) {
      fail("layer_profile_preparation_invalid", "当前 job 没有可导入的 Gerber preparation 层清单。");
    }
    const byPath = new Map();
    for (const item of preparation.layer_inputs) {
      if (!isPlainObject(item) || typeof item.layer_id !== "string" || !item.layer_id || typeof item.file_path !== "string" || !item.file_path) {
        fail("layer_profile_preparation_invalid", "当前 Gerber preparation 含有非法层身份。");
      }
      if (byPath.has(item.file_path)) {
        fail("layer_profile_preparation_invalid", `当前 Gerber preparation 重复包含文件：${item.file_path}。`);
      }
      byPath.set(item.file_path, item);
    }
    return byPath;
  }

  function validateCopperOrder(layers) {
    const copper = layers.filter((item) => copperRoles.has(item.role));
    const indices = new Set();
    for (const item of copper) {
      if (!Number.isInteger(item.copper_layer_index) || item.copper_layer_index <= 0) {
        fail("layer_profile_copper_index_invalid", `${item.file_path} 的 copper_layer_index 必须是正整数。`);
      }
      if (indices.has(item.copper_layer_index)) {
        fail("layer_profile_copper_order_invalid", `铜层序号重复：${item.copper_layer_index}。`);
      }
      indices.add(item.copper_layer_index);
    }
    const top = copper.filter((item) => item.role === "top_copper");
    const bottom = copper.filter((item) => item.role === "bottom_copper");
    if (top.length > 1 || bottom.length > 1) {
      fail("layer_profile_copper_order_invalid", "同一 job 最多只能定义一个 top_copper 和一个 bottom_copper。");
    }
    if (top.length && top[0].copper_layer_index !== 1) {
      fail("layer_profile_copper_order_invalid", "top_copper 的 copper_layer_index 必须为 1。");
    }
    if (bottom.length) {
      const innerIndices = copper.filter((item) => item.role === "inner_copper").map((item) => item.copper_layer_index);
      if (innerIndices.some((index) => bottom[0].copper_layer_index <= index)) {
        fail("layer_profile_copper_order_invalid", "bottom_copper 的序号必须大于所有 inner_copper。");
      }
    }
  }

  function buildDraftLayers(profile, preparation) {
    const preparationByPath = validatePreparation(preparation);
    if (!isPlainObject(profile)) fail("layer_profile_root_invalid", "层定义 profile 顶层必须是 JSON object。");
    requireExactFields(profile, rootFields, "layer_profile_fields_invalid", "层定义 profile 顶层");
    if (profile.schema_version !== "1.0") fail("layer_profile_version_unsupported", "层定义 profile 的 schema_version 必须为 1.0。");
    if (profile.matching_key !== "file_path") fail("layer_profile_matching_key_invalid", "层定义 profile 的 matching_key 必须为 file_path。");
    if (profile.confirmation !== "explicit") fail("layer_profile_confirmation_invalid", "层定义 profile 必须声明 confirmation=explicit，导入不能自动 Confirm。");
    if (!Array.isArray(profile.layers) || !profile.layers.length) fail("layer_profile_layers_invalid", "层定义 profile 的 layers 必须是非空数组。");

    const profileByPath = new Map();
    for (const [index, item] of profile.layers.entries()) {
      if (!isPlainObject(item)) fail("layer_profile_layer_invalid", `layers[${index}] 必须是 JSON object。`);
      requireExactFields(item, layerFields, "layer_profile_layer_fields_invalid", `layers[${index}] `);
      if (typeof item.file_path !== "string" || !item.file_path || item.file_path.trim() !== item.file_path) {
        fail("layer_profile_file_path_invalid", `layers[${index}].file_path 必须是非空且无首尾空白的字符串。`);
      }
      if (profileByPath.has(item.file_path)) fail("layer_profile_duplicate_file_path", `层定义 profile 重复包含文件：${item.file_path}。`);
      if (!roleSet.has(item.role)) fail("layer_profile_role_invalid", `${item.file_path} 使用了非法 role：${String(item.role)}。`);
      if (copperRoles.has(item.role)) {
        if (!Number.isInteger(item.copper_layer_index) || item.copper_layer_index <= 0) {
          fail("layer_profile_copper_index_invalid", `${item.file_path} 的 copper_layer_index 必须是正整数。`);
        }
      } else if (item.copper_layer_index !== null) {
        fail("layer_profile_copper_index_invalid", `${item.file_path} 不是铜层，copper_layer_index 必须为 null。`);
      }
      profileByPath.set(item.file_path, item);
    }

    const missing = [...preparationByPath.keys()].filter((path) => !profileByPath.has(path));
    const extra = [...profileByPath.keys()].filter((path) => !preparationByPath.has(path));
    if (missing.length || extra.length) {
      const details = [
        missing.length ? `缺少：${missing.join(", ")}` : "",
        extra.length ? `多余：${extra.join(", ")}` : "",
      ].filter(Boolean).join("；");
      fail("layer_profile_file_mismatch", `profile 必须完整且只覆盖当前 preparation（${details}）。`);
    }

    const layers = preparation.layer_inputs.map((source) => {
      const configured = profileByPath.get(source.file_path);
      return {
        layer_id: source.layer_id,
        file_path: source.file_path,
        role: configured.role,
        copper_layer_index: configured.copper_layer_index,
      };
    });
    validateCopperOrder(layers);
    return layers;
  }

  return Object.freeze({ roles, buildDraftLayers, LayerDefinitionProfileError });
});
