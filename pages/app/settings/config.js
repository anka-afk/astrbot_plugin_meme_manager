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
  const modePanel = document.getElementById("selection-mode-panel");
  const configDialog = document.getElementById("config-flow-dialog");
  const configDialogBody = document.getElementById("config-flow-dialog-body");
  const configDialogSave = document.getElementById("config-flow-dialog-save");
  const configDialogReset = document.getElementById("config-flow-dialog-reset");
  const configDialogStatus = document.getElementById(
    "config-flow-dialog-status",
  );
  const rulesDialog = document.getElementById("rules-flow-dialog");
  const rulesDialogBody = document.getElementById("rules-flow-dialog-body");
  const rulesPanel = document.querySelector('[data-settings-panel="rules"]');
  const modeInputs = [...form.querySelectorAll('input[name="selection-mode"]')];
  const modePaths = ["semantic.enabled", "generation.emotion.llm.enabled"];
  const workflow = document.getElementById("mode-workflow");
  const workflowSteps = document.getElementById("mode-workflow-steps");
  const collectionWorkflow = document.getElementById("collection-workflow");
  const collectionSteps = [
    [
      "来源与目标",
      "符合范围的聊天图片进入收集；目标包决定分类依据和收录位置。",
      "collect",
      [
        ["enabled", "收集开关"],
        ["scope", "来源范围"],
        ["target_pack_id", "目标表情包"],
      ],
    ],
    [
      "采样与间隔",
      "先限制每条消息的图片数，再抽样并检查来源冷却。抽样概率仅作用于平台表情；普通图片首次只记录，重复出现后逐步提高识别概率。",
      "collect-limits",
      [
        ["max_images_per_message", "单条图片上限"],
        ["sampling_probability", "平台表情抽样"],
        ["cooldown_seconds", "来源冷却"],
      ],
    ],
    [
      "视觉识别",
      "待审箱有容量才继续；有缓存时复用，否则检查每日额度并调用支持图片输入的模型。",
      "collect-recognition",
      [
        ["pending_limit", "待审核容量"],
        ["daily_recognition_limit", "每日识别上限"],
        ["vision_provider_id", "视觉模型"],
      ],
    ],
    [
      "质量与分类",
      "非表情或表情置信度不足时不收录；分类置信度决定自动入包时是否归入待分类。",
      "collect-quality",
      [
        ["min_meme_confidence", "表情置信度"],
        ["min_category_confidence", "分类置信度"],
      ],
    ],
    [
      "审核与入包",
      "按审核开关决定由你确认，还是自动接收入包。",
      "collect-review",
      [["manual_review", "人工审核"]],
    ],
  ];
  for (const [title, description, group, settings] of collectionSteps) {
    const item = document.createElement("li");
    const node = document.createElement("div");
    node.className = "workflow-node collection-workflow-node";
    const heading = document.createElement("strong");
    heading.textContent = title;
    const detail = document.createElement("span");
    detail.textContent = description;
    node.append(heading, detail);
    const links = document.createElement("div");
    links.className = "collection-workflow-links";
    for (const [path, label] of settings) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "ghost";
      button.dataset.workflowTarget = group;
      button.dataset.workflowField = `auto_collect.${path}`;
      const icon = document.createElement("i");
      icon.className = "fas fa-gear";
      icon.setAttribute("aria-hidden", "true");
      button.append(icon, document.createTextNode(` ${label}`));
      links.append(button);
    }
    node.append(links);
    item.append(node);
    document.getElementById("collection-workflow-steps").append(item);
  }
  const workflows = {
    "category-native": [
      ["选择表情包", "按使用规则为当前会话选择表情包"],
      ["决定是否配图", "按触发范围、概率和数量控制"],
      ["生成分类标签", "回复模型根据提示词和示例输出标签"],
      ["匹配并取图", "识别标签，匹配当前图包分类并随机取图"],
      ["发送表情", "按平台消息格式发送图片"],
    ],
    "category-auxiliary": [
      ["选择表情包", "按使用规则为当前会话选择表情包"],
      ["决定是否配图", "按触发范围、概率和数量控制"],
      ["辅助模型选分类", "根据回复选择分类标签"],
      ["匹配并取图", "识别标签，匹配当前图包分类并随机取图"],
      ["发送表情", "按平台消息格式发送图片"],
    ],
    "semantic-tool": [
      ["选择表情包", "按使用规则为当前会话选择表情包"],
      ["决定是否配图", "按触发范围、概率和数量控制"],
      ["发起搜索", "回复模型按需调用搜索工具"],
      ["检索并选图", "从索引中查找候选并选择图片"],
      ["发送表情", "按平台消息格式发送图片"],
    ],
    "semantic-auxiliary": [
      ["选择表情包", "按使用规则为当前会话选择表情包"],
      ["决定是否配图", "按触发范围、概率和数量控制"],
      ["辅助模型生成检索词", "根据回复提炼选图意图"],
      ["检索并选图", "从索引中查找候选并选择图片"],
      ["发送表情", "按平台消息格式发送图片"],
    ],
  };
  const workspaceModes = {
    "category-native": {
      primary: [
        "prompts",
        "category-example",
        "reply-example",
        "matching",
        "category-matching",
      ],
      fallback: [],
    },
    "category-auxiliary": {
      primary: ["emotion-model", "matching", "category-matching"],
      fallback: [],
    },
    "semantic-tool": {
      primary: ["semantic", "semantic-models"],
      fallback: [
        "prompts",
        "category-example",
        "reply-example",
        "matching",
        "category-matching",
      ],
    },
    "semantic-auxiliary": {
      primary: ["semantic", "semantic-models", "emotion-model"],
      fallback: ["matching", "category-matching"],
    },
  };
  const workspaceCommon = ["quantity-guidance", "appearance", "delivery"];
  const workspaceCollection = [
    "collect",
    "collect-limits",
    "collect-recognition",
    "collect-quality",
    "collect-review",
  ];
  const workspaceStorage = [
    "storage",
    "r2",
    "webdav",
    "stardots",
    "lsky",
    "downloads",
    "sync",
    "preview",
  ];
  const categories = {
    sending: ["表情发送", "控制表情何时出现，以及它在聊天中的呈现方式。"],
    models: ["模式", "选择模式，并从流程图打开相关设置。"],
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
      modes: ["semantic-tool", "semantic-auxiliary"],
      category: "models",
      title: "语义检索",
      description: "启用后，优先从已完成语义化的表情包中检索。",
      prefixes: ["semantic.enabled", "semantic.top_k", "semantic.min_score"],
    },
    {
      id: "semantic-models",
      modes: ["semantic-tool", "semantic-auxiliary"],
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
      modes: ["category-auxiliary", "semantic-auxiliary"],
      category: "models",
      title: "情感辅助",
      description: "可由单独的模型负责选图，默认复用当前对话的回复模型。",
      prefixes: ["generation.emotion.llm."],
    },
    {
      id: "collect",
      category: "collect",
      title: "来源与目标",
      description: "决定是否收集、从哪里收集，以及使用哪个表情包的分类。",
      prefixes: [
        "auto_collect.enabled",
        "auto_collect.scope",
        "auto_collect.target_pack_id",
      ],
    },
    {
      id: "collect-limits",
      category: "collect",
      title: "采样与间隔",
      description: "限制消息中的候选图片数、平台表情抽样及同一来源的识别间隔。",
      prefixes: [
        "auto_collect.max_images_per_message",
        "auto_collect.sampling_probability",
        "auto_collect.cooldown_seconds",
      ],
    },
    {
      id: "collect-recognition",
      category: "collect",
      title: "视觉识别",
      description:
        "待审箱满时停止提交候选；没有缓存时，按每日额度调用视觉模型。",
      prefixes: [
        "auto_collect.pending_limit",
        "auto_collect.daily_recognition_limit",
        "auto_collect.vision_provider_id",
      ],
    },
    {
      id: "collect-quality",
      category: "collect",
      title: "质量与分类",
      description:
        "表情置信度控制是否收录；分类置信度控制自动入包时的分类去向。",
      prefixes: [
        "auto_collect.min_meme_confidence",
        "auto_collect.min_category_confidence",
      ],
    },
    {
      id: "collect-review",
      category: "collect",
      title: "审核与入包",
      description:
        "开启后由你确认入包；关闭后仅新收集图片自动入包，已有待审核图片仍需处理。",
      prefixes: ["auto_collect.manual_review"],
    },
    {
      id: "storage",
      category: "storage",
      title: "图床连接",
      description:
        "先选择服务，再填写下方连接参数。切换服务会保留已填写的配置。",
      icon: "fa-cloud",
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
      id: "lsky",
      category: "storage",
      title: "兰空图床（开源版 2.x）",
      prefixes: ["storage.providers.lsky."],
      provider: "lsky",
    },
    {
      id: "downloads",
      category: "storage",
      title: "资源下载",
      description: "用于从 GitHub 安装社区表情包。",
      icon: "fa-download",
      prefixes: ["community."],
    },
    {
      id: "sync",
      category: "storage",
      title: "同步状态",
      description: "控制管理页同步状态的缓存时长。",
      icon: "fa-arrows-rotate",
      prefixes: ["sync."],
    },
    {
      id: "preview",
      category: "storage",
      title: "页面预览",
      description: "控制浏览图片时的加载并发。",
      icon: "fa-images",
      prefixes: ["webui."],
    },
    {
      id: "prompts",
      modes: ["category-native"],
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
      modes: ["category-native"],
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
      modes: ["category-native"],
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
      modes: ["category-native", "category-auxiliary"],
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
      "留空时跟随人设或会话规则；指定后使用该包的分类进行识别，收集的图片也归属该包。",
  };
  const optionLabels = {
    cloudflare_r2: "Cloudflare R2",
    stardots: "StarDots",
    webdav: "WebDAV",
    lsky: "兰空图床（开源版 2.x）",
    only_chat_llm: "仅普通聊天回复",
    chat_and_plugin_llm: "普通聊天与插件触发的回复",
  };
  const fields = new Map();
  const controls = new Map();
  const changes = new Map();
  let revision = "";
  let active = "models";
  let providers = { chat: [], embedding: [] };
  let packs = [];
  let loaded = false;
  let semanticStatus = null;
  let applicationFailed = false;
  let dialogOrigin = null;
  let dialogCard = null;
  let dialogPlaceholder = null;
  let rulesPlaceholder = null;

  function restoreDialogCard() {
    if (!dialogCard) return;
    if (configDialog.open) configDialog.close();
    if (dialogPlaceholder?.isConnected)
      dialogPlaceholder.replaceWith(dialogCard);
    dialogCard = null;
    dialogPlaceholder = null;
    configDialogBody.replaceChildren();
  }

  function openConfigDialog(group, origin) {
    restoreDialogCard();
    const card = document.getElementById(`config-group-${group.id}`);
    dialogPlaceholder = document.createComment("");
    card.before(dialogPlaceholder);
    configDialogBody.replaceChildren(card);
    dialogCard = card;
    const title = document.getElementById("config-flow-dialog-title");
    title.textContent = group.title;
    configDialog.showModal();
    updateView();
    (controls.get(origin.dataset.workflowField) || title).focus({
      preventScroll: true,
    });
  }

  function updateView() {
    const query = search.value.trim().toLocaleLowerCase();
    const semantic = Boolean(
      changes.get(modePaths[0]) ?? fields.get(modePaths[0])?.value,
    );
    const auxiliary = Boolean(
      changes.get(modePaths[1]) ?? fields.get(modePaths[1])?.value,
    );
    const mode = semantic
      ? auxiliary
        ? "semantic-auxiliary"
        : "semantic-tool"
      : auxiliary
      ? "category-auxiliary"
      : "category-native";
    const savedSemantic = Boolean(fields.get(modePaths[0])?.value);
    const savedAuxiliary = Boolean(fields.get(modePaths[1])?.value);
    const savedMode = savedSemantic
      ? savedAuxiliary
        ? "semantic-auxiliary"
        : "semantic-tool"
      : savedAuxiliary
      ? "category-auxiliary"
      : "category-native";
    if (dialogCard && query) restoreDialogCard();
    modePanel.hidden =
      !loaded ||
      (query
        ? ![
            "选图模式 原生 分类 标签 语义 检索 tool 情感 辅助 模型",
            ...modePaths,
            ...modePaths.map((path) => fields.get(path)?.label || ""),
          ]
            .join(" ")
            .toLocaleLowerCase()
            .includes(query)
        : active !== "models");
    for (const input of modeInputs) {
      input.checked = input.value === mode;
      input.closest("label").classList.toggle("selected", input.checked);
    }
    workflow.hidden = modePanel.hidden;
    sections.hidden = !query && active === "models";
    collectionWorkflow.hidden =
      !loaded || Boolean(query) || active !== "collect";
    const collectionEnabled =
      changes.get("auto_collect.enabled") ??
      fields.get("auto_collect.enabled")?.value;
    const manualReview =
      changes.get("auto_collect.manual_review") ??
      fields.get("auto_collect.manual_review")?.value;
    document.getElementById(
      "collection-workflow-state",
    ).textContent = `当前选择：${collectionEnabled ? "开启" : "关闭"}`;
    document.getElementById("collection-workflow-result").textContent = `${
      collectionEnabled ? "" : "自动收集处于关闭状态，以上展示启用后的流程。"
    }${
      manualReview
        ? "当前选择：进入目标包的待审核列表 → 在表情包管理页预览、修正分类并接收。分类置信度不足时保留建议供你判断。"
        : "当前选择：新收集图片自动入包；分类置信度不足时归入 needs_review（待分类）。已有待审核图片仍需处理，目标包忙碌时新图片也会暂留待审核。"
    }`;
    const selectedModeName = modeInputs
      .find((input) => input.checked)
      .closest("label")
      .querySelector("strong").textContent;
    document.getElementById(
      "mode-workflow-title",
    ).textContent = `${selectedModeName}流程`;
    if (workflowSteps.dataset.mode !== mode) {
      workflowSteps.replaceChildren();
      workflowSteps.dataset.mode = mode;
      const selectionGroups = workspaceModes[mode].primary.filter(
        (id) =>
          ![
            "matching",
            "category-matching",
            "semantic",
            "semantic-models",
          ].includes(id),
      );
      const retrievalGroups = workspaceModes[mode].primary.filter((id) =>
        [
          "matching",
          "category-matching",
          "semantic",
          "semantic-models",
        ].includes(id),
      );
      const linkedGroups = new Set([
        ...workspaceModes[mode].primary,
        ...workspaceModes[mode].fallback,
        ...workspaceCommon,
        ...workspaceCollection,
        ...workspaceStorage,
      ]);
      const otherGroups = groups
        .filter((group) => !linkedGroups.has(group.id))
        .map((group) => group.id);
      const branches = [
        [
          ["自动收集", workspaceCollection],
          ["表情包使用规则", ["@rules"]],
        ],
        [["触发与数量", ["appearance", "quantity-guidance"]]],
        [
          ["当前模式", selectionGroups],
          ["其他模式的设置", otherGroups],
        ],
        [
          ["匹配与检索", retrievalGroups],
          ["不可用时的回退", workspaceModes[mode].fallback],
          ["图片来源与存储", workspaceStorage],
        ],
        [["发送方式", ["delivery"]]],
      ];
      for (const [index, [title, description]] of workflows[mode].entries()) {
        const item = document.createElement("li");
        const row = document.createElement("div");
        row.className = "mode-workflow-row";
        const node = document.createElement("div");
        node.className = "workflow-node";
        const label = document.createElement("strong");
        label.textContent = title;
        const detail = document.createElement("span");
        detail.textContent = description;
        node.append(label, detail);
        const branchList = document.createElement("div");
        branchList.className = "mode-workflow-branches";
        for (const [headingText, targetIds] of branches[index]) {
          if (!targetIds.length) continue;
          const branch = document.createElement("details");
          branch.className = "mode-workflow-branch";
          branch.open = [
            "触发与数量",
            "当前模式",
            "匹配与检索",
            "不可用时的回退",
            "发送方式",
            "表情包使用规则",
          ].includes(headingText);
          const heading = document.createElement("summary");
          heading.textContent = headingText;
          const links = document.createElement("div");
          links.className = "collection-workflow-links";
          for (const target of targetIds) {
            const extra = target.startsWith("@");
            const group = extra
              ? { title: "编辑使用规则" }
              : groups.find((item) => item.id === target);
            const button = document.createElement("button");
            button.type = "button";
            button.className = "ghost";
            if (extra) button.dataset.workflowExtra = target.slice(1);
            else button.dataset.workflowTarget = target;
            button.textContent = group.title;
            links.append(button);
          }
          branch.append(heading, links);
          branchList.append(branch);
        }
        row.append(node, branchList);
        item.append(row);
        workflowSteps.append(item);
      }
    }
    const modeName = modeInputs
      .find((input) => input.value === savedMode)
      .closest("label")
      .querySelector("strong").textContent;
    document.getElementById(
      "selection-mode-status",
    ).textContent = `已保存：${modeName}${
      mode !== savedMode ? "，已选择新模式，待保存" : ""
    }${applicationFailed ? "，应用失败，请重新加载插件" : ""}`;
    document.getElementById("selection-mode-readiness").hidden = !semantic;
    const defaultPack = packs.find((pack) => pack.is_default);
    let readiness = "暂未获取到默认表情包的就绪状态，可前往语义化页面检查。";
    if (defaultPack && semanticStatus) {
      const name = defaultPack.name || defaultPack.id;
      readiness =
        semanticStatus.semantic_caption_complete && semanticStatus.index_ready
          ? `默认表情包「${name}」的语义数据与索引已就绪。`
          : `默认表情包「${name}」的语义数据或索引未就绪，使用该包的会话将回退到${
              auxiliary ? "辅助模型分类标签" : "原生分类标签"
            }。`;
    }
    if (changes.has("semantic.embedding_provider_id"))
      readiness = "向量模型已修改，保存后将重新检查默认表情包的索引状态。";
    document.getElementById("selection-mode-readiness-text").textContent =
      readiness;
    document.getElementById("selection-mode-detail").textContent = auxiliary
      ? "选图会额外调用辅助模型，默认复用当前对话的回复模型。切换模式会保留已填写的参数。"
      : semantic
      ? "需要回复模型支持 Tool 调用；工具不可用时回退到原生分类标签。"
      : "无需额外选图模型。可在「提示词模板」与「标签与匹配」中调整分类选图，在「表情发送」中调整公共发送设置。";
    resetPrompts.hidden =
      Boolean(query) ||
      !(
        active === "prompts" ||
        (active === "models" &&
          ["prompts", "category-example", "reply-example"].includes(
            dialogCard?.id?.replace("config-group-", ""),
          ))
      );
    configDialogReset.hidden = !(
      dialogCard &&
      ["prompts", "category-example", "reply-example"].includes(
        dialogCard.id.replace("config-group-", ""),
      )
    );
    const configVisible = Boolean(query) || Boolean(categories[active]);
    for (const panel of panels) {
      panel.hidden = configVisible
        ? panel.dataset.settingsPanel !== "config"
        : panel.dataset.settingsPanel !== active;
      if (panel === rulesPanel && rulesDialog.open) panel.hidden = false;
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
    sections.classList.toggle("storage-layout", active === "storage" && !query);
    let visibleCount = modePanel.hidden ? 0 : 1;
    for (const group of groups) {
      const card = document.getElementById(`config-group-${group.id}`);
      if (!card) continue;
      let matches = 0;
      for (const row of card.querySelectorAll(".config-field")) {
        row.hidden =
          row.dataset.modeSwitch === "true" ||
          (Boolean(query) && !row.dataset.search.includes(query));
        if (!row.hidden) matches++;
      }
      card.hidden = query
        ? !matches
        : group.category !== active ||
          Boolean(group.provider && group.provider !== selectedProvider);
      if (!query && group.modes && !group.modes.includes(mode))
        card.hidden = true;
      if (card === dialogCard) card.hidden = false;
      card.querySelector(".config-mode-inactive")?.remove();
      if (!card.hidden && group.modes && !group.modes.includes(mode)) {
        const inactive = document.createElement("p");
        inactive.className = "config-mode-inactive config-card-description";
        inactive.textContent = workspaceModes[mode].fallback.includes(group.id)
          ? "当前模式仅在回退到分类选图时使用这些设置。"
          : "当前选图模式不使用这些设置；参数会保留。";
        card.querySelector("header").append(inactive);
      }
      if (!card.hidden) visibleCount += matches;
    }
    for (const column of sections.querySelectorAll(".storage-column")) {
      column.hidden = ![...column.children].some((card) => !card.hidden);
    }
    document.getElementById("config-search-empty").hidden =
      !loaded || visibleCount > 0;
  }

  function updateState(message = "", error = false) {
    configState.dirty = changes.size > 0;
    save.disabled = !loaded || !changes.size || configState.busy;
    reload.disabled = configState.busy;
    resetPrompts.disabled = !loaded || configState.busy;
    configDialogReset.disabled = !loaded || configState.busy;
    configDialogSave.disabled = !loaded || !changes.size || configState.busy;
    sections.inert = configState.busy;
    modePanel.inert = !loaded || configState.busy;
    workflow.inert = !loaded || configState.busy;
    configDialogBody.inert = !loaded || configState.busy;
    collectionWorkflow.inert = !loaded || configState.busy;
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
    configDialogStatus.textContent = status.textContent;
    configDialogStatus.classList.toggle("error", error);
    for (const tab of tabs) {
      const dot = tab.querySelector(".settings-dirty-dot");
      if (dot)
        dot.hidden = ![...changes.keys()].some(
          (path) =>
            fields.get(path)?.category === tab.dataset.settingsTab ||
            tab.dataset.settingsTab === "models",
        );
    }
  }

  function renderFields(snapshot) {
    restoreDialogCard();
    fields.clear();
    controls.clear();
    sections.replaceChildren();
    const storagePrimary = document.createElement("div");
    storagePrimary.className = "storage-column storage-primary";
    const storageSecondary = document.createElement("div");
    storageSecondary.className = "storage-column storage-secondary";
    sections.append(storagePrimary, storageSecondary);
    for (const group of groups) {
      const card = document.createElement("section");
      card.className = "panel config-card";
      card.id = `config-group-${group.id}`;
      const header = document.createElement("header");
      header.className = "config-card-header";
      const heading = document.createElement("h3");
      heading.id = `${card.id}-title`;
      heading.tabIndex = -1;
      card.setAttribute("aria-labelledby", heading.id);
      heading.textContent = group.title;
      if (group.icon) {
        const icon = document.createElement("i");
        icon.className = `fas ${group.icon}`;
        icon.setAttribute("aria-hidden", "true");
        heading.prepend(icon, document.createTextNode(" "));
      }
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
      if (group.category === "storage") {
        (group.provider || group.id === "storage"
          ? storagePrimary
          : storageSecondary
        ).append(card);
      } else sections.append(card);
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
      row.dataset.modeSwitch = String(modePaths.includes(field.path));
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
            provider.model
              ? `${provider.id}（${provider.model}）`
              : provider.id,
            provider.id,
          ]);
          if (field.special === "select_provider_embedding") {
            emptyLabel = "自动选择可用向量模型";
            options = providers.embedding.map((provider) => [
              provider.model
                ? `${provider.id}（${provider.model}）`
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
              field.bounds.min ??
              (/(top_k|\.timeout)$/.test(field.path) ? 1 : 0);
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
            ? "已保存，输入新值可替换"
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
              ? "已保存，输入新值可替换"
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
      const defaultPack = packs.find((pack) => pack.is_default);
      semanticStatus = defaultPack
        ? await api
            .apiGet("semantic/status", { pack_id: defaultPack.id })
            .catch(() => null)
        : null;
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
  form.addEventListener("click", (event) => {
    const trigger = event.target.closest(
      "[data-workflow-target], [data-workflow-extra]",
    );
    if (!trigger || !loaded || configState.busy) return;
    dialogOrigin = trigger;
    if (trigger.dataset.workflowExtra === "rules") {
      rulesPlaceholder = document.createComment("");
      rulesPanel.before(rulesPlaceholder);
      rulesDialogBody.replaceChildren(rulesPanel);
      rulesDialog.showModal();
      updateView();
      return;
    }
    const group = groups.find(
      (item) => item.id === trigger.dataset.workflowTarget,
    );
    if (group) openConfigDialog(group, trigger);
  });
  configDialog.addEventListener("click", (event) => {
    if (event.target.closest("[data-flow-dialog-close]")) configDialog.close();
  });
  configDialogReset.addEventListener("click", () => resetPrompts.click());
  configDialog.addEventListener("close", () => {
    restoreDialogCard();
    updateView();
    if (dialogOrigin?.isConnected) dialogOrigin.focus({ preventScroll: true });
  });
  rulesDialog.addEventListener("click", (event) => {
    if (event.target.closest("[data-rules-dialog-close]")) rulesDialog.close();
  });
  rulesDialog.addEventListener("close", () => {
    if (rulesPlaceholder?.isConnected) rulesPlaceholder.replaceWith(rulesPanel);
    rulesPlaceholder = null;
    rulesDialogBody.replaceChildren();
    updateView();
    if (dialogOrigin?.isConnected) dialogOrigin.focus({ preventScroll: true });
  });
  for (const input of modeInputs) {
    input.addEventListener("change", () => {
      if (!loaded || configState.busy || !input.checked) return;
      restoreDialogCard();
      const values = [
        input.value.startsWith("semantic-"),
        input.value.endsWith("-auxiliary"),
      ];
      for (const [index, path] of modePaths.entries()) {
        controls.get(path).checked = values[index];
        if (values[index] === Boolean(fields.get(path).value))
          changes.delete(path);
        else changes.set(path, values[index]);
      }
      updateState();
      updateView();
    });
  }
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
      control.placeholder = field.configured
        ? "已保存，输入新值可替换"
        : "请输入";
    }
    updateState();
    if (
      path === "storage.provider" ||
      path === "semantic.embedding_provider_id" ||
      path.startsWith("auto_collect.")
    )
      updateView();
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
        if (!configDialog.contains(control)) {
          restoreDialogCard();
          active = fields.get(path).category;
          search.value = labels[path] || fields.get(path).label;
          updateView();
        }
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
      applicationFailed = result.applied === false;
      renderFields(result);
      message = result.message || "设置已保存并生效。";
      if (result.applied === false) {
        notice.hidden = false;
        notice.classList.add("error");
        notice.textContent = message;
      }
      const defaultPack = packs.find((pack) => pack.is_default);
      semanticStatus = defaultPack
        ? await api
            .apiGet("semantic/status", { pack_id: defaultPack.id })
            .catch(() => null)
        : null;
      updateView();
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
