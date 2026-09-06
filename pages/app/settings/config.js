import { openStorageGuide } from "./storage-guide.js";

const configState = { dirty: false, busy: false };
window.MemeSettings = configState;

async function initPluginConfig() {
  await window.AstrBotPluginPage.ready();
  const api = window.AstrBotPluginPage;
  const form = document.getElementById("plugin-config-form");
  const sections = document.getElementById("config-sections");
  const search = document.getElementById("settings-search");
  const save = document.getElementById("save-config-btn");
  const reload = document.getElementById("reload-config-btn");
  const resetPrompts = document.getElementById("reset-prompts-btn");
  const status = document.getElementById("config-save-status");
  const notice = document.getElementById("config-load-status");
  const tabs = [...document.querySelectorAll("[data-settings-tab]")];
  const panels = [...document.querySelectorAll("[data-settings-panel]")];
  const categories = {
    sending: ["表情发送", "控制表情何时出现，以及它在聊天中的呈现方式。"],
    models: ["语义与模型", "让模型读懂表情，再为对话选出合适的一张。"],
    collect: ["自动收集", "从聊天中收集表情，用来源范围和频率控制收集节奏。"],
    storage: ["存储与下载", "连接图床、管理同步，并设置资源下载方式。"],
    prompts: [
      "提示词模板",
      "调整分类标签模式下，提供给回复模型的表情使用说明。",
    ],
    matching: [
      "标签与匹配",
      "设置表情标签的识别方式，让模型输出与表情分类对应。",
    ],
  };
  const groups = [
    {
      id: "appearance",
      category: "sending",
      title: "出现时机",
      description: "少一点打扰，多一点恰好。",
      prefixes: [
        "generation.trigger.",
        "generation.emotion.probability",
        "generation.emotion.max_memes_per_message",
      ],
    },
    {
      id: "delivery",
      category: "sending",
      title: "发送方式",
      description: "根据聊天平台选择适合的图片和消息格式。",
      prefixes: ["generation.message."],
    },
    {
      id: "semantic",
      category: "models",
      title: "语义检索",
      description: "启用后，优先从已完成语义化的表情包中检索。",
      prefixes: ["semantic.enabled", "semantic.top_k", "semantic.min_score"],
    },
    {
      id: "semantic-models",
      category: "models",
      title: "语义化模型",
      description: "视觉模型生成图片描述，向量模型用于相似度检索。",
      prefixes: [
        "semantic.vision_provider_id",
        "semantic.embedding_provider_id",
      ],
    },
    {
      id: "emotion-model",
      category: "models",
      title: "情感辅助",
      description: "可由单独的模型负责选图，默认复用当前对话的回复模型。",
      prefixes: ["generation.emotion.llm."],
    },
    {
      id: "collect",
      category: "collect",
      title: "收集偏好",
      description: "收集开关、视觉模型和收集范围。",
      prefixes: [
        "auto_collect.enabled",
        "auto_collect.vision_provider_id",
        "auto_collect.scope",
        "auto_collect.target_pack_id",
      ],
    },
    {
      id: "collect-limits",
      category: "collect",
      title: "频率与质量",
      description: "减少不必要的模型调用，保留更有把握的结果。",
      prefixes: ["auto_collect."],
    },
    {
      id: "storage",
      category: "storage",
      title: "图床服务",
      description: "仅需填写当前服务的参数，其他服务的设置会保留。",
      prefixes: ["storage.provider"],
    },
    {
      id: "r2",
      category: "storage",
      title: "Cloudflare R2",
      prefixes: ["storage.providers.cloudflare_r2."],
      provider: "cloudflare_r2",
    },
    {
      id: "webdav",
      category: "storage",
      title: "WebDAV",
      prefixes: ["storage.providers.webdav."],
      provider: "webdav",
    },
    {
      id: "stardots",
      category: "storage",
      title: "StarDots",
      prefixes: ["storage.providers.stardots."],
      provider: "stardots",
    },
    {
      id: "downloads",
      category: "storage",
      title: "同步与下载",
      prefixes: ["sync.", "community."],
    },
    {
      id: "prompts",
      category: "prompts",
      title: "分类提示词",
      description:
        "前缀定义能力与发送格式，后缀规定内部资料边界和图片理解；中间自动插入分类列表。",
      prefixes: ["generation.prompt.head", "generation.prompt.tail"],
    },
    {
      id: "quantity-guidance",
      category: "prompts",
      title: "表情回复数量",
      description:
        "控制何时配图和单条回复选多少张，适用于分类、语义和情感辅助选图，与发送阶段的硬上限独立。",
      prefixes: [
        "generation.prompt.quantity_guidance_enabled",
        "generation.prompt.quantity_guidance",
      ],
    },
    {
      id: "category-example",
      category: "prompts",
      title: "分类使用示例",
      description:
        "用一组虚构分类演示如何选取分类键并生成标记。实际回复仍使用当前表情包的分类。",
      prefixes: [
        "generation.prompt.category_example_enabled",
        "generation.prompt.category_example",
      ],
    },
    {
      id: "reply-example",
      category: "prompts",
      title: "回复形式示例",
      description:
        "演示图文结合、纯文字和纯表情三种形式。可单独开启，也可与分类使用示例一起使用。",
      prefixes: [
        "generation.prompt.reply_example_enabled",
        "generation.prompt.reply_example",
      ],
    },
    {
      id: "matching",
      category: "matching",
      title: "标签识别",
      description: "识别回复中的表情标记，并清理无法匹配的标签。",
      prefixes: ["generation.markup."],
    },
    {
      id: "category-matching",
      category: "matching",
      title: "分类匹配",
      description: "自动使用当前会话所选表情包的分类，无需维护额外词表。",
      prefixes: ["generation.matching."],
    },
  ];
  const labels = {
    "generation.trigger.scope": "应用范围",
    "generation.emotion.probability": "表情出现概率（%）",
    "generation.emotion.llm.provider_id": "情感辅助模型",
    "semantic.top_k": "每次检索候选数",
    "semantic.min_score": "最低相似度",
    "semantic.vision_provider_id": "图片描述模型",
    "semantic.embedding_provider_id": "向量模型",
    "auto_collect.vision_provider_id": "收集识别模型",
    "auto_collect.target_pack_id": "收集到的表情包",
    "generation.message.mixed_probability": "图文同条回复概率（%）",
  };
  const hints = {
    "generation.emotion.probability":
      "在语义检索模式下，每轮按此概率决定是否让回复模型选图。",
    "generation.emotion.llm.enabled":
      "将选图交给情感辅助模型，回复模型只负责对话。",
    "generation.emotion.llm.provider_id":
      "留空时复用当前回复模型，也可以单独指定。",
    "generation.trigger.scope":
      "选择为哪些模型回复附加表情。指令和固定文本回复不受影响。",
    "semantic.enabled":
      "由回复模型检索表情；启用情感辅助后，改由辅助模型选图。",
    "semantic.embedding_provider_id":
      "留空时自动选择可用向量模型。更换模型后，可到语义化页面检查并重建索引。",
    "auto_collect.target_pack_id":
      "留空时跟随人设或会话规则。启用语义检索时，先进入待语义化列表。",
  };
  const optionLabels = {
    cloudflare_r2: "Cloudflare R2",
    stardots: "StarDots",
    webdav: "WebDAV",
    only_chat_llm: "仅普通聊天回复",
    chat_and_plugin_llm: "普通聊天与插件触发的回复",
  };
  const fields = new Map();
  const controls = new Map();
  const changes = new Map();
  let revision = "";
  let active = "rules";
  let providers = { chat: [], embedding: [] };
  let packs = [];
  let loaded = false;

  function updateView() {
    const query = search.value.trim().toLocaleLowerCase();
    resetPrompts.hidden = active !== "prompts" || Boolean(query);
    const configVisible = Boolean(query) || Boolean(categories[active]);
    for (const panel of panels) {
      panel.hidden = configVisible
        ? panel.dataset.settingsPanel !== "config"
        : panel.dataset.settingsPanel !== active;
    }
    for (const tab of tabs) {
      if (!query && tab.dataset.settingsTab === active)
        tab.setAttribute("aria-current", "page");
      else tab.removeAttribute("aria-current");
    }
    const title = query
      ? ["搜索结果", "按名称或说明查找全部配置，也可搜索尚未启用的服务。"]
      : categories[active];
    if (title) {
      document.getElementById("config-section-title").textContent = title[0];
      document.getElementById("config-section-description").textContent =
        title[1];
    }
    const selectedProvider =
      changes.get("storage.provider") ?? fields.get("storage.provider")?.value;
    let visibleCount = 0;
    for (const group of groups) {
      const card = document.getElementById(`config-group-${group.id}`);
      if (!card) continue;
      let matches = 0;
      for (const row of card.querySelectorAll(".config-field")) {
        row.hidden = Boolean(query) && !row.dataset.search.includes(query);
        if (!row.hidden) matches++;
      }
      card.hidden = query
        ? !matches
        : group.category !== active ||
          Boolean(group.provider && group.provider !== selectedProvider);
      if (!card.hidden) visibleCount += matches;
    }
    document.getElementById("config-search-empty").hidden =
      !loaded || visibleCount > 0;
  }

  function updateState(message = "", error = false) {
    configState.dirty = changes.size > 0;
    save.disabled = !loaded || !changes.size || configState.busy;
    reload.disabled = configState.busy;
    resetPrompts.disabled = !loaded || configState.busy;
    sections.inert = configState.busy;
    form.setAttribute("aria-busy", String(configState.busy));
    save.textContent = configState.busy ? "正在处理…" : "保存并应用";
    status.textContent =
      message ||
      (changes.size
        ? `有 ${changes.size} 项更改未保存`
        : loaded
        ? "所有更改已保存"
        : "尚未读取配置");
    status.classList.toggle("error", error);
    for (const tab of tabs) {
      const dot = tab.querySelector(".settings-dirty-dot");
      if (dot)
        dot.hidden = ![...changes.keys()].some(
          (path) => fields.get(path)?.category === tab.dataset.settingsTab,
        );
    }
  }

  function renderFields(snapshot) {
    fields.clear();
    controls.clear();
    sections.replaceChildren();
    for (const group of groups) {
      const card = document.createElement("section");
      card.className = "panel config-card";
      card.id = `config-group-${group.id}`;
      const header = document.createElement("header");
      header.className = "config-card-header";
      const heading = document.createElement("h3");
      heading.id = `${card.id}-title`;
      card.setAttribute("aria-labelledby", heading.id);
      heading.textContent = group.title;
      header.append(heading);
      if (group.provider) {
        const guide = document.createElement("button");
        guide.type = "button";
        guide.className = "ghost storage-guide-trigger";
        guide.textContent = "图床配置教程";
        guide.setAttribute("aria-haspopup", "dialog");
        guide.addEventListener("click", () => openStorageGuide(group.provider));
        header.append(guide);
      }
      if (group.description) {
        const description = document.createElement("p");
        description.className = "config-card-description";
        description.textContent = group.description;
        header.append(description);
      }
      const body = document.createElement("div");
      body.className = "config-card-body";
      card.append(header, body);
      sections.append(card);
    }
    for (const field of snapshot.fields) {
      const assigned = groups.find((item) =>
        item.prefixes.some((prefix) =>
          prefix.endsWith(".")
            ? field.path.startsWith(prefix)
            : field.path === prefix,
        ),
      );
      if (!assigned) throw new Error("配置分类已更新，请刷新页面后重试");
      field.category = assigned.category;
      fields.set(field.path, field);
      const row = document.createElement("div");
      row.className = `config-field${
        field.type === "bool" ? " config-switch-field" : ""
      }${["text", "list"].includes(field.type) ? " config-field-wide" : ""}`;
      row.dataset.search = [
        labels[field.path] || field.label,
        field.hint,
        field.path,
        ...field.groups,
        assigned.title,
      ]
        .join(" ")
        .toLocaleLowerCase();
      const copy = document.createElement("div");
      const label = document.createElement("label");
      label.htmlFor = `config-${field.path.replaceAll(".", "-")}`;
      label.textContent = labels[field.path] || field.label;
      copy.append(label);
      const help = document.createElement("p");
      help.id = `${label.htmlFor}-hint`;
      help.className = "config-field-hint";
      help.textContent = hints[field.path] || field.hint;
      if (field.type === "list") help.textContent += " 每行填写一项。";
      if (field.secret)
        help.textContent = field.configured
          ? "已配置。留空保留原值，输入新值可替换。"
          : "尚未配置。保存后不会回显密钥。";
      copy.append(help);
      row.append(copy);
      const controlBox = document.createElement("div");
      controlBox.className = "config-control";
      let control;
      if (field.type === "bool") {
        control = document.createElement("input");
        control.type = "checkbox";
        control.className = "config-switch";
        control.setAttribute("role", "switch");
        control.checked = Boolean(field.value);
      } else if (
        field.options.length ||
        field.special.startsWith("select_provider") ||
        field.path === "auto_collect.target_pack_id"
      ) {
        control = document.createElement("select");
        if (field.options.length) {
          for (const option of field.options)
            control.add(new Option(optionLabels[option] || option, option));
        } else {
          let emptyLabel = "请选择模型";
          let options = providers.chat.map((provider) => [
            provider.model ? `${provider.id} · ${provider.model}` : provider.id,
            provider.id,
          ]);
          if (field.special === "select_provider_embedding") {
            emptyLabel = "自动选择可用向量模型";
            options = providers.embedding.map((provider) => [
              provider.model
                ? `${provider.id} · ${provider.model}`
                : provider.id,
              provider.id,
            ]);
          } else if (field.path === "generation.emotion.llm.provider_id")
            emptyLabel = "跟随当前对话的回复模型";
          else if (field.path === "auto_collect.target_pack_id") {
            emptyLabel = "跟随人设与会话规则";
            options = packs.map((pack) => [
              `${pack.name || pack.id} (${pack.id})`,
              pack.id,
            ]);
          }
          control.add(new Option(emptyLabel, ""));
          for (const [name, value] of options)
            control.add(new Option(name, value));
        }
        if (
          field.value &&
          ![...control.options].some((item) => item.value === field.value)
        )
          control.add(new Option(`${field.value}（当前不可用）`, field.value));
        control.value = field.value;
      } else if (["text", "list"].includes(field.type)) {
        control = document.createElement("textarea");
        control.rows = field.type === "text" ? 7 : 4;
        control.value =
          field.type === "list" ? field.value.join("\n") : field.value;
        control.spellcheck = false;
      } else {
        control = document.createElement("input");
        control.type = field.secret
          ? "password"
          : ["int", "float"].includes(field.type)
          ? "number"
          : "text";
        control.value = field.value;
        if (["int", "float"].includes(field.type)) {
          control.required = true;
          if (field.path !== "generation.emotion.max_memes_per_message") {
            control.min =
              field.bounds.min ?? (/(top_k|\.timeout)$/.test(field.path) ? 1 : 0);
          }
          const max =
            field.bounds.max ??
            (/(min_score|min_meme_confidence|min_category_confidence)$/.test(
              field.path,
            )
              ? 1
              : null);
          if (max !== null) control.max = max;
          control.step = field.type === "int" ? "1" : "any";
        }
        if (field.secret) {
          control.autocomplete = "new-password";
          control.placeholder = field.configured
            ? "已保存 · 输入可替换"
            : "请输入";
        }
      }
      control.id = label.htmlFor;
      control.setAttribute("aria-describedby", help.id);
      control.dataset.configPath = field.path;
      controlBox.append(control);
      controls.set(field.path, control);
      if (field.secret) {
        const clear = document.createElement("button");
        clear.type = "button";
        clear.className = "ghost config-secret-clear";
        clear.textContent = "清除";
        clear.setAttribute("aria-label", `清除${label.textContent}`);
        clear.addEventListener("click", () => {
          if (changes.has(field.path) && changes.get(field.path) === "") {
            changes.delete(field.path);
            clear.textContent = "清除";
            control.placeholder = field.configured
              ? "已保存 · 输入可替换"
              : "请输入";
          } else {
            control.value = "";
            changes.set(field.path, "");
            clear.textContent = "撤销";
            control.placeholder = "保存后清除";
          }
          clear.setAttribute(
            "aria-label",
            `${clear.textContent === "撤销" ? "撤销清除" : "清除"}${
              label.textContent
            }`,
          );
          updateState();
        });
        controlBox.append(clear);
      }
      row.append(controlBox);
      document
        .querySelector(`#config-group-${assigned.id} .config-card-body`)
        .append(row);
    }
    updateView();
  }

  async function loadConfig() {
    configState.busy = true;
    notice.hidden = false;
    notice.classList.remove("error");
    notice.textContent = "正在读取配置…";
    updateState("正在读取配置…");
    try {
      const [snapshot, packResult] = await Promise.all([
        api.apiGet("settings/config"),
        api.apiGet("packs").catch(() => null),
      ]);
      providers = snapshot.providers || providers;
      if (packResult) packs = packResult.packs || [];
      revision = snapshot.revision;
      changes.clear();
      renderFields(snapshot);
      loaded = true;
      notice.hidden = true;
      updateView();
    } catch (error) {
      notice.textContent = `读取失败：${
        error.message || String(error)
      }。可点击“重新加载”重试。`;
      notice.classList.add("error");
    } finally {
      configState.busy = false;
      updateState();
    }
  }

  for (const tab of tabs) {
    tab.addEventListener("click", () => {
      active = tab.dataset.settingsTab;
      search.value = "";
      history.replaceState(
        null,
        "",
        `${location.pathname}${location.search}#${active}`,
      );
      updateView();
      window.scrollTo({ top: 0, behavior: "instant" });
    });
  }
  search.addEventListener("input", updateView);
  const initialTab = location.hash.slice(1);
  if (tabs.some((tab) => tab.dataset.settingsTab === initialTab))
    active = initialTab;
  window.addEventListener("hashchange", () => {
    const target = location.hash.slice(1);
    if (tabs.some((tab) => tab.dataset.settingsTab === target)) {
      active = target;
      search.value = "";
      updateView();
    }
  });
  form.addEventListener("input", (event) => {
    const control = event.target;
    const path = control.dataset.configPath;
    const field = fields.get(path);
    if (!field) return;
    let value = control.value;
    if (field.type === "bool") value = control.checked;
    else if (["int", "float"].includes(field.type))
      value = control.valueAsNumber;
    else if (field.type === "list")
      value = control.value
        .split(/\r?\n/)
        .map((item) => item.trim())
        .filter(Boolean);
    if (field.secret && value === "") changes.delete(path);
    else if (JSON.stringify(value) === JSON.stringify(field.value))
      changes.delete(path);
    else changes.set(path, value);
    if (field.secret) {
      const clear = control.parentElement.querySelector("button");
      clear.textContent = "清除";
      clear.setAttribute("aria-label", `清除${labels[path] || field.label}`);
      control.placeholder = field.configured ? "已保存 · 输入可替换" : "请输入";
    }
    updateState();
    if (path === "storage.provider") updateView();
  });
  resetPrompts.addEventListener("click", async () => {
    if (!loaded || configState.busy) return;
    const confirmed = await window.MemeUI.confirm({
      title: "恢复默认模板",
      message:
        "将前缀、后缀和两组示例恢复为当前版本的默认内容，保留各示例的开关选择。你可以检查、修改后再保存。",
      confirmText: "填入默认模板",
    });
    if (!confirmed || configState.busy) return;
    for (const [path, field] of fields) {
      if (!path.startsWith("generation.prompt.") || field.type !== "text")
        continue;
      const control = controls.get(path);
      control.value = field.default;
      control.dispatchEvent(new Event("input", { bubbles: true }));
    }
  });
  reload.addEventListener("click", async () => {
    if (
      changes.size &&
      !(await window.MemeUI.confirm({
        title: "重新加载配置",
        message: "这会丢弃当前未保存的插件配置。使用规则的更改会保留。",
        confirmText: "重新加载",
        danger: true,
      }))
    )
      return;
    await loadConfig();
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!changes.size || configState.busy) return;
    for (const path of changes.keys()) {
      const control = controls.get(path);
      if (!control.checkValidity()) {
        active = fields.get(path).category;
        search.value = labels[path] || fields.get(path).label;
        updateView();
        control.focus();
        control.reportValidity();
        updateState(
          `请检查「${labels[path] || fields.get(path).label}」的填写内容。`,
          true,
        );
        return;
      }
    }
    configState.busy = true;
    notice.hidden = true;
    updateState("正在保存并应用设置…");
    let message = "";
    let failed = false;
    try {
      const result = await api.apiPost("settings/config", {
        revision,
        changes: Object.fromEntries(changes),
      });
      revision = result.revision;
      changes.clear();
      renderFields(result);
      message = result.message || "设置已保存并生效。";
      if (result.applied === false) {
        notice.hidden = false;
        notice.classList.add("error");
        notice.textContent = message;
      }
    } catch (error) {
      failed = true;
      message = error.message || String(error);
      notice.hidden = false;
      notice.classList.add("error");
      notice.textContent = message;
    } finally {
      configState.busy = false;
      updateState(message, failed);
    }
  });
  updateView();
  await loadConfig();
}

void initPluginConfig().catch((error) => window.MemeUI.showPageError(error));
