import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const script = (
  await readFile(
    new URL("../../pages/app/catalog/updates.js", import.meta.url),
    "utf8",
  )
).replace("export function", "function");
const tick = () => new Promise((resolve) => setImmediate(resolve));

class Element {
  children = [];
  listeners = {};
  dataset = {};
  isConnected = true;
  checked = false;
  value = "";
  addEventListener(name, callback) {
    this.listeners[name] = callback;
  }
  setAttribute() {}
  append(...children) {
    this.children.push(...children);
  }
  replaceChildren(...children) {
    this.children = children;
  }
  add(child) {
    this.children.push(child);
    if (this.children.length === 1) this.value = child.value;
  }
  showModal() {
    this.open = true;
  }
  close() {
    this.open = false;
  }
}

function setup({ baseline = true, preview, runningCheck = false } = {}) {
  const nodes = new Map(),
    calls = [],
    storage = new Map();
  const get = (id) => {
    if (!nodes.has(id)) nodes.set(id, new Element());
    return nodes.get(id);
  };
  let job = {};
  const context = vm.createContext({
    document: {
      body: new Element(),
      createElement: () =>
        Object.assign(new Element(), {
          querySelector: (id) => get(id.slice(1)),
        }),
      createTextNode: (text) => text,
    },
    Option: class extends Element {
      constructor(text, value) {
        super();
        this.textContent = text;
        this.value = value;
      }
    },
    window: {},
    sessionStorage: {
      setItem: (key, value) => storage.set(key, value),
      getItem: (key) => storage.get(key),
      removeItem: (key) => storage.delete(key),
    },
    setTimeout,
  });
  vm.runInContext(script, context);
  const component = context.setupCatalogUpdates({
    apiGet: async () => ({ state: "completed", ...job }),
    apiPost: async (_, body) => {
      calls.push(body);
      if (body.action === "cancel") {
        job.state = "cancelled";
        return {};
      }
      if (body.action === "preview")
        return preview
          ? preview(body)
          : {
              ready: true,
              has_baseline: baseline,
              counts: {},
              categories: [],
              rows: [],
              plan_token: "reviewed",
            };
      if (body.action === "restore_preview")
        return {
          current_images: 4,
          current_version: "2",
          version: "1",
          plan_token: "restore-reviewed",
        };
      job = {
        state:
          runningCheck && body.action === "prepare" ? "running" : "completed",
        action: body.action,
        result:
          body.action === "prepare"
            ? { session: "session", has_baseline: baseline }
            : { backup_id: "backup" },
      };
      return { job_id: "job" };
    },
    onBusy() {},
    async onDone() {},
  });
  return {
    component,
    calls,
    get: (suffix) => get(`update-${suffix}`),
    restoreAck: get("restore-ack"),
    storage,
    dialog: context.document.body.children[0],
    finishDownload() {
      job.state = "completed";
    },
  };
}

test("background download can be reopened and completion does not force a popup", async () => {
  const state = setup({ runningCheck: true });
  const pending = state.component.open({ id: "pack" }, {});
  await tick();
  state.get("close").listeners.click();
  assert.equal(state.dialog.open, false);
  await state.component.open({ id: "pack" }, {});
  assert.equal(state.dialog.open, true);
  assert.equal(
    state.calls.filter((body) => body.action === "prepare").length,
    1,
  );
  state.get("close").listeners.click();
  state.finishDownload();
  await pending;
  assert.equal(state.dialog.open, false);
});

test("empty server category targets are defaulted and reviewed before enabling apply", async () => {
  const state = setup({
    preview: async (body) => ({
      ready: Boolean(body.categories.happy),
      counts: {},
      rows: [
        {
          path: "memes/happy/new.png",
          local_path: "memes/happy/new.png",
          kind: "added",
          action: "add",
          selectable: true,
          local_exists: false,
        },
      ],
      local_categories: { happy: "Local" },
      plan_token: body.categories.happy || "unresolved",
      categories: [
        {
          source: "happy",
          target: body.categories.happy || "",
          local_description: "Local",
          remote_description: "Remote",
        },
      ],
    }),
  });
  await state.component.open({ id: "pack" }, {});
  assert.equal(state.calls.at(-1).categories.happy, "happy");
  const group = state.get("images").children[0].children[1];
  assert.equal(group.children[1].value, "happy");
  assert.match(group.children[0].textContent, /\n本地：.*\n上游：/);
  assert.equal(state.get("apply").disabled, false);
});

