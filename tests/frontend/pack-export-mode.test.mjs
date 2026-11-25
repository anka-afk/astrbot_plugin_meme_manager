import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(
  new URL("../../pages/app/a_manage/script.js", import.meta.url),
  "utf8",
);
const exportFunctions = source.slice(
  source.indexOf("  function selectedExportMode("),
  source.indexOf("  function packSupportsVectorStatus("),
);
const pageHideHandler = source.slice(
  source.indexOf('  window.addEventListener("pagehide",'),
  source.indexOf('  window.addEventListener("pageshow",'),
);

function createExportButton() {
  let html = "";
  let text = "";
  return {
    disabled: false,
    dataset: {},
    get innerHTML() {
      return html;
    },
    set innerHTML(value) {
      html = String(value);
      text = html.replace(/<[^>]*>/g, "");
    },
    get textContent() {
      return text;
    },
    set textContent(value) {
      text = String(value);
      html = text;
    },
  };
}

function setup({ available = true, fail = false } = {}) {
  const state = { available, fail, defer: false };
  let selected = "share";
  const options = ["share", "backup"].map((value) => ({
    value,
    disabled: false,
    get checked() {
      return selected === value;
    },
    set checked(next) {
      if (next) selected = value;
      else if (selected === value) selected = "";
    },
    closest() {
      return { classList: { toggle() {} } };
    },
  }));
  const [share, backup] = options;
  const requests = [];
  const pending = [];
  const downloads = [];
  const downloadPending = [];
  const listeners = new Map();
  const context = vm.createContext({
    activeManagePackId: "pack-a",
    managePageActive: true,
    managePacksById: new Map([
      ["pack-a", { id: "pack-a", name: "Pack A", image_count: 2 }],
      ["pack-b", { id: "pack-b", name: "Pack B", image_count: 3 }],
    ]),
    exportCapabilityRequestId: 0,
    exportCapabilityPackId: "",
    exportDownloadInFlight: false,
    managePackVectorStatusRequestId: 0,
    managePackSummaryRequestId: 0,
    exportModeInputs: options,
    exportModeBackup: backup,
    exportPackDownloadBtn: createExportButton(),
    exportPackResult: {},
    vectorBackupHint: { textContent: "" },
    transferCurrentPack: { textContent: "" },
    showToast() {},
    setPackTransferResult() {},
    setButtonBusy(button, busyText) {
      if (!button) return;
      if (!button.dataset.originalHtml) {
        button.dataset.originalHtml = button.innerHTML;
      }
      button.disabled = true;
      button.textContent = busyText;
    },
    restoreButton(button) {
      if (!button) return;
      button.disabled = false;
      if (button.dataset.originalHtml) {
        button.innerHTML = button.dataset.originalHtml;
      }
    },
    document: {
      getElementById(id) {
        return id === "export-mode-share" ? share : null;
      },
    },
    window: {
      addEventListener(name, callback) {
        listeners.set(name, callback);
      },
      AstrBotPluginPage: {
        download(route, params) {
          downloads.push({ route, params });
          return new Promise((resolve, reject) => {
            downloadPending.push({ resolve, reject });
          });
        },
      },
    },
    apiGet(route, params) {
      requests.push({ route, params });
      if (state.defer) {
        return new Promise((resolve, reject) => {
          pending.push({ resolve, reject });
        });
      }
      if (state.fail) return Promise.reject(new Error("offline"));
      return Promise.resolve({
        vector_backup_available: state.available,
        embedding_model: "test-model",
        embedding_dimension: 2,
      });
    },
  });
  vm.runInContext(exportFunctions, context);
  vm.runInContext(pageHideHandler, context);
  return {
    context,
    state,
    share,
    backup,
    requests,
    pending,
    downloads,
    downloadPending,
    listeners,
  };
}

test("repeated same-pack refreshes keep a checked backup selection", async () => {
  const { context, backup, requests } = setup({ available: true });
  await context.refreshPackExportCapability("pack-a");
  backup.checked = true;

  await context.refreshPackExportCapability("pack-a");
  await context.refreshPackExportCapability("pack-a");

  assert.equal(context.selectedExportMode(), "backup");
  assert.equal(backup.checked, true);
  assert.equal(backup.disabled, false);
  assert.equal(context.exportPackDownloadBtn.disabled, false);
  assert.equal(requests.length, 3);
});

test("a pending same-pack refresh keeps the backup radio checked but blocked", async () => {
  const { context, state, backup, pending } = setup({ available: true });
  await context.refreshPackExportCapability("pack-a");
  backup.checked = true;

  state.defer = true;
  const refresh = context.refreshPackExportCapability("pack-a");

  assert.equal(backup.checked, true);
  assert.equal(context.selectedExportMode(), "backup");
  assert.equal(backup.disabled, true);
  assert.equal(context.exportPackDownloadBtn.disabled, true);

  pending[0].resolve({ vector_backup_available: true });
  await refresh;

  assert.equal(backup.checked, true);
  assert.equal(backup.disabled, false);
  assert.equal(context.exportPackDownloadBtn.disabled, false);
});

