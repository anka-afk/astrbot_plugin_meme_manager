document.addEventListener("DOMContentLoaded", () => {
  const selectionState = {
    enabled: false,
    items: new Map(),
  };
  let latestEmojiData = {};
  let dangerConfirmResolver = null;
  let dangerConfirmStage = "ack";
  let dangerConfirmTimer = null;
  let dangerConfirmConfig = null;

  const toggleSelectionModeBtn = document.getElementById(
    "toggle-selection-mode-btn"
  );
  const batchMoveBtn = document.getElementById("batch-move-btn");
  const batchDeleteBtn = document.getElementById("batch-delete-btn");
  const clearAllBtn = document.getElementById("clear-all-btn");
  const selectionSummary = document.getElementById("selection-summary");
  const toastContainer = document.getElementById("toast-container");
  const batchContextMenu = document.getElementById("batch-context-menu");
  const batchContextMenuTitle = document.getElementById(
    "batch-context-menu-title"
  );
  const batchContextMenuSubtitle = document.getElementById(
    "batch-context-menu-subtitle"
  );
  const contextMenuDeleteBtn = document.getElementById(
    "context-menu-delete-btn"
  );
  const contextMenuMoveBtn = document.getElementById("context-menu-move-btn");
  const contextMenuCopyBtn = document.getElementById("context-menu-copy-btn");
  const contextMenuPasteBtn = document.getElementById("context-menu-paste-btn");
  const sidebarToggleBtn = document.getElementById("sidebar-toggle-btn");
  const sidebarCloseBtn = document.getElementById("sidebar-close-btn");
  const sidebarBackdrop = document.getElementById("sidebar-backdrop");
  const leftPanel = document.getElementById("app-sidebar-panel");
  const dragHud = document.getElementById("drag-hud");
  const dragHudLabel = document.getElementById("drag-hud-label");
  const dragHudCaption = document.getElementById("drag-hud-caption");
  const moveTargetModalRoot = document.getElementById("move-target-modal");
  const moveTargetModalTitle = document.getElementById("move-target-modal-title");
  const moveTargetModalDescription = document.getElementById(
    "move-target-modal-description"
  );
  const moveTargetList = document.getElementById("move-target-list");
  const moveTargetCancelBtn = document.getElementById("move-target-cancel-btn");
  const categoryEditModalRoot = document.getElementById("category-edit-modal");
  const categoryEditModalTitle = document.getElementById(
    "category-edit-modal-title"
  );
  const categoryEditModalDescription = document.getElementById(
    "category-edit-modal-description"
  );
  const categoryEditNameInput = document.getElementById(
    "category-edit-name-input"
  );
  const categoryEditDescInput = document.getElementById(
    "category-edit-desc-input"
  );
  const categoryEditCancelBtn = document.getElementById(
    "category-edit-cancel-btn"
  );
  const categoryEditSaveBtn = document.getElementById("category-edit-save-btn");
  const confirmModalRoot = document.getElementById("confirm-modal");
  const confirmModalTitle = document.getElementById("confirm-modal-title");
  const confirmModalDescription = document.getElementById(
    "confirm-modal-description"
  );
  const confirmModalCancelBtn = document.getElementById(
    "confirm-modal-cancel-btn"
  );
  const confirmModalConfirmBtn = document.getElementById(
    "confirm-modal-confirm-btn"
  );
  const dangerModalRoot = document.getElementById("danger-confirm-modal");
  const dangerModalTitle = document.getElementById("danger-modal-title");
  const dangerModalDescription = document.getElementById(
    "danger-modal-description"
  );
  const dangerModalStageText = document.getElementById(
    "danger-modal-stage-text"
  );
  const dangerModalAcknowledge = document.getElementById("danger-modal-ack");
  const dangerModalCancelBtn = document.getElementById(
    "danger-modal-cancel-btn"
  );
  const dangerModalConfirmBtn = document.getElementById(
    "danger-modal-confirm-btn"
  );
  let confirmResolver = null;
  const MOBILE_LAYOUT_MEDIA = "(max-width: 960px)";
  const DRAG_HUD_OFFSET_X = 18;
  const DRAG_HUD_OFFSET_Y = 88;
  const LONG_PRESS_DURATION_MS = 3000;
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
  const requestState = {
    emojis: { controller: null, seq: 0 },
    syncStatus: { controller: null, seq: 0 },
    imgHostStatus: { controller: null, seq: 0 },
  };
  let initialStatusTimerId = null;
  let activeCategoryEdit = null;
  let pendingMoveTargetItems = [];

  // 获取表情包数据和描述
  async function fetchEmojis() {
    const requestToken = startLatestRequest("emojis");
    try {
      const [emojiResponse, tagDescriptions] = await Promise.all([
        fetch("/api/emoji", { signal: requestToken.controller.signal }).then((res) => {
          if (!res.ok) throw new Error("获取表情包数据失败");
          return res.json();
        }),
        fetch("/api/emotions", {
          signal: requestToken.controller.signal,
        }).then((res) => {
          if (!res.ok) throw new Error("获取标签描述失败");
          return res.json();
        }),
      ]);
      if (!isLatestRequest("emojis", requestToken)) {
        return;
      }
      clearDragMode();
      closeBatchContextMenu();
      latestEmojiData = emojiResponse;
      pruneSelectionState();
      displayCategories(emojiResponse, tagDescriptions);
      updateSidebar(emojiResponse, tagDescriptions);
      updateSelectionUI();
    } catch (error) {
      if (error.name === "AbortError") {
        return;
      }
      console.error("加载表情包数据失败", error);
    } finally {
      finishLatestRequest("emojis", requestToken);
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
      confirmClassName.includes("danger")
    );
    confirmModalRoot.classList.remove("hidden");
    confirmModalRoot.setAttribute("aria-hidden", "false");

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
    { defaultErrorMessage = "请求失败" } = {}
  ) {
    const response = await fetch(url, options);
    const payload = await parseResponsePayload(response).catch(() => ({}));

    if (!response.ok) {
      const error = new Error(payload.message || payload.error || defaultErrorMessage);
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
      await fetchEmojis();
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

  function startLatestRequest(key) {
    const state = requestState[key];
    state.controller?.abort();
    state.seq += 1;
    state.controller = new AbortController();
    return {
      seq: state.seq,
      controller: state.controller,
    };
  }

  function isLatestRequest(key, requestToken) {
    const state = requestState[key];
    return state.seq === requestToken.seq && state.controller === requestToken.controller;
  }

  function finishLatestRequest(key, requestToken) {
    const state = requestState[key];
    if (state.controller === requestToken.controller) {
      state.controller = null;
    }
  }

  function cancelAllPendingRequests() {
    Object.values(requestState).forEach((state) => {
      state.controller?.abort();
      state.controller = null;
    });
    if (initialStatusTimerId) {
      clearTimeout(initialStatusTimerId);
      initialStatusTimerId = null;
    }
  }

  function isCompactViewport() {
    return window.matchMedia(MOBILE_LAYOUT_MEDIA).matches;
  }

  function updateSidebarToggleState() {
    const sidebarExpanded = isCompactViewport()
      ? document.body.classList.contains("sidebar-open")
      : !document.body.classList.contains("sidebar-collapsed");

    if (sidebarToggleBtn) {
      sidebarToggleBtn.setAttribute("aria-expanded", String(sidebarExpanded));
      sidebarToggleBtn.setAttribute(
        "aria-label",
        sidebarExpanded ? "收起侧边栏" : "展开侧边栏"
      );
    }

    if (sidebarBackdrop) {
      sidebarBackdrop.classList.toggle(
        "hidden",
        !(isCompactViewport() && sidebarExpanded)
      );
      sidebarBackdrop.setAttribute(
        "aria-hidden",
        String(!(isCompactViewport() && sidebarExpanded))
      );
    }

    if (leftPanel) {
      leftPanel.setAttribute("aria-hidden", String(!sidebarExpanded));
    }
  }

  function openSidebar() {
    if (!isCompactViewport()) {
      return;
    }
    document.body.classList.add("sidebar-open");
    updateSidebarToggleState();
  }

  function closeSidebar() {
    document.body.classList.remove("sidebar-open");
    updateSidebarToggleState();
  }

  function syncSidebarLayout() {
    if (isCompactViewport()) {
      document.body.classList.remove("sidebar-collapsed");
      closeSidebar();
      return;
    }

    document.body.classList.remove("sidebar-open");
    document.body.classList.remove("sidebar-collapsed");
    updateSidebarToggleState();
  }

  function toggleSidebar() {
    if (!isCompactViewport()) {
      return;
    }

    if (document.body.classList.contains("sidebar-open")) {
      closeSidebar();
    } else {
      openSidebar();
    }
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
      left.localeCompare(right, "zh-CN")
    );
  }

  function getMoveableCountForTarget(items, targetCategory) {
    if (!targetCategory) {
      return 0;
    }

    return dedupeEmojiItems(items).filter((item) => item.category !== targetCategory)
      .length;
  }

  function getAvailableMoveTargets(items = Array.from(selectionState.items.values())) {
    const uniqueItems = dedupeEmojiItems(items);
    if (uniqueItems.length === 0) {
      return [];
    }

    return getSortedCategories().filter(
      (category) => getMoveableCountForTarget(uniqueItems, category) > 0
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
    if (selectionState.enabled && isEmojiSelected(targetCategory, targetEmoji)) {
      return dedupeEmojiItems(Array.from(selectionState.items.values()));
    }

    return [{ category: targetCategory, emoji: targetEmoji }];
  }

  function getPasteableClipboardItems(targetCategory) {
    if (!targetCategory) {
      return [];
    }

    return getClipboardItems().filter((item) => item.category !== targetCategory);
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
        targetItems.length > 0 ? `批量管理 ${targetItems.length} 个文件` : "批量管理";
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
        targetItems.length === 0 || getAvailableMoveTargets(targetItems).length === 0;
    }
    if (contextMenuCopyBtn) {
      contextMenuCopyBtn.disabled = targetItems.length === 0;
    }
    if (contextMenuPasteBtn) {
      contextMenuPasteBtn.disabled = pasteableItems.length === 0 || !targetCategory;
    }

    batchContextMenu.classList.remove("hidden");
    batchContextMenu.setAttribute("aria-hidden", "false");

    requestAnimationFrame(() => {
      const menuWidth = batchContextMenu.offsetWidth || 240;
      const menuHeight = batchContextMenu.offsetHeight || 220;
      const left = Math.min(
        window.innerWidth - menuWidth - 12,
        Math.max(12, event.clientX)
      );
      const top = Math.min(
        window.innerHeight - menuHeight - 12,
        Math.max(12, event.clientY)
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
        event.target.closest(".category")
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
        dragModeState.items.length > 0
    );
  }

  function syncInteractionGuardState() {
    document.body.classList.toggle("drag-session-active", hasActiveDragInteraction());
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
      Math.max(10, clientX + DRAG_HUD_OFFSET_X)
    );
    const y = Math.min(
      window.innerHeight - hudHeight - 10,
      Math.max(10, clientY - DRAG_HUD_OFFSET_Y)
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
        (dragModeState.lastClientY - bottomThreshold) * 0.18
      );
    }

    if (deltaY !== 0) {
      window.scrollBy({ top: deltaY, behavior: "auto" });
      updateActiveDropTarget(
        dragModeState.lastClientX,
        dragModeState.lastClientY
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
    dragHud.style.setProperty("--drag-hud-progress", `${safeProgress * 360}deg`);
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

    if (targetCategory && hasMoveableItemsForTarget(dragItems, targetCategory)) {
      await moveEmojiItemsToCategory(targetCategory, dragItems);
      return;
    }

    if (wasDragging) {
      clearDragMode();
      showToast(
        "未拖到有效分类，已取消本次移动。",
        "warning",
        "拖拽未完成"
      );
      return;
    }

    if (event.pointerType !== "mouse" && dragItems.length > 0) {
      showToast(
        "拖拽模式已开启，继续拖到目标分类即可移动。",
        "info",
        "等待拖拽"
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
        dragModeState.captureElement.releasePointerCapture(dragModeState.pointerId);
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
      dragItems.map(({ category, emoji }) => createSelectionKey(category, emoji))
    );

    document.querySelectorAll(".emoji-item").forEach((emojiItem) => {
      const emojiKey = createSelectionKey(
        emojiItem.dataset.category,
        emojiItem.dataset.emoji
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
          dragModeState.captureElement.setPointerCapture(dragModeState.pointerId);
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
      showToast("拖拽模式已自动退出，请重新长按进入。", "info", "拖拽模式已结束");
    }, DRAG_READY_TIMEOUT_MS);

    showToast(
      dragItems.length > 1
        ? `已进入拖拽模式，可拖动这 ${dragItems.length} 个表情包到目标分类。`
        : "已进入拖拽模式，可将表情包拖到目标分类。",
      "success",
      "拖拽模式已开启"
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
    setLongPressProgress(
      0,
      `${Math.ceil(LONG_PRESS_DURATION_MS / 1000)}s`
    );

    longPressState.intervalId = window.setInterval(() => {
      if (!longPressState.emojiItem) {
        return;
      }

      const elapsed = performance.now() - longPressState.startTime;
      const progress = elapsed / LONG_PRESS_DURATION_MS;
      const remainingSeconds = Math.max(
        1,
        Math.ceil((LONG_PRESS_DURATION_MS - elapsed) / 1000)
      );
      setLongPressProgress(
        progress,
        `${remainingSeconds}s`
      );
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
      const rawPayload = event.dataTransfer?.getData("application/x-meme-emoji");
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
      (item) => item.category !== targetCategory
    );
  }

  function clearCategoryDropHighlights() {
    document.querySelectorAll(".category-drop-active").forEach((categoryDiv) => {
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
      const uploadProgress = uploadBlock.querySelector(".emoji-upload-progress");
      const uploadProgressBar = uploadBlock.querySelector(
        ".emoji-upload-progress-bar"
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
        uploadHint.textContent = "点击上传图片，或将表情长按 3 秒后拖到这里";
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
        "文件类型不支持"
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
        "已自动去重"
      );
    }

    if (uniqueFiles.length === 0) {
      return;
    }

    if (isCategoryUploading(category)) {
      showToast(
        `分类 ${category} 正在上传文件，请等待当前批次完成。`,
        "info",
        "上传进行中"
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
      2200
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
        "上传成功"
      );
      return;
    }

    if (uploadState.completed > 0 && uploadState.failed === 0) {
      showToast(
        `上传完成，新增 ${uploadState.completed} 个，跳过重复 ${uploadState.duplicates} 个。`,
        "warning",
        "上传已去重",
        4500
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
        4500
      );
      return;
    }

    if (uploadState.completed > 0) {
      showToast(
        `上传完成，成功 ${uploadState.completed} 个，重复 ${uploadState.duplicates} 个，失败 ${uploadState.failed} 个。`,
        "warning",
        "部分上传失败",
        4500
      );
      return;
    }

    const firstErrorMessage =
      failedUploads[0]?.error?.message || "服务器返回错误";
    showToast(
      `本次上传全部失败：${firstErrorMessage}`,
      "error",
      "上传失败",
      4500
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
      `上传 ${category} 分类表情包，支持点击选择或拖拽图片`
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
    uploadHint.textContent = "点击上传图片，或将表情长按 3 秒后拖到这里";

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
          "上传进行中"
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
            "上传进行中"
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
          "上传进行中"
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
      if (!selectionState.enabled) return;
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
  }

  function openMoveTargetModal(items = Array.from(selectionState.items.values())) {
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
      (item) => item.category !== targetCategory
    );
    if (moveableItems.length === 0) {
      showToast(
        "当前选择的表情包已经都在目标分类中。",
        "warning",
        "无需移动"
      );
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
        const data = await requestJson(
          "/api/emoji/batch_move",
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              source_category: sourceCategory,
              target_category: targetCategory,
              image_files: imageFiles,
            }),
          },
          { defaultErrorMessage: `移动失败：${sourceCategory}` }
        );

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
      showToast(
        messageParts.join("\n"),
        "warning",
        "移动部分完成",
        5600
      );
      return;
    }

    showToast(
      `已移动 ${movedCount} 个表情包到 ${targetCategory}`,
      "success",
      "移动成功"
    );
  }

  async function copyEmojiItemsToCategory(targetCategory, items) {
    if (!targetCategory) {
      showToast("请先选择要粘贴到的分类。", "warning", "缺少目标分类");
      return;
    }

    const pasteableItems = dedupeEmojiItems(items).filter(
      (item) => item.category !== targetCategory
    );

    if (pasteableItems.length === 0) {
      showToast(
        "当前没有可粘贴到该分类的文件。",
        "warning",
        "无需粘贴"
      );
      return;
    }

    const groupedItems = groupEmojiItemsByCategory(pasteableItems);
    let copiedCount = 0;
    const conflictFiles = [];
    const missingFiles = [];
    const requestErrors = [];

    for (const [sourceCategory, imageFiles] of groupedItems.entries()) {
      try {
        const data = await requestJson(
          "/api/emoji/batch_copy",
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              source_category: sourceCategory,
              target_category: targetCategory,
              image_files: imageFiles,
            }),
          },
          { defaultErrorMessage: `复制失败：${sourceCategory}` }
        );

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
      showToast(
        messageParts.join("\n"),
        "warning",
        "粘贴部分完成",
        5600
      );
      return;
    }

    showToast(
      `已粘贴 ${copiedCount} 个表情包到 ${targetCategory}`,
      "success",
      "粘贴成功"
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

  async function waitForSyncCompletion() {
    while (true) {
      const status = await requestJson("/api/img_host/sync/check_process", {}, {
        defaultErrorMessage: "检查同步状态失败",
      });

      if (status.completed) {
        if (!status.success) {
          throw new Error("同步失败");
        }
        return status;
      }

      await sleep(1000);
    }
  }

  // 根据数据生成 DOM 节点，展示每个分类及其表情包，并添加上传块
  function displayCategories(emojiData, tagDescriptions) {
    const container = document.getElementById("emoji-categories");
    container.innerHTML = "";

    Object.entries(emojiData).forEach(([category, emojis]) => {
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
          emojiItem.tabIndex = 0;

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

          // 使用 data-bg 存储图片URL
          emojiItem.setAttribute("data-bg", `/memes/${category}/${emoji}`);
          emojiGrid.appendChild(emojiItem);
        });
      }

      const { uploadBlock, fileInput } = createUploadDropzone(category);

      // 将文件输入框和上传块添加到表情包网格中
      emojiGrid.appendChild(uploadBlock);
      emojiGrid.appendChild(fileInput);

      categoryDiv.appendChild(emojiGrid);
      attachCategoryDropTarget(categoryDiv, category);
      container.appendChild(categoryDiv);
    });

    // 懒加载背景图片
    const lazyBackgrounds = document.querySelectorAll(".emoji-item");
    const observer = new IntersectionObserver(
      (entries, observer) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            const emojiItem = entry.target;
            const bgUrl = emojiItem.getAttribute("data-bg");
            emojiItem.style.backgroundImage = `url('${bgUrl}')`; // 加载背景图片
            emojiItem.removeAttribute("data-bg"); // 移除临时属性
            observer.unobserve(emojiItem); // 停止观察
          }
        });
      },
      { threshold: 0.1 }
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
          closeSidebar();
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
      const selectionIndicator = emojiItem.querySelector(".selection-indicator");

      emojiItem.classList.toggle("selection-mode", selectionState.enabled);
      emojiItem.classList.toggle("selected", selected);
      if (selectionIndicator) {
        selectionIndicator.classList.toggle("checked", selected);
        selectionIndicator.setAttribute(
          "aria-label",
          selected ? "已选中" : "未选择"
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
      const selectAllBtn = categoryDiv.querySelector(".select-all-category-btn");
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

    const allSelected = emojis.every((emoji) => isEmojiSelected(category, emoji));
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
      "已复制到批量剪贴板"
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
      dangerModalStageText.textContent =
        `安全等待中，还需 ${remaining} 秒，倒计时结束后才可执行。`;
    }
    if (dangerModalConfirmBtn) {
      dangerModalConfirmBtn.disabled = true;
      dangerModalConfirmBtn.textContent = `等待 ${remaining} 秒`;
    }

    dangerConfirmTimer = setInterval(() => {
      remaining -= 1;
      if (remaining > 0) {
        dangerModalStageText.textContent =
          `安全等待中，还需 ${remaining} 秒，倒计时结束后才可执行。`;
        dangerModalConfirmBtn.textContent = `等待 ${remaining} 秒`;
        return;
      }

      clearInterval(dangerConfirmTimer);
      dangerConfirmTimer = null;
      dangerConfirmStage = "ready";
      dangerModalStageText.textContent = "5 秒倒计时已结束，请点击下方按钮执行。";
      dangerModalConfirmBtn.disabled = false;
      dangerModalConfirmBtn.textContent = dangerConfirmConfig.actionLabel;
    }, 1000);
  }

  function showDangerConfirm({ title, description, actionLabel, countdown = 5 }) {
    if (
      !dangerModalRoot ||
      !dangerModalTitle ||
      !dangerModalDescription ||
      !dangerModalConfirmBtn
    ) {
      return Promise.resolve(
        confirm(`${title}\n\n${description}\n\n确认要继续执行吗？`)
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
    dangerModalRoot.classList.remove("hidden");
    dangerModalRoot.setAttribute("aria-hidden", "false");

    return new Promise((resolve) => {
      dangerConfirmResolver = resolve;
    });
  }

  // 上传表情包
  async function uploadEmoji(category, file) {
    const formData = new FormData();
    formData.append("category", category);
    formData.append("image_file", file);

    return requestJson(
      "/api/emoji/add",
      {
        method: "POST",
        body: formData,
      },
      { defaultErrorMessage: "上传失败，服务器返回错误" }
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
      const data = await requestJson(
        "/api/emoji/delete",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ category, image_file: emoji }),
        },
        { defaultErrorMessage: "删除表情包失败" }
      );
      selectionState.items.delete(createSelectionKey(category, emoji));
      await refreshUi({ emojis: true });
      showToast(
        `已从 ${data.category} 删除 ${data.filename}`,
        "success",
        "删除成功"
      );
    } catch (error) {
      console.error("删除表情包失败", error);
      showToast(`删除表情包失败：${error.message}`, "error", "删除失败", 4500);
    }
  }

  async function deleteEmojiItems(
    items,
    { useSelectionState = true, confirmMode = "normal" } = {}
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
        const data = await requestJson(
          "/api/emoji/batch_delete",
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ category, image_files: imageFiles }),
          },
          { defaultErrorMessage: `批量删除失败: ${category}` }
        );
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
        5200
      );
      return;
    }

    showToast(
      `已删除 ${deletedCount} 个表情包`,
      "success",
      "批量删除完成"
    );
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
      await requestJson(
        "/api/category/delete",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ category }),
        },
        { defaultErrorMessage: "删除分类失败" }
      );
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
        "无需清空"
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
      const data = await requestJson(
        "/api/category/clear",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ category }),
        },
        { defaultErrorMessage: "清空分类失败" }
      );
      clearSelections();
      await refreshUi({ emojis: true });
      showToast(
        `已清空分类 ${category}，删除 ${data.deleted_count} 个表情包。`,
        "success",
        "清空成功"
      );
    } catch (error) {
      console.error("清空分类失败:", error);
      showToast(`清空分类失败：${error.message}`, "error", "清空失败", 4500);
    }
  }

  async function clearAllEmojiFiles() {
    const totalEmojiCount = Object.values(latestEmojiData).reduce(
      (sum, emojis) => sum + (Array.isArray(emojis) ? emojis.length : 0),
      0
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
      const data = await requestJson(
        "/api/emoji/clear_all",
        {
          method: "POST",
        },
        { defaultErrorMessage: "清空全部表情包失败" }
      );
      clearSelections();
      await refreshUi({ emojis: true });
      showToast(
        `已清空全部表情包，共删除 ${data.deleted_count} 个文件，涉及 ${data.affected_categories} 个分类。`,
        "success",
        "清空成功",
        4200
      );
    } catch (error) {
      console.error("清空全部表情包失败:", error);
      showToast(
        `清空全部表情包失败：${error.message}`,
        "error",
        "清空失败",
        4500
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

  if (sidebarToggleBtn) {
    sidebarToggleBtn.addEventListener("click", () => {
      toggleSidebar();
    });
  }

  if (sidebarCloseBtn) {
    sidebarCloseBtn.addEventListener("click", () => {
      closeSidebar();
    });
  }

  if (sidebarBackdrop) {
    sidebarBackdrop.addEventListener("click", () => {
      closeSidebar();
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
        Math.ceil((LONG_PRESS_DURATION_MS - elapsed) / 1000)
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
    { passive: false }
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
    true
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
      const isBatchContextMenuOpen = !batchContextMenu.classList.contains("hidden");
      if (isBatchContextMenuOpen) {
        closeBatchContextMenu();
        return;
      }
    }
    if (event.key === "Escape" && isCompactViewport()) {
      const isSidebarOpen = document.body.classList.contains("sidebar-open");
      if (isSidebarOpen) {
        closeSidebar();
        return;
      }
    }
    if (event.key === "Escape" && moveTargetModalRoot) {
      const isMoveTargetOpen = !moveTargetModalRoot.classList.contains("hidden");
      if (isMoveTargetOpen) {
        closeMoveTargetModal();
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
        await requestJson(
          "/api/category/restore",
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
            },
            body: JSON.stringify({
              category: categoryName,
              description: categoryDesc,
            }),
          },
          { defaultErrorMessage: "添加类别失败" }
        );

        document.getElementById("new-category-name").value = "";
        document.getElementById("new-category-description").value = "";
        document.getElementById("add-category-form").style.display = "none";
        document.getElementById("add-category-btn").style.display = "block";
        await refreshUi({ emojis: true, syncStatus: true });
        showToast(
          `类别「${categoryName}」已添加。`,
          "success",
          "添加成功"
        );
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
      payload && typeof payload.differences === "object" && payload.differences !== null
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
            })
        )
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
              })
            );
            actions.appendChild(
              createButton({
                className: "remove-btn",
                text: "从配置中删除",
                onClick: () => removeFromConfig(category),
              })
            );
            return actions;
          }
        )
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
      })
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
      })
    );
  }

  // 检查同步状态的函数
  async function checkSyncStatus(showAlert = true) {
    const statusDiv = document.getElementById("sync-status");
    if (!statusDiv) return;

    const btn = document.getElementById("check-sync-btn");
    setButtonBusy(btn, "正在检查中...");
    const requestToken = startLatestRequest("syncStatus");

    try {
      const data = await requestJson(
        "/api/sync/status",
        { signal: requestToken.controller.signal },
        {
          defaultErrorMessage: "检查同步状态失败",
        }
      );
      if (!isLatestRequest("syncStatus", requestToken)) {
        return;
      }
      if (data.status === "error") throw new Error(data.message);

      const differences = normalizeSyncDifferences(data);
      renderSyncStatus(statusDiv, differences);

      if (showAlert) {
        showToast("配置状态已刷新。", "success", "检查完成");
      }
    } catch (error) {
      if (error.name === "AbortError") {
        return;
      }
      console.error("检查同步状态失败:", error);
      if (!isLatestRequest("syncStatus", requestToken)) {
        return;
      }
      renderSyncStatusError(statusDiv, error.message);
      if (showAlert) {
        showToast(error.message, "error", "检查失败");
      }
    } finally {
      finishLatestRequest("syncStatus", requestToken);
      if (isLatestRequest("syncStatus", requestToken) || !requestState.syncStatus.controller) {
        restoreButton(btn);
      }
    }
  }

  async function syncToRemote() {
    const btn = document.getElementById("upload-sync-btn");
    try {
      setButtonBusy(btn, "同步中...");

      await requestJson(
        "/api/img_host/sync/upload",
        {
          method: "POST",
        },
        { defaultErrorMessage: "同步到云端失败" }
      );
      await waitForSyncCompletion();
      await refreshUi({ syncStatus: true, imgHostStatus: true });
      showToast("云端上传同步已完成。", "success", "同步成功");
    } catch (error) {
      console.error("同步到云端失败:", error);
      showToast(error.message, "error", "同步失败");
    } finally {
      restoreButton(btn);
    }
  }

  async function syncFromRemote() {
    const btn = document.getElementById("download-sync-btn");
    try {
      setButtonBusy(btn, "同步中...");

      await requestJson(
        "/api/img_host/sync/download",
        {
          method: "POST",
        },
        { defaultErrorMessage: "从云端同步失败" }
      );
      await waitForSyncCompletion();
      await refreshUi({ emojis: true, syncStatus: true, imgHostStatus: true });
      showToast("云端下载同步已完成。", "success", "同步成功");
    } catch (error) {
      console.error("从云端同步失败:", error);
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
    .getElementById("download-sync-btn")
    .addEventListener("click", syncFromRemote);

  // 同步配置的函数
  async function syncConfig() {
    try {
      await requestJson(
        "/api/sync/config",
        {
          method: "POST",
        },
        { defaultErrorMessage: "同步配置失败" }
      );
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
      const data = await requestJson(
        "/api/category/restore",
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ category }),
        },
        { defaultErrorMessage: "恢复类别失败" }
      );

      await refreshUi({ emojis: true, syncStatus: true });
      showToast(
        `类别「${category}」已恢复。\n描述：${data.description || "请补充描述"}`,
        "success",
        "恢复成功"
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
      await requestJson(
        "/api/category/remove_from_config",
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ category }),
        },
        { defaultErrorMessage: "从配置中删除类别失败" }
      );

      await refreshUi({ syncStatus: true });
      showToast(
        `类别「${category}」已从配置中移除。`,
        "success",
        "移除成功"
      );
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
        await requestJson(
          "/api/category/rename",
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ old_name: oldName, new_name: newName }),
          },
          { defaultErrorMessage: "重命名类别失败" }
        );
      }

      await requestJson(
        "/api/category/update_description",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ tag: newName, description: newDesc }),
        },
        { defaultErrorMessage: "更新描述失败" }
      );

      await refreshUi({ emojis: true, syncStatus: true });
      closeCategoryEditModal();
      showToast(
        `类别「${newName}」已保存。`,
        "success",
        "保存成功"
      );
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
  updateSidebarToggleState();
  window.addEventListener("resize", () => {
    syncSidebarLayout();
    closeBatchContextMenu();
  });
  window.addEventListener("beforeunload", () => {
    cancelAllPendingRequests();
  });
  void (async () => {
    await fetchEmojis();
    initialStatusTimerId = window.setTimeout(() => {
      initialStatusTimerId = null;
      void checkSyncStatus(false);
      void checkImgHostSyncStatus(false);
    }, 180);
  })();

  // 检查图床同步状态
  async function checkImgHostSyncStatus(showAlert = true) {
    const uploadCountElement = document.getElementById("upload-count");
    const downloadCountElement = document.getElementById("download-count");
    const providerElement = document.getElementById("img-host-provider");
    const remoteImageCountElement = document.getElementById("remote-image-count");
    const remoteStorageSizeElement =
      document.getElementById("remote-storage-size");

    const requestToken = startLatestRequest("imgHostStatus");
    try {
      const data = await requestJson(
        "/api/img_host/sync/status",
        { signal: requestToken.controller.signal },
        {
          defaultErrorMessage: "获取图床同步状态失败",
        }
      );
      if (!isLatestRequest("imgHostStatus", requestToken)) {
        return;
      }

      const uploadCount = data.upload_count ?? data.to_upload?.length ?? 0;
      const downloadCount = data.download_count ?? data.to_download?.length ?? 0;
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
          `${data.provider_label || "图床"}：云端 ${remoteImageCount} 张，待上传 ${uploadCount} 个，待下载 ${downloadCount} 个。`,
          "info",
          "图床状态已刷新"
        );
      }
    } catch (error) {
      if (error.name === "AbortError") {
        return;
      }
      console.error("检查图床同步状态失败:", error);
      if (!isLatestRequest("imgHostStatus", requestToken)) {
        return;
      }
      if (uploadCountElement) {
        uploadCountElement.textContent = "--";
      }
      if (downloadCountElement) {
        downloadCountElement.textContent = "--";
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
    } finally {
      finishLatestRequest("imgHostStatus", requestToken);
    }
  }
});
