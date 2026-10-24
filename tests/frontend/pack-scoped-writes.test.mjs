import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const script = await readFile(
  new URL("../../pages/app/a_manage/script.js", import.meta.url),
  "utf8",
);
const sharedUi = await readFile(
  new URL("../../pages/app/shared/ui.js", import.meta.url),
  "utf8",
);
const between = (start, end) =>
  script.slice(
    script.indexOf(start),
    script.indexOf(end, script.indexOf(start)),
  );
const betweenShared = (start, end) =>
  sharedUi.slice(
    sharedUi.indexOf(start),
    sharedUi.indexOf(end, sharedUi.indexOf(start)),
  );
const apiHelpers = between(
  "  async function apiGet(",
  "  const collectionReview = {",
);
const uploadHelper = between(
  "  async function uploadEmoji(",
  "  // 删除表情包",
);
const chunkHelpers = betweenShared(
  "const PACK_ARCHIVE_CHUNK_SIZE",
  "window.MemeUI = {",
);

function manageContext(packId) {
  const posted = [];
  const uploaded = [];
  const context = vm.createContext({
    activeManagePackId: packId,
    managePackSelect: { value: packId },
    previews: {
      invalidate() {},
      get: async () => ({}),
    },
    window: {
      AstrBotPluginPage: {
        apiGet: async (endpoint, params) => ({ endpoint, params }),
        apiPost: async (endpoint, body) => {
          posted.push({ endpoint, body });
          return {};
        },
        upload: async (endpoint, file) => {
          uploaded.push({ endpoint, file });
          return {};
        },
      },
    },
  });
  vm.runInContext(apiHelpers + uploadHelper, context);
  context.posted = posted;
  context.uploaded = uploaded;
  return context;
}

test("write requests carry the managed pack id", async () => {
  const c = manageContext("pack-b");
  await c.apiPost("emoji/delete", { category: "happy", image_file: "a.png" });
  await c.apiPost("category/restore", { category: "joy" });
  await c.apiPost("sync/config");
  assert.deepEqual(
    c.posted.map((entry) => entry.body.managed_pack_id),
    ["pack-b", "pack-b", "pack-b"],
  );
});

test("pack scoped endpoints keep their own payload", async () => {
  const c = manageContext("pack-b");
  await c.apiPost("packs/move-images", {
    source_pack_id: "pack-a",
    target_pack_id: "pack-b",
  });
  assert.equal(c.posted[0].body.managed_pack_id, undefined);
});

test("uploads target the managed pack", async () => {
  const c = manageContext("pack-b");
  await c.uploadEmoji("happy", { name: "a.png" });
  assert.equal(c.uploaded[0].endpoint, "emoji/add/pack-b/happy");
});

test("the default pack read-only gate is gone", () => {
  assert.equal(script.includes("管理视图模式"), false);
  assert.equal(script.includes("defaultManagePackId"), false);
});

test("upload endpoint keeps the category unencoded", async () => {
  const uploaded = [];
  const context = vm.createContext({
    activeManagePackId: "pack-b",
    managePackSelect: { value: "pack-b" },
    window: {
      AstrBotPluginPage: {
        upload: async (endpoint, file) => {
          uploaded.push({ endpoint, file });
          return {};
        },
      },
    },
  });
  vm.runInContext(uploadHelper, context);
  await context.uploadEmoji("开心", { name: "a.png" });
  assert.equal(uploaded[0].endpoint, "emoji/add/pack-b/开心");
});

test("large archives are uploaded in chunks", async () => {
  const posted = [];
  const uploaded = [];
  const context = vm.createContext({
    window: {
      AstrBotPluginPage: {
        apiPost: async (endpoint, body) => {
          posted.push({ endpoint, body });
          return {};
        },
        upload: async (endpoint, file) => {
          uploaded.push({ endpoint, file });
          return { import_token: "token" };
        },
      },
    },
  });
  vm.runInContext(chunkHelpers, context);
  const file = {
    size: 20 * 1024 * 1024,
    name: "big.zip",
    slice: (start, end) => ({ size: end - start, name: "big.zip" }),
  };
  const result = await context.uploadPackArchiveInChunks(file);
  assert.equal(posted.length, 1);
  assert.equal(posted[0].endpoint, "packs/import/session");
  assert.equal(posted[0].body.filename, "big.zip");
  assert.equal(posted[0].body.total_chunks, 3);
  assert.equal(uploaded.length, 3);
  assert.match(uploaded[0].endpoint, /^packs\/import\/session\/chunk\/[0-9a-f]{32}\/0\/3$/);
  assert.match(uploaded[2].endpoint, /^packs\/import\/session\/chunk\/[0-9a-f]{32}\/2\/3$/);
  assert.deepEqual(result, { import_token: "token" });
});
