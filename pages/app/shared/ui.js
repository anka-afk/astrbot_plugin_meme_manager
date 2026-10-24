// 四个插件页面共用页面外框和对话框行为。
const pageNames = [
  ["a_manage", "表情包管理", "fa-face-grin-beam"],
  ["catalog", "资源广场", "fa-store"],
  ["semantic", "语义化", "fa-wand-magic-sparkles"],
  ["settings", "设置中心", "fa-sliders"],
];
const nav = document.querySelector(".nav-actions");
if (nav) {
  nav.className = "nav-actions";
  nav.setAttribute("role", "navigation");
  nav.setAttribute("aria-label", "页面导航");
  nav.replaceChildren();
  for (const [page, label, icon] of pageNames) {
    const link = document.createElement("a");
    link.className = "nav-link";
    link.dataset.navTarget = `../${page}/index.html`;
    link.href = link.dataset.navTarget;
    if (page === "catalog") link.dataset.navView = "catalog";
    if (location.pathname.includes(`/${page}/`))
      link.setAttribute("aria-current", "page");
    link.innerHTML = `<i class="fas ${icon} icon" aria-hidden="true"></i>${label}`;
    nav.append(link);
  }
}
const main = document.querySelector("main, #content");
if (main) {
  if (!main.id) main.id = "main-content";
  main.tabIndex = -1;
  const skip = document.createElement("a");
  skip.className = "skip-link";
  skip.href = `#${main.id}`;
  skip.textContent = "跳到主要内容";
  document.body.prepend(skip);
}

let navAuthToken = "";
function withCurrentAuthParams(targetPath, extraParams = {}) {
  const nextUrl = new URL(targetPath, location.href);
  for (const [key, value] of new URLSearchParams(location.search)) {
    if (key !== "asset_token" && !nextUrl.searchParams.has(key))
      nextUrl.searchParams.set(key, value);
  }
  for (const [key, value] of Object.entries(extraParams)) {
    if (value === null || value === undefined || value === "")
      nextUrl.searchParams.delete(key);
    else nextUrl.searchParams.set(key, String(value));
  }
  return nextUrl;
}
async function ensureNavAuthToken() {
  if (navAuthToken) return navAuthToken;
  try {
    const response = await window.AstrBotPluginPage.apiGet("bridge/auth_token");
    navAuthToken = String(response?.token || "").trim();
  } catch {
    navAuthToken = "";
  }
  return navAuthToken;
}
async function applySecureNavLinks() {
  const token = await ensureNavAuthToken();
  for (const link of document.querySelectorAll("a[data-nav-target]")) {
    link.href = withCurrentAuthParams(link.dataset.navTarget, {
      view: link.dataset.navView || null,
      asset_token: token || null,
    }).toString();
  }
}

const confirmMask = document.createElement("div");
confirmMask.className = "dialog-mask ui-confirm-mask hidden";
confirmMask.setAttribute("aria-hidden", "true");
confirmMask.innerHTML = `<section class="dialog-card" role="dialog" aria-modal="true" aria-labelledby="ui-confirm-title" aria-describedby="ui-confirm-message">
  <h3 id="ui-confirm-title" class="ui-dialog-title text-h3 pa-4 pb-0 pl-6"></h3>
  <p id="ui-confirm-message" class="dialog-message"></p>
  <div class="dialog-actions"><button type="button" id="ui-confirm-cancel" class="ghost" variant="text">取消</button><button type="button" id="ui-confirm-accept" variant="tonal">确认</button></div>
</section>`;
document.body.append(confirmMask);
let confirmResolver = null;
const confirmCancel = confirmMask.querySelector("#ui-confirm-cancel");
const confirmAccept = confirmMask.querySelector("#ui-confirm-accept");
for (const button of [confirmCancel, confirmAccept]) {
  button.addEventListener("click", () => {
    confirmMask.classList.add("hidden");
    confirmMask.setAttribute("aria-hidden", "true");
    const resolve = confirmResolver;
    confirmResolver = null;
    resolve?.(button === confirmAccept);
  });
}
confirmMask.addEventListener("click", (event) => {
  if (event.target === confirmMask) confirmCancel.click();
});

