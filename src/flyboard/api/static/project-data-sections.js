"use strict";

(function exposeProjectDataSections(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ProjectDataSections = api;
})(typeof globalThis === "object" ? globalThis : this, function createProjectDataSections() {
  const definitions = Object.freeze([
    Object.freeze({ key: "project", category: "项目参数", title: "项目信息" }),
    Object.freeze({ key: "design", category: "板件设计参数", title: "板件设计信息" }),
  ]);

  function groupProjectDataRows(rows, fieldState = "all") {
    const filtered = fieldState === "all" ? rows : rows.filter((row) => row.field_state === fieldState);
    return definitions.map((definition) => ({
      ...definition,
      rows: filtered.filter((row) => row.category === definition.category),
    }));
  }

  return Object.freeze({ definitions, groupProjectDataRows });
});
