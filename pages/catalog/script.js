async function initCatalogPage() {
  await window.AstrBotPluginPage.ready();

  const indexUrlInput = document.getElementById("index-url-input");
  const fetchIndexBtn = document.getElementById("fetch-index-btn");
  const loadCacheBtn = document.getElementById("load-cache-btn");
  const indexMeta = document.getElementById("index-meta");
  const overwriteCheckbox = document.getElementById("overwrite-checkbox");
  const defaultCheckbox = document.getElementById("default-checkbox");
  const refreshInstalledBtn = document.getElementById("refresh-installed-btn");
  const sourceRepoInput = document.getElementById("source-repo-input");
  const sourceRefInput = document.getElementById("source-ref-input");
  const sourceSubpathInput = document.getElementById("source-subpath-input");
  const installSourceBtn = document.getElementById("install-source-btn");
  const officialGrid = document.getElementById("official-grid");
  const communityGrid = document.getElementById("community-grid");
  const officialPackCount = document.getElementById("official-pack-count");
  const communityPackCount = document.getElementById("community-pack-count");
  const logList = document.getElementById("log-list");

  let cachedIndex = null;
  let installedPackIds = new Set();

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

  function createPackCard(pack, { forceOfficial = false } = {}) {
    const card = document.createElement("article");
    card.className = `pack-card${forceOfficial ? " official" : ""}`;

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
    installBtn.addEventListener("click", () =>
      installByPackId(pack.id, installBtn),
    );

    titleRow.appendChild(titleWrap);
    titleRow.appendChild(installBtn);

    const tagRow = document.createElement("div");
    tagRow.className = "tag-row";

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

    const meta = document.createElement("div");
    meta.className = "pack-meta";
    meta.innerHTML = `
      <span>维护者: ${pack.maintainer || "未知"}</span>
      <span>协议: ${pack.license || "未知"}</span>
      <span>来源: ${pack.source?.repo || "-"}@${pack.source?.ref || "-"}</span>
    `;

    card.appendChild(titleRow);
    card.appendChild(tagRow);
    card.appendChild(desc);
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
      officialGrid.innerHTML = "<p>暂无官方包，请先拉取索引。</p>";
    } else {
      officialGrid.classList.remove("empty");
      officialGrid.innerHTML = "";
      for (const pack of officialPacks) {
        officialGrid.appendChild(createPackCard(pack, { forceOfficial: true }));
      }
    }

    if (!communityPacks.length) {
      communityGrid.classList.add("empty");
      communityGrid.innerHTML = "<p>暂无社区包，请先拉取或读取缓存索引。</p>";
      return;
    }

    communityGrid.classList.remove("empty");
    communityGrid.innerHTML = "";
    for (const pack of communityPacks) {
      communityGrid.appendChild(createPackCard(pack));
    }
  }

  function updateIndexMeta() {
    if (!cachedIndex) {
      indexMeta.textContent = "尚未加载索引。";
      return;
    }
    const sourceUrl = cachedIndex.source_url || "未知来源";
    const fetchedAt = cachedIndex.fetched_at || "未知时间";
    const count = Array.isArray(cachedIndex?.index?.packs)
      ? cachedIndex.index.packs.length
      : 0;
    indexMeta.textContent = `来源: ${sourceUrl} | 缓存时间: ${fetchedAt} | 条目: ${count}`;
  }

  async function fetchIndex() {
    const indexUrl = String(indexUrlInput.value || "").trim();
    if (!indexUrl) {
      addLog("请先填写索引 URL", true);
      return;
    }

    setLoading(fetchIndexBtn, "拉取中...");
    try {
      const response = await apiPost("community/index/fetch", {
        index_url: indexUrl,
      });
      cachedIndex = {
        fetched_at: response.fetched_at,
        source_url: response.source_url,
        index: response.index,
      };
      await refreshInstalledSet();
      updateIndexMeta();
      renderCatalog();
      addLog(`索引拉取成功，共 ${response.pack_count || 0} 个条目`);
    } catch (error) {
      addLog(`索引拉取失败: ${error?.message || String(error)}`, true);
    } finally {
      clearLoading(fetchIndexBtn);
    }
  }

  async function loadCachedIndex() {
    setLoading(loadCacheBtn, "读取中...");
    try {
      const response = await apiGet("community/index/cache");
      cachedIndex = {
        fetched_at: response.fetched_at,
        source_url: response.source_url,
        index: response.index,
      };
      await refreshInstalledSet();
      updateIndexMeta();
      renderCatalog();
      addLog(`已读取缓存索引，共 ${response.pack_count || 0} 个条目`);
    } catch (error) {
      addLog(`读取缓存失败: ${error?.message || String(error)}`, true);
    } finally {
      clearLoading(loadCacheBtn);
    }
  }

  async function installByPackId(packId, button) {
    if (!packId) {
      addLog("无效的 pack_id", true);
      return;
    }

    setLoading(button, "安装中...");
    try {
      const response = await apiPost("community/install", {
        pack_id: packId,
        overwrite: overwriteCheckbox.checked,
        set_as_default: defaultCheckbox.checked,
      });
      addLog(`安装成功: ${response.pack_id} ${response.version || ""}`);
      await refreshInstalledSet();
      renderCatalog();
    } catch (error) {
      addLog(`安装失败(${packId}): ${error?.message || String(error)}`, true);
    } finally {
      clearLoading(button);
    }
  }

  async function installBySource() {
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
        overwrite: overwriteCheckbox.checked,
        set_as_default: defaultCheckbox.checked,
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

  fetchIndexBtn.addEventListener("click", () => {
    void fetchIndex();
  });

  loadCacheBtn.addEventListener("click", () => {
    void loadCachedIndex();
  });

  refreshInstalledBtn.addEventListener("click", async () => {
    setLoading(refreshInstalledBtn, "刷新中...");
    await refreshInstalledSet();
    renderCatalog();
    addLog("已刷新安装状态");
    clearLoading(refreshInstalledBtn);
  });

  installSourceBtn.addEventListener("click", () => {
    void installBySource();
  });

  await refreshInstalledSet();
  updateIndexMeta();
  renderCatalog();
  addLog("资源广场已就绪");

  try {
    await loadCachedIndex();
  } catch (_) {
    // 首次进入时允许没有缓存
  }
}

void initCatalogPage();
