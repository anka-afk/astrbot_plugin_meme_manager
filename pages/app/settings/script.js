async function initSettingsPage() {
  await window.AstrBotPluginPage.ready();

  const { applySecureNavLinks } = window.MemeUI;

  await applySecureNavLinks();

  const rulesList = document.getElementById("rules-list");
  const addRuleBtn = document.getElementById("add-rule-btn");
  const reloadRulesBtn = document.getElementById("reload-rules-btn");
  const saveRulesBtn = document.getElementById("save-rules-btn");
  const rulesValidation = document.getElementById("rules-validation");
  const rulesSaveStatus = document.getElementById("rules-save-status");

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
  let savedRulesSnapshot = "";
  let rulesLoaded = false;
  let rulesBusy = false;
  let allowPageLeave = false;
  let dragRuleIndex = -1;
  let personaTargets = [];
  let sessionTargets = [];
  let migrationPacksById = new Map();
  let activeTransferPackId = "";
  let pendingPackImportToken = "";
  let exportCapabilityRequestId = 0;
  let packImportBusy = false;

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
    while (logList.children.length > 80) logList.lastElementChild.remove();
  }

  function setLoading(button, loadingText) {
    button.dataset.originalHtml = button.innerHTML;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = loadingText;
  }

  function clearLoading(button) {
    button.disabled = false;
    button.removeAttribute("aria-busy");
    if (button.dataset.originalHtml) {
      button.innerHTML = button.dataset.originalHtml;
      delete button.dataset.originalHtml;
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

  function updateRulesState(message = "", type = "") {
    const dirty = rulesLoaded && JSON.stringify(rules) !== savedRulesSnapshot;
    rulesList.inert = rulesBusy;
    rulesList.setAttribute("aria-busy", String(rulesBusy));
    addRuleBtn.disabled = rulesBusy || !rulesLoaded || !installedPacks.length;
    reloadRulesBtn.disabled = rulesBusy;
    saveRulesBtn.disabled = rulesBusy || !rulesLoaded || !dirty;
    importBackupBtn.disabled = rulesBusy || packImportBusy;
    packImportConfirmBtn.disabled = rulesBusy || packImportBusy;
    setPackTransferResult(
      rulesSaveStatus,
      message ||
        (dirty
          ? "有未保存的更改"
          : rulesLoaded
          ? "所有更改已保存"
          : "正在读取设置…"),
      type,
    );
    rulesSaveStatus.classList.toggle("unsaved", dirty);
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
    transferPackSelect.disabled = installedPacks.length === 0;
    if (!installedPacks.length) {
      const option = document.createElement("option");
      option.textContent = "暂无表情包";
      option.value = "";
      transferPackSelect.appendChild(option);
    }
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
          ? `包含完整本机向量${
              modelHint ? `（${modelHint}）` : ""
            }，适合迁回相同模型环境。`
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
    transferPackSelect.disabled = true;
    exportModeInputs.forEach((input) => {
      input.disabled = true;
    });
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
      transferPackSelect.disabled = installedPacks.length === 0;
      document.getElementById("export-mode-share").disabled = false;
      await refreshPackExportCapability(activeTransferPackId);
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
    if (packImportOverwriteManual) packImportOverwriteManual.disabled = true;
    if (!keepResult) setPackTransferResult(packImportResult, "", "");
  }

  function renderPackImportInspection(data) {
    const formatLabels = {
      v2: data?.export_mode === "backup" ? "新版带向量备份" : "新版分享包",
      v1: "兼容版资源包",
      legacy: "旧版无语义包 · 将自动转换",
    };
    if (packImportPreviewName) {
      packImportPreviewName.textContent = `${
        data?.name || data?.pack_id || "待导入表情包"
      } (${data?.pack_id || "未知 ID"})`;
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
    if (!file || packImportBusy) {
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
    packImportBusy = true;
    importBackupBtn.disabled = true;
    packImportFile.disabled = true;
    packImportDropzone.setAttribute("aria-disabled", "true");
    packImportDropzone.setAttribute("aria-busy", "true");
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
      packImportConfirmBtn.focus();
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
      packImportBusy = false;
      importBackupBtn.disabled = rulesBusy;
      packImportFile.disabled = false;
      packImportDropzone.removeAttribute("aria-disabled");
      packImportDropzone.removeAttribute("aria-busy");
      packImportDropzone?.classList.remove("checking");
    }
  }

  async function confirmPackImport() {
    if (packImportBusy || rulesBusy) return;
    if (!pendingPackImportToken) {
      setPackTransferResult(
        packImportResult,
        "请先选择并检查压缩包。",
        "error",
      );
      addLog("单包导入失败: 缺少导入凭证", true);
      return;
    }
    const dirty = rulesLoaded && JSON.stringify(rules) !== savedRulesSnapshot;
    if (packImportOverwrite?.checked || dirty) {
      const overwriteMessage = packImportOverwrite?.checked
        ? packImportOverwriteManual?.checked
          ? "同名表情包将被覆盖，且本机人工描述、标签和图片文字也会被替换。确定继续吗？"
          : "同名表情包及其向量将被覆盖，本机人工描述、标签和图片文字会保留。"
        : "";
      const confirmed = await window.MemeUI.confirm({
        title: "确认导入表情包",
        message: [
          overwriteMessage,
          dirty ? "导入成功后将重新加载规则，当前未保存的规则更改会丢失。" : "",
        ]
          .filter(Boolean)
          .join("\n\n"),
        confirmText: "继续导入",
        danger: true,
      });
      if (!confirmed) {
        return;
      }
    }

    packImportBusy = true;
    rulesBusy = true;
    updateRulesState("正在导入表情包…");
    setLoading(packImportConfirmBtn, "正在导入...");
    packImportResetBtn.disabled = true;
    packImportSetDefault.disabled = true;
    packImportOverwrite.disabled = true;
    packImportOverwriteManual.disabled = true;
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
      addLog(`单包导入成功: ${importedPackId || data?.name || "未知表情包"}`);
      try {
        await refreshPacksAndRules(importedPackId);
      } catch (refreshError) {
        setPackTransferResult(
          packImportResult,
          `表情包已导入，但设置刷新失败：${
            refreshError?.message || String(refreshError)
          }。请点击「重新加载」。`,
          "error",
        );
        addLog("表情包已导入，但设置刷新失败", true);
      }
      packImportDropzone.focus();
    } catch (error) {
      setPackTransferResult(
        packImportResult,
        error?.message || String(error),
        "error",
      );
      addLog(`单包导入失败: ${error?.message || String(error)}`, true);
    } finally {
      packImportBusy = false;
      rulesBusy = false;
      updateRulesState();
      packImportResetBtn.disabled = false;
      packImportSetDefault.disabled = false;
      packImportOverwrite.disabled = false;
      packImportOverwriteManual.disabled = !packImportOverwrite.checked;
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

  function appendPackOptions(select, selectedPackId = "") {
    if (
      !installedPacks.some((pack) => String(pack.id) === String(selectedPackId))
    ) {
      const missingOption = document.createElement("option");
      missingOption.value = String(selectedPackId || "");
      missingOption.textContent = selectedPackId
        ? `表情包不存在（${selectedPackId}）`
        : "请选择表情包";
      missingOption.selected = true;
      select.appendChild(missingOption);
    }
    for (const pack of installedPacks) {
      const packId = String(pack.id || "");
      const option = document.createElement("option");
      option.value = packId;
      option.selected = packId === String(selectedPackId);
      option.textContent = `${pack.name || packId} (${packId})`;
      select.appendChild(option);
    }
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
    updateRulesState();
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
    const movedRule = rulesList.children[toIndex];
    movedRule?.querySelector(".drag-handle")?.focus();
    updateRulesState(`规则已移至第 ${toIndex + 1} 位，记得保存更改。`);
  }

  function removeRule(index) {
    if (!rules[index] || rules[index].scope === "default") {
      return;
    }
    rules.splice(index, 1);
    renderRules();
    const nextRule = rulesList.children[Math.min(index, rules.length - 1)];
    nextRule
      ?.querySelector("button:not(:disabled), select:not(:disabled)")
      ?.focus();
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
        errors.push(`${position} 缺少规则标识，请删除后重新添加`);
      } else if (idSet.has(id)) {
        errors.push(`${position} 的规则标识重复，请删除后重新添加`);
      } else {
        idSet.add(id);
      }

      if (!["persona", "session", "default"].includes(scope)) {
        errors.push(`${position} 的匹配类型无效: ${scope || "(空)"}`);
      }
      if (!packId) {
        errors.push(`${position} 尚未选择表情包`);
      } else if (!installedPacks.some((pack) => String(pack.id) === packId)) {
        errors.push(`${position} 的表情包已不存在，请重新选择`);
      }

      if (scope === "default") {
        defaultCount += 1;
      }

      if (scope === "persona" || scope === "session") {
        if (!target) {
          errors.push(
            `${position} 尚未填写${scope === "persona" ? "人设" : "会话"}目标`,
          );
        } else {
          const key = `${scope}::${target}`;
          if (scopeTargetSet.has(key)) {
            errors.push(
              `${position} 与前面的规则重复匹配${
                scope === "persona" ? "人设" : "会话"
              }「${target}」`,
            );
          } else {
            scopeTargetSet.add(key);
          }
        }
      }
    });

    if (defaultCount !== 1) {
      errors.push("必须且仅能存在一条默认规则");
    }
    if (rules.length && rules[rules.length - 1]?.scope !== "default") {
      errors.push("默认规则必须位于最后");
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
    rulesValidation.replaceChildren();
    const heading = document.createElement("strong");
    heading.textContent = "规则存在问题，请先修复：";
    const errorList = document.createElement("ul");
    for (const error of errors) {
      const item = document.createElement("li");
      item.textContent = error;
      errorList.appendChild(item);
    }
    rulesValidation.append(heading, errorList);
    return false;
  }

  function renderRules() {
    rulesList.innerHTML = "";

    rules.forEach((rule, index) => {
      const isDefault = rule.scope === "default";
      const wrapper = document.createElement("div");
      wrapper.className = `rule-item${isDefault ? " default" : ""}`;
      wrapper.dataset.index = String(index);
      wrapper.setAttribute("role", "group");
      wrapper.setAttribute(
        "aria-label",
        isDefault ? "默认规则" : `第 ${index + 1} 条规则`,
      );

      const titleRow = document.createElement("div");
      titleRow.className = "rule-title-row";
      const title = document.createElement("div");
      const titleText = document.createElement("strong");
      titleText.textContent = isDefault ? "默认规则" : `规则 #${index + 1}`;
      title.appendChild(titleText);
      titleRow.appendChild(title);

      if (!isDefault) {
        const dragHandle = document.createElement("button");
        dragHandle.type = "button";
        dragHandle.className = "drag-handle";
        dragHandle.innerHTML =
          '<i class="fas fa-grip-vertical" aria-hidden="true"></i>';
        dragHandle.draggable = true;
        dragHandle.title = "拖动排序，或使用方向键上移、下移";
        dragHandle.setAttribute(
          "aria-label",
          `调整第 ${index + 1} 条规则顺序，使用上下方向键`,
        );
        dragHandle.addEventListener("keydown", (event) => {
          if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
          event.preventDefault();
          moveRuleToIndex(index, index + (event.key === "ArrowUp" ? -1 : 1));
        });
        titleRow.appendChild(dragHandle);
      }

      wrapper.appendChild(titleRow);

      const grid = document.createElement("div");
      grid.className = "rule-grid";

      const scopeField = document.createElement("div");
      scopeField.className = "field-row";
      scopeField.innerHTML = `
        <label for="rule-scope-${index}">匹配类型</label>
        <select id="rule-scope-${index}" data-role="scope">
          <option value="persona" ${
            rule.scope === "persona" ? "selected" : ""
          }>指定人设</option>
          <option value="session" ${
            rule.scope === "session" ? "selected" : ""
          }>指定会话</option>
          ${isDefault ? '<option value="default" selected>默认</option>' : ""}
        </select>
      `;

      const targetField = document.createElement("div");
      targetField.className = "field-row";
      const targetListId = `target-suggestions-${index}`;
      const targetPlaceholder =
        rule.scope === "persona"
          ? "选择或填写人设 ID"
          : rule.scope === "session"
          ? "选择或填写会话 ID"
          : "适用于其他所有情况";
      const targetSuggestions = getTargetSuggestions(rule.scope);
      const targetLabel = document.createElement("label");
      targetLabel.textContent =
        rule.scope === "persona"
          ? "人设目标"
          : rule.scope === "session"
          ? "会话目标"
          : "匹配范围";
      targetLabel.htmlFor = `rule-target-${index}`;
      const targetInputElement = document.createElement("input");
      targetInputElement.dataset.role = "target";
      targetInputElement.id = `rule-target-${index}`;
      targetInputElement.type = "text";
      targetInputElement.value = String(rule.target || "");
      targetInputElement.disabled = isDefault;
      targetInputElement.placeholder = targetPlaceholder;
      targetInputElement.setAttribute("list", targetListId);
      const targetList = document.createElement("datalist");
      targetList.id = targetListId;
      for (const suggestion of targetSuggestions) {
        const option = document.createElement("option");
        option.value = suggestion;
        targetList.appendChild(option);
      }
      targetField.append(targetLabel, targetInputElement, targetList);

      const packField = document.createElement("div");
      packField.className = "field-row";
      const packLabel = document.createElement("label");
      packLabel.textContent = "使用表情包";
      packLabel.htmlFor = `rule-pack-${index}`;
      const packSelectElement = document.createElement("select");
      packSelectElement.dataset.role = "pack";
      packSelectElement.id = `rule-pack-${index}`;
      appendPackOptions(packSelectElement, rule.pack_id);
      packField.append(packLabel, packSelectElement);

      grid.appendChild(scopeField);
      grid.appendChild(targetField);
      grid.appendChild(packField);
      wrapper.appendChild(grid);

      const actions = document.createElement("div");
      actions.className = "rule-actions";
      if (!isDefault) {
        actions.innerHTML = `
          <button type="button" class="ghost compact-btn" data-action="up" ${
            index === 0 ? "disabled" : ""
          } aria-label="上移第 ${index + 1} 条规则">↑ 上移</button>
          <button type="button" class="ghost compact-btn" data-action="down" ${
            index >= rules.length - 2 ? "disabled" : ""
          } aria-label="下移第 ${index + 1} 条规则">↓ 下移</button>
          <button type="button" class="danger compact-btn" data-action="remove" aria-label="删除第 ${
            index + 1
          } 条规则">删除规则</button>
        `;
        actions
          .querySelector('[data-action="up"]')
          .addEventListener("click", () => moveRuleToIndex(index, index - 1));
        actions
          .querySelector('[data-action="down"]')
          .addEventListener("click", () => moveRuleToIndex(index, index + 1));
      }
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

        // A different scope needs a new target, but the chosen pack remains valid.
        const firstSuggestion = getTargetSuggestions(selectedScope)[0] || "";
        rules[index].target = firstSuggestion;
        renderRules();
        rulesList.children[index]
          ?.querySelector('input[data-role="target"]')
          ?.focus();
      });

      targetInput.addEventListener("input", () => {
        updateRuleFromInput(index, "target", targetInput.value);
      });

      packSelect.addEventListener("change", () => {
        updateRuleFromInput(index, "pack_id", packSelect.value);
      });

      actions
        .querySelector('[data-action="remove"]')
        ?.addEventListener("click", () => {
          removeRule(index);
        });

      wrapper.addEventListener("dragstart", (event) => {
        if (isDefault || !event.target.closest(".drag-handle")) {
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
        event.stopPropagation();
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
    updateRulesState();
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
    savedRulesSnapshot = JSON.stringify(rules);
    rulesLoaded = true;
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
    if (rulesBusy || !rulesLoaded) return;
    if (!renderRulesValidation()) {
      updateRulesState("请先修复上方标记的问题。", "error");
      rulesValidation.focus();
      return;
    }
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

    rulesBusy = true;
    updateRulesState("正在保存规则…");
    setLoading(saveRulesBtn, "保存中...");
    let saveError = "";
    try {
      const response = await apiPost("settings/rules", { rules: payloadRules });
      rules = Array.isArray(response?.rules) ? response.rules : payloadRules;
      ensureDefaultRuleAtEnd(response?.default_pack_id || "");
      savedRulesSnapshot = JSON.stringify(rules);
      renderRules();
      addLog("规则保存成功");
      const rebuildPacks = Array.isArray(response?.semantic_rebuild_packs)
        ? response.semantic_rebuild_packs
        : [];
      for (const packId of rebuildPacks) {
        const shouldRebuild = await window.MemeUI.confirm({
          title: "为表情包建立向量索引",
          message: `表情包「${packId}」已切换，还没有与当前向量模型匹配的索引。现在建立索引后即可用于语义检索。`,
          confirmText: "建立索引",
        });
        if (!shouldRebuild) continue;
        try {
          await apiPost("semantic/rebuild-index", {
            pack_id: packId,
            force: true,
          });
          addLog(`资源包 ${packId} 的向量重建已完成`);
        } catch (rebuildError) {
          addLog(
            `资源包 ${packId} 向量重建失败: ${
              rebuildError?.message || String(rebuildError)
            }`,
            true,
          );
        }
      }
    } catch (error) {
      saveError = `保存失败：${
        error?.message || String(error)
      }。更改仍保留，可重试。`;
      addLog(`规则保存失败: ${error?.message || String(error)}`, true);
    } finally {
      rulesBusy = false;
      clearLoading(saveRulesBtn);
      updateRulesState(
        saveError || "规则已保存",
        saveError ? "error" : "success",
      );
    }
  }

  async function exportBackup() {
    setLoading(exportBackupBtn, "导出中...");
    setPackTransferResult(exportResult, "正在创建备份…");
    try {
      const outputDir = String(backupOutputDirInput.value || "").trim();
      const response = await apiPost("settings/backup/export", {
        output_dir: outputDir || undefined,
      });
      setPackTransferResult(
        exportResult,
        `已保存到服务器：${response.archive_path || ""}`,
        "success",
      );
      addLog(`备份导出成功: ${response.archive_path || ""}`);
    } catch (error) {
      setPackTransferResult(
        exportResult,
        `导出失败: ${error?.message || String(error)}`,
        "error",
      );
      addLog(`备份导出失败: ${error?.message || String(error)}`, true);
    } finally {
      clearLoading(exportBackupBtn);
    }
  }

  async function importBackup() {
    if (rulesBusy || packImportBusy) return;
    const file = backupFileInput.files?.[0];
    if (!file) {
      setPackTransferResult(importResult, "请先选择备份 zip 文件。", "error");
      backupFileInput.focus();
      addLog("请先选择备份 zip 文件", true);
      return;
    }
    if (!file.name.toLowerCase().endsWith(".zip")) {
      setPackTransferResult(
        importResult,
        "请选择 zip 格式的备份文件。",
        "error",
      );
      return;
    }
    const dirty = rulesLoaded && JSON.stringify(rules) !== savedRulesSnapshot;
    const confirmed = await window.MemeUI.confirm({
      title: "恢复全量备份",
      message: `将从「${file.name}」恢复表情包与运行时设置。${
        importOverwriteCheckbox.checked
          ? "全部现有表情包和向量索引会被备份替换，备份中没有的表情包也会被移除。"
          : "保留现有表情包，仅添加缺少的表情包；运行时设置仍会从备份恢复。"
      }${
        dirty
          ? "\n\n恢复成功后将重新加载规则，当前未保存的规则更改会丢失。"
          : ""
      }`,
      confirmText: "确认恢复",
      danger: true,
    });
    if (!confirmed) return;

    rulesBusy = true;
    updateRulesState("正在恢复备份…");
    setLoading(importBackupBtn, "导入中...");
    backupFileInput.disabled = true;
    importOverwriteCheckbox.disabled = true;
    setPackTransferResult(importResult, "正在恢复备份，请不要关闭页面…");
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
      setPackTransferResult(
        importResult,
        `恢复成功：${response?.restored_packs ?? 0} 个表情包`,
        "success",
      );
      backupFileInput.value = "";
      addLog(`备份导入成功，恢复 ${response?.restored_packs ?? 0} 个 pack`);
      try {
        await refreshPacksAndRules();
      } catch (refreshError) {
        setPackTransferResult(
          importResult,
          `备份已恢复，但设置刷新失败：${
            refreshError?.message || String(refreshError)
          }。请点击「重新加载」。`,
          "error",
        );
        addLog("备份已恢复，但设置刷新失败", true);
      }
    } catch (error) {
      setPackTransferResult(
        importResult,
        `恢复失败: ${error?.message || String(error)}`,
        "error",
      );
      addLog(`备份导入失败: ${error?.message || String(error)}`, true);
    } finally {
      rulesBusy = false;
      updateRulesState();
      backupFileInput.disabled = false;
      importOverwriteCheckbox.disabled = false;
      clearLoading(importBackupBtn);
    }
  }

  addRuleBtn.addEventListener("click", () => {
    rules.splice(Math.max(rules.length - 1, 0), 0, buildNewRule("persona"));
    renderRules();
    const targetInput = rulesList.children[
      Math.max(rules.length - 2, 0)
    ]?.querySelector('input[data-role="target"]');
    targetInput?.focus();
  });

  reloadRulesBtn.addEventListener("click", async () => {
    if (rulesBusy) return;
    if (rulesLoaded && JSON.stringify(rules) !== savedRulesSnapshot) {
      const confirmed = await window.MemeUI.confirm({
        title: "重新加载规则",
        message:
          "当前有未保存的更改。重新加载会丢弃这些更改，恢复到上次保存的规则。",
        confirmText: "丢弃并重新加载",
        danger: true,
      });
      if (!confirmed) return;
    }
    rulesBusy = true;
    updateRulesState("正在重新加载规则…");
    setLoading(reloadRulesBtn, "加载中...");
    let reloadError = "";
    try {
      await refreshPacksAndRules();
      addLog("规则已重新加载");
    } catch (error) {
      reloadError = `加载失败：${error?.message || String(error)}。请重试。`;
      addLog(`重新加载失败: ${error?.message || String(error)}`, true);
    } finally {
      rulesBusy = false;
      clearLoading(reloadRulesBtn);
      updateRulesState(
        reloadError || "规则已重新加载",
        reloadError ? "error" : "success",
      );
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
    if (packImportBusy) return;
    resetPackImportPreview();
    packImportDropzone.focus();
  });

  packImportOverwrite?.addEventListener("change", () => {
    packImportOverwriteManual.disabled = !packImportOverwrite.checked;
    if (!packImportOverwrite.checked) packImportOverwriteManual.checked = false;
  });

  packImportDropzone?.addEventListener("keydown", (event) => {
    if ((event.key === "Enter" || event.key === " ") && !packImportBusy) {
      event.preventDefault();
      packImportFile.click();
    }
  });

  window.addEventListener("beforeunload", (event) => {
    if (
      !allowPageLeave &&
      (packImportBusy ||
        window.MemeSettings?.dirty ||
        window.MemeSettings?.busy ||
        Boolean(saveRulesBtn.dataset.originalHtml) ||
        Boolean(importBackupBtn.dataset.originalHtml) ||
        (rulesLoaded && JSON.stringify(rules) !== savedRulesSnapshot))
    ) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  document.addEventListener(
    "click",
    async (event) => {
      const link = event.target.closest("a[href]");
      if (
        !link ||
        link.target === "_blank" ||
        link.hasAttribute("download") ||
        event.button !== 0 ||
        event.ctrlKey ||
        event.metaKey ||
        event.shiftKey ||
        event.altKey
      )
        return;
      const destination = new URL(link.href, window.location.href);
      if (
        destination.origin === window.location.origin &&
        destination.pathname === window.location.pathname &&
        destination.search === window.location.search &&
        destination.hash
      )
        return;
      const rulesDirty =
        rulesLoaded && JSON.stringify(rules) !== savedRulesSnapshot;
      if (!rulesDirty && !window.MemeSettings?.dirty) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (window.MemeSettings?.busy) {
        window.MemeUI.toast("设置正在处理，请稍候再离开。", "info");
        return;
      }
      const confirmed = await window.MemeUI.confirm({
        title: "离开设置页面",
        message: "设置还有未保存的更改。离开后这些更改会丢失。",
        confirmText: "丢弃更改并离开",
        danger: true,
      });
      if (confirmed) {
        allowPageLeave = true;
        window.location.assign(link.href);
      }
    },
    true,
  );

  packImportConfirmBtn?.addEventListener("click", () => {
    void confirmPackImport();
  });

  updateExportModeAppearance();
  resetPackImportPreview();

  rulesBusy = true;
  updateRulesState();
  let initialError = "";
  try {
    await refreshPacksAndRules();
    addLog("设置中心已就绪");
  } catch (error) {
    initialError = `加载失败：${
      error?.message || String(error)
    }。请点击「重新加载」重试。`;
    rulesList.replaceChildren();
    const placeholder = document.createElement("p");
    placeholder.className = "rules-placeholder";
    placeholder.textContent = "暂时无法读取规则。你的已保存设置未受影响。";
    rulesList.appendChild(placeholder);
    setPackTransferResult(
      transferCurrentPack,
      "暂时无法读取表情包，请重新加载。",
      "error",
    );
    exportPackDownloadBtn.disabled = true;
    addLog(`初始化失败: ${error?.message || String(error)}`, true);
  } finally {
    rulesBusy = false;
    updateRulesState(initialError, initialError ? "error" : "");
  }
}

void initSettingsPage().catch((error) => window.MemeUI.showPageError(error));
