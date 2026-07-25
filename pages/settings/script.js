async function initSettingsPage() {
  await window.AstrBotPluginPage.ready();

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

  const rulesList = document.getElementById("rules-list");
  const addRuleBtn = document.getElementById("add-rule-btn");
  const reloadRulesBtn = document.getElementById("reload-rules-btn");
  const saveRulesBtn = document.getElementById("save-rules-btn");
  const rulesValidation = document.getElementById("rules-validation");

  const backupOutputDirInput = document.getElementById(
    "backup-output-dir-input",
  );
  const exportBackupBtn = document.getElementById("export-backup-btn");
  const exportResult = document.getElementById("export-result");
  const backupFileInput = document.getElementById("backup-file-input");
  const importOverwriteCheckbox = document.getElementById(
    "import-overwrite-checkbox",
  );
  const importBackupBtn = document.getElementById("import-backup-btn");
  const importResult = document.getElementById("import-result");

  const transferPackSelect = document.getElementById("transfer-pack-select");
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

  const logList = document.getElementById("log-list");

  let installedPacks = [];
  let rules = [];
  let dragRuleIndex = -1;
  let personaTargets = [];
  let sessionTargets = [];
  let migrationPacksById = new Map();
  let activeTransferPackId = "";
  let pendingPackImportToken = "";
  let exportCapabilityRequestId = 0;

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

  function setPackTransferResult(element, message = "", type = "") {
    if (!element) {
      return;
    }
    element.textContent = String(message || "");
    element.classList.toggle("success", type === "success");
    element.classList.toggle("error", type === "error");
  }

  function selectedExportMode() {
    return exportModeInputs.find((input) => input.checked)?.value || "share";
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

  function syncTransferPackOptions(preferredPackId = "") {
    if (!transferPackSelect) {
      return "";
    }
    transferPackSelect.innerHTML = "";
    installedPacks.forEach((pack) => {
      const packId = String(pack?.id || "").trim();
      const packName = String(pack?.name || packId || "未命名");
      const count = Number(pack?.image_count || 0);
      const option = document.createElement("option");
      option.value = packId;
      option.textContent = `${packName} (${count} 张)`;
      transferPackSelect.appendChild(option);
    });

    const candidateIds = new Set(
      installedPacks.map((item) => String(item?.id || "").trim()),
    );
    const nextPackId =
      (preferredPackId && candidateIds.has(String(preferredPackId).trim())
        ? String(preferredPackId).trim()
        : candidateIds.has(activeTransferPackId)
          ? activeTransferPackId
          : String(installedPacks[0]?.id || "").trim()) || "";

    activeTransferPackId = nextPackId;
    transferPackSelect.value = nextPackId;
    return nextPackId;
  }

  async function refreshPackExportCapability(packId = activeTransferPackId) {
    const normalizedPackId = String(packId || "").trim();
    const requestId = ++exportCapabilityRequestId;
    const pack = migrationPacksById.get(normalizedPackId);

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
      if (vectorBackupHint)
        vectorBackupHint.textContent = "当前没有可导出的表情包。";
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
    if (vectorBackupHint) {
      vectorBackupHint.textContent = "正在检查当前表情包的向量状态…";
    }
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
      addLog(`读取单包导出能力失败: ${error?.message || String(error)}`, true);
    } finally {
      if (requestId === exportCapabilityRequestId) {
        updateExportModeAppearance();
      }
    }
  }

  async function downloadCurrentPack() {
    const packId = String(activeTransferPackId || "").trim();
    if (!packId) {
      setPackTransferResult(
        exportPackResult,
        "当前没有可导出的表情包。",
        "error",
      );
      addLog("当前没有可导出的表情包", true);
      return;
    }
    const mode = selectedExportMode();
    setLoading(exportPackDownloadBtn, "正在生成压缩包...");
    setPackTransferResult(
      exportPackResult,
      "正在整理文件，请不要关闭页面。",
      "",
    );
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
      addLog(`单包导出成功: ${packId} (${label})`);
    } catch (error) {
      setPackTransferResult(
        exportPackResult,
        error?.message || String(error),
        "error",
      );
      addLog(`单包导出失败: ${error?.message || String(error)}`, true);
    } finally {
      clearLoading(exportPackDownloadBtn);
      updateExportModeAppearance();
    }
  }

  function resetPackImportPreview({ keepResult = false } = {}) {
    pendingPackImportToken = "";
    if (packImportFile) packImportFile.value = "";
    if (packImportFileLabel)
      packImportFileLabel.textContent = "选择或拖入 zip 文件";
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
    if (
      !String(file.name || "")
        .toLowerCase()
        .endsWith(".zip")
    ) {
      setPackTransferResult(
        packImportResult,
        "请选择 zip 格式的表情包。",
        "error",
      );
      addLog("单包导入失败: 文件格式不支持", true);
      return;
    }
    pendingPackImportToken = "";
    if (packImportFileLabel) {
      packImportFileLabel.textContent = `正在检查 ${file.name}…`;
    }
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
      setPackTransferResult(
        packImportResult,
        "检查完成，请确认导入选项。",
        "success",
      );
      addLog(`单包导入检查完成: ${data?.pack_id || file.name}`);
    } catch (error) {
      resetPackImportPreview({ keepResult: true });
      setPackTransferResult(
        packImportResult,
        error?.message || String(error),
        "error",
      );
      addLog(`单包导入检查失败: ${error?.message || String(error)}`, true);
    } finally {
      packImportDropzone?.classList.remove("checking");
    }
  }

  async function confirmPackImport() {
    if (!pendingPackImportToken) {
      setPackTransferResult(
        packImportResult,
        "请先选择并检查压缩包。",
        "error",
      );
      addLog("单包导入失败: 缺少导入凭证", true);
      return;
    }
    if (packImportOverwrite?.checked) {
      const confirmed = window.confirm(
        packImportOverwriteManual?.checked
          ? "同名表情包将被覆盖，且本机人工描述、标签和图片文字也会被替换。确定继续吗？"
          : "同名表情包及其向量将被覆盖，但本机人工描述、标签和图片文字会保留。确定继续吗？",
      );
      if (!confirmed) {
        return;
      }
    }

    setLoading(packImportConfirmBtn, "正在导入...");
    setPackTransferResult(
      packImportResult,
      "正在安装表情包，请不要关闭页面。",
      "",
    );
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
      await refreshPacksAndRules(importedPackId);
      addLog(`单包导入成功: ${importedPackId || data?.name || "未知表情包"}`);
    } catch (error) {
      setPackTransferResult(
        packImportResult,
        error?.message || String(error),
        "error",
      );
      addLog(`单包导入失败: ${error?.message || String(error)}`, true);
    } finally {
      clearLoading(packImportConfirmBtn);
    }
  }

  function ensureDefaultRuleAtEnd(defaultPackId = "") {
    const normalRules = rules.filter((rule) => rule.scope !== "default");
    let defaultRule = rules.find((rule) => rule.scope === "default");
    if (!defaultRule) {
      defaultRule = {
        id: "default",
        scope: "default",
        pack_id: defaultPackId || installedPacks[0]?.id || "",
      };
    }
    rules = [...normalRules, defaultRule];
  }

  function findDefaultRuleIndex() {
    return rules.findIndex((rule) => rule.scope === "default");
  }

  function getPackOptions(selectedPackId = "") {
    return installedPacks
      .map((pack) => {
        const selectedAttr =
          String(pack.id) === String(selectedPackId) ? "selected" : "";
        return `<option value="${pack.id}" ${selectedAttr}>${pack.name || pack.id} (${pack.id})</option>`;
      })
      .join("");
  }

  function getTargetSuggestions(scope) {
    if (scope === "persona") {
      return personaTargets
        .map((item) => String(item.id || "").trim())
        .filter(Boolean);
    }
    if (scope === "session") {
      return sessionTargets
        .map((item) => String(item || "").trim())
        .filter(Boolean);
    }
    return [];
  }

  function updateRuleFromInput(index, key, value) {
    if (!rules[index]) {
      return;
    }
    if (
      key === "scope" &&
      String(value || "") === "default" &&
      String(rules[index].scope || "") !== "default"
    ) {
      return;
    }
    rules[index][key] = value;
    renderRulesValidation();
  }

  function moveRuleToIndex(fromIndex, toIndex) {
    if (fromIndex === toIndex || fromIndex < 0 || toIndex < 0) {
      return;
    }

    const defaultIndex = findDefaultRuleIndex();
    if (defaultIndex < 0) {
      return;
    }
    if (fromIndex >= defaultIndex) {
      return;
    }
    if (toIndex >= defaultIndex) {
      toIndex = defaultIndex - 1;
    }
    if (toIndex < 0) {
      toIndex = 0;
    }

    const cloned = [...rules];
    const [item] = cloned.splice(fromIndex, 1);
    cloned.splice(toIndex, 0, item);
    rules = cloned;
    ensureDefaultRuleAtEnd();
    renderRules();
  }

  function removeRule(index) {
    if (!rules[index] || rules[index].scope === "default") {
      return;
    }
    rules.splice(index, 1);
    renderRules();
  }

  function getClientValidationErrors() {
    const errors = [];
    const idSet = new Set();
    const scopeTargetSet = new Set();
    let defaultCount = 0;

    rules.forEach((rule, index) => {
      const position = `第 ${index + 1} 条`;
      const id = String(rule.id || "").trim();
      const scope = String(rule.scope || "").trim();
      const packId = String(rule.pack_id || "").trim();
      const target = String(rule.target || "").trim();

      if (!id) {
        errors.push(`${position} 缺少 id`);
      } else if (idSet.has(id)) {
        errors.push(`${position} 的 id 与其他规则重复: ${id}`);
      } else {
        idSet.add(id);
      }

      if (!["persona", "session", "default"].includes(scope)) {
        errors.push(`${position} 的 scope 非法: ${scope || "(空)"}`);
      }
      if (!packId) {
        errors.push(`${position} 缺少 pack_id`);
      }

      if (scope === "default") {
        defaultCount += 1;
      }

      if (scope === "persona" || scope === "session") {
        if (!target) {
          errors.push(`${position} 缺少 target`);
        } else {
          const key = `${scope}::${target}`;
          if (scopeTargetSet.has(key)) {
            errors.push(
              `${position} 与前序规则冲突: ${scope} 目标 ${target} 重复`,
            );
          } else {
            scopeTargetSet.add(key);
          }
        }
      }
    });

    if (defaultCount !== 1) {
      errors.push("必须且仅能存在一条 default 规则");
    }
    if (rules.length && rules[rules.length - 1]?.scope !== "default") {
      errors.push("default 规则必须位于最后");
    }

    return errors;
  }

  function renderRulesValidation() {
    const errors = getClientValidationErrors();
    if (!errors.length) {
      rulesValidation.classList.add("hidden");
      rulesValidation.textContent = "";
      return true;
    }

    rulesValidation.classList.remove("hidden");
    rulesValidation.innerHTML = `
      <strong>规则存在问题，请先修复：</strong>
      <ul>${errors.map((item) => `<li>${item}</li>`).join("")}</ul>
    `;
    return false;
  }

  function renderRules() {
    rulesList.innerHTML = "";

    rules.forEach((rule, index) => {
      const isDefault = rule.scope === "default";
      const wrapper = document.createElement("div");
      wrapper.className = `rule-item${isDefault ? " default" : ""}`;
      wrapper.dataset.index = String(index);
      if (!isDefault) {
        wrapper.draggable = true;
      }

      const titleRow = document.createElement("div");
      titleRow.className = "rule-title-row";
      const title = document.createElement("div");
      title.innerHTML = `<strong>${isDefault ? "默认规则" : `规则 #${index + 1}`}</strong>`;
      titleRow.appendChild(title);

      if (!isDefault) {
        const dragHandle = document.createElement("button");
        dragHandle.type = "button";
        dragHandle.className = "drag-handle";
        dragHandle.textContent = "拖拽排序";
        dragHandle.title = "拖拽调整顺序";
        titleRow.appendChild(dragHandle);
      }

      wrapper.appendChild(titleRow);

      const grid = document.createElement("div");
      grid.className = "rule-grid";

      const scopeField = document.createElement("div");
      scopeField.className = "field-row";
      scopeField.innerHTML = `
        <label>scope</label>
        <select data-role="scope">
          <option value="persona" ${rule.scope === "persona" ? "selected" : ""}>persona</option>
          <option value="session" ${rule.scope === "session" ? "selected" : ""}>session</option>
          ${isDefault ? '<option value="default" selected>default</option>' : ""}
        </select>
      `;

      const targetField = document.createElement("div");
      targetField.className = "field-row";
      const targetListId = `target-suggestions-${index}`;
      const targetPlaceholder =
        rule.scope === "persona"
          ? "从 persona 建议中选择或手动填写"
          : rule.scope === "session"
            ? "从 session 建议中选择或手动填写"
            : "default 规则无需 target";
      const targetSuggestions = getTargetSuggestions(rule.scope);
      targetField.innerHTML = `
        <label>target</label>
        <input data-role="target" type="text" value="${rule.target || ""}" ${isDefault ? "disabled" : ""} placeholder="${targetPlaceholder}" list="${targetListId}" />
        <datalist id="${targetListId}">
          ${targetSuggestions.map((item) => `<option value="${item}"></option>`).join("")}
        </datalist>
      `;

      const packField = document.createElement("div");
      packField.className = "field-row";
      packField.innerHTML = `
        <label>pack_id</label>
        <select data-role="pack">${getPackOptions(rule.pack_id)}</select>
      `;

      grid.appendChild(scopeField);
      grid.appendChild(targetField);
      grid.appendChild(packField);
      wrapper.appendChild(grid);

      const actions = document.createElement("div");
      actions.className = "rule-actions";
      actions.innerHTML = `
        <button type="button" class="danger" data-action="remove" ${isDefault ? "disabled" : ""}>删除</button>
      `;
      wrapper.appendChild(actions);

      const scopeSelect = scopeField.querySelector('select[data-role="scope"]');
      const targetInput = targetField.querySelector(
        'input[data-role="target"]',
      );
      const packSelect = packField.querySelector('select[data-role="pack"]');

      scopeSelect.disabled = isDefault;
      scopeSelect.addEventListener("change", () => {
        const selectedScope = scopeSelect.value;
        updateRuleFromInput(index, "scope", scopeSelect.value);
        if (!rules[index] || rules[index].scope === "default") {
          renderRules();
          return;
        }

        // scope 切换后重置 target 和 pack_id，避免旧值残留
        const firstSuggestion = getTargetSuggestions(selectedScope)[0] || "";
        rules[index].target = firstSuggestion;
        rules[index].pack_id = installedPacks[0]?.id || "";
        renderRules();
      });

      targetInput.addEventListener("input", () => {
        updateRuleFromInput(index, "target", targetInput.value);
      });

      packSelect.addEventListener("change", () => {
        updateRuleFromInput(index, "pack_id", packSelect.value);
      });

      actions
        .querySelector('[data-action="remove"]')
        .addEventListener("click", () => {
          removeRule(index);
        });

      wrapper.addEventListener("dragstart", (event) => {
        if (isDefault) {
          event.preventDefault();
          return;
        }
        dragRuleIndex = index;
        wrapper.classList.add("dragging");
        if (event.dataTransfer) {
          event.dataTransfer.effectAllowed = "move";
          event.dataTransfer.setData("text/plain", String(index));
        }
      });

      wrapper.addEventListener("dragend", () => {
        dragRuleIndex = -1;
        wrapper.classList.remove("dragging");
        rulesList
          .querySelectorAll(".rule-item.drop-target")
          .forEach((item) => item.classList.remove("drop-target"));
      });

      wrapper.addEventListener("dragover", (event) => {
        if (dragRuleIndex < 0 || isDefault) {
          return;
        }
        event.preventDefault();
        wrapper.classList.add("drop-target");
      });

      wrapper.addEventListener("dragleave", () => {
        wrapper.classList.remove("drop-target");
      });

      wrapper.addEventListener("drop", (event) => {
        event.preventDefault();
        wrapper.classList.remove("drop-target");
        if (dragRuleIndex < 0 || isDefault) {
          return;
        }
        moveRuleToIndex(dragRuleIndex, index);
      });

      rulesList.appendChild(wrapper);
    });

    const defaultIndex = findDefaultRuleIndex();
    rulesList.ondragover = (event) => {
      if (dragRuleIndex < 0) {
        return;
      }
      event.preventDefault();
    };
    rulesList.ondrop = (event) => {
      if (dragRuleIndex < 0) {
        return;
      }
      event.preventDefault();
      moveRuleToIndex(dragRuleIndex, Math.max(defaultIndex - 1, 0));
    };

    renderRulesValidation();
  }

  async function refreshPacksAndRules(preferredTransferPackId = "") {
    const [packsResponse, rulesResponse, targetsResponse] = await Promise.all([
      apiGet("packs"),
      apiGet("settings/rules"),
      apiGet("settings/targets"),
    ]);

    installedPacks = Array.isArray(packsResponse?.packs)
      ? packsResponse.packs
      : [];
    migrationPacksById = new Map(
      installedPacks
        .map((pack) => [String(pack?.id || "").trim(), pack])
        .filter(([packId]) => Boolean(packId)),
    );
    rules = Array.isArray(rulesResponse?.rules) ? rulesResponse.rules : [];
    personaTargets = Array.isArray(targetsResponse?.persona_targets)
      ? targetsResponse.persona_targets
      : [];
    sessionTargets = Array.isArray(targetsResponse?.session_targets)
      ? targetsResponse.session_targets
      : [];
    ensureDefaultRuleAtEnd(rulesResponse?.default_pack_id || "");
    renderRules();
    const nextTransferPackId = syncTransferPackOptions(preferredTransferPackId);
    await refreshPackExportCapability(nextTransferPackId);
  }

  function buildNewRule(scope) {
    const firstSuggestion = getTargetSuggestions(scope)[0] || "";
    return {
      id: `${scope}-${Date.now()}`,
      scope,
      target: firstSuggestion,
      pack_id: installedPacks[0]?.id || "",
    };
  }

  async function saveRules() {
    const payloadRules = rules.map((rule) => {
      const normalized = {
        id: String(rule.id || "").trim(),
        scope: String(rule.scope || "").trim(),
        pack_id: String(rule.pack_id || "").trim(),
      };
      if (normalized.scope !== "default") {
        normalized.target = String(rule.target || "").trim();
      }
      return normalized;
    });

    setLoading(saveRulesBtn, "保存中...");
    try {
      if (!renderRulesValidation()) {
        addLog("规则校验失败，请先修复后再保存", true);
        return;
      }
      const response = await apiPost("settings/rules", { rules: payloadRules });
      rules = Array.isArray(response?.rules) ? response.rules : payloadRules;
      ensureDefaultRuleAtEnd(response?.default_pack_id || "");
      renderRules();
      addLog("规则保存成功");
      const rebuildPacks = Array.isArray(response?.semantic_rebuild_packs)
        ? response.semantic_rebuild_packs
        : [];
      for (const packId of rebuildPacks) {
        const shouldRebuild = window.confirm(
          `资源包「${packId}」已切换，但还没有按当前 Embedding 模型建立本机向量。是否现在重建？`,
        );
        if (!shouldRebuild) continue;
        try {
          await apiPost("semantic/rebuild-index", {
            pack_id: packId,
            force: true,
          });
          addLog(`资源包 ${packId} 的向量重建已完成`);
        } catch (rebuildError) {
          addLog(
            `资源包 ${packId} 向量重建失败: ${rebuildError?.message || String(rebuildError)}`,
            true,
          );
        }
      }
    } catch (error) {
      addLog(`规则保存失败: ${error?.message || String(error)}`, true);
    } finally {
      clearLoading(saveRulesBtn);
    }
  }

  async function exportBackup() {
    setLoading(exportBackupBtn, "导出中...");
    try {
      const outputDir = String(backupOutputDirInput.value || "").trim();
      const response = await apiPost("settings/backup/export", {
        output_dir: outputDir || undefined,
      });
      exportResult.textContent = `导出成功: ${response.archive_path || ""}`;
      addLog(`备份导出成功: ${response.archive_path || ""}`);
    } catch (error) {
      exportResult.textContent = `导出失败: ${error?.message || String(error)}`;
      addLog(`备份导出失败: ${error?.message || String(error)}`, true);
    } finally {
      clearLoading(exportBackupBtn);
    }
  }

  async function importBackup() {
    const file = backupFileInput.files?.[0];
    if (!file) {
      addLog("请先选择备份 zip 文件", true);
      return;
    }

    setLoading(importBackupBtn, "导入中...");
    try {
      const bytes = await file.arrayBuffer();
      let binary = "";
      const view = new Uint8Array(bytes);
      const chunkSize = 0x8000;
      for (let offset = 0; offset < view.length; offset += chunkSize) {
        const chunk = view.subarray(offset, offset + chunkSize);
        binary += String.fromCharCode(...chunk);
      }
      const response = await apiPost("settings/backup/import", {
        overwrite: importOverwriteCheckbox.checked,
        file_name: file.name,
        file_b64: btoa(binary),
      });
      importResult.textContent = `导入成功: 恢复 ${response?.restored_packs ?? 0} 个 pack`;
      addLog(`备份导入成功，恢复 ${response?.restored_packs ?? 0} 个 pack`);
      await refreshPacksAndRules();
    } catch (error) {
      importResult.textContent = `导入失败: ${error?.message || String(error)}`;
      addLog(`备份导入失败: ${error?.message || String(error)}`, true);
    } finally {
      clearLoading(importBackupBtn);
    }
  }

  addRuleBtn.addEventListener("click", () => {
    rules.splice(Math.max(rules.length - 1, 0), 0, buildNewRule("persona"));
    renderRules();
  });

  reloadRulesBtn.addEventListener("click", async () => {
    setLoading(reloadRulesBtn, "加载中...");
    try {
      await refreshPacksAndRules();
      addLog("规则已重新加载");
    } catch (error) {
      addLog(`重新加载失败: ${error?.message || String(error)}`, true);
    } finally {
      clearLoading(reloadRulesBtn);
    }
  });

  saveRulesBtn.addEventListener("click", () => {
    void saveRules();
  });

  exportBackupBtn.addEventListener("click", () => {
    void exportBackup();
  });

  importBackupBtn.addEventListener("click", () => {
    void importBackup();
  });

  transferPackSelect?.addEventListener("change", () => {
    activeTransferPackId = String(transferPackSelect.value || "").trim();
    void refreshPackExportCapability(activeTransferPackId);
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

  updateExportModeAppearance();
  resetPackImportPreview();

  try {
    await refreshPacksAndRules();
    addLog("设置中心已就绪");
  } catch (error) {
    addLog(`初始化失败: ${error?.message || String(error)}`, true);
  }
}

void initSettingsPage();
