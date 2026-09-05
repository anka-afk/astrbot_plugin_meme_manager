async function initCatalogPage() {
  await window.AstrBotPluginPage.ready();

  const FIXED_INDEX_URL =
    "https://raw.githubusercontent.com/anka-afk/astrbot-meme-pack-index/main/community-index.json";

  const { applySecureNavLinks } = window.MemeUI;

  const sourceRepoInput = document.getElementById("source-repo-input");
  const sourceInstallForm = document.getElementById("source-install-form");
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
  const installProgressDialog = document.getElementById(
    "install-progress-dialog",
  );
  const installProgressPackName = document.getElementById(
    "install-progress-pack-name",
  );
  const installProgressPhase = document.getElementById(
    "install-progress-phase",
  );
  const installProgressPercent = document.getElementById(
    "install-progress-percent",
  );
  const installProgressTrack = document.getElementById(
    "install-progress-track",
  );
  const installProgressBar = document.getElementById("install-progress-bar");
  const installProgressBytes = document.getElementById(
    "install-progress-bytes",
  );
  const installProgressCancel = document.getElementById(
    "install-progress-cancel",
  );
  const officialGrid = document.getElementById("official-grid");
  const communityGrid = document.getElementById("community-grid");
  const officialPackCount = document.getElementById("official-pack-count");
  const communityPackCount = document.getElementById("community-pack-count");
  const logList = document.getElementById("log-list");
  const catalogSearch = document.getElementById("catalog-search");
  const catalogFilter = document.getElementById("catalog-filter");
  const catalogStatus = document.getElementById("catalog-status");
  const installedStatus = document.getElementById("installed-status");
  const refreshCatalogBtn = document.getElementById("refresh-catalog-btn");
  const installTask = document.getElementById("install-task");
  const installTaskName = document.getElementById("install-task-name");
  const installTaskStatus = document.getElementById("install-task-status");

  await applySecureNavLinks();

  let cachedIndex = null;
  let installedPackIds = new Set();
  let pendingInstallAction = null;
  let activeInstallJobId = "";
  let installBusy = true;
  let installedStateKnown = false;
  let indexLoading = false;

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
    item.textContent = `[${now.toLocaleTimeString("zh-CN", {
      hour12: false,
    })}] ${message}`;
    logList.prepend(item);
    while (logList.children.length > 60) {
      logList.lastElementChild.remove();
    }
    if (isError) {
      window.MemeUI?.toast?.(message, "error");
    }
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

  function setInstallBusy(busy) {
    installBusy = busy;
    installSourceBtn.disabled = busy;
    document.querySelectorAll(".pack-install").forEach((button) => {
      button.disabled = busy || button.dataset.installed === "true";
      if (busy && button.dataset.installed !== "true") {
        button.title = "请等待当前安装任务结束";
      } else {
        button.removeAttribute("title");
      }
    });
  }

  function openInstallDialog(packName, onConfirm) {
    if (installBusy) return;
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

  async function monitorInstallProgress(jobId, packName) {
    activeInstallJobId = jobId;
    setInstallBusy(true);
    installTaskName.textContent = `正在安装 · ${packName || "未命名"}`;
    installTaskStatus.textContent = "正在准备安装任务";
    installTask.classList.remove("hidden");
    installProgressPackName.textContent = `目标: ${packName || "未命名"}`;
    installProgressPhase.textContent = "正在准备安装任务";
    installProgressPercent.textContent = "0%";
    installProgressBar.style.width = "0%";
    installProgressTrack.classList.add("indeterminate");
    installProgressTrack.setAttribute("aria-valuenow", "0");
    installProgressBytes.textContent = "等待下载开始…";
    installProgressCancel.disabled = false;
    installProgressCancel.textContent = "取消安装";
    installProgressDialog.classList.remove("hidden");
    installProgressDialog.setAttribute("aria-hidden", "false");
    let consecutivePollFailures = 0;
    let installSucceeded = false;
    try {
      while (true) {
        await new Promise((resolve) =>
          window.setTimeout(
            resolve,
            Math.min(1000 + consecutivePollFailures * 1000, 10000),
          ),
        );
        let status;
        try {
          status = await apiGet("community/install/status", {
            job_id: jobId,
          });
          consecutivePollFailures = 0;
        } catch (error) {
          if (String(error?.message || "").includes("安装任务不存在或已过期")) {
            throw new Error("安装任务已失效，请刷新已安装列表后检查结果");
          }
          consecutivePollFailures += 1;
          installProgressPhase.textContent =
            "连接暂时中断，正在重连；后台安装可能仍在继续";
          installTaskStatus.textContent = installProgressPhase.textContent;
          installProgressPercent.textContent = "重连中";
          continue;
        }
        const hasKnownProgress =
          status?.progress !== null &&
          status?.progress !== undefined &&
          Number.isFinite(Number(status.progress));
        const progress = hasKnownProgress
          ? Math.max(0, Math.min(100, Number(status.progress)))
          : 0;
        const downloadedBytes = Number(status?.downloaded_bytes || 0);
        const totalBytes = Number(status?.total_bytes || 0);
        installProgressPhase.textContent = status?.message || "正在安装表情包";
        installTaskStatus.textContent = installProgressPhase.textContent;
        installProgressCancel.disabled = status?.status === "cancelling";
        installProgressCancel.textContent =
          status?.status === "cancelling" ? "正在取消…" : "取消安装";
        installProgressPercent.textContent = hasKnownProgress
          ? `${Math.round(progress)}%`
          : "进行中";
        installProgressBar.style.width = `${progress}%`;
        if (hasKnownProgress) {
          installProgressTrack.setAttribute(
            "aria-valuenow",
            String(Math.round(progress)),
          );
          installProgressTrack.removeAttribute("aria-valuetext");
        } else {
          installProgressTrack.removeAttribute("aria-valuenow");
          installProgressTrack.setAttribute(
            "aria-valuetext",
            installProgressPhase.textContent,
          );
        }
        installProgressTrack.classList.toggle(
          "indeterminate",
          !hasKnownProgress,
        );
        if (status?.phase === "downloading") {
          const downloadedMegabytes = (downloadedBytes / 1024 ** 2).toFixed(1);
          const totalMegabytes = (totalBytes / 1024 ** 2).toFixed(1);
          installProgressBytes.textContent = totalBytes
            ? `${downloadedMegabytes} MB / ${totalMegabytes} MB`
            : `已下载 ${downloadedMegabytes} MB`;
        } else {
          installProgressBytes.textContent =
            status?.phase === "queued" || status?.phase === "connecting"
              ? "连接下载源后将显示下载进度"
              : "正在处理资源包，请稍候…";
        }
        if (status?.status === "succeeded") {
          installSucceeded = true;
          await new Promise((resolve) => window.setTimeout(resolve, 400));
          return status.result || {};
        }
        if (status?.status === "failed") {
          throw new Error(status?.message || "安装失败");
        }
        if (status?.status === "cancelled") {
          const error = new Error(status?.message || "安装已取消");
          error.name = "AbortError";
          throw error;
        }
      }
    } finally {
      if (activeInstallJobId === jobId) {
        activeInstallJobId = "";
        installTask.classList.add("hidden");
        installProgressDialog.classList.add("hidden");
        installProgressDialog.setAttribute("aria-hidden", "true");
        if (installSucceeded) {
          void window.MemeUI.confirm({
            title: "表情包安装成功",
            message:
              "请前往设置中心配置使用规则，让新下载的表情包在对应会话中生效。",
            confirmText: "前往设置中心",
            cancelText: "稍后配置",
          }).then((confirmed) => {
            if (confirmed) {
              location.href = window.MemeUI.withCurrentAuthParams(
                "../settings/index.html#rules",
                { view: null, asset_token: window.MemeUI.navAuthToken || null },
              ).toString();
            }
          });
        }
      }
    }
  }

  async function installWithProgress(payload, packName) {
    const startResponse = await apiPost("community/install/start", payload);
    const jobId = String(startResponse?.job_id || "").trim();
    if (!jobId) {
      throw new Error("安装任务未返回 job_id");
    }
    return monitorInstallProgress(jobId, packName);
  }

  async function restoreActiveInstall() {
    setInstallBusy(true);
    try {
      const status = await apiGet("community/install/status");
      const jobId = String(status?.job_id || "").trim();
      if (!jobId || !["running", "cancelling"].includes(status?.status)) {
        return;
      }
      addLog("检测到后台安装任务，已恢复进度显示");
      await monitorInstallProgress(
        jobId,
        status?.source_label || "后台安装任务",
      );
      addLog("后台安装任务已完成");
      window.MemeUI?.toast?.("后台安装任务已完成", "success");
      await refreshInstalledSet();
      renderCatalog();
    } catch (error) {
      addLog(
        `后台任务状态: ${error?.message || String(error)}`,
        error?.name !== "AbortError",
      );
    } finally {
      setInstallBusy(false);
    }
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
      installedStateKnown = true;
      installedStatus.classList.add("hidden");
      catalogFilter.disabled = false;
    } catch (error) {
      installedStateKnown = false;
      catalogFilter.value = "all";
      catalogFilter.disabled = true;
      installedStatus.textContent =
        "已安装状态暂未同步，点击「刷新资源」重试。";
      installedStatus.classList.remove("hidden");
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
    // Older indexes describe the format with tags instead of explicit features.
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

    // Use jsDelivr as a fallback when the raw GitHub host is unavailable.
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

    const isInstalled =
      installedStateKnown && installedPackIds.has(String(pack.id || "").trim());
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
    installBtn.className = `pack-install${isInstalled ? " ghost" : ""}`;
    installBtn.dataset.installed = String(isInstalled);
    installBtn.disabled = isInstalled || installBusy;
    installBtn.setAttribute(
      "aria-label",
      `${isInstalled ? "已安装" : "安装"} ${
        pack.name || pack.id || "未命名资源包"
      }`,
    );
    installBtn.addEventListener("click", () => {
      openInstallDialog(pack.name || pack.id || "未命名", async (options) => {
        await installByPack(pack, installBtn, options);
      });
    });

    titleRow.appendChild(titleWrap);
    titleRow.appendChild(installBtn);

    const tagRow = document.createElement("div");
    tagRow.className = "tag-row";

    if (format.kind === "semantic") {
      const formatTag = document.createElement("span");
      formatTag.className = "tag format-tag semantic";
      const formatTagIcon = document.createElement("i");
      formatTagIcon.className = "fas fa-wand-magic-sparkles";
      const formatTagText = document.createElement("span");
      formatTagText.textContent = format.badge;
      formatTag.append(formatTagIcon, formatTagText);
      tagRow.appendChild(formatTag);
    }

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

    let formatNote = null;
    if (format.kind === "semantic") {
      formatNote = document.createElement("p");
      formatNote.className = "pack-format-note semantic";
      const formatNoteIcon = document.createElement("i");
      formatNoteIcon.className = "fas fa-cubes-stacked";
      const formatNoteText = document.createElement("span");
      formatNoteText.textContent = format.note;
      formatNote.append(formatNoteIcon, formatNoteText);
    }

    const meta = document.createElement("div");
    meta.className = "pack-meta";
    const maintainerMeta = document.createElement("span");
    maintainerMeta.textContent = `维护者: ${pack.maintainer || "未知"}`;
    const licenseMeta = document.createElement("span");
    licenseMeta.textContent = `协议: ${pack.license || "未知"}`;
    const sourceMeta = document.createElement("span");
    sourceMeta.textContent = `来源: ${pack.source?.repo || "-"}@${
      pack.source?.ref || "-"
    }`;
    meta.append(maintainerMeta, licenseMeta, sourceMeta);

    card.appendChild(createPackCover(pack));
    card.appendChild(titleRow);
    card.appendChild(tagRow);
    card.appendChild(desc);
    if (formatNote) {
      card.appendChild(formatNote);
    }
    card.appendChild(meta);
    return card;
  }

  function renderCatalog() {
    if (!cachedIndex) return;
    const query = catalogSearch.value.trim().toLocaleLowerCase();
    const filter = catalogFilter.value;
    const allPacks = readPacksFromCache();
    const packs = allPacks.filter((pack) => {
      const isInstalled =
        installedStateKnown &&
        installedPackIds.has(String(pack.id || "").trim());
      if (filter === "installed" && !isInstalled) return false;
      if (filter === "available" && isInstalled) return false;
      const searchable = [
        pack.name,
        pack.id,
        pack.description,
        pack.maintainer,
        pack.source?.repo,
        ...(Array.isArray(pack.tags) ? pack.tags : []),
      ]
        .join(" ")
        .toLocaleLowerCase();
      return !query || searchable.includes(query);
    });
    for (const [grid, count, official] of [
      [officialGrid, officialPackCount, true],
      [communityGrid, communityPackCount, false],
    ]) {
      const group = packs.filter((pack) => isOfficialPack(pack) === official);
      const total = allPacks.filter(
        (pack) => isOfficialPack(pack) === official,
      ).length;
      count.textContent =
        query || filter !== "all"
          ? `${group.length} / ${total}`
          : String(total);
      grid.replaceChildren();
      grid.classList.toggle("empty", !group.length);
      if (!group.length) {
        const message = document.createElement("p");
        message.textContent =
          total && (query || filter !== "all")
            ? "没有符合条件的资源包，试试其他关键词或安装状态。"
            : `暂无${official ? "官方" : "社区"}包。`;
        grid.appendChild(message);
      } else {
        for (const pack of group) {
          grid.appendChild(createPackCard(pack, { forceOfficial: official }));
        }
      }
    }
  }

  async function fetchIndex() {
    if (indexLoading) return;
    indexLoading = true;
    setLoading(refreshCatalogBtn, "刷新中…");
    catalogStatus.classList.remove("error");
    catalogStatus.textContent = cachedIndex
      ? "正在更新资源，当前显示上次缓存。"
      : "正在获取资源列表…";
    officialGrid.setAttribute("aria-busy", "true");
    communityGrid.setAttribute("aria-busy", "true");
    if (!cachedIndex) {
      for (const grid of [officialGrid, communityGrid]) {
        grid.textContent = "正在读取资源…";
      }
    }
    const installedRefresh = refreshInstalledSet();
    try {
      const response = await apiPost("community/index/fetch", {
        index_url: FIXED_INDEX_URL,
      });
      if (!Array.isArray(response?.index?.packs)) {
        throw new Error("资源列表格式不正确，请稍后重试");
      }
      cachedIndex = {
        fetched_at: response.fetched_at,
        source_url: response.source_url,
        index: response.index,
      };
      await installedRefresh;
      renderCatalog();
      catalogStatus.textContent = `已同步 ${
        readPacksFromCache().length
      } 个资源包 · ${new Date().toLocaleTimeString("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
      })}`;
      addLog(`索引拉取成功，共 ${response.pack_count || 0} 个条目`);
    } catch (error) {
      await installedRefresh;
      renderCatalog();
      catalogStatus.classList.add("error");
      catalogStatus.textContent = cachedIndex
        ? "更新失败，正在显示上次缓存。可点击「刷新资源」重试。"
        : `资源加载失败：${
            error?.message || String(error)
          }。请点击「刷新资源」重试。`;
      if (!cachedIndex) {
        for (const grid of [officialGrid, communityGrid]) {
          grid.textContent = "资源暂时不可用，请刷新重试。";
        }
      }
      addLog(`索引拉取失败: ${error?.message || String(error)}`, true);
    } finally {
      indexLoading = false;
      clearLoading(refreshCatalogBtn);
      officialGrid.setAttribute("aria-busy", "false");
      communityGrid.setAttribute("aria-busy", "false");
    }
  }

  async function loadCachedIndex({ silentOnMissing = false } = {}) {
    try {
      const response = await apiGet("community/index/cache");
      if (!Array.isArray(response?.index?.packs)) return false;
      cachedIndex = {
        fetched_at: response.fetched_at,
        source_url: response.source_url,
        index: response.index,
      };
      await refreshInstalledSet();
      renderCatalog();
      catalogStatus.textContent = `已读取 ${
        readPacksFromCache().length
      } 个缓存资源包，正在检查更新…`;
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
    if (installBusy) return;
    const packId = String(pack?.id || "").trim();
    const source =
      pack && typeof pack.source === "object" && pack.source
        ? pack.source
        : null;
    if (!packId) {
      addLog("无效的 pack_id", true);
      return;
    }

    setInstallBusy(true);
    setLoading(button, "安装中…");
    try {
      const payload = {
        pack_id: packId,
        overwrite: Boolean(options.overwrite),
        set_as_default: Boolean(options.setAsDefault),
      };
      if (source) {
        payload.source = source;
      }

      const response = await installWithProgress(payload, pack?.name || packId);
      addLog(
        `安装成功: ${response.pack_id || packId} ${response.version || ""}`,
      );
      window.MemeUI?.toast?.(
        `已安装 ${pack.name || response.pack_id}`,
        "success",
      );
      await refreshInstalledSet();
      renderCatalog();
    } catch (error) {
      if (error?.name === "AbortError") {
        addLog(`已取消安装: ${pack.name || packId}`);
        window.MemeUI?.toast?.("已取消安装", "info");
      } else {
        addLog(`安装失败(${packId}): ${error?.message || String(error)}`, true);
      }
    } finally {
      clearLoading(button);
      setInstallBusy(false);
    }
  }

  async function installBySource(options = {}) {
    if (installBusy) return;
    const repo = String(sourceRepoInput.value || "").trim();
    const ref = String(sourceRefInput.value || "").trim();
    const subpath = normalizeGithubSubpath(sourceSubpathInput.value);

    setInstallBusy(true);
    setLoading(installSourceBtn, "安装中…");
    try {
      const response = await installWithProgress(
        {
          source: {
            type: "github",
            repo,
            ref,
            subpath,
          },
          overwrite: Boolean(options.overwrite),
          set_as_default: Boolean(options.setAsDefault),
        },
        `${repo}@${ref}`,
      );
      addLog(
        `按来源安装成功: ${response.pack_id || repo} ${response.version || ""}`,
      );
      window.MemeUI?.toast?.(`已安装 ${response.pack_id || repo}`, "success");
      await refreshInstalledSet();
      renderCatalog();
    } catch (error) {
      if (error?.name === "AbortError") {
        addLog(`已取消安装: ${repo}@${ref}`);
        window.MemeUI?.toast?.("已取消安装", "info");
      } else {
        addLog(`按来源安装失败: ${error?.message || String(error)}`, true);
      }
    } finally {
      clearLoading(installSourceBtn);
      setInstallBusy(false);
    }
  }

  sourceInstallForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (installBusy) return;
    const repo = sourceRepoInput.value.trim();
    const ref = sourceRefInput.value.trim();
    const subpath = normalizeGithubSubpath(sourceSubpathInput.value);
    sourceRepoInput.setCustomValidity(
      /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo)
        ? ""
        : "请填写 owner/repo 格式的 GitHub 仓库名",
    );
    sourceRefInput.setCustomValidity(ref ? "" : "请填写分支名、标签或提交 ID");
    sourceSubpathInput.setCustomValidity(
      subpath && !subpath.includes("\\") && !subpath.split("/").includes("..")
        ? ""
        : "请填写有效目录，根目录填 .，不能包含 .. 或反斜杠",
    );
    if (!sourceInstallForm.reportValidity()) return;
    openInstallDialog(`${repo}@${ref} · ${subpath}`, async (options) => {
      await installBySource(options);
    });
  });

  for (const input of [sourceRepoInput, sourceRefInput, sourceSubpathInput]) {
    input.addEventListener("input", () => input.setCustomValidity(""));
  }

  catalogSearch.addEventListener("input", renderCatalog);
  catalogFilter.addEventListener("change", renderCatalog);
  refreshCatalogBtn.addEventListener("click", () => void fetchIndex());
  document
    .getElementById("install-progress-hide")
    .addEventListener("click", () => {
      installProgressDialog.classList.add("hidden");
      installProgressDialog.setAttribute("aria-hidden", "true");
    });
  document
    .getElementById("show-install-progress")
    .addEventListener("click", () => {
      if (!activeInstallJobId) return;
      installProgressDialog.classList.remove("hidden");
      installProgressDialog.setAttribute("aria-hidden", "false");
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

  installProgressCancel.addEventListener("click", async () => {
    if (!activeInstallJobId || installProgressCancel.disabled) return;
    installProgressCancel.disabled = true;
    installProgressCancel.textContent = "正在取消...";
    try {
      await apiPost("community/install/cancel", {
        job_id: activeInstallJobId,
      });
      installProgressPhase.textContent = "正在取消安装，请稍候";
    } catch (error) {
      installProgressCancel.disabled = false;
      installProgressCancel.textContent = "取消安装";
      addLog(`取消安装失败: ${error?.message || String(error)}`, true);
    }
  });

  addLog("资源广场已就绪");
  void restoreActiveInstall();
  await loadCachedIndex({ silentOnMissing: true });
  await fetchIndex();
}

void initCatalogPage().catch((error) => {
  const status = document.getElementById("catalog-status");
  status.textContent = `页面初始化失败：${
    error?.message || String(error)
  }。请重新打开页面。`;
  status.classList.add("error");
  for (const id of ["official-grid", "community-grid"]) {
    document.getElementById(id).textContent = "资源暂时不可用。";
  }
});
