async function initApp() {
  await window.AstrBotPluginPage.ready();
  window.AstrBotPluginPage.getContext();

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

  await applySecureNavLinks();

  const managedPackIdFromUrl = String(
    new URLSearchParams(window.location.search).get("managed_pack_id") || "",
  ).trim();

  async function apiGet(endpoint, params = {}) {
    const mergedParams = { ...params };
    const managedPackId = String(
      activeManagePackId || managePackSelect?.value || "",
    ).trim();
    if (
      managedPackId &&
      [
        "emoji",
        "emotions",
        "meme_image",
        "meme_image_data",
        "meme_image_semantic",
        "semantic/reviews",
        "img_host/sync/status",
        "img_host/sync/task_status",
      ].includes(endpoint)
    ) {
      mergedParams.managed_pack_id = managedPackId;
    }
    return await window.AstrBotPluginPage.apiGet(endpoint, mergedParams);
  }

  async function apiPost(endpoint, body = {}) {
    const mergedBody = { ...body };
    const selectedPackId = String(
      activeManagePackId || managePackSelect?.value || "",
    ).trim();
    if (
      selectedPackId &&
      defaultManagePackId &&
      selectedPackId !== defaultManagePackId &&
      ["emoji/", "category/"].some((prefix) => endpoint.startsWith(prefix))
    ) {
      throw new Error(
        "当前为管理视图模式，仅支持浏览。请切回默认管理包后再执行编辑操作。",
      );
    }
    if (selectedPackId && endpoint.startsWith("img_host/sync/")) {
      mergedBody.managed_pack_id = selectedPackId;
    }
    if (selectedPackId && endpoint.startsWith("semantic/")) {
      mergedBody.pack_id = selectedPackId;
    }
    return await window.AstrBotPluginPage.apiPost(endpoint, mergedBody);
  }

  const selectionState = {
    enabled: false,
    items: new Map(),
  };
  let latestEmojiData = {};
  let latestTagDescriptions = {};
  let semanticReviewByPath = new Map();
  let semanticReviewStatistics = {};
  let semanticReviewAvailable = false;
  let activeSemanticReviewFilter = "all";
  let dangerConfirmResolver = null;
  let dangerConfirmStage = "ack";
  let dangerConfirmTimer = null;
  let dangerConfirmConfig = null;

  const toggleSelectionModeBtn = document.getElementById(
    "toggle-selection-mode-btn",
  );
  const batchMoveBtn = document.getElementById("batch-move-btn");
  const batchDeleteBtn = document.getElementById("batch-delete-btn");
  const clearAllBtn = document.getElementById("clear-all-btn");
  const selectionSummary = document.getElementById("selection-summary");
  const toastContainer = document.getElementById("toast-container");
  const batchContextMenu = document.getElementById("batch-context-menu");
  const batchContextMenuTitle = document.getElementById(
    "batch-context-menu-title",
  );
  const batchContextMenuSubtitle = document.getElementById(
    "batch-context-menu-subtitle",
  );
  const contextMenuDeleteBtn = document.getElementById(
    "context-menu-delete-btn",
  );
  const contextMenuMoveBtn = document.getElementById("context-menu-move-btn");
  const contextMenuCopyBtn = document.getElementById("context-menu-copy-btn");
  const contextMenuPasteBtn = document.getElementById("context-menu-paste-btn");
  const consoleToggleBtn = document.getElementById("console-toggle-btn");
  const directoryToggleBtn = document.getElementById("directory-toggle-btn");
  const sidebarBackdrop = document.getElementById("sidebar-backdrop");
  const leftPanel = document.getElementById("app-sidebar-panel");
  const directoryPanel = document.getElementById("app-directory-panel");
  const dragHud = document.getElementById("drag-hud");
  const dragHudLabel = document.getElementById("drag-hud-label");
  const dragHudCaption = document.getElementById("drag-hud-caption");
  const imagePreviewModalRoot = document.getElementById("image-preview-modal");
  const imagePreviewImg = document.getElementById("image-preview-img");
  const imagePreviewLoading = document.getElementById("image-preview-loading");
  const imagePreviewOriginalBtn = document.getElementById(
    "image-preview-original-btn",
  );
  const imagePreviewCloseBtn = document.getElementById(
    "image-preview-close-btn",
  );
  const imagePreviewSemantic = document.getElementById(
    "image-preview-semantic",
  );
  const imagePreviewSemanticState = document.getElementById(
    "image-preview-semantic-state",
  );
  const imagePreviewSemanticFilename = document.getElementById(
    "image-preview-semantic-filename",
  );
  const imagePreviewSemanticCaption = document.getElementById(
    "image-preview-semantic-caption",
  );
  const imagePreviewSemanticTagsWrap = document.getElementById(
    "image-preview-semantic-tags-wrap",
  );
  const imagePreviewSemanticTags = document.getElementById(
    "image-preview-semantic-tags",
  );
  const imagePreviewVisibleTextWrap = document.getElementById(
    "image-preview-visible-text-wrap",
  );
  const imagePreviewVisibleText = document.getElementById(
    "image-preview-visible-text",
  );
  const imagePreviewSemanticIndex = document.getElementById(
    "image-preview-semantic-index",
  );
  const imagePreviewCategoryTag = document.getElementById(
    "image-preview-category-tag",
  );
  const imagePreviewCategoryReview = document.getElementById(
    "image-preview-category-review",
  );
  const imagePreviewCategoryReviewState = document.getElementById(
    "image-preview-category-review-state",
  );
  const imagePreviewCategoryReviewReason = document.getElementById(
    "image-preview-category-review-reason",
  );
  const imagePreviewReclassification = document.getElementById(
    "image-preview-reclassification",
  );
  const imagePreviewCategoryConfirmBtn = document.getElementById(
    "image-preview-category-confirm-btn",
  );
  const imagePreviewReviewActions = document.getElementById(
    "image-preview-review-actions",
  );
  const imagePreviewReviewEditBtn = document.getElementById(
    "image-preview-review-edit-btn",
  );
  const imagePreviewSourceState = document.getElementById(
    "image-preview-source-state",
  );
  const imagePreviewVectorState = document.getElementById(
    "image-preview-vector-state",
  );
  const imagePreviewDuplicateNotice = document.getElementById(
    "image-preview-duplicate-notice",
  );
  const imagePreviewSemanticActions = document.getElementById(
    "image-preview-semantic-actions",
  );
  const imagePreviewEditBtn = document.getElementById("image-preview-edit-btn");
  const imagePreviewRestoreAutoBtn = document.getElementById(
    "image-preview-restore-auto-btn",
  );
  const imagePreviewEditForm = document.getElementById(
    "image-preview-edit-form",
  );
  const imagePreviewFixedTags = document.getElementById(
    "image-preview-fixed-tags",
  );
  const imagePreviewReviewInstruction = document.getElementById(
    "image-preview-review-instruction",
  );
  const imagePreviewReviewRewriteBtn = document.getElementById(
    "image-preview-review-rewrite-btn",
  );
  const imagePreviewReviewRewriteStatus = document.getElementById(
    "image-preview-review-rewrite-status",
  );
  const imagePreviewTargetCategory = document.getElementById(
    "image-preview-target-category",
  );
  const imagePreviewCaptionInput = document.getElementById(
    "image-preview-caption-input",
  );
  const imagePreviewTagsInput = document.getElementById(
    "image-preview-tags-input",
  );
  const imagePreviewVisibleTextInput = document.getElementById(
    "image-preview-visible-text-input",
  );
  const imagePreviewCategoryDecision = document.getElementById(
    "image-preview-category-decision",
  );
  const imagePreviewEditScope = document.getElementById(
    "image-preview-edit-scope",
  );
  const imagePreviewEditCancelBtn = document.getElementById(
    "image-preview-edit-cancel-btn",
  );
  const imagePreviewSaveBtn = document.getElementById("image-preview-save-btn");
  const imagePreviewSaveVectorBtn = document.getElementById(
    "image-preview-save-vector-btn",
  );
  const semanticReviewStats = document.getElementById("semantic-review-stats");
  const semanticReviewToolbar = document.getElementById(
    "semantic-review-toolbar",
  );
  const moveTargetModalRoot = document.getElementById("move-target-modal");
  const moveTargetModalTitle = document.getElementById(
    "move-target-modal-title",
  );
  const moveTargetModalDescription = document.getElementById(
    "move-target-modal-description",
  );
  const moveTargetList = document.getElementById("move-target-list");
  const moveTargetCancelBtn = document.getElementById("move-target-cancel-btn");
  const categoryEditModalRoot = document.getElementById("category-edit-modal");
  const categoryEditModalTitle = document.getElementById(
    "category-edit-modal-title",
  );
  const categoryEditModalDescription = document.getElementById(
    "category-edit-modal-description",
  );
  const categoryEditNameInput = document.getElementById(
    "category-edit-name-input",
  );
  const categoryEditDescInput = document.getElementById(
    "category-edit-desc-input",
  );
  const categoryEditCancelBtn = document.getElementById(
    "category-edit-cancel-btn",
  );
  const categoryEditSaveBtn = document.getElementById("category-edit-save-btn");
  const confirmModalRoot = document.getElementById("confirm-modal");
  const confirmModalTitle = document.getElementById("confirm-modal-title");
  const confirmModalDescription = document.getElementById(
    "confirm-modal-description",
  );
  const confirmModalCancelBtn = document.getElementById(
    "confirm-modal-cancel-btn",
  );
  const confirmModalConfirmBtn = document.getElementById(
    "confirm-modal-confirm-btn",
  );
  const dangerModalRoot = document.getElementById("danger-confirm-modal");
  const dangerModalTitle = document.getElementById("danger-modal-title");
  const dangerModalDescription = document.getElementById(
    "danger-modal-description",
  );
  const dangerModalStageText = document.getElementById(
    "danger-modal-stage-text",
  );
  const dangerModalAcknowledge = document.getElementById("danger-modal-ack");
  const dangerModalCancelBtn = document.getElementById(
    "danger-modal-cancel-btn",
  );
  const dangerModalConfirmBtn = document.getElementById(
    "danger-modal-confirm-btn",
  );
  const imgHostSyncProgress = document.getElementById("img-host-sync-progress");
  const imgHostSyncProgressText = document.getElementById(
    "img-host-sync-progress-text",
  );
  const managePackSelect = document.getElementById("manage-pack-select");
  const packSemanticStatus = document.getElementById("pack-semantic-status");
  const packSemanticStatusText = document.getElementById(
    "pack-semantic-status-text",
  );
  const packVectorStatus = document.getElementById("pack-vector-status");
  const packVectorStatusText = document.getElementById(
    "pack-vector-status-text",
  );
  const rebuildPackVectorsBtn = document.getElementById(
    "rebuild-pack-vectors-btn",
  );
  const switchManagePackBtn = document.getElementById("switch-manage-pack-btn");
  const deleteManagePackBtn = document.getElementById("delete-manage-pack-btn");
  const transferCurrentPack = document.getElementById("transfer-current-pack");
  const exportModeInputs = Array.from(
    document.querySelectorAll('input[name="export-mode"]'),
  );
  const exportModeBackup = document.getElementById("export-mode-backup");
  const vectorBackupHint = document.getElementById("vector-backup-hint");
  const exportPackDownloadBtn = document.getElementById(
    "export-pack-download-btn",
  );
  const exportPackResult = document.getElementById("export-pack-result");
  const packImportDropzone = document.getElementById("pack-import-dropzone");
  const packImportFile = document.getElementById("pack-import-file");
  const packImportFileLabel = document.getElementById("pack-import-file-label");
  const packImportPreview = document.getElementById("pack-import-preview");
  const packImportPreviewName = document.getElementById(
    "pack-import-preview-name",
  );
  const packImportPreviewFormat = document.getElementById(
    "pack-import-preview-format",
  );
  const packImportImageCount = document.getElementById(
    "pack-import-image-count",
  );
  const packImportCategoryCount = document.getElementById(
    "pack-import-category-count",
  );
  const packImportSemanticCount = document.getElementById(
    "pack-import-semantic-count",
  );
  const packImportVectorState = document.getElementById(
    "pack-import-vector-state",
  );
  const packImportWarning = document.getElementById("pack-import-warning");
  const packImportSetDefault = document.getElementById(
    "pack-import-set-default",
  );
  const packImportOverwrite = document.getElementById("pack-import-overwrite");
  const packImportOverwriteManual = document.getElementById(
    "pack-import-overwrite-manual",
  );
  const packImportResetBtn = document.getElementById("pack-import-reset-btn");
  const packImportConfirmBtn = document.getElementById(
    "pack-import-confirm-btn",
  );
  const packImportResult = document.getElementById("pack-import-result");
  let defaultManagePackId = "";
  let activeManagePackId = "";
  let managePacksById = new Map();
  let pendingPackImportToken = "";
  let exportCapabilityRequestId = 0;
  let managePackVectorStatusRequestId = 0;
  let latestManagePackVectorStatus = null;
  let latestManagePackVectorStatusId = "";
  let confirmResolver = null;
  let confirmRestoreFocusElement = null;
  const MOBILE_LAYOUT_MEDIA = "(max-width: 960px)";
  const DRAG_HUD_OFFSET_X = 18;
  const DRAG_HUD_OFFSET_Y = 88;
  const LONG_PRESS_DURATION_MS = 2000;
  const LONG_PRESS_TICK_MS = 60;
  const LONG_PRESS_CANCEL_DISTANCE_PX = 18;
  const DRAG_READY_TIMEOUT_MS = 15000;
  const longPressState = {
    emojiItem: null,
    pointerId: null,
    startTime: 0,
    startX: 0,
    startY: 0,
    currentX: 0,
    currentY: 0,
    timeoutId: null,
    intervalId: null,
  };
  const dragModeState = {
    items: [],
    timeoutId: null,
    pointerId: null,
    activeCategory: null,
    isPointerDragging: false,
    captureElement: null,
    autoScrollFrameId: null,
    lastClientX: 0,
    lastClientY: 0,
  };
  const clipboardState = {
    items: [],
  };
  const contextMenuState = {
    items: [],
    targetCategory: null,
  };
  const uploadStateByCategory = new Map();
  let initialStatusTimerId = null;
  let activeCategoryEdit = null;
  let pendingMoveTargetItems = [];
  let imagePreviewState = null;
  let emptyPackGuideShown = false;
  let firstUseCatalogGuideShown = false;

  function formatPackOptionLabel(pack) {
    const name = String(pack?.name || pack?.id || "未命名");
    const id = String(pack?.id || "").trim();
    const imageCount = Number(pack?.image_count || 0);
    const semanticStatus = String(pack?.semantic_status || "none");
    const semanticLabel =
      semanticStatus === "complete"
        ? "已语义化"
        : semanticStatus === "partial"
          ? "部分语义化"
          : "未语义化";
    return `${name} (${id}) · ${imageCount} 张 · ${semanticLabel}`;
  }

  function setPackTransferResult(element, message = "", type = "") {
    if (!element) {
      return;
    }
    element.textContent = String(message || "");
    element.classList.toggle("success", type === "success");
    element.classList.toggle("error", type === "error");
  }

  function selectedExportMode() {
    return (
      exportModeInputs.find((input) => input.checked)?.value || "share"
    );
  }

  function updateExportModeAppearance() {
    exportModeInputs.forEach((input) => {
      const option = input.closest(".export-mode-option");
      option?.classList.toggle("selected", input.checked);
      option?.classList.toggle("disabled", input.disabled);
    });
    if (exportPackDownloadBtn) {
      exportPackDownloadBtn.innerHTML =
        selectedExportMode() === "backup"
          ? '<i class="fas fa-download icon"></i>下载自用备份'
          : '<i class="fas fa-download icon"></i>下载分享版';
    }
  }

  async function refreshPackExportCapability(packId = activeManagePackId) {
    const normalizedPackId = String(packId || "").trim();
    const requestId = ++exportCapabilityRequestId;
    const pack = managePacksById.get(normalizedPackId);
    if (transferCurrentPack) {
      transferCurrentPack.textContent = pack
        ? `当前：${pack.name || pack.id} · ${Number(pack.image_count || 0)} 张`
        : normalizedPackId
          ? `当前：${normalizedPackId}`
          : "暂无可导出的表情包";
    }
    if (!normalizedPackId) {
      if (exportPackDownloadBtn) exportPackDownloadBtn.disabled = true;
      if (exportModeBackup) exportModeBackup.disabled = true;
      if (vectorBackupHint) vectorBackupHint.textContent = "当前没有可导出的表情包。";
      updateExportModeAppearance();
      return;
    }

    if (exportPackDownloadBtn) exportPackDownloadBtn.disabled = false;
    if (exportModeBackup) {
      if (exportModeBackup.checked) {
        const shareInput = document.getElementById("export-mode-share");
        if (shareInput) shareInput.checked = true;
      }
      exportModeBackup.disabled = true;
    }
    if (vectorBackupHint) vectorBackupHint.textContent = "正在检查当前表情包的向量状态…";
    updateExportModeAppearance();
    try {
      const status = await apiGet("packs/export/status", {
        pack_id: normalizedPackId,
      });
      if (requestId !== exportCapabilityRequestId) {
        return;
      }
      const available = Boolean(status?.vector_backup_available);
      if (exportModeBackup) exportModeBackup.disabled = !available;
      if (!available && exportModeBackup?.checked) {
        const shareInput = document.getElementById("export-mode-share");
        if (shareInput) shareInput.checked = true;
      }
      if (vectorBackupHint) {
        const modelHint = [
          String(status?.embedding_model || "").trim(),
          Number(status?.embedding_dimension || 0)
            ? `${Number(status.embedding_dimension)} 维`
            : "",
        ]
          .filter(Boolean)
          .join(" · ");
        vectorBackupHint.textContent = available
          ? `包含完整本机向量${modelHint ? `（${modelHint}）` : ""}，适合迁回相同模型环境。`
          : "当前没有完整向量；完成语义化并建立索引后才可导出。";
      }
    } catch (error) {
      if (requestId !== exportCapabilityRequestId) {
        return;
      }
      if (exportModeBackup) exportModeBackup.disabled = true;
      if (vectorBackupHint) {
        vectorBackupHint.textContent = "暂时无法读取向量状态，请稍后重试。";
      }
    } finally {
      if (requestId === exportCapabilityRequestId) {
        updateExportModeAppearance();
      }
    }
  }

  async function downloadCurrentPack() {
    const packId = String(activeManagePackId || "").trim();
    if (!packId) {
      showToast("当前没有可导出的表情包。", "warning", "无法导出");
      return;
    }
    const mode = selectedExportMode();
    setButtonBusy(exportPackDownloadBtn, "正在生成压缩包…");
    setPackTransferResult(exportPackResult, "正在整理文件，请不要关闭页面。", "");
    try {
      await window.AstrBotPluginPage.download("packs/export/download", {
        pack_id: packId,
        mode,
      });
      const label = mode === "backup" ? "带向量自用备份" : "无向量分享版";
      setPackTransferResult(
        exportPackResult,
        `${label}已生成，并已开始下载。`,
        "success",
      );
      showToast(`${label}已开始下载。`, "success", "导出成功");
    } catch (error) {
      setPackTransferResult(
        exportPackResult,
        error?.message || String(error),
        "error",
      );
      showToast(error?.message || String(error), "error", "导出失败");
    } finally {
      restoreButton(exportPackDownloadBtn);
      updateExportModeAppearance();
    }
  }

  function packSupportsVectorStatus(packId) {
    const pack = managePacksById.get(String(packId || "").trim());
    return pack?.supports_vector_rebuild === true;
  }

  function hideManagePackVectorStatus() {
    packVectorStatus?.classList.add("hidden");
    rebuildPackVectorsBtn?.classList.add("hidden");
    latestManagePackVectorStatus = null;
    latestManagePackVectorStatusId = "";
  }

  function renderManagePackVectorStatus(packId, status, state = "ready") {
    const normalizedPackId = String(packId || "").trim();
    if (!normalizedPackId || !packSupportsVectorStatus(normalizedPackId)) {
      hideManagePackVectorStatus();
      return;
    }

    packVectorStatus?.classList.remove(
      "hidden",
      "vector-ready",
      "vector-rebuild",
      "vector-unconfigured",
      "vector-pending",
      "vector-loading",
    );
    rebuildPackVectorsBtn?.classList.remove("hidden");

    if (state === "loading") {
      packVectorStatus?.classList.add("vector-loading");
      if (packVectorStatusText) {
        packVectorStatusText.textContent = "正在读取向量维度…";
      }
      if (rebuildPackVectorsBtn) {
        rebuildPackVectorsBtn.disabled = true;
        rebuildPackVectorsBtn.innerHTML =
          '<i class="fas fa-spinner fa-spin icon"></i>读取中';
      }
      return;
    }

    if (state === "error" || !status) {
      packVectorStatus?.classList.add("vector-pending");
      if (packVectorStatusText) {
        packVectorStatusText.textContent = "向量状态读取失败";
      }
      if (packVectorStatus) {
        packVectorStatus.title = "暂时无法读取当前包的向量状态，请稍后重试。";
      }
      if (rebuildPackVectorsBtn) {
        rebuildPackVectorsBtn.disabled = true;
        rebuildPackVectorsBtn.innerHTML =
          '<i class="fas fa-arrows-rotate icon"></i>状态不可用';
      }
      return;
    }

    const providerReady = Boolean(status.embedding_provider_ready);
    const captionComplete = Boolean(status.semantic_caption_complete);
    const indexReady = Boolean(status.index_ready);
    const rebuildRequired = Boolean(status.dimension_rebuild_required);
    const configuredDimension = Number(
      status.embedding_configured_dimension || 0,
    );
    const indexDimension = Number(status.index_embedding_dimension || 0);
    const taskStatus = String(status.task_status || "idle");
    const taskBusy = ["running", "paused"].includes(taskStatus);
    const currentDimensionLabel = configuredDimension
      ? `${configuredDimension} 维`
      : "维度待检测";
    const modelLabel = [
      String(status.embedding_provider_id || "").trim(),
      String(status.embedding_model || "").trim(),
    ]
      .filter(Boolean)
      .join(" / ");

    if (!providerReady) {
      packVectorStatus?.classList.add("vector-unconfigured");
      if (packVectorStatusText) {
        packVectorStatusText.textContent = indexDimension
          ? `索引 ${indexDimension} 维 · 未配置向量模型`
          : "未配置向量模型";
      }
      if (packVectorStatus) {
        packVectorStatus.title =
          "当前没有可用的向量模型。已有语义描述不会丢失，配置模型后即可在此重建。";
      }
      if (rebuildPackVectorsBtn) {
        rebuildPackVectorsBtn.disabled = true;
        rebuildPackVectorsBtn.title =
          "请先在插件配置中选择并启用向量模型。";
        rebuildPackVectorsBtn.innerHTML =
          '<i class="fas fa-circle-exclamation icon"></i>先配置向量模型';
      }
      return;
    }

    if (taskBusy) {
      packVectorStatus?.classList.add("vector-pending");
      if (packVectorStatusText) {
        packVectorStatusText.textContent =
          taskStatus === "paused"
            ? `当前 ${currentDimensionLabel} · 任务已暂停`
            : `当前 ${currentDimensionLabel} · 正在处理`;
      }
    } else if (!captionComplete) {
      packVectorStatus?.classList.add("vector-pending");
      if (packVectorStatusText) {
        packVectorStatusText.textContent =
          `当前 ${currentDimensionLabel} · 描述未完成`;
      }
    } else if (indexReady && !rebuildRequired) {
      packVectorStatus?.classList.add("vector-ready");
      if (packVectorStatusText) {
        packVectorStatusText.textContent =
          `向量 ${indexDimension || configuredDimension || "未知"} 维`;
      }
    } else {
      packVectorStatus?.classList.add("vector-rebuild");
      if (packVectorStatusText) {
        const oldIndexHint =
          indexDimension && indexDimension !== configuredDimension
            ? ` · 原索引 ${indexDimension} 维`
            : "";
        packVectorStatusText.textContent =
          `当前 ${currentDimensionLabel}${oldIndexHint} · 待重建`;
      }
    }

    if (packVectorStatus) {
      const indexHint = indexDimension
        ? `包内索引为 ${indexDimension} 维。`
        : "当前包还没有可用的本机向量索引。";
      packVectorStatus.title =
        `${modelLabel ? `当前模型：${modelLabel}；` : ""}` +
        `模型维度：${currentDimensionLabel}；${indexHint}`;
    }
    if (rebuildPackVectorsBtn) {
      rebuildPackVectorsBtn.disabled = taskBusy || !captionComplete;
      rebuildPackVectorsBtn.title = taskBusy
        ? "语义任务进行中，结束后才能重建向量。"
        : !captionComplete
          ? "请先完成当前包的全部语义描述。"
          : "使用当前向量模型重新建立本机向量索引。";
      rebuildPackVectorsBtn.innerHTML = taskBusy
        ? '<i class="fas fa-spinner fa-spin icon"></i>正在处理'
        : !captionComplete
          ? '<i class="fas fa-clock icon"></i>等待语义描述'
          : `<i class="fas fa-arrows-rotate icon"></i>按${
              configuredDimension ? ` ${configuredDimension} 维` : "当前维度"
            }重建`;
    }
  }

  async function refreshManagePackVectorStatus(packId = activeManagePackId) {
    const normalizedPackId = String(packId || "").trim();
    const requestId = ++managePackVectorStatusRequestId;
    if (!normalizedPackId || !packSupportsVectorStatus(normalizedPackId)) {
      hideManagePackVectorStatus();
      return null;
    }

    renderManagePackVectorStatus(normalizedPackId, null, "loading");
    try {
      const status = await apiGet("semantic/status", {
        pack_id: normalizedPackId,
      });
      if (requestId !== managePackVectorStatusRequestId) {
        return null;
      }
      latestManagePackVectorStatus = status;
      latestManagePackVectorStatusId = normalizedPackId;
      renderManagePackVectorStatus(normalizedPackId, status);
      return status;
    } catch (error) {
      if (requestId === managePackVectorStatusRequestId) {
        latestManagePackVectorStatus = null;
        latestManagePackVectorStatusId = "";
        renderManagePackVectorStatus(normalizedPackId, null, "error");
      }
      console.warn("读取当前图包向量状态失败:", error);
      return null;
    }
  }

  async function performVectorRebuild(packId, knownStatus = null) {
    const normalizedPackId = String(packId || "").trim();
    if (!normalizedPackId || !packSupportsVectorStatus(normalizedPackId)) {
      showToast("旧版表情包不支持向量重建。", "warning", "无法重建");
      return false;
    }
    const status =
      knownStatus ||
      (latestManagePackVectorStatusId === normalizedPackId
        ? latestManagePackVectorStatus
        : null) ||
      (await refreshManagePackVectorStatus(normalizedPackId));
    if (!status) {
      showToast("暂时无法读取向量状态，请稍后重试。", "error", "无法重建");
      return false;
    }
    if (!status.embedding_provider_ready) {
      showToast(
        "请先在插件配置中选择并启用向量模型。",
        "warning",
        "未配置向量模型",
      );
      return false;
    }
    if (!status.semantic_caption_complete) {
      showToast("请先完成当前包的全部语义描述。", "warning", "暂不能重建");
      return false;
    }
    if (["running", "paused"].includes(String(status.task_status || ""))) {
      showToast("当前语义任务尚未结束。", "warning", "暂不能重建");
      return false;
    }

    setButtonBusy(rebuildPackVectorsBtn, "正在重建…");
    try {
      showToast("正在按当前向量模型建立索引…", "info", "开始重建");
      const result = await apiPost("semantic/rebuild-index", {
        pack_id: normalizedPackId,
        force: true,
      });
      await refreshManagePackSummaries();
      showToast(result?.message || "向量索引已建立。", "success", "重建完成");
      return true;
    } catch (error) {
      showToast(
        error?.message || String(error),
        "error",
        "向量重建失败",
        5000,
      );
      return false;
    } finally {
      restoreButton(rebuildPackVectorsBtn);
      await refreshManagePackVectorStatus(normalizedPackId);
    }
  }

  async function confirmAndRebuildVector(
    packId,
    status,
    { importedShare = false, manual = false } = {},
  ) {
    const normalizedPackId = String(packId || "").trim();
    const pack = managePacksById.get(normalizedPackId);
    const packName = String(pack?.name || normalizedPackId);
    const modelLabel = [
      String(status?.embedding_model || "").trim(),
      Number(status?.embedding_configured_dimension || 0)
        ? `${Number(status.embedding_configured_dimension)} 维`
        : "",
    ]
      .filter(Boolean)
      .join(" · ");
    const alreadyReady = Boolean(status?.index_ready);
    const confirmed = await showConfirm({
      title: importedShare
        ? "分享包需要重建向量"
        : alreadyReady && manual
          ? "重新建立当前包向量？"
          : "当前表情包需要重建向量",
      description:
        `「${packName}」${
          importedShare
            ? "来自无向量分享版，需要在本机补建向量。"
            : alreadyReady && manual
              ? "已有可用向量，重建后会替换当前本机索引。"
              : "尚未按当前向量模型建立可用的本机索引。"
        }` +
        `本次只会调用向量模型${modelLabel ? `（${modelLabel}）` : ""}，` +
        "不会重新调用视觉模型，也不会覆盖现有语义描述。是否继续？",
      confirmLabel: "立即重建向量",
    });
    if (!confirmed) {
      if (!manual) {
        showToast(
          "语义描述已保留，可稍后点击首页的重建按钮。",
          "warning",
          "稍后重建",
        );
      }
      return false;
    }
    return performVectorRebuild(normalizedPackId, status);
  }

  async function maybeOfferVectorRebuild(packId, guidance = null) {
    const normalizedPackId = String(packId || "").trim();
    if (!normalizedPackId || !packSupportsVectorStatus(normalizedPackId)) {
      return false;
    }
    const status =
      latestManagePackVectorStatusId === normalizedPackId
        ? latestManagePackVectorStatus
        : await refreshManagePackVectorStatus(normalizedPackId);
    if (!status) {
      return false;
    }
    const rebuildRequired = Boolean(
      status.semantic_enabled &&
        status.embedding_provider_ready &&
        status.semantic_caption_complete &&
        status.dimension_rebuild_required &&
        !["running", "paused"].includes(String(status.task_status || "")),
    );
    if (!rebuildRequired) {
      return false;
    }
    return confirmAndRebuildVector(normalizedPackId, status, {
      importedShare: String(guidance?.export_mode || "") === "share",
    });
  }

  function resetPackImportPreview({ keepResult = false } = {}) {
    pendingPackImportToken = "";
    if (packImportFile) packImportFile.value = "";
    if (packImportFileLabel) packImportFileLabel.textContent = "选择或拖入 zip 压缩包";
    packImportDropzone?.classList.remove("hidden");
    packImportPreview?.classList.add("hidden");
    packImportWarning?.classList.add("hidden");
    if (packImportSetDefault) packImportSetDefault.checked = false;
    if (packImportOverwrite) packImportOverwrite.checked = false;
    if (packImportOverwriteManual) packImportOverwriteManual.checked = false;
    if (!keepResult) setPackTransferResult(packImportResult, "", "");
  }

  function renderPackImportInspection(data) {
    const formatLabels = {
      v2: data?.export_mode === "backup" ? "新版带向量备份" : "新版分享包",
      v1: "兼容版资源包",
      legacy: "旧版无语义包 · 将自动转换",
    };
    if (packImportPreviewName) {
      packImportPreviewName.textContent = `${data?.name || data?.pack_id || "待导入表情包"} (${data?.pack_id || "未知 ID"})`;
    }
    if (packImportPreviewFormat) {
      packImportPreviewFormat.textContent =
        formatLabels[data?.detected_format] || "已识别的表情包";
    }
    if (packImportImageCount) {
      packImportImageCount.textContent = Number(data?.image_count || 0);
    }
    if (packImportCategoryCount) {
      packImportCategoryCount.textContent = Number(data?.category_count || 0);
    }
    if (packImportSemanticCount) {
      packImportSemanticCount.textContent = data?.semantic_metadata
        ? `${Number(data?.semantic_done || 0)} 条`
        : "无";
    }
    if (packImportVectorState) {
      packImportVectorState.textContent = data?.vectors_present
        ? "包含，将校验"
        : "不包含";
    }
    const warnings = Array.isArray(data?.warnings) ? data.warnings : [];
    if (packImportWarning) {
      packImportWarning.textContent = warnings.join(" ");
      packImportWarning.classList.toggle("hidden", warnings.length === 0);
    }
    packImportDropzone?.classList.add("hidden");
    packImportPreview?.classList.remove("hidden");
  }

  async function stagePackImport(file) {
    if (!file) {
      return;
    }
    if (!String(file.name || "").toLowerCase().endsWith(".zip")) {
      showToast("请选择 zip 格式的表情包。", "warning", "格式不支持");
      return;
    }
    pendingPackImportToken = "";
    if (packImportFileLabel) packImportFileLabel.textContent = `正在检查 ${file.name}…`;
    packImportDropzone?.classList.add("checking");
    setPackTransferResult(packImportResult, "正在检查压缩包结构和兼容性…", "");
    try {
      const data = await window.AstrBotPluginPage.upload(
        "packs/import/stage",
        file,
      );
      pendingPackImportToken = String(data?.import_token || "").trim();
      if (!pendingPackImportToken) {
        throw new Error("服务器没有返回导入凭证");
      }
      renderPackImportInspection(data);
      setPackTransferResult(packImportResult, "检查完成，请确认导入选项。", "success");
    } catch (error) {
      resetPackImportPreview({ keepResult: true });
      setPackTransferResult(
        packImportResult,
        error?.message || String(error),
        "error",
      );
      showToast(error?.message || String(error), "error", "压缩包检查失败");
    } finally {
      packImportDropzone?.classList.remove("checking");
    }
  }

  async function confirmPackImport() {
    if (!pendingPackImportToken) {
      showToast("请先选择并检查压缩包。", "warning", "无法导入");
      return;
    }
    if (packImportOverwrite?.checked) {
      const confirmed = await showConfirm({
        title: "确认覆盖同名表情包？",
        description: packImportOverwriteManual?.checked
          ? "原表情包、向量和本机人工语义都会被替换。建议先导出自用备份。"
          : "原表情包及其向量会被替换，但本机人工描述、标签和图片文字会保留。",
        confirmLabel: "确认覆盖并导入",
        confirmClassName: "danger",
      });
      if (!confirmed) {
        return;
      }
    }

    setButtonBusy(packImportConfirmBtn, "正在导入…");
    setPackTransferResult(packImportResult, "正在安装表情包，请不要关闭页面。", "");
    try {
      const data = await apiPost("packs/import/apply", {
        import_token: pendingPackImportToken,
        overwrite: Boolean(packImportOverwrite?.checked),
        overwrite_manual_semantics: Boolean(
          packImportOverwrite?.checked && packImportOverwriteManual?.checked,
        ),
        set_as_default: Boolean(packImportSetDefault?.checked),
      });
      const importedPackId = String(data?.pack_id || "").trim();
      const vectorHint = data?.vectors_restored
        ? "，向量已恢复"
        : data?.vector_warning
          ? `；${data.vector_warning}`
          : "";
      resetPackImportPreview({ keepResult: true });
      setPackTransferResult(
        packImportResult,
        `已导入 ${data?.name || importedPackId}${vectorHint}`,
        "success",
      );
      await loadManagePackSwitcher(importedPackId);
      await refreshUi({ emojis: true, syncStatus: true });
      await refreshPackExportCapability(importedPackId);
      showToast(`表情包 ${importedPackId} 已导入。`, "success", "导入成功");
      await maybeOfferVectorRebuild(importedPackId, data);
    } catch (error) {
      setPackTransferResult(
        packImportResult,
        error?.message || String(error),
        "error",
      );
      showToast(error?.message || String(error), "error", "导入失败");
    } finally {
      restoreButton(packImportConfirmBtn);
    }
  }

  function updateManagePackSemanticAppearance(packId) {
    const normalizedPackId = String(packId || "").trim();
    const pack = managePacksById.get(normalizedPackId);
    const content = document.getElementById("content");
    const knownStatuses = ["none", "partial", "complete"];
    const status = knownStatuses.includes(String(pack?.semantic_status || ""))
      ? String(pack.semantic_status)
      : "none";

    knownStatuses.forEach((item) => {
      packSemanticStatus?.classList.toggle(`semantic-${item}`, item === status);
      content?.classList.toggle(`pack-semantic-${item}`, item === status);
    });

    if (!pack || !packSemanticStatusText) {
      applySemanticReviewData({ available: false });
      if (packSemanticStatusText) {
        packSemanticStatusText.textContent = "未语义化";
      }
      return;
    }

    const imageCount = Number(pack.image_count || 0);
    const completedCount = Number(pack.semantic_caption_done || 0);
    const semanticTotal = Number(pack.semantic_caption_total || 0);
    const failedCount = Number(pack.semantic_caption_failed || 0);
    if (status === "complete") {
      packSemanticStatusText.textContent = `语义已完成 · ${imageCount} 张`;
      packSemanticStatus.title =
        semanticTotal && semanticTotal !== imageCount
          ? `${imageCount} 张图片中有重复内容，共复用 ${semanticTotal} 条语义描述。`
          : `当前图包的 ${imageCount} 张图片均已有语义描述。`;
      return;
    }
    if (status === "partial") {
      const failureHint = failedCount > 0 ? `，${failedCount} 条失败` : "";
      packSemanticStatusText.textContent = pack.semantic_files_changed
        ? `语义待更新 · 已有 ${completedCount} 条`
        : semanticTotal
          ? `部分语义 · ${completedCount}/${semanticTotal}${failureHint}`
          : "部分语义 · 尚未完成";
      packSemanticStatus.title = pack.semantic_files_changed
        ? "图包新增了图片，或原图片内容已被替换，需要继续语义化。"
        : "当前图包仍有图片的语义描述未完成。";
      return;
    }
    packSemanticStatusText.textContent =
      imageCount > 0 ? "未语义化" : "空图包 · 暂无语义";
    packSemanticStatus.title =
      imageCount > 0
        ? "当前图包还没有可用的图片语义描述。"
        : "空图包无需语义化。";
    applySemanticReviewData({ available: false });
  }

  async function refreshManagePackSummaries() {
    try {
      const response = await apiGet("packs");
      const packs = Array.isArray(response?.packs) ? response.packs : [];
      managePacksById = new Map(
        packs.map((pack) => [String(pack?.id || "").trim(), pack]),
      );
      Array.from(managePackSelect?.options || []).forEach((option) => {
        const pack = managePacksById.get(String(option.value || "").trim());
        if (pack) {
          option.textContent = formatPackOptionLabel(pack);
        }
      });
      updateManagePackSemanticAppearance(activeManagePackId);
      await Promise.all([
        refreshPackExportCapability(activeManagePackId),
        refreshManagePackVectorStatus(activeManagePackId),
      ]);
      return packs;
    } catch (error) {
      console.warn("刷新图包语义状态失败:", error);
      return [];
    }
  }

  function syncManagedPackQuery(managedPackId) {
    const nextUrl = new URL(window.location.href);
    const normalized = String(managedPackId || "").trim();
    if (normalized) {
      nextUrl.searchParams.set("managed_pack_id", normalized);
    } else {
      nextUrl.searchParams.delete("managed_pack_id");
    }
    window.history.replaceState(null, "", nextUrl.toString());
  }

  function buildCatalogPageUrl() {
    return withCurrentAuthParams("../catalog/index.html", {
      view: "catalog",
      asset_token: navAuthToken || null,
    }).toString();
  }

  async function openCatalogPage() {
    await ensureNavAuthToken();
    window.location.href = buildCatalogPageUrl();
  }

  function isSingleEmptyPack(packs) {
    if (!Array.isArray(packs) || packs.length !== 1) {
      return false;
    }
    const onlyPack = packs[0] || {};
    return Number(onlyPack?.image_count || 0) === 0;
  }

  async function maybeShowFirstUseCatalogGuide(packs) {
    if (firstUseCatalogGuideShown || !isSingleEmptyPack(packs)) {
      return;
    }

    firstUseCatalogGuideShown = true;
    const confirmed = await showConfirm({
      title: "第一次使用？",
      description: "可以前往资源广场下载官方表情包哦。",
      confirmLabel: "前往广场",
    });
    if (!confirmed) {
      return;
    }
    await openCatalogPage();
  }

  async function loadManagePackSwitcher(preferredPackId = "") {
    if (!managePackSelect) {
      return [];
    }
    try {
      const response = await apiGet("packs");
      const packs = Array.isArray(response?.packs) ? response.packs : [];
      managePacksById = new Map(
        packs.map((pack) => [String(pack?.id || "").trim(), pack]),
      );
      managePackSelect.innerHTML = "";

      if (!packs.length) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "暂无可用表情包";
        managePackSelect.appendChild(option);
        managePackSelect.disabled = true;
        activeManagePackId = "";
        updateManagePackSemanticAppearance("");
        hideManagePackVectorStatus();
        await refreshPackExportCapability("");
        if (switchManagePackBtn) {
          switchManagePackBtn.disabled = true;
        }
        if (deleteManagePackBtn) {
          deleteManagePackBtn.disabled = true;
        }
        return packs;
      }

      let selectedPackId = "";
      defaultManagePackId = "";
      packs.forEach((pack) => {
        const option = document.createElement("option");
        option.value = String(pack.id || "").trim();
        option.textContent = formatPackOptionLabel(pack);
        if (!selectedPackId) {
          selectedPackId = option.value;
        }
        if (!defaultManagePackId && pack?.is_default) {
          defaultManagePackId = option.value;
        }
        managePackSelect.appendChild(option);
      });

      if (!defaultManagePackId) {
        defaultManagePackId = selectedPackId;
      }

      managePackSelect.disabled = false;
      if (switchManagePackBtn) {
        switchManagePackBtn.disabled = false;
      }
      if (deleteManagePackBtn) {
        deleteManagePackBtn.disabled = false;
      }
      const preferred = String(preferredPackId || "").trim();
      const canUsePreferred = packs.some(
        (item) => String(item?.id || "").trim() === preferred,
      );
      const canUseUrlPack = packs.some(
        (item) => String(item?.id || "").trim() === managedPackIdFromUrl,
      );
      if (canUsePreferred) {
        selectedPackId = preferred;
      } else if (canUseUrlPack) {
        selectedPackId = managedPackIdFromUrl;
      }
      managePackSelect.value = selectedPackId;
      activeManagePackId = selectedPackId;
      updateManagePackSemanticAppearance(selectedPackId);
      syncManagedPackQuery(selectedPackId);
      await Promise.all([
        refreshPackExportCapability(selectedPackId),
        refreshManagePackVectorStatus(selectedPackId),
      ]);

      await maybeShowFirstUseCatalogGuide(packs);
      return packs;
    } catch (error) {
      showToast(error?.message || String(error), "error", "加载表情包失败");
      return [];
    }
  }

  async function switchManagePack() {
    if (!managePackSelect) {
      return;
    }
    const targetPackId = String(managePackSelect.value || "").trim();
    if (!targetPackId) {
      showToast("请先选择表情包。", "warning", "切换失败");
      return;
    }

    setButtonBusy(switchManagePackBtn, "切换中...");
    closeImagePreview();
    const previousActivePackId = activeManagePackId;
    activeManagePackId = targetPackId;
    applySemanticReviewData({ available: false });
    updateManagePackSemanticAppearance(targetPackId);
    try {
      syncManagedPackQuery(targetPackId);
      await refreshUi({ emojis: true });
      await refreshPackExportCapability(targetPackId);
      showToast(`已切换管理视图到 ${targetPackId}。`, "success", "切换成功");
      await maybeOfferVectorRebuild(targetPackId);
    } catch (error) {
      activeManagePackId = previousActivePackId;
      updateManagePackSemanticAppearance(previousActivePackId);
      syncManagedPackQuery(previousActivePackId);
      await Promise.all([
        refreshPackExportCapability(previousActivePackId),
        refreshManagePackVectorStatus(previousActivePackId),
      ]);
      showToast(error?.message || String(error), "error", "切换失败");
    } finally {
      restoreButton(switchManagePackBtn);
    }
  }

  async function deleteCurrentManagePack() {
    if (!managePackSelect) {
      return;
    }

    const targetPackId = String(
      activeManagePackId || managePackSelect.value || "",
    ).trim();
    if (!targetPackId) {
      showToast("请先选择要删除的表情包。", "warning", "删除失败");
      return;
    }

    const confirmed = await showDangerConfirm({
      title: `删除表情包组「${targetPackId}」`,
      description:
        "该操作会删除整个表情包组（包括分类与图片）。删除后会自动切换到其他表情包；如果删空会自动创建一个空表情包。",
      actionLabel: "确认删除当前表情包组",
      countdown: 5,
    });
    if (!confirmed) {
      return;
    }

    setButtonBusy(deleteManagePackBtn, "删除中...");
    try {
      const data = await apiPost("packs/uninstall", { pack_id: targetPackId });
      const switchedPackId = String(data?.switched_default_to || "").trim();
      await loadManagePackSwitcher(switchedPackId);
      await refreshUi({ emojis: true, syncStatus: true });
      const switchedHint = switchedPackId ? `，已切换到 ${switchedPackId}` : "";
      const createdHint = data?.auto_created_empty_pack
        ? "（已自动创建空表情包）"
        : "";
      showToast(
        `已删除 ${targetPackId}${switchedHint}${createdHint}`,
        "success",
        "删除成功",
      );
    } catch (error) {
      showToast(error?.message || String(error), "error", "删除失败", 4500);
    } finally {
      restoreButton(deleteManagePackBtn);
    }
  }

  async function installOfficialFirstPackFromHint(triggerBtn) {
    setButtonBusy(triggerBtn, "安装中...");
    try {
      const data = await apiPost("community/install_official_first", {
        overwrite: false,
        set_as_default: true,
      });
      const installedPackId = String(data?.pack_id || "").trim();
      await loadManagePackSwitcher(installedPackId);
      await refreshUi({ emojis: true, syncStatus: true });
      const installedName = String(
        data?.selected_pack_name ||
          data?.name ||
          installedPackId ||
          "官方表情包",
      );
      showToast(
        `已安装 ${installedName}，并切换为默认表情包。`,
        "success",
        "安装成功",
      );
    } catch (error) {
      showToast(error?.message || String(error), "error", "安装失败");
    } finally {
      restoreButton(triggerBtn);
    }
  }

  function semanticReviewKey(category, filename) {
    return `${String(category || "")}\u0000${String(filename || "")}`;
  }

  function semanticReviewLabel(status) {
    return (
      {
        auto_match: "自动符合",
        needs_review: "建议人工复核",
        manual_confirmed: "已人工确认",
        manual_rejected: "人工确认分类不符",
        unchecked: "尚未检查",
      }[String(status || "")] || "尚未检查"
    );
  }

  function updateSemanticReviewToolbar() {
    if (!semanticReviewStats) return;
    semanticReviewStats.querySelectorAll("button[data-review-filter]").forEach((button) => {
      const filter = String(button.dataset.reviewFilter || "all");
      const count =
        filter === "all"
          ? Number(semanticReviewStatistics.total || 0)
          : Number(semanticReviewStatistics[filter] || 0);
      const value = button.querySelector("span");
      if (value) value.textContent = String(count);
      button.classList.toggle("active", filter === activeSemanticReviewFilter);
    });
  }

  function applySemanticReviewData(payload) {
    semanticReviewAvailable = Boolean(payload?.available);
    const items =
      semanticReviewAvailable && Array.isArray(payload?.items)
        ? payload.items
        : [];
    semanticReviewByPath = new Map(
      items.map((item) => [
        semanticReviewKey(item?.category, item?.filename),
        item,
      ]),
    );
    semanticReviewStatistics = semanticReviewAvailable
      ? payload?.statistics || {}
      : {};
    if (!semanticReviewAvailable) {
      activeSemanticReviewFilter = "all";
    }
    semanticReviewToolbar?.classList.toggle("hidden", !semanticReviewAvailable);
    updateSemanticReviewToolbar();
  }

  // 获取表情包数据、描述和分类审核状态
  async function fetchEmojis() {
    try {
      const [emojiResponse, tagDescriptions, reviewResult] = await Promise.all([
        apiGet("emoji"),
        apiGet("emotions"),
        apiGet("semantic/reviews").catch((error) => {
          console.warn("读取分类审核状态失败:", error);
          return { items: [], statistics: {} };
        }),
      ]);
      clearDragMode();
      closeBatchContextMenu();
      latestEmojiData = emojiResponse;
      latestTagDescriptions = tagDescriptions;
      applySemanticReviewData(reviewResult);
      pruneSelectionState();
      displayCategories(emojiResponse, tagDescriptions);
      updateSidebar(emojiResponse, tagDescriptions);
      updateSelectionUI();
    } catch (error) {
      console.error("加载表情包数据失败", error);
    }
  }

  function createButton({
    className = "",
    text = "",
    disabled = false,
    onClick = null,
  }) {
    const button = document.createElement("button");
    button.type = "button";
    if (className) {
      button.className = className;
    }
    button.textContent = text;
    button.disabled = disabled;
    if (onClick) {
      button.addEventListener("click", onClick);
    }
    return button;
  }

  function createIconButton({
    className = "",
    iconClass = "",
    title = "",
    ariaLabel = "",
    onClick = null,
  }) {
    const button = document.createElement("button");
    button.type = "button";
    if (className) {
      button.className = className;
    }
    if (title) {
      button.title = title;
    }
    if (ariaLabel) {
      button.setAttribute("aria-label", ariaLabel);
    }

    if (iconClass) {
      const icon = document.createElement("i");
      icon.className = iconClass;
      button.appendChild(icon);
    }

    if (onClick) {
      button.addEventListener("click", onClick);
    }

    return button;
  }

  function setButtonBusy(button, busyText) {
    if (!button) return;
    if (!button.dataset.originalHtml) {
      button.dataset.originalHtml = button.innerHTML;
    }
    button.disabled = true;
    button.textContent = busyText;
  }

  function restoreButton(button) {
    if (!button) return;
    button.disabled = false;
    if (button.dataset.originalHtml) {
      button.innerHTML = button.dataset.originalHtml;
    }
  }

  function getImageRequestParams(category, emoji, size = "preview") {
    return {
      category,
      filename: emoji,
      size,
    };
  }

  function setEmojiPreviewLoading(emojiItem) {
    emojiItem.classList.remove("emoji-load-error", "emoji-loaded");
    emojiItem.classList.add("emoji-loading");
    emojiItem.setAttribute("aria-label", "正在加载表情包预览");
  }

  function setEmojiPreviewLoaded(emojiItem, dataUrl) {
    emojiItem.style.backgroundImage = `url("${dataUrl}")`;
    emojiItem.dataset.previewDataUrl = dataUrl;
    emojiItem.classList.remove("emoji-loading", "emoji-load-error");
    emojiItem.classList.add("emoji-loaded");
    emojiItem.setAttribute(
      "aria-label",
      `预览表情包 ${emojiItem.dataset.emoji || ""}`,
    );
  }

  function setEmojiPreviewError(emojiItem) {
    emojiItem.style.backgroundImage = "";
    emojiItem.classList.remove("emoji-loading", "emoji-loaded");
    emojiItem.classList.add("emoji-load-error");
    emojiItem.setAttribute("aria-label", "预览加载失败，点击重试");
  }

  async function loadEmojiPreview(emojiItem, { force = false } = {}) {
    if (
      !emojiItem ||
      (!force &&
        (emojiItem.dataset.previewDataUrl ||
          emojiItem.dataset.loading === "true"))
    ) {
      return;
    }

    const { category, emoji } = emojiItem.dataset;
    if (!category || !emoji) {
      setEmojiPreviewError(emojiItem);
      return;
    }

    emojiItem.dataset.loading = "true";
    setEmojiPreviewLoading(emojiItem);
    try {
      const data = await apiGet(
        "meme_image_data",
        getImageRequestParams(category, emoji, "preview"),
      );
      if (!data?.data_url) {
        throw new Error("图片接口未返回预览数据");
      }
      setEmojiPreviewLoaded(emojiItem, data.data_url);
    } catch (error) {
      console.error("加载表情包预览失败:", error);
      setEmojiPreviewError(emojiItem);
    } finally {
      emojiItem.dataset.loading = "false";
    }
  }

  function retryEmojiPreview(emojiItem) {
    if (!emojiItem) {
      return;
    }
    delete emojiItem.dataset.previewDataUrl;
    void loadEmojiPreview(emojiItem, { force: true });
  }

  async function loadPreviewImage(category, emoji, size = "preview") {
    const data = await apiGet(
      "meme_image_data",
      getImageRequestParams(category, emoji, size),
    );
    if (!data?.data_url) {
      throw new Error("图片接口未返回预览数据");
    }
    return data.data_url;
  }

  async function loadImageSemantic(category, emoji) {
    const data = await apiGet("meme_image_semantic", {
      category,
      filename: emoji,
    });
    return data?.semantic || { status: "none" };
  }

  function renderImageSemantic(semantic, { loading = false, error = "" } = {}) {
    if (!imagePreviewSemantic) {
      return;
    }
    const statuses = ["loading", "complete", "partial", "failed", "none"];
    const rawStatus = loading
      ? "loading"
      : error
        ? "failed"
        : String(semantic?.status || "none");
    const status = rawStatus === "pending" ? "partial" : rawStatus;
    const normalizedStatus = statuses.includes(status) ? status : "none";
    if (imagePreviewState && !loading && !error) {
      imagePreviewState.semantic = semantic || { status: "none" };
    }
    statuses.forEach((item) => {
      imagePreviewSemantic.classList.toggle(
        `semantic-${item}`,
        item === normalizedStatus,
      );
    });

    if (imagePreviewSemanticFilename) {
      imagePreviewSemanticFilename.textContent = imagePreviewState?.emoji || "";
    }
    const reviewStatus = String(
      semantic?.category_review_status || "unchecked",
    );
    const canEdit = !loading && !error && Boolean(semantic?.can_edit_semantic);
    const needsHumanReview = ["needs_review", "manual_rejected"].includes(
      reviewStatus,
    );
    const showCategoryReview =
      loading || (!error && normalizedStatus !== "none");
    imagePreviewCategoryReview?.classList.toggle(
      "hidden",
      !showCategoryReview,
    );
    if (imagePreviewCategoryTag) {
      imagePreviewCategoryTag.textContent = loading
        ? "读取固定标签…"
        : String(
            semantic?.category_tag ||
              `category:${imagePreviewState?.category || ""}`,
          );
    }
    if (imagePreviewCategoryReviewState) {
      imagePreviewCategoryReviewState.textContent = loading
        ? "读取审核状态…"
        : semanticReviewLabel(reviewStatus);
    }
    const reviewReason = String(semantic?.category_review_reason || "").trim();
    if (imagePreviewCategoryReviewReason) {
      imagePreviewCategoryReviewReason.textContent = reviewReason;
      imagePreviewCategoryReviewReason.classList.toggle(
        "hidden",
        loading || !reviewReason,
      );
    }
    const reclassificationStatus = String(
      semantic?.reclassification_status || "",
    );
    if (imagePreviewReclassification) {
      const fromCategory = String(
        semantic?.reclassified_from_category || "",
      );
      const toCategory = String(semantic?.reclassified_to_category || "");
      const reclassificationReason = String(
        semantic?.reclassification_reason || "",
      ).trim();
      imagePreviewReclassification.textContent = reclassificationStatus
        ? `自动重分类：${fromCategory || "原分类"} → ${toCategory || "当前分类"}${
            reclassificationReason ? `；${reclassificationReason}` : ""
          }`
        : "";
      imagePreviewReclassification.classList.toggle(
        "hidden",
        loading || !reclassificationStatus,
      );
    }
    imagePreviewCategoryConfirmBtn?.classList.toggle(
      "hidden",
      !canEdit || !semantic?.can_confirm_category,
    );
    imagePreviewReviewEditBtn?.classList.toggle(
      "hidden",
      !canEdit || !needsHumanReview,
    );
    const showReviewActions = Boolean(
      canEdit &&
        (semantic?.can_confirm_category || needsHumanReview),
    );
    imagePreviewReviewActions?.classList.toggle("hidden", !showReviewActions);
    const manualModified = Boolean(semantic?.manual_modified);
    if (imagePreviewSourceState) {
      imagePreviewSourceState.textContent = manualModified
        ? "人工修改"
        : "自动生成";
      imagePreviewSourceState.classList.toggle("status-manual", manualModified);
    }
    const embeddingStatus = String(semantic?.embedding_status || "pending");
    if (imagePreviewVectorState) {
      const vectorLabels = {
        done: "向量更新完成",
        failed: "向量更新失败",
        running: "向量更新中",
        pending: manualModified
          ? "语义已保存，向量待更新"
          : "向量等待更新",
        cleared: "向量等待更新",
      };
      imagePreviewVectorState.textContent = loading
        ? "读取向量状态…"
        : vectorLabels[embeddingStatus] || "向量等待更新";
      imagePreviewVectorState.classList.remove(
        "status-done",
        "status-failed",
        "status-pending",
      );
      imagePreviewVectorState.classList.add(
        embeddingStatus === "done"
          ? "status-done"
          : embeddingStatus === "failed"
            ? "status-failed"
            : "status-pending",
      );
    }
    const duplicatePaths = Array.isArray(semantic?.same_content_paths)
      ? semantic.same_content_paths
      : [];
    if (imagePreviewDuplicateNotice) {
      imagePreviewDuplicateNotice.textContent = duplicatePaths.length
        ? `检测到另外 ${duplicatePaths.length} 张内容相同的图片。本次只修改当前路径，不会改动：${duplicatePaths.join("、")}`
        : "";
      imagePreviewDuplicateNotice.classList.toggle(
        "hidden",
        loading || duplicatePaths.length === 0,
      );
    }
    const showBottomActions = Boolean(
      canEdit && (!needsHumanReview || semantic?.can_restore_auto),
    );
    imagePreviewSemanticActions?.classList.toggle(
      "hidden",
      !showBottomActions,
    );
    imagePreviewEditBtn?.classList.toggle(
      "hidden",
      !canEdit || needsHumanReview,
    );
    imagePreviewRestoreAutoBtn?.classList.toggle(
      "hidden",
      !canEdit || !semantic?.can_restore_auto,
    );
    if (loading) {
      imagePreviewSemanticState.textContent = "读取中";
      imagePreviewSemanticCaption.textContent = "正在读取这张图片的语义信息…";
    } else if (error) {
      imagePreviewSemanticState.textContent = "读取失败";
      imagePreviewSemanticCaption.textContent = error;
    } else if (normalizedStatus === "complete") {
      imagePreviewSemanticState.textContent = "已语义化";
      imagePreviewSemanticCaption.textContent =
        String(semantic?.caption || "").trim() || "已有语义记录。";
    } else if (normalizedStatus === "failed") {
      imagePreviewSemanticState.textContent = "语义化失败";
      imagePreviewSemanticCaption.textContent =
        String(semantic?.error || "").trim() ||
        "这张图片上次语义化失败，可以前往语义化页面重试。";
    } else if (normalizedStatus === "partial") {
      imagePreviewSemanticState.textContent = "等待完成";
      imagePreviewSemanticCaption.textContent =
        "这张图片已进入语义化流程，但描述尚未完成。";
    } else {
      imagePreviewSemanticState.textContent = "未语义化";
      imagePreviewSemanticCaption.textContent =
        "这张图片还没有语义描述，可以前往语义化页面继续处理。";
    }

    const tags = Array.isArray(semantic?.tags) ? semantic.tags : [];
    if (imagePreviewSemanticTags) {
      imagePreviewSemanticTags.replaceChildren();
      tags.forEach((tag) => {
        const chip = document.createElement("span");
        chip.textContent = String(tag);
        imagePreviewSemanticTags.appendChild(chip);
      });
    }
    imagePreviewSemanticTagsWrap?.classList.toggle(
      "hidden",
      loading || tags.length === 0,
    );

    const visibleText = String(semantic?.visible_text || "").trim();
    if (imagePreviewVisibleText) {
      imagePreviewVisibleText.textContent = visibleText;
    }
    imagePreviewVisibleTextWrap?.classList.toggle(
      "hidden",
      loading || !visibleText,
    );

    if (imagePreviewSemanticIndex) {
      imagePreviewSemanticIndex.textContent =
        normalizedStatus === "complete"
          ? embeddingStatus === "done"
            ? "语义向量已建立，可用于搜索。"
            : embeddingStatus === "failed"
              ? `语义已保存，但向量更新失败${semantic?.embedding_error ? `：${semantic.embedding_error}` : "。"}`
              : "语义已保存，向量等待更新。"
          : "";
    }
  }

  function setImageSemanticEditing(editing) {
    if (!imagePreviewSemantic || !imagePreviewEditForm) return;
    imagePreviewSemantic.classList.toggle("editing", editing);
    imagePreviewEditForm.classList.toggle("hidden", !editing);
    if (!editing) return;
    const semantic = imagePreviewState?.semantic || {};
    if (imagePreviewCaptionInput) {
      imagePreviewCaptionInput.value = String(semantic.caption || "");
    }
    if (imagePreviewTagsInput) {
      const tags = Array.isArray(semantic.editable_tags)
        ? semantic.editable_tags
        : [];
      imagePreviewTagsInput.value = tags.join("，");
    }
    if (imagePreviewVisibleTextInput) {
      imagePreviewVisibleTextInput.value = String(semantic.visible_text || "");
    }
    if (imagePreviewCategoryDecision) {
      imagePreviewCategoryDecision.value =
        semantic.category_review_status === "manual_confirmed"
          ? "match"
          : semantic.category_review_status === "manual_rejected"
            ? "mismatch"
            : semantic.status === "none"
              ? "match"
              : "keep";
    }
    if (imagePreviewFixedTags) {
      imagePreviewFixedTags.replaceChildren();
      const fixedTags = Array.isArray(semantic.fixed_category_tags)
        ? semantic.fixed_category_tags
        : [];
      fixedTags.forEach((tag) => {
        const chip = document.createElement("span");
        chip.textContent = String(tag);
        imagePreviewFixedTags.appendChild(chip);
      });
    }
    if (imagePreviewReviewInstruction) {
      imagePreviewReviewInstruction.value = "";
    }
    if (imagePreviewReviewRewriteStatus) {
      imagePreviewReviewRewriteStatus.textContent =
        "模型会重写语义并自动选择分类；只生成候选，不会直接保存或移动。";
      imagePreviewReviewRewriteStatus.classList.remove("has-proposal");
    }
    if (imagePreviewTargetCategory) {
      imagePreviewTargetCategory.replaceChildren();
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "保持当前分类";
      imagePreviewTargetCategory.appendChild(placeholder);
      const currentCategory = String(imagePreviewState?.category || "");
      const categories = Array.from(
        new Set([
          ...Object.keys(latestEmojiData || {}),
          ...Object.keys(latestTagDescriptions || {}),
        ]),
      )
        .filter((category) => category && category !== currentCategory)
        .sort((left, right) => left.localeCompare(right, "zh-CN"));
      categories.forEach((category) => {
        const option = document.createElement("option");
        option.value = category;
        option.textContent = category;
        imagePreviewTargetCategory.appendChild(option);
      });
      updateImageSemanticMoveChoice();
    }
    const duplicateCount = Number(semantic.same_content_count || 0);
    if (imagePreviewEditScope) {
      imagePreviewEditScope.textContent = duplicateCount
        ? `只修改当前路径。检测到另外 ${duplicateCount} 张同内容图片，它们不会被一并修改。`
        : "只修改当前图片路径和当前分类，不会批量共享修改。";
    }
    window.setTimeout(() => imagePreviewCaptionInput?.focus(), 0);
  }

  function parseManualSemanticTags(value) {
    return Array.from(
      new Set(
        String(value || "")
          .split(/[，,\n]+/)
          .map((tag) => tag.trim())
          .filter(Boolean),
      ),
    );
  }

  function imageSemanticSnapshotPayload() {
    const state = imagePreviewState;
    const semantic = state?.semantic || {};
    if (
      !state ||
      String(activeManagePackId || "") !== String(state.packId || "")
    ) {
      throw new Error("当前图包已经切换，请重新打开图片后再编辑。");
    }
    if (!semantic.content_sha256 || !semantic.entry_id) {
      throw new Error("图片编辑快照无效，请重新打开图片后再操作。");
    }
    return {
      expected_pack_id: state.packId,
      category: state.category,
      filename: state.emoji,
      expected_content_sha256: String(semantic.content_sha256),
      expected_entry_id: String(semantic.entry_id),
    };
  }

  function imageSemanticEditPayload() {
    const caption = String(imagePreviewCaptionInput?.value || "").trim();
    if (!caption) {
      throw new Error("图片含义不能为空。");
    }
    return {
      ...imageSemanticSnapshotPayload(),
      caption,
      tags: parseManualSemanticTags(imagePreviewTagsInput?.value),
      visible_text: String(imagePreviewVisibleTextInput?.value || "").trim(),
      category_decision: String(imagePreviewCategoryDecision?.value || "keep"),
      target_category: String(imagePreviewTargetCategory?.value || "").trim(),
    };
  }

  function updateImageSemanticMoveChoice() {
    const targetCategory = String(
      imagePreviewTargetCategory?.value || "",
    ).trim();
    const saving = imagePreviewEditForm?.dataset.saving === "true";
    if (imagePreviewSaveBtn && !saving) {
      imagePreviewSaveBtn.disabled = Boolean(targetCategory);
      imagePreviewSaveBtn.title = targetCategory
        ? "选择新分类后，需要同时更新该图向量"
        : "只保存语义，向量等待后续更新";
    }
    if (imagePreviewSaveVectorBtn && !saving) {
      imagePreviewSaveVectorBtn.disabled = false;
      imagePreviewSaveVectorBtn.textContent = targetCategory
        ? "保存、移动并更新该图向量"
        : "保存并更新该图向量";
    }
  }

  function setImageSemanticSaving(
    saving,
    activeButton = null,
    busyLabel = "保存中...",
  ) {
    if (imagePreviewEditForm) {
      imagePreviewEditForm.dataset.saving = saving ? "true" : "false";
    }
    [
      imagePreviewEditCancelBtn,
      imagePreviewSaveBtn,
      imagePreviewSaveVectorBtn,
      imagePreviewEditBtn,
      imagePreviewReviewEditBtn,
      imagePreviewRestoreAutoBtn,
      imagePreviewCategoryConfirmBtn,
      imagePreviewReviewRewriteBtn,
    ].forEach((button) => {
      if (button) button.disabled = saving;
    });
    [
      imagePreviewCaptionInput,
      imagePreviewTagsInput,
      imagePreviewVisibleTextInput,
      imagePreviewCategoryDecision,
      imagePreviewReviewInstruction,
      imagePreviewTargetCategory,
    ].forEach((field) => {
      if (field) field.disabled = saving;
    });
    if (saving && activeButton) {
      setButtonBusy(activeButton, busyLabel);
    } else if (!saving && activeButton) {
      restoreButton(activeButton);
    }
    if (!saving) {
      updateImageSemanticMoveChoice();
    }
  }

  async function requestImageSemanticRevision() {
    const previewState = imagePreviewState;
    const reviewInstruction = String(
      imagePreviewReviewInstruction?.value || "",
    ).trim();
    if (!reviewInstruction) {
      showToast(
        "请先写明哪里判断错了，以及希望模型怎样修改。",
        "warning",
        "缺少复审意见",
      );
      imagePreviewReviewInstruction?.focus();
      return;
    }
    try {
      const payload = {
        ...imageSemanticSnapshotPayload(),
        review_instruction: reviewInstruction,
      };
      setImageSemanticSaving(
        true,
        imagePreviewReviewRewriteBtn,
        "视觉模型分析中...",
      );
      const result = await apiPost("semantic/propose_image_revision", payload);
      if (imagePreviewState !== previewState) return;
      const proposal = result?.proposal || {};
      if (imagePreviewCaptionInput) {
        imagePreviewCaptionInput.value = String(proposal.caption || "");
      }
      if (imagePreviewTagsInput) {
        imagePreviewTagsInput.value = Array.isArray(proposal.tags)
          ? proposal.tags.join("，")
          : "";
      }
      if (imagePreviewVisibleTextInput) {
        imagePreviewVisibleTextInput.value = String(
          proposal.visible_text || "",
        );
      }
      const categoryFit = String(proposal.category_fit || "uncertain");
      const classificationAction = String(
        proposal.classification_action || "manual_required",
      );
      if (imagePreviewCategoryDecision) {
        if (
          classificationAction === "keep_current" &&
          categoryFit === "match"
        ) {
          imagePreviewCategoryDecision.value = "match";
        } else if (
          ["return_original", "move_to_other"].includes(
            classificationAction,
          ) || categoryFit === "conflict"
        ) {
          imagePreviewCategoryDecision.value = "mismatch";
        } else {
          imagePreviewCategoryDecision.value = "keep";
        }
      }
      const currentCategory = String(
        proposal.current_category || previewState?.category || "",
      ).trim();
      const originalCategory = String(
        proposal.original_category || "",
      ).trim();
      const selectedCategory = String(
        proposal.selected_category || "",
      ).trim();
      let targetOptionAvailable = true;
      if (imagePreviewTargetCategory) {
        const selectableTarget =
          selectedCategory && selectedCategory !== currentCategory
            ? selectedCategory
            : "";
        const hasTargetOption = Array.from(
          imagePreviewTargetCategory.options,
        ).some((option) => option.value === selectableTarget);
        targetOptionAvailable = !selectableTarget || hasTargetOption;
        imagePreviewTargetCategory.value = hasTargetOption
          ? selectableTarget
          : "";
        updateImageSemanticMoveChoice();
      }
      if (imagePreviewReviewRewriteStatus) {
        const reviewReason = String(
          proposal.category_review_reason || "",
        ).trim();
        let categoryChoiceText = "分类建议：模型未能确定，请手动选择。";
        if (!targetOptionAvailable) {
          categoryChoiceText = `模型建议改到“${selectedCategory}”，但当前分类列表已变化，请刷新页面后重新检查。`;
        } else if (classificationAction === "keep_current") {
          categoryChoiceText = `分类建议：保持当前分类“${currentCategory}”。`;
        } else if (classificationAction === "return_original") {
          categoryChoiceText = `分类建议：回到原分类“${originalCategory || selectedCategory}”。`;
        } else if (classificationAction === "move_to_other") {
          categoryChoiceText = `分类建议：改到“${selectedCategory}”。`;
        }
        imagePreviewReviewRewriteStatus.textContent = [
          "视觉模型候选已填入，尚未保存。",
          categoryChoiceText,
          reviewReason ? `分类判断：${reviewReason}` : "",
        ]
          .filter(Boolean)
          .join(" ");
        imagePreviewReviewRewriteStatus.classList.add("has-proposal");
      }
      showToast(
        result?.message || "视觉模型候选已填入，请检查后再保存。",
        "success",
        "候选已生成",
        4800,
      );
    } catch (error) {
      showToast(error?.message || String(error), "error", "重写失败", 4800);
    } finally {
      setImageSemanticSaving(false, imagePreviewReviewRewriteBtn);
    }
  }

  async function saveCurrentImageSemantic({ updateVector = false } = {}) {
    const activeButton = updateVector
      ? imagePreviewSaveVectorBtn
      : imagePreviewSaveBtn;
    const previewState = imagePreviewState;
    try {
      const payload = imageSemanticEditPayload();
      if (payload.target_category && !updateVector) {
        throw new Error(
          "选择新分类后，请点击“保存、移动并更新该图向量”。",
        );
      }
      setImageSemanticSaving(
        true,
        activeButton,
        payload.target_category ? "保存、移动并更新中..." : "保存中...",
      );
      const endpoint = updateVector
        ? "semantic/save_image_and_vector"
        : "semantic/save_image";
      const result = await apiPost(endpoint, payload);
      if (imagePreviewState === previewState) {
        if (result?.moved) {
          previewState.category = String(
            result?.category || result?.target_category || previewState.category,
          );
          previewState.emoji = String(result?.filename || previewState.emoji);
          if (imagePreviewImg) {
            imagePreviewImg.alt = `表情包预览：${previewState.emoji}`;
          }
        }
        renderImageSemantic(result?.semantic || {});
        setImageSemanticEditing(false);
      }
      await fetchEmojis();
      const vectorStatus = String(result?.vector_update?.status || "pending");
      const toastType =
        updateVector && vectorStatus !== "done" ? "warning" : "success";
      showToast(
        result?.message || "人工语义已保存，向量等待更新。",
        toastType,
        result?.moved
          ? "保存、移动与向量更新"
          : updateVector
            ? "保存与向量更新"
            : "保存成功",
      );
    } catch (error) {
      showToast(error?.message || String(error), "error", "保存失败");
    } finally {
      setImageSemanticSaving(false, activeButton);
    }
  }

  async function restoreCurrentImageAutoSemantic() {
    const state = imagePreviewState;
    const semantic = state?.semantic || {};
    if (!state || !semantic.can_restore_auto) return;
    const confirmed = await showConfirm({
      title: "确认恢复自动生成",
      description:
        "这会放弃当前图片的人工描述、普通标签和可见文字。其他同内容图片不会受影响。确认继续吗？",
      confirmLabel: "放弃人工修改",
      confirmClassName: "danger",
    });
    if (!confirmed) return;
    try {
      if (String(activeManagePackId || "") !== String(state.packId || "")) {
        throw new Error("当前图包已经切换，请重新打开图片后再操作。");
      }
      setImageSemanticSaving(true, imagePreviewRestoreAutoBtn);
      const result = await apiPost("semantic/restore_image_auto", {
        expected_pack_id: state.packId,
        category: state.category,
        filename: state.emoji,
        expected_content_sha256: String(semantic.content_sha256 || ""),
        expected_entry_id: String(semantic.entry_id || ""),
      });
      if (imagePreviewState === state) {
        renderImageSemantic(result?.semantic || {});
        setImageSemanticEditing(false);
      }
      await fetchEmojis();
      showToast(result?.message || "已恢复自动生成状态。", "success");
    } catch (error) {
      showToast(error?.message || String(error), "error", "恢复失败");
    } finally {
      setImageSemanticSaving(false, imagePreviewRestoreAutoBtn);
    }
  }

  async function confirmCurrentImageCategory() {
    if (!imagePreviewState || !imagePreviewCategoryConfirmBtn) return;
    const previewState = imagePreviewState;
    setButtonBusy(imagePreviewCategoryConfirmBtn, "保存中...");
    try {
      const result = await apiPost("semantic/confirm_category", {
        expected_pack_id: previewState.packId,
        category: previewState.category,
        filename: previewState.emoji,
        expected_content_sha256: String(
          previewState.semantic?.content_sha256 || "",
        ),
        expected_entry_id: String(previewState.semantic?.entry_id || ""),
      });
      if (imagePreviewState === previewState) {
        renderImageSemantic(result?.semantic || {});
      }
      await fetchEmojis();
      showToast("已保存人工确认；图片或分类变化后会自动失效。", "success");
    } catch (error) {
      showToast(error?.message || String(error), "error", "确认失败");
    } finally {
      restoreButton(imagePreviewCategoryConfirmBtn);
    }
  }

  function setImagePreviewBusy(isBusy) {
    if (imagePreviewLoading) {
      imagePreviewLoading.classList.toggle("hidden", !isBusy);
    }
    if (imagePreviewOriginalBtn) {
      imagePreviewOriginalBtn.disabled = isBusy;
    }
  }

  function closeImagePreview() {
    setImageSemanticEditing(false);
    imagePreviewState = null;
    if (imagePreviewModalRoot) {
      imagePreviewModalRoot.classList.add("hidden");
      imagePreviewModalRoot.setAttribute("aria-hidden", "true");
    }
    if (imagePreviewImg) {
      imagePreviewImg.removeAttribute("src");
    }
    renderImageSemantic(null, { loading: true });
    setImagePreviewBusy(false);
    unlockPageScroll();
  }

  async function openImagePreview(category, emoji, previewDataUrl = "") {
    if (!imagePreviewModalRoot || !imagePreviewImg) {
      return;
    }

    const previewState = {
      category,
      emoji,
      packId: String(activeManagePackId || managePackSelect?.value || ""),
      semantic: null,
    };
    imagePreviewState = previewState;
    lockPageScroll();
    imagePreviewModalRoot.classList.remove("hidden");
    imagePreviewModalRoot.setAttribute("aria-hidden", "false");
    imagePreviewImg.alt = `表情包预览：${emoji}`;
    renderImageSemantic(null, { loading: true });
    if (previewDataUrl) {
      imagePreviewImg.src = previewDataUrl;
    } else {
      imagePreviewImg.removeAttribute("src");
    }

    setImagePreviewBusy(!previewDataUrl);
    const previewRequest = previewDataUrl
      ? Promise.resolve(previewDataUrl)
      : loadPreviewImage(category, emoji, "preview");
    const semanticRequest = loadImageSemantic(category, emoji);
    const [previewResult, semanticResult] = await Promise.allSettled([
      previewRequest,
      semanticRequest,
    ]);
    if (imagePreviewState !== previewState) {
      return;
    }
    if (previewResult.status === "rejected") {
      const error = previewResult.reason;
      console.error("打开大图预览失败:", error);
      closeImagePreview();
      showToast("图片预览加载失败，请稍后重试。", "error", "加载失败");
      return;
    }
    imagePreviewImg.src = previewResult.value;
    if (semanticResult.status === "fulfilled") {
      renderImageSemantic(semanticResult.value);
    } else {
      console.error("读取图片语义失败:", semanticResult.reason);
      renderImageSemantic(null, {
        error: "语义信息暂时读取失败，请稍后重新打开图片。",
      });
    }
    setImagePreviewBusy(false);

    imagePreviewCloseBtn?.focus();
  }

  async function showOriginalPreview() {
    if (!imagePreviewState || !imagePreviewImg) {
      return;
    }

    const previewState = imagePreviewState;
    setImagePreviewBusy(true);
    try {
      const originalDataUrl = await loadPreviewImage(
        previewState.category,
        previewState.emoji,
        "original",
      );
      if (imagePreviewState === previewState) {
        imagePreviewImg.src = originalDataUrl;
      }
    } catch (error) {
      console.error("加载原图失败:", error);
      showToast("原图加载失败，可能文件过大或已不存在。", "error", "加载失败");
    } finally {
      if (imagePreviewState === previewState) {
        setImagePreviewBusy(false);
      }
    }
  }

  function showToast(message, type = "info", title = "提示", duration = 3200) {
    if (!toastContainer) {
      return;
    }

    const toast = document.createElement("div");
    toast.className = `toast toast-${type}`;

    const content = document.createElement("div");
    content.className = "toast-content";

    const titleElement = document.createElement("p");
    titleElement.className = "toast-title";
    titleElement.textContent = title;

    const messageElement = document.createElement("p");
    messageElement.className = "toast-message";
    messageElement.textContent = message;

    content.appendChild(titleElement);
    content.appendChild(messageElement);
    toast.appendChild(content);
    toastContainer.appendChild(toast);

    window.setTimeout(() => {
      toast.remove();
    }, duration);
  }

  function closeConfirm(result) {
    if (confirmModalRoot) {
      const restoreTarget = confirmRestoreFocusElement;
      confirmRestoreFocusElement = null;
      if (
        restoreTarget instanceof HTMLElement &&
        restoreTarget.isConnected &&
        !restoreTarget.hasAttribute("disabled")
      ) {
        restoreTarget.focus();
      }
      if (
        confirmModalRoot.contains(document.activeElement) &&
        document.activeElement instanceof HTMLElement
      ) {
        document.activeElement.blur();
      }
      confirmModalRoot.inert = true;
      confirmModalRoot.classList.add("hidden");
      confirmModalRoot.setAttribute("aria-hidden", "true");
    }
    if (confirmModalConfirmBtn) {
      confirmModalConfirmBtn.classList.remove("danger");
      confirmModalConfirmBtn.textContent = "确认";
    }
    if (confirmResolver) {
      const resolver = confirmResolver;
      confirmResolver = null;
      resolver(result);
    }
    unlockPageScroll();
  }

  function showConfirm({
    title,
    description,
    confirmLabel = "确认",
    confirmClassName = "",
  }) {
    if (
      !confirmModalRoot ||
      !confirmModalTitle ||
      !confirmModalDescription ||
      !confirmModalConfirmBtn
    ) {
      return Promise.resolve(confirm(`${title}\n\n${description}`));
    }

    confirmModalTitle.textContent = title;
    confirmModalDescription.textContent = description;
    confirmModalConfirmBtn.textContent = confirmLabel;
    confirmModalConfirmBtn.classList.toggle(
      "danger",
      confirmClassName.includes("danger"),
    );
    const activeElement = document.activeElement;
    confirmRestoreFocusElement =
      activeElement instanceof HTMLElement &&
      !confirmModalRoot.contains(activeElement)
        ? activeElement
        : null;
    lockPageScroll();
    confirmModalRoot.inert = false;
    confirmModalRoot.classList.remove("hidden");
    confirmModalRoot.setAttribute("aria-hidden", "false");
    window.requestAnimationFrame(() => {
      if (!confirmModalRoot.classList.contains("hidden")) {
        (confirmModalCancelBtn || confirmModalConfirmBtn).focus();
      }
    });

    return new Promise((resolve) => {
      confirmResolver = resolve;
    });
  }

  async function parseResponsePayload(response) {
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      return response.json();
    }

    const text = await response.text();
    return {
      message:
        text.startsWith("<!DOCTYPE") || text.startsWith("<html")
          ? "服务器返回了错误页面，请联系管理员"
          : text,
    };
  }

  async function requestJson(
    url,
    options = {},
    { defaultErrorMessage = "请求失败" } = {},
  ) {
    const response = await fetch(url, options);
    const payload = await parseResponsePayload(response).catch(() => ({}));

    if (!response.ok) {
      const error = new Error(
        payload.message || payload.error || defaultErrorMessage,
      );
      error.status = response.status;
      error.code = payload.code || null;
      error.payload = payload;
      throw error;
    }

    return payload;
  }

  async function refreshUi({
    emojis = false,
    syncStatus = false,
    imgHostStatus = false,
  } = {}) {
    if (emojis) {
      await Promise.all([fetchEmojis(), refreshManagePackSummaries()]);
    }
    if (syncStatus) {
      await checkSyncStatus(false);
    }
    if (imgHostStatus) {
      await checkImgHostSyncStatus(false);
    }
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  // 模态弹窗共享滚动锁：计数式，支持弹窗叠加（预览上再弹确认框）
  let scrollLockCount = 0;
  let scrollLockSavedY = 0;

  function lockPageScroll() {
    scrollLockCount += 1;
    if (scrollLockCount > 1) {
      return;
    }
    scrollLockSavedY = window.scrollY || 0;
    document.body.classList.add("page-scroll-locked");
    document.body.style.top = `-${scrollLockSavedY}px`;
  }

  function unlockPageScroll() {
    if (scrollLockCount === 0) {
      return;
    }
    scrollLockCount -= 1;
    if (scrollLockCount > 0) {
      return;
    }
    document.body.classList.remove("page-scroll-locked");
    document.body.style.top = "";
    window.scrollTo(0, scrollLockSavedY);
  }

  function isCompactViewport() {
    return window.matchMedia(MOBILE_LAYOUT_MEDIA).matches;
  }

  function isConsoleVisible() {
    return isCompactViewport()
      ? document.body.classList.contains("panel-console-open")
      : !document.body.classList.contains("panel-console-hidden");
  }

  function isDirectoryVisible() {
    return isCompactViewport()
      ? document.body.classList.contains("panel-directory-open")
      : !document.body.classList.contains("panel-directory-hidden");
  }

  function setConsoleVisible(visible) {
    if (isCompactViewport()) {
      document.body.classList.toggle("panel-console-open", visible);
      return;
    }
    document.body.classList.toggle("panel-console-hidden", !visible);
  }

  function setDirectoryVisible(visible) {
    if (isCompactViewport()) {
      document.body.classList.toggle("panel-directory-open", visible);
      return;
    }
    document.body.classList.toggle("panel-directory-hidden", !visible);
  }

  function closeAllPanels() {
    setConsoleVisible(false);
    setDirectoryVisible(false);
  }

  function updatePanelToggleState() {
    const consoleVisible = isConsoleVisible();
    const directoryVisible = isDirectoryVisible();

    if (consoleToggleBtn) {
      consoleToggleBtn.setAttribute("aria-expanded", String(consoleVisible));
      consoleToggleBtn.setAttribute(
        "aria-label",
        consoleVisible ? "收起控制台" : "展开控制台",
      );
      consoleToggleBtn.classList.toggle("active", consoleVisible);
    }

    if (directoryToggleBtn) {
      directoryToggleBtn.setAttribute(
        "aria-expanded",
        String(directoryVisible),
      );
      directoryToggleBtn.setAttribute(
        "aria-label",
        directoryVisible ? "收起目录" : "展开目录",
      );
      directoryToggleBtn.classList.toggle("active", directoryVisible);
    }

    if (sidebarBackdrop) {
      const showBackdrop =
        isCompactViewport() && (consoleVisible || directoryVisible);
      sidebarBackdrop.classList.toggle("hidden", !showBackdrop);
      sidebarBackdrop.setAttribute("aria-hidden", String(!showBackdrop));
    }

    leftPanel?.setAttribute("aria-hidden", String(!consoleVisible));
    directoryPanel?.setAttribute("aria-hidden", String(!directoryVisible));
  }

  function syncSidebarLayout() {
    if (isCompactViewport()) {
      document.body.classList.remove("panel-console-hidden");
      document.body.classList.remove("panel-directory-hidden");
      closeAllPanels();
      updatePanelToggleState();
      return;
    }

    document.body.classList.remove(
      "panel-console-open",
      "panel-directory-open",
    );
    if (
      !document.body.classList.contains("panel-console-hidden") &&
      !document.body.classList.contains("panel-directory-hidden")
    ) {
      setConsoleVisible(true);
      setDirectoryVisible(true);
    }
    updatePanelToggleState();
  }

  function toggleConsolePanel() {
    const visible = !isConsoleVisible();
    if (visible && isCompactViewport()) {
      // 移动端抽屉互斥：打开控制台时先收起目录
      setDirectoryVisible(false);
    }
    setConsoleVisible(visible);
    updatePanelToggleState();
  }

  function toggleDirectoryPanel() {
    const visible = !isDirectoryVisible();
    if (visible && isCompactViewport()) {
      // 移动端抽屉互斥：打开目录时先收起控制台
      setConsoleVisible(false);
    }
    setDirectoryVisible(visible);
    updatePanelToggleState();
  }

  function formatBytes(bytes) {
    if (typeof bytes !== "number" || Number.isNaN(bytes) || bytes < 0) {
      return "未知";
    }
    if (bytes === 0) {
      return "0 B";
    }

    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = bytes;
    let unitIndex = 0;

    while (value >= 1024 && unitIndex < units.length - 1) {
      value /= 1024;
      unitIndex += 1;
    }

    const precision = unitIndex === 0 ? 0 : value >= 100 ? 0 : 1;
    return `${value.toFixed(precision)} ${units[unitIndex]}`;
  }

  function getSortedCategories() {
    return Object.keys(latestEmojiData).sort((left, right) =>
      left.localeCompare(right, "zh-CN"),
    );
  }

  function getMoveableCountForTarget(items, targetCategory) {
    if (!targetCategory) {
      return 0;
    }

    return dedupeEmojiItems(items).filter(
      (item) => item.category !== targetCategory,
    ).length;
  }

  function getAvailableMoveTargets(
    items = Array.from(selectionState.items.values()),
  ) {
    const uniqueItems = dedupeEmojiItems(items);
    if (uniqueItems.length === 0) {
      return [];
    }

    return getSortedCategories().filter(
      (category) => getMoveableCountForTarget(uniqueItems, category) > 0,
    );
  }

  function dedupeEmojiItems(items) {
    const uniqueItems = new Map();
    (items || []).forEach((item) => {
      if (!item?.category || !item?.emoji) {
        return;
      }
      uniqueItems.set(createSelectionKey(item.category, item.emoji), {
        category: item.category,
        emoji: item.emoji,
      });
    });
    return Array.from(uniqueItems.values());
  }

  function groupEmojiItemsByCategory(items) {
    const groupedItems = new Map();
    dedupeEmojiItems(items).forEach((item) => {
      if (!groupedItems.has(item.category)) {
        groupedItems.set(item.category, []);
      }
      groupedItems.get(item.category).push(item.emoji);
    });
    return groupedItems;
  }

  function setClipboardItems(items) {
    clipboardState.items = dedupeEmojiItems(items);
  }

  function getClipboardItems() {
    return dedupeEmojiItems(clipboardState.items);
  }

  function getContextMenuTargetItems(targetEmojiItem) {
    if (!targetEmojiItem) {
      return dedupeEmojiItems(Array.from(selectionState.items.values()));
    }

    const targetCategory = targetEmojiItem.dataset.category;
    const targetEmoji = targetEmojiItem.dataset.emoji;
    if (
      selectionState.enabled &&
      isEmojiSelected(targetCategory, targetEmoji)
    ) {
      return dedupeEmojiItems(Array.from(selectionState.items.values()));
    }

    return [{ category: targetCategory, emoji: targetEmoji }];
  }

  function getPasteableClipboardItems(targetCategory) {
    if (!targetCategory) {
      return [];
    }

    return getClipboardItems().filter(
      (item) => item.category !== targetCategory,
    );
  }

  function closeBatchContextMenu() {
    contextMenuState.items = [];
    contextMenuState.targetCategory = null;
    if (batchContextMenu) {
      batchContextMenu.classList.add("hidden");
      batchContextMenu.setAttribute("aria-hidden", "true");
      batchContextMenu.style.left = "-9999px";
      batchContextMenu.style.top = "-9999px";
    }
  }

  function openBatchContextMenu(event) {
    if (!batchContextMenu || !selectionState.enabled) {
      return;
    }

    closeBatchContextMenu();

    const targetEmojiItem = event.target.closest(".emoji-item");
    const targetCategoryElement = event.target.closest(".category");
    const targetCategory =
      targetEmojiItem?.dataset.category ||
      targetCategoryElement?.dataset.category ||
      null;
    const targetItems = getContextMenuTargetItems(targetEmojiItem);
    const pasteableItems = getPasteableClipboardItems(targetCategory);

    if (targetItems.length === 0 && pasteableItems.length === 0) {
      return;
    }

    contextMenuState.items = targetItems;
    contextMenuState.targetCategory = targetCategory;

    if (batchContextMenuTitle) {
      batchContextMenuTitle.textContent =
        targetItems.length > 0
          ? `批量管理 ${targetItems.length} 个文件`
          : "批量管理";
    }
    if (batchContextMenuSubtitle) {
      if (targetCategory && pasteableItems.length > 0) {
        batchContextMenuSubtitle.textContent = `当前分类：${targetCategory}，可粘贴 ${pasteableItems.length} 个文件`;
      } else if (targetCategory) {
        batchContextMenuSubtitle.textContent = `当前分类：${targetCategory}`;
      } else {
        batchContextMenuSubtitle.textContent = "选择一个操作继续";
      }
    }

    if (contextMenuDeleteBtn) {
      contextMenuDeleteBtn.disabled = targetItems.length === 0;
    }
    if (contextMenuMoveBtn) {
      contextMenuMoveBtn.disabled =
        targetItems.length === 0 ||
        getAvailableMoveTargets(targetItems).length === 0;
    }
    if (contextMenuCopyBtn) {
      contextMenuCopyBtn.disabled = targetItems.length === 0;
    }
    if (contextMenuPasteBtn) {
      contextMenuPasteBtn.disabled =
        pasteableItems.length === 0 || !targetCategory;
    }

    batchContextMenu.classList.remove("hidden");
    batchContextMenu.setAttribute("aria-hidden", "false");

    requestAnimationFrame(() => {
      const menuWidth = batchContextMenu.offsetWidth || 240;
      const menuHeight = batchContextMenu.offsetHeight || 220;
      const left = Math.min(
        window.innerWidth - menuWidth - 12,
        Math.max(12, event.clientX),
      );
      const top = Math.min(
        window.innerHeight - menuHeight - 12,
        Math.max(12, event.clientY),
      );
      batchContextMenu.style.left = `${left}px`;
      batchContextMenu.style.top = `${top}px`;
    });
  }

  function shouldOpenBatchContextMenu(event) {
    if (!selectionState.enabled || hasActiveDragInteraction()) {
      return false;
    }

    return Boolean(
      event.target.closest(".emoji-item") ||
      event.target.closest(".emoji-upload") ||
      event.target.closest(".category"),
    );
  }

  function getDragItemsForEmoji(category, emoji) {
    if (selectionState.enabled && isEmojiSelected(category, emoji)) {
      return dedupeEmojiItems(Array.from(selectionState.items.values()));
    }
    return [{ category, emoji }];
  }

  function getDragReadyLabel(itemCount) {
    return itemCount > 1 ? `${itemCount}项` : "拖";
  }

  function hasActiveDragInteraction() {
    return Boolean(
      longPressState.emojiItem ||
      dragModeState.pointerId !== null ||
      dragModeState.items.length > 0,
    );
  }

  function syncInteractionGuardState() {
    document.body.classList.toggle(
      "drag-session-active",
      hasActiveDragInteraction(),
    );
  }

  function updateDragHudPosition(clientX, clientY) {
    if (!dragHud) {
      return;
    }

    const hudRect = dragHud.getBoundingClientRect();
    const hudWidth = hudRect.width || 72;
    const hudHeight = hudRect.height || 72;
    const x = Math.min(
      window.innerWidth - hudWidth - 10,
      Math.max(10, clientX + DRAG_HUD_OFFSET_X),
    );
    const y = Math.min(
      window.innerHeight - hudHeight - 10,
      Math.max(10, clientY - DRAG_HUD_OFFSET_Y),
    );

    dragHud.style.transform = `translate3d(${Math.round(x)}px, ${Math.round(y)}px, 0)`;
  }

  function stopDragAutoScroll() {
    if (dragModeState.autoScrollFrameId) {
      cancelAnimationFrame(dragModeState.autoScrollFrameId);
      dragModeState.autoScrollFrameId = null;
    }
  }

  function stepDragAutoScroll() {
    if (dragModeState.pointerId === null) {
      stopDragAutoScroll();
      return;
    }

    const topThreshold = 96;
    const bottomThreshold = window.innerHeight - 96;
    let deltaY = 0;

    if (dragModeState.lastClientY < topThreshold) {
      deltaY = Math.max(-18, (dragModeState.lastClientY - topThreshold) * 0.18);
    } else if (dragModeState.lastClientY > bottomThreshold) {
      deltaY = Math.min(
        18,
        (dragModeState.lastClientY - bottomThreshold) * 0.18,
      );
    }

    if (deltaY !== 0) {
      window.scrollBy({ top: deltaY, behavior: "auto" });
      updateActiveDropTarget(
        dragModeState.lastClientX,
        dragModeState.lastClientY,
      );
      showDragHud({
        label: getDragReadyLabel(dragModeState.items.length),
        caption: dragModeState.activeCategory
          ? `松手后移动到 ${dragModeState.activeCategory}`
          : "拖到屏幕边缘可自动滚动",
        progress: 1,
        clientX: dragModeState.lastClientX,
        clientY: dragModeState.lastClientY,
        state: dragModeState.activeCategory ? "target" : "ready",
      });
    }

    dragModeState.autoScrollFrameId = requestAnimationFrame(stepDragAutoScroll);
  }

  function ensureDragAutoScroll() {
    if (dragModeState.autoScrollFrameId) {
      return;
    }
    dragModeState.autoScrollFrameId = requestAnimationFrame(stepDragAutoScroll);
  }

  function showDragHud({
    label,
    caption,
    progress = 0,
    clientX = null,
    clientY = null,
    state = "press",
  }) {
    if (!dragHud) {
      return;
    }

    const safeProgress = Math.max(0, Math.min(progress, 1));
    dragHud.classList.remove("hidden");
    dragHud.classList.add("visible");
    dragHud.dataset.state = state;
    dragHud.style.setProperty(
      "--drag-hud-progress",
      `${safeProgress * 360}deg`,
    );
    dragHud.setAttribute("aria-hidden", "false");

    if (dragHudLabel) {
      dragHudLabel.textContent = label;
    }
    if (dragHudCaption) {
      dragHudCaption.textContent = caption;
    }
    if (typeof clientX === "number" && typeof clientY === "number") {
      updateDragHudPosition(clientX, clientY);
    }
  }

  function hideDragHud() {
    if (!dragHud) {
      return;
    }

    dragHud.classList.remove("visible");
    dragHud.classList.add("hidden");
    dragHud.dataset.state = "idle";
    dragHud.style.setProperty("--drag-hud-progress", "0deg");
    dragHud.style.transform = "translate3d(-9999px, -9999px, 0)";
    dragHud.setAttribute("aria-hidden", "true");

    if (dragHudLabel) {
      dragHudLabel.textContent = `${Math.ceil(LONG_PRESS_DURATION_MS / 1000)}s`;
    }
    if (dragHudCaption) {
      dragHudCaption.textContent = `长按 ${Math.ceil(LONG_PRESS_DURATION_MS / 1000)} 秒进入拖拽`;
    }
  }

  function setLongPressProgress(progress, label) {
    if (!longPressState.emojiItem) {
      return;
    }

    showDragHud({
      label,
      caption: `长按 ${Math.ceil(LONG_PRESS_DURATION_MS / 1000)} 秒进入拖拽`,
      progress,
      clientX: longPressState.currentX,
      clientY: longPressState.currentY,
      state: "press",
    });
  }

  function resetLongPressVisual(emojiItem) {
    if (!emojiItem) {
      return;
    }

    emojiItem.classList.remove("long-press-active");
  }

  function cancelLongPress({ preserveReady = false, keepHud = false } = {}) {
    if (longPressState.timeoutId) {
      clearTimeout(longPressState.timeoutId);
      longPressState.timeoutId = null;
    }
    if (longPressState.intervalId) {
      clearInterval(longPressState.intervalId);
      longPressState.intervalId = null;
    }
    if (longPressState.emojiItem) {
      longPressState.emojiItem.classList.remove("long-press-active");
      if (!preserveReady) {
        resetLongPressVisual(longPressState.emojiItem);
      }
    }

    longPressState.emojiItem = null;
    longPressState.pointerId = null;
    longPressState.startTime = 0;
    longPressState.startX = 0;
    longPressState.startY = 0;
    longPressState.currentX = 0;
    longPressState.currentY = 0;

    if (!keepHud && dragModeState.pointerId === null) {
      hideDragHud();
    }

    syncInteractionGuardState();
  }

  function updateActiveDropTarget(clientX, clientY) {
    clearCategoryDropHighlights();
    dragModeState.activeCategory = null;

    const hoveredElement = document.elementFromPoint(clientX, clientY);
    const categoryDiv = hoveredElement?.closest(".category");
    const targetCategory = categoryDiv?.dataset?.category;

    if (!categoryDiv || !targetCategory) {
      return;
    }

    if (!hasMoveableItemsForTarget(dragModeState.items, targetCategory)) {
      return;
    }

    dragModeState.activeCategory = targetCategory;
    categoryDiv.classList.add("category-drop-active");
  }

  function startPointerDrag(event) {
    if (dragModeState.items.length === 0) {
      return;
    }

    dragModeState.pointerId = event.pointerId;
    dragModeState.isPointerDragging = false;
    dragModeState.activeCategory = null;
    dragModeState.captureElement = event.currentTarget;
    dragModeState.lastClientX = event.clientX;
    dragModeState.lastClientY = event.clientY;
    updateActiveDropTarget(event.clientX, event.clientY);
    ensureDragAutoScroll();
    showDragHud({
      label: getDragReadyLabel(dragModeState.items.length),
      caption: "拖到目标分类，松手即可移动",
      progress: 1,
      clientX: event.clientX,
      clientY: event.clientY,
      state: "ready",
    });
  }

  function updatePointerDrag(event) {
    if (
      dragModeState.pointerId === null ||
      dragModeState.pointerId !== event.pointerId ||
      dragModeState.items.length === 0
    ) {
      return;
    }

    dragModeState.isPointerDragging = true;
    dragModeState.lastClientX = event.clientX;
    dragModeState.lastClientY = event.clientY;
    updateActiveDropTarget(event.clientX, event.clientY);
    showDragHud({
      label: getDragReadyLabel(dragModeState.items.length),
      caption: dragModeState.activeCategory
        ? `松手后移动到 ${dragModeState.activeCategory}`
        : "拖到目标分类，松手即可移动",
      progress: 1,
      clientX: event.clientX,
      clientY: event.clientY,
      state: dragModeState.activeCategory ? "target" : "ready",
    });
  }

  async function finishPointerDrag(event) {
    if (
      dragModeState.pointerId === null ||
      dragModeState.pointerId !== event.pointerId
    ) {
      return;
    }

    const targetCategory = dragModeState.activeCategory;
    const dragItems = dedupeEmojiItems(dragModeState.items);
    const wasDragging = dragModeState.isPointerDragging;

    dragModeState.pointerId = null;
    dragModeState.activeCategory = null;
    dragModeState.isPointerDragging = false;
    dragModeState.lastClientX = 0;
    dragModeState.lastClientY = 0;
    stopDragAutoScroll();
    if (
      dragModeState.captureElement &&
      typeof event.pointerId === "number" &&
      typeof dragModeState.captureElement.releasePointerCapture === "function"
    ) {
      try {
        dragModeState.captureElement.releasePointerCapture(event.pointerId);
      } catch {}
    }
    dragModeState.captureElement = null;
    clearCategoryDropHighlights();
    hideDragHud();
    syncInteractionGuardState();

    if (
      targetCategory &&
      hasMoveableItemsForTarget(dragItems, targetCategory)
    ) {
      await moveEmojiItemsToCategory(targetCategory, dragItems);
      return;
    }

    if (wasDragging) {
      clearDragMode();
      showToast("未拖到有效分类，已取消本次移动。", "warning", "拖拽未完成");
      return;
    }

    if (event.pointerType !== "mouse" && dragItems.length > 0) {
      showToast(
        "拖拽模式已开启，继续拖到目标分类即可移动。",
        "info",
        "等待拖拽",
      );
    }
  }

  function clearDragMode() {
    cancelLongPress({ keepHud: true });

    if (dragModeState.timeoutId) {
      clearTimeout(dragModeState.timeoutId);
      dragModeState.timeoutId = null;
    }

    stopDragAutoScroll();
    if (
      dragModeState.captureElement &&
      typeof dragModeState.pointerId === "number" &&
      typeof dragModeState.captureElement.releasePointerCapture === "function"
    ) {
      try {
        dragModeState.captureElement.releasePointerCapture(
          dragModeState.pointerId,
        );
      } catch {}
    }

    dragModeState.items = [];
    dragModeState.pointerId = null;
    dragModeState.activeCategory = null;
    dragModeState.isPointerDragging = false;
    dragModeState.captureElement = null;
    dragModeState.lastClientX = 0;
    dragModeState.lastClientY = 0;
    document.querySelectorAll(".emoji-item").forEach((emojiItem) => {
      emojiItem.classList.remove("drag-ready", "dragging");
      resetLongPressVisual(emojiItem);
    });
    clearCategoryDropHighlights();
    hideDragHud();
    syncInteractionGuardState();
  }

  function armDragMode(items, pointerContext = {}) {
    const dragItems = dedupeEmojiItems(items);
    if (dragItems.length === 0) {
      return;
    }

    clearDragMode();
    dragModeState.items = dragItems;
    const armedKeys = new Set(
      dragItems.map(({ category, emoji }) =>
        createSelectionKey(category, emoji),
      ),
    );

    document.querySelectorAll(".emoji-item").forEach((emojiItem) => {
      const emojiKey = createSelectionKey(
        emojiItem.dataset.category,
        emojiItem.dataset.emoji,
      );
      const armed = armedKeys.has(emojiKey);
      emojiItem.classList.toggle("drag-ready", armed);
      resetLongPressVisual(emojiItem);
    });

    if (
      typeof pointerContext.clientX === "number" &&
      typeof pointerContext.clientY === "number"
    ) {
      dragModeState.pointerId =
        typeof pointerContext.pointerId === "number"
          ? pointerContext.pointerId
          : null;
      dragModeState.captureElement = pointerContext.sourceElement || null;
      dragModeState.lastClientX = pointerContext.clientX;
      dragModeState.lastClientY = pointerContext.clientY;
      if (
        dragModeState.captureElement &&
        dragModeState.pointerId !== null &&
        typeof dragModeState.captureElement.setPointerCapture === "function"
      ) {
        try {
          dragModeState.captureElement.setPointerCapture(
            dragModeState.pointerId,
          );
        } catch {}
      }
      ensureDragAutoScroll();
      showDragHud({
        label: getDragReadyLabel(dragItems.length),
        caption: "拖到目标分类，松手即可移动",
        progress: 1,
        clientX: pointerContext.clientX,
        clientY: pointerContext.clientY,
        state: "ready",
      });
    }

    syncInteractionGuardState();

    dragModeState.timeoutId = window.setTimeout(() => {
      clearDragMode();
      showToast(
        "拖拽模式已自动退出，请重新长按进入。",
        "info",
        "拖拽模式已结束",
      );
    }, DRAG_READY_TIMEOUT_MS);

    showToast(
      dragItems.length > 1
        ? `已进入拖拽模式，可拖动这 ${dragItems.length} 个表情包到目标分类。`
        : "已进入拖拽模式，可将表情包拖到目标分类。",
      "success",
      "拖拽模式已开启",
    );
  }

  function startLongPress(emojiItem, category, emoji, event) {
    if (
      (event.pointerType === "mouse" && event.button !== 0) ||
      event.target.closest(".delete-btn")
    ) {
      return;
    }

    if (
      emojiItem.classList.contains("drag-ready") &&
      dragModeState.items.length > 0
    ) {
      emojiItem.dataset.suppressClick = "true";
      if (typeof emojiItem.setPointerCapture === "function") {
        try {
          emojiItem.setPointerCapture(event.pointerId);
        } catch {}
      }
      startPointerDrag(event);
      return;
    }

    const dragItems = getDragItemsForEmoji(category, emoji);
    if (dragItems.length === 0) {
      return;
    }

    cancelLongPress();
    if (
      dragModeState.items.length > 0 &&
      !emojiItem.classList.contains("drag-ready")
    ) {
      clearDragMode();
    }

    longPressState.emojiItem = emojiItem;
    longPressState.pointerId = event.pointerId;
    longPressState.startTime = performance.now();
    longPressState.startX = event.clientX;
    longPressState.startY = event.clientY;
    longPressState.currentX = event.clientX;
    longPressState.currentY = event.clientY;

    emojiItem.classList.add("long-press-active");
    syncInteractionGuardState();
    setLongPressProgress(0, `${Math.ceil(LONG_PRESS_DURATION_MS / 1000)}s`);

    longPressState.intervalId = window.setInterval(() => {
      if (!longPressState.emojiItem) {
        return;
      }

      const elapsed = performance.now() - longPressState.startTime;
      const progress = elapsed / LONG_PRESS_DURATION_MS;
      const remainingSeconds = Math.max(
        1,
        Math.ceil((LONG_PRESS_DURATION_MS - elapsed) / 1000),
      );
      setLongPressProgress(progress, `${remainingSeconds}s`);
    }, LONG_PRESS_TICK_MS);

    longPressState.timeoutId = window.setTimeout(() => {
      emojiItem.dataset.suppressClick = "true";
      const pointerContext = {
        pointerId: longPressState.pointerId,
        clientX: longPressState.currentX,
        clientY: longPressState.currentY,
        sourceElement: emojiItem,
      };
      cancelLongPress({ preserveReady: true, keepHud: true });
      armDragMode(dragItems, pointerContext);
    }, LONG_PRESS_DURATION_MS);
  }

  function finishLongPress(event) {
    if (
      !longPressState.emojiItem ||
      (typeof event.pointerId === "number" &&
        longPressState.pointerId !== null &&
        event.pointerId !== longPressState.pointerId)
    ) {
      return;
    }

    cancelLongPress();
  }

  function isInternalEmojiDrag(event) {
    const dragTypes = Array.from(event.dataTransfer?.types || []);
    return dragTypes.includes("application/x-meme-emoji");
  }

  function getDraggedEmojiPayload(event) {
    try {
      const rawPayload = event.dataTransfer?.getData(
        "application/x-meme-emoji",
      );
      if (!rawPayload) {
        return null;
      }
      const payload = JSON.parse(rawPayload);
      if (Array.isArray(payload?.items) && payload.items.length > 0) {
        const items = dedupeEmojiItems(payload.items);
        return items.length > 0 ? { items } : null;
      }
      if (!payload?.category || !payload?.emoji) {
        return null;
      }
      return { items: [{ category: payload.category, emoji: payload.emoji }] };
    } catch {
      return null;
    }
  }

  function hasMoveableItemsForTarget(items, targetCategory) {
    return dedupeEmojiItems(items).some(
      (item) => item.category !== targetCategory,
    );
  }

  function clearCategoryDropHighlights() {
    document
      .querySelectorAll(".category-drop-active")
      .forEach((categoryDiv) => {
        categoryDiv.classList.remove("category-drop-active");
      });
  }

  function normalizeUploadFiles(fileList) {
    const validFiles = [];
    let invalidCount = 0;

    Array.from(fileList || []).forEach((file) => {
      const isImageFile =
        file instanceof File &&
        (file.type.startsWith("image/") ||
          /\.(png|jpe?g|gif|webp|bmp|svg)$/i.test(file.name));

      if (isImageFile) {
        validFiles.push(file);
        return;
      }
      invalidCount += 1;
    });

    return { validFiles, invalidCount };
  }

  function dedupeUploadFiles(files) {
    const uniqueFiles = [];
    const seenSignatures = new Set();
    let duplicateCount = 0;

    files.forEach((file) => {
      const signature = [
        file.name,
        file.size,
        file.lastModified,
        file.type,
      ].join("::");

      if (seenSignatures.has(signature)) {
        duplicateCount += 1;
        return;
      }

      seenSignatures.add(signature);
      uniqueFiles.push(file);
    });

    return { uniqueFiles, duplicateCount };
  }

  function refreshUploadDropzones(category = null) {
    document.querySelectorAll(".emoji-upload").forEach((uploadBlock) => {
      if (category && uploadBlock.dataset.category !== category) {
        return;
      }

      const uploadTitle = uploadBlock.querySelector(".emoji-upload-title");
      const uploadHint = uploadBlock.querySelector(".emoji-upload-hint");
      const uploadMeta = uploadBlock.querySelector(".emoji-upload-meta");
      const uploadProgress = uploadBlock.querySelector(
        ".emoji-upload-progress",
      );
      const uploadProgressBar = uploadBlock.querySelector(
        ".emoji-upload-progress-bar",
      );
      const uploadIconInner = uploadBlock.querySelector(".emoji-upload-icon i");

      if (
        !uploadTitle ||
        !uploadHint ||
        !uploadMeta ||
        !uploadProgress ||
        !uploadProgressBar ||
        !uploadIconInner
      ) {
        return;
      }

      const state = uploadStateByCategory.get(uploadBlock.dataset.category);

      if (!state) {
        uploadBlock.classList.remove("uploading");
        uploadBlock.setAttribute("aria-busy", "false");
        uploadTitle.textContent = "上传表情包";
        uploadHint.textContent = "点击上传图片，或将表情长按 2 秒后拖到这里";
        uploadMeta.textContent = "";
        uploadMeta.classList.add("hidden");
        uploadProgress.classList.add("hidden");
        uploadProgressBar.style.width = "0%";
        uploadIconInner.className = "fas fa-cloud-arrow-up";
        return;
      }

      const processedCount = state.completed + state.failed + state.duplicates;
      const currentIndex = Math.min(processedCount + 1, state.total);
      const progressPercent =
        state.total > 0 ? Math.round((processedCount / state.total) * 100) : 0;

      uploadBlock.classList.add("uploading");
      uploadBlock.setAttribute("aria-busy", "true");
      uploadIconInner.className = "fas fa-spinner fa-spin";
      uploadMeta.classList.remove("hidden");
      uploadProgress.classList.remove("hidden");
      uploadProgressBar.style.width = `${progressPercent}%`;

      if (state.refreshing) {
        uploadTitle.textContent = "正在刷新列表";
        uploadHint.textContent = `已处理 ${state.total} 个文件，正在更新界面`;
      } else {
        uploadTitle.textContent = `正在上传 ${currentIndex}/${state.total}`;
        uploadHint.textContent = state.currentFileName
          ? `当前文件：${state.currentFileName}`
          : "正在准备上传文件";
      }

      const metaParts = [`已完成 ${processedCount}/${state.total}`];
      if (state.duplicates > 0) {
        metaParts.push(`重复 ${state.duplicates}`);
      }
      if (state.failed > 0) {
        metaParts.push(`失败 ${state.failed}`);
      }
      uploadMeta.textContent = metaParts.join("，");
    });
  }

  function isCategoryUploading(category) {
    return uploadStateByCategory.has(category);
  }

  async function uploadFilesToCategory(category, fileList) {
    const { validFiles, invalidCount } = normalizeUploadFiles(fileList);

    if (invalidCount > 0) {
      showToast(
        `已忽略 ${invalidCount} 个非图片文件。`,
        "warning",
        "文件类型不支持",
      );
    }

    if (validFiles.length === 0) {
      return;
    }

    const { uniqueFiles, duplicateCount } = dedupeUploadFiles(validFiles);

    if (duplicateCount > 0) {
      showToast(
        `已忽略本批次中 ${duplicateCount} 个重复文件。`,
        "info",
        "已自动去重",
      );
    }

    if (uniqueFiles.length === 0) {
      return;
    }

    if (isCategoryUploading(category)) {
      showToast(
        `分类 ${category} 正在上传文件，请等待当前批次完成。`,
        "info",
        "上传进行中",
      );
      return;
    }

    const uploadState = {
      total: uniqueFiles.length,
      completed: 0,
      failed: 0,
      duplicates: 0,
      currentFileName: uniqueFiles[0]?.name || "",
      refreshing: false,
    };
    uploadStateByCategory.set(category, uploadState);
    refreshUploadDropzones(category);

    showToast(
      uniqueFiles.length > 1
        ? `开始向 ${category} 上传 ${uniqueFiles.length} 个文件。`
        : `开始向 ${category} 上传 1 个文件。`,
      "info",
      "上传开始",
      2200,
    );

    const failedUploads = [];
    const duplicateUploads = [];

    for (const file of uniqueFiles) {
      uploadState.currentFileName = file.name;
      refreshUploadDropzones(category);

      try {
        await uploadEmoji(category, file);
        uploadState.completed += 1;
      } catch (error) {
        if (error.code === "duplicate_emoji" || error.status === 409) {
          uploadState.duplicates += 1;
          duplicateUploads.push({ fileName: file.name, error });
        } else {
          uploadState.failed += 1;
          failedUploads.push({ fileName: file.name, error });
        }
      }

      refreshUploadDropzones(category);
    }

    if (uploadState.completed > 0) {
      uploadState.refreshing = true;
      uploadState.currentFileName = "";
      refreshUploadDropzones(category);
      await refreshUi({ emojis: true });
    }

    uploadStateByCategory.delete(category);
    refreshUploadDropzones(category);

    if (uploadState.failed === 0 && uploadState.duplicates === 0) {
      showToast(
        uploadState.completed > 1
          ? `已向 ${category} 上传 ${uploadState.completed} 个文件。`
          : `已向 ${category} 上传 1 个文件。`,
        "success",
        "上传成功",
      );
      return;
    }

    if (uploadState.completed > 0 && uploadState.failed === 0) {
      showToast(
        `上传完成，新增 ${uploadState.completed} 个，跳过重复 ${uploadState.duplicates} 个。`,
        "warning",
        "上传已去重",
        4500,
      );
      return;
    }

    if (
      uploadState.completed === 0 &&
      uploadState.duplicates > 0 &&
      uploadState.failed === 0
    ) {
      const firstDuplicateMessage =
        duplicateUploads[0]?.error?.message || "这些文件已存在于当前分类";
      showToast(
        `未新增文件，已跳过 ${uploadState.duplicates} 个重复项：${firstDuplicateMessage}`,
        "info",
        "无需重复上传",
        4500,
      );
      return;
    }

    if (uploadState.completed > 0) {
      showToast(
        `上传完成，成功 ${uploadState.completed} 个，重复 ${uploadState.duplicates} 个，失败 ${uploadState.failed} 个。`,
        "warning",
        "部分上传失败",
        4500,
      );
      return;
    }

    const firstErrorMessage =
      failedUploads[0]?.error?.message || "服务器返回错误";
    showToast(
      `本次上传全部失败：${firstErrorMessage}`,
      "error",
      "上传失败",
      4500,
    );
  }

  function createUploadDropzone(category) {
    const uploadBlock = document.createElement("div");
    uploadBlock.className = "emoji-upload";
    uploadBlock.dataset.category = category;
    uploadBlock.tabIndex = 0;
    uploadBlock.setAttribute("role", "button");
    uploadBlock.setAttribute(
      "aria-label",
      `上传 ${category} 分类表情包，支持点击选择或拖拽图片`,
    );

    const uploadIcon = document.createElement("div");
    uploadIcon.className = "emoji-upload-icon";
    const uploadIconInner = document.createElement("i");
    uploadIconInner.className = "fas fa-cloud-arrow-up";
    uploadIcon.appendChild(uploadIconInner);

    const uploadTitle = document.createElement("div");
    uploadTitle.className = "emoji-upload-title";
    uploadTitle.textContent = "上传表情包";

    const uploadHint = document.createElement("div");
    uploadHint.className = "emoji-upload-hint";
    uploadHint.textContent = "点击上传图片，或将表情长按 2 秒后拖到这里";

    const uploadMeta = document.createElement("div");
    uploadMeta.className = "emoji-upload-meta hidden";

    const uploadProgress = document.createElement("div");
    uploadProgress.className = "emoji-upload-progress hidden";
    const uploadProgressBar = document.createElement("span");
    uploadProgressBar.className = "emoji-upload-progress-bar";
    uploadProgress.appendChild(uploadProgressBar);

    uploadBlock.appendChild(uploadIcon);
    uploadBlock.appendChild(uploadTitle);
    uploadBlock.appendChild(uploadHint);
    uploadBlock.appendChild(uploadMeta);
    uploadBlock.appendChild(uploadProgress);

    const fileInput = document.createElement("input");
    fileInput.type = "file";
    fileInput.style.display = "none";
    fileInput.accept = "image/*";
    fileInput.multiple = true;

    let dragDepth = 0;

    const setDragState = (active) => {
      uploadBlock.classList.toggle("drag-active", active);
    };

    uploadBlock.addEventListener("click", () => {
      if (isCategoryUploading(category)) {
        showToast(
          `分类 ${category} 正在上传文件，请稍候。`,
          "info",
          "上传进行中",
        );
        return;
      }
      fileInput.click();
    });

    uploadBlock.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        if (isCategoryUploading(category)) {
          showToast(
            `分类 ${category} 正在上传文件，请稍候。`,
            "info",
            "上传进行中",
          );
          return;
        }
        fileInput.click();
      }
    });

    fileInput.addEventListener("change", (event) => {
      void uploadFilesToCategory(category, event.target.files);
      fileInput.value = "";
    });

    uploadBlock.addEventListener("dragenter", (event) => {
      if (isInternalEmojiDrag(event)) {
        event.preventDefault();
        return;
      }
      event.preventDefault();
      dragDepth += 1;
      setDragState(true);
    });

    uploadBlock.addEventListener("dragover", (event) => {
      if (isInternalEmojiDrag(event)) {
        event.preventDefault();
        return;
      }
      event.preventDefault();
      if (event.dataTransfer) {
        event.dataTransfer.dropEffect = "copy";
      }
      setDragState(true);
    });

    uploadBlock.addEventListener("dragleave", (event) => {
      if (isInternalEmojiDrag(event)) {
        event.preventDefault();
        return;
      }
      event.preventDefault();
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth === 0) {
        setDragState(false);
      }
    });

    uploadBlock.addEventListener("drop", (event) => {
      if (isInternalEmojiDrag(event)) {
        event.preventDefault();
        dragDepth = 0;
        setDragState(false);
        return;
      }
      event.preventDefault();
      dragDepth = 0;
      setDragState(false);
      if (isCategoryUploading(category)) {
        showToast(
          `分类 ${category} 正在上传文件，请等待当前批次完成。`,
          "info",
          "上传进行中",
        );
        return;
      }
      void uploadFilesToCategory(category, event.dataTransfer?.files);
    });

    refreshUploadDropzones(category);

    return { uploadBlock, fileInput };
  }

  function createDragProgressIndicator() {
    const indicator = document.createElement("div");
    indicator.className = "drag-progress-indicator";

    const ring = document.createElement("div");
    ring.className = "drag-progress-ring";

    const center = document.createElement("div");
    center.className = "drag-progress-center";

    const label = document.createElement("span");
    label.className = "drag-progress-label";
    label.textContent = "拖";

    center.appendChild(label);
    indicator.appendChild(ring);
    indicator.appendChild(center);

    return indicator;
  }

  function bindEmojiInteractions(emojiItem, category, emoji) {
    const selectionIndicator = emojiItem.querySelector(".selection-indicator");
    if (selectionIndicator) {
      selectionIndicator.addEventListener("pointerdown", (event) => {
        event.stopPropagation();
      });
      selectionIndicator.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        if (!selectionState.enabled) {
          setSelectionMode(true);
        }
        toggleEmojiSelection(category, emoji);
      });
    }

    emojiItem.addEventListener("click", () => {
      if (emojiItem.dataset.suppressClick === "true") {
        emojiItem.dataset.suppressClick = "false";
        return;
      }
      if (emojiItem.classList.contains("emoji-load-error")) {
        retryEmojiPreview(emojiItem);
        return;
      }
      if (!selectionState.enabled) {
        void openImagePreview(
          category,
          emoji,
          emojiItem.dataset.previewDataUrl || "",
        );
        return;
      }
      toggleEmojiSelection(category, emoji);
    });

    emojiItem.addEventListener("keydown", (event) => {
      if (!selectionState.enabled) return;
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        toggleEmojiSelection(category, emoji);
      }
    });

    emojiItem.addEventListener("pointerdown", (event) => {
      startLongPress(emojiItem, category, emoji, event);
    });
  }

  function closeMoveTargetModal() {
    if (moveTargetModalRoot) {
      moveTargetModalRoot.classList.add("hidden");
      moveTargetModalRoot.setAttribute("aria-hidden", "true");
    }
    pendingMoveTargetItems = [];
    if (moveTargetList) {
      moveTargetList.innerHTML = "";
    }
    unlockPageScroll();
  }

  function openMoveTargetModal(
    items = Array.from(selectionState.items.values()),
  ) {
    const uniqueItems = dedupeEmojiItems(items);
    if (uniqueItems.length === 0) {
      showToast("请先选择要移动的表情包。", "warning", "未选择项目");
      return;
    }

    const availableTargets = getAvailableMoveTargets(uniqueItems);
    if (availableTargets.length === 0) {
      showToast("当前没有可移动到的其他分类。", "warning", "无法移动");
      return;
    }

    pendingMoveTargetItems = uniqueItems;
    if (moveTargetModalTitle) {
      moveTargetModalTitle.textContent = "选择目标分类";
    }
    if (moveTargetModalDescription) {
      moveTargetModalDescription.textContent =
        uniqueItems.length > 1
          ? `已选 ${uniqueItems.length} 个表情包，选择要批量移动到的分类。`
          : "选择要移动到的目标分类。";
    }

    if (moveTargetList) {
      moveTargetList.innerHTML = "";
      availableTargets.forEach((category) => {
        const moveableCount = getMoveableCountForTarget(uniqueItems, category);
        const optionButton = createButton({
          className: "move-target-option",
          onClick: async () => {
            closeMoveTargetModal();
            await moveEmojiItemsToCategory(category, uniqueItems);
          },
        });

        const title = document.createElement("span");
        title.className = "move-target-option-title";
        title.textContent = category;

        const meta = document.createElement("span");
        meta.className = "move-target-option-meta";
        meta.textContent = `可移动 ${moveableCount} 个表情包`;

        optionButton.appendChild(title);
        optionButton.appendChild(meta);
        moveTargetList.appendChild(optionButton);
      });
    }

    if (moveTargetModalRoot) {
      lockPageScroll();
      moveTargetModalRoot.classList.remove("hidden");
      moveTargetModalRoot.setAttribute("aria-hidden", "false");
    }
  }

  async function moveEmojiItemsToCategory(targetCategory, items) {
    if (!targetCategory) {
      showToast("请先选择目标分类。", "warning", "缺少目标分类");
      return;
    }

    const moveableItems = dedupeEmojiItems(items).filter(
      (item) => item.category !== targetCategory,
    );
    if (moveableItems.length === 0) {
      showToast("当前选择的表情包已经都在目标分类中。", "warning", "无需移动");
      clearDragMode();
      return;
    }

    clearDragMode();

    const groupedItems = groupEmojiItemsByCategory(moveableItems);

    let movedCount = 0;
    const movedKeys = [];
    const conflictFiles = [];
    const missingFiles = [];
    const requestErrors = [];

    for (const [sourceCategory, imageFiles] of groupedItems.entries()) {
      try {
        const data = await apiPost("emoji/batch_move", {
          source_category: sourceCategory,
          target_category: targetCategory,
          image_files: imageFiles,
        });

        movedCount += data.moved_count || 0;
        (data.moved_files || []).forEach((filename) => {
          movedKeys.push(createSelectionKey(sourceCategory, filename));
        });
        (data.conflicting_files || []).forEach((filename) => {
          conflictFiles.push(`${sourceCategory}/${filename}`);
        });
        (data.missing_files || []).forEach((filename) => {
          missingFiles.push(`${sourceCategory}/${filename}`);
        });
      } catch (error) {
        console.error("批量移动表情包失败", error);
        requestErrors.push(`${sourceCategory}: ${error.message}`);
      }
    }

    movedKeys.forEach((selectionKey) => {
      selectionState.items.delete(selectionKey);
    });

    if (movedCount > 0) {
      await refreshUi({ emojis: true, imgHostStatus: true });
    } else {
      updateSelectionUI();
    }

    if (
      requestErrors.length > 0 ||
      conflictFiles.length > 0 ||
      missingFiles.length > 0
    ) {
      const messageParts = [`已成功移动 ${movedCount} 个表情包。`];
      if (conflictFiles.length > 0) {
        messageParts.push(`目标分类已存在：${conflictFiles.join("、")}`);
      }
      if (missingFiles.length > 0) {
        messageParts.push(`源文件不存在：${missingFiles.join("、")}`);
      }
      if (requestErrors.length > 0) {
        messageParts.push(`请求失败：${requestErrors.join("；")}`);
      }
      showToast(messageParts.join("\n"), "warning", "移动部分完成", 5600);
      return;
    }

    showToast(
      `已移动 ${movedCount} 个表情包到 ${targetCategory}`,
      "success",
      "移动成功",
    );
  }

  async function copyEmojiItemsToCategory(targetCategory, items) {
    if (!targetCategory) {
      showToast("请先选择要粘贴到的分类。", "warning", "缺少目标分类");
      return;
    }

    const pasteableItems = dedupeEmojiItems(items).filter(
      (item) => item.category !== targetCategory,
    );

    if (pasteableItems.length === 0) {
      showToast("当前没有可粘贴到该分类的文件。", "warning", "无需粘贴");
      return;
    }

    const groupedItems = groupEmojiItemsByCategory(pasteableItems);
    let copiedCount = 0;
    const conflictFiles = [];
    const missingFiles = [];
    const requestErrors = [];

    for (const [sourceCategory, imageFiles] of groupedItems.entries()) {
      try {
        const data = await apiPost("emoji/batch_copy", {
          source_category: sourceCategory,
          target_category: targetCategory,
          image_files: imageFiles,
        });

        copiedCount += data.copied_count || 0;
        (data.conflicting_files || []).forEach((filename) => {
          conflictFiles.push(`${sourceCategory}/${filename}`);
        });
        (data.missing_files || []).forEach((filename) => {
          missingFiles.push(`${sourceCategory}/${filename}`);
        });
      } catch (error) {
        console.error("批量复制表情包失败", error);
        requestErrors.push(`${sourceCategory}: ${error.message}`);
      }
    }

    if (copiedCount > 0) {
      await refreshUi({ emojis: true, imgHostStatus: true });
    }

    if (
      requestErrors.length > 0 ||
      conflictFiles.length > 0 ||
      missingFiles.length > 0
    ) {
      const messageParts = [`已成功粘贴 ${copiedCount} 个表情包。`];
      if (conflictFiles.length > 0) {
        messageParts.push(`目标分类已存在：${conflictFiles.join("、")}`);
      }
      if (missingFiles.length > 0) {
        messageParts.push(`源文件不存在：${missingFiles.join("、")}`);
      }
      if (requestErrors.length > 0) {
        messageParts.push(`请求失败：${requestErrors.join("；")}`);
      }
      showToast(messageParts.join("\n"), "warning", "粘贴部分完成", 5600);
      return;
    }

    showToast(
      `已粘贴 ${copiedCount} 个表情包到 ${targetCategory}`,
      "success",
      "粘贴成功",
    );
  }

  function attachCategoryDropTarget(categoryDiv, category) {
    let dragDepth = 0;

    const setActive = (active) => {
      categoryDiv.classList.toggle("category-drop-active", active);
    };

    categoryDiv.addEventListener("dragenter", (event) => {
      if (!isInternalEmojiDrag(event)) {
        return;
      }

      const payload = getDraggedEmojiPayload(event);
      if (!payload || !hasMoveableItemsForTarget(payload.items, category)) {
        return;
      }

      event.preventDefault();
      dragDepth += 1;
      setActive(true);
    });

    categoryDiv.addEventListener("dragover", (event) => {
      if (!isInternalEmojiDrag(event)) {
        return;
      }

      const payload = getDraggedEmojiPayload(event);
      if (!payload || !hasMoveableItemsForTarget(payload.items, category)) {
        return;
      }

      event.preventDefault();
      if (event.dataTransfer) {
        event.dataTransfer.dropEffect = "move";
      }
      setActive(true);
    });

    categoryDiv.addEventListener("dragleave", (event) => {
      if (!isInternalEmojiDrag(event)) {
        return;
      }

      event.preventDefault();
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth === 0) {
        setActive(false);
      }
    });

    categoryDiv.addEventListener("drop", async (event) => {
      if (!isInternalEmojiDrag(event)) {
        return;
      }

      const payload = getDraggedEmojiPayload(event);
      dragDepth = 0;
      setActive(false);
      if (!payload || !hasMoveableItemsForTarget(payload.items, category)) {
        return;
      }

      event.preventDefault();
      await moveEmojiItemsToCategory(category, payload.items);
    });
  }

  function setImgHostSyncProgress(message, state = "info") {
    if (!imgHostSyncProgress || !imgHostSyncProgressText) {
      return;
    }

    imgHostSyncProgress.classList.remove(
      "hidden",
      "sync-progress-success",
      "sync-progress-error",
      "sync-progress-warning",
    );
    if (state === "success") {
      imgHostSyncProgress.classList.add("sync-progress-success");
    } else if (state === "error") {
      imgHostSyncProgress.classList.add("sync-progress-error");
    } else if (state === "warning") {
      imgHostSyncProgress.classList.add("sync-progress-warning");
    }
    imgHostSyncProgressText.textContent = message;
  }

  function hideImgHostSyncProgress(delay = 0) {
    if (!imgHostSyncProgress) {
      return;
    }
    window.setTimeout(() => {
      imgHostSyncProgress.classList.add("hidden");
    }, delay);
  }

  async function waitForSyncCompletion(actionLabel = "同步") {
    return new Promise((resolve, reject) => {
      let done = false;
      let subscriptionId = null;
      let pollTimer = null;
      let timeoutTimer = null;

      const cleanup = async () => {
        if (pollTimer) {
          clearInterval(pollTimer);
          pollTimer = null;
        }
        if (timeoutTimer) {
          clearTimeout(timeoutTimer);
          timeoutTimer = null;
        }
        if (subscriptionId && window.AstrBotPluginPage.unsubscribeSSE) {
          try {
            await window.AstrBotPluginPage.unsubscribeSSE(subscriptionId);
          } catch (error) {
            console.warn("取消同步进度订阅失败:", error);
          }
        }
      };

      const finish = (error, result) => {
        if (done) return;
        done = true;
        void cleanup();
        if (error) {
          reject(error);
        } else {
          resolve(result);
        }
      };

      const handleStatus = (status) => {
        if (!status || done) return;

        if (status.running) {
          setImgHostSyncProgress(`${actionLabel}进行中...`, "info");
          return;
        }

        if (!status.completed) {
          return;
        }

        if (status.success === false) {
          const message =
            status.exit_code != null
              ? `${actionLabel}失败，进程退出码：${status.exit_code}`
              : `${actionLabel}失败`;
          finish(new Error(message));
          return;
        }

        if (status.success === true) {
          finish(null, status);
          return;
        }

        finish(new Error("没有检测到正在运行的同步任务"));
      };

      const pollStatus = async () => {
        try {
          const status = await apiGet("img_host/sync/task_status");
          handleStatus(status);
        } catch (error) {
          console.warn("轮询同步状态失败:", error);
        }
      };

      timeoutTimer = window.setTimeout(
        () => {
          finish(
            new Error(`${actionLabel}超时，请查看 AstrBot 日志确认结果。`),
          );
        },
        30 * 60 * 1000,
      );

      pollTimer = window.setInterval(pollStatus, 2000);
      void pollStatus();

      const syncParams = {};
      const currentManagedPackId = String(
        activeManagePackId ||
          managePackSelect?.value ||
          managedPackIdFromUrl ||
          "",
      ).trim();
      if (currentManagedPackId) {
        syncParams.managed_pack_id = currentManagedPackId;
      }

      window.AstrBotPluginPage.subscribeSSE(
        "img_host/sync/progress",
        {
          onOpen: () =>
            setImgHostSyncProgress(`${actionLabel}进行中...`, "info"),
          onMessage: ({ parsed }) => handleStatus(parsed),
          onError: () =>
            setImgHostSyncProgress(
              "实时进度连接异常，已切换轮询确认结果。",
              "warning",
            ),
        },
        syncParams,
      )
        .then((id) => {
          subscriptionId = id;
        })
        .catch((error) => {
          console.warn("订阅同步进度失败，改用轮询:", error);
          setImgHostSyncProgress(
            "实时进度不可用，正在轮询同步结果。",
            "warning",
          );
        });
    });
  }

  // 根据数据生成 DOM 节点，展示每个分类及其表情包，并添加上传块
  function displayCategories(emojiData, tagDescriptions) {
    const container = document.getElementById("emoji-categories");
    container.innerHTML = "";

    const categoryEntries = Object.entries(emojiData || {})
      .map(([category, emojis]) => {
        const source = Array.isArray(emojis) ? emojis : [];
        if (activeSemanticReviewFilter === "all") {
          return [category, source];
        }
        return [
          category,
          source.filter((emoji) => {
            const review = semanticReviewByPath.get(
              semanticReviewKey(category, emoji),
            );
            if (activeSemanticReviewFilter === "reclassified") {
              return Boolean(review?.reclassification_status);
            }
            return (
              String(review?.category_review_status || "unchecked") ===
              activeSemanticReviewFilter
            );
          }),
        ];
      })
      .filter(([, emojis]) => activeSemanticReviewFilter === "all" || emojis.length);
    const totalEmojiCount = categoryEntries.reduce((total, [, emojis]) => {
      return total + (Array.isArray(emojis) ? emojis.length : 0);
    }, 0);

    if (!categoryEntries.length || totalEmojiCount === 0) {
      const hint = document.createElement("div");
      hint.className = "empty-pack-hint";
      hint.innerHTML =
        activeSemanticReviewFilter === "all"
          ? `
        <p class="empty-pack-hint-title">当前还没有表情包内容</p>
        <p class="empty-pack-hint-meta">你可以先新建分类上传表情，或前往资源广场下载官方包；也可直接一键安装官方包。</p>
        <div class="empty-pack-hint-actions">
          <button id="empty-hint-install-official" type="button">一键安装官方包</button>
          <button id="empty-hint-create-category" type="button">新建分类</button>
          <a id="empty-hint-open-catalog" href="#">前往资源广场下载</a>
        </div>
      `
          : `<p class="empty-pack-hint-title">当前筛选条件下没有图片</p>
             <p class="empty-pack-hint-meta">请选择其他分类审核状态，或切回“全部”。</p>`;
      container.appendChild(hint);

      if (activeSemanticReviewFilter === "all" && !emptyPackGuideShown) {
        showToast(
          "当前是空表情包，建议前往资源广场下载官方包。",
          "info",
          "提示",
        );
        emptyPackGuideShown = true;
      }

      const installOfficialBtn = document.getElementById(
        "empty-hint-install-official",
      );
      installOfficialBtn?.addEventListener("click", async () => {
        await installOfficialFirstPackFromHint(installOfficialBtn);
      });

      const createCategoryBtn = document.getElementById(
        "empty-hint-create-category",
      );
      createCategoryBtn?.addEventListener("click", () => {
        document.getElementById("add-category-btn")?.click();
      });

      const openCatalogLink = document.getElementById(
        "empty-hint-open-catalog",
      );
      if (openCatalogLink) {
        openCatalogLink.href = buildCatalogPageUrl();
      }
    }

    categoryEntries.forEach(([category, emojis]) => {
      const categoryDiv = document.createElement("div");
      categoryDiv.className = "category";
      categoryDiv.id = `category-${category}`;
      categoryDiv.dataset.category = category;

      const description = tagDescriptions[category] || `请添加描述`;
      const titleDiv = document.createElement("div");
      titleDiv.className = "category-title";
      const categorySelectedCount = getCategorySelectedCount(category);
      const allSelectedInCategory =
        Array.isArray(emojis) &&
        emojis.length > 0 &&
        emojis.every((emoji) => isEmojiSelected(category, emoji));
      const headerDiv = document.createElement("div");
      headerDiv.className = "category-header";

      const titleMain = document.createElement("div");
      titleMain.className = "category-title-main";

      const categoryName = document.createElement("div");
      categoryName.className = "category-name";
      categoryName.id = `category-name-${category}`;
      categoryName.textContent = category;

      const selectionSummary = document.createElement("span");
      selectionSummary.className = "category-selection-summary";
      selectionSummary.id = `category-selection-summary-${category}`;
      selectionSummary.textContent = selectionState.enabled
        ? `已选 ${categorySelectedCount} / ${emojis.length || 0}`
        : "未开启批量选择";

      titleMain.appendChild(categoryName);
      titleMain.appendChild(selectionSummary);

      const actionsDiv = document.createElement("div");
      actionsDiv.className = "category-actions";

      const editButton = createButton({
        className: "edit-category-btn",
        text: "编辑类别",
        onClick: () => editCategory(category),
      });
      const toggleCategoryButton = createButton({
        className: "select-all-category-btn",
        text: selectionState.enabled
          ? allSelectedInCategory
            ? "取消本类"
            : "本类全选"
          : "本类选择",
        disabled: !Array.isArray(emojis) || emojis.length === 0,
        onClick: () => toggleCategorySelection(category, emojis),
      });
      const clearCategoryButton = createButton({
        className: "clear-category-btn danger",
        text: "清空本类",
        onClick: () => clearCategory(category),
      });
      const deleteCategoryButton = createIconButton({
        className: "delete-category-btn icon-only-btn danger",
        iconClass: "fas fa-trash",
        title: `删除类别 ${category}`,
        ariaLabel: `删除类别 ${category}`,
        onClick: () => deleteCategory(category),
      });

      actionsDiv.appendChild(editButton);
      actionsDiv.appendChild(toggleCategoryButton);
      actionsDiv.appendChild(clearCategoryButton);
      actionsDiv.appendChild(deleteCategoryButton);

      headerDiv.appendChild(titleMain);
      headerDiv.appendChild(actionsDiv);

      const descriptionElement = document.createElement("p");
      descriptionElement.className = "description";
      descriptionElement.id = `category-desc-${category}`;
      descriptionElement.textContent = description;

      titleDiv.appendChild(headerDiv);
      titleDiv.appendChild(descriptionElement);
      categoryDiv.appendChild(titleDiv);

      const emojiGrid = document.createElement("div");
      emojiGrid.className = "emoji-grid";

      // emojis 是数组
      if (Array.isArray(emojis)) {
        emojis.forEach((emoji) => {
          const emojiItem = document.createElement("div");
          emojiItem.className = "emoji-item";
          emojiItem.dataset.category = category;
          emojiItem.dataset.emoji = emoji;
          emojiItem.dataset.suppressClick = "false";
          emojiItem.dataset.loading = "false";
          emojiItem.tabIndex = 0;

          const review = semanticReviewByPath.get(
            semanticReviewKey(category, emoji),
          );
          if (semanticReviewAvailable && review) {
            const reviewStatus = String(
              review.category_review_status || "unchecked",
            );
            const reclassificationStatus = String(
              review.reclassification_status || "",
            );
            emojiItem.classList.add(`review-${reviewStatus}`);
            if (reclassificationStatus) {
              emojiItem.classList.add("review-reclassified");
            }
            emojiItem.dataset.categoryReviewStatus = reviewStatus;

            const semanticBadge = document.createElement("span");
            semanticBadge.className = "emoji-item-semantic-badge";
            const fixedCategoryTag = String(
              review.category_tag || `category:${category}`,
            );
            semanticBadge.textContent = `${fixedCategoryTag} · ${
              reclassificationStatus ? "自动重分类 · " : ""
            }${semanticReviewLabel(reviewStatus)}`;
            const reviewReason = String(
              review.category_review_reason || "",
            ).trim();
            semanticBadge.title =
              reclassificationStatus
                ? `${semanticBadge.textContent}；${String(
                    review.reclassified_from_category || "原分类",
                  )} → ${String(
                    review.reclassified_to_category || category,
                  )}；原因：${String(
                    review.reclassification_reason || reviewReason,
                  )}`
                : reviewStatus === "needs_review" && reviewReason
                  ? `${semanticBadge.textContent}；原因：${reviewReason}`
                  : semanticBadge.textContent;
            emojiItem.appendChild(semanticBadge);
          }

          const selectionIndicator = document.createElement("button");
          selectionIndicator.type = "button";
          selectionIndicator.className = "selection-indicator";
          selectionIndicator.setAttribute("aria-label", "选择表情包");
          emojiItem.appendChild(selectionIndicator);

          // 删除按钮
          const deleteBtn = document.createElement("button");
          deleteBtn.className = "delete-btn";
          deleteBtn.innerHTML = "×";
          deleteBtn.onclick = (e) => {
            e.stopPropagation();
            deleteEmoji(category, emoji);
          };
          emojiItem.appendChild(deleteBtn);
          bindEmojiInteractions(emojiItem, category, emoji);

          setEmojiPreviewLoading(emojiItem);
          emojiGrid.appendChild(emojiItem);
        });
      }

      if (activeSemanticReviewFilter === "all") {
        const { uploadBlock, fileInput } = createUploadDropzone(category);

        // 筛选状态下不显示上传入口，避免把新图片误认为筛选结果。
        emojiGrid.appendChild(uploadBlock);
        emojiGrid.appendChild(fileInput);
      }

      categoryDiv.appendChild(emojiGrid);
      attachCategoryDropTarget(categoryDiv, category);
      container.appendChild(categoryDiv);
    });

    const lazyBackgrounds = container.querySelectorAll(".emoji-item");
    const observer = new IntersectionObserver(
      (entries, observer) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            void loadEmojiPreview(entry.target);
            observer.unobserve(entry.target);
          }
        });
      },
      {
        rootMargin: "220px 0px",
        threshold: 0.01,
      },
    );

    lazyBackgrounds.forEach((item) => {
      observer.observe(item);
    });

    updateSelectionDecorations();
  }

  // 更新侧边栏目录
  function updateSidebar(data, tagDescriptions) {
    const sidebarList = document.getElementById("sidebar-list");
    if (!sidebarList) return;
    sidebarList.innerHTML = "";

    for (const category in data) {
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.href = "#category-" + category;
      a.textContent = category;
      a.addEventListener("click", () => {
        if (isCompactViewport()) {
          closeAllPanels();
          updatePanelToggleState();
        }
      });
      li.appendChild(a);
      sidebarList.appendChild(li);
    }
  }

  function createSelectionKey(category, emoji) {
    return `${category}::${emoji}`;
  }

  function isEmojiSelected(category, emoji) {
    return selectionState.items.has(createSelectionKey(category, emoji));
  }

  function getCategorySelectedCount(category) {
    let count = 0;
    selectionState.items.forEach((item) => {
      if (item.category === category) {
        count += 1;
      }
    });
    return count;
  }

  function pruneSelectionState() {
    const availableKeys = new Set();
    Object.entries(latestEmojiData).forEach(([category, emojis]) => {
      if (!Array.isArray(emojis)) return;
      emojis.forEach((emoji) => {
        availableKeys.add(createSelectionKey(category, emoji));
      });
    });

    Array.from(selectionState.items.keys()).forEach((key) => {
      if (!availableKeys.has(key)) {
        selectionState.items.delete(key);
      }
    });
  }

  function updateSelectionToolbar() {
    const selectedCount = selectionState.items.size;
    const availableMoveTargets = getAvailableMoveTargets();

    if (selectionSummary) {
      selectionSummary.textContent = selectionState.enabled
        ? `已选中 ${selectedCount} 个表情包`
        : "未开启批量选择";
    }
    if (toggleSelectionModeBtn) {
      toggleSelectionModeBtn.textContent = selectionState.enabled
        ? "退出批量选择"
        : "开启批量选择";
    }
    if (batchDeleteBtn) {
      batchDeleteBtn.disabled = !selectionState.enabled || selectedCount === 0;
    }
    if (batchMoveBtn) {
      batchMoveBtn.disabled =
        !selectionState.enabled ||
        selectedCount === 0 ||
        availableMoveTargets.length === 0;
    }
  }

  function updateSelectionDecorations() {
    document.querySelectorAll(".emoji-item").forEach((emojiItem) => {
      const category = emojiItem.dataset.category;
      const emoji = emojiItem.dataset.emoji;
      const selected = isEmojiSelected(category, emoji);
      const selectionIndicator = emojiItem.querySelector(
        ".selection-indicator",
      );

      emojiItem.classList.toggle("selection-mode", selectionState.enabled);
      emojiItem.classList.toggle("selected", selected);
      if (selectionIndicator) {
        selectionIndicator.classList.toggle("checked", selected);
        selectionIndicator.setAttribute(
          "aria-label",
          selected ? "已选中" : "未选择",
        );
      }
    });

    document.querySelectorAll(".category").forEach((categoryDiv) => {
      const category = categoryDiv.dataset.category;
      const totalCount = Array.isArray(latestEmojiData[category])
        ? latestEmojiData[category].length
        : 0;
      const selectedCount = getCategorySelectedCount(category);
      const summary = categoryDiv.querySelector(".category-selection-summary");
      const selectAllBtn = categoryDiv.querySelector(
        ".select-all-category-btn",
      );
      const hasEmojis = totalCount > 0;
      const allSelected = hasEmojis && selectedCount === totalCount;

      if (summary) {
        summary.textContent = selectionState.enabled
          ? `已选 ${selectedCount} / ${totalCount}`
          : "未开启批量选择";
      }
      if (selectAllBtn) {
        selectAllBtn.disabled = !hasEmojis;
        selectAllBtn.textContent = selectionState.enabled
          ? allSelected
            ? "取消本类"
            : "本类全选"
          : "本类选择";
      }
    });
  }

  function updateSelectionUI() {
    updateSelectionToolbar();
    updateSelectionDecorations();
  }

  function clearSelections() {
    clearDragMode();
    closeMoveTargetModal();
    closeBatchContextMenu();
    selectionState.items.clear();
    updateSelectionUI();
  }

  function setSelectionMode(enabled) {
    clearDragMode();
    closeMoveTargetModal();
    closeBatchContextMenu();
    selectionState.enabled = enabled;
    if (!enabled) {
      selectionState.items.clear();
    }
    updateSelectionUI();
  }

  function toggleEmojiSelection(category, emoji) {
    clearDragMode();
    closeMoveTargetModal();
    closeBatchContextMenu();
    const selectionKey = createSelectionKey(category, emoji);
    if (selectionState.items.has(selectionKey)) {
      selectionState.items.delete(selectionKey);
    } else {
      selectionState.items.set(selectionKey, { category, emoji });
    }
    updateSelectionUI();
  }

  function toggleCategorySelection(category, emojis) {
    if (!Array.isArray(emojis) || emojis.length === 0) {
      return;
    }

    clearDragMode();
    closeMoveTargetModal();
    closeBatchContextMenu();
    if (!selectionState.enabled) {
      setSelectionMode(true);
    }

    const allSelected = emojis.every((emoji) =>
      isEmojiSelected(category, emoji),
    );
    emojis.forEach((emoji) => {
      const selectionKey = createSelectionKey(category, emoji);
      if (allSelected) {
        selectionState.items.delete(selectionKey);
      } else {
        selectionState.items.set(selectionKey, { category, emoji });
      }
    });
    updateSelectionUI();
  }

  function getSelectedItemsByCategory() {
    const groupedSelections = new Map();
    selectionState.items.forEach(({ category, emoji }) => {
      if (!groupedSelections.has(category)) {
        groupedSelections.set(category, []);
      }
      groupedSelections.get(category).push(emoji);
    });
    return groupedSelections;
  }

  function copyItemsToClipboard(items) {
    const uniqueItems = dedupeEmojiItems(items);
    if (uniqueItems.length === 0) {
      showToast("请先选择要复制的表情包。", "warning", "未选择项目");
      return false;
    }

    setClipboardItems(uniqueItems);
    showToast(
      uniqueItems.length > 1
        ? `已复制 ${uniqueItems.length} 个表情包，可在目标分类右键后粘贴。`
        : "已复制 1 个表情包，可在目标分类右键后粘贴。",
      "success",
      "已复制到批量剪贴板",
    );
    return true;
  }

  function resetDangerConfirmState() {
    if (dangerConfirmTimer) {
      clearInterval(dangerConfirmTimer);
      dangerConfirmTimer = null;
    }
    dangerConfirmConfig = null;
    dangerConfirmStage = "ack";
    if (dangerModalAcknowledge) {
      dangerModalAcknowledge.checked = false;
      dangerModalAcknowledge.disabled = false;
    }
    if (dangerModalStageText) {
      dangerModalStageText.textContent =
        "请先勾选已理解，勾选后会自动开始 5 秒倒计时。";
    }
    if (dangerModalConfirmBtn) {
      dangerModalConfirmBtn.disabled = true;
      dangerModalConfirmBtn.textContent = "请先勾选上方选项";
    }
  }

  function closeDangerConfirm(result) {
    if (dangerModalRoot) {
      dangerModalRoot.classList.add("hidden");
      dangerModalRoot.setAttribute("aria-hidden", "true");
    }
    resetDangerConfirmState();
    if (dangerConfirmResolver) {
      const resolver = dangerConfirmResolver;
      dangerConfirmResolver = null;
      resolver(result);
    }
    unlockPageScroll();
  }

  function startDangerCountdown() {
    if (dangerConfirmStage !== "ack" || !dangerConfirmConfig) {
      return;
    }

    const countdown = dangerConfirmConfig?.countdown ?? 5;
    let remaining = countdown;

    dangerConfirmStage = "countdown";
    if (dangerModalAcknowledge) {
      dangerModalAcknowledge.disabled = true;
    }
    if (dangerModalStageText) {
      dangerModalStageText.textContent = `安全等待中，还需 ${remaining} 秒，倒计时结束后才可执行。`;
    }
    if (dangerModalConfirmBtn) {
      dangerModalConfirmBtn.disabled = true;
      dangerModalConfirmBtn.textContent = `等待 ${remaining} 秒`;
    }

    dangerConfirmTimer = setInterval(() => {
      remaining -= 1;
      if (remaining > 0) {
        dangerModalStageText.textContent = `安全等待中，还需 ${remaining} 秒，倒计时结束后才可执行。`;
        dangerModalConfirmBtn.textContent = `等待 ${remaining} 秒`;
        return;
      }

      clearInterval(dangerConfirmTimer);
      dangerConfirmTimer = null;
      dangerConfirmStage = "ready";
      dangerModalStageText.textContent =
        "5 秒倒计时已结束，请点击下方按钮执行。";
      dangerModalConfirmBtn.disabled = false;
      dangerModalConfirmBtn.textContent = dangerConfirmConfig.actionLabel;
    }, 1000);
  }

  function showDangerConfirm({
    title,
    description,
    actionLabel,
    countdown = 5,
  }) {
    if (
      !dangerModalRoot ||
      !dangerModalTitle ||
      !dangerModalDescription ||
      !dangerModalConfirmBtn
    ) {
      return Promise.resolve(
        confirm(`${title}\n\n${description}\n\n确认要继续执行吗？`),
      );
    }

    resetDangerConfirmState();
    dangerConfirmConfig = { actionLabel, countdown };
    dangerModalTitle.textContent = title;
    dangerModalDescription.textContent = description;
    if (dangerModalStageText) {
      dangerModalStageText.textContent = `请先勾选已理解，勾选后会自动开始 ${countdown} 秒倒计时。倒计时结束后才可执行。`;
    }
    if (dangerModalConfirmBtn) {
      dangerModalConfirmBtn.textContent = "请先勾选上方选项";
      dangerModalConfirmBtn.disabled = true;
    }
    lockPageScroll();
    dangerModalRoot.classList.remove("hidden");
    dangerModalRoot.setAttribute("aria-hidden", "false");

    return new Promise((resolve) => {
      dangerConfirmResolver = resolve;
    });
  }

  // 上传表情包
  async function uploadEmoji(category, file) {
    return await window.AstrBotPluginPage.upload(
      "emoji/add/" + encodeURIComponent(category),
      file,
    );
  }

  // 删除表情包
  async function deleteEmoji(category, emoji) {
    const confirmed = await showConfirm({
      title: "删除表情包",
      description: `确认删除分类「${category}」中的表情包「${emoji}」？此操作不可恢复。`,
      confirmLabel: "确认删除",
      confirmClassName: "danger",
    });
    if (!confirmed) return;

    try {
      const data = await apiPost("emoji/delete", {
        category,
        image_file: emoji,
      });
      selectionState.items.delete(createSelectionKey(category, emoji));
      await refreshUi({ emojis: true });
      showToast(
        `已从 ${data.category} 删除 ${data.filename}`,
        "success",
        "删除成功",
      );
    } catch (error) {
      console.error("删除表情包失败", error);
      showToast(`删除表情包失败：${error.message}`, "error", "删除失败", 4500);
    }
  }

  async function deleteEmojiItems(
    items,
    { useSelectionState = true, confirmMode = "normal" } = {},
  ) {
    const uniqueItems = dedupeEmojiItems(items);
    const selectedCount = uniqueItems.length;
    if (selectedCount === 0) {
      showToast("请先选择要删除的表情包", "warning", "未选择项目");
      return;
    }

    const confirmDescription = `确认删除已选中的 ${selectedCount} 个表情包？未成功删除的项目会保留选中状态。`;
    const confirmed =
      confirmMode === "danger"
        ? await showDangerConfirm({
            title: "批量删除表情包",
            description: confirmDescription,
            actionLabel: "确认删除已选文件",
            countdown: 5,
          })
        : await showConfirm({
            title: "批量删除表情包",
            description: confirmDescription,
            confirmLabel: "确认批量删除",
            confirmClassName: "danger",
          });
    if (!confirmed) {
      return;
    }

    let deletedCount = 0;
    const errors = [];
    const deletedKeys = [];
    const groupedSelections = groupEmojiItemsByCategory(uniqueItems);

    for (const [category, imageFiles] of groupedSelections.entries()) {
      try {
        const data = await apiPost("emoji/batch_delete", {
          category,
          image_files: imageFiles,
        });
        deletedCount += data.deleted_count || 0;
        (data.deleted_files || []).forEach((filename) => {
          deletedKeys.push(createSelectionKey(category, filename));
        });
      } catch (error) {
        console.error("批量删除失败", error);
        errors.push(`${category}: ${error.message}`);
      }
    }

    if (useSelectionState) {
      deletedKeys.forEach((selectionKey) => {
        selectionState.items.delete(selectionKey);
      });
    }

    if (deletedCount > 0) {
      await refreshUi({ emojis: true });
    } else {
      updateSelectionUI();
    }

    if (errors.length > 0) {
      showToast(
        `已删除 ${deletedCount} 个表情包。\n失败分类：${errors.join("；")}`,
        "warning",
        "批量删除部分完成",
        5200,
      );
      return;
    }

    showToast(`已删除 ${deletedCount} 个表情包`, "success", "批量删除完成");
  }

  async function batchDeleteSelected() {
    await deleteEmojiItems(Array.from(selectionState.items.values()));
  }

  // 删除表情包类别
  async function deleteCategory(category) {
    const emojiCount = Array.isArray(latestEmojiData[category])
      ? latestEmojiData[category].length
      : 0;

    const confirmed = await showDangerConfirm({
      title: `删除分类「${category}」`,
      description: `该操作会删除分类「${category}」本身，并移除其描述配置${
        emojiCount > 0 ? `，同时删除其中的 ${emojiCount} 个表情包` : ""
      }。`,
      actionLabel: "确认删除当前分类",
      countdown: 5,
    });
    if (!confirmed) {
      return;
    }

    try {
      await apiPost("category/delete", { category });
      await refreshUi({ emojis: true, syncStatus: true });
      showToast(`已删除分类 ${category}`, "success", "删除成功");
    } catch (error) {
      console.error("删除分类失败:", error);
      showToast(`删除分类失败：${error.message}`, "error", "删除失败", 4500);
    }
  }

  async function clearCategory(category) {
    const emojiCount = Array.isArray(latestEmojiData[category])
      ? latestEmojiData[category].length
      : 0;
    if (emojiCount === 0) {
      showToast(
        `分类 ${category} 当前没有可清空的表情包`,
        "warning",
        "无需清空",
      );
      return;
    }

    const confirmed = await showDangerConfirm({
      title: `清空分类「${category}」`,
      description: `该操作会删除分类「${category}」下的 ${emojiCount} 个表情包，但会保留分类名称和描述配置。`,
      actionLabel: "确认清空当前分类",
      countdown: 5,
    });
    if (!confirmed) {
      return;
    }

    try {
      const data = await apiPost("category/clear", { category });
      clearSelections();
      await refreshUi({ emojis: true });
      showToast(
        `已清空分类 ${category}，删除 ${data.deleted_count} 个表情包。`,
        "success",
        "清空成功",
      );
    } catch (error) {
      console.error("清空分类失败:", error);
      showToast(`清空分类失败：${error.message}`, "error", "清空失败", 4500);
    }
  }

  async function clearAllEmojiFiles() {
    const totalEmojiCount = Object.values(latestEmojiData).reduce(
      (sum, emojis) => sum + (Array.isArray(emojis) ? emojis.length : 0),
      0,
    );
    if (totalEmojiCount === 0) {
      showToast("当前没有可清空的表情包", "warning", "无需清空");
      return;
    }

    const confirmed = await showDangerConfirm({
      title: "清空全部表情包",
      description: `该操作会删除全部 ${totalEmojiCount} 个表情包，但保留现有分类目录和描述配置。`,
      actionLabel: "确认清空全部表情包",
      countdown: 5,
    });
    if (!confirmed) {
      return;
    }

    try {
      const data = await apiPost("emoji/clear_all");
      clearSelections();
      await refreshUi({ emojis: true });
      showToast(
        `已清空全部表情包，共删除 ${data.deleted_count} 个文件，涉及 ${data.affected_categories} 个分类。`,
        "success",
        "清空成功",
        4200,
      );
    } catch (error) {
      console.error("清空全部表情包失败:", error);
      showToast(
        `清空全部表情包失败：${error.message}`,
        "error",
        "清空失败",
        4500,
      );
    }
  }

  if (toggleSelectionModeBtn) {
    toggleSelectionModeBtn.addEventListener("click", () => {
      setSelectionMode(!selectionState.enabled);
    });
  }

  if (batchDeleteBtn) {
    batchDeleteBtn.addEventListener("click", batchDeleteSelected);
  }

  if (batchMoveBtn) {
    batchMoveBtn.addEventListener("click", () => {
      openMoveTargetModal(Array.from(selectionState.items.values()));
    });
  }

  if (clearAllBtn) {
    clearAllBtn.addEventListener("click", clearAllEmojiFiles);
  }

  if (contextMenuDeleteBtn) {
    contextMenuDeleteBtn.addEventListener("click", async () => {
      const menuItems = dedupeEmojiItems(contextMenuState.items);
      closeBatchContextMenu();
      await deleteEmojiItems(menuItems, {
        useSelectionState:
          menuItems.length > 0 &&
          menuItems.every((item) => isEmojiSelected(item.category, item.emoji)),
        confirmMode: "danger",
      });
    });
  }

  if (contextMenuMoveBtn) {
    contextMenuMoveBtn.addEventListener("click", async () => {
      const menuItems = dedupeEmojiItems(contextMenuState.items);
      closeBatchContextMenu();
      const confirmed = await showConfirm({
        title: "移动表情包",
        description: `确认继续为这 ${menuItems.length} 个表情包选择目标分类？`,
        confirmLabel: "继续选择目标分类",
      });
      if (!confirmed) {
        return;
      }
      openMoveTargetModal(menuItems);
    });
  }

  if (contextMenuCopyBtn) {
    contextMenuCopyBtn.addEventListener("click", async () => {
      const menuItems = dedupeEmojiItems(contextMenuState.items);
      closeBatchContextMenu();
      const confirmed = await showConfirm({
        title: "复制表情包",
        description: `确认复制这 ${menuItems.length} 个表情包到 WebUI 剪贴板？`,
        confirmLabel: "确认复制",
      });
      if (!confirmed) {
        return;
      }
      copyItemsToClipboard(menuItems);
    });
  }

  if (contextMenuPasteBtn) {
    contextMenuPasteBtn.addEventListener("click", async () => {
      const targetCategory = contextMenuState.targetCategory;
      const clipboardItems = getClipboardItems();
      closeBatchContextMenu();
      const confirmed = await showConfirm({
        title: "粘贴表情包",
        description: `确认将剪贴板中的 ${clipboardItems.length} 个表情包粘贴到「${targetCategory}」？`,
        confirmLabel: "确认粘贴",
      });
      if (!confirmed) {
        return;
      }
      await copyEmojiItemsToCategory(targetCategory, clipboardItems);
    });
  }

  if (consoleToggleBtn) {
    consoleToggleBtn.addEventListener("click", () => {
      toggleConsolePanel();
    });
  }

  if (directoryToggleBtn) {
    directoryToggleBtn.addEventListener("click", () => {
      toggleDirectoryPanel();
    });
  }

  if (sidebarBackdrop) {
    sidebarBackdrop.addEventListener("click", () => {
      closeAllPanels();
      updatePanelToggleState();
    });
  }

  document.querySelectorAll(".panel-close-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      closeAllPanels();
      updatePanelToggleState();
    });
  });

  const sidebarListElement = document.getElementById("sidebar-list");
  if (sidebarListElement) {
    sidebarListElement.addEventListener("click", (event) => {
      const link = event.target.closest("a");
      if (!link || !isCompactViewport()) {
        return;
      }
      // 移动端：选中目录项后自动收起抽屉，让正文露出来
      closeAllPanels();
      updatePanelToggleState();
    });
  }

  if (dangerModalAcknowledge) {
    dangerModalAcknowledge.addEventListener("change", () => {
      if (dangerConfirmStage === "ack") {
        if (!dangerModalAcknowledge.checked) {
          dangerModalConfirmBtn.disabled = true;
          dangerModalConfirmBtn.textContent = "请先勾选上方选项";
          return;
        }
        startDangerCountdown();
      }
    });
  }

  if (dangerModalCancelBtn) {
    dangerModalCancelBtn.addEventListener("click", () => {
      closeDangerConfirm(false);
    });
  }

  if (dangerModalConfirmBtn) {
    dangerModalConfirmBtn.addEventListener("click", () => {
      if (dangerConfirmStage === "ack" && dangerModalAcknowledge?.checked) {
        startDangerCountdown();
        return;
      }
      if (dangerConfirmStage === "ready") {
        closeDangerConfirm(true);
      }
    });
  }

  if (dangerModalRoot) {
    dangerModalRoot.addEventListener("click", (event) => {
      if (event.target === dangerModalRoot) {
        closeDangerConfirm(false);
      }
    });
  }

  if (confirmModalCancelBtn) {
    confirmModalCancelBtn.addEventListener("click", () => {
      closeConfirm(false);
    });
  }

  if (confirmModalConfirmBtn) {
    confirmModalConfirmBtn.addEventListener("click", () => {
      closeConfirm(true);
    });
  }

  if (confirmModalRoot) {
    confirmModalRoot.addEventListener("click", (event) => {
      if (event.target === confirmModalRoot) {
        closeConfirm(false);
      }
    });
  }

  if (categoryEditCancelBtn) {
    categoryEditCancelBtn.addEventListener("click", () => {
      closeCategoryEditModal();
    });
  }

  if (categoryEditSaveBtn) {
    categoryEditSaveBtn.addEventListener("click", async () => {
      await saveCategory();
    });
  }

  if (moveTargetCancelBtn) {
    moveTargetCancelBtn.addEventListener("click", () => {
      closeMoveTargetModal();
    });
  }

  if (moveTargetModalRoot) {
    moveTargetModalRoot.addEventListener("click", (event) => {
      if (event.target === moveTargetModalRoot) {
        closeMoveTargetModal();
      }
    });
  }

  if (imagePreviewCloseBtn) {
    imagePreviewCloseBtn.addEventListener("click", () => {
      closeImagePreview();
    });
  }

  if (imagePreviewOriginalBtn) {
    imagePreviewOriginalBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      void showOriginalPreview();
    });
  }

  if (imagePreviewModalRoot) {
    imagePreviewModalRoot.addEventListener("click", (event) => {
      if (
        event.target === imagePreviewModalRoot ||
        event.target?.classList?.contains("image-preview-stage")
      ) {
        closeImagePreview();
      }
    });
  }

  if (categoryEditModalRoot) {
    categoryEditModalRoot.addEventListener("click", (event) => {
      if (event.target === categoryEditModalRoot) {
        closeCategoryEditModal();
      }
    });
  }

  [categoryEditNameInput, categoryEditDescInput].forEach((input) => {
    input?.addEventListener("keydown", async (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        await saveCategory();
      }
    });
  });

  document.addEventListener("pointermove", (event) => {
    if (
      longPressState.emojiItem &&
      typeof event.pointerId === "number" &&
      event.pointerId === longPressState.pointerId
    ) {
      const offsetX = event.clientX - longPressState.startX;
      const offsetY = event.clientY - longPressState.startY;
      const movedDistance = Math.hypot(offsetX, offsetY);
      if (movedDistance > LONG_PRESS_CANCEL_DISTANCE_PX) {
        cancelLongPress();
        return;
      }

      longPressState.currentX = event.clientX;
      longPressState.currentY = event.clientY;

      const elapsed = performance.now() - longPressState.startTime;
      const progress = Math.min(1, elapsed / LONG_PRESS_DURATION_MS);
      const remainingSeconds = Math.max(
        1,
        Math.ceil((LONG_PRESS_DURATION_MS - elapsed) / 1000),
      );
      setLongPressProgress(progress, `${remainingSeconds}s`);
      event.preventDefault();
    }

    if (
      dragModeState.pointerId !== null &&
      typeof event.pointerId === "number" &&
      event.pointerId === dragModeState.pointerId
    ) {
      updatePointerDrag(event);
      event.preventDefault();
    }
  });

  const handlePointerRelease = async (event) => {
    finishLongPress(event);
    await finishPointerDrag(event);
  };

  document.addEventListener("pointerup", (event) => {
    void handlePointerRelease(event);
  });

  document.addEventListener("pointercancel", (event) => {
    void handlePointerRelease(event);
  });

  document.addEventListener(
    "touchmove",
    (event) => {
      if (dragModeState.pointerId !== null) {
        event.preventDefault();
      }
    },
    { passive: false },
  );

  document.addEventListener("dragstart", (event) => {
    if (hasActiveDragInteraction() || event.target?.closest?.(".emoji-item")) {
      event.preventDefault();
    }
  });

  document.addEventListener("contextmenu", (event) => {
    if (shouldOpenBatchContextMenu(event)) {
      event.preventDefault();
      openBatchContextMenu(event);
      return;
    }

    closeBatchContextMenu();

    if (hasActiveDragInteraction()) {
      event.preventDefault();
    }
  });

  document.addEventListener("click", (event) => {
    if (!batchContextMenu || batchContextMenu.classList.contains("hidden")) {
      return;
    }
    if (event.target.closest("#batch-context-menu")) {
      return;
    }
    closeBatchContextMenu();
  });

  document.addEventListener(
    "scroll",
    () => {
      closeBatchContextMenu();
    },
    true,
  );

  document.addEventListener("selectstart", (event) => {
    if (
      hasActiveDragInteraction() ||
      event.target?.closest?.(".emoji-item") ||
      event.target?.closest?.(".emoji-upload")
    ) {
      event.preventDefault();
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && dragModeState.items.length > 0) {
      clearDragMode();
      showToast("已退出拖拽模式。", "info", "拖拽模式已关闭");
      return;
    }
    if (event.key === "Escape" && batchContextMenu) {
      const isBatchContextMenuOpen =
        !batchContextMenu.classList.contains("hidden");
      if (isBatchContextMenuOpen) {
        closeBatchContextMenu();
        return;
      }
    }
    if (event.key === "Escape" && isCompactViewport()) {
      const isAnyPanelOpen = isConsoleVisible() || isDirectoryVisible();
      if (isAnyPanelOpen) {
        closeAllPanels();
        updatePanelToggleState();
        return;
      }
    }
    if (event.key === "Escape" && moveTargetModalRoot) {
      const isMoveTargetOpen =
        !moveTargetModalRoot.classList.contains("hidden");
      if (isMoveTargetOpen) {
        closeMoveTargetModal();
        return;
      }
    }
    if (event.key === "Escape" && imagePreviewModalRoot) {
      const isPreviewOpen = !imagePreviewModalRoot.classList.contains("hidden");
      if (isPreviewOpen) {
        closeImagePreview();
        return;
      }
    }
    if (event.key === "Escape" && categoryEditModalRoot) {
      const isEditOpen = !categoryEditModalRoot.classList.contains("hidden");
      if (isEditOpen) {
        closeCategoryEditModal();
        return;
      }
    }
    if (event.key === "Escape" && confirmModalRoot) {
      const isConfirmOpen = !confirmModalRoot.classList.contains("hidden");
      if (isConfirmOpen) {
        closeConfirm(false);
        return;
      }
    }
    if (event.key === "Escape" && dangerModalRoot) {
      const isOpen = !dangerModalRoot.classList.contains("hidden");
      if (isOpen) {
        closeDangerConfirm(false);
      }
    }
  });

  // 分类相关的事件处理
  document
    .getElementById("add-category-btn")
    .addEventListener("click", function () {
      document.getElementById("add-category-form").style.display = "block";
      this.style.display = "none";
    });

  document
    .getElementById("save-category-btn")
    .addEventListener("click", async function () {
      const categoryName = document
        .getElementById("new-category-name")
        .value.trim();
      const categoryDesc =
        document.getElementById("new-category-description").value.trim() ||
        "请添加描述";

      if (!categoryName) {
        showToast("请输入类别名称后再保存。", "warning", "缺少类别名称");
        return;
      }

      const saveButton = this;
      setButtonBusy(saveButton, "保存中...");

      try {
        await apiPost("category/restore", {
          category: categoryName,
          description: categoryDesc,
        });

        document.getElementById("new-category-name").value = "";
        document.getElementById("new-category-description").value = "";
        document.getElementById("add-category-form").style.display = "none";
        document.getElementById("add-category-btn").style.display = "block";
        await refreshUi({ emojis: true, syncStatus: true });
        showToast(`类别「${categoryName}」已添加。`, "success", "添加成功");
      } catch (error) {
        console.error("添加类别失败:", error);
        showToast(error.message, "error", "添加失败");
      } finally {
        restoreButton(saveButton);
      }
    });

  function createSyncStatusSection(title, categories, actionsBuilder = null) {
    const section = document.createElement("div");
    section.className = "status-section";

    const heading = document.createElement("h4");
    heading.textContent = title;
    section.appendChild(heading);

    const list = document.createElement("ul");
    categories.forEach((category) => {
      const item = document.createElement("li");
      const label = document.createElement("span");
      label.textContent = category;
      item.appendChild(label);

      if (actionsBuilder) {
        item.appendChild(actionsBuilder(category));
      }

      list.appendChild(item);
    });
    section.appendChild(list);

    return section;
  }

  function normalizeSyncDifferences(payload) {
    const source =
      payload &&
      typeof payload.differences === "object" &&
      payload.differences !== null
        ? payload.differences
        : payload;

    return {
      missing_in_config: Array.isArray(source?.missing_in_config)
        ? source.missing_in_config
        : [],
      deleted_categories: Array.isArray(source?.deleted_categories)
        ? source.deleted_categories
        : [],
    };
  }

  function renderSyncStatus(statusDiv, differences) {
    statusDiv.innerHTML = "";
    const fragments = [];
    const normalizedDifferences = normalizeSyncDifferences(differences);

    if (normalizedDifferences.missing_in_config.length > 0) {
      fragments.push(
        createSyncStatusSection(
          "新增类别（需要添加到配置）：",
          normalizedDifferences.missing_in_config,
          () =>
            createButton({
              className: "sync-btn",
              text: "同步配置",
              onClick: () => syncConfig(),
            }),
        ),
      );
    }

    if (normalizedDifferences.deleted_categories.length > 0) {
      fragments.push(
        createSyncStatusSection(
          "已删除的类别（配置中仍存在）：",
          normalizedDifferences.deleted_categories,
          (category) => {
            const actions = document.createElement("div");
            actions.className = "action-buttons";
            actions.appendChild(
              createButton({
                className: "restore-btn",
                text: "恢复类别",
                onClick: () => restoreCategory(category),
              }),
            );
            actions.appendChild(
              createButton({
                className: "remove-btn",
                text: "从配置中删除",
                onClick: () => removeFromConfig(category),
              }),
            );
            return actions;
          },
        ),
      );
    }

    if (fragments.length === 0) {
      const text = document.createElement("p");
      text.textContent = "配置与文件夹结构一致！";
      statusDiv.appendChild(text);
      return;
    }

    fragments.forEach((fragment) => {
      statusDiv.appendChild(fragment);
    });

    const syncActions = document.createElement("div");
    syncActions.className = "sync-actions";
    syncActions.appendChild(
      createButton({
        className: "main-sync-btn",
        text: "同步所有配置",
        onClick: () => syncConfig(),
      }),
    );
    statusDiv.appendChild(syncActions);
  }

  function renderSyncStatusError(statusDiv, message) {
    statusDiv.innerHTML = "";

    const errorText = document.createElement("p");
    errorText.style.color = "red";
    errorText.textContent = `检查同步状态失败: ${message}`;
    statusDiv.appendChild(errorText);

    statusDiv.appendChild(
      createButton({
        className: "retry-btn",
        text: "重试",
        onClick: () => checkSyncStatus(),
      }),
    );
  }

  async function checkSyncStatus(showAlert = true) {
    const statusDiv = document.getElementById("sync-status");
    if (!statusDiv) return;

    const btn = document.getElementById("check-sync-btn");
    setButtonBusy(btn, "正在检查中...");

    try {
      const data = await apiGet("sync/status");
      if (data.status === "error") throw new Error(data.message);

      const differences = normalizeSyncDifferences(data);
      renderSyncStatus(statusDiv, differences);

      if (showAlert) {
        showToast("配置状态已刷新。", "success", "检查完成");
      }
    } catch (error) {
      console.error("检查同步状态失败:", error);
      renderSyncStatusError(statusDiv, error.message);
      if (showAlert) {
        showToast(error.message, "error", "检查失败");
      }
    } finally {
      restoreButton(btn);
    }
  }

  async function syncToRemote() {
    const btn = document.getElementById("upload-sync-btn");
    try {
      setButtonBusy(btn, "上传中, 这可能需要很久...");
      setImgHostSyncProgress("正在启动上传同步...", "info");

      await apiPost("img_host/sync/upload");
      await waitForSyncCompletion("上传同步");
      await refreshUi({ syncStatus: true, imgHostStatus: true });
      setImgHostSyncProgress("上传同步已完成。", "success");
      hideImgHostSyncProgress(3000);
      showToast("云端上传同步已完成。", "success", "同步成功");
    } catch (error) {
      console.error("同步到云端失败:", error);
      setImgHostSyncProgress(error.message, "error");
      showToast(error.message, "error", "同步失败");
    } finally {
      restoreButton(btn);
    }
  }

  async function forceSyncToRemote() {
    const confirmed = await showDangerConfirm({
      title: "强制同步云端",
      description:
        "该操作会让云端图库与当前本地图库完全一致：上传本地缺失文件，并删除云端多出的文件。本地已经删除的图片会从云端删除。",
      actionLabel: "确认强制同步云端",
      countdown: 5,
    });
    if (!confirmed) {
      return;
    }

    const btn = document.getElementById("force-upload-sync-btn");
    try {
      setButtonBusy(btn, "强制同步中...");
      setImgHostSyncProgress("正在启动强制同步云端...", "warning");

      await apiPost("img_host/sync/overwrite_to_remote");
      await waitForSyncCompletion("强制同步云端");
      await refreshUi({ syncStatus: true, imgHostStatus: true });
      setImgHostSyncProgress("强制同步云端已完成。", "success");
      hideImgHostSyncProgress(3000);
      showToast("强制同步云端已完成。", "success", "同步成功");
    } catch (error) {
      console.error("强制同步云端失败:", error);
      setImgHostSyncProgress(error.message, "error");
      showToast(error.message, "error", "同步失败");
    } finally {
      restoreButton(btn);
    }
  }

  async function syncFromRemote() {
    const btn = document.getElementById("download-sync-btn");
    try {
      setButtonBusy(btn, "下载中...");
      setImgHostSyncProgress("正在启动下载同步...", "info");

      await apiPost("img_host/sync/download");
      await waitForSyncCompletion("下载同步");
      await refreshUi({ emojis: true, syncStatus: true, imgHostStatus: true });
      setImgHostSyncProgress("下载同步已完成。", "success");
      hideImgHostSyncProgress(3000);
      showToast("云端下载同步已完成。", "success", "同步成功");
    } catch (error) {
      console.error("从云端同步失败:", error);
      setImgHostSyncProgress(error.message, "error");
      showToast(error.message, "error", "同步失败");
    } finally {
      restoreButton(btn);
    }
  }

  // 同步按钮的事件监听器
  document
    .getElementById("check-sync-btn")
    .addEventListener("click", checkSyncStatus);
  document
    .getElementById("upload-sync-btn")
    .addEventListener("click", syncToRemote);
  document
    .getElementById("force-upload-sync-btn")
    .addEventListener("click", forceSyncToRemote);
  document
    .getElementById("download-sync-btn")
    .addEventListener("click", syncFromRemote);

  // 同步配置的函数
  async function syncConfig() {
    try {
      await apiPost("sync/config");
      await refreshUi({ emojis: true, syncStatus: true });
      showToast("配置已同步到最新状态。", "success", "同步成功");
    } catch (error) {
      console.error("同步配置失败:", error);
      showToast(error.message, "error", "同步失败");
    }
  }

  // 恢复类别
  async function restoreCategory(category) {
    try {
      const data = await apiPost("category/restore", { category });

      await refreshUi({ emojis: true, syncStatus: true });
      showToast(
        `类别「${category}」已恢复。\n描述：${data.description || "请补充描述"}`,
        "success",
        "恢复成功",
      );
    } catch (error) {
      console.error("恢复类别失败:", error);
      showToast(error.message, "error", "恢复失败");
    }
  }

  // 从配置中删除类别
  async function removeFromConfig(category) {
    const confirmed = await showConfirm({
      title: "从配置中删除类别",
      description: `确定要从配置中删除「${category}」吗？该操作不会删除磁盘上的文件夹，只会移除配置记录。`,
      confirmLabel: "确认删除",
      confirmClassName: "danger",
    });
    if (!confirmed) {
      return;
    }

    try {
      await apiPost("category/remove_from_config", { category });

      await refreshUi({ syncStatus: true });
      showToast(`类别「${category}」已从配置中移除。`, "success", "移除成功");
    } catch (error) {
      console.error("从配置中删除类别失败:", error);
      showToast(error.message, "error", "移除失败");
    }
  }

  function closeCategoryEditModal() {
    if (categoryEditModalRoot) {
      categoryEditModalRoot.classList.add("hidden");
      categoryEditModalRoot.setAttribute("aria-hidden", "true");
    }
    activeCategoryEdit = null;
    if (categoryEditNameInput) {
      categoryEditNameInput.value = "";
    }
    if (categoryEditDescInput) {
      categoryEditDescInput.value = "";
    }
    unlockPageScroll();
  }

  // 编辑类别
  function editCategory(category) {
    const currentDescription = document
      .getElementById(`category-desc-${category}`)
      ?.textContent?.trim();

    activeCategoryEdit = category;
    if (categoryEditModalTitle) {
      categoryEditModalTitle.textContent = `编辑类别「${category}」`;
    }
    if (categoryEditModalDescription) {
      categoryEditModalDescription.textContent =
        "修改类别名称和描述，保存后立即生效。";
    }
    if (categoryEditNameInput) {
      categoryEditNameInput.value = category;
    }
    if (categoryEditDescInput) {
      categoryEditDescInput.value =
        currentDescription && currentDescription !== "请添加描述"
          ? currentDescription
          : "";
    }
    if (categoryEditModalRoot) {
      lockPageScroll();
      categoryEditModalRoot.classList.remove("hidden");
      categoryEditModalRoot.setAttribute("aria-hidden", "false");
    }
    window.setTimeout(() => {
      categoryEditNameInput?.focus();
      categoryEditNameInput?.select();
    }, 0);
  }

  // 取消编辑
  function cancelEdit() {
    closeCategoryEditModal();
  }

  // 保存类别修改
  async function saveCategory(oldName = activeCategoryEdit) {
    const newName = categoryEditNameInput?.value.trim() || "";
    const newDesc = categoryEditDescInput?.value.trim() || "";

    if (!newName) {
      showToast("类别名称不能为空。", "warning", "保存失败");
      return;
    }

    if (!oldName) {
      showToast("未找到当前正在编辑的类别。", "error", "保存失败");
      return;
    }

    setButtonBusy(categoryEditSaveBtn, "保存中...");

    try {
      if (oldName !== newName) {
        await apiPost("category/rename", {
          old_name: oldName,
          new_name: newName,
        });
      }

      await apiPost("category/update_description", {
        tag: newName,
        description: newDesc,
      });

      await refreshUi({ emojis: true, syncStatus: true });
      closeCategoryEditModal();
      showToast(`类别「${newName}」已保存。`, "success", "保存成功");
    } catch (error) {
      console.error("保存类别修改失败:", error);
      showToast(error.message, "error", "保存失败");
    } finally {
      restoreButton(categoryEditSaveBtn);
    }
  }

  // 这些函数是全局可访问的
  window.restoreCategory = restoreCategory;
  window.removeFromConfig = removeFromConfig;
  window.syncConfig = syncConfig;
  window.editCategory = editCategory;
  window.cancelEdit = cancelEdit;
  window.saveCategory = saveCategory;

  // 初始化加载数据
  syncSidebarLayout();
  updatePanelToggleState();
  window.addEventListener("resize", () => {
    syncSidebarLayout();
    closeBatchContextMenu();
  });

  exportModeInputs.forEach((input) => {
    input.addEventListener("change", updateExportModeAppearance);
  });
  exportPackDownloadBtn?.addEventListener("click", () => {
    void downloadCurrentPack();
  });
  packImportFile?.addEventListener("change", (event) => {
    const file = event.target?.files?.[0];
    void stagePackImport(file);
  });
  ["dragenter", "dragover"].forEach((eventName) => {
    packImportDropzone?.addEventListener(eventName, (event) => {
      event.preventDefault();
      packImportDropzone.classList.add("dragover");
    });
  });
  ["dragleave", "drop"].forEach((eventName) => {
    packImportDropzone?.addEventListener(eventName, (event) => {
      event.preventDefault();
      packImportDropzone.classList.remove("dragover");
    });
  });
  packImportDropzone?.addEventListener("drop", (event) => {
    const file = event.dataTransfer?.files?.[0];
    void stagePackImport(file);
  });
  packImportResetBtn?.addEventListener("click", () => {
    resetPackImportPreview();
  });
  packImportConfirmBtn?.addEventListener("click", () => {
    void confirmPackImport();
  });
  rebuildPackVectorsBtn?.addEventListener("click", () => {
    const packId = String(activeManagePackId || "").trim();
    const status =
      latestManagePackVectorStatusId === packId
        ? latestManagePackVectorStatus
        : null;
    if (!status) {
      void refreshManagePackVectorStatus(packId);
      return;
    }
    void confirmAndRebuildVector(packId, status, { manual: true });
  });
  updateExportModeAppearance();

  await loadManagePackSwitcher();
  await fetchEmojis();
  semanticReviewStats?.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-review-filter]");
    if (!button) return;
    activeSemanticReviewFilter = String(button.dataset.reviewFilter || "all");
    updateSemanticReviewToolbar();
    displayCategories(latestEmojiData, latestTagDescriptions);
    updateSidebar(latestEmojiData, latestTagDescriptions);
  });
  imagePreviewCategoryConfirmBtn?.addEventListener("click", () => {
    void confirmCurrentImageCategory();
  });
  imagePreviewEditBtn?.addEventListener("click", () => {
    setImageSemanticEditing(true);
  });
  imagePreviewReviewEditBtn?.addEventListener("click", () => {
    setImageSemanticEditing(true);
  });
  imagePreviewEditCancelBtn?.addEventListener("click", () => {
    setImageSemanticEditing(false);
  });
  imagePreviewEditForm?.addEventListener("submit", (event) => {
    event.preventDefault();
    void saveCurrentImageSemantic({ updateVector: false });
  });
  imagePreviewSaveVectorBtn?.addEventListener("click", () => {
    void saveCurrentImageSemantic({ updateVector: true });
  });
  imagePreviewReviewRewriteBtn?.addEventListener("click", () => {
    void requestImageSemanticRevision();
  });
  imagePreviewTargetCategory?.addEventListener("change", () => {
    updateImageSemanticMoveChoice();
  });
  imagePreviewRestoreAutoBtn?.addEventListener("click", () => {
    void restoreCurrentImageAutoSemantic();
  });
  await maybeOfferVectorRebuild(activeManagePackId);
  switchManagePackBtn?.addEventListener("click", () => {
    void switchManagePack();
  });
  deleteManagePackBtn?.addEventListener("click", () => {
    void deleteCurrentManagePack();
  });
  initialStatusTimerId = window.setTimeout(() => {
    initialStatusTimerId = null;
    void checkSyncStatus(false);
    void checkImgHostSyncStatus(false);
  }, 180);

  // 检查图床同步状态
  async function checkImgHostSyncStatus(showAlert = true) {
    const uploadCountElement = document.getElementById("upload-count");
    const downloadCountElement = document.getElementById("download-count");
    const remoteExtraCountElement =
      document.getElementById("remote-extra-count");
    const localExtraCountElement = document.getElementById("local-extra-count");
    const providerElement = document.getElementById("img-host-provider");
    const remoteImageCountElement =
      document.getElementById("remote-image-count");
    const remoteStorageSizeElement = document.getElementById(
      "remote-storage-size",
    );

    try {
      const data = await apiGet("img_host/sync/status");

      const uploadCount = data.upload_count ?? data.to_upload?.length ?? 0;
      const downloadCount =
        data.download_count ?? data.to_download?.length ?? 0;
      const remoteExtraCount =
        data.remote_extra_count ?? data.to_delete_remote?.length ?? 0;
      const localExtraCount =
        data.local_extra_count ?? data.to_delete_local?.length ?? 0;
      const remoteImageCount =
        data.remote_image_count ??
        data.remote_count ??
        data.remote_images?.length ??
        0;
      let remoteStorageText = "未知";
      if (typeof data.remote_total_bytes === "number") {
        remoteStorageText = formatBytes(data.remote_total_bytes);
      } else if (typeof data.remote_total_bytes_estimated === "number") {
        remoteStorageText = `${formatBytes(data.remote_total_bytes_estimated)}（本地估算）`;
      }

      if (uploadCountElement) {
        uploadCountElement.textContent = uploadCount;
      }
      if (downloadCountElement) {
        downloadCountElement.textContent = downloadCount;
      }
      if (remoteExtraCountElement) {
        remoteExtraCountElement.textContent = remoteExtraCount;
      }
      if (localExtraCountElement) {
        localExtraCountElement.textContent = localExtraCount;
      }
      if (providerElement) {
        providerElement.textContent = data.provider_label || "未知图床";
      }
      if (remoteImageCountElement) {
        remoteImageCountElement.textContent = remoteImageCount;
      }
      if (remoteStorageSizeElement) {
        remoteStorageSizeElement.textContent = remoteStorageText;
      }

      if (showAlert) {
        showToast(
          `${data.provider_label || "图床"}：云端 ${remoteImageCount} 张，待上传 ${uploadCount} 个，待下载 ${downloadCount} 个，云端多出 ${remoteExtraCount} 个。`,
          "info",
          "图床状态已刷新",
        );
      }
    } catch (error) {
      console.error("检查图床同步状态失败:", error);
      if (uploadCountElement) {
        uploadCountElement.textContent = "--";
      }
      if (downloadCountElement) {
        downloadCountElement.textContent = "--";
      }
      if (remoteExtraCountElement) {
        remoteExtraCountElement.textContent = "--";
      }
      if (localExtraCountElement) {
        localExtraCountElement.textContent = "--";
      }
      if (providerElement) {
        providerElement.textContent = "--";
      }
      if (remoteImageCountElement) {
        remoteImageCountElement.textContent = "--";
      }
      if (remoteStorageSizeElement) {
        remoteStorageSizeElement.textContent = "--";
      }
      if (showAlert) {
        showToast(error.message, "error", "检查失败");
      }
    }
  }
}

initApp();