test("old installs default to upstream merge and submit the reviewed token", async () => {
  const state = setup({ baseline: false });
  await state.component.open({ id: "pack" }, {});
  assert.equal(state.get("strategy").value, "merge_upstream");
  assert.equal(state.get("apply").disabled, false);
  await state.get("apply").listeners.click();
  const request = state.calls.find((body) => body.action === "apply");
  assert.equal(request.strategy, "merge_upstream");
  assert.equal(request.plan_token, "reviewed");
  assert.equal(state.storage.size, 0);
});

test("upstream merge defaults to upstream conflicts and deletions", async () => {
  const state = setup({
    preview: (body) => ({
      ready: true,
      counts: {},
      categories: [],
      rows: [
        {
          path: "memes/happy/conflict.png",
          local_path: "memes/happy/conflict.png",
          kind: "conflict",
          local_exists: true,
          selectable: true,
        },
        {
          path: "memes/happy/delete.png",
          local_path: "memes/happy/delete.png",
          kind: "upstream_deleted",
          local_exists: true,
          action: body.keep_deleted.includes("memes/happy/delete.png")
            ? "keep"
            : "delete",
        },
      ],
      plan_token: body.keep_deleted.length ? "kept" : "upstream-reviewed",
    }),
  });
  await state.component.open({ id: "pack" }, {});
  assert.equal(state.get("strategy").value, "merge_upstream");
  const [group] = state.get("images").children;
  const [conflict] = group.children[1].children;
  assert.equal(conflict.children[3].value, "upstream");
  const deletion = group.children[1].children[1];
  const deletionCheck = deletion.children.at(-1).children[0];
  assert.equal(deletionCheck.checked, true);
  assert.equal(deletion.children[2].children.length, 1);
  deletionCheck.checked = false;
  deletionCheck.listeners.change();
  await tick();
  assert.equal(state.get("images").children[0], group);
  assert.equal(state.get("fields").disabled, false);
  await state.get("apply").listeners.click();
  const request = state.calls.find((body) => body.action === "apply");
  assert.equal(request.strategy, "merge_upstream");
  assert.deepEqual(Array.from(request.keep_deleted), [
    "memes/happy/delete.png",
  ]);
  assert.equal(request.plan_token, "kept");
});

test("image selection is submitted with the reviewed update plan", async () => {
  const state = setup({
    preview: (body) => ({
      ready: true,
      counts: {},
      categories: [],
      rows: [
        {
          path: "memes/happy/new.png",
          local_path: "memes/happy/new.png",
          kind: "added",
          action: body.skip.includes("memes/happy/new.png") ? "keep" : "add",
          selectable: true,
          local_exists: false,
        },
      ],
      plan_token: body.skip.includes("memes/happy/new.png")
        ? "skipped"
        : "selected",
    }),
  });
  await state.component.open({ id: "pack" }, {});
  const card = state.get("images").children[0].children[1].children[0];
  const check = card.children.at(-1).children[0];
  assert.equal(check.checked, true);
  check.checked = false;
  check.listeners.change();
  await tick();
  assert.equal(state.get("images").children[0].children[1].children[0], card);
  assert.equal(state.get("fields").disabled, false);
  await state.get("apply").listeners.click();
  const request = state.calls.find((body) => body.action === "apply");
  assert.deepEqual(Array.from(request.skip), ["memes/happy/new.png"]);
  assert.equal(request.plan_token, "skipped");
});

test("cancel stops a running check without opening a preview or applying changes", async () => {
  const state = setup({ runningCheck: true });
  const pending = state.component.open({ id: "pack" }, {});
  await tick();
  assert.equal(state.get("cancel").hidden, false);
  await state.get("cancel").listeners.click();
  await pending;
  assert.equal(
    state.calls.find((body) => body.action === "cancel").job_id,
    "job",
  );
  assert.equal(
    state.calls.some((body) => ["preview", "apply"].includes(body.action)),
    false,
  );
  assert.match(state.get("status").textContent, /已取消/);
  assert.equal(state.storage.size, 0);
  assert.equal(state.get("cancel").hidden, true);
});

test("full replacement remains disabled until explicit acknowledgment", async () => {
  const state = setup();
  await state.component.open({ id: "pack" }, {});
  state.get("strategy").value = "replace";
  state.get("strategy").listeners.change();
  await tick();
  assert.equal(state.get("warning").hidden, false);
  assert.equal(state.get("apply").disabled, true);
  await state.get("apply").listeners.click();
  assert.equal(
    state.calls.some((body) => body.action === "apply"),
    false,
  );
  state.get("ack").checked = true;
  state.get("ack").listeners.change();
  await state.get("apply").listeners.click();
  assert.equal(state.calls.at(-1).acknowledge_replace, true);
});

