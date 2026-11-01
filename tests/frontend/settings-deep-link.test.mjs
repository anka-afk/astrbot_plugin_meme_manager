import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(
  new URL("../../pages/app/settings/config.js", import.meta.url),
  "utf8",
);
const navigation = source.slice(
  source.indexOf("  function navigateToSetting("),
  source.indexOf("  function showFieldError("),
);
const hashHandler = source.slice(
  source.indexOf("  function applyLocationHash()"),
  source.indexOf("  applyLocationHash();"),
);
const loader = source.slice(
  source.indexOf("  async function loadConfig()"),
  source.indexOf(
    "  for (const tab of tabs) {",
    source.indexOf("  async function loadConfig()"),
  ),
);

function fixture(hash) {
  const events = [];
  const details = { open: false, parentElement: { closest: () => null } };
  const groups = [
    { id: "semantic-models", category: "models" },
    { id: "collect-review", category: "collect" },
  ];
  const heading = {
    closest: (selector) => (selector === "details" ? details : null),
    focus() {
      assert.equal(context.configState.busy, false);
      assert.equal(context.loaded, true);
      events.push("focus");
    },
    scrollIntoView() {
      events.push("scroll");
    },
  };
  const tabIds = ["models", "collect", "storage", "rules", "decision"];
  const context = vm.createContext({
    groups,
    active: "models",
    activeFlowGroup: "",
    pendingGroupAnchor: "",
    loaded: false,
    configState: { busy: false },
    dialogContent: null,
    rulesPanel: {},
    configDialog: { contains: () => false },
    configDialogTitle: heading,
    configDialogBody: { scrollTop: 0 },
    configMask: { classList: { remove() {} }, setAttribute() {} },
    categories: { models: ["配图"], collect: ["自动收集"] },
    mountDialogContent(node) {
      context.dialogContent = node;
      details.open = true;
    },
    closeSettingsDialog() {
      context.dialogContent = null;
    },
    queueMicrotask: (fn) => fn(),
    modePaths: [],
    controls: new Map(),
    changes: new Map([["generation.emotion.llm.provider_id", "draft"]]),
    search: { value: "old-search" },
    location: {
      pathname: "/settings/index.html",
      search: "?asset_token=test",
      hash,
    },
    tabs: tabIds.map((id) => ({ dataset: { settingsTab: id } })),
    mobileNavigation: { getClientRects: () => [] },
    history: {
      replaceState: (_state, _title, url) => {
        context.location.hash = url.slice(url.indexOf("#"));
      },
    },
    document: { getElementById: () => heading, querySelector: () => heading },
    updateView() {
      events.push("view");
    },
    updateState() {
      events.push(context.configState.busy ? "busy" : "ready");
    },
    notice: { hidden: true, classList: { remove() {}, add() {} } },
    api: {
      apiGet: async (path) =>
        path === "settings/config"
          ? { revision: "next", providers: { chat: [], embedding: [] } }
          : { packs: [] },
    },
    providers: {},
    providerListErrors: [],
    decisionServiceStatus: null,
    applicationFailed: false,
    packs: [],
    semanticStatus: null,
    revision: "",
    renderFields() {
      events.push("render");
    },
    window: { MemeUI: { applySecureNavLinks: async () => {} } },
  });
  vm.runInContext(navigation + hashHandler + loader, context);
  return { context, details, events };
}

test("initial card links wait for configuration and unlock before opening and focusing", async () => {
  const { context, details, events } = fixture("#config-group-semantic-models");
  context.applyLocationHash();
  assert.equal(context.pendingGroupAnchor, "semantic-models");
  assert.equal(details.open, false);
  assert.ok(!events.includes("focus"));
  await context.loadConfig();
  assert.equal(context.active, "models");
  assert.equal(context.activeFlowGroup, "semantic-models");
  assert.equal(context.pendingGroupAnchor, "");
  assert.equal(context.location.hash, "#config-group-semantic-models");
  assert.equal(details.open, true);
  assert.ok(events.indexOf("focus") > events.lastIndexOf("ready"));
  assert.equal(context.changes.size, 0);
});

test("page hash changes open another group without discarding drafts", () => {
  const { context, details } = fixture("#config-group-collect-review");
  context.loaded = true;
  context.applyLocationHash();
  assert.equal(context.active, "collect");
  assert.equal(context.activeFlowGroup, "collect-review");
  assert.equal(details.open, true);
  assert.equal(
    context.changes.get("generation.emotion.llm.provider_id"),
    "draft",
  );
});

test("the latest hash wins while configuration is busy", () => {
  const { context, events } = fixture("#config-group-semantic-models");
  context.loaded = true;
  context.configState.busy = true;
  context.applyLocationHash();
  context.location.hash = "#config-group-collect-review";
  context.applyLocationHash();
  assert.equal(context.pendingGroupAnchor, "collect-review");
  assert.ok(!events.includes("focus"));
  context.configState.busy = false;
  context.applyLocationHash();
  assert.equal(context.active, "collect");
  assert.equal(context.pendingGroupAnchor, "");
});

test("legacy tab links and unknown anchors do not activate a model or discard drafts", () => {
  const { context, events } = fixture("#storage");
  context.loaded = true;
  for (const tab of ["storage", "models", "rules"]) {
    context.location.hash = `#${tab}`;
    context.applyLocationHash();
    assert.equal(context.active, tab);
    assert.equal(context.pendingGroupAnchor, "");
  }
  context.location.hash = "#config-group-not-a-real-group";
  context.applyLocationHash();
  assert.equal(context.active, "rules");
  assert.ok(!events.includes("focus"));
  assert.equal(context.changes.size, 1);
});

test("failed initial loads retain the anchor for a later successful reload", async () => {
  const { context, events } = fixture("#config-group-semantic-models");
  context.applyLocationHash();
  const apiGet = context.api.apiGet;
  context.api.apiGet = async () => {
    throw new Error("offline");
  };
  await context.loadConfig();
  assert.equal(context.pendingGroupAnchor, "semantic-models");
  assert.ok(!events.includes("focus"));
  context.api.apiGet = apiGet;
  await context.loadConfig();
  assert.equal(context.activeFlowGroup, "semantic-models");
  assert.equal(context.pendingGroupAnchor, "");
});

test("a tab hash cancels a queued card jump before configuration finishes", async () => {
  const { context, events } = fixture("#config-group-semantic-models");
  context.applyLocationHash();
  context.location.hash = "#rules";
  context.applyLocationHash();
  await context.loadConfig();
  assert.equal(context.active, "rules");
  assert.equal(context.pendingGroupAnchor, "");
  assert.ok(!events.includes("focus"));
});

test("semantic page links and names point at the current data preparation group", async () => {
  const page = await readFile(
    new URL("../../pages/app/semantic/index.html", import.meta.url),
    "utf8",
  );
  const script = await readFile(
    new URL("../../pages/app/semantic/script.js", import.meta.url),
    "utf8",
  );
  assert.equal(
    (page.match(/settings\/index.html#config-group-semantic-models/g) || [])
      .length,
    2,
  );
  assert.ok(script.includes("设置中心 → 配图 → 语义数据准备"));
  assert.ok(!script.includes("语义与模型"));
});

test("rules popup links are distinct from the legacy standalone rules tab", () => {
  const { context } = fixture("#config-group-rules");
  context.loaded = true;
  context.applyLocationHash();
  assert.equal(context.dialogContent, context.rulesPanel);
  assert.equal(context.location.hash, "#config-group-rules");
  context.location.hash = "#rules";
  context.applyLocationHash();
  assert.equal(context.dialogContent, null);
  assert.equal(context.active, "rules");
});