// 仅观察模态遮罩，避免渲染列表时反复抢夺焦点。
const dialogStack = [];
const dialogObserver = new MutationObserver(() => {
  for (const dialog of document.querySelectorAll('[role="dialog"]')) {
    const mask = dialog.parentElement;
    const visible =
      !mask.classList.contains("hidden") &&
      mask.getAttribute("aria-hidden") !== "true" &&
      !mask.hidden;
    const index = dialogStack.findIndex((entry) => entry.dialog === dialog);
    if (visible && index < 0) {
      dialogStack.push({ dialog, previous: document.activeElement });
      dialog.tabIndex = -1;
      const first =
        dialog.querySelector("[data-dialog-dismiss]:not(:disabled)") ||
        dialog.querySelector(
          'button[id*="cancel"]:not(:disabled), button[id*="close"]:not(:disabled), input:not(:disabled), button:not(:disabled)',
        );
      (first || dialog).focus({ preventScroll: true });
    } else if (!visible && index >= 0) {
      const [entry] = dialogStack.splice(index, 1);
      if (
        entry.previous?.isConnected &&
        !entry.previous.closest(".hidden, [inert]")
      )
        entry.previous.focus({ preventScroll: true });
    }
  }
  document.body.classList.toggle("ui-modal-open", dialogStack.length > 0);
});
for (const dialog of document.querySelectorAll('[role="dialog"]')) {
  const mask = dialog.parentElement;
  let pointerDownTarget = null;
  let pointerUpTarget = null;
  mask.addEventListener(
    "pointerdown",
    (event) => {
      pointerDownTarget = event.target;
      pointerUpTarget = null;
    },
    true,
  );
  mask.addEventListener(
    "pointerup",
    (event) => {
      pointerUpTarget = event.target;
    },
    true,
  );
  mask.addEventListener(
    "pointercancel",
    () => {
      pointerDownTarget = null;
      pointerUpTarget = null;
    },
    true,
  );
  mask.addEventListener(
    "click",
    (event) => {
      // Drag selections can retarget click to the dialog or its backdrop.
      if (
        event.detail > 0 &&
        (event.target === mask || event.target === dialog) &&
        (event.target !== pointerDownTarget || event.target !== pointerUpTarget)
      ) {
        event.stopImmediatePropagation();
      }
      pointerDownTarget = null;
      pointerUpTarget = null;
    },
    true,
  );
  dialogObserver.observe(mask, {
    attributes: true,
    attributeFilter: ["class", "aria-hidden", "hidden"],
  });
}
document.addEventListener(
  "keydown",
  (event) => {
    const dialog = dialogStack.at(-1)?.dialog;
    if (!dialog) return;
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopImmediatePropagation();
      const cancel =
        dialog.querySelector("[data-dialog-dismiss]") ||
        dialog.querySelector('button[id*="cancel"], button[id*="close"]');
      if (cancel && !cancel.disabled) cancel.click();
    }
    if (event.key !== "Tab") return;
    const focusable = [
      ...dialog.querySelectorAll(
        'button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]',
      ),
    ].filter(
      (element) =>
        element.getClientRects().length &&
        !element.closest("[inert], [hidden], .hidden"),
    );
    const first = focusable[0];
    const last = focusable.at(-1);
    if (!first) {
      event.preventDefault();
      dialog.focus();
    } else if (
      event.shiftKey &&
      (document.activeElement === first ||
        !dialog.contains(document.activeElement))
    ) {
      event.preventDefault();
      last.focus();
    } else if (
      !event.shiftKey &&
      (document.activeElement === last ||
        !dialog.contains(document.activeElement))
    ) {
      event.preventDefault();
      first.focus();
    }
  },
  true,
);

// 压缩包分片上传：宿主对单次请求体有上限，大压缩包必须分片传输。
const PACK_ARCHIVE_CHUNK_SIZE = 8 * 1024 * 1024;

function randomImportSessionId() {
  const bytes = new Uint8Array(16);
  const cryptoApi = window.crypto || {};
  if (typeof cryptoApi.getRandomValues === "function") {
    cryptoApi.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join(
    "",
  );
}

async function uploadPackArchiveInChunks(file, { onProgress } = {}) {
  const pageApi = window.AstrBotPluginPage;
  if (!pageApi) {
    throw new Error("页面无法连接 AstrBot");
  }
  const totalChunks = Math.max(
    1,
    Math.ceil((file?.size || 0) / PACK_ARCHIVE_CHUNK_SIZE),
  );
  const sessionId = randomImportSessionId();
  await pageApi.apiPost("packs/import/session", {
    session_id: sessionId,
    filename: String(file?.name || "pack.zip"),
    total_chunks: totalChunks,
  });
  let result = null;
  for (let index = 0; index < totalChunks; index += 1) {
    const start = index * PACK_ARCHIVE_CHUNK_SIZE;
    const end = Math.min(start + PACK_ARCHIVE_CHUNK_SIZE, file.size);
    const chunk = file.slice(start, end);
    result = await pageApi.upload(
      `packs/import/session/chunk/${sessionId}/${index}/${totalChunks}`,
      chunk,
    );
    onProgress?.(index + 1, totalChunks);
  }
  return result;
}

window.MemeUI = {
  get navAuthToken() {
    return navAuthToken;
  },
  withCurrentAuthParams,
  ensureNavAuthToken,
  applySecureNavLinks,
  uploadPackArchiveInChunks,
  toast(message, type = "info") {
    let container = document.getElementById("toast-container");
    if (!container) {
      container = document.createElement("div");
      container.id = "toast-container";
      container.className = "toast-container";
      container.setAttribute("aria-live", "polite");
      document.body.append(container);
    }
    const toast = document.createElement("div");
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.append(toast);
    window.setTimeout(() => toast.remove(), type === "error" ? 6000 : 3500);
  },
  confirm({
    title = "确认操作",
    message = "",
    confirmText = "确认",
    cancelText = "取消",
    danger = false,
  } = {}) {
    if (confirmResolver) return Promise.resolve(false);
    confirmMask.querySelector("#ui-confirm-title").textContent = title;
    confirmMask.querySelector("#ui-confirm-message").textContent = message;
    confirmAccept.textContent = confirmText;
    confirmCancel.textContent = cancelText;
    confirmAccept.className = danger ? "danger" : "";
    confirmMask.classList.remove("hidden");
    confirmMask.setAttribute("aria-hidden", "false");
    return new Promise((resolve) => {
      confirmResolver = resolve;
    });
  },
  showPageError(error) {
    console.error("插件页面初始化失败：", error);
    let banner = document.querySelector(".ui-page-error");
    if (banner) return;
    banner = document.createElement("div");
    banner.className = "ui-page-error";
    banner.setAttribute("role", "alert");
    const message = document.createElement("span");
    message.textContent = `页面加载失败：${
      error?.message || "请检查连接后重试"
    }`;
    const retry = document.createElement("button");
    retry.className = "ghost";
    retry.textContent = "重新加载";
    retry.addEventListener("click", () => location.reload());
    banner.append(message, retry);
    document.querySelector(".page-header")?.after(banner);
  },
};
