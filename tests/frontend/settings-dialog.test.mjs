import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(
  new URL("../../pages/app/settings/config.js", import.meta.url),
  "utf8",
);
const helpers = source.slice(
  source.indexOf("  function restoreDialogContent()"),
  source.indexOf("  function navigateToSetting("),
);

function fixture() {
  const home = [];
  const detach = (node) => {
    if (node.home) node.home.splice(node.home.indexOf(node), 1);
    node.home = null;
  };
  const body = {
    children: [],
    replaceChildren(...nodes) {
      for (const node of [...this.children]) detach(node);
      for (const node of nodes) {
        detach(node);
        node.home = this.children;
        this.children.push(node);
      }
    },
  };
  const card = {
    id: "config-group-semantic-models", tagName: "DETAILS", hidden: true, home,
    input: { value: "draft-provider" },
    before(marker) {
      marker.home = this.home;
      this.home.splice(this.home.indexOf(this), 0, marker);
    },
  };
  const rules = {
    ...card,
    id: "rules",
    tagName: "SECTION",
    input: { value: "unsaved-rule" },
  };
  home.push(card, rules);
  let focused = false;
  const hidden = new Set();
  const context = vm.createContext({
    dialogContent: null, dialogPlaceholder: null, configDialogBody: body,
    configMask: {
      classList: { add: (value) => hidden.add(value) },
      setAttribute() {},
    },
    configState: { busy: false },
    active: "models", activeFlowGroup: "semantic-models",
    dialogFieldPath: "", pendingGroupAnchor: "",
    dialogOrigin: {
      isConnected: true,
      closest: () => null,
      focus() { focused = true; },
    },
    dialogOriginGroup: "semantic-models", dialogOriginTab: "",
    form: { querySelectorAll: () => [] },
    mobileNavigation: { getClientRects: () => [] },
    changes: new Map([["semantic.vision_provider_id", "draft-provider"]]),
    location: {
      pathname: "/settings/index.html",
      search: "?asset_token=fixture",
      hash: "#config-group-semantic-models",
    },
    history: {
      replaceState: (_state, _title, url) => {
        context.location.hash = url.slice(url.indexOf("#"));
      },
    },
    updateView() {}, updateState() {},
    document: {
      createComment() {
        return {
          home: null,
          get isConnected() {
            return Boolean(this.home);
          },
          replaceWith(node) {
            const owner = this.home;
            detach(node);
            owner.splice(owner.indexOf(this), 1, node);
            node.home = owner;
            this.home = null;
          },
        };
      },
    },
  });
  vm.runInContext(helpers, context);
  return { context, home, body, card, rules, hidden, focused: () => focused };
}

test("switching modal panels restores the same components and preserves input drafts", () => {
  const { context, home, body, card, rules } = fixture();
  const originalInput = card.input;
  context.mountDialogContent(card);
  assert.equal(body.children[0], card);
  assert.equal(card.open, true);
  assert.equal(home.includes(card), false);
  card.input.value = "edited-in-dialog";
  context.mountDialogContent(rules);
  assert.equal(home[0], card);
  assert.equal(card.input, originalInput);
  assert.equal(card.input.value, "edited-in-dialog");
  assert.equal(body.children[0], rules);
  assert.equal(rules.input.value, "unsaved-rule");
  context.restoreDialogContent();
  assert.deepEqual(home, [card, rules]);
  assert.equal(body.children.length, 0);
  assert.equal(context.changes.get("semantic.vision_provider_id"), "draft-provider");
});

test("closing waits for an active save, then returns focus while preserving drafts", () => {
  const state = fixture();
  state.context.mountDialogContent(state.card);
  state.context.configState.busy = true;
  state.context.closeSettingsDialog();
  assert.equal(state.body.children[0], state.card);
  assert.equal(state.hidden.has("hidden"), false);
  state.context.configState.busy = false;
  state.context.closeSettingsDialog();
  assert.equal(state.home[0], state.card);
  assert.equal(state.hidden.has("hidden"), true);
  assert.equal(state.focused(), true);
  assert.equal(state.context.location.hash, "#models");
  assert.equal(state.context.changes.size, 1);
});
