import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(
  new URL("../../pages/app/settings/config.js", import.meta.url),
  "utf8",
);
const page = await readFile(
  new URL("../../pages/app/settings/index.html", import.meta.url),
  "utf8",
);
const declarations = source.slice(
  source.indexOf("  const workflows = {"),
  source.indexOf("  const labels = {"),
);
const {
  workflows,
  workspaceModes,
  workspaceCommon,
  workspaceCollection,
  workspaceStorage,
  groups,
} = vm.runInNewContext(
  `${declarations}; ({ workflows, workspaceModes, workspaceCommon, workspaceCollection, workspaceStorage, groups })`,
);
const collectionSteps = vm.runInNewContext(
  `${source.slice(source.indexOf("  const collectionSteps = ["), source.indexOf("  for (const ["))}; collectionSteps`,
);

test("collection workflow opens the group containing each linked setting", () => {
  for (const [, , groupId, settings] of collectionSteps) {
    const group = groups.find((item) => item.id === groupId);
    assert.ok(group, groupId);
    for (const [path] of settings) {
      assert.ok(group.prefixes.includes(`auto_collect.${path}`), path);
    }
  }
  const recognition = collectionSteps.find((step) => step[2] === "collect-recognition");
  assert.ok(recognition[3].some(([path]) => path === "content_review_enabled"));
  assert.ok(recognition[3].some(([path]) => path === "content_review_prompt"));
});

test("Tool trigger settings belong to Tool mode and default to autonomous calls", async () => {
  const schema = JSON.parse(await readFile(new URL("../../_conf_schema.json", import.meta.url), "utf8"));
  const tool = schema.semantic.items;
  assert.equal(tool.tool_probability_enabled.type, "bool");
  assert.equal(tool.tool_probability_enabled.default, false);
  assert.equal(tool.tool_call_probability.default, 100);
  assert.equal(tool.tool_call_probability.slider.min, 0);
  assert.equal(schema.generation.items.emotion.items.probability.slider.min, 0);
  assert.equal(
    schema.generation.items.emotion.items.explicit_request_bypass_probability.type,
    "bool",
  );
  assert.equal(
    schema.generation.items.emotion.items.explicit_request_bypass_probability.default,
    true,
  );
  const group = groups.find((item) => item.id === "tool-trigger");
  assert.deepEqual([...group.modes], ["semantic-tool"]);
  assert.ok(workspaceModes["semantic-tool"].primary.includes("tool-trigger"));
  const appearance = groups.find((item) => item.id === "appearance");
  assert.ok(
    appearance.prefixes.includes(
      "generation.emotion.explicit_request_bypass_probability",
    ),
  );
});

test("Tool trigger toggle records boolean changes and refreshes dependent visibility", () => {
  const fields = new Map([["semantic.tool_probability_enabled", { type: "bool", value: false }]]);
  const changes = new Map();
  let input;
  let updates = 0;
  const handlerSource = source.slice(source.indexOf('  form.addEventListener("input",'), source.indexOf('  resetPrompts.addEventListener("click",'));
  vm.runInNewContext(handlerSource, {
    form: { addEventListener(type, handler) { assert.equal(type, "input"); input = handler; } },
    fields, changes, updateState() {}, updateView() { updates++; },
  });
  input({ target: { dataset: { configPath: "semantic.tool_probability_enabled" }, checked: true } });
  assert.equal(changes.get("semantic.tool_probability_enabled"), true);
  assert.equal(updates, 1);
  input({ target: { dataset: { configPath: "semantic.tool_probability_enabled" }, checked: false } });
  assert.equal(changes.size, 0);
  assert.equal(updates, 2);
});

test("every mode workflow exposes every configuration group", () => {
  const all = groups.map((group) => group.id);
  assert.equal(new Set(all).size, all.length);
  for (const [mode, { primary, fallback }] of Object.entries(workspaceModes)) {
    assert.equal(workflows[mode].length, 5);
    assert.equal(workflows[mode][0][0], "选择表情包");
    const attached = [
      ...primary,
      ...fallback,
      ...workspaceCommon,
      ...workspaceCollection,
      ...workspaceStorage,
    ];
    assert.equal(new Set(attached).size, attached.length, mode);
    const other = all.filter((id) => !attached.includes(id));
    assert.deepEqual(new Set([...attached, ...other]), new Set(all), mode);
  }
});

test("mode selection precedes the workflow and edits open in dialogs", () => {
  assert.ok(
    page.indexOf('id="selection-mode-panel"') <
      page.indexOf('id="mode-workflow"'),
  );
  assert.ok(page.includes('id="config-flow-dialog"'));
  assert.ok(page.includes('id="rules-flow-dialog"'));
  assert.ok(!page.includes('id="mode-workspace"'));
  assert.ok(source.includes('["表情包使用规则", ["@rules"]]'));
  assert.ok(!source.includes('["导入与备份", ["@backup"]]'));
});

test("dialog edits the original configuration card and restores it on close", () => {
  const original = [];
  let focused = false;
  const card = {
    id: "config-group-appearance",
    before(placeholder) {
      original.splice(original.indexOf(card), 0, placeholder);
      original.splice(original.indexOf(card), 1);
    },
    querySelector() {
      return { focus: () => (focused = true) };
    },
  };
  original.push(card);
  const title = { textContent: "", focus: () => (focused = true) };
  const body = {
    children: [],
    replaceChildren(...children) {
      this.children = children;
    },
  };
  const dialog = {
    open: false,
    showModal() {
      this.open = true;
    },
    close() {
      this.open = false;
    },
  };
  const context = vm.createContext({
    dialogCard: null,
    dialogPlaceholder: null,
    configDialog: dialog,
    configDialogBody: body,
    controls: new Map(),
    document: {
      createComment() {
        return {
          isConnected: true,
          replaceWith(node) {
            original.splice(original.indexOf(this), 1, node);
          },
        };
      },
      getElementById(id) {
        return id === "config-flow-dialog-title" ? title : card;
      },
    },
    updateView() {},
  });
  vm.runInContext(
    `${source.slice(
      source.indexOf("  function restoreDialogCard()"),
      source.indexOf("  function updateView()"),
    )}; openConfigDialog({ id: "appearance", title: "出现时机" }, { dataset: {} });`,
    context,
  );
  assert.equal(dialog.open, true);
  assert.equal(body.children[0], card);
  assert.equal(title.textContent, "出现时机");
  assert.equal(focused, true);
  vm.runInContext("restoreDialogCard()", context);
  assert.equal(dialog.open, false);
  assert.equal(original[0], card);
  assert.equal(body.children.length, 0);
});
