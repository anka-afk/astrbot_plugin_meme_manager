// Run `node tests/frontend/preview.mjs` to inspect the UI with disposable fixtures.
// This server never connects to AstrBot or writes plugin data.
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { dirname, extname, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const pages = resolve(here, "../../pages");
const mime = {
  ".html": "text/html",
  ".css": "text/css",
  ".js": "text/javascript",
  ".woff2": "font/woff2",
  ".woff": "font/woff",
  ".ttf": "font/ttf",
};
const server = createServer(async (request, response) => {
  try {
    const pathname = decodeURIComponent(
      new URL(request.url, "http://localhost").pathname,
    );
    if (pathname === "/api/plugin/page/bridge-sdk.js") {
      response.writeHead(200, { "Content-Type": "text/javascript" });
      response.end("");
      return;
    }
    if (pathname === "/__preview/settings") {
      const schema = JSON.parse(
        await readFile(resolve(pages, "../_conf_schema.json"), "utf8"),
      );
      const fields = [];
      const pending = [["", schema, []]];
      while (pending.length) {
        const [prefix, items, groups] = pending.pop();
        for (const [key, spec] of Object.entries(items)) {
          const path = prefix ? `${prefix}.${key}` : key;
          if (spec.type === "object") {
            pending.push([path, spec.items, [...groups, spec.description]]);
            continue;
          }
          const secret = [
            "key",
            "secret",
            "password",
            "access_key_id",
            "secret_access_key",
          ].includes(key);
          const value =
            spec.default ??
            { bool: false, int: 0, float: 0, list: [] }[spec.type] ??
            "";
          fields.push({
            path,
            label: spec.description,
            groups,
            type: spec.type,
            hint: spec.hint || "",
            default: secret ? "" : value,
            value: secret ? "" : value,
            secret,
            configured: secret,
            options: spec.options || [],
            special: spec._special || "",
            bounds: spec.slider || {},
          });
        }
      }
      response.writeHead(200, {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
      });
      response.end(
        JSON.stringify({
          revision: "fixture-1",
          fields,
          providers: {
            chat: [
              { id: "chat-main", model: "Chat model" },
              { id: "vision-main", model: "Vision model" },
            ],
            embedding: [{ id: "embedding-main", model: "Embedding model" }],
          },
        }),
      );
      return;
    }
    const target =
      pathname === "/__preview/bridge.js"
        ? resolve(here, "preview-bridge.js")
        : resolve(
            pages,
            `.${pathname.endsWith("/") ? `${pathname}index.html` : pathname}`,
          );
    if (
      target !== resolve(here, "preview-bridge.js") &&
      !target.startsWith(`${pages}${sep}`)
    )
      throw new Error("Invalid path");
    let content = await readFile(target);
    if (extname(target) === ".html") {
      content = content
        .toString()
        .replace(
          "</head>",
          '<script src="/__preview/bridge.js"></script></head>',
        );
    }
    response.writeHead(200, {
      "Content-Type": `${
        mime[extname(target)] || "application/octet-stream"
      }; charset=utf-8`,
      "Cache-Control": "no-store",
    });
    response.end(content);
  } catch {
    response.writeHead(404);
    response.end("Not found");
  }
});
server.listen(
  Number(process.env.MEME_PREVIEW_PORT || 8766),
  "127.0.0.1",
  () => {
    console.log(
      `Frontend fixture preview: http://127.0.0.1:${
        server.address().port
      }/app/`,
    );
  },
);
