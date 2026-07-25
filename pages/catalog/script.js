async function initCatalogPage() {
  await window.AstrBotPluginPage.ready();

  const FIXED_INDEX_URL =
    "https://raw.githubusercontent.com/anka-afk/astrbot-meme-pack-index/main/community-index.json";

  function withCurrentAuthParams(targetPath, extraParams = {}) {
    const nextUrl = new URL(targetPath, window.location.href);
    const currentParams = new URLSearchParams(window.location.search);
    for (const [key, value] of currentParams.entries()) {
      if (key === "asset_token") {
        continue;
      }
      if (!nextUrl.searchParams.has(key)) {
        nextUrl.searchParams.set(key, value);
      }
    }
    for (const [key, value] of Object.entries(extraParams)) {
      if (value === null || value === undefined || value === "") {
        nextUrl.searchParams.delete(key);
      } else {
        nextUrl.searchParams.set(key, String(value));
      }
    }
    return nextUrl;
  }

  let navAuthToken = "";
  async function ensureNavAuthToken() {
    if (navAuthToken) {
      return navAuthToken;
    }
    try {
      const response =
        await window.AstrBotPluginPage.apiGet("bridge/auth_token");
      navAuthToken = String(response?.token || "").trim();
    } catch (_) {
      navAuthToken = "";
    }
    return navAuthToken;
  }

  async function applySecureNavLinks() {
    const token = await ensureNavAuthToken();
    document.querySelectorAll("a[data-nav-target]").forEach((link) => {
      const targetPath = link.getAttribute("data-nav-target");
      if (!targetPath) {
        return;
      }
      const navView = link.getAttribute("data-nav-view") || "";
      const nextUrl = withCurrentAuthParams(targetPath, {
        view: navView || null,
        asset_token: token || null,
      });
      link.href = nextUrl.toString();
    });
  }

  const sourceRepoInput = document.getElementById("source-repo-input");
  const sourceRefInput = document.getElementById("source-ref-input");
  const sourceSubpathInput = document.getElementById("source-subpath-input");
  const installSourceBtn = document.getElementById("install-source-btn");
  const installDialog = document.getElementById("install-dialog");
  const installDialogPackName = document.getElementById(
    "install-dialog-pack-name",
  );
  const installDialogOverwriteCheckbox = document.getElementById(
    "install-overwrite-checkbox",
  );
  const installDialogDefaultCheckbox = document.getElementById(
    "install-default-checkbox",
  );
  const installDialogCancel = document.getElementById("install-dialog-cancel");
  const installDialogConfirm = document.getElementById(
    "install-dialog-confirm",
  );
  const officialGrid = document.getElementById("official-grid");
  const communityGrid = document.getElementById("community-grid");
  const officialPackCount = document.getElementById("official-pack-count");
  const communityPackCount = document.getElementById("community-pack-count");
  const logList = document.getElementById("log-list");

  await applySecureNavLinks();

  let cachedIndex = null;
  let installedPackIds = new Set();
  let pendingInstallAction = null;

  async function apiGet(endpoint, params = {}) {
    return window.AstrBotPluginPage.apiGet(endpoint, params);
  }

  async function apiPost(endpoint, body = {}) {
    return window.AstrBotPluginPage.apiPost(endpoint, body);
  }

  function addLog(message, isError = false) {
    const item = document.createElement("div");
    item.className = `log-item${isError ? " error" : ""}`;
    const now = new Date();
    item.textContent = `[${now.toLocaleTimeString("zh-CN", { hour12: false })}] ${message}`;
    logList.prepend(item);
  }

  function setLoading(button, loadingText) {
    if (!button.dataset.originalHtml) {
      button.dataset.originalHtml = button.innerHTML;
    }
    button.disabled = true;
    button.textContent = loadingText;
  }

  function clearLoading(button) {
    button.disabled = false;
    if (button.dataset.originalHtml) {
      button.innerHTML = button.dataset.originalHtml;
    }
  }

  function openInstallDialog(packName, onConfirm) {
    pendingInstallAction = onConfirm;
    installDialogPackName.textContent = `目标: ${packName || "未命名"}`;
    installDialogOverwriteCheckbox.checked = false;
    installDialogDefaultCheckbox.checked = false;
    installDialog.classList.remove("hidden");
    installDialog.setAttribute("aria-hidden", "false");
  }

  function closeInstallDialog() {
    pendingInstallAction = null;
    installDialog.classList.add("hidden");
    installDialog.setAttribute("aria-hidden", "true");
  }

  async function confirmInstallDialog() {
    if (!pendingInstallAction) {
      closeInstallDialog();
      return;
    }
    const handler = pendingInstallAction;
    pendingInstallAction = null;
    const options = {
      overwrite: installDialogOverwriteCheckbox.checked,
      setAsDefault: installDialogDefaultCheckbox.checked,
    };
    closeInstallDialog();
    await handler(options);
  }

  async function refreshInstalledSet() {
    try {
      const response = await apiGet("packs");
      const packs = Array.isArray(response?.packs) ? response.packs : [];
      installedPackIds = new Set(
        packs.map((item) => String(item.id || "").trim()),
      );
    } catch (error) {
      addLog(`刷新已安装列表失败: ${error?.message || String(error)}`, true);
    }
  }

  function readPacksFromCache() {
    const packs = cachedIndex?.index?.packs;
    if (!Array.isArray(packs)) {
      return [];
    }
    return packs.filter((item) => item && typeof item === "object");
  }

  function isOfficialPack(pack) {
    const packId = String(pack?.id || "")
      .trim()
      .toLowerCase();
    const tags = Array.isArray(pack?.tags)
      ? pack.tags.map((tag) =>
          String(tag || "")
            .trim()
            .toLowerCase(),
        )
      : [];
    return packId.startsWith("official-") || tags.includes("official");
  }

  function readPackFormat(pack) {
    // 现有索引可直接使用 semantic/v2 标签；同时兼容未来的显式能力字段。
    const tags = new Set(
      (Array.isArray(pack?.tags) ? pack.tags : []).map((tag) =>
        String(tag || "")
          .trim()
          .toLowerCase(),
      ),
    );
    const features =
      pack?.features && typeof pack.features === "object" ? pack.features : {};
    const protocol =
      pack?.protocol && typeof pack.protocol === "object" ? pack.protocol : {};
    const formatVersion = Number(
      pack?.format_version || protocol?.format_version || 0,
    );
    const hasSemanticMetadata = Boolean(
      features.semantic_metadata ||
      pack?.semantic_metadata ||
      tags.has("semantic") ||
      tags.has("semantic-v2") ||
      tags.has("语义包"),
    );
    const isNewFormat = Boolean(
      hasSemanticMetadata ||
      formatVersion >= 2 ||
      tags.has("v2") ||
      tags.has("new-format"),
    );

    if (isNewFormat) {
      return {
        kind: "semantic",
        badge: hasSemanticMetadata ? "新版语义包" : "新版包",
        note: hasSemanticMetadata
          ? "包含可复用语义描述；不会分发本机向量，安装后可按当前向量模型重建。"
          : "采用新版包结构，兼容语义描述与本机向量重建流程。",
      };
    }
    return {
      kind: "classic",
      badge: "传统包",
      note: "使用传统分类描述，无需配置向量模型，安装后继续使用分类匹配。",
    };
  }

  function normalizeGithubSubpath(subpath) {
    return String(subpath || "")
      .trim()
      .replace(/^\/+|\/+$/g, "");
  }

  function buildPackCoverCandidates(pack) {
    const explicitCover = String(pack?.cover_url || "").trim();
    const candidates = explicitCover ? [explicitCover] : [];

    const source =
      pack && typeof pack.source === "object" && pack.source
        ? pack.source
        : null;
    if (
      !source ||
      String(source.type || "")
        .trim()
        .toLowerCase() !== "github"
    ) {
      return candidates;
    }

    const repo = String(source.repo || "").trim();
    const ref = String(source.ref || "main").trim() || "main";
    const normalizedSubpath = normalizeGithubSubpath(source.subpath);
    if (!repo) {
      return candidates;
    }

    const encodedRef = encodeURIComponent(ref);
    const rootPrefix = `https://raw.githubusercontent.com/${repo}/${encodedRef}`;
    const subpathPrefix = normalizedSubpath
      ? `${rootPrefix}/${normalizedSubpath}`
      : rootPrefix;

    candidates.push(`${subpathPrefix}/previews/cover.jpg`);
    if (normalizedSubpath) {
      candidates.push(`${rootPrefix}/previews/cover.jpg`);
    }

    // jsDelivr 作为 raw.githubusercontent.com 的备用镜像
    const jsdelivrPrefix = `https://cdn.jsdelivr.net/gh/${repo}@${ref}`;
    const jsdelivrSubpathPrefix = normalizedSubpath
      ? `${jsdelivrPrefix}/${normalizedSubpath}`
      : jsdelivrPrefix;
    candidates.push(`${jsdelivrSubpathPrefix}/previews/cover.jpg`);
    if (normalizedSubpath) {
      candidates.push(`${jsdelivrPrefix}/previews/cover.jpg`);
    }

    return [...new Set(candidates)];
  }

  function createPackCover(pack) {
    const coverCandidates = buildPackCoverCandidates(pack);
    const coverWrap = document.createElement("div");
    coverWrap.className = "pack-cover";

    if (!coverCandidates.length) {
      coverWrap.classList.add("empty");
      coverWrap.setAttribute("aria-hidden", "true");
      return coverWrap;
    }

    const img = document.createElement("img");
    img.className = "pack-cover-image";
    img.alt = `${pack?.name || pack?.id || "表情包"} 封面`;
    img.loading = "lazy";
    img.decoding = "async";

    let currentIndex = 0;
    const tryLoad = () => {
      if (currentIndex >= coverCandidates.length) {
        coverWrap.classList.add("empty");
        return;
      }
      img.src = coverCandidates[currentIndex];
      currentIndex += 1;
    };

    img.addEventListener("load", () => {
      coverWrap.classList.remove("empty");
    });
    img.addEventListener("error", tryLoad);

    coverWrap.appendChild(img);
    tryLoad();
    return coverWrap;
  }

  function createPackCard(pack, { forceOfficial = false } = {}) {
    const format = readPackFormat(pack);
    const card = document.createElement("article");
    card.className = `pack-card ${format.kind}-pack${
      forceOfficial ? " official" : ""
    }`;

    const isInstalled = installedPackIds.has(String(pack.id || "").trim());
    const tags = Array.isArray(pack.tags) ? pack.tags : [];

    const titleRow = document.createElement("div");
    titleRow.className = "pack-title-row";

    const titleWrap = document.createElement("div");
    const title = document.createElement("h3");
    title.className = "pack-title";
    title.textContent = pack.name || pack.id || "未命名";

    const id = document.createElement("p");
    id.className = "pack-id";
    id.textContent = `ID: ${pack.id || "-"}`;
    titleWrap.appendChild(title);
    titleWrap.appendChild(id);

    const installBtn = document.createElement("button");
    installBtn.type = "button";
    installBtn.textContent = isInstalled ? "已安装" : "安装";
    installBtn.className = isInstalled ? "ghost" : "";
    installBtn.disabled = isInstalled;
    installBtn.addEventListener("click", () => {
      openInstallDialog(pack.name || pack.id || "未命名", async (options) => {
        await installByPack(pack, installBtn, options);
      });
    });

    titleRow.appendChild(titleWrap);
    titleRow.appendChild(installBtn);

    const tagRow = document.createElement("div");
    tagRow.className = "tag-row";

    const formatTag = document.createElement("span");
    formatTag.className = `tag format-tag ${format.kind}`;
    const formatTagIcon = document.createElement("i");
    formatTagIcon.className =
      format.kind === "semantic"
        ? "fas fa-wand-magic-sparkles"
        : "fas fa-box-archive";
    const formatTagText = document.createElement("span");
    formatTagText.textContent = format.badge;
    formatTag.append(formatTagIcon, formatTagText);
    tagRow.appendChild(formatTag);

    const verifyTag = document.createElement("span");
    verifyTag.className = `tag ${pack.verified ? "verified" : "unverified"}`;
    verifyTag.textContent = pack.verified ? "已验证" : "未验证";
    tagRow.appendChild(verifyTag);

    if (forceOfficial) {
      const officialTag = document.createElement("span");
      officialTag.className = "tag verified";
      officialTag.textContent = "官方";
      tagRow.appendChild(officialTag);
    }

    if (isInstalled) {
      const installedTag = document.createElement("span");
      installedTag.className = "tag installed";
      installedTag.textContent = "已安装";
      tagRow.appendChild(installedTag);
    }

    for (const tag of tags.slice(0, 4)) {
      const span = document.createElement("span");
      span.className = "tag";
      span.textContent = String(tag);
      tagRow.appendChild(span);
    }

    const desc = document.createElement("p");
    desc.className = "pack-desc";
    desc.textContent = pack.description || "暂无描述";

    const formatNote = document.createElement("p");
    formatNote.className = `pack-format-note ${format.kind}`;
    const formatNoteIcon = document.createElement("i");
    formatNoteIcon.className =
      format.kind === "semantic"
        ? "fas fa-cubes-stacked"
        : "fas fa-layer-group";
    const formatNoteText = document.createElement("span");
    formatNoteText.textContent = format.note;
    formatNote.append(formatNoteIcon, formatNoteText);

    const meta = document.createElement("div");
    meta.className = "pack-meta";
    meta.innerHTML = `
      <span>维护者: ${pack.maintainer || "未知"}</span>
      <span>协议: ${pack.license || "未知"}</span>
      <span>来源: ${pack.source?.repo || "-"}@${pack.source?.ref || "-"}</span>
    `;

    card.appendChild(createPackCover(pack));
    card.appendChild(titleRow);
    card.appendChild(tagRow);
    card.appendChild(desc);
    card.appendChild(formatNote);
    card.appendChild(meta);
    return card;
  }

  function renderCatalog() {
    const packs = readPacksFromCache();
    const officialPacks = packs.filter((pack) => isOfficialPack(pack));
    const communityPacks = packs.filter((pack) => !isOfficialPack(pack));

    officialPackCount.textContent = String(officialPacks.length);
    communityPackCount.textContent = String(communityPacks.length);

    if (!officialPacks.length) {
      officialGrid.classList.add("empty");
      officialGrid.innerHTML = "<p>暂无官方包。</p>";
    } else {
      officialGrid.classList.remove("empty");
      officialGrid.innerHTML = "";
      for (const pack of officialPacks) {
        officialGrid.appendChild(createPackCard(pack, { forceOfficial: true }));
      }
    }

    if (!communityPacks.length) {
      communityGrid.classList.add("empty");
      communityGrid.innerHTML = "<p>暂无社区包。</p>";
      return;
    }

    communityGrid.classList.remove("empty");
    communityGrid.innerHTML = "";
    for (const pack of communityPacks) {
      communityGrid.appendChild(createPackCard(pack));
    }
  }

  async function fetchIndex() {
    try {
      const response = await apiPost("community/index/fetch", {
        index_url: FIXED_INDEX_URL,
      });
      cachedIndex = {
        fetched_at: response.fetched_at,
        source_url: response.source_url,
        index: response.index,
      };
      await refreshInstalledSet();
      renderCatalog();
      addLog(`索引拉取成功，共 ${response.pack_count || 0} 个条目`);
    } catch (error) {
      addLog(`索引拉取失败: ${error?.message || String(error)}`, true);
    }
  }

  async function loadCachedIndex({ silentOnMissing = false } = {}) {
    try {
      const response = await apiGet("community/index/cache");
      cachedIndex = {
        fetched_at: response.fetched_at,
        source_url: response.source_url,
        index: response.index,
      };
      await refreshInstalledSet();
      renderCatalog();
      addLog(`已读取缓存索引，共 ${response.pack_count || 0} 个条目`);
      return true;
    } catch (error) {
      const errorMessage = error?.message || String(error);
      const isMissingCache = errorMessage.includes("缓存不存在");
      if (!(silentOnMissing && isMissingCache)) {
        addLog(`读取缓存失败: ${errorMessage}`, true);
      }
      return false;
    }
  }

  async function installByPack(pack, button, options = {}) {
    const packId = String(pack?.id || "").trim();
    const source =
      pack && typeof pack.source === "object" && pack.source
        ? pack.source
        : null;
    if (!packId) {
      addLog("无效的 pack_id", true);
      return;
    }

    setLoading(button, "安装中...");
    try {
      const payload = {
        pack_id: packId,
        overwrite: Boolean(options.overwrite),
        set_as_default: Boolean(options.setAsDefault),
      };
      if (source) {
        payload.source = source;
      }

      const response = await apiPost("community/install", payload);
      addLog(`安装成功: ${response.pack_id} ${response.version || ""}`);
      await refreshInstalledSet();
      renderCatalog();
    } catch (error) {
      addLog(`安装失败(${packId}): ${error?.message || String(error)}`, true);
    } finally {
      clearLoading(button);
    }
  }

  async function installBySource(options = {}) {
    const repo = String(sourceRepoInput.value || "").trim();
    const ref = String(sourceRefInput.value || "").trim();
    const subpath = String(sourceSubpathInput.value || "").trim();

    if (!repo || !ref || !subpath) {
      addLog("手动安装参数不完整，请填写 repo/ref/subpath", true);
      return;
    }

    setLoading(installSourceBtn, "安装中...");
    try {
      const response = await apiPost("community/install", {
        source: {
          type: "github",
          repo,
          ref,
          subpath,
        },
        overwrite: Boolean(options.overwrite),
        set_as_default: Boolean(options.setAsDefault),
      });
      addLog(`按来源安装成功: ${response.pack_id} ${response.version || ""}`);
      await refreshInstalledSet();
      renderCatalog();
    } catch (error) {
      addLog(`按来源安装失败: ${error?.message || String(error)}`, true);
    } finally {
      clearLoading(installSourceBtn);
    }
  }

  installSourceBtn.addEventListener("click", () => {
    openInstallDialog("手动来源安装", async (options) => {
      await installBySource(options);
    });
  });

  installDialogCancel.addEventListener("click", () => {
    closeInstallDialog();
  });

  installDialogConfirm.addEventListener("click", () => {
    void confirmInstallDialog();
  });

  installDialog.addEventListener("click", (event) => {
    if (event.target === installDialog) {
      closeInstallDialog();
    }
  });

  await refreshInstalledSet();
  renderCatalog();
  addLog("资源广场已就绪");
  const hasCache = await loadCachedIndex({ silentOnMissing: true });
  if (hasCache) {
    await fetchIndex();
    return;
  }
  await fetchIndex();
}

void initCatalogPage();
