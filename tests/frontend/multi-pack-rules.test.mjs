import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(
  new URL("../../pages/app/settings/script.js", import.meta.url), "utf8",
);
const functions = source.slice(
  source.indexOf("  function getRulePackIds("),
  source.indexOf("  function getTargetSuggestions("),
);
const save = source.slice(
  source.indexOf("  async function saveRules("),
  source.indexOf("  async function exportBackup("),
);

test("saving an edited rule preserves ordered pack IDs and legacy rules", async () => {
  const submitted = [];
  const sandbox = {
    rules: [
      { id: "session", scope: "session", target: "chat", pack_id: "stale", pack_ids: ["pack-b", "pack-a", "pack-b"] },
      { id: "default", scope: "default", pack_id: "pack-a" },
    ],
    rulesBusy: false, rulesLoaded: true, savedRulesSnapshot: "",
    saveRulesBtn: {},
    renderRulesValidation: () => true,
    updateRulesState() {}, setLoading() {}, clearLoading() {},
    ensureDefaultRuleAtEnd() {}, renderRules() {}, addLog() {},
    apiPost: async (path, payload) => {
      submitted.push({ path, payload: JSON.parse(JSON.stringify(payload)) });
      return payload;
    },
  };
  vm.createContext(sandbox);
  await vm.runInContext(`${functions}\n${save}\nsaveRules()`, sandbox);
  assert.equal(submitted.length, 1);
  assert.deepEqual(submitted[0], {
    path: "settings/rules",
    payload: { rules: [
      { id: "session", scope: "session", target: "chat", pack_id: "pack-b", pack_ids: ["pack-b", "pack-a"] },
      { id: "default", scope: "default", pack_id: "pack-a" },
    ] },
  });
});

class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.dataset = {};
    this.listeners = {};
    this.value = "";
  }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.append(child); }
  replaceChildren() { this.children = []; }
  setAttribute(key, value) { this[key] = value; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  focus() { this.focused = true; }
  querySelector(selector) {
    const action = selector.match(/data-pack-action="([^"]+)"/)[1];
    return this.children.find((child) => child.dataset.packAction === action);
  }
}

function setupPicker(rule, installedPacks = [
  { id: "pack-a", name: "日常" }, { id: "pack-b", name: "周末" },
]) {
  const sandbox = {
    rules: [rule], installedPacks,
    document: { createElement: (tag) => new Element(tag) },
    updateRuleFromInput(index, key, value) { sandbox.rules[index][key] = value; },
  };
  vm.createContext(sandbox);
  const picker = vm.runInContext(`${functions}\ncreateRulePackPicker(rules[0], 0);`, sandbox);
  const [status, selected, choices] = picker.children;
  const [summary, search, options, empty] = choices.children;
  const toggle = (id, checked) => {
    const checkbox = options.children.find((row) => row.children[0].value === id).children[0];
    checkbox.checked = checked;
    checkbox.listeners.change();
  };
  const click = (id, action) => {
    const row = selected.children.find((item) => item.dataset.packId === id);
    row.children[2].querySelector(`[data-pack-action="${action}"]`).listeners.click();
  };
  return { rule, status, selected, choices, summary, search, options, empty, toggle, click };
}

test("clicking checkboxes adds packs in order, reordering changes the primary, and removal keeps remaining packs", () => {
  const state = setupPicker({ pack_id: "pack-b" });
  state.toggle("pack-a", true);
  assert.deepEqual(Array.from(state.rule.pack_ids), ["pack-b", "pack-a"]);
  state.click("pack-a", "up");
  assert.equal(state.rule.pack_id, "pack-a");
  assert.deepEqual(Array.from(state.rule.pack_ids), ["pack-a", "pack-b"]);
  assert.equal(state.selected.children[0].children[1].children[2].textContent, "主表情包");
  state.click("pack-a", "down");
  assert.equal(state.rule.pack_id, "pack-b");
  state.click("pack-b", "remove");
  assert.equal(state.rule.pack_id, "pack-a");
  assert.deepEqual(Array.from(state.rule.pack_ids), ["pack-a"]);
  assert.equal(state.options.children[1].children[0].checked, false);
  state.toggle("pack-a", false);
  assert.equal(state.rule.pack_id, "");
  assert.equal(state.selected.hidden, true);
  assert.match(state.status.textContent, /至少一个/);
});

test("missing secondary packs remain visible and can be removed without losing other selections", () => {
  const state = setupPicker({ pack_id: "pack-b", pack_ids: ["pack-b", "missing"] });
  assert.equal(state.selected.children[1].className, "rule-pack-row missing");
  assert.equal(state.selected.children[1].children[1].children[1].textContent, "missing");
  state.click("missing", "remove");
  assert.deepEqual(Array.from(state.rule.pack_ids), ["pack-b"]);
});

test("search matches names and IDs without changing selection, and Escape returns focus", () => {
  const state = setupPicker({ pack_id: "pack-b" });
  for (const query of ["日常", "PACK-A"]) {
    state.search.value = query;
    state.search.listeners.input();
    assert.equal(state.options.children[0].hidden, false);
    assert.equal(state.options.children[1].hidden, true);
  }
  state.search.value = "no-match";
  state.search.listeners.input();
  assert.equal(state.empty.hidden, false);
  assert.equal(state.rule.pack_id, "pack-b");
  state.choices.open = true;
  state.choices.listeners.keydown({ key: "Escape", preventDefault() {} });
  assert.equal(state.choices.open, false);
  assert.equal(state.summary.focused, true);
  assert.match(setupPicker({ pack_id: "" }, []).empty.textContent, /先前往资源广场安装/);
});
