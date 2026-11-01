import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";
import { describeSetup } from "../../pages/app/settings/setup-state.js";

const source = await readFile(
  new URL("../../pages/app/settings/config.js", import.meta.url),
  "utf8",
);
const page = await readFile(
  new URL("../../pages/app/settings/index.html", import.meta.url),
  "utf8",
);
const declarations = source.slice(
  source.indexOf("  const combinationNames = {"),
  source.indexOf("  const labels = {"),
);
const { groups, combinationNames, combinationKey, selectionWorkflow } =
  vm.runInNewContext(
    `${declarations}; ({groups, combinationNames, combinationKey, selectionWorkflow})`,
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

test("direct sends have a default-on switch separate from automatic attachment gates", async () => {
  const schema = JSON.parse(
    await readFile(new URL("../../_conf_schema.json", import.meta.url), "utf8"),
  );
  const emotion = schema.generation.items.emotion.items;
  assert.equal(emotion.send_tool_enabled.type, "bool");
  assert.equal(emotion.send_tool_enabled.default, true);
  assert.ok(
    groups.find((group) => group.id === "appearance").prefixes.includes(
      "generation.emotion.send_tool_enabled",
    ),
  );
  assert.match(emotion.probability.hint, /不限制.*send_meme/);
  assert.match(emotion.max_memes_per_message.hint, /不限制.*send_meme/);
});
const describe = (values = {}, options = {}) =>
  describeSetup(
    { ...defaults, ...values },
    {
      providers,
      tokenConfigured: false,
      defaultPack: { id: "pack", name: "默认包" },
      semanticStatus: { index_ready: true, semantic_caption_complete: true },
      ...options,
    },
  );

test("default and reply-time semantic choices do not require a decision service", () => {
  assert.equal(describe().selectionIssues.length, 0);
  assert.match(describe().requestNote, /无需额外/);
  const tool = describe({
    "semantic.enabled": true,
    "generation.emotion.llm.backend": "decision",
  });
  assert.equal(tool.consumers.length, 0);
  assert.equal(tool.selectionIssues.length, 0);
  assert.equal(tool.rows[0].group, "tool-trigger");
  assert.match(tool.requestNote, /工具/);
});

test("post-reply setup identifies chat and decision backends independently", () => {
  const values = {
    "generation.emotion.llm.enabled": true,
    "generation.emotion.llm.backend": "decision",
  };
  assert.match(
    describe({ ...values, "generation.emotion.llm.backend": "chat" })
      .requestNote,
    /聊天模型选分类/,
  );
  const setup = describe(values);
  assert.deepEqual(
    setup.selectionIssues.map((item) => item.path),
    ["decision_service.token", "decision_service.model"],
  );
  assert.deepEqual(setup.consumers, ["辅助配图"]);
  assert.ok(
    setup.selectionIssues.every((item) => item.group === "decision-service"),
  );
  assert.match(setup.requestNote, /决策服务选分类/);
});

test("generated queries inherit the correct chat model for each selection backend", () => {
  const values = {
    "semantic.enabled": true,
    "generation.emotion.llm.enabled": true,
    "generation.emotion.llm.provider_id": "chat-main",
  };
  const chat = describe(values);
  assert.match(
    chat.rows.find((item) => item.label === "检索词").value,
    /chat-main/,
  );
  assert.match(chat.requestNote, /检索词生成.*聊天模型选图/);
  const decision = describe(
    {
      ...values,
      "generation.emotion.llm.backend": "decision",
      "decision_service.model": "jev",
    },
    { tokenConfigured: true },
  );
  assert.match(
    decision.rows.find((item) => item.label === "检索词").value,
    /跟随本轮主模型/,
  );
  assert.equal(decision.selectionIssues.length, 0);
  assert.match(decision.requestNote, /检索词生成.*决策服务选图/);
});

test("direct reply queries ignore hidden unavailable query providers", () => {
  const setup = describe({
    "semantic.enabled": true,
    "generation.emotion.llm.enabled": true,
    "semantic.query.source": "reply_text",
    "semantic.query.provider_id": "missing",
  });
  assert.equal(setup.selectionIssues.length, 0);
  assert.equal(
    setup.rows.find((item) => item.label === "检索词").value,
    "直接使用可见回复文本",
  );
  assert.ok(!setup.requestNote.includes("检索词生成"));
});

test("provider feedback is actionable and does not reuse old readiness after embedding changes", () => {
  const setup = describe(
    {
      "semantic.enabled": true,
      "generation.emotion.llm.enabled": true,
      "generation.emotion.llm.provider_id": "old",
      "semantic.embedding_provider_id": "removed",
    },
    { embeddingChanged: true },
  );
  assert.ok(
    setup.selectionIssues.some(
      (item) => item.path === "generation.emotion.llm.provider_id",
    ),
  );
  assert.ok(
    setup.selectionIssues.some(
      (item) => item.path === "semantic.embedding_provider_id",
    ),
  );
  assert.match(
    setup.rows.find((item) => item.label === "默认包准备").value,
    /保存后重新检查/,
  );
  assert.equal(setup.preparationWarning, true);
  assert.match(
    describe({ "semantic.enabled": true }).rows.find(
      (item) => item.label === "默认包准备",
    ).value,
    /默认包/,
  );
});

test("semantic readiness stays visible before the selection workflow", () => {
  assert.equal(describe().preparationWarning, false);
  assert.equal(
    describe({ "semantic.enabled": true }, { semanticStatus: null })
      .preparationWarning,
    true,
  );
  assert.equal(
    describe(
      { "semantic.enabled": true },
      {
        defaultPack: { id: "ready", name: "Ready" },
        semanticStatus: { index_ready: true, semantic_caption_complete: true },
      },
    ).preparationWarning,
    false,
  );
  assert.ok(
    page.indexOf('id="selection-readiness-note"') <
      page.indexOf('id="mode-workflow"'),
  );
});

test("unavailable provider lists allow manually entered IDs without declaring providers missing", () => {
  const setup = describe(
    {
      "semantic.enabled": true,
      "generation.emotion.llm.enabled": true,
      "generation.emotion.llm.provider_id": "manual-chat",
      "semantic.embedding_provider_id": "manual-embedding",
      "semantic.query.provider_id": "manual-query",
      "auto_collect.enabled": true,
      "auto_collect.vision_provider_id": "manual-vision",
    },
    {
      providers: { chat: [], embedding: [] },
      providerListErrors: ["chat", "embedding"],
    },
  );
  assert.equal(setup.selectionIssues.length, 0);
  assert.equal(setup.collectionIssues.length, 0);
  assert.match(setup.rows[0].value, /请核对 ID/);
});

test("automatic acceptance describes review routes concisely", () => {
  const values = {
    "auto_collect.enabled": true,
    "auto_collect.manual_review": false,
    "auto_collect.vision_provider_id": "vision",
  };
  assert.equal(
    describe(values).collectionRoute,
    "符合收录条件的图片自动入包，分类不确定的归入「待分类」。",
  );
  const decision = describe({
    ...values,
    "auto_collect.judgment_backend": "decision",
  });
  assert.match(decision.collectionRoute, /其余进入待审核/);
  assert.ok(!decision.collectionRoute.includes("待分类"));
  assert.equal(decision.collectionIssues.length, 2);
  assert.equal(
    describe({
      ...values,
      "auto_collect.judgment_backend": "decision",
      "auto_collect.enabled": false,
    }).collectionIssues.length,
    0,
  );
  assert.match(
    describe({ "auto_collect.judgment_backend": "decision" }).collectionNote,
    /未开启/,
  );
});

test("locating a draft opens its original field in a dialog while tab navigation stays available", () => {
  const search = { value: "" };
  const details = { open: false, parentElement: { closest: () => null } };
  let focused = "";
  let scrolled = false;
  const field = {
    closest: (selector) =>
      selector === "details" ? details : search.value ? null : {},
    focus: () => (focused = "field"),
    scrollIntoView: () => (scrolled = true),
  };
  const tab = { closest: () => null, focus: () => (focused = "tab") };
  const context = vm.createContext({
    groups: [{ id: "emotion-model", category: "models" }],
    active: "models",
    loaded: true,
    configState: { busy: false },
    dialogContent: null,
    rulesPanel: {},
    configDialog: { contains: () => false },
    configDialogTitle: tab,
    configDialogBody: { scrollTop: 0 },
    configMask: { classList: { remove() {} }, setAttribute() {} },
    categories: { models: ["配图"] },
    mountDialogContent(node) {
      context.dialogContent = node;
      details.open = true;
    },
    closeSettingsDialog() {
      context.dialogContent = null;
    },
    queueMicrotask: (fn) => fn(),
    updateState() {},
    search,
    modePaths: ["semantic.enabled", "generation.emotion.llm.enabled"],
    controls: new Map([["generation.emotion.llm.provider_id", field]]),
    location: { pathname: "/settings/", search: "" },
    history: { replaceState() {} },
    updateView() {},
    document: {
      activeElement: tab,
      getElementById: () => ({}),
      querySelector: () => tab,
    },
    mobileNavigation: { getClientRects: () => [] },
  });
  const start = source.indexOf("  function navigateToSetting(");
  vm.runInContext(
    source.slice(start, source.indexOf("  function showFieldError(", start)),
    context,
  );
  context.navigateToSetting(
    "emotion-model",
    "generation.emotion.llm.provider_id",
  );
  assert.equal(search.value, "");
  assert.equal(context.dialogFieldPath, "generation.emotion.llm.provider_id");
  assert.equal(details.open, true);
  assert.equal(focused, "field");
  assert.equal(scrolled, true);
  assert.equal(context.activeFlowGroup, "emotion-model");
  context.navigateToSetting(undefined, "", "storage");
  assert.equal(context.active, "storage");
  assert.equal(focused, "tab");
  context.mobileNavigation = {
    getClientRects: () => [{}],
    closest: () => null,
    focus: () => (focused = "mobile"),
  };
  context.navigateToSetting(undefined, "", "backup");
  assert.equal(focused, "mobile");
});

test("setup feedback never repeats secrets or credentials embedded in invalid URLs", () => {
  const secret = "sensitive-test-credential";
  const setup = describe(
    {
      "generation.emotion.llm.enabled": true,
      "generation.emotion.llm.backend": "decision",
      "decision_service.token": secret,
      "decision_service.base_url": `https://user:${secret}@example.test/v1`,
      "decision_service.model": "jev",
    },
    { tokenConfigured: true },
  );
  assert.equal(setup.serviceIssues.length, 1);
  assert.equal(setup.serviceIssues[0].path, "decision_service.base_url");
  assert.ok(!JSON.stringify(setup).includes(secret));
});

test("every persisted field has exactly one group and collection has only three groups", async () => {
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
  assert.deepEqual(
    Array.from(
      groups.filter((group) => group.category === "collect"),
      (group) => group.id,
    ),
    ["collect", "collect-recognition", "collect-review"],
  );
  const service = groups.find((group) => group.id === "decision-service");
  assert.equal(service.category, "decision");
  assert.equal(service.when, undefined);
});

test("configuration uses interactive workflow nodes, two responsibility dimensions and a fixed service entry", () => {
  assert.ok(page.includes('data-settings-tab="decision"'));
  assert.ok(page.includes('id="config-flow-dialog"'));
  assert.ok(!source.includes("data-workflow-target"));
  assert.ok(
    page.indexOf('id="mode-workflow"') < page.indexOf('id="config-sections"'),
  );
  assert.match(page, /<section[^>]*id="mode-workflow"/);
  assert.ok(page.includes('name="selection-agent"'));
  assert.ok(!page.includes('name="selection-timing"'));
  assert.ok(page.includes("标签粒度"));
  assert.ok(page.includes("由谁选择表情包"));
  assert.ok(source.includes("node.dataset.settingGroup = step.group"));
  assert.ok(source.includes("group.id !== activeFlowGroup"));
  assert.ok(
    source.includes('control.setAttribute("form", "plugin-config-form")'),
  );
  assert.ok(source.includes("for (const container of [form, configDialog])"));
  assert.equal(
    (page.match(/data-setting-group="collect(?:-recognition|-review)?"/g) || [])
      .length,
    3,
  );
  assert.ok(page.includes("保存全部更改"));
});

test("all combinations keep retrieval and selection independent of collection", () => {
  for (const semantic of [false, true])
    for (const postReply of [false, true]) {
      assert.ok(combinationNames[combinationKey(semantic, postReply)]);
      const steps = selectionWorkflow({
        semantic,
        postReply,
        backend: "decision",
        querySource: "reply_text",
      });
      assert.equal(steps.length, 4);
      assert.ok(!JSON.stringify(steps).includes("自动收集"));
      assert.equal(steps[0].tab, "rules");
      assert.equal(
        steps[1].group,
        semantic ? "semantic-models" : "category-matching",
      );
      assert.equal(
        steps[2].group,
        postReply ? "emotion-model" : semantic ? "tool-trigger" : "prompts",
      );
    }
});

test("dimension edits preserve hidden drafts and revert the original persisted flags", () => {
  const paths = ["semantic.enabled", "generation.emotion.llm.enabled"];
  for (const savedSemantic of [false, true])
    for (const savedPost of [false, true]) {
      const fields = new Map(
        paths.map((path, index) => [
          path,
          { value: [savedSemantic, savedPost][index] },
        ]),
      );
      const changes = new Map([
        ["semantic.query.provider_id", "draft-provider"],
      ]);
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
      const start = source.lastIndexOf(
        "  for (const input of dimensionInputs) {",
      );
      vm.runInContext(
        source.slice(
          start,
          source.indexOf("  function applyLocationHash()", start),
        ),
        context,
      );
      method.value = savedSemantic ? "category" : "semantic";
      handlers[0]();
      assert.equal(changes.get(paths[0]), !savedSemantic);
      assert.equal(changes.has(paths[1]), false);
      method.value = savedSemantic ? "semantic" : "category";
      handlers[0]();
      assert.deepEqual(
        [...changes],
        [["semantic.query.provider_id", "draft-provider"]],
      );
      timing.value = savedPost ? "reply" : "assisted";
      handlers[1]();
      assert.equal(changes.get(paths[1]), !savedPost);
      assert.equal(changes.has(paths[0]), false);
      timing.value = savedPost ? "assisted" : "reply";
      handlers[1]();
      assert.deepEqual(
        [...changes],
        [["semantic.query.provider_id", "draft-provider"]],
      );
    }
});
