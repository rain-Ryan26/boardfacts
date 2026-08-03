"use strict";

(function exposeMaterialUploadSelection(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.MaterialUploadSelection = api;
})(typeof globalThis === "object" ? globalThis : this, function createMaterialUploadSelection() {
  function fail(message) {
    throw { code: "invalid_upload_manifest", message };
  }

  function flatUploadSelection(files) {
    const entries = Array.from(files).map((file) => ({ file, relativePath: file.name }));
    if (!entries.length) fail("没有可上传的文件。");
    const archives = entries.filter((entry) => entry.relativePath.toLowerCase().endsWith(".zip"));
    if (archives.length) {
      if (entries.length !== 1) fail("ZIP 压缩包不能与其他文件一起上传。");
      return { mode: "archive", entries };
    }
    return { mode: "files", entries };
  }

  function folderPickerSelection(files) {
    const selected = Array.from(files);
    if (!selected.length) fail("所选文件夹中没有可上传的文件。");
    const entries = selected.map((file) => {
      const parts = String(file.webkitRelativePath || "").split("/").filter(Boolean);
      if (parts.length < 2) fail("浏览器没有提供文件夹相对路径。");
      return { file, root: parts[0], relativePath: parts.slice(1).join("/") };
    });
    const roots = new Set(entries.map((entry) => entry.root));
    if (roots.size !== 1) fail("一次只能上传一个文件夹。");
    return { mode: "directory", entries };
  }

  function entryFile(entry) {
    return new Promise((resolve, reject) => entry.file(resolve, reject));
  }

  async function directoryChildren(entry) {
    const reader = entry.createReader();
    const children = [];
    while (true) {
      const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
      if (!batch.length) return children;
      children.push(...batch);
    }
  }

  async function collectDroppedDirectory(entry, prefix = "") {
    const entries = [];
    for (const child of await directoryChildren(entry)) {
      if (child.isFile) {
        entries.push({ file: await entryFile(child), relativePath: `${prefix}${child.name}` });
      } else if (child.isDirectory) {
        entries.push(...await collectDroppedDirectory(child, `${prefix}${child.name}/`));
      } else {
        fail("拖入内容包含浏览器无法识别的项目，请改用选择文件夹或 ZIP。");
      }
    }
    return entries;
  }

  async function droppedUploadSelection(dataTransfer) {
    const items = Array.from(dataTransfer?.items || []).filter((item) => item.kind === "file");
    if (!items.length) fail("没有可上传的文件。");
    const topEntries = items.map((item) => {
      if (typeof item.webkitGetAsEntry !== "function") {
        fail("浏览器无法读取拖放目录结构，请改用选择文件、选择文件夹或 ZIP。");
      }
      const entry = item.webkitGetAsEntry();
      if (!entry) fail("浏览器未能读取全部拖入资料，请重新选择文件、文件夹或 ZIP。");
      return entry;
    });
    const directories = topEntries.filter((entry) => entry.isDirectory);
    if (directories.length) {
      if (topEntries.length !== 1) fail("一次只能拖入一个文件夹，不能与其他资料混合。");
      const entries = await collectDroppedDirectory(directories[0]);
      if (!entries.length) fail("拖入的文件夹中没有可上传的文件。");
      return { mode: "directory", entries };
    }
    if (topEntries.some((entry) => !entry.isFile)) {
      fail("拖入内容包含浏览器无法识别的项目，请改用选择文件夹或 ZIP。");
    }
    return flatUploadSelection(await Promise.all(topEntries.map((entry) => entryFile(entry))));
  }

  return Object.freeze({ flatUploadSelection, folderPickerSelection, droppedUploadSelection });
});