import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const script = (
  await readFile(
    new URL("../../pages/app/a_manage/pack-transfer.js", import.meta.url),
    "utf8",
  )
).replace("export function", "function");
const tick = () => new Promise((resolve) => setImmediate(resolve));

class Element {
  constructor() {
    this.children = [];
    this.listeners = {};
    this.value = "";
    this.disabled = false;
    this.open = false;
  }
  addEventListener(name, callback) {
    this.listeners[name] = callback;
  }
  append(...children) {
    this.children.push(...children);
  }
  replaceChildren(...children) {
    this.children = children;
    this.value = children[0]?.value || "";
  }
  add(option) {
    this.children.push(option);
    if (this.children.length === 1) this.value = option.value;
  }
  setAttribute(name, value) {
    this[name] = value;
  }
  get selectedOptions() {
    return this.children.filter((option) => option.value === this.value);
  }
  showModal() {
    this.open = true;
  }
  close() {
    this.open = false;
    this.listeners.close?.();
  }
}

function setup(apiPost) {
  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const context = vm.createContext({
    document: { getElementById: get, createElement: () => new Element() },
    Option: class extends Element {
      constructor(text, value) {
        super();
        this.textContent = text;
        this.value = value;
      }
    },
  });
  vm.runInContext(script, context);
  const completed = [],
    busy = [];
  const component = context.setupPackTransfer({
    apiGet: async () => ({
      packs: [{ id: "source" }, { id: "target", name: "Destination" }],
    }),
    apiPost,
    onComplete: async (...args) => completed.push(args),
    onBusy: (value) => busy.push(value),
  });
  return {
    component,
    get: (suffix) => get(`pack-transfer-${suffix}`),
    completed,
    busy,
  };
}

const items = [{ category: "happy", emoji: "a.png" }];
const preview = (ready, token = "token") => ({
  target_categories: { other: "Other" },
  plan: [],
  ready,
  plan_token: token,
});

test("explicit destination submits the captured source and selected category", async () => {
  const calls = [];
  const state = setup(async (_, body) => {
    calls.push(body);
    return body.preview
      ? preview(!!body.target_category)
      : { moved: items, failed: [], warnings: [] };
  });
  await state.component.open("source", items);
  assert.equal(state.get("confirm").disabled, true);
  assert.equal(state.get("pack").children.length, 1);
  state.get("category").value = "other";
  state.get("category").listeners.change();
  await tick();
  assert.equal(state.get("confirm").disabled, false);
  await state.get("confirm").listeners.click();
  const request = calls.at(-1);
  assert.equal(request.preview, false);
  assert.equal(request.source_pack_id, "source");
  assert.equal(request.target_pack_id, "target");
  assert.equal(request.target_category, "other");
  assert.equal(request.mode, "target");
  assert.equal(request.plan_token, "token");
  assert.equal(state.completed.length, 1);
  assert.deepEqual(state.busy, [true, false]);
});

test("late preview cannot enable submission of a superseded plan", async () => {
  let resolveOld;
  const state = setup(async (_, body) =>
    body.mode === "target"
      ? await new Promise((resolve) => {
          resolveOld = resolve;
        })
      : preview(false, "new"),
  );
  const pending = state.component.open("source", items);
  await tick();
  state.get("mode").value = "preserve";
  state.get("mode").listeners.change();
  await tick();
  resolveOld(preview(true, "old"));
  await pending;
  assert.equal(state.get("confirm").disabled, true);
  assert.equal(state.get("category-field").hidden, true);
});

test("failed execution requires a fresh preview and preserves the open dialog", async () => {
  const state = setup(async (_, body) => {
    if (!body.preview) throw new Error("plan changed");
    return preview(true);
  });
  await state.component.open("source", items);
  await state.get("confirm").listeners.click();
  assert.equal(state.get("dialog").open, true);
  assert.equal(state.get("confirm").disabled, true);
  assert.equal(state.get("fields").disabled, false);
  assert.equal(state.completed.length, 0);
  state.get("retry").listeners.click();
  await tick();
  assert.equal(state.get("confirm").disabled, false);
});

test("unresolved conflicts block confirmation and render a category override", async () => {
  const state = setup(async () => ({
    ...preview(false),
    plan: [
      {
        source_category: "happy",
        target_category: "happy",
        count: 1,
        conflict: true,
        source_description: "One",
        target_description: "Two",
      },
    ],
  }));
  await state.component.open("source", items);
  state.get("mode").value = "preserve";
  state.get("mode").listeners.change();
  await tick();
  assert.equal(state.get("confirm").disabled, true);
  const select = state.get("plan").children[0].children.at(-1);
  assert.equal(select["aria-label"], "happy 的目标分类");
  assert.equal(select.children[1].value, "other");
});
