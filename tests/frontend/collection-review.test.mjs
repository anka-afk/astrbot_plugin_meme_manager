import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const script = await readFile(
  new URL("../../pages/app/a_manage/script.js", import.meta.url),
  "utf8",
);
const reviewCode = script.slice(
  script.indexOf("  const collectionReview = {"),
  script.indexOf("  const selectionState = {"),
);

class Element {
  constructor(tag = "") {
    this.tag = tag;
    this.children = [];
    this.listeners = {};
    this.value = "";
    this.open = false;
    this.selectedIndex = 0;
    this.isConnected = true;
  }
  append(...children) {
    this.children.push(...children);
  }
  replaceChildren(...children) {
    this.children = children;
  }
  setAttribute(name, value) {
    this[name] = value;
  }
  addEventListener(name, handler) {
    this.listeners[name] = handler;
  }
  querySelectorAll() {
    return [];
  }
  close() {
    this.open = false;
  }
  showModal() {
    this.open = true;
  }
  focus() {}
}

function setup(
  apiGet = async () => ({ data_url: "data:image/gif;base64,AAAA" }),
) {
  const elements = new Map();
  const context = vm.createContext({
    document: {
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, new Element());
        return elements.get(id);
      },
      createElement: (tag) => new Element(tag),
      createTextNode: (text) => ({ textContent: text }),
    },
    Option: class extends Element {
      constructor(text, value) {
        super("option");
        this.textContent = text;
        this.value = value;
      }
    },
    apiGet,
    apiPost: async () => ({}),
    showToast() {},
    refreshUi: async () => {},
    activeManagePackId: "pack-a",
    managePackSelect: new Element("select"),
  });
  vm.runInContext(
    reviewCode + "\nglobalThis.review = collectionReview;",
    context,
  );
  context.review.packId = "pack-a";
  context.review.sort.value = "desc";
  context.review.categories = { happy: "Happy", sad: "Sad" };
  return context;
}

const samples = [
  {
    id: "low",
    suggested_category: "happy",
    category_confidence: 0.2,
    meme_confidence: 0.99,
    caption: "<script>alert(1)</script>",
  },
  {
    id: "high",
    suggested_category: "sad",
    category_confidence: 0.95,
    meme_confidence: 0.9,
  },
  {
    id: "mid",
    suggested_category: "happy",
    category_confidence: 0.7,
    meme_confidence: 0.95,
  },
];

test("similar image preview loads on hover, reuses the request, and can be pinned", async () => {
  const calls = [];
  const context = setup(async (endpoint, body) => {
    calls.push({ endpoint, body });
    return { data_url: "data:image/png;base64,AAAA" };
  });
  context.review.items = [
    { ...samples[0], near_duplicate: "happy/existing.png" },
  ];
  context.renderCollectionReview();
  const warning = context.review.grid.children[0].children.at(-1);
  const [trigger, panel] = warning.children;
  assert.equal(panel.hidden, true);
  assert.equal(
    calls.some((call) => call.endpoint === "meme_image_data"),
    false,
  );
  await warning.listeners.pointerenter();
  assert.equal(panel.hidden, false);
  assert.equal(panel.children[0].src, "data:image/png;base64,AAAA");
  const request = calls.find((call) => call.endpoint === "meme_image_data");
  assert.deepEqual(JSON.parse(JSON.stringify(request.body)), {
    managed_pack_id: "pack-a",
    category: "happy",
    filename: "existing.png",
    size: "preview",
  });
  warning.listeners.pointerleave();
  assert.equal(panel.hidden, true);
  await warning.listeners.pointerenter();
  assert.equal(
    calls.filter((call) => call.endpoint === "meme_image_data").length,
    1,
  );
  trigger.listeners.click();
  warning.listeners.pointerleave();
  assert.equal(panel.hidden, false);
  trigger.listeners.click();
  assert.equal(panel.hidden, true);
});

test("classification confidence orders highest first, and descriptions remain plain text", async () => {
  const context = setup();
  context.review.items = samples;
  context.renderCollectionReview();
  const cards = context.review.grid.children;
  assert.match(cards[0].children[3].textContent, /95%/);
  assert.match(cards[2].children[3].textContent, /20%/);
  assert.equal(cards[2].children[4].textContent, "<script>alert(1)</script>");
  context.review.sort.value = "asc";
  context.renderCollectionReview();
  assert.match(context.review.grid.children[0].children[3].textContent, /20%/);
});

test("select all operates on filtered results and preserves selections from other categories", () => {
  const context = setup();
  context.review.items = samples;
  context.review.selected.add("high");
  context.review.filter.value = "happy";
  context.review.selectAll.checked = true;
  context.review.selectAll.listeners.change();
  assert.deepEqual([...context.review.selected].sort(), ["high", "low", "mid"]);
  context.review.selectAll.checked = false;
  context.review.selectAll.listeners.change();
  assert.deepEqual([...context.review.selected], ["high"]);
  assert.equal(context.review.grid.children.length, 2);
});

test("late inbox response cannot overwrite a newly selected pack", async () => {
  const responses = new Map();
  const context = setup(
    (_endpoint, body) =>
      new Promise((resolve) => responses.set(body.pack_id, resolve)),
  );
  const first = context.loadCollectionReview();
  context.activeManagePackId = "pack-b";
  const second = context.loadCollectionReview();
  responses.get("pack-b")({
    count: 1,
    items: [{ id: "b", suggested_category: "sad" }],
  });
  await second;
  responses.get("pack-a")({ count: 3, items: samples });
  await first;
  assert.equal(context.review.packId, "pack-b");
  assert.equal(context.review.items[0].id, "b");
  assert.equal(context.review.open.textContent, "待审核表情 (1)");
});

test("accept sends selected IDs and edited categories to their captured pack", async () => {
  const calls = [];
  const context = setup(async () => ({ count: 0, items: [] }));
  context.apiPost = async (endpoint, body) => {
    calls.push({ endpoint, body });
    return { imported: 1 };
  };
  context.review.items = samples;
  context.review.selected.add("low");
  context.review.edits.set("low", "sad");
  await context.submitCollectionReview("accept");
  assert.equal(calls[0].endpoint, "auto-collect/inbox/accept");
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body)), {
    pack_id: "pack-a",
    items: [{ id: "low", category: "sad" }],
  });
  assert.equal(context.review.open.hidden, true);
  assert.equal(context.review.selected.size, 0);
});

test("failed mutation preserves selection and releases controls for retry", async () => {
  const context = setup();
  context.apiPost = async () => {
    throw new Error("offline");
  };
  context.review.items = samples;
  context.review.selected.add("mid");
  await context.submitCollectionReview("discard");
  assert.equal(context.review.selected.has("mid"), true);
  assert.equal(context.review.busy, false);
  assert.equal(context.review.discard.disabled, false);
  assert.match(context.review.status.textContent, /offline/);
});
