import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const script = await readFile(
  new URL("../../pages/app/a_manage/script.js", import.meta.url),
  "utf8",
);
const between = (start, end) =>
  script.slice(
    script.indexOf(start),
    script.indexOf(end, script.indexOf(start)),
  );
const helpers = between(
  "  function dedupeEmojiItems(",
  "  function setClipboardItems(",
);
const moving = between(
  "  async function moveEmojiItemsToCategory(",
  "  async function copyEmojiItemsToCategory(",
);

function element() {
  const classes = new Set();
  return {
    dataset: {},
    disabled: false,
    classList: {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
      toggle: (name, enabled) =>
        enabled ? classes.add(name) : classes.delete(name),
    },
    closest: () => null,
  };
}

function moveContext(apiPost) {
  const context = vm.createContext({
    Map,
    Set,
    console: { error() {} },
    moveBusy: false,
    lastMove: null,
    activeManagePackId: "pack",
    defaultManagePackId: "pack",
    managePackSelect: element(),
    moveUndoBtn: element(),
    moveResult: element(),
    moveResultText: element(),
    selectionState: { enabled: false, items: new Map() },
    createSelectionKey: (category, emoji) => `${category}/${emoji}`,
    clearDragMode() {},
    updateSelectionToolbar() {},
    updateSelectionUI() {},
    refreshUi: async () => {},
    showToast() {},
    apiPost,
  });
  vm.runInContext(helpers + moving, context);
  return context;
}

test("partial moves retain failed selections and undo only confirmed successes", async () => {
  const calls = [];
  const c = moveContext(async (_, body) => {
    calls.push(body);
    return {
      moved_files: body.image_files.filter((f) => f !== "conflict.png"),
      conflicting_files: body.image_files.filter((f) => f === "conflict.png"),
    };
  });
  await c.moveEmojiItemsToCategory("target", [
    { category: "a", emoji: "ok.png" },
    { category: "a", emoji: "ok.png" },
    { category: "a", emoji: "conflict.png" },
    { category: "b", emoji: "other.png" },
  ]);
  assert.equal(c.lastMove.items.length, 2);
  assert.equal(c.selectionState.enabled, true);
  assert.deepEqual([...c.selectionState.items.keys()], ["a/conflict.png"]);
  await c.moveEmojiItemsToCategory(
    c.lastMove.targetCategory,
    c.lastMove.items,
    true,
  );
  assert.equal(c.lastMove, null);
  assert.deepEqual(
    calls
      .slice(2)
      .map((b) => [b.source_category, b.target_category, [...b.image_files]]),
    [
      ["target", "a", ["ok.png"]],
      ["target", "b", ["other.png"]],
    ],
  );
  assert.equal(c.moveBusy, false);
  assert.equal(c.managePackSelect.disabled, false);
});

test("partial undo retries only unresolved items and keeps the result actionable", async () => {
  const c = moveContext(async () => ({
    moved_files: ["ok.png"],
    conflicting_files: ["blocked.png"],
  }));
  c.lastMove = {
    packId: "pack",
    targetCategory: "target",
    items: [
      { category: "a", emoji: "ok.png" },
      { category: "a", emoji: "blocked.png" },
    ],
  };
  await c.moveEmojiItemsToCategory("target", c.lastMove.items, true);
  assert.deepEqual(
    Array.from(c.lastMove.items, (i) => i.emoji),
    ["blocked.png"],
  );
  assert.equal(c.moveUndoBtn.textContent, "重试撤销");
  assert.equal(c.moveUndoBtn.disabled, false);
});

test("request failure and refresh failure preserve recoverable state", async () => {
  const c = moveContext(async (_, body) => {
    if (body.source_category === "b") throw new Error("offline");
    return { moved_files: body.image_files };
  });
  c.refreshUi = async () => {
    throw new Error("refresh offline");
  };
  await c.moveEmojiItemsToCategory("target", [
    { category: "a", emoji: "ok.png" },
    { category: "b", emoji: "retry.png" },
  ]);
  assert.equal(c.lastMove.items.length, 1);
  assert.equal(c.selectionState.items.has("b/retry.png"), true);
  assert.equal(c.moveBusy, false);
  assert.equal(c.managePackSelect.disabled, false);
});

test("concurrent move and undo from another pack issue no requests", async () => {
  let calls = 0;
  let finish;
  const c = moveContext(async () => {
    calls++;
    return await new Promise((resolve) => {
      finish = resolve;
    });
  });
  const items = [{ category: "a", emoji: "x.png" }];
  const pending = c.moveEmojiItemsToCategory("target", items);
  await c.moveEmojiItemsToCategory("target", items);
  assert.equal(calls, 1);
  finish({ moved_files: ["x.png"] });
  await pending;
  c.activeManagePackId = "other";
  await c.moveEmojiItemsToCategory("target", items, true);
  assert.equal(calls, 1);
});

function gestureContext() {
  const handlers = new Map();
  let timer;
  const c = vm.createContext({
    LONG_PRESS_DURATION_MS: 500,
    LONG_PRESS_TICK_MS: 60,
    LONG_PRESS_CANCEL_DISTANCE_PX: 10,
    MOUSE_DRAG_DISTANCE_PX: 6,
    performance: { now: () => 0 },
    moveBusy: false,
    activeManagePackId: "pack",
    defaultManagePackId: "pack",
    longPressState: {},
    dragModeState: { items: [], pointerId: null },
    selectionState: { enabled: false, items: new Map() },
    window: {
      setTimeout: (fn) => {
        timer = fn;
        return 1;
      },
      setInterval: () => 1,
      addEventListener: (key, fn) => handlers.set(key, fn),
    },
    document: { addEventListener: (key, fn) => handlers.set(key, fn) },
    syncInteractionGuardState() {},
    setLongPressProgress() {},
    getDragItemsForEmoji: (category, emoji) => [{ category, emoji }],
    isEmojiSelected: (category, emoji) =>
      c.selectionState.items.has(`${category}/${emoji}`),
    setSelectionMode: () => {
      c.selectionState.enabled = true;
    },
    toggleEmojiSelection: (category, emoji) =>
      c.selectionState.items.set(`${category}/${emoji}`, { category, emoji }),
    cancelLongPress: () => {
      timer = null;
      c.longPressState.emojiItem = null;
      c.longPressState.pointerId = null;
    },
    clearDragMode: () => {
      c.cancelLongPress();
      c.dragModeState.pointerId = null;
      c.dragModeState.items = [];
    },
    armDragMode: (items, pointer) => {
      c.cancelLongPress();
      c.dragModeState.items = items;
      c.dragModeState.pointerId = pointer.pointerId;
    },
    updatePointerDrag() {},
    finishPointerDrag: () => {
      c.drops++;
    },
    drops: 0,
  });
  vm.runInContext(
    between("  function startLongPress(", "  function isInternalEmojiDrag(") +
      between(
        '  document.addEventListener("pointermove"',
        '  document.addEventListener("dragstart"',
      ),
    c,
  );
  return { c, handlers, fireTimer: () => timer?.() };
}

function pointer(pointerType, x = 0, y = 0) {
  return {
    pointerType,
    pointerId: 1,
    button: 0,
    clientX: x,
    clientY: y,
    target: element(),
    preventDefault() {
      this.prevented = true;
    },
  };
}

test("mouse click remains a click until movement crosses the drag threshold", () => {
  const { c, handlers, fireTimer } = gestureContext();
  const item = element();
  item.dataset = { category: "a", emoji: "x.png" };
  c.startLongPress(item, "a", "x.png", pointer("mouse"));
  fireTimer();
  handlers.get("pointermove")(pointer("mouse", 3, 0));
  assert.equal(c.dragModeState.items.length, 0);
  handlers.get("pointermove")(pointer("mouse", 8, 0));
  assert.equal(c.dragModeState.items.length, 1);
  assert.equal(item.dataset.suppressClick, "true");
});

test("touch long press selects without dragging and does not deselect an existing selection", () => {
  const { c, fireTimer } = gestureContext();
  const item = element();
  c.startLongPress(item, "a", "x.png", pointer("touch"));
  fireTimer();
  assert.equal(c.selectionState.enabled, true);
  assert.equal(c.selectionState.items.size, 1);
  assert.equal(c.dragModeState.items.length, 0);
  c.startLongPress(item, "a", "x.png", pointer("touch"));
  fireTimer();
  assert.equal(c.selectionState.items.size, 1);
});

test("touch scrolling cancels selection without preventing scrolling; pointercancel never drops", () => {
  const { c, handlers, fireTimer } = gestureContext();
  c.startLongPress(element(), "a", "x.png", pointer("touch"));
  const scroll = pointer("touch", 0, 20);
  handlers.get("pointermove")(scroll);
  fireTimer();
  assert.equal(scroll.prevented, undefined);
  assert.equal(c.selectionState.items.size, 0);
  c.dragModeState.pointerId = 1;
  c.dragModeState.items = [{ category: "a", emoji: "x.png" }];
  handlers.get("pointercancel")(pointer("mouse"));
  assert.equal(c.drops, 0);
  assert.equal(c.dragModeState.items.length, 0);
});
