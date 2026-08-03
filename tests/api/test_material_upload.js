"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const {
  flatUploadSelection,
  folderPickerSelection,
  droppedUploadSelection,
} = require("../../src/flyboard/api/static/material-upload.js");

function file(name, webkitRelativePath = "") {
  return { name, webkitRelativePath };
}

function fileEntry(name) {
  const value = file(name);
  return {
    name,
    isFile: true,
    isDirectory: false,
    file(resolve) { resolve(value); },
  };
}

function directoryEntry(name, batches) {
  return {
    name,
    isFile: false,
    isDirectory: true,
    createReader() {
      let index = 0;
      return {
        readEntries(resolve) {
          resolve(batches[index++] || []);
        },
      };
    },
  };
}

function itemFor(entry) {
  return { kind: "file", webkitGetAsEntry() { return entry; } };
}

test("ordinary files and one ZIP produce the closed upload modes", () => {
  assert.equal(flatUploadSelection([file("note.txt")]).mode, "files");
  assert.equal(flatUploadSelection([file("customer.ZIP")]).mode, "archive");
  assert.throws(
    () => flatUploadSelection([file("customer.zip"), file("note.txt")]),
    (problem) => problem.code === "invalid_upload_manifest",
  );
});

test("folder picker removes exactly one common browser root", () => {
  const selection = folderPickerSelection([
    file("one.txt", "customer/docs/one.txt"),
    file("two.txt", "customer/drawings/two.txt"),
  ]);
  assert.equal(selection.mode, "directory");
  assert.deepEqual(selection.entries.map((entry) => entry.relativePath), [
    "docs/one.txt",
    "drawings/two.txt",
  ]);
});

test("directory drop reads every readEntries batch and preserves nested paths", async () => {
  const nested = directoryEntry("docs", [[fileEntry("note.txt")], []]);
  const root = directoryEntry("customer", [
    [fileEntry("top.gtl"), nested],
    [fileEntry("tail.txt")],
    [],
  ]);
  const selection = await droppedUploadSelection({ items: [itemFor(root)] });
  assert.equal(selection.mode, "directory");
  assert.deepEqual(selection.entries.map((entry) => entry.relativePath), [
    "top.gtl",
    "docs/note.txt",
    "tail.txt",
  ]);
});

test("drop rejects partial Entry support instead of silently losing files", async () => {
  const missing = { kind: "file", webkitGetAsEntry() { return null; } };
  await assert.rejects(
    () => droppedUploadSelection({ items: [itemFor(fileEntry("one.txt")), missing] }),
    (problem) => problem.code === "invalid_upload_manifest" && /全部拖入资料/.test(problem.message),
  );
  await assert.rejects(
    () => droppedUploadSelection({ items: [{ kind: "file" }] }),
    (problem) => problem.code === "invalid_upload_manifest" && /选择文件/.test(problem.message),
  );
});

test("drop rejects a directory mixed with another top-level item", async () => {
  const directory = directoryEntry("customer", [[fileEntry("one.txt")], []]);
  await assert.rejects(
    () => droppedUploadSelection({ items: [itemFor(directory), itemFor(fileEntry("two.txt"))] }),
    (problem) => problem.code === "invalid_upload_manifest" && /不能与其他资料混合/.test(problem.message),
  );
});