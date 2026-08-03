"use strict";

(function bootstrap(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.GerberWorkspace = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function createGerberWorkspace() {
  function emptyReadState() {
    return {
      preparations: { status: "idle", problem: null },
      definitions: { status: "idle", problem: null },
    };
  }

  function normalizeProblem(reason, fallbackCode, fallbackMessage) {
    if (reason && typeof reason === "object") {
      return {
        ...reason,
        code: reason.code || fallbackCode,
        message: reason.message || fallbackMessage,
      };
    }
    return { code: fallbackCode, message: fallbackMessage };
  }

  function resolveResource(previous, result, fallbackCode, fallbackMessage) {
    if (result?.status === "fulfilled") {
      return {
        value: result.value,
        read: { status: "ready", problem: null },
      };
    }
    return {
      value: previous,
      read: {
        status: "error",
        problem: normalizeProblem(result?.reason, fallbackCode, fallbackMessage),
      },
    };
  }

  function resolveReads(previous, results) {
    const preparations = resolveResource(
      previous.preparations,
      results.preparations,
      "gerber_preparations_unavailable",
      "Gerber preparation 读取失败。",
    );
    const definitions = resolveResource(
      previous.definitions,
      results.definitions,
      "layer_definitions_unavailable",
      "LayerDefinition 读取失败。",
    );
    return {
      preparations: preparations.value,
      definitions: definitions.value,
      readState: {
        preparations: preparations.read,
        definitions: definitions.read,
      },
    };
  }

  function resolveSelectedJob(selectedJob, preparations) {
    if (preparations.some((item) => item.job_material_id === selectedJob)) return selectedJob;
    return preparations[0]?.job_material_id || null;
  }

  function readsReady(readState) {
    return readState.preparations.status === "ready"
      && readState.definitions.status === "ready";
  }

  function definitionReadOnly(definition, terminalRun, readState) {
    return Boolean(definition?.confirmed_at) || terminalRun || !readsReady(readState);
  }

  function readProblems(readState) {
    return [readState.preparations.problem, readState.definitions.problem].filter(Boolean);
  }

  return Object.freeze({
    emptyReadState,
    resolveReads,
    resolveSelectedJob,
    readsReady,
    definitionReadOnly,
    readProblems,
  });
});