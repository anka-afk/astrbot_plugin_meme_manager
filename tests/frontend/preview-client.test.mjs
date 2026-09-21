import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(
  new URL("../../pages/app/shared/preview.js", import.meta.url),
  "utf8",
);
const { PreviewClient } = await import(
  `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`
);
const tick = () => new Promise((resolve) => setImmediate(resolve));

function fixture(concurrency = 2) {
  const pending = [];
  const calls = [];
  let revision = "v1";
  const api = {
    async apiGet(endpoint, params) {
      calls.push({ endpoint, params });
      if (endpoint === "preview/manifest")
        return {
          concurrency,
          pack_id: params.managed_pack_id || "default",
          versions: Object.fromEntries(
            ["a", "b", "c", "d", "e"].map((name) => [
              `happy/${name}`,
              revision,
            ]),
          ),
        };
      return new Promise((resolve, reject) =>
        pending.push({ resolve, reject, params }),
      );
    },
  };
  const client = new PreviewClient(api);
  const get = (filename, options, pack = "default") =>
    client.get(
      "meme_image_data",
      {
        managed_pack_id: pack,
        category: "happy",
        filename,
        size: "preview",
      },
      options,
    );
  return {
    client,
    calls,
    pending,
    get,
    change: () => {
      revision = "v2";
      client.invalidate();
    },
  };
}

for (const limit of [1, 2, 4]) {
  test(`at most ${limit} requests are active, including after a failure`, async () => {
    const f = fixture(limit);
    const results = Promise.allSettled(
      ["a", "b", "c", "d", "e"].map((name) => f.get(name)),
    );
    await tick();
    assert.equal(f.pending.length, limit);
    f.pending[0].reject(new Error("network"));
    await tick();
    assert.equal(f.pending.length, limit + 1);
    let index = 1;
    while (index < 5) {
      f.pending[index++].resolve({ data_url: "data:image/png;base64,AA==" });
      await tick();
      assert.ok(f.client.active <= limit);
    }
    assert.equal(
      (await results).filter((r) => r.status === "fulfilled").length,
      4,
    );
  });
}

test("deduplicates downloads, reuses previews, and separates revisions and packs", async () => {
  const f = fixture();
  const first = f.get("a");
  const duplicate = f.get("a");
  await tick();
  assert.equal(f.pending.length, 1);
  f.pending[0].resolve({ data_url: "data:image/png;base64,AA==" });
  await Promise.all([first, duplicate]);
  await tick();
  await f.get("a");
  assert.equal(f.pending.length, 1);
  f.change();
  const updated = f.get("a");
  const anotherPack = f.get("a", {}, "second");
  await tick();
  assert.equal(f.pending.length, 3);
  assert.equal(f.pending[1].params.v, "v2");
  assert.equal(f.pending[2].params.managed_pack_id, "second");
  f.pending[1].resolve({ data_url: "new" });
  f.pending[2].resolve({ data_url: "other" });
  await Promise.all([updated, anotherPack]);
});

test("prioritizes explicit previews and discards obsolete queued work", async () => {
  const f = fixture(1);
  let visible = true;
  const first = f.get("a");
  const obsolete = f
    .get("b", { isCurrent: () => visible })
    .catch((e) => e.name);
  const normal = f.get("c");
  const priority = f.get("d", { priority: true });
  await tick();
  visible = false;
  f.pending[0].resolve({ data_url: "first" });
  await tick();
  assert.equal(f.pending[1].params.filename, "d");
  f.pending[1].resolve({ data_url: "priority" });
  await tick();
  assert.equal(f.pending[2].params.filename, "c");
  f.pending[2].resolve({ data_url: "normal" });
  await Promise.all([first, normal, priority]);
  assert.equal(await obsolete, "AbortError");
});

test("cancelled consumers cannot release active network slots early", async () => {
  const f = fixture(1);
  let visible = true;
  const old = f.get("a", { isCurrent: () => visible }).catch((e) => e.name);
  await tick();
  visible = false;
  const next = f.get("b");
  await tick();
  assert.equal(f.pending.length, 1);
  f.pending[0].resolve({ data_url: "old" });
  await tick();
  assert.equal(await old, "AbortError");
  assert.equal(f.pending.length, 2);
  f.pending[1].resolve({ data_url: "next" });
  await next;
});
