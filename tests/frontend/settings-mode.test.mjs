import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";
import { describeSetup } from "../../pages/app/settings/setup-state.js";

const source = await readFile(
  new URL("../../pages/app/settings/config.js", import.meta.url),
  "utf8",
);
const declarations = source.slice(
  source.indexOf("  const combinationNames = {"),
  source.indexOf("  const labels = {"),
);
const { groups } =
  vm.runInNewContext(
    `${declarations}; ({groups})`,
  );
const providers = {
  chat: [
    { id: "chat-main", model: "Chat" },
    { id: "vision", model: "Vision" },
  ],
  embedding: [{ id: "embed", model: "Embedding" }],
};
const defaults = {
  "semantic.enabled": false,
  "generation.emotion.llm.enabled": false,
  "generation.emotion.llm.backend": "chat",
  "semantic.query.source": "generated",
  "auto_collect.enabled": false,
  "auto_collect.manual_review": true,
  "decision_service.base_url": "https://system-one.dev/v1",
};

const describe = (values = {}, options = {}) => describeSetup(
  { ...defaults, ...values },
  { providers, tokenConfigured: false,
    defaultPack: { id: "pack", name: "默认包" },
    semanticStatus: { index_ready: true, semantic_caption_complete: true }, ...options },
);

test("active consumers require their own models while inactive drafts stay editable", () => {
  assert.equal(describe().selectionIssues.length, 0);
  const values = { "generation.emotion.llm.enabled": true,
    "generation.emotion.llm.backend": "decision" };
  assert.deepEqual(describe(values).selectionIssues.map(item => item.path),
    ["decision_service.token", "decision_service.model"]);
  assert.equal(describe({ ...values, "decision_service.model": "model" },
    { tokenConfigured: true }).selectionIssues.length, 0);
  const semantic = { "semantic.enabled": true, "generation.emotion.llm.enabled": true,
    "semantic.query.source": "reply_text", "semantic.query.provider_id": "hidden-missing",
    "semantic.embedding_provider_id": "embed" };
  assert.equal(describe(semantic).selectionIssues.length, 0);
  assert.equal(describe(semantic, { semanticStatus: null }).preparationWarning, true);
  assert.equal(describe({ ...semantic, "generation.emotion.llm.provider_id": "manual" },
    { providers: {chat: [], embedding: []}, providerListErrors: ["chat", "embedding"] }
  ).selectionIssues.length, 0);
  const secret = "private-secret";
  const feedback = describe({ ...values, "decision_service.token": secret,
    "decision_service.base_url": `https://user:${secret}@example.test` });
  assert.ok(!JSON.stringify(feedback).includes(secret));
});

test("every persisted field has exactly one editable group", async () => {
  const schema = JSON.parse(
    await readFile(new URL("../../_conf_schema.json", import.meta.url), "utf8"),
  );
  function visit(items, prefix = "") {
    for (const [key, spec] of Object.entries(items)) {
      const path = prefix ? `${prefix}.${key}` : key;
      if (spec.type === "object") visit(spec.items, path);
      else
        assert.equal(
          groups.filter((group) =>
            group.prefixes.some((part) =>
              part.endsWith(".") ? path.startsWith(part) : path === part,
            ),
          ).length,
          1,
          path,
        );
    }
  }
  visit(schema);

});

test("脱敏账户允许再次保存，清除或错误草稿仍被拦截", () => {
  const fields = new Map(Object.entries({
    ...defaults,
    "generation.emotion.llm.enabled": true,
    "generation.emotion.llm.backend": "decision",
    "decision_service.provider": "cloudflare",
    "decision_service.account_id": "",
    "decision_service.token": "",
    "decision_service.model": "clef-flash",
  }).map(([path, value]) => [path, {value, configured: true}]));
  const start = source.indexOf("    const setup = describeSetup(", source.indexOf('form.addEventListener("submit"'));
  const end = source.indexOf("    configState.busy = true;", start);
  for (const [path, value, expected] of [
    ["decision_service.timeout", 20, ""],
    ["decision_service.account_id", "", "decision_service.account_id"],
    ["decision_service.account_id", "   ", "decision_service.account_id"],
    ["decision_service.account_id", "bad-id", "decision_service.account_id"],
    ["decision_service.token", "", "decision_service.token"],
  ]) {
    let error = "";
    vm.runInNewContext(`(() => {${source.slice(start, end)}})()`, {
      fields, changes: new Map([[path, value]]), providers, describeSetup,
      showFieldError: (path) => { error = path; },
    });
    assert.equal(error, expected);
  }
});

test("dimension edits preserve hidden drafts and revert the original persisted flags", () => {
  const paths = ["semantic.enabled", "generation.emotion.llm.enabled"];
  for (const savedSemantic of [false, true])
    for (const savedPost of [false, true]) {
      const fields = new Map(paths.map((path, index) =>
        [path, { value: [savedSemantic, savedPost][index] }]));
      const draft = ["semantic.query.provider_id", "draft-provider"];
      const changes = new Map([draft]);
      const method = {
        name: "selection-method",
        checked: true,
        value: savedSemantic ? "semantic" : "category",
      };
      const timing = {
        name: "selection-agent",
        checked: true,
        value: savedPost ? "assisted" : "reply",
      };
      const handlers = [];
      method.addEventListener = timing.addEventListener = (_type, handler) =>
        handlers.push(handler);
      const context = vm.createContext({
        dimensionInputs: [method, timing],
        loaded: true,
        configState: { busy: false },
        modePaths: paths,
        controls: new Map(paths.map((path) => [path, {}])),
        fields,
        changes,
        updateState() {},
        updateView() {},
        form: {
          querySelector: (selector) =>
            selector.includes("selection-method") ? method : timing,
        },
      });
      const start = source.lastIndexOf("  for (const input of dimensionInputs) {");
      vm.runInContext(
        source.slice(
          start,
          source.indexOf("  function applyLocationHash()", start),
        ),
        context,
      );
      for (const [index, input, changed, original] of [
        [0, method, savedSemantic ? "category" : "semantic", savedSemantic ? "semantic" : "category"],
        [1, timing, savedPost ? "reply" : "assisted", savedPost ? "assisted" : "reply"],
      ]) {
        input.value = changed;
        handlers[index]();
        assert.equal(changes.get(paths[index]), !fields.get(paths[index]).value);
        assert.equal(changes.has(paths[1 - index]), false);
        input.value = original;
        handlers[index]();
        assert.deepEqual([...changes], [draft]);
      }
    }
});
