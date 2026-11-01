import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../../pages/app/settings/config.js", import.meta.url), "utf8");
const start = source.indexOf('          fetchModels.addEventListener("click", async () => {');
const handler = source.slice(start, source.indexOf("          controlBox.append(fetchModels);", start));

function fixture(apiPost) {
  let click;
  const calls = [];
  const control = { value: "manual-id", isConnected: true };
  const address = { value: "https://example.test/v1", focus() {} };
  const context = vm.createContext({
    fetchModels: { addEventListener: (_, fn) => { click = fn; }, disabled: false },
    help: { textContent: "" }, control,
    choices: {
      items: [],
      replaceChildren(...items) { this.items = items; },
      append(item) { this.items.push(item); },
    },
    controls: new Map([["decision_service.base_url", address]]),
    api: { apiPost: (...args) => { calls.push(args); return apiPost(...args); } },
    modelListRequest: 0, decisionModels: [], decisionModelsBaseUrl: "",
    Option: class { constructor(label, value) { Object.assign(this, { label, value }); } },
  });
  vm.runInContext(handler, context);
  return { context, address, calls, click: () => click() };
}

for (const outcome of ["catalog", "empty", "error"]) {
  test("model discovery preserves manual input: " + outcome, async () => {
    const state = fixture(async () => {
      if (outcome === "error") throw new Error("unavailable");
      return { models: outcome === "empty" ? [] : [{ id: "exact-id", name: "Readable" }] };
    });
    await state.click();
    assert.deepEqual(JSON.parse(JSON.stringify(state.calls)), [["settings/decision-models", { base_url: state.address.value }]]);
    assert.equal(state.context.control.value, "manual-id");
    assert.equal(state.context.fetchModels.disabled, false);
    if (outcome === "catalog") assert.equal(state.context.choices.items[1].value, "exact-id");
    else assert.match(state.context.help.textContent, /填写模型 ID/);
  });
}

for (const mutation of ["address", "rerender", "typing"]) {
  test("pending model discovery handles " + mutation, async () => {
    let resolve;
    const state = fixture(() => new Promise(done => { resolve = done; }));
    state.address.value = "";
    await state.click();
    assert.equal(state.calls.length, 0);
    state.address.value = "https://example.test/v1";
    const pending = state.click();
    if (mutation === "address") state.context.modelListRequest++;
    if (mutation === "rerender") state.context.control.isConnected = false;
    if (mutation === "typing") state.context.control.value = "typed-id";
    resolve({ models: [{ id: "catalog-id" }] });
    await pending;
    assert.equal(state.context.control.value, mutation === "typing" ? "typed-id" : "manual-id");
    if (mutation !== "typing") assert.equal(state.context.choices.items.length, 0);
  });
}
