async function initSettingsPage() {
  await window.AstrBotPluginPage.ready();

  const rulesList = document.getElementById("rules-list");
  const addPersonaRuleBtn = document.getElementById("add-persona-rule-btn");
  const addSessionRuleBtn = document.getElementById("add-session-rule-btn");
  const reloadRulesBtn = document.getElementById("reload-rules-btn");
  const saveRulesBtn = document.getElementById("save-rules-btn");

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

  function getPackOptions(selectedPackId = "") {
    return installedPacks
      .map((pack) => {
        const selectedAttr =
          String(pack.id) === String(selectedPackId) ? "selected" : "";
        return `<option value="${pack.id}" ${selectedAttr}>${pack.name || pack.id} (${pack.id})</option>`;
      })
      .join("");
  }

  function updateRuleFromInput(index, key, value) {
    if (!rules[index]) {
      return;
    }
    rules[index][key] = value;
  }

  function moveRule(index, delta) {
    const target = index + delta;
    if (
      index < 0 ||
      target < 0 ||
      index >= rules.length ||
      target >= rules.length
    ) {
      return;
    }
    if (rules[index].scope === "default" || rules[target].scope === "default") {
      return;
    }

    const cloned = [...rules];
    const tmp = cloned[index];
    cloned[index] = cloned[target];
    cloned[target] = tmp;
    rules = cloned;
    renderRules();
  }

  function removeRule(index) {
    if (!rules[index] || rules[index].scope === "default") {
      return;
    }
    rules.splice(index, 1);
    renderRules();
  }

  function renderRules() {
    rulesList.innerHTML = "";

    rules.forEach((rule, index) => {
      const isDefault = rule.scope === "default";
      const wrapper = document.createElement("div");
      wrapper.className = `rule-item${isDefault ? " default" : ""}`;

      const title = document.createElement("div");
      title.innerHTML = `<strong>${isDefault ? "默认规则（固定在最后）" : `规则 #${index + 1}`}</strong>`;
      wrapper.appendChild(title);

      const grid = document.createElement("div");
      grid.className = "rule-grid";

      const scopeField = document.createElement("div");
      scopeField.className = "field-row";
      scopeField.innerHTML = `
        <label>scope</label>
        <select data-role="scope">
          <option value="persona" ${rule.scope === "persona" ? "selected" : ""}>persona</option>
          <option value="session" ${rule.scope === "session" ? "selected" : ""}>session</option>
          <option value="default" ${rule.scope === "default" ? "selected" : ""}>default</option>
        </select>
      `;

      const targetField = document.createElement("div");
      targetField.className = "field-row";
      targetField.innerHTML = `
        <label>target</label>
        <input data-role="target" type="text" value="${rule.target || ""}" ${isDefault ? "disabled" : ""} placeholder="persona_id 或 session_id" />
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
        <button type="button" class="ghost" data-action="up" ${isDefault || index === 0 ? "disabled" : ""}>上移</button>
        <button type="button" class="ghost" data-action="down" ${isDefault || index >= rules.length - 2 ? "disabled" : ""}>下移</button>
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
        updateRuleFromInput(index, "scope", scopeSelect.value);
        if (scopeSelect.value === "default") {
          delete rules[index].target;
        }
        renderRules();
      });

      targetInput.addEventListener("input", () => {
        updateRuleFromInput(index, "target", targetInput.value);
      });

      packSelect.addEventListener("change", () => {
        updateRuleFromInput(index, "pack_id", packSelect.value);
      });

      actions
        .querySelector('[data-action="up"]')
        .addEventListener("click", () => {
          moveRule(index, -1);
        });
      actions
        .querySelector('[data-action="down"]')
        .addEventListener("click", () => {
          moveRule(index, 1);
        });
      actions
        .querySelector('[data-action="remove"]')
        .addEventListener("click", () => {
          removeRule(index);
        });

      rulesList.appendChild(wrapper);
    });
  }

  async function refreshPacksAndRules() {
    const [packsResponse, rulesResponse] = await Promise.all([
      apiGet("packs"),
      apiGet("settings/rules"),
    ]);

    installedPacks = Array.isArray(packsResponse?.packs)
      ? packsResponse.packs
      : [];
    rules = Array.isArray(rulesResponse?.rules) ? rulesResponse.rules : [];
    ensureDefaultRuleAtEnd(rulesResponse?.default_pack_id || "");
    renderRules();
  }

  function buildNewRule(scope) {
    return {
      id: `${scope}-${Date.now()}`,
      scope,
      target: "",
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
      const overwrite = importOverwriteCheckbox.checked ? "1" : "0";
      const endpoint = `settings/backup/import?overwrite=${overwrite}`;
      const response = await window.AstrBotPluginPage.upload(endpoint, file);
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

  addPersonaRuleBtn.addEventListener("click", () => {
    rules.splice(Math.max(rules.length - 1, 0), 0, buildNewRule("persona"));
    renderRules();
  });

  addSessionRuleBtn.addEventListener("click", () => {
    rules.splice(Math.max(rules.length - 1, 0), 0, buildNewRule("session"));
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
