import { openStorageGuide } from "./storage-guide.js";
import { describeSetup } from "./setup-state.js";

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
  const mobileNavigation = document.getElementById("settings-category");
  for (const tab of tabs)
    mobileNavigation.add(
      new Option(tab.textContent.trim(), tab.dataset.settingsTab),
    );
  mobileNavigation.addEventListener("change", () =>
    tabs
      .find((tab) => tab.dataset.settingsTab === mobileNavigation.value)
      ?.click(),
  );
  const panels = [...document.querySelectorAll("[data-settings-panel]")];
  const modePanel = document.getElementById("selection-mode-panel");
  const dimensionInputs = [
    ...form.querySelectorAll(
      'input[name="selection-method"], input[name="selection-agent"]',
    ),
  ];
  const modePaths = ["semantic.enabled", "generation.emotion.llm.enabled"];
  const workflow = document.getElementById("mode-workflow");
  const workflowSteps = document.getElementById("mode-workflow-steps");
  const collectionWorkflow = document.getElementById("collection-workflow");
  const configMask = document.getElementById("config-flow-mask");
  const configDialog = document.getElementById("config-flow-dialog");
  const configDialogBody = document.getElementById("config-flow-dialog-body");
  const configDialogTitle = document.getElementById("config-flow-dialog-title");
  const configDialogSave = document.getElementById("config-flow-dialog-save");
  const configDialogReset = document.getElementById("config-flow-dialog-reset");
  const rulesPanel = panels.find(
    (panel) => panel.dataset.settingsPanel === "rules",
  );
  const combinationNames = {
    "category-reply": "分类级标签 · 主模型自选",
    "category-post": "分类级标签 · 额外模型辅助",
    "semantic-reply": "图片级标签 · 主模型自选",
    "semantic-post": "图片级标签 · 额外模型辅助",
  };

  function combinationKey(semantic, postReply) {
    return `${semantic ? "semantic" : "category"}-${
      postReply ? "post" : "reply"
    }`;
  }

  function selectionWorkflow({ semantic, postReply }) {
    return [
      {
        title: "表情来源",
        tab: "rules",
      },
      {
        title: semantic ? "图片标签" : "分类标签",
        group: semantic ? "semantic-models" : "category-matching",
        links: semantic
          ? [
              ["semantic", "检索参数"],
              ...(postReply ? [["semantic-query", "检索词来源"]] : []),
            ]
          : [],
      },
      {
        title: postReply ? "辅助模型" : "主模型",
        group: postReply
          ? "emotion-model"
          : semantic
          ? "tool-trigger"
          : "prompts",
        links: postReply ? [["selection-context", "上下文与人格"]] : [],
      },
      {
        title: "取图与发送",
        group: "appearance",
        links: [["delivery", "发送方式"]],
      },
    ];
  }
  const categories = {
    sending: ["表情发送", ""],
    models: ["模式", ""],
    decision: ["决策服务", ""],
    collect: ["自动收集", ""],
    storage: ["存储与下载", ""],
    prompts: ["提示词模板", ""],
    matching: ["标签与匹配", ""],
  };
  const groups = [
    {
      id: "appearance",
      category: "sending",
      title: "出现时机",
      prefixes: [
        "generation.trigger.",
        "generation.emotion.probability",
        "generation.emotion.send_tool_enabled",
        "generation.emotion.max_memes_per_message",
      ],
    },
    {
      id: "delivery",
      category: "sending",
      title: "发送方式",
      prefixes: ["generation.message."],
    },
    {
      id: "tool-trigger",
      when: "reply-semantic",
      category: "models",
      title: "Tool 调用策略",
      advanced: true,
      prefixes: [
        "semantic.tool_probability_enabled",
        "semantic.tool_call_probability",
      ],
    },
    {
      id: "semantic",
      when: "semantic",
      category: "models",
      title: "检索参数",
      advanced: true,
      prefixes: ["semantic.enabled", "semantic.top_k", "semantic.min_score"],
    },
    {
      id: "semantic-models",
      when: "semantic",
      category: "models",
      title: "语义数据准备",
      advanced: true,
      prefixes: [
        "semantic.vision_provider_id",
        "semantic.embedding_provider_id",
      ],
    },
    {
      id: "semantic-query",
      when: "post-semantic",
      category: "models",
      title: "检索词来源",
      advanced: true,
      prefixes: ["semantic.query."],
    },
    {
      id: "emotion-model",
      when: "post",
      category: "models",
      title: "辅助选图模型",
      prefixes: [
        "generation.emotion.llm.enabled",
        "generation.emotion.llm.backend",
        "generation.emotion.llm.provider_id",
      ],
    },
    {
      id: "selection-context",
      when: "post",
      category: "models",
      title: "上下文与人格",
      advanced: true,
      prefixes: [
        "generation.emotion.llm.context_turns",
        "generation.emotion.llm.inject_persona",
      ],
    },
    {
      id: "decision-service",
      category: "decision",
      title: "决策服务接口",
      prefixes: ["decision_service."],
      advancedPaths: [
        "decision_service.timeout",
        "decision_service.max_concurrency",
      ],
      advancedTitle: "超时与并发（高级）",
    },
    {
      id: "collect",
      category: "collect",
      title: "来源与频率",
      prefixes: [
        "auto_collect.enabled",
        "auto_collect.scope",
        "auto_collect.target_pack_id",
        "auto_collect.max_images_per_message",
        "auto_collect.sampling_probability",
        "auto_collect.cooldown_seconds",
        "auto_collect.daily_recognition_limit",
      ],
      advancedPaths: [
        "auto_collect.max_images_per_message",
        "auto_collect.sampling_probability",
        "auto_collect.cooldown_seconds",
        "auto_collect.daily_recognition_limit",
      ],
      advancedTitle: "采样与用量（可选）",
    },
    {
      id: "collect-recognition",
      category: "collect",
      title: "识别与分类",
      prefixes: [
        "auto_collect.judgment_backend",
        "auto_collect.vision_provider_id",
        "auto_collect.content_review_enabled",
        "auto_collect.content_review_prompt",
        "auto_collect.min_meme_confidence",
        "auto_collect.min_category_confidence",
      ],
      advancedPaths: [
        "auto_collect.min_meme_confidence",
        "auto_collect.min_category_confidence",
      ],
      advancedTitle: "收录与分类阈值（高级）",
    },
    {
      id: "collect-review",
      category: "collect",
      title: "审核与入包",
      prefixes: [
        "auto_collect.manual_review",
        "auto_collect.pending_limit",
        "semantic.auto_collect_embedding",
      ],
    },
    {
      id: "storage",
      category: "storage",
      title: "图床连接",
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
      icon: "fa-download",
      prefixes: ["community."],
    },
    {
      id: "sync",
      category: "storage",
      title: "同步状态",
      icon: "fa-arrows-rotate",
      prefixes: ["sync."],
    },
    {
      id: "preview",
      category: "storage",
      title: "页面预览",
      icon: "fa-images",
      prefixes: ["webui."],
    },
    {
      id: "prompts",
      when: "reply-category",
      category: "prompts",
      title: "分类提示词",
      prefixes: ["generation.prompt.head", "generation.prompt.tail"],
    },
    {
      id: "quantity-guidance",
      category: "prompts",
      title: "表情回复数量",
      prefixes: [
        "generation.prompt.quantity_guidance_enabled",
        "generation.prompt.quantity_guidance",
      ],
    },
    {
      id: "category-example",
      when: "reply-category",
      category: "prompts",
      title: "分类使用示例",
      prefixes: [
        "generation.prompt.category_example_enabled",
        "generation.prompt.category_example",
      ],
    },
    {
      id: "reply-example",
      when: "reply-category",
      category: "prompts",
      title: "回复形式示例",
      prefixes: [
        "generation.prompt.reply_example_enabled",
        "generation.prompt.reply_example",
      ],
    },
    {
      id: "matching",
      category: "matching",
      title: "标签识别",
      prefixes: ["generation.markup."],
    },
    {
      id: "category-matching",
      when: "category",
      category: "matching",
      title: "分类匹配",
      prefixes: ["generation.matching."],
    },
  ];
  const labels = {
    "semantic.enabled": "标签粒度",
    "generation.emotion.llm.enabled": "由谁选择表情包",
    "generation.trigger.scope": "应用范围",
    "generation.emotion.probability": "表情发送概率（%）",
    "generation.emotion.send_tool_enabled": "启用主动发送表情工具",
    "generation.emotion.llm.provider_id": "配图聊天模型",
    "generation.emotion.llm.backend": "辅助模型类型",
    "semantic.query.source": "检索词来源",
    "semantic.query.provider_id": "检索词聊天模型",
    "decision_service.provider": "服务提供方",
    "decision_service.account_id": "Cloudflare 账户 ID",
    "decision_service.base_url": "决策服务基础地址",
    "decision_service.token": "决策服务密钥",
    "decision_service.model": "决策模型 ID",
    "auto_collect.judgment_backend": "收录与分类方式",
    "semantic.top_k": "每次检索候选数",
    "semantic.min_score": "最低相似度",
    "semantic.embedding_provider_id": "向量模型",
    "auto_collect.vision_provider_id": "图片理解模型",
    "auto_collect.manual_review": "人工确认入包",
    "auto_collect.content_review_enabled": "内容规则筛选",
    "semantic.vision_provider_id": "建库图片描述模型",
    "auto_collect.target_pack_id": "收集到的表情包",
    "generation.message.mixed_probability": "图文同条回复概率（%）",
  };
  const hints = {
    "webui.preview_concurrency":
      "每个页面同时加载的预览图片数，保存后刷新页面生效",
    "storage.provider": "选择图片同步使用的图床服务；切换会保留已填参数",
    "storage.providers.lsky.url":
      "填写站点根地址，例如 https://img.example.com，不含 /api/v1；仅支持开源版 2.x。",
    "storage.providers.lsky.strategy_id":
      "0 使用默认策略，上传沿用账号默认相册。请关闭图片压缩、格式转换和覆盖式水印，保持原图内容。",
    "storage.providers.lsky.namespace":
      "所有表情包共用。修改后连接新空间，不搬迁旧图。",
    "storage.providers.cloudflare_r2.prefix":
      "仅同步此目录内的图片；修改后连接新目录，不搬迁旧文件。",
    "storage.providers.webdav.base_path": "图片在 WebDAV 中的保存目录",
    "sync.status_cache_ttl_seconds": "管理页同步状态的缓存时间，0 表示关闭",
    "community.github_accelerator_url":
      "下载 GitHub 资源时使用此前缀，留空直连",
    "decision_service.provider":
      "systemone 使用 System One 决策服务；cloudflare 使用 Workers AI 的 clef 系列决策模型",
    "decision_service.account_id":
      "REST 地址 /accounts/{账户ID}/ 中的 32 位十六进制账户 ID；仅 Cloudflare 提供方必填",
    "decision_service.base_url":
      "仅 System One 使用：v1 服务地址；Cloudflare 提供方忽略此字段",
    "decision_service.model":
      "System One 填写模型 ID；Cloudflare 填写 clef、clef-flash 或 clef-omni；「测试连接」发送一次极小请求",
    "decision_service.timeout": "单次决策任务的最长等待时间",
    "decision_service.max_concurrency": "辅助配图与自动收集共享的请求并发上限",
    "semantic.enabled": "开启后按图片语义检索，索引不可用时回退分类",
    "semantic.query.source": "生成检索短语，或直接使用可见回复文本",
    "semantic.query.provider_id": "留空跟随配图聊天模型；决策辅助时跟随主模型",
    "semantic.top_k": "每次提供给选图模型的最大候选数量",
    "semantic.min_score": "低于此相似度的图片不进入候选",
    "semantic.tool_probability_enabled":
      "关闭时由模型自主调用；开启后按概率主动触发检索，可能增加一次模型请求。",
    "semantic.tool_call_probability":
      "控制是否检索，与表情发送概率独立；0% 禁用，100% 每轮尝试",
    "semantic.auto_collect_embedding":
      "启用语义检索后，入包时增量生成向量；待分类或描述未完成的图片请先到语义化页面处理",
    "semantic.vision_provider_id": "用于生成图库图片描述，需支持图片输入",
    "semantic.embedding_provider_id":
      "留空自动选择可用向量模型；更换后请到语义化页面检查并重建索引",
    "auto_collect.enabled": "按来源、频率和抽样规则收集聊天图片",
    "auto_collect.judgment_backend": "视觉模型合并判断，或增加一次决策请求",
    "auto_collect.vision_provider_id":
      "用于识别和分类收集的图片，需支持图片输入",
    "auto_collect.manual_review":
      "开启后在表情包管理中预览、调整分类并接收；关闭后新图片可自动入包，决策不确定或失败时仍需人工复核，已有待审图片仍需处理",
    "auto_collect.content_review_enabled":
      "与人工确认入包独立；视觉模式随同次识别筛选，决策模式根据图片描述和可见文字初筛，可能增加一个计费问题",
    "auto_collect.content_review_prompt":
      "仅影响后续自动收集，不重新审核已有图库或待审图片；留空使用默认规则",
    "auto_collect.pending_limit":
      "每个表情包独立计算；满后暂停提交新图片，不会删除已有待审图片",
    "auto_collect.scope":
      "可填写原始 ID，或用 group:群ID、user:用户ID 区分群聊和私聊；留空表示全部来源",
    "auto_collect.target_pack_id":
      "指定后使用该包的分类识别并收录图片；留空跟随会话使用规则",
    "auto_collect.sampling_probability":
      "仅控制平台标记的表情；普通图片首次仅记录，重复出现后逐步提高识别概率。",
    "auto_collect.max_images_per_message":
      "优先处理平台标记的表情，其余按消息顺序处理，每条消息最多处理指定数量",
    "auto_collect.cooldown_seconds":
      "限制同一群聊或用户不同消息之间的识图间隔，同一消息可按图片上限处理多张",
    "auto_collect.daily_recognition_limit":
      "每日最多发起的视觉识别任务数，0 表示不限",
    "auto_collect.min_meme_confidence":
      "视觉路径低于此值不收录；决策路径按概率收录、拒收或复核",
    "auto_collect.min_category_confidence":
      "视觉模式：自动入包时低于阈值归入「待分类」，人工审核时保留建议分类；决策模式低于阈值需人工复核",
    "generation.trigger.scope":
      "仅为所选范围的 LLM 回复附加表情，指令和固定文本回复不触发",
    "generation.emotion.llm.enabled": "开启后在回复生成后增加一次独立选图请求",
    "generation.emotion.llm.backend":
      "聊天 LLM 仅在分类模式可多选，决策服务单选",
    "generation.emotion.llm.provider_id":
      "辅助选图使用的聊天模型，留空跟随本轮主模型",
    "generation.emotion.llm.context_turns": "提供最近 N 轮对话，0 表示不提供",
    "generation.emotion.llm.inject_persona": "将当前人格设定提供给辅助选图模型",
    "generation.emotion.max_memes_per_message":
      "自动配图最多 N 张，负数不限，0 不自动附图；不限制 send_meme 主动发送工具",
    "generation.emotion.probability":
      "每轮按此概率允许自动附图；0% 不自动附图，不限制 send_meme 主动发送工具",
    "generation.emotion.send_tool_enabled":
      "向主模型提供 send_meme 工具，可按分类或本轮语义候选立即发图；由模型决定调用",
    "generation.prompt.head": "定义表情工具和发送格式，分类列表自动接在末尾",
    "generation.prompt.tail": "接在分类列表后，约束标签使用和选图方式",
    "generation.prompt.quantity_guidance_enabled":
      "仅向模型提供频率和数量偏好，不修改发送硬上限",
    "generation.prompt.quantity_guidance": "填写频率和数量偏好，留空不添加",
    "generation.prompt.category_example_enabled":
      "追加虚构分类示例，不会新增可用分类",
    "generation.prompt.category_example":
      "演示分类标记；修改时保留虚构示例与真实分类的边界说明",
    "generation.prompt.reply_example_enabled":
      "追加虚构回复示例，不会新增可用分类",
    "generation.prompt.reply_example":
      "演示回复形式；修改时保留虚构示例与真实分类的边界说明",
    "generation.markup.filter_all_tags":
      "清理所有表情标记，可能误删代码或普通括号内容",
    "generation.markup.enable_alternative": "将 :happy: 识别为 &&happy&&",
    "generation.markup.remove_invalid_alternative":
      "删除无法识别的 () 或 [] 标记；可能影响其他插件，出现标签丢失时可先关闭本项排查",
    "generation.markup.enable_repeated_detection":
      "合并 angryangryangry 这类重复标签",
    "generation.matching.enable_loose_matching":
      "使用当前会话表情包的分类，识别独占一行且没有标记包裹的分类名，无需额外词表",
    "generation.message.convert_static_to_gif":
      "部分微信接入需要转换后才能显示为表情气泡；其他平台通常无需开启，显示异常或体积变大时可关闭",
    "generation.message.enable_mixed": "将文字和表情放在同一条消息中",
    "generation.message.mixed_probability": "图文同条模式下按此概率发送",
    "generation.message.streaming_compatibility":
      "流式回复时将图片作为独立消息发送",
    "generation.message.send_image_as_base64":
      "将本地图片转为 base64://，适配不支持 file:// 的平台",
    "generation.message.content_cleanup_rule": "按正则表达式清理分段后的文本",
  };
  const optionLabels = {
    cloudflare_r2: "Cloudflare R2",
    stardots: "StarDots",
    webdav: "WebDAV",
    lsky: "兰空图床（开源版 2.x）",
    only_chat_llm: "仅普通聊天回复",
    chat_and_plugin_llm: "普通聊天与插件触发的回复",
    chat: "聊天 LLM",
    decision: "决策服务",
    systemone: "System One 决策服务",
    cloudflare: "Cloudflare Workers AI",
    generated: "由聊天模型生成",
    reply_text: "使用可见回复文本",
    vision: "视觉模型合并判断",
  };
  const fields = new Map();
  const controls = new Map();
  const changes = new Map();
  let revision = "";
  let active = "models";
  let providers = { chat: [], embedding: [] };
  let providerListErrors = [];
  let decisionModels = [];
  let decisionModelsBaseUrl = "";
  let modelListRequest = 0;
  let decisionHintRequest = 0;
  const cloudflareDecisionModels = [
    { id: "clef-flash", name: "Clef Flash · 快速 9B" },
    { id: "clef", name: "Clef · 27B" },
    { id: "clef-omni", name: "Clef Omni · 音视频输入" },
  ];
  let packs = [];
  let loaded = false;
  let semanticStatus = null;
  let activeFlowGroup = "";
  let pendingGroupAnchor = "";
  let dialogContent = null;
  let dialogPlaceholder = null;
  let dialogOrigin = null;
  let dialogOriginGroup = "";
  let dialogOriginTab = "";
  let dialogFieldPath = "";

  function decisionProvider() {
    return String(
      changes.get("decision_service.provider") ??
        fields.get("decision_service.provider")?.value ??
        "systemone",
    );
  }

  function invalidateDecisionTest() {
    // 修改配置后，已完成的提示和仍在等待的响应都不再有效。
    decisionHintRequest++;
    const help = document.getElementById("config-decision_service-model-hint");
    if (help) {
      help.dataset.state = "";
      help.textContent = help.dataset.baseHint;
    }
  }

  function updateDecisionModelControl(provider) {
    const cloudflare = (provider ?? decisionProvider()) === "cloudflare";
    const control = controls.get("decision_service.model");
    const choices = document.getElementById(
      "config-decision_service-model-choices",
    );
    const help = document.getElementById("config-decision_service-model-hint");
    const button = document.getElementById("fetch-decision-models");
    if (control)
      control.placeholder = cloudflare
        ? "clef、clef-flash 或 clef-omni"
        : "从列表选择或填写模型 ID";
    if (choices) {
      choices.replaceChildren(new Option("从列表选择…", ""));
      const baseUrl = String(
        changes.get("decision_service.base_url") ??
          fields.get("decision_service.base_url")?.value ??
          "",
      ).trim();
      const models = cloudflare
        ? cloudflareDecisionModels
        : baseUrl === decisionModelsBaseUrl
        ? decisionModels
        : [];
      for (const model of models)
        choices.append(new Option(model.name || model.id, model.id));
      choices.disabled = choices.options.length <= 1;
    }
    if (button) {
      button.hidden = cloudflare;
      button.disabled = false;
      button.textContent = "获取列表";
    }
    if (help)
      help.textContent = cloudflare
        ? `${help.dataset.baseHint}；从列表选择或手动填写`
        : `${help.dataset.baseHint}；可获取列表或手动填写`;
    if (help) help.dataset.state = "";
  }

  function restoreDialogContent() {
    if (dialogContent && dialogPlaceholder?.isConnected)
      dialogPlaceholder.replaceWith(dialogContent);
    dialogContent = null;
    dialogPlaceholder = null;
    configDialogBody.replaceChildren();
  }

  function mountDialogContent(node) {
    restoreDialogContent();
    dialogPlaceholder = document.createComment("settings-dialog-content");
    node.before(dialogPlaceholder);
    configDialogBody.replaceChildren(node);
    dialogContent = node;
    node.hidden = false;
    if (node.tagName === "DETAILS") node.open = true;
  }

  function closeSettingsDialog(restoreFocus = true) {
    if (configState.busy) return;
    restoreDialogContent();
    configMask.classList.add("hidden");
    configMask.setAttribute("aria-hidden", "true");
    activeFlowGroup = "";
    dialogFieldPath = "";
    pendingGroupAnchor = "";
    history.replaceState(
      null,
      "",
      `${location.pathname}${location.search}#${active}`,
    );
    updateView();
    updateState();
    if (!restoreFocus) return;
    const origin =
      dialogOrigin?.isConnected && !dialogOrigin.closest("[hidden], .hidden")
        ? dialogOrigin
        : [
            ...form.querySelectorAll(
              "[data-setting-group], [data-setting-tab]",
            ),
          ].find(
            (node) =>
              ((dialogOriginGroup &&
                node.dataset.settingGroup === dialogOriginGroup) ||
                (dialogOriginTab &&
                  node.dataset.settingTab === dialogOriginTab)) &&
              node.getClientRects().length &&
              !node.closest("[hidden]"),
          );
    (
      origin ||
      (mobileNavigation.getClientRects().length
        ? mobileNavigation
        : document.querySelector(`[data-settings-tab="${active}"]`))
    )?.focus();
  }

  function navigateToSetting(groupId, path = "", tabId = "", origin = null) {
    const fallbackGroup =
      tabId === "models"
        ? "emotion-model"
        : tabId === "collect"
        ? "collect-recognition"
        : "";
    const group = groups.find((item) => item.id === (groupId || fallbackGroup));
    const dimension = modePaths.indexOf(path);
    const rules = tabId === "rules";
    const node = rules
      ? rulesPanel
      : group
      ? document.getElementById(`config-group-${group.id}`)
      : null;
    pendingGroupAnchor = "";
    search.value = "";
    if (dimension < 0 && node) {
      if (!dialogContent || (origin && !configDialog.contains(origin))) {
        dialogOrigin = origin || document.activeElement;
        dialogOriginGroup = groupId || "";
        dialogOriginTab = tabId || "";
      }
      activeFlowGroup = group?.id || "";
      dialogFieldPath = path;
      mountDialogContent(node);
      configDialogTitle.textContent = rules ? "表情包使用规则" : group.title;
      document.getElementById("config-flow-dialog-context").textContent = rules
        ? "使用规则 · 单独保存"
        : categories[group.category]?.[0] || "插件设置";
      configMask.classList.remove("hidden");
      configMask.setAttribute("aria-hidden", "false");
      configDialogBody.scrollTop = 0;
      history.replaceState(
        null,
        "",
        `${location.pathname}${location.search}#config-group-${
          rules ? "rules" : group.id
        }`,
      );
      updateView();
      updateState();
      const target = controls.get(path) || configDialogTitle;
      for (
        let detail = target.closest("details");
        detail && configDialog.contains(detail);
        detail = detail.parentElement?.closest("details")
      )
        detail.open = true;
      queueMicrotask(() => {
        if (dialogContent !== node || configState.busy) return;
        target.focus({ preventScroll: true });
        if (path)
          target.scrollIntoView({ block: "nearest", behavior: "instant" });
      });
      return;
    }
    if (dialogContent) closeSettingsDialog(false);
    active = dimension >= 0 ? "models" : tabId || group?.category || "models";
    history.replaceState(
      null,
      "",
      `${location.pathname}${location.search}#${active}`,
    );
    updateView();
    const target =
      (dimension >= 0
        ? form.querySelector(
            `input[name="${
              dimension === 0 ? "selection-method" : "selection-agent"
            }"]:checked`,
          )
        : controls.get(path)) ||
      (mobileNavigation.getClientRects().length
        ? mobileNavigation
        : document.querySelector(`[data-settings-tab="${active}"]`));
    for (
      let detail = target.closest("details");
      detail;
      detail = detail.parentElement?.closest("details")
    )
      detail.open = true;
    if (path && dimension < 0 && target.closest("[hidden]")) {
      search.value = path;
      updateView();
    }
    target.focus();
    if (group || path)
      target.scrollIntoView({ block: "start", behavior: "instant" });
  }

  function showFieldError(path, message) {
    const group = groups.find((item) =>
      item.prefixes.some((prefix) =>
        prefix.endsWith(".") ? path.startsWith(prefix) : prefix === path,
      ),
    );
    navigateToSetting(group?.id, path);
    const control = controls.get(path);
    const row = control.closest(".config-field");
    let error = row.querySelector(".config-field-error");
    if (!error) {
      error = document.createElement("p");
      error.className = "config-field-error";
      error.id = `${control.id}-error`;
      row.append(error);
    }
    error.textContent = message;
    control.setAttribute("aria-invalid", "true");
    control.setAttribute("aria-describedby", `${control.id}-hint ${error.id}`);
    updateState(message, true);
  }

  function updateView() {
    mobileNavigation.value = active;
    const query = search.value.trim().toLocaleLowerCase();
    const currentValue = (path) =>
      changes.has(path) ? changes.get(path) : fields.get(path)?.value;
    const semantic = Boolean(currentValue(modePaths[0]));
    const postReply = Boolean(currentValue(modePaths[1]));
    const backend = currentValue("generation.emotion.llm.backend") || "chat";
    const querySource = currentValue("semantic.query.source") || "generated";
    const judgmentBackend =
      currentValue("auto_collect.judgment_backend") || "vision";
    const selectionDecision = postReply && backend === "decision";
    const collectionDecision =
      Boolean(currentValue("auto_collect.enabled")) &&
      judgmentBackend === "decision";
    const mode = combinationKey(semantic, postReply);
    modePanel.hidden =
      !loaded ||
      (query
        ? ![
            "配图 标签粒度 由谁选择表情包 由谁选标签 主模型 回复模型 自选 辅助 分类 图片 语义 检索 tool 聊天 决策 模型 选图方式 选择时机 回复时 回复后",
            "原生分类标签 辅助模型分类标签 Tool 语义检索 辅助模型语义检索 情感辅助模型 模式",
            ...Object.values(combinationNames),
            ...modePaths,
            ...modePaths.map((path) => fields.get(path)?.label || ""),
          ]
            .join(" ")
            .toLocaleLowerCase()
            .includes(query)
        : active !== "models");
    for (const input of dimensionInputs) {
      input.checked =
        input.name === "selection-method"
          ? input.value === (semantic ? "semantic" : "category")
          : input.value === (postReply ? "assisted" : "reply");
      input.closest("label").classList.toggle("selected", input.checked);
    }
    sections.hidden = !query && ["models", "collect"].includes(active);
    workflow.hidden = !loaded || Boolean(query) || active !== "models";
    collectionWorkflow.hidden =
      !loaded || Boolean(query) || active !== "collect";
    const workflowKey = [mode, backend, querySource, activeFlowGroup].join(":");
    if (workflowSteps.dataset.state !== workflowKey) {
      workflowSteps.dataset.state = workflowKey;
      workflowSteps.replaceChildren();
      for (const step of selectionWorkflow({
        semantic,
        postReply,
        backend,
        querySource,
      })) {
        const item = document.createElement("li");
        const node = document.createElement("button");
        node.type = "button";
        node.className = "workflow-node";
        if (step.group) node.dataset.settingGroup = step.group;
        if (step.tab) node.dataset.settingTab = step.tab;
        if (
          groups.find((group) => group.id === step.group)?.category === "models"
        ) {
          node.setAttribute(
            "aria-expanded",
            String(activeFlowGroup === step.group),
          );
          node.setAttribute("aria-controls", `config-group-${step.group}`);
        }
        const heading = document.createElement("strong");
        heading.textContent = step.title;
        const action = document.createElement("small");
        action.textContent = "设置 →";
        node.append(heading, action);
        item.append(node);
        {
          const links = document.createElement("div");
          links.className = "workflow-links";
          for (const [group, title] of step.links || []) {
            const link = document.createElement("button");
            link.type = "button";
            link.className = "ghost setup-link";
            link.dataset.settingGroup = group;
            link.textContent = title;
            links.append(link);
          }
          item.append(links);
        }
        workflowSteps.append(item);
      }
    }
    document.getElementById("mode-workflow-title").textContent = "当前流程";
    const values = Object.fromEntries(
      [...fields].map(([path]) => [path, currentValue(path)]),
    );
    const setup = describeSetup(values, {
      providers,
      tokenConfigured: changes.has("decision_service.token")
        ? Boolean(changes.get("decision_service.token"))
        : Boolean(fields.get("decision_service.token")?.configured),
      accountIdConfigured:
        !changes.has("decision_service.account_id") &&
        Boolean(fields.get("decision_service.account_id")?.configured),
      semanticStatus,
      defaultPack: packs.find((pack) => pack.is_default),
      embeddingChanged: changes.has("semantic.embedding_provider_id"),
      providerListErrors,
    });
    const readiness = document.getElementById("selection-readiness-note");
    readiness.hidden = !semantic;
    if (semantic) {
      readiness.textContent = `${
        setup.preparationWarning &&
        setup.selectionIssues.some(
          (item) => item.path === "semantic.embedding_provider_id",
        )
          ? "尚无可用向量模型"
          : setup.rows.find((item) => item.label === "默认包准备").value
      }。这里展示默认包状态；各会话按实际使用的表情包判断，语义数据或索引不可用时回退分类选图。`;
      if (setup.preparationWarning) {
        const link = document.createElement("button");
        link.type = "button";
        link.className = "ghost setup-link";
        link.dataset.settingGroup = "semantic-models";
        link.textContent = "检查语义数据准备";
        readiness.append(link);
      }
    }
    const issues =
      active === "collect" ? setup.collectionIssues : setup.selectionIssues;
    const selectionChecklist = document.getElementById("selection-required");
    const collectionChecklist = document.getElementById("collection-required");
    selectionChecklist.replaceChildren();
    collectionChecklist.replaceChildren();
    selectionChecklist.hidden = !setup.selectionIssues.length;
    collectionChecklist.hidden = !setup.collectionIssues.length;
    const checklist =
      active === "collect" ? collectionChecklist : selectionChecklist;
    for (const issue of issues) {
      const item = document.createElement("li");
      const link = document.createElement("button");
      link.type = "button";
      link.className = "ghost setup-link";
      link.textContent = issue.text;
      if (issue.group) link.dataset.settingGroup = issue.group;
      if (issue.tab) link.dataset.settingTab = issue.tab;
      if (issue.path) link.dataset.settingPath = issue.path;
      item.append(link);
      checklist.append(item);
    }
    document.getElementById("collection-workflow-result").textContent =
      setup.collectionRoute;
    document.getElementById("collection-workflow-state").textContent =
      currentValue("auto_collect.enabled") ? "已开启" : "未开启";
    document.getElementById("collection-analysis-summary").textContent =
      judgmentBackend === "decision" ? "视觉模型 + 决策服务" : "视觉模型";
    document.getElementById("collection-review-summary").textContent =
      currentValue("auto_collect.manual_review") ? "待审核后入包" : "自动入包";
    for (const node of collectionWorkflow.querySelectorAll(".workflow-node"))
      node.setAttribute(
        "aria-expanded",
        String(node.dataset.settingGroup === activeFlowGroup),
      );
    const reviewNote = document.getElementById("collection-route-note");
    if (reviewNote) reviewNote.textContent = setup.collectionRoute;
    const reference = document.getElementById("decision-service-reference");
    if (reference) {
      reference.hidden = !selectionDecision;
      reference.textContent = `决策服务：${
        currentValue("decision_service.model") || "未配置"
      }。`;
      const serviceLink = document.createElement("button");
      serviceLink.type = "button";
      serviceLink.className = "ghost compact setup-link";
      serviceLink.textContent = "配置决策服务";
      serviceLink.dataset.settingGroup = "decision-service";
      reference.append(serviceLink);
    }
    document.getElementById("selection-mode-detail").textContent =
      postReply || semantic ? setup.requestNote : "分类标签随机取图";
    resetPrompts.hidden = Boolean(query) || active !== "prompts";
    const configVisible = Boolean(query) || Boolean(categories[active]);
    for (const panel of panels) {
      if (panel === dialogContent) {
        panel.hidden = false;
        continue;
      }
      panel.hidden = configVisible
        ? panel.dataset.settingsPanel !== "config"
        : panel.dataset.settingsPanel !== active;
    }
    for (const tab of tabs) {
      if (!query && tab.dataset.settingsTab === active)
        tab.setAttribute("aria-current", "page");
      else tab.removeAttribute("aria-current");
    }
    const title = query ? ["搜索结果", ""] : categories[active];
    if (title) {
      document.getElementById("config-section-title").textContent = title[0];
      document.getElementById("config-section-description").textContent =
        title[1];
    }
    const selectedProvider =
      changes.get("storage.provider") ?? fields.get("storage.provider")?.value;
    sections.classList.toggle("storage-layout", active === "storage" && !query);
    let visibleCount = modePanel.hidden && collectionWorkflow.hidden ? 0 : 1;
    const conditions = {
      semantic,
      category: !semantic,
      "reply-semantic": semantic && !postReply,
      "post-semantic": semantic && postReply,
      "reply-category": !semantic && !postReply,
      post: postReply,
      decision: selectionDecision || collectionDecision,
    };
    for (const group of groups) {
      const card = document.getElementById(`config-group-${group.id}`);
      if (!card) continue;
      const explicitlyOpened = group.id === activeFlowGroup;
      let matches = 0;
      for (const row of card.querySelectorAll(".config-field")) {
        row.hidden =
          row.dataset.modeSwitch === "true" ||
          (!query &&
            row.dataset.path === "semantic.tool_call_probability" &&
            !(
              changes.get("semantic.tool_probability_enabled") ??
              fields.get("semantic.tool_probability_enabled")?.value
            )) ||
          (!query &&
            row.dataset.path === "generation.emotion.llm.provider_id" &&
            backend === "decision") ||
          (!query &&
            row.dataset.path === "semantic.query.provider_id" &&
            querySource !== "generated") ||
          (!query &&
            row.dataset.path === "auto_collect.content_review_prompt" &&
            !currentValue("auto_collect.content_review_enabled")) ||
          (!query &&
            row.dataset.path === "decision_service.account_id" &&
            decisionProvider() !== "cloudflare") ||
          (!query &&
            row.dataset.path === "decision_service.base_url" &&
            decisionProvider() === "cloudflare") ||
          (Boolean(query) && !row.dataset.search.includes(query));
        if (card === dialogContent && row.dataset.path === dialogFieldPath)
          row.hidden = false;
        if (!row.hidden) matches++;
      }
      card.hidden = query
        ? !matches
        : group.category !== active ||
          Boolean(
            group.provider &&
              group.provider !== selectedProvider &&
              !explicitlyOpened,
          );
      if (!query && group.when && !conditions[group.when] && !explicitlyOpened)
        card.hidden = true;
      if (
        !query &&
        ["models", "collect"].includes(active) &&
        group.category === active &&
        group.id !== activeFlowGroup
      )
        card.hidden = true;
      if (card === dialogContent) card.hidden = false;
      card.querySelector(".config-mode-inactive")?.remove();
      if (
        !card.hidden &&
        ((group.when && !conditions[group.when]) ||
          (group.provider && group.provider !== selectedProvider))
      ) {
        const inactive = document.createElement("p");
        inactive.className = "config-mode-inactive config-card-description";
        inactive.textContent = group.provider
          ? "当前存储方式未使用；已填参数会保留，切换后保存生效"
          : semantic &&
            [
              "prompts",
              "category-example",
              "reply-example",
              "matching",
              "category-matching",
            ].includes(group.id)
          ? "分类回退时使用"
          : "当前模式未使用；已填参数会保留，切换后保存生效";
        card.querySelector(".config-card-header").append(inactive);
      }
      if (!card.hidden && query) {
        if (card.tagName === "DETAILS") card.open = true;
        for (const detail of card.querySelectorAll("details"))
          detail.open = true;
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
    configDialogReset.hidden = ![
      "config-group-prompts",
      "config-group-category-example",
      "config-group-reply-example",
    ].includes(dialogContent?.id);
    configDialogSave.disabled = save.disabled;
    configDialogSave.hidden = dialogContent === rulesPanel;
    configDialogSave.textContent = configState.busy
      ? "正在处理…"
      : "保存全部更改";
    configDialogBody.inert = configState.busy;
    for (const button of configDialog.querySelectorAll("[data-dialog-dismiss]"))
      button.disabled = configState.busy;
    sections.inert = configState.busy;
    modePanel.inert = !loaded || configState.busy;
    workflow.inert = !loaded || configState.busy;
    collectionWorkflow.inert = !loaded || configState.busy;
    form.setAttribute("aria-busy", String(configState.busy));
    save.textContent = configState.busy ? "正在处理…" : "保存全部更改";
    status.textContent =
      message ||
      (changes.size
        ? `共 ${changes.size} 项配置更改待保存`
        : loaded
        ? "所有更改已保存"
        : "尚未读取配置");
    status.classList.toggle("error", error);
    const changedSections = [
      ...new Set(
        [...changes.keys()]
          .map((path) => categories[fields.get(path)?.category]?.[0])
          .filter(Boolean),
      ),
    ];
    const saveScope = document.getElementById("config-save-scope");
    saveScope.hidden = !changes.size;
    saveScope.textContent = changes.size
      ? `将保存：${changedSections.join("、")}`
      : "";
    const dialogStatus = document.getElementById("config-flow-dialog-status");
    dialogStatus.classList.toggle("error", error);
    dialogStatus.textContent =
      dialogContent === rulesPanel
        ? "使用规则单独保存"
        : `${status.textContent}${
            status.textContent.endsWith("。") ? "" : "。"
          }${changes.size ? `将保存：${changedSections.join("、")}。` : ""}`;
    document.getElementById("config-changes-review").hidden = !changes.size;
    document.getElementById(
      "config-changes-summary",
    ).textContent = `查看 ${changes.size} 项待保存修改`;
    const changedList = document.getElementById("config-changes-list");
    changedList.replaceChildren();
    for (const [path, value] of changes) {
      const field = fields.get(path);
      const item = document.createElement("li");
      const link = document.createElement("button");
      link.type = "button";
      link.className = "ghost setup-link";
      link.dataset.settingPath = path;
      link.dataset.settingGroup =
        groups.find((group) =>
          group.prefixes.some((prefix) =>
            prefix.endsWith(".") ? path.startsWith(prefix) : prefix === path,
          ),
        )?.id || "";
      const description =
        path === modePaths[0]
          ? value
            ? "图片级标签（语义）"
            : "分类级标签"
          : path === modePaths[1]
          ? value
            ? "额外模型辅助选择"
            : "主模型自行选择"
          : field?.secret
          ? value
            ? "替换已保存密钥"
            : "清除已保存密钥"
          : typeof value === "boolean"
          ? value
            ? "开启"
            : "关闭"
          : Array.isArray(value)
          ? `${value.length} 项`
          : String(optionLabels[value] || value || "留空 / 自动选择");
      link.textContent = `${
        labels[path] || field?.label || path
      }：${description}`;
      item.append(link);
      changedList.append(item);
    }
    for (const tab of tabs) {
      const dot = tab.querySelector(".settings-dirty-dot");
      if (dot)
        dot.hidden = ![...changes.keys()].some(
          (path) => fields.get(path)?.category === tab.dataset.settingsTab,
        );
    }
  }

  function renderFields(snapshot) {
    const dialogCardId =
      dialogContent && dialogContent !== rulesPanel ? dialogContent.id : "";
    if (dialogCardId) restoreDialogContent();
    modelListRequest++;
    invalidateDecisionTest();
    fields.clear();
    controls.clear();
    sections.replaceChildren();
    const storagePrimary = document.createElement("div");
    storagePrimary.className = "storage-column storage-primary";
    const storageSecondary = document.createElement("div");
    storageSecondary.className = "storage-column storage-secondary";
    sections.append(storagePrimary, storageSecondary);
    const modelOrder = [
      "emotion-model",
      "semantic-query",
      "tool-trigger",
      "semantic",
      "semantic-models",
      "selection-context",
    ];
    for (const group of [...groups].sort(
      (a, b) =>
        (modelOrder.includes(a.id) ? modelOrder.indexOf(a.id) : 100) -
        (modelOrder.includes(b.id) ? modelOrder.indexOf(b.id) : 100),
    )) {
      const card = document.createElement(
        group.advanced ? "details" : "section",
      );
      card.className = "panel config-card";
      if (group.advanced && group.id === activeFlowGroup) card.open = true;
      card.id = `config-group-${group.id}`;
      const header = document.createElement(
        group.advanced ? "summary" : "header",
      );
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
      if (group.advancedPaths) {
        const details = document.createElement("details");
        details.className = "config-advanced-fields";
        const summary = document.createElement("summary");
        summary.textContent = group.advancedTitle;
        const container = document.createElement("div");
        container.className = "config-advanced-body";
        details.append(summary, container);
        body.append(details);
      }
      if (group.id === "emotion-model") {
        const reference = document.createElement("p");
        reference.id = "decision-service-reference";
        reference.className = "config-notice";
        body.append(reference);
      }
      if (group.id === "collect-review") {
        const route = document.createElement("p");
        route.id = "collection-route-note";
        route.className = "config-notice";
        body.append(route);
      }
      if (group.id === "semantic-models") {
        const link = document.createElement("a");
        link.href = "../semantic/index.html";
        link.dataset.navTarget = "../semantic/index.html";
        link.textContent = "前往语义化 →";
        link.className = "semantic-prepare-link";
        header.append(link);
      }
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
      row.dataset.path = field.path;
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
      const baseHint = hints[field.path] || String(field.hint || "").trim();
      help.dataset.baseHint = baseHint;
      help.textContent = baseHint;
      if (field.type === "list")
        help.textContent = `${baseHint}${baseHint ? "；" : ""}每行一项`;
      if (field.secret)
        help.textContent = `${baseHint}${baseHint ? "；" : ""}保存后不回显，${
          field.configured ? "留空保留原值" : "尚未配置"
        }`;
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
        field.special.startsWith("select_provider") ||
        field.path === "decision_service.model"
      ) {
        control = document.createElement("input");
        control.type = "text";
        control.value = field.value;
        control.autocomplete = "off";
        const choices = document.createElement("select");
        choices.id = `${label.htmlFor}-choices`;
        choices.className = "model-picker-options";
        choices.setAttribute("aria-label", `从列表选择${label.textContent}`);
        choices.append(new Option("从列表选择…", ""));
        const picker = document.createElement("div");
        picker.className = "model-picker-input";
        picker.append(control, choices);
        choices.addEventListener("change", () => {
          if (!choices.value) return;
          control.value = choices.value;
          control.dispatchEvent(new Event("input", { bubbles: true }));
          choices.value = "";
        });
        controlBox.classList.add("model-picker");
        controlBox.append(picker);
        const decisionModel = field.path === "decision_service.model";
        const kind =
          field.special === "select_provider_embedding" ? "embedding" : "chat";
        if (decisionModel) {
          control.maxLength = 128;
          help.setAttribute("aria-live", "polite");
          const fetchModels = document.createElement("button");
          fetchModels.type = "button";
          fetchModels.className = "ghost compact fetch-models";
          fetchModels.id = "fetch-decision-models";
          fetchModels.textContent = "获取列表";
          fetchModels.addEventListener("click", async () => {
            const baseUrl = controls
              .get("decision_service.base_url")
              .value.trim();
            if (!baseUrl) {
              help.textContent = "请填写服务地址";
              controls.get("decision_service.base_url").focus();
              return;
            }
            const requestId = ++modelListRequest;
            const hintRequest = ++decisionHintRequest;
            fetchModels.disabled = true;
            fetchModels.textContent = "获取中…";
            help.dataset.state = "";
            help.textContent = "正在获取模型列表";
            try {
              const result = await api.apiPost("settings/decision-models", {
                provider: "systemone",
                base_url: baseUrl,
              });
              if (requestId !== modelListRequest || !control.isConnected)
                return;
              decisionModels = result.models || [];
              decisionModelsBaseUrl = baseUrl;
              choices.replaceChildren(new Option("从列表选择…", ""));
              for (const model of decisionModels)
                choices.append(new Option(model.name || model.id, model.id));
              choices.disabled = !decisionModels.length;
              if (hintRequest === decisionHintRequest)
                help.textContent = decisionModels.length
                  ? `${decisionModels.length} 个模型`
                  : "模型列表为空，可填写模型 ID";
            } catch (error) {
              if (
                requestId !== modelListRequest ||
                hintRequest !== decisionHintRequest ||
                !control.isConnected
              )
                return;
              help.textContent = `${
                error.message || "获取失败"
              }，可填写模型 ID`;
            } finally {
              if (requestId === modelListRequest && control.isConnected) {
                fetchModels.disabled = false;
                fetchModels.textContent = "刷新列表";
              }
            }
          });
          controlBox.append(fetchModels);
          const testService = document.createElement("button");
          testService.type = "button";
          testService.className = "ghost compact fetch-models";
          testService.id = "test-decision-service";
          testService.textContent = "测试连接";
          testService.addEventListener("click", async () => {
            if (testService.disabled || configState.busy) return;
            const requestId = ++decisionHintRequest;
            const draft = {};
            for (const name of [
              "provider",
              "account_id",
              "base_url",
              "token",
              "model",
              "timeout",
            ]) {
              const path = `decision_service.${name}`;
              const field = fields.get(path);
              if (changes.has(path)) draft[name] = changes.get(path);
              else if (field && !field.secret) draft[name] = field.value;
            }
            testService.disabled = true;
            testService.textContent = "测试中…";
            help.dataset.state = "";
            help.textContent =
              "正在发送一次极小的测试请求，可能产生少量额度消耗…";
            try {
              const result = await api.apiPost("settings/decision-test", draft);
              if (requestId !== decisionHintRequest || !control.isConnected)
                return;
              help.dataset.state = result.ok ? "ok" : "error";
              help.textContent = result.message;
            } catch (error) {
              if (requestId !== decisionHintRequest || !control.isConnected)
                return;
              help.dataset.state = "error";
              help.textContent = `${
                error.message || "测试失败"
              }，请检查网络后重试。`;
            } finally {
              if (control.isConnected) {
                testService.disabled = false;
                testService.textContent = "测试连接";
              }
            }
          });
          controlBox.append(testService);
        } else {
          for (const provider of providers[kind] || [])
            choices.append(
              new Option(
                provider.model
                  ? `${provider.id}（${provider.model}）`
                  : provider.id,
                provider.id,
              ),
            );
          control.placeholder =
            field.path === "generation.emotion.llm.provider_id"
              ? "留空跟随本轮主模型"
              : field.path === "semantic.query.provider_id"
              ? "留空跟随当前选图流程的聊天模型"
              : kind === "embedding"
              ? "留空自动选择向量模型"
              : "从列表选择或填写提供商 ID";
          if (providerListErrors.includes(kind))
            help.textContent += "；模型列表不可用，可手动填写";
        }
        choices.disabled = choices.options.length <= 1;
      } else if (
        field.options.length ||
        field.path === "auto_collect.target_pack_id"
      ) {
        control = document.createElement("select");
        if (field.options.length) {
          for (const option of field.options)
            control.add(
              new Option(
                field.path === "auto_collect.judgment_backend" &&
                option === "decision"
                  ? "决策服务分项判断"
                  : optionLabels[option] || option,
                option,
              ),
            );
        } else {
          control.add(new Option("跟随人设与会话规则", ""));
          for (const pack of packs)
            control.add(
              new Option(`${pack.name || pack.id} (${pack.id})`, pack.id),
            );
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
      control.setAttribute("form", "plugin-config-form");
      control.setAttribute("aria-describedby", help.id);
      control.dataset.configPath = field.path;
      if (!control.parentElement) controlBox.prepend(control);
      controls.set(field.path, control);
      if (field.secret) {
        const clear = document.createElement("button");
        clear.type = "button";
        clear.className = "ghost config-secret-clear";
        clear.textContent = "清除";
        clear.setAttribute("aria-label", `清除${label.textContent}`);
        clear.addEventListener("click", () => {
          if (field.path.startsWith("decision_service."))
            invalidateDecisionTest();
          control.removeAttribute("aria-invalid");
          row.querySelector(".config-field-error")?.remove();
          control.setAttribute("aria-describedby", help.id);
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
          updateView();
        });
        controlBox.append(clear);
      }
      row.append(controlBox);
      const body = document.querySelector(
        `#config-group-${assigned.id} .config-card-body`,
      );
      if (assigned.advancedPaths?.includes(field.path))
        body.querySelector(".config-advanced-body").append(row);
      else
        body.insertBefore(row, body.querySelector("details, .config-notice"));
    }
    updateDecisionModelControl();
    if (dialogCardId) mountDialogContent(document.getElementById(dialogCardId));
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
      providerListErrors = snapshot.provider_list_errors || [];
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
      await window.MemeUI.applySecureNavLinks();
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
      if (loaded && pendingGroupAnchor) applyLocationHash();
    }
  }

  for (const tab of tabs) {
    tab.addEventListener("click", () => {
      if (dialogContent) closeSettingsDialog(false);
      active = tab.dataset.settingsTab;
      pendingGroupAnchor = "";
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
  for (const container of [form, configDialog])
    container.addEventListener("click", (event) => {
      if (
        dialogContent?.tagName === "DETAILS" &&
        event.target.closest("summary") ===
          dialogContent.querySelector("summary") &&
        !event.target.closest("a, button")
      )
        event.preventDefault();
      if (event.target.closest("[data-dialog-dismiss]")) {
        closeSettingsDialog();
        return;
      }
      const trigger = event.target.closest(
        "[data-setting-group], [data-setting-tab]",
      );
      if (!trigger || !loaded || configState.busy) return;
      navigateToSetting(
        trigger.dataset.settingGroup,
        trigger.dataset.settingPath,
        trigger.dataset.settingTab,
        trigger,
      );
    });
  configMask.addEventListener("click", (event) => {
    if (event.target === configMask) closeSettingsDialog();
  });
  configDialogReset.addEventListener("click", () => resetPrompts.click());
  for (const input of dimensionInputs) {
    input.addEventListener("change", () => {
      if (!loaded || configState.busy || !input.checked) return;
      const values = [
        form.querySelector('input[name="selection-method"]:checked').value ===
          "semantic",
        form.querySelector('input[name="selection-agent"]:checked').value ===
          "assisted",
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
  function applyLocationHash() {
    const raw = location.hash.slice(1);
    const rules = raw === "config-group-rules";
    const group = raw.startsWith("config-group-")
      ? groups.find((item) => item.id === raw.slice("config-group-".length))
      : null;
    const nextTab = rules ? "rules" : group?.category || raw;
    if (!tabs.some((tab) => tab.dataset.settingsTab === nextTab)) {
      pendingGroupAnchor = "";
      return;
    }
    active = nextTab;
    search.value = "";
    pendingGroupAnchor = group?.id || (rules ? "rules" : "");
    if (configState.busy) {
      pendingGroupAnchor ||= raw;
      updateView();
    } else if ((group || rules) && loaded)
      navigateToSetting(group?.id, "", rules ? "rules" : "");
    else {
      if (dialogContent) closeSettingsDialog(false);
      updateView();
    }
  }
  applyLocationHash();
  window.addEventListener("hashchange", applyLocationHash);
  for (const container of [form, configDialog])
    container.addEventListener("input", (event) => {
      const control = event.target;
      const path = control.dataset.configPath;
      const field = fields.get(path);
      if (!field) return;
      if (path.startsWith("decision_service.")) invalidateDecisionTest();
      if (path === "storage.provider") activeFlowGroup = "";
      if (
        path === "decision_service.base_url" &&
        decisionProvider() !== "cloudflare"
      ) {
        modelListRequest++;
        decisionModels = [];
        decisionModelsBaseUrl = "";
        const choices = document.getElementById(
          "config-decision_service-model-choices",
        );
        if (choices) {
          choices.replaceChildren(new Option("从列表选择…", ""));
          choices.disabled = true;
        }
        const button = document.getElementById("fetch-decision-models");
        if (button) {
          button.disabled = false;
          button.textContent = "获取列表";
          const modelHint = document.getElementById(
            "config-decision_service-model-hint",
          );
          modelHint.textContent = `${modelHint.dataset.baseHint}；服务地址已修改`;
        }
      }
      if (path === "decision_service.provider") {
        modelListRequest++;
        decisionModels = [];
        decisionModelsBaseUrl = "";
        // 草稿稍后才写入，先使用当前控件值更新模型候选。
        updateDecisionModelControl(control.value);
      }
      control.removeAttribute("aria-invalid");
      control
        .closest(".config-field")
        ?.querySelector(".config-field-error")
        ?.remove();
      control.setAttribute("aria-describedby", `${control.id}-hint`);
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
      updateView();
    });
  resetPrompts.addEventListener("click", async () => {
    if (!loaded || configState.busy) return;
    const confirmed = await window.MemeUI.confirm({
      title: "恢复默认模板",
      message:
        "将前缀、后缀和示例恢复为默认内容，保留示例开关。填入后可检查修改，再保存生效。",
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
        showFieldError(
          path,
          `请检查「${labels[path] || fields.get(path).label}」的填写内容。`,
        );
        return;
      }
    }
    const setup = describeSetup(
      Object.fromEntries(
        [...fields].map(([path, field]) => [
          path,
          changes.has(path) ? changes.get(path) : field.value,
        ]),
      ),
      {
        providers,
        tokenConfigured: changes.has("decision_service.token")
          ? Boolean(changes.get("decision_service.token"))
          : Boolean(fields.get("decision_service.token")?.configured),
        accountIdConfigured:
          !changes.has("decision_service.account_id") &&
          Boolean(fields.get("decision_service.account_id")?.configured),
      },
    );
    if (setup.consumers.length && setup.serviceIssues.length) {
      const issue = setup.serviceIssues[0];
      showFieldError(issue.path, issue.text);
      return;
    }
    configState.busy = true;
    notice.hidden = true;
    updateState("正在保存并应用设置…");
    let message = "";
    let failed = false;
    let errorPath = "";
    const savedSections = [
      ...new Set(
        [...changes.keys()]
          .map((path) => categories[fields.get(path)?.category]?.[0])
          .filter(Boolean),
      ),
    ];
    try {
      const result = await api.apiPost("settings/config", {
        revision,
        changes: Object.fromEntries(changes),
      });
      revision = result.revision;
      providers = result.providers || providers;
      providerListErrors = result.provider_list_errors || [];
      changes.clear();
      renderFields(result);
      await window.MemeUI.applySecureNavLinks();
      message = result.message || "设置已保存并生效。";
      if (result.applied !== false && savedSections.length)
        message = `已保存并生效：${savedSections.join("、")}。`;
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
      errorPath =
        [...changes.keys()].find((path) =>
          [fields.get(path)?.label, labels[path]].some(
            (label) => label && message.includes(label),
          ),
        ) || "";
      notice.hidden = false;
      notice.classList.add("error");
      notice.textContent = message;
    } finally {
      configState.busy = false;
      updateState(message, failed);
      if (errorPath) showFieldError(errorPath, message);
      else if (pendingGroupAnchor) applyLocationHash();
      else if (dialogContent && !configDialog.contains(document.activeElement))
        configDialogTitle.focus();
    }
  });
  updateView();
  await loadConfig();
}

void initPluginConfig().catch((error) => window.MemeUI.showPageError(error));
