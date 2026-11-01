import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(
  new URL("../../pages/app/settings/config.js", import.meta.url),
  "utf8",
);
const start = source.indexOf(
  '          fetchModels.addEventListener("click", async () => {',
);
const handlerSource = source.slice(
  start,
  source.indexOf("          controlBox.append(fetchModels);", start),
);

function fixture(apiPost) {
  let click;
  const button = {
    addEventListener: (_event, handler) => {
      click = handler;
    },
    disabled: false,
  };
  const help = { textContent: "" };
  const choices = {
    items: [],
    replaceChildren(...items) {
      this.items = items;
    },
    append(item) {
      this.items.push(item);
    },
  };
  const control = { value: "my-manual-model", isConnected: true };
  const address = {
    value: "https://example.test/v1",
    focused: false,
    focus() {
      this.focused = true;
    },
  };
  const calls = [];
  const context = vm.createContext({
    fetchModels: button,
    help,
    choices,
    control,
    controls: new Map([["decision_service.base_url", address]]),
    api: {
      apiPost: (...args) => {
        calls.push(args);
        return apiPost(...args);
      },
    },
    modelListRequest: 0,
    decisionModels: [],
    decisionModelsBaseUrl: "",
    Option: class {
      constructor(label, value) {
        this.label = label;
        this.value = value;
      }
    },
  });
  vm.runInContext(handlerSource, context);
  return {
    context,
    button,
    help,
    choices,
    control,
    address,
    calls,
    click: () => click(),
  };
}

test("discovery offers exact catalog IDs without changing manual input or saving configuration", async () => {
  const state = fixture(async () => ({
    models: [{ id: "exact-id", name: "Readable Name" }],
  }));
  await state.click();
  assert.equal(state.calls.length, 1);
  assert.equal(state.calls[0][0], "settings/decision-models");
  assert.equal(
    JSON.stringify(state.calls[0][1]),
    JSON.stringify({ base_url: "https://example.test/v1" }),
  );
  assert.equal(state.choices.items[1].value, "exact-id");
  assert.equal(state.control.value, "my-manual-model");
  assert.equal(state.button.disabled, false);
  assert.match(state.help.textContent, /1 个模型/);
});

test("failed and empty catalogs keep manual IDs editable", async () => {
  for (const apiPost of [
    async () => {
      throw new Error("Service unavailable");
    },
    async () => ({ models: [] }),
  ]) {
    const state = fixture(apiPost);
    await state.click();
    assert.equal(state.control.value, "my-manual-model");
    assert.equal(state.button.disabled, false);
    assert.match(state.help.textContent, /填写模型 ID/);
  }
});

test("a late catalog cannot populate the list after an address edit or rerender", async () => {
  let resolve;
  const state = fixture(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  const pending = state.click();
  state.context.modelListRequest++;
  state.help.textContent = "Address changed";
  resolve({ models: [{ id: "stale", name: "Stale" }] });
  await pending;
  assert.equal(state.choices.items.length, 0);
  assert.equal(state.help.textContent, "Address changed");
  assert.equal(state.control.value, "my-manual-model");
});

test("missing address does not start discovery and typing during discovery is preserved", async () => {
  let resolve;
  const state = fixture(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  state.address.value = "";
  await state.click();
  assert.equal(state.calls.length, 0);
  assert.equal(state.address.focused, true);
  state.address.value = "https://example.test/v1";
  const pending = state.click();
  state.control.value = "typed-while-loading";
  resolve({ models: [{ id: "catalog-id" }] });
  await pending;
  assert.equal(state.control.value, "typed-while-loading");
});

test("choosing a catalog entry writes its exact ID through the ordinary input handler", () => {
  let change;
  let dispatched;
  const choices = {
    value: "exact-catalog-id",
    addEventListener: (_name, handler) => {
      change = handler;
    },
  };
  const control = {
    value: "manual-id",
    dispatchEvent: (event) => {
      dispatched = event;
    },
  };
  const context = vm.createContext({ choices, control, Event });
  const start = source.indexOf(
    '        choices.addEventListener("change", () => {',
  );
  vm.runInContext(
    source.slice(
      start,
      source.indexOf('        controlBox.classList.add("model-picker")', start),
    ),
    context,
  );
  change();
  assert.equal(control.value, "exact-catalog-id");
  assert.equal(dispatched.type, "input");
  assert.equal(dispatched.bubbles, true);
  assert.equal(choices.value, "");
});
