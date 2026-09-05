async function initSemanticPage() {
  const toastContainer = document.querySelector("#toast-container");
  const notice = document.querySelector("#notice");
  const otherTasksWarning = document.querySelector("#other-tasks-warning");
  const packSelect = document.querySelector("#pack");
  const statusBox = document.querySelector("#status");
  const statusError = document.querySelector("#status-error");
  const statusRetry = document.querySelector("#status-retry");
  const statusRefreshLabel = document.querySelector("#status-refresh-label");
  const taskState = document.querySelector("#task-state");
  const taskProgress = document.querySelector("#task-progress");
  const taskProgressLabel = document.querySelector("#task-progress-label");
  const taskGuidance = document.querySelector("#task-guidance");
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
  const imagePreviewRetry = document.querySelector("#image-preview-retry");
  const autoInboxPanel = document.querySelector("#auto-inbox-panel");
  const autoInboxCount = document.querySelector("#auto-inbox-count");
  const autoInboxItems = document.querySelector("#auto-inbox-items");
  const autoInboxSemanticize = document.querySelector(
    "#auto-inbox-semanticize",
  );
  const buttons = Array.from(document.querySelectorAll("button[data-action]"));
  let requestRunning = false;
  let embeddingReady = false;
  let visionReady = false;
  let captionComplete = false;
  let toastTimer = null;
  let lastReportedError = "";
  let lastActionError = "";
  let packsLoaded = false;
  let statusLoading = false;
  let lastRecordsSnapshot = "";
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
  let activePreviewItem = null;
  let previewVersion = 0;
  let activePreviewRequests = 0;
  let pendingAutoInboxCount = 0;
  const previewQueue = [];

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

  function reportError(title, error) {
    const message = errorMessage(error);
    showNotice(`${title}：${message}`, true);
    if (message !== lastReportedError) showToast(message, true);
    lastReportedError = message;
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
        (action === "force" || !captionComplete);
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
        !latestStatus ||
        !packSelect.value ||
        !allowedByTaskState ||
        (needsEmbedding && !embeddingReady) ||
        (needsCompleteCaption && !captionComplete) ||
        (needsVision && !visionReady);
      if (action === "pause" && latestStatus?.task_phase === "indexing") {
        button.title = "正在建立向量索引，这个收尾阶段不能暂停";
      } else if (action === "pause") {
        button.title = "立即中断本轮模型请求，未完成图片会退回等待队列";
      } else if (needsEmbedding && !embeddingReady) {
        button.title = "请先在设置中心配置可用的向量模型";
      } else if (needsVision && !visionReady) {
        button.title = "请先在设置中心配置可用的视觉模型";
      } else if (needsCompleteCaption && !captionComplete) {
        button.title = "请先完成图片描述生成";
      } else {
        button.removeAttribute("title");
      }
    });
    const queueIsActive = Boolean(latestStatus?.worker_alive);
    concurrencyInput.disabled =
      requestRunning || queueIsActive || !latestStatus;
    concurrencyHint.textContent = queueIsActive
      ? `当前队列固定为 ${
          latestStatus?.concurrency || concurrencyInput.value || 1
        } 并发；如需调整，请先清空队列后重新开始。`
      : "同时提交给视觉模型的图片上限，建议按模型限流设置。";
    autoInboxSemanticize.disabled =
      requestRunning ||
      !latestStatus ||
      pendingAutoInboxCount <= 0 ||
      !embeddingReady ||
      !visionReady ||
      latestStatus?.can_start === false;
    recordsPrev.disabled =
      requestRunning || statusLoading || recordsCurrentPage <= 1;
    recordsNext.disabled =
      requestRunning ||
      statusLoading ||
      recordsCurrentPage >= recordsTotalPages;
    recordsFilter.disabled = requestRunning || !packsLoaded;
    statusRetry.disabled = requestRunning || statusLoading;
  }

  function setBusy(value) {
    if (value) {
      statusRequestSequence += 1;
      statusLoading = false;
      recordsBox.setAttribute("aria-busy", "false");
    }
    requestRunning = value;
    updateButtonState();
    packSelect.disabled = value;
    document
      .querySelector(".control-panel")
      .setAttribute("aria-busy", String(value));
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
    renderAutoInbox(data.auto_collect_inbox);
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
    const focusedGroup = statusBox.contains(document.activeElement)
      ? document.activeElement
          .closest(".metric-group")
          ?.querySelector("summary")?.textContent
      : null;
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
    const total = Math.max(0, Number(data.total_tasks || 0));
    const completed = Math.max(0, Number(data.caption_done || 0));
    taskState.textContent = `${taskText || "等待开始"} · ${phaseText}`;
    taskProgress.max = total || 1;
    taskProgress.value = Math.min(completed, total);
    taskProgressLabel.textContent = total
      ? `描述 ${completed} / ${total}`
      : "暂无待处理图片";
    taskGuidance.textContent =
      data.status_message ||
      (data.task_status === "running"
        ? "正在处理图片，进度每 3 秒自动更新；需要时可暂停任务。"
        : data.can_resume
        ? "任务已暂停或中断，点击“继续队列”接着处理。"
        : data.can_retry
        ? "部分图片处理失败，可筛选失败记录，或点击“重试失败项”。"
        : "点击“一键完整语义化”开始。任务运行时每 3 秒自动更新。");
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
      groupOpen("任务进度", false),
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
      groupOpen("图片和描述", false),
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
    if (focusedGroup) {
      Array.from(statusBox.querySelectorAll("summary"))
        .find((summary) => summary.textContent === focusedGroup)
        ?.focus({ preventScroll: true });
    }
    if (lastActionError) {
      showNotice(lastActionError, true);
    } else if (data.last_error) {
      showNotice(data.last_error, true);
    } else if (["running", "paused"].includes(String(data.task_status || ""))) {
      showNotice(data.status_message || "任务状态已更新");
    } else if (data.semantic_enabled && data.semantic_config_ready === false) {
      showNotice(
        "语义检索已开启，但向量模型尚未就绪；当前仍使用分类逻辑。请前往设置中心的“语义与模型”检查配置。",
        true,
      );
    } else if (!embeddingReady) {
      showNotice(
        data.embedding_configured_provider_id
          ? `已配置 Embedding 模型「${data.embedding_configured_provider_id}」，但当前 Provider 不可用；请检查 Provider 是否启用或配置是否已加载。`
          : "尚未选择向量模型，请前往设置中心的“语义与模型”完成配置。",
        true,
      );
    } else if (
      data.dimension_rebuild_required &&
      !["running", "paused"].includes(String(data.task_status || ""))
    ) {
      showNotice(
        `已检测到 Embedding 模型「${
          [data.embedding_provider_id, data.embedding_model]
            .filter(Boolean)
            .join(" / ") || "当前模型"
        }」，但当前资源包还没有按此模型建立本机索引；请展开“高级维护”，点击“按当前维度重建向量”。`,
        false,
      );
    } else if (!visionReady) {
      showNotice(
        "未配置视觉模型，请前往设置中心的“语义与模型”选择图片描述模型。",
        true,
      );
    } else {
      showNotice(data.status_message || "");
    }
    updateTaskTimer();
    updateButtonState();
  }

  function renderAutoInbox(data) {
    const visible = Boolean(data?.visible);
    pendingAutoInboxCount = Math.max(0, Number(data?.count || 0));
    autoInboxPanel.classList.toggle("hidden", !visible);
    autoInboxCount.textContent = String(pendingAutoInboxCount);
    autoInboxItems.replaceChildren();
    if (!visible) return;
    const items = Array.isArray(data?.items) ? data.items : [];
    if (!items.length) {
      const empty = document.createElement("p");
      empty.className = "panel-hint";
      empty.textContent = "当前语义包没有等待整理的自动收集图片。";
      autoInboxItems.append(empty);
      return;
    }
    items.slice(0, 8).forEach((item) => {
      const row = document.createElement("div");
      row.className = "auto-inbox-item";
      const category = document.createElement("strong");
      category.textContent = item.suggested_category || "needs_review";
      const source = document.createElement("span");
      const sourceLabel = item.source_kind === "group" ? "群聊" : "个人";
      source.textContent = `${sourceLabel} ${item.source_id || "未知来源"}`;
      const receivedAt = document.createElement("time");
      const date = new Date(item.received_at || "");
      receivedAt.textContent = Number.isNaN(date.getTime())
        ? ""
        : date.toLocaleString();
      row.append(category, source, receivedAt);
      autoInboxItems.append(row);
    });
    if (pendingAutoInboxCount > 8) {
      const more = document.createElement("p");
      more.className = "panel-hint";
      more.textContent = `另有 ${pendingAutoInboxCount - 8} 张等待处理。`;
      autoInboxItems.append(more);
    }
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
    const requestedPackId = packSelect.value;
    const key = `${previewKey(item)}:${size}`;
    if (size === "preview" && previewCache.has(key))
      return previewCache.get(key);
    if (previewRequests.has(key)) return await previewRequests.get(key);
    const { category, filename } = imageLocation(item);
    if (!category || !filename) throw new Error("图片路径不可用");
    const requestPromise = schedulePreviewRequest(
      () =>
        apiGet("meme_image_data", {
          managed_pack_id: requestedPackId,
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
    activePreviewItem = null;
    previewVersion += 1;
    imagePreviewMask.classList.add("hidden");
    imagePreviewMask.setAttribute("aria-hidden", "true");
    imagePreviewImg.removeAttribute("src");
    imagePreviewLoading.textContent = "正在加载大图……";
    imagePreviewLoading.classList.remove("hidden");
    imagePreviewRetry.classList.add("hidden");
  }

  async function openImagePreview(item, previewDataUrl = "") {
    const key = previewKey(item);
    const version = ++previewVersion;
    activePreviewKey = key;
    activePreviewItem = item;
    imagePreviewTitle.textContent = item.relative_path || "表情包预览";
    imagePreviewMask.classList.remove("hidden");
    imagePreviewMask.setAttribute("aria-hidden", "false");
    imagePreviewLoading.textContent = "正在加载大图……";
    imagePreviewLoading.classList.remove("hidden");
    imagePreviewRetry.classList.add("hidden");
    if (previewDataUrl) imagePreviewImg.src = previewDataUrl;
    else imagePreviewImg.removeAttribute("src");
    try {
      const original = await loadRecordImage(item, "original");
      if (activePreviewKey !== key || version !== previewVersion) return;
      imagePreviewImg.src = original;
      imagePreviewLoading.classList.add("hidden");
    } catch (error) {
      if (activePreviewKey !== key || version !== previewVersion) return;
      imagePreviewLoading.textContent = "大图加载失败，已保留缩略图";
      if (!previewDataUrl) {
        imagePreviewLoading.textContent = "图片预览加载失败";
      }
      imagePreviewRetry.classList.remove("hidden");
    }
  }

  function renderRecords(data) {
    recordsBox.classList.remove("empty");
    const records = Array.isArray(data.items) ? data.items : [];
    recordsCurrentPage = Number(data.page || recordsCurrentPage || 1);
    recordsTotalPages = Math.max(1, Number(data.total_pages || 1));
    recordCount.textContent = `共 ${Number(data.total || 0)} 条`;
    recordsPage.textContent = `第 ${recordsCurrentPage} / ${recordsTotalPages} 页`;
    recordsPrev.disabled = recordsCurrentPage <= 1 || requestRunning;
    recordsNext.disabled =
      recordsCurrentPage >= recordsTotalPages || requestRunning;
    const snapshot = JSON.stringify([packSelect.value, recordsStatus, data]);
    if (snapshot === lastRecordsSnapshot) return;
    lastRecordsSnapshot = snapshot;
    const focusedPreview = recordsBox.contains(document.activeElement)
      ? document.activeElement.dataset.previewPath
      : null;
    recordsBox.replaceChildren();
    if (!records.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent =
        recordsStatus === "all"
          ? "还没有语义记录。选择资源包后，点击“一键完整语义化”开始处理。"
          : "没有符合此筛选条件的记录，可切换为“全部”查看。";
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
      copy.textContent = `描述结果：${item.caption || "暂无返回结果"}${
        tags ? ` · 标签：${tags}` : ""
      }`;
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
      state.textContent = `描述：${statusText(
        item.caption_status,
      )} / 向量：${statusText(item.embedding_status)}`;
      const previewButton = document.createElement("button");
      previewButton.type = "button";
      previewButton.className = "record-preview-button";
      previewButton.dataset.previewPath = item.relative_path || "";
      previewButton.setAttribute("aria-haspopup", "dialog");
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
            if (previewButton.isConnected)
              previewText.textContent = "加载失败，点击重试";
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
    if (focusedPreview) {
      Array.from(recordsBox.querySelectorAll(".record-preview-button"))
        .find((button) => button.dataset.previewPath === focusedPreview)
        ?.focus({ preventScroll: true });
    }
  }

  const { applySecureNavLinks } = window.MemeUI;

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
    packsLoaded = true;
    statusError.classList.add("hidden");
    statusRetry.textContent = "刷新";
    if (!packs.length) {
      packSelect.add(new Option("暂无资源包", ""));
      taskState.textContent = "暂无资源包";
      taskGuidance.textContent =
        "请先在表情管理中创建资源包，或从资源广场导入。";
      recordsBox.textContent = "暂无记录，请先添加资源包。";
      recordsBox.classList.add("empty");
      recordsBox.setAttribute("aria-busy", "false");
      statusRefreshLabel.textContent = "暂无任务";
    }
    setBusy(false);
  }

  async function loadStatus(apiGet) {
    if (!packSelect.value || requestRunning) return null;
    const requestSequence = ++statusRequestSequence;
    const requestedPackId = packSelect.value;
    statusLoading = true;
    recordsBox.setAttribute("aria-busy", "true");
    updateButtonState();
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
      statusError.textContent = `刷新失败：${errorMessage(error)}。${
        latestStatus ? "已保留上次结果，" : ""
      }可点击“重试刷新”。`;
      statusError.classList.remove("hidden");
      statusRefreshLabel.textContent = "连接中断";
      statusRetry.textContent = "重试刷新";
      throw error;
    } finally {
      if (requestSequence === statusRequestSequence) {
        statusLoading = false;
        recordsBox.setAttribute("aria-busy", "false");
        updateButtonState();
      }
    }
    if (
      requestSequence !== statusRequestSequence ||
      requestedPackId !== packSelect.value
    )
      return null;
    if (!statusData.last_error) lastReportedError = "";
    statusError.classList.add("hidden");
    statusRefreshLabel.textContent = "每 3 秒自动刷新";
    statusRetry.textContent = "刷新";
    renderStatus(statusData);
    renderRecords(recordsData);
    return statusData;
  }

  async function runAction(apiPost, name) {
    if (!packSelect.value || requestRunning) return;
    const requestedPackId = packSelect.value;
    const confirmation = {
      force: {
        title: "强制重新生成",
        message:
          "将重新调用视觉模型处理全部图片，覆盖已有描述，并产生新的模型调用消耗。",
        confirmText: "覆盖并重新生成",
      },
      clear: {
        title: "清空当前任务队列",
        message:
          "将取消正在运行的语义任务，并清空待处理队列。已完成的描述和原图片会保留。",
        confirmText: "取消并清空队列",
      },
      "delete-all": {
        title: "删除全部语义化数据",
        message:
          "将删除全部图片描述、标签、失败记录、任务状态和本机向量索引，保留原图片。删除后无法恢复描述，重新语义化会再次产生模型调用消耗。",
        confirmText: "删除语义化数据",
      },
    }[name];
    if (confirmation) {
      const confirmed = await window.MemeUI.confirm({
        ...confirmation,
        message: `资源包：${
          packSelect.selectedOptions[0]?.textContent || requestedPackId
        }\n\n${confirmation.message}`,
        danger: true,
      });
      if (!confirmed) return;
    }
    if (requestedPackId !== packSelect.value || requestRunning) return;
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
    lastActionError = "";
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
          Math.min(16, Math.floor(Number(concurrencyInput.value)) || 1),
        );
      }
      const result = await apiPost(route, body);
      if (startsTask) concurrencyDirty = false;
      showToast(result?.message || "操作已提交");
      showNotice(result?.message || "操作已提交");
    } catch (error) {
      lastActionError = `操作失败：${errorMessage(
        error,
      )}。请检查提示后重新提交。`;
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

  async function importAutoInboxAndStart(apiPost, apiGet) {
    if (!packSelect.value || requestRunning || pendingAutoInboxCount <= 0)
      return;
    const requestedPackId = packSelect.value;
    const confirmed = await window.MemeUI.confirm({
      title: "确认合入并语义化",
      message: `将 ${pendingAutoInboxCount} 张自动收集图片按建议分类合入「${
        packSelect.selectedOptions[0]?.textContent || requestedPackId
      }」，并立即启动完整语义化。`,
      confirmText: "合入并语义化",
    });
    if (!confirmed) return;
    if (requestedPackId !== packSelect.value || requestRunning) return;
    lastActionError = "";
    let importedCount = 0;
    setBusy(true);
    showNotice("正在合入自动收集待整理桶……");
    try {
      const imported = await apiPost("semantic/auto-inbox/import", {
        pack_id: packSelect.value,
      });
      importedCount = Number(imported?.imported || 0);
      if (importedCount > 0) {
        const started = await apiPost("semantic/start", {
          pack_id: packSelect.value,
          mode: "full",
          force: false,
          concurrency: Math.max(
            1,
            Math.min(16, Math.floor(Number(concurrencyInput.value)) || 1),
          ),
        });
        showToast(started?.message || imported?.message || "语义化任务已启动");
        showNotice(started?.message || "图片已合入，语义化任务已启动");
      } else {
        showToast(imported?.message || "没有需要合入的新图片");
        showNotice(imported?.message || "没有需要合入的新图片");
      }
    } catch (error) {
      lastActionError =
        importedCount > 0
          ? `${importedCount} 张图片已合入，但启动语义化失败：${errorMessage(
              error,
            )}。可点击“一键完整语义化”重试。`
          : `合入待整理桶失败：${errorMessage(error)}`;
      showNotice(lastActionError, true);
      showToast(lastActionError, true);
    } finally {
      setBusy(false);
      try {
        await loadStatus(apiGet);
      } catch (error) {
        await reportError("读取状态失败", error);
      }
    }
  }

  imagePreviewClose.addEventListener("click", closeImagePreview);
  imagePreviewRetry.addEventListener("click", () => {
    if (activePreviewItem)
      void openImagePreview(
        activePreviewItem,
        imagePreviewImg.getAttribute("src") || "",
      );
  });
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
    reportError(
      "页面无法连接 AstrBot",
      "请从 AstrBot WebUI 的“语义化”入口打开此页面，不要直接访问本地 HTML 文件。",
    );
    taskState.textContent = "无法连接 AstrBot";
    statusRefreshLabel.textContent = "未连接";
    statusRetry.disabled = true;
    recordsBox.setAttribute("aria-busy", "false");
    return;
  }
  const apiGet = (path, params = {}) => pageApi.apiGet(path, params);
  const apiPost = (path, body = {}) => pageApi.apiPost(path, body);
  statusRetry.addEventListener("click", async () => {
    if (requestRunning || statusLoading) return;
    statusRetry.disabled = true;
    statusRefreshLabel.textContent = "正在刷新…";
    try {
      await waitForBridgeReady(pageApi);
      if (!packsLoaded || !packSelect.value) await loadPacks(apiGet);
      await loadStatus(apiGet);
    } catch (error) {
      statusError.textContent = `加载失败：${errorMessage(
        error,
      )}。请检查连接后重试。`;
      statusError.classList.remove("hidden");
      statusRefreshLabel.textContent = "连接中断";
      statusRetry.textContent = "重试刷新";
    } finally {
      updateButtonState();
    }
  });
  buttons.forEach((button) =>
    button.addEventListener("click", () =>
      runAction(apiPost, button.dataset.action),
    ),
  );
  autoInboxSemanticize.addEventListener("click", () =>
    importAutoInboxAndStart(apiPost, apiGet),
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
      Math.min(16, Math.floor(Number(concurrencyInput.value)) || 1),
    );
    concurrencyInput.value = String(value);
    concurrencyDirty = true;
  });
  packSelect.addEventListener("change", async () => {
    recordsCurrentPage = 1;
    latestStatus = null;
    lastActionError = "";
    lastRecordsSnapshot = "";
    concurrencyDirty = false;
    closeImagePreview();
    taskState.textContent = "正在加载所选资源包…";
    taskProgress.value = 0;
    taskProgressLabel.textContent = "";
    statusBox.replaceChildren();
    recordsBox.replaceChildren();
    try {
      const statusData = await loadStatus(apiGet);
      if (statusData?.dimension_rebuild_required) {
        document.querySelector(".advanced-actions").open = true;
      }
    } catch (error) {
      await reportError("读取状态失败", error);
    }
  });

  try {
    setBusy(true);
    await waitForBridgeReady(pageApi);
    await applySecureNavLinks(pageApi);
    await loadPacks(apiGet);
    await loadStatus(apiGet);
  } catch (error) {
    setBusy(false);
    statusError.textContent = `加载失败：${errorMessage(
      error,
    )}。请检查连接后点击“重试刷新”。`;
    statusError.classList.remove("hidden");
    statusRefreshLabel.textContent = "连接中断";
    statusRetry.textContent = "重试刷新";
    taskState.textContent = "暂时无法读取任务状态";
    recordsBox.setAttribute("aria-busy", "false");
  }
  window.setInterval(updateTaskTimer, 1000);
  window.setInterval(() => {
    if (document.hidden || statusLoading) return;
    void loadStatus(apiGet).catch(() => {});
  }, 3000);
}

initSemanticPage().catch((error) => {
  const message = error?.message || String(error);
  const notice = document.querySelector("#notice");
  if (notice) {
    notice.textContent = message;
    notice.classList.add("error");
  }
});
