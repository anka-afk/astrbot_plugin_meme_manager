async function initSemanticPage() {
  const dialogMask = document.querySelector("#dialog-mask");
  const dialogTitle = document.querySelector("#dialog-title");
  const dialogMessage = document.querySelector("#dialog-message");
  const dialogCancel = document.querySelector("#dialog-cancel");
  const dialogConfirm = document.querySelector("#dialog-confirm");
  const toastContainer = document.querySelector("#toast-container");
  const notice = document.querySelector("#notice");
  const otherTasksWarning = document.querySelector("#other-tasks-warning");
  const packSelect = document.querySelector("#pack");
  const statusBox = document.querySelector("#status");
  const recordsBox = document.querySelector("#items");
  const recordCount = document.querySelector("#record-count");
  const recordsPrev = document.querySelector("#records-prev");
  const recordsNext = document.querySelector("#records-next");
  const recordsPage = document.querySelector("#records-page");
  const recordsFilter = document.querySelector("#records-filter");
  const concurrencyInput = document.querySelector("#concurrency");
  const concurrencyHint = document.querySelector("#concurrency-hint");
  const taskTimer = document.querySelector("#task-timer");
  const imagePreviewMask = document.querySelector("#image-preview-mask");
  const imagePreviewTitle = document.querySelector("#image-preview-title");
  const imagePreviewClose = document.querySelector("#image-preview-close");
  const imagePreviewImg = document.querySelector("#image-preview-img");
  const imagePreviewLoading = document.querySelector("#image-preview-loading");
  const buttons = Array.from(document.querySelectorAll("button[data-action]"));
  let requestRunning = false;
  let embeddingReady = false;
  let visionReady = false;
  let captionComplete = false;
  let dialogResolver = null;
  let toastTimer = null;
  let modalErrorOpen = false;
  let lastModalError = "";
  let recordsCurrentPage = 1;
  const recordsPageSize = 20;
  let recordsTotalPages = 1;
  let recordsStatus = "all";
  let elapsedSeconds = 0;
  let timerRunning = false;
  let timerUpdatedAt = Date.now();
  let concurrencyDirty = false;
  let latestStatus = null;
  let statusRequestSequence = 0;
  const previewCache = new Map();
  const previewRequests = new Map();
  let activePreviewKey = "";
  let activePreviewRequests = 0;
  const previewQueue = [];

  function setDialog(
    open,
    title = "",
    message = "",
    confirmText = "确定",
    showCancel = true,
  ) {
    dialogTitle.textContent = title;
    dialogMessage.textContent = message;
    dialogConfirm.textContent = confirmText;
    dialogCancel.hidden = !showCancel;
    dialogMask.classList.toggle("hidden", !open);
    dialogMask.setAttribute("aria-hidden", open ? "false" : "true");
  }

  function closeDialog(value) {
    setDialog(false);
    if (dialogResolver) {
      const resolve = dialogResolver;
      dialogResolver = null;
      resolve(value);
    }
  }

  function showDialog(title, message, options = {}) {
    return new Promise((resolve) => {
      dialogResolver = resolve;
      setDialog(
        true,
        title,
        message,
        options.confirmText || "确定",
        options.showCancel !== false,
      );
    });
  }

  async function confirmTwice({
    title,
    message,
    finalTitle = "再次确认",
    finalMessage,
    confirmText = "继续",
    finalConfirmText = "确认执行",
  }) {
    const first = await showDialog(title, message, { confirmText });
    if (!first) return false;
    return await showDialog(
      finalTitle,
      finalMessage || `这是最后一次确认。确定要执行“${title}”吗？`,
      { confirmText: finalConfirmText },
    );
  }

  function showToast(message, isError = false) {
    toastContainer.replaceChildren();
    const toast = document.createElement("div");
    toast.className = `toast${isError ? " error" : ""}`;
    toast.textContent = String(message || "");
    toastContainer.append(toast);
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(
      () => toastContainer.replaceChildren(),
      4200,
    );
  }

  function showNotice(message, isError = false) {
    notice.textContent = String(message || "");
    notice.classList.toggle("error", isError);
  }

  function errorMessage(error) {
    const message = error?.message || error;
    return String(message || "操作失败，请查看日志后重试");
  }

  async function reportError(title, error, showModal = true) {
    const message = errorMessage(error);
    showNotice(message, true);
    showToast(message, true);
    if (!showModal || modalErrorOpen || message === lastModalError) return;
    modalErrorOpen = true;
    lastModalError = message;
    try {
      await showDialog(title, message, { showCancel: false });
    } finally {
      modalErrorOpen = false;
    }
  }

  async function waitForBridgeReady(pageApi) {
    let timer = null;
    try {
      return await Promise.race([
        pageApi.ready(),
        new Promise((_, reject) => {
          timer = window.setTimeout(
            () =>
              reject(
                new Error("AstrBot 页面桥接未就绪，请从 WebUI 入口重新打开"),
              ),
            8000,
          );
        }),
      ]);
    } finally {
      window.clearTimeout(timer);
    }
  }

  function updateButtonState() {
    buttons.forEach((button) => {
      const action = button.dataset.action;
      const needsEmbedding =
        ["start", "retry", "index", "dimension", "force"].includes(action) ||
        (action === "resume" && latestStatus?.task_mode !== "caption_only");
      const needsCompleteCaption = ["index", "dimension"].includes(action);
      const needsVision =
        ["start", "resume", "retry", "force"].includes(action) &&
        !captionComplete;
      const allowedByTaskState =
        !latestStatus ||
        {
          start: latestStatus.can_start,
          force: latestStatus.can_start,
          resume: latestStatus.can_resume,
          retry: latestStatus.can_retry,
          pause: latestStatus.can_pause,
          index: latestStatus.can_rebuild_index,
          dimension: latestStatus.can_rebuild_index,
          clear: !latestStatus.external_operation,
          "delete-all": !latestStatus.external_operation,
        }[action] !== false;
      button.disabled =
        requestRunning ||
        !packSelect.value ||
        !allowedByTaskState ||
        (needsEmbedding && !embeddingReady) ||
        (needsCompleteCaption && !captionComplete) ||
        (needsVision && !visionReady);
      if (action === "pause" && latestStatus?.task_phase === "indexing") {
        button.title = "正在建立向量索引，这个收尾阶段不能暂停";
      } else if (action === "pause") {
        button.title = "立即中断本轮模型请求，未完成图片会退回等待队列";
      } else {
        button.removeAttribute("title");
      }
    });
    const queueIsActive = Boolean(latestStatus?.worker_alive);
    concurrencyInput.disabled = requestRunning || queueIsActive;
    concurrencyHint.textContent = queueIsActive
      ? `当前队列固定为 ${latestStatus?.concurrency || concurrencyInput.value || 1} 并发；如需调整，请先清空队列后重新开始。`
      : "同时提交给视觉模型的图片上限，建议按模型限流设置。";
  }

  function setBusy(value) {
    if (value) statusRequestSequence += 1;
    requestRunning = value;
    updateButtonState();
    packSelect.disabled = value;
  }

  function addMetric(label, value) {
    const metric = document.createElement("div");
    metric.className = "metric";
    const name = document.createElement("span");
    name.textContent = label;
    const content = document.createElement("strong");
    content.textContent = String(value ?? "-");
    metric.append(name, content);
    statusBox.append(metric);
  }

  function formatDuration(value) {
    const total = Math.max(0, Math.floor(Number(value) || 0));
    const hours = String(Math.floor(total / 3600)).padStart(2, "0");
    const minutes = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
    const seconds = String(total % 60).padStart(2, "0");
    return `${hours}:${minutes}:${seconds}`;
  }

  function updateTaskTimer() {
    if (timerRunning) {
      elapsedSeconds += Math.max(0, (Date.now() - timerUpdatedAt) / 1000);
    }
    timerUpdatedAt = Date.now();
    taskTimer.textContent = `用时 ${formatDuration(elapsedSeconds)}`;
  }

  function renderMetricGroup(title, metrics, open = true) {
    const group = document.createElement("details");
    group.className = "metric-group";
    group.open = open;
    const summary = document.createElement("summary");
    summary.textContent = title;
    const content = document.createElement("div");
    content.className = "metric-group-content";
    group.append(summary, content);
    metrics.forEach(([label, value]) => {
      const metric = document.createElement("div");
      metric.className = "metric";
      const name = document.createElement("span");
      name.textContent = label;
      const valueNode = document.createElement("strong");
      valueNode.textContent = String(value ?? "-");
      metric.append(name, valueNode);
      content.append(metric);
    });
    statusBox.append(group);
  }

  function renderStatus(data) {
    latestStatus = data;
    embeddingReady = Boolean(data.embedding_provider_ready);
    visionReady = Boolean(data.vision_provider_ready);
    captionComplete = Boolean(data.semantic_caption_complete);
    if (data.concurrency && !concurrencyDirty)
      concurrencyInput.value = String(data.concurrency);
    elapsedSeconds = Number(data.elapsed_seconds || 0);
    timerRunning = data.task_status === "running";
    timerUpdatedAt = Date.now();
    const previousOpen = new Map(
      Array.from(statusBox.querySelectorAll(".metric-group")).map((group) => [
        group.querySelector("summary")?.textContent,
        group.open,
      ]),
    );
    const groupOpen = (title, defaultOpen) =>
      previousOpen.has(title) ? previousOpen.get(title) : defaultOpen;
    let taskText =
      {
        idle: "空闲",
        running: "运行中",
        paused: "已暂停",
        completed: "已完成",
        completed_with_errors: "完成但有失败",
        failed: "任务失败",
      }[data.task_status] || data.task_status;
    if (data.task_status === "running" && !data.worker_alive)
      taskText = "已中断（可继续）";
    if (data.task_status === "paused" && data.active_request_count)
      taskText = "正在中断请求";
    const phaseText =
      {
        captioning: "生成图片描述",
        indexing: "建立向量索引",
        finished: "已结束",
        failed: "异常结束",
      }[data.task_phase] || "尚未开始";
    statusBox.replaceChildren();
    renderMetricGroup(
      "任务进度",
      [
        ["任务状态", taskText],
        ["当前阶段", phaseText],
        ["图片总数", data.total_tasks],
        ["模型请求中", data.active_request_count],
        ["排队待描述", data.queued_caption_tasks],
        ["描述完成", data.caption_done],
        ["自动重分类", data.reclassified_items || 0],
        ["处理失败", data.failed_tasks],
        ["并发上限", data.concurrency || 1],
      ],
      groupOpen("任务进度", true),
    );
    renderMetricGroup(
      "图片和描述",
      [
        ["文件", data.file_total],
        ["独立图片", data.unique_total],
        ["重复复用", data.reused_duplicate_files],
        ["描述完成", data.caption_done],
        ["描述失败", data.caption_failed],
      ],
      groupOpen("图片和描述", true),
    );
    renderMetricGroup(
      "向量处理",
      [
        ["向量完成", data.embedding_done],
        ["向量失败", data.embedding_failed],
        ["向量模型", data.embedding_provider_ready ? "已配置并可用" : "不可用"],
        [
          "实际向量模型",
          [data.embedding_provider_id, data.embedding_model]
            .filter(Boolean)
            .join(" / ") || "自动选择中",
        ],
        ["向量索引", data.index_ready ? "可用" : "未建立"],
        ["配置维度", data.embedding_configured_dimension || "未检测"],
        ["已校验维度", data.embedding_verified_dimension || "未校验"],
        ["索引维度", data.index_embedding_dimension || "未建立"],
      ],
      groupOpen("向量处理", false),
    );
    renderMetricGroup(
      "视觉模型和消耗",
      [
        ["视觉模型", data.vision_provider_ready ? "已配置并可用" : "不可用"],
        [
          "当前视觉模型",
          [data.vision_provider_id, data.vision_model]
            .filter(Boolean)
            .join(" / ") || "未选择",
        ],
        ["视觉调用次数", data.vision_calls],
        ["输入 Token", data.token_usage_input],
        ["输出 Token", data.token_usage_output],
        ["消耗 Token", data.token_usage_total],
      ],
      groupOpen("视觉模型和消耗", false),
    );
    renderMetricGroup(
      "其他状态",
      [
        [
          "任务队列",
          {
            external_operation: "其他文件任务运行中",
            cleared: "已清空",
            settling: "正在中断请求",
            paused: "已完全暂停",
            interrupted: "已中断，可继续",
            indexing: "描述完成，正在建索引",
            running: "正在处理",
            failed: "有失败项",
            waiting: "等待开始",
            done: "无待处理项",
            empty: "没有图片",
          }[data.queue_status] || "未知",
        ],
        ["待建立向量", data.queued_embedding_tasks],
        [
          "其他资源包任务",
          Array.isArray(data.other_active_tasks)
            ? data.other_active_tasks.length
            : 0,
        ],
        [
          "语义查询",
          data.semantic_enabled
            ? data.semantic_config_ready
              ? "已配置"
              : "未配置"
            : "未启用",
        ],
      ],
      groupOpen("其他状态", false),
    );
    otherTasksWarning.textContent = String(data.other_tasks_warning || "");
    if (data.last_error) {
      showNotice(data.last_error, true);
    } else if (["running", "paused"].includes(String(data.task_status || ""))) {
      showNotice(data.status_message || "任务状态已更新");
    } else if (data.semantic_enabled && data.semantic_config_ready === false) {
      showNotice(
        "语义查询开关已打开，但未配置 Embedding 向量模型；当前仍使用旧分类逻辑。请先在插件配置中选择 Embedding 模型。",
        true,
      );
    } else if (!embeddingReady) {
      showNotice(
        data.embedding_configured_provider_id
          ? `已配置 Embedding 模型「${data.embedding_configured_provider_id}」，但当前 Provider 不可用；请检查 Provider 是否启用或配置是否已加载。`
          : "尚未选择 Embedding 向量模型：完整语义化、建立索引和语义查询不可用；请先选择 Embedding 模型。",
        true,
      );
    } else if (
      data.dimension_rebuild_required &&
      !["running", "paused"].includes(String(data.task_status || ""))
    ) {
      showNotice(
        `已检测到 Embedding 模型「${[data.embedding_provider_id, data.embedding_model].filter(Boolean).join(" / ") || "当前模型"}」，但当前资源包还没有按此模型建立本机索引；请点击“按当前维度重建向量”。`,
        false,
      );
    } else if (!visionReady) {
      showNotice(
        "未配置视觉模型：一键完整语义化无法处理待生成描述的图片，请先选择视觉模型。",
        true,
      );
    } else {
      showNotice(data.status_message || "");
    }
    updateTaskTimer();
    updateButtonState();
  }

  function imageLocation(item) {
    const parts = String(item?.relative_path || "")
      .replace(/\\/g, "/")
      .split("/")
      .filter(Boolean);
    if (parts[0] === "memes") parts.shift();
    const filename = parts.pop() || "";
    return { category: parts.join("/"), filename };
  }

  function previewKey(item) {
    return `${packSelect.value}:${String(item?.relative_path || "")}`;
  }

  function rememberPreview(key, dataUrl) {
    previewCache.delete(key);
    previewCache.set(key, dataUrl);
    while (previewCache.size > 24) {
      previewCache.delete(previewCache.keys().next().value);
    }
  }

  function pumpPreviewQueue() {
    while (activePreviewRequests < 4 && previewQueue.length) {
      const job = previewQueue.shift();
      activePreviewRequests += 1;
      Promise.resolve()
        .then(job.task)
        .then(job.resolve, job.reject)
        .finally(() => {
          activePreviewRequests -= 1;
          pumpPreviewQueue();
        });
    }
  }

  function schedulePreviewRequest(task, priority = false) {
    return new Promise((resolve, reject) => {
      const job = { task, resolve, reject };
      if (priority) previewQueue.unshift(job);
      else previewQueue.push(job);
      pumpPreviewQueue();
    });
  }

  async function loadRecordImage(item, size = "preview") {
    const key = `${previewKey(item)}:${size}`;
    if (size === "preview" && previewCache.has(key))
      return previewCache.get(key);
    if (previewRequests.has(key)) return await previewRequests.get(key);
    const { category, filename } = imageLocation(item);
    if (!category || !filename) throw new Error("图片路径不可用");
    const requestPromise = schedulePreviewRequest(
      () =>
        apiGet("meme_image_data", {
          managed_pack_id: packSelect.value,
          category,
          filename,
          size,
        }),
      size === "original",
    )
      .then((data) => {
        if (!data?.data_url) throw new Error("图片接口未返回预览数据");
        if (size === "preview") rememberPreview(key, data.data_url);
        return data.data_url;
      })
      .finally(() => previewRequests.delete(key));
    previewRequests.set(key, requestPromise);
    return await requestPromise;
  }

  function closeImagePreview() {
    activePreviewKey = "";
    imagePreviewMask.classList.add("hidden");
    imagePreviewMask.setAttribute("aria-hidden", "true");
    imagePreviewImg.removeAttribute("src");
    imagePreviewLoading.textContent = "正在加载大图……";
    imagePreviewLoading.classList.remove("hidden");
  }

  async function openImagePreview(item, previewDataUrl = "") {
    const key = previewKey(item);
    activePreviewKey = key;
    imagePreviewTitle.textContent = item.relative_path || "表情包预览";
    imagePreviewMask.classList.remove("hidden");
    imagePreviewMask.setAttribute("aria-hidden", "false");
    imagePreviewLoading.textContent = "正在加载大图……";
    imagePreviewLoading.classList.remove("hidden");
    if (previewDataUrl) imagePreviewImg.src = previewDataUrl;
    else imagePreviewImg.removeAttribute("src");
    try {
      const original = await loadRecordImage(item, "original");
      if (activePreviewKey !== key) return;
      imagePreviewImg.src = original;
      imagePreviewLoading.classList.add("hidden");
    } catch (error) {
      if (activePreviewKey !== key) return;
      imagePreviewLoading.textContent = "大图加载失败，已保留缩略图";
      if (!previewDataUrl) {
        imagePreviewLoading.textContent = "图片预览加载失败";
      }
    }
  }

  function renderRecords(data) {
    recordsBox.replaceChildren();
    const records = Array.isArray(data.items) ? data.items : [];
    recordsCurrentPage = Number(data.page || recordsCurrentPage || 1);
    recordsTotalPages = Math.max(1, Number(data.total_pages || 1));
    recordCount.textContent = `共 ${Number(data.total || 0)} 条`;
    recordsPage.textContent = `第 ${recordsCurrentPage} / ${recordsTotalPages} 页`;
    recordsPrev.disabled = recordsCurrentPage <= 1 || requestRunning;
    recordsNext.disabled =
      recordsCurrentPage >= recordsTotalPages || requestRunning;
    if (!records.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "暂无语义记录";
      recordsBox.append(empty);
      return;
    }
    records.forEach((item) => {
      const row = document.createElement("div");
      row.className = "record";
      const path = document.createElement("div");
      path.className = "record-path";
      path.textContent = item.relative_path || "路径不可用";
      const copy = document.createElement("div");
      copy.className = "record-copy";
      const tags = Array.isArray(item.tags) ? item.tags.join("、") : "";
      copy.textContent = `描述结果：${item.caption || "暂无返回结果"}${tags ? ` · 标签：${tags}` : ""}`;
      if (item.visible_text)
        copy.textContent += ` · 图片文字：${item.visible_text}`;
      if (item.reclassification_status) {
        const reclassification = document.createElement("div");
        reclassification.className = "record-reclassification";
        reclassification.textContent = `自动重分类：${
          item.reclassified_from_category || "原分类"
        } → ${item.reclassified_to_category || item.category || "当前分类"}${
          item.reclassification_reason
            ? `；原因：${item.reclassification_reason}`
            : ""
        }`;
        copy.append(reclassification);
      }
      if (item.error) {
        const error = document.createElement("div");
        error.className = "record-error";
        error.textContent = `失败原因：${item.error}`;
        copy.append(error);
      }
      const state = document.createElement("div");
      state.className = "record-state";
      const statusText = (value) =>
        ({
          pending: "待处理",
          running: "进行中",
          done: "已完成",
          failed: "失败",
          cleared: "已清理",
        })[value] ||
        value ||
        "-";
      state.textContent = `描述：${statusText(item.caption_status)} / 向量：${statusText(item.embedding_status)}`;
      const previewButton = document.createElement("button");
      previewButton.type = "button";
      previewButton.className = "record-preview-button";
      previewButton.title = "点击放大查看表情包";
      previewButton.setAttribute(
        "aria-label",
        `放大查看 ${item.relative_path || "表情包"}`,
      );
      const previewImage = document.createElement("img");
      previewImage.alt = `表情包缩略图：${item.relative_path || ""}`;
      const previewText = document.createElement("span");
      previewText.textContent = "加载缩略图";
      const cachedPreview = previewCache.get(`${previewKey(item)}:preview`);
      if (cachedPreview) {
        previewImage.src = cachedPreview;
        previewButton.append(previewImage);
      } else {
        previewButton.append(previewText);
        void loadRecordImage(item)
          .then((dataUrl) => {
            if (!previewButton.isConnected) return;
            previewImage.src = dataUrl;
            previewButton.replaceChildren(previewImage);
          })
          .catch(() => {
            if (previewButton.isConnected) previewText.textContent = "预览失败";
          });
      }
      previewButton.addEventListener("click", () => {
        const dataUrl =
          previewCache.get(`${previewKey(item)}:preview`) ||
          previewImage.src ||
          "";
        void openImagePreview(item, dataUrl);
      });
      row.append(path, copy, state, previewButton);
      recordsBox.append(row);
    });
  }

  function withCurrentAuthParams(targetPath, extraParams = {}) {
    const nextUrl = new URL(targetPath, window.location.href);
    const currentParams = new URLSearchParams(window.location.search);
    for (const [key, value] of currentParams.entries()) {
      if (key !== "asset_token" && !nextUrl.searchParams.has(key))
        nextUrl.searchParams.set(key, value);
    }
    Object.entries(extraParams).forEach(([key, value]) => {
      if (value === null || value === undefined || value === "")
        nextUrl.searchParams.delete(key);
      else nextUrl.searchParams.set(key, String(value));
    });
    return nextUrl;
  }

  async function applySecureNavLinks(pageApi) {
    let token = "";
    try {
      const response = await pageApi.apiGet("bridge/auth_token");
      token = String(response?.token || "").trim();
    } catch (_) {
      // 认证参数已经由当前页面携带时，不需要阻塞页面操作。
    }
    document.querySelectorAll("a[data-nav-target]").forEach((link) => {
      const target = link.getAttribute("data-nav-target");
      if (!target) return;
      const navView = link.getAttribute("data-nav-view") || "";
      link.href = withCurrentAuthParams(target, {
        view: navView || null,
        asset_token: token || null,
      }).toString();
    });
  }

  async function loadPacks(apiGet) {
    const data = await apiGet("packs");
    const packs = Array.isArray(data?.packs) ? data.packs : [];
    packSelect.replaceChildren();
    packs.forEach((item) => {
      const option = document.createElement("option");
      option.value = String(item.id || "");
      option.textContent = item.name || item.id || "未命名资源包";
      packSelect.append(option);
    });
    setBusy(false);
  }

  async function loadStatus(apiGet) {
    if (!packSelect.value || requestRunning) return null;
    const requestSequence = ++statusRequestSequence;
    const requestedPackId = packSelect.value;
    const params = { pack_id: requestedPackId };
    const itemParams = {
      ...params,
      page: recordsCurrentPage,
      page_size: recordsPageSize,
    };
    if (recordsStatus !== "all") itemParams.status = recordsStatus;
    let statusData;
    let recordsData;
    try {
      [statusData, recordsData] = await Promise.all([
        apiGet("semantic/status", params),
        apiGet("semantic/items", itemParams),
      ]);
    } catch (error) {
      if (
        requestSequence !== statusRequestSequence ||
        requestedPackId !== packSelect.value
      )
        return null;
      throw error;
    }
    if (
      requestSequence !== statusRequestSequence ||
      requestedPackId !== packSelect.value
    )
      return null;
    if (!statusData.last_error) lastModalError = "";
    renderStatus(statusData);
    renderRecords(recordsData);
    if (statusData.last_error) {
      void reportError("语义任务提示", statusData.last_error);
    }
    return statusData;
  }

  async function runAction(apiPost, name) {
    if (!packSelect.value || requestRunning) return;
    if (name === "force") {
      const confirmed = await confirmTwice({
        title: "确认强制重新生成",
        message: "这会重新调用视觉模型处理全部图片，并覆盖已有描述。是否继续？",
        finalTitle: "最后确认：覆盖全部描述",
        finalMessage:
          "已有图片描述将被覆盖，并产生新的模型调用消耗。确定执行吗？",
        confirmText: "进入二次确认",
        finalConfirmText: "确认覆盖并重新生成",
      });
      if (!confirmed) return;
    }
    if (name === "clear") {
      const confirmed = await confirmTwice({
        title: "确认清空当前任务队列",
        message:
          "这会取消正在运行的语义任务并清空待处理队列；已完成描述和原图片会保留。",
        finalTitle: "最后确认：取消并清空任务",
        finalMessage:
          "正在运行的任务会立即取消，未完成队列会被清空。确定执行吗？",
        confirmText: "进入二次确认",
        finalConfirmText: "确认取消并清空",
      });
      if (!confirmed) return;
    }
    if (name === "delete-all") {
      const confirmed = await confirmTwice({
        title: "确认删除全部语义化数据",
        message:
          "这会删除当前资源包的全部图片描述、标签、失败记录、任务状态和本机向量索引；原图片不会删除。",
        finalTitle: "最后确认：彻底删除语义化数据",
        finalMessage:
          "删除后无法恢复已有描述，重新语义化会再次产生模型调用消耗。确定删除吗？",
        confirmText: "进入二次确认",
        finalConfirmText: "确认彻底删除",
      });
      if (!confirmed) return;
    }
    const route =
      name === "index"
        ? "semantic/rebuild-index"
        : name === "dimension"
          ? "semantic/rebuild-index"
          : name === "clear"
            ? "semantic/clear-local-state"
            : name === "delete-all"
              ? "semantic/delete-all"
              : `semantic/${name === "force" ? "start" : name}`;
    const mode = "full";
    const startsTask = ["start", "retry", "resume", "force"].includes(name);
    setBusy(true);
    showNotice(
      name === "pause" ? "正在中断本轮请求并恢复等待队列……" : "正在提交操作……",
    );
    try {
      const body = {
        pack_id: packSelect.value,
        mode,
        force: name === "force" || name === "dimension",
      };
      if (startsTask) {
        body.concurrency = Math.max(
          1,
          Math.min(16, Number(concurrencyInput.value) || 1),
        );
      }
      const result = await apiPost(route, body);
      if (startsTask) concurrencyDirty = false;
      showToast(result?.message || "操作已提交");
      showNotice(result?.message || "操作已提交");
    } catch (error) {
      await reportError("操作失败", error);
    } finally {
      setBusy(false);
      try {
        await loadStatus(apiGet);
      } catch (error) {
        await reportError("读取状态失败", error);
      }
    }
  }

  dialogCancel.addEventListener("click", () => closeDialog(false));
  dialogConfirm.addEventListener("click", () => closeDialog(true));
  dialogMask.addEventListener("click", (event) => {
    if (event.target === dialogMask && !dialogCancel.hidden) closeDialog(false);
  });
  imagePreviewClose.addEventListener("click", closeImagePreview);
  imagePreviewMask.addEventListener("click", (event) => {
    if (event.target === imagePreviewMask) closeImagePreview();
  });
  document.addEventListener("keydown", (event) => {
    if (
      event.key === "Escape" &&
      !imagePreviewMask.classList.contains("hidden")
    ) {
      closeImagePreview();
    }
  });

  const pageApi = window.AstrBotPluginPage;
  if (!pageApi) {
    await showDialog(
      "页面无法连接 AstrBot",
      "请从 AstrBot WebUI 的“语义化”入口打开此页面，不要直接访问本地 HTML 文件。",
      { showCancel: false },
    );
    return;
  }
  await waitForBridgeReady(pageApi);
  await applySecureNavLinks(pageApi);
  const apiGet = (path, params = {}) => pageApi.apiGet(path, params);
  const apiPost = (path, body = {}) => pageApi.apiPost(path, body);
  buttons.forEach((button) =>
    button.addEventListener("click", () =>
      runAction(apiPost, button.dataset.action),
    ),
  );
  recordsPrev.addEventListener("click", async () => {
    if (recordsCurrentPage <= 1 || requestRunning) return;
    recordsCurrentPage -= 1;
    try {
      await loadStatus(apiGet);
    } catch (error) {
      await reportError("读取任务记录失败", error);
    }
  });
  recordsNext.addEventListener("click", async () => {
    if (recordsCurrentPage >= recordsTotalPages || requestRunning) return;
    recordsCurrentPage += 1;
    try {
      await loadStatus(apiGet);
    } catch (error) {
      await reportError("读取任务记录失败", error);
    }
  });
  recordsFilter.addEventListener("change", async () => {
    recordsStatus = recordsFilter.value || "all";
    recordsCurrentPage = 1;
    try {
      await loadStatus(apiGet);
    } catch (error) {
      await reportError("读取筛选结果失败", error);
    }
  });
  concurrencyInput.addEventListener("change", () => {
    const value = Math.max(
      1,
      Math.min(16, Number(concurrencyInput.value) || 1),
    );
    concurrencyInput.value = String(value);
    concurrencyDirty = true;
  });
  packSelect.addEventListener("change", async () => {
    recordsCurrentPage = 1;
    try {
      const statusData = await loadStatus(apiGet);
      if (
        statusData?.semantic_enabled &&
        statusData.semantic_caption_complete &&
        statusData.dimension_rebuild_required
      ) {
        const confirmed = await showDialog(
          "当前资源包需要重建向量",
          `已切换到「${packSelect.options[packSelect.selectedIndex]?.textContent || packSelect.value}」。\n` +
            `当前模型：${statusData.embedding_provider_id || "自动选择"}，维度：${statusData.embedding_configured_dimension || "未知"}。\n` +
            "这个资源包尚未按当前模型建立本机向量索引，是否现在重建？",
          { confirmText: "重建向量" },
        );
        if (confirmed) await runAction(apiPost, "dimension");
      }
    } catch (error) {
      await reportError("读取状态失败", error);
    }
  });

  try {
    await loadPacks(apiGet);
    await loadStatus(apiGet);
    window.setInterval(updateTaskTimer, 1000);
    window.setInterval(
      () =>
        loadStatus(apiGet).catch((error) => reportError("自动刷新失败", error)),
      3000,
    );
  } catch (error) {
    setBusy(false);
    await reportError("加载语义页面失败", error);
  }
}

initSemanticPage().catch((error) => {
  const message = error?.message || String(error);
  const notice = document.querySelector("#notice");
  if (notice) {
    notice.textContent = message;
    notice.classList.add("error");
  }
});