test("an unavailable pack falls back to share", async () => {
  const { context, state, share, backup } = setup({ available: true });
  await context.refreshPackExportCapability("pack-a");
  backup.checked = true;

  state.available = false;
  await context.refreshPackExportCapability("pack-a");

  assert.equal(context.selectedExportMode(), "share");
  assert.equal(share.checked, true);
  assert.equal(backup.checked, false);
  assert.equal(backup.disabled, true);
  assert.equal(context.exportPackDownloadBtn.disabled, false);
});

test("a failed refresh discards a previously available backup", async () => {
  const { context, state, share, backup } = setup({ available: true });
  await context.refreshPackExportCapability("pack-a");
  backup.checked = true;

  state.fail = true;
  await context.refreshPackExportCapability("pack-a");

  assert.equal(context.selectedExportMode(), "share");
  assert.equal(share.checked, true);
  assert.equal(backup.checked, false);
  assert.equal(backup.disabled, true);
  assert.equal(context.exportPackDownloadBtn.disabled, false);
});

test("an older response cannot overwrite a newer refresh", async () => {
  const { context, state, backup, pending } = setup({ available: true });
  await context.refreshPackExportCapability("pack-a");
  backup.checked = true;

  state.defer = true;
  const first = context.refreshPackExportCapability("pack-a");
  const second = context.refreshPackExportCapability("pack-a");
  assert.equal(pending.length, 2);

  pending[1].resolve({ vector_backup_available: false });
  await second;
  assert.equal(context.selectedExportMode(), "share");
  assert.equal(backup.disabled, true);

  pending[0].resolve({ vector_backup_available: true });
  await first;
  assert.equal(context.selectedExportMode(), "share");
  assert.equal(backup.disabled, true);
  assert.equal(context.exportPackDownloadBtn.disabled, false);
});

test("a response for a previous pack cannot overwrite the active pack", async () => {
  const { context, state, backup, pending } = setup({ available: true });
  await context.refreshPackExportCapability("pack-a");
  backup.checked = true;

  state.defer = true;
  const previousPack = context.refreshPackExportCapability("pack-a");
  context.activeManagePackId = "pack-b";
  const activePack = context.refreshPackExportCapability("pack-b");

  assert.equal(context.selectedExportMode(), "share");
  assert.equal(backup.checked, false);
  assert.equal(backup.disabled, true);

  pending[1].resolve({ vector_backup_available: true });
  await activePack;
  pending[0].resolve({ vector_backup_available: false });
  await previousPack;

  assert.equal(context.exportCapabilityPackId, "pack-b");
  assert.equal(backup.disabled, false);
  assert.equal(context.exportPackDownloadBtn.disabled, false);
});

test("pagehide invalidates a pending export response after return", async () => {
  const { context, state, backup, pending, listeners } = setup({
    available: true,
  });
  await context.refreshPackExportCapability("pack-a");
  backup.checked = true;

  state.defer = true;
  const refresh = context.refreshPackExportCapability("pack-a");
  const requestIdBeforeLeave = context.exportCapabilityRequestId;

  listeners.get("pagehide")();
  context.managePageActive = true;
  pending[0].resolve({ vector_backup_available: true });
  await refresh;

  assert.equal(context.exportCapabilityRequestId, requestIdBeforeLeave + 1);
  assert.equal(backup.checked, true);
  assert.equal(backup.disabled, true);
  assert.equal(context.exportPackDownloadBtn.disabled, true);
});

test("non-active packs and inactive pages are ignored", async () => {
  const { context, requests, backup } = setup({ available: true });

  await context.refreshPackExportCapability("pack-b");
  assert.equal(requests.length, 0);
  assert.equal(context.transferCurrentPack.textContent, "");
  assert.equal(backup.disabled, false);

  context.managePageActive = false;
  await context.refreshPackExportCapability("pack-a");
  assert.equal(requests.length, 0);
  assert.equal(context.exportPackDownloadBtn.disabled, false);
});

for (const outcome of ["resolve", "reject"]) {
  test(`an in-flight download blocks refresh and duplicate downloads (${outcome})`, async () => {
    const { context, requests, downloads, downloadPending } = setup({
      available: true,
    });
    await context.refreshPackExportCapability("pack-a");
    const requestsBeforeDownload = requests.length;
    const firstDownload = context.downloadCurrentPack();

    assert.equal(downloads.length, 1);
    assert.equal(context.exportDownloadInFlight, true);
    assert.equal(context.exportPackDownloadBtn.disabled, true);
    assert.equal(context.exportPackDownloadBtn.textContent, "正在生成压缩包…");

    await context.refreshPackExportCapability("pack-a");
    context.exportModeInputs[1].checked = true;
    context.updateExportModeAppearance();
    await context.downloadCurrentPack();

    assert.equal(requests.length, requestsBeforeDownload);
    assert.equal(downloads.length, 1);
    assert.equal(context.exportPackDownloadBtn.disabled, true);
    assert.equal(context.exportPackDownloadBtn.textContent, "正在生成压缩包…");

    if (outcome === "reject") {
      downloadPending[0].reject(new Error("download failed"));
    } else {
      downloadPending[0].resolve();
    }
    await firstDownload;

    assert.equal(context.exportDownloadInFlight, false);
    assert.equal(requests.length, requestsBeforeDownload + 1);
    assert.equal(context.exportPackDownloadBtn.disabled, false);
    assert.notEqual(
      context.exportPackDownloadBtn.textContent,
      "正在生成压缩包…",
    );
  });
}
