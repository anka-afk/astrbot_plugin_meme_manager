import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../../pages/app/settings/config.js", import.meta.url), "utf8");
const start = source.indexOf('testService.addEventListener("click", async () => {');
const handler = source.slice(start, source.indexOf("controlBox.append(testService);", start));
const invalidate = source.slice(source.indexOf("  function invalidateDecisionTest()"), source.indexOf("  function updateDecisionModelControl("));

function fixture(apiPost, changes = {}) {
  let click;
  const calls = [];
  const help = { dataset: { state: "", baseHint: "提示" }, textContent: "提示" };
  const context = vm.createContext({
    testService: { addEventListener: (_, fn) => { click = fn; }, disabled: false },
    configState: { busy: false }, decisionHintRequest: 0,
    control: { isConnected: true }, help,
    document: { getElementById: () => help },
    changes: new Map(Object.entries(changes)),
    fields: new Map(Object.entries({
      provider: "cloudflare", account_id: "", token: "", model: "clef-flash", timeout: 15,
    }).map(([name, value]) => [`decision_service.${name}`, {
      value, secret: ["account_id", "token"].includes(name),
    }])),
    api: { apiPost: (...args) => { calls.push(args); return apiPost(...args); } },
  });
  vm.runInContext(invalidate + handler, context);
  return { context, calls, click: () => click() };
}

test("测试提交当前草稿，未修改的脱敏字段不发送，空值不丢失", async () => {
  const state = fixture(async () => ({ ok: true, message: "连接成功。" }), {
    "decision_service.account_id": "a".repeat(32), "decision_service.model": "",
  });
  await state.click();
  assert.deepEqual(JSON.parse(JSON.stringify(state.calls)), [["settings/decision-test", {
    provider: "cloudflare", account_id: "a".repeat(32), model: "", timeout: 15,
  }]]);
  assert.equal(state.context.help.dataset.state, "ok");
  assert.equal(state.context.help.textContent, "连接成功。");
  assert.equal(state.context.testService.disabled, false);
  state.context.invalidateDecisionTest();
  assert.equal(state.context.help.dataset.state, "");
});

test("清除与替换密钥均作为显式草稿提交，失败后可重试", async () => {
  for (const value of ["", "新密钥"]) {
    const state = fixture(async () => { throw new Error("服务不可用"); }, {
      "decision_service.token": value,
    });
    await state.click();
    assert.equal(state.calls[0][1].token, value);
    assert.equal(state.context.help.dataset.state, "error");
    assert.match(state.context.help.textContent, /服务不可用/);
    assert.ok(!state.context.help.textContent.includes("新密钥"));
    assert.equal(state.context.testService.disabled, false);
  }
});

test("编辑、清除和重绘后丢弃旧响应，等待期间不重复请求", async () => {
  for (const mutation of ["edit", "clear", "rerender"]) {
    for (const failed of [false, true]) {
      let resolve, reject;
      const state = fixture(() => new Promise((yes, no) => { resolve = yes; reject = no; }));
      const pending = state.click();
      await state.click();
      assert.equal(state.calls.length, 1);
      if (mutation === "rerender") state.context.control.isConnected = false;
      else state.context.invalidateDecisionTest();
      if (failed) reject(new Error("旧错误"));
      else resolve({ ok: true, message: "旧结果" });
      await pending;
      assert.equal(state.context.help.dataset.state, "");
      assert.ok(!state.context.help.textContent.includes("旧"));
      assert.equal(state.context.testService.disabled, mutation === "rerender");
    }
  }
});
