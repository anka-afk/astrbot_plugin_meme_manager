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

  const logList = document.getElementById("log-list");

  let installedPacks = [];
  let rules = [];
  let dragRuleIndex = -1;
  let personaTargets = [];
  let sessionTargets = [];

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

  async function refreshPacksAndRules() {
    const [packsResponse, rulesResponse, targetsResponse] = await Promise.all([
      apiGet("packs"),
      apiGet("settings/rules"),
      apiGet("settings/targets"),
    ]);

    installedPacks = Array.isArray(packsResponse?.packs)
      ? packsResponse.packs
      : [];
    rules = Array.isArray(rulesResponse?.rules) ? rulesResponse.rules : [];
    personaTargets = Array.isArray(targetsResponse?.persona_targets)
      ? targetsResponse.persona_targets
      : [];
    sessionTargets = Array.isArray(targetsResponse?.session_targets)
      ? targetsResponse.session_targets
      : [];
    ensureDefaultRuleAtEnd(rulesResponse?.default_pack_id || "");
    renderRules();
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

  try {
    await refreshPacksAndRules();
    addLog("设置中心已就绪");
  } catch (error) {
    addLog(`初始化失败: ${error?.message || String(error)}`, true);
  }
}

void initSettingsPage();
