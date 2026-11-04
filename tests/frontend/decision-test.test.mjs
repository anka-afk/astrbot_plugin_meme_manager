import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../../pages/app/settings/config.js", import.meta.url), "utf8");
const marker = 'testService.addEventListener("click", async () => {';
const start = source.indexOf(marker);
const handler = source.slice(start, source.indexOf("controlBox.append(testService);", start));

function fixture(apiPost, { changes = {}, fields = {}, connected = true } = {}) {
  let click;
  const calls = [];
  const context = vm.createContext({
    testService: {
      addEventListener: (_, fn) => { click = fn; },
      disabled: false,
      textContent: "测试连接",
    },
    control: { isConnected: connected },
    help: { dataset: { state: "", baseHint: "提示" }, textContent: "提示" },
    changes: new Map(Object.entries(changes)),
    fields: new Map(Object.entries(fields).map(([path, value]) => [path, { value }])),
    api: { apiPost: (...args) => { calls.push(args); return apiPost(...args); } },
  });
  vm.runInContext(handler, context);
  return { context, calls, click: () => click() };
}

const CLOUDFLARE = {
  "decision_service.provider": "cloudflare",
  "decision_service.account_id": "a".repeat(32),
  "decision_service.model": "clef-flash",
};

test("decision test posts the draft values and shows the result", async () => {
  const state = fixture(async () => ({
    ok: true,
    message: "连接成功：clef-flash，用时 862 ms。",
  }), { changes: CLOUDFLARE });
  await state.click();
  assert.deepEqual(JSON.parse(JSON.stringify(state.calls)), [
    ["settings/decision-test", {
      provider: "cloudflare",
      account_id: CLOUDFLARE["decision_service.account_id"],
      base_url: "",
      model: "clef-flash",
      token: "",
      token_cleared: false,
    }],
  ]);
  assert.equal(state.context.help.textContent, "连接成功：clef-flash，用时 862 ms。");
  assert.equal(state.context.help.dataset.state, "ok");
  assert.equal(state.context.testService.disabled, false);
  assert.equal(state.context.testService.textContent, "测试连接");
});

test("draft edits win over the saved snapshot", async () => {
  const state = fixture(async () => ({ ok: true, message: "ok" }), {
    changes: { "decision_service.model": "clef" },
    fields: {
      "decision_service.provider": "cloudflare",
      "decision_service.account_id": CLOUDFLARE["decision_service.account_id"],
      "decision_service.model": "clef-flash",
    },
  });
  await state.click();
  const body = state.calls[0][1];
  assert.equal(body.model, "clef");
  assert.equal(body.provider, "cloudflare");
  assert.equal(body.account_id, CLOUDFLARE["decision_service.account_id"]);
});

test("cleared key is reported instead of silently reusing the saved one", async () => {
  const state = fixture(async () => ({ ok: false, message: "请填写决策服务密钥。" }), {
    changes: { ...CLOUDFLARE, "decision_service.token": "" },
  });
  await state.click();
  assert.equal(state.calls[0][1].token, "");
  assert.equal(state.calls[0][1].token_cleared, true);
  assert.equal(state.context.help.dataset.state, "error");
});

test("typed key is forwarded and never echoed in the hint", async () => {
  const state = fixture(async () => ({ ok: true, message: "连接成功。" }), {
    changes: { ...CLOUDFLARE, "decision_service.token": "typed-secret" },
  });
  await state.click();
  assert.equal(state.calls[0][1].token, "typed-secret");
  assert.equal(state.calls[0][1].token_cleared, false);
  assert.ok(!state.context.help.textContent.includes("typed-secret"));
});

test("transport failure keeps the button usable and marks the hint", async () => {
  const state = fixture(async () => {
    throw new Error("服务不可用");
  }, { changes: CLOUDFLARE });
  await state.click();
  assert.equal(state.context.help.dataset.state, "error");
  assert.match(state.context.help.textContent, /服务不可用/);
  assert.match(state.context.help.textContent, /请检查网络后重试/);
  assert.equal(state.context.testService.disabled, false);
});

test("a detached field stops updating the detached hint", async () => {
  const state = fixture(async () => ({ ok: true, message: "连接成功。" }), {
    changes: CLOUDFLARE,
    connected: false,
  });
  await state.click();
  // 字段已随重新渲染而分离，不再写回旧节点，也不恢复按钮状态。
  assert.equal(state.context.help.dataset.state, "");
  assert.equal(state.context.testService.disabled, true);
});