test("stale preview cannot enable a newer unresolved selection", async () => {
  let resolveOld;
  const state = setup({
    preview: async (body) =>
      body.strategy === "replace"
        ? new Promise((resolve) => {
            resolveOld = resolve;
          })
        : {
            ready: false,
            counts: {},
            categories: [],
            rows: [],
            plan_token: "new",
          },
  });
  await state.component.open({ id: "pack" }, {});
  state.get("strategy").value = "replace";
  state.get("strategy").listeners.change();
  await tick();
  state.get("strategy").value = "merge";
  state.get("strategy").listeners.change();
  await tick();
  resolveOld({
    ready: true,
    counts: {},
    categories: [],
    rows: [],
    plan_token: "old",
  });
  await tick();
  assert.equal(state.get("apply").disabled, true);
});

test("restoration requires acknowledgment and uses its own preview token", async () => {
  const state = setup();
  await state.component.open(
    { id: "pack" },
    { backups: [{ id: "backup", created_at: 1, version: "1" }] },
    true,
  );
  assert.equal(state.get("apply").disabled, true);
  state.restoreAck.checked = true;
  state.restoreAck.listeners.change({ target: state.restoreAck });
  await state.get("apply").listeners.click();
  const request = state.calls.find((body) => body.action === "restore");
  assert.equal(request.acknowledge_restore, true);
  assert.equal(request.plan_token, "restore-reviewed");
});

test("refreshing installation state removes deleted pack IDs and rejects late responses", async () => {
  const source = await readFile(
    new URL("../../pages/app/catalog/script.js", import.meta.url),
    "utf8",
  );
  const start = source.indexOf("  async function refreshInstalledSet()");
  const end = source.indexOf("  function readPacksFromCache()", start);
  let installed = [{ id: "deleted" }],
    pending = null;
  const context = vm.createContext({
    apiGet: async (endpoint) =>
      endpoint === "packs"
        ? pending
          ? pending
          : { packs: installed }
        : { packs: {} },
    installedStatus: { classList: { add() {}, remove() {} } },
    catalogFilter: {},
    addLog() {},
  });
  vm.runInContext(
    `let installedRequest=0, installedPackIds=new Set(), installedStateKnown=false, updateStates={}; ${source.slice(
      start,
      end,
    )}; function ids(){return [...installedPackIds];}`,
    context,
  );
  await context.refreshInstalledSet();
  assert.deepEqual(Array.from(context.ids()), ["deleted"]);
  let resolveOld;
  pending = new Promise((resolve) => {
    resolveOld = resolve;
  });
  const oldRefresh = context.refreshInstalledSet();
  pending = null;
  installed = [];
  await context.refreshInstalledSet();
  resolveOld({ packs: [{ id: "deleted" }] });
  await oldRefresh;
  assert.deepEqual(Array.from(context.ids()), []);
});

test("updatable packs appear in both update and source sections, then hide when none remain", async () => {
  const html = await readFile(
    new URL("../../pages/app/catalog/index.html", import.meta.url),
    "utf8",
  );
  assert.match(html, /id="updates-zone" class="panel catalog-list hidden"/);
  const source = await readFile(
    new URL("../../pages/app/catalog/script.js", import.meta.url),
    "utf8",
  );
  const start = source.indexOf("  function renderCatalog()");
  const end = source.indexOf("  async function fetchIndex()", start);
  const grid = () => ({
    children: [],
    classList: { toggle() {} },
    replaceChildren() {
      this.children = [];
    },
    appendChild(child) {
      this.children.push(child);
    },
  });
  const updatesZone = {
    hidden: true,
    classList: {
      toggle(_, hidden) {
        updatesZone.hidden = hidden;
      },
    },
  };
  const context = vm.createContext({
    cachedIndex: true,
    catalogSearch: { value: "" },
    catalogFilter: { value: "all" },
    readPacksFromCache: () => [
      { id: "official", official: true },
      { id: "community", official: false },
    ],
    installedStateKnown: true,
    installedPackIds: new Set(["official", "community"]),
    updateStates: { official: { available: true } },
    updatesZone,
    updatesGrid: grid(),
    updatesPackCount: {},
    officialGrid: grid(),
    officialPackCount: {},
    communityGrid: grid(),
    communityPackCount: {},
    isOfficialPack: (pack) => pack.official,
    createPackCard: (pack) => pack.id,
    document: { createElement: () => ({}) },
  });
  vm.runInContext(source.slice(start, end), context);
  context.renderCatalog();
  assert.equal(updatesZone.hidden, false);
  assert.deepEqual(Array.from(context.updatesGrid.children), ["official"]);
  assert.deepEqual(Array.from(context.officialGrid.children), ["official"]);
  assert.deepEqual(Array.from(context.communityGrid.children), ["community"]);
  context.updateStates = {};
  context.renderCatalog();
  assert.equal(updatesZone.hidden, true);
  assert.equal(context.updatesGrid.children.length, 0);
});
