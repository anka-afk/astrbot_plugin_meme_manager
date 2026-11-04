import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(
  new URL("../../pages/app/settings/config.js", import.meta.url),
  "utf8",
);
const marker = 'testService.addEventListener("click", async () => {';
const start = source.indexOf(marker);
const handler = source.slice(
  start,
  source.indexOf("controlBox.append(testService);", start),
);
const invalidate = source.slice(
  source.indexOf("  function invalidateDecisionTest()"),
  source.indexOf("  function updateDecisionModelControl("),
);

function fixture(
  apiPost,
  { changes = {}, fields = {}, connected = true } = {},
) {
  let click;
  const calls = [];
  const help = {
    dataset: { state: "", baseHint: "提示" },
    textContent: "提示",
  };
  const context = vm.createContext({
    testService: {
      addEventListener: (_, fn) => {
        click = fn;
      },
      disabled: false,
      textContent: "测试连接",
    },
    configState: { busy: false },
    decisionTestRequest: 0,
    control: { isConnected: connected },
    help,
    document: { getElementById: () => help },
    changes: new Map(Object.entries(changes)),
    fields: new Map(
      Object.entries(fields).map(([path, value]) => [
        path,
        {
          value,
          secret: [
            "decision_service.account_id",
            "decision_service.token",
          ].includes(path),
        },
      ]),
    ),
    api: {
      apiPost: (...args) => {
        calls.push(args);
        return apiPost(...args);
      },
    },
  });
  vm.runInContext(invalidate + handler, context);
  return { context, calls, click: () => click() };
}

const CLOUDFLARE = {
  "decision_service.provider": "cloudflare",
  "decision_service.account_id": "a".repeat(32),
  "decision_service.model": "clef-flash",
};

test("decision test posts draft changes and displays the result", async () => {
  const state = fixture(async () => ({ ok: true, message: "连接成功。" }), {
    changes: CLOUDFLARE,
  });
  await state.click();
  assert.deepEqual(JSON.parse(JSON.stringify(state.calls)), [
    [
      "settings/decision-test",
      {
        provider: "cloudflare",
        account_id: "a".repeat(32),
        model: "clef-flash",
      },
    ],
  ]);
  assert.equal(state.context.help.textContent, "连接成功。");
  assert.equal(state.context.help.dataset.state, "ok");
  assert.equal(state.context.testService.disabled, false);
});

test("unchanged secrets are omitted and explicit empty drafts are preserved", async () => {
  const state = fixture(async () => ({ ok: true, message: "ok" }), {
    changes: { "decision_service.model": "", "decision_service.base_url": "" },
    fields: {
      ...CLOUDFLARE,
      "decision_service.account_id": "",
      "decision_service.token": "",
      "decision_service.timeout": 15,
    },
  });
  await state.click();
  assert.deepEqual(JSON.parse(JSON.stringify(state.calls[0][1])), {
    provider: "cloudflare",
    base_url: "",
    model: "",
    timeout: 15,
  });
});

for (const name of ["account_id", "token"]) {
  for (const value of ["", "replacement-value"]) {
    test(`explicit ${name} change is sent: ${value || "cleared"}`, async () => {
      const state = fixture(
        async () => ({ ok: false, message: "配置不可用。" }),
        {
          changes: { ...CLOUDFLARE, [`decision_service.${name}`]: value },
        },
      );
      await state.click();
      assert.equal(state.calls[0][1][name], value);
      assert.equal(state.context.help.dataset.state, "error");
      assert.ok(!state.context.help.textContent.includes("replacement-value"));
    });
  }
}

test("transport failures release the button for retry", async () => {
  const state = fixture(async () => {
    throw new Error("服务不可用");
  });
  await state.click();
  assert.equal(state.context.help.dataset.state, "error");
  assert.match(state.context.help.textContent, /服务不可用/);
  assert.equal(state.context.testService.disabled, false);
});

for (const outcome of ["success", "failure"]) {
  for (const mutation of ["edit", "clear", "rerender"]) {
    test(`late ${outcome} is ignored after ${mutation}`, async () => {
      let resolve, reject;
      const state = fixture(
        () =>
          new Promise((yes, no) => {
            resolve = yes;
            reject = no;
          }),
      );
      const pending = state.click();
      await state.click();
      assert.equal(state.calls.length, 1);
      if (mutation === "rerender") state.context.control.isConnected = false;
      else {
        state.context.changes.set(
          "decision_service.account_id",
          mutation === "clear" ? "" : "b".repeat(32),
        );
        state.context.invalidateDecisionTest();
      }
      if (outcome === "success")
        resolve({ ok: true, message: "旧配置连接成功。" });
      else reject(new Error("旧配置失败"));
      await pending;
      assert.equal(state.context.help.dataset.state, "");
      assert.ok(!state.context.help.textContent.includes("旧配置"));
      assert.equal(state.context.testService.disabled, mutation === "rerender");
    });
  }
}

test("editing after a successful test removes its success state", async () => {
  const state = fixture(async () => ({ ok: true, message: "连接成功。" }));
  await state.click();
  state.context.invalidateDecisionTest();
  assert.equal(state.context.help.dataset.state, "");
  assert.equal(state.context.help.textContent, "提示");
});
