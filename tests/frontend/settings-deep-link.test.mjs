import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../../pages/app/settings/config.js", import.meta.url), "utf8");
const code = source.slice(source.indexOf("  function navigateToSetting("), source.indexOf("  function showFieldError("))
  + source.slice(source.indexOf("  function applyLocationHash()"), source.indexOf("  applyLocationHash();"))
  + source.slice(source.indexOf("  async function loadConfig()"), source.indexOf("  for (const tab of tabs) {", source.indexOf("  async function loadConfig()")));

function fixture(hash) {
  const events = [];
  const heading = {
    closest: () => null, scrollIntoView() {},
    focus() {
      assert.equal(context.configState.busy, false);
      assert.equal(context.loaded, true);
      events.push("focus");
    },
  };
  const context = vm.createContext({
    groups: [{ id: "semantic-models", category: "models" }, { id: "collect-review", category: "collect" }],
    active: "models", activeFlowGroup: "", pendingGroupAnchor: "", loaded: false,
    configState: { busy: false }, dialogContent: null, rulesPanel: {},
    configDialog: { contains: () => false }, configDialogTitle: heading,
    configDialogBody: { scrollTop: 0 },
    configMask: { classList: { remove() {} }, setAttribute() {} },
    categories: { models: ["模式"], collect: ["自动收集"] },
    mountDialogContent(node) { context.dialogContent = node; },
    closeSettingsDialog() { context.dialogContent = null; },
    queueMicrotask: fn => fn(), modePaths: [], controls: new Map(),
    changes: new Map([["generation.emotion.llm.provider_id", "draft"]]), search: { value: "" },
    location: { pathname: "/settings/index.html", search: "?asset_token=test", hash },
    tabs: ["models", "collect", "storage", "rules", "decision"].map(id => ({ dataset: { settingsTab: id } })),
    mobileNavigation: { getClientRects: () => [] },
    history: { replaceState: (_, __, url) => { context.location.hash = url.slice(url.indexOf("#")); } },
    document: { getElementById: () => heading, querySelector: () => heading },
    updateView() {}, updateState() {}, renderFields() {},
    notice: { hidden: true, classList: { remove() {}, add() {} } },
    api: { apiGet: async path => path === "settings/config"
      ? { revision: "next", providers: { chat: [], embedding: [] } } : { packs: [] } },
    providers: {}, providerListErrors: [], decisionServiceStatus: null, applicationFailed: false,
    packs: [], semanticStatus: null, revision: "",
    window: { MemeUI: { applySecureNavLinks: async () => {} } },
  });
  vm.runInContext(code, context);
  return { context, events };
}

for (const next of ["#config-group-semantic-models", "#config-group-collect-review", "#rules"]) {
  test("deferred navigation follows the latest target: " + next, async () => {
    const { context, events } = fixture("#config-group-semantic-models");
    context.applyLocationHash();
    assert.equal(events.length, 0);
    context.location.hash = next;
    context.applyLocationHash();
    await context.loadConfig();
    assert.equal(context.pendingGroupAnchor, "");
    assert.equal(context.changes.size, 0);
    assert.equal(context.location.search, "?asset_token=test");
    if (next === "#rules") {
      assert.equal(context.active, "rules");
      assert.equal(events.length, 0);
    } else {
      assert.equal(context.activeFlowGroup, next.replace("#config-group-", ""));
      assert.ok(events.includes("focus"));
    }
  });
}

test("failed loads retain the link for retry; legacy and rules links preserve drafts", async () => {
  const { context, events } = fixture("#config-group-semantic-models");
  context.applyLocationHash();
  const apiGet = context.api.apiGet;
  context.api.apiGet = async () => { throw new Error("offline"); };
  await context.loadConfig();
  assert.equal(context.pendingGroupAnchor, "semantic-models");
  assert.equal(events.length, 0);
  context.api.apiGet = apiGet;
  await context.loadConfig();
  context.changes.set("generation.emotion.llm.provider_id", "draft");
  for (const tab of ["models", "storage", "rules"]) {
    context.location.hash = "#" + tab;
    context.applyLocationHash();
    assert.equal(context.active, tab);
  }
  context.location.hash = "#config-group-rules";
  context.applyLocationHash();
  assert.equal(context.dialogContent, context.rulesPanel);
  context.location.hash = "#rules";
  context.applyLocationHash();
  assert.equal(context.dialogContent, null);
  context.location.hash = "#config-group-unknown";
  context.applyLocationHash();
  assert.equal(context.active, "rules");
  assert.equal(context.changes.size, 1);
});
