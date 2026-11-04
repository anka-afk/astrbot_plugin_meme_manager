export function describeSetup(
  values,
  {
    providers,
    tokenConfigured,
    accountIdConfigured = false,
    semanticStatus,
    defaultPack,
    embeddingChanged = false,
    providerListErrors = [],
  },
) {
  const semantic = Boolean(values["semantic.enabled"]);
  const postReply = Boolean(values["generation.emotion.llm.enabled"]);
  const decision =
    postReply && values["generation.emotion.llm.backend"] === "decision";
  const collectionEnabled = Boolean(values["auto_collect.enabled"]);
  const collectionDecision =
    values["auto_collect.judgment_backend"] === "decision";
  const generated = values["semantic.query.source"] !== "reply_text";
  const chatNames = new Map(
    (providers.chat || []).map((item) => [
      item.id,
      item.model ? `${item.id} · ${item.model}` : item.id,
    ]),
  );
  const selectionProvider = values["generation.emotion.llm.provider_id"] || "";
  const queryProvider = values["semantic.query.provider_id"] || "";
  const embeddingId = values["semantic.embedding_provider_id"] || "";
  const embedding =
    (embeddingId
      ? (providers.embedding || []).find((item) => item.id === embeddingId)
      : (providers.embedding || [])[0]) ||
    (embeddingId && providerListErrors.includes("embedding")
      ? { id: embeddingId }
      : null);
  const rows = [];
  const selectionIssues = [];
  const collectionIssues = [];
  const serviceIssues = [];
  const consumers = [
    decision && "辅助配图",
    collectionEnabled && collectionDecision && "自动收集",
  ].filter(Boolean);
  const cloudflare = values["decision_service.provider"] === "cloudflare";

  if (cloudflare) {
    const accountId = String(
      values["decision_service.account_id"] || "",
    ).trim();
    if (!accountId && !accountIdConfigured)
      serviceIssues.push({
        text: "填写 Cloudflare 账户 ID",
        group: "decision-service",
        path: "decision_service.account_id",
      });
    else if (accountId && !/^[0-9a-fA-F]{32}$/.test(accountId))
      serviceIssues.push({
        text: "检查 Cloudflare 账户 ID（32 位十六进制）",
        group: "decision-service",
        path: "decision_service.account_id",
      });
  } else if (!values["decision_service.base_url"])
    serviceIssues.push({
      text: "填写决策服务地址",
      group: "decision-service",
      path: "decision_service.base_url",
    });
  else {
    try {
      const url = new URL(values["decision_service.base_url"]);
      if (
        !["http:", "https:"].includes(url.protocol) ||
        !url.hostname ||
        url.username ||
        url.password ||
        url.search ||
        url.hash
      )
        throw new Error();
    } catch {
      serviceIssues.push({
        text: "检查决策服务地址",
        group: "decision-service",
        path: "decision_service.base_url",
      });
    }
  }
  if (!tokenConfigured)
    serviceIssues.push({
      text: "填写决策服务密钥",
      group: "decision-service",
      path: "decision_service.token",
    });
  const decisionModel = String(values["decision_service.model"] || "").trim();
  if (!decisionModel)
    serviceIssues.push({
      text: cloudflare ? "填写 Cloudflare 决策模型 ID" : "填写决策模型 ID",
      group: "decision-service",
      path: "decision_service.model",
    });
  else if (
    cloudflare &&
    !["clef", "clef-flash", "clef-omni"].includes(decisionModel)
  )
    serviceIssues.push({
      text: "决策模型 ID 仅支持 clef、clef-flash、clef-omni",
      group: "decision-service",
      path: "decision_service.model",
    });

  const selector = decision
    ? `决策模型 · ${values["decision_service.model"] || "尚未填写"}`
    : postReply
    ? selectionProvider
      ? chatNames.get(selectionProvider) ||
        `${selectionProvider}（未在列表中找到，请核对 ID）`
      : "跟随本轮主模型"
    : "由本轮主模型完成";
  rows.push({
    label: "标签选择",
    value: selector,
    group: postReply ? "emotion-model" : semantic ? "tool-trigger" : "prompts",
  });
  if (decision) selectionIssues.push(...serviceIssues);
  if (
    postReply &&
    !decision &&
    selectionProvider &&
    !providerListErrors.includes("chat") &&
    !chatNames.has(selectionProvider)
  )
    selectionIssues.push({
      text: "重新选择可用的配图聊天模型",
      group: "emotion-model",
      path: "generation.emotion.llm.provider_id",
    });

  if (semantic) {
    if (postReply) {
      const inherited = decision
        ? "跟随本轮主模型"
        : selectionProvider
        ? chatNames.get(selectionProvider) || selectionProvider
        : "跟随本轮主模型";
      rows.push({
        label: "检索词",
        value: generated
          ? `聊天模型生成 · ${
              queryProvider
                ? chatNames.get(queryProvider) ||
                  `${queryProvider}（未在列表中找到，请核对 ID）`
                : inherited
            }`
          : "直接使用可见回复文本",
        group: "semantic-query",
      });
      if (
        generated &&
        queryProvider &&
        !chatNames.has(queryProvider) &&
        !providerListErrors.includes("chat")
      )
        selectionIssues.push({
          text: "重新选择可用的检索词模型",
          group: "semantic-query",
          path: "semantic.query.provider_id",
        });
    } else
      rows.push({
        label: "语义检索",
        value: "由主模型调用搜索工具",
        group: "tool-trigger",
      });
    rows.push({
      label: "向量模型",
      value: embedding
        ? `${embeddingId ? "" : "自动选择 · "}${embedding.id}${
            embedding.model ? ` · ${embedding.model}` : ""
          }`
        : "尚无可用向量模型",
      group: "semantic-models",
    });
    if (!embedding)
      selectionIssues.push({
        text: "配置可用向量模型",
        group: "semantic-models",
        path: "semantic.embedding_provider_id",
      });
    const packName = defaultPack?.name || defaultPack?.id;
    rows.push({
      label: "默认包准备",
      value: embeddingChanged
        ? "向量模型有修改，保存后重新检查"
        : !packName
        ? "尚无默认表情包，请先配置使用规则"
        : !semanticStatus
        ? `「${packName}」状态暂不可用`
        : semanticStatus.index_ready && semanticStatus.semantic_caption_complete
        ? `「${packName}」描述与索引已就绪`
        : `「${packName}」尚未完全就绪，前往语义化检查`,
      group: "semantic-models",
    });
    if (!defaultPack)
      selectionIssues.push({ text: "选择默认表情包", tab: "rules" });
  }

  const visionId = values["auto_collect.vision_provider_id"] || "";
  if (
    collectionEnabled &&
    (!visionId ||
      (!chatNames.has(visionId) && !providerListErrors.includes("chat")))
  )
    collectionIssues.push({
      text: visionId ? "重新选择可用的图片理解模型" : "选择图片理解模型",
      group: "collect-recognition",
      path: "auto_collect.vision_provider_id",
    });
  if (collectionEnabled && collectionDecision)
    collectionIssues.push(...serviceIssues);
  const manualReview = Boolean(values["auto_collect.manual_review"]);
  const collectionRoute = manualReview
    ? "符合收录条件的图片进入待审核。"
    : collectionDecision
    ? "分类明确的图片自动入包，其余进入待审核。"
    : "符合收录条件的图片自动入包，分类不确定的归入「待分类」。";
  const requestNote = postReply
    ? semantic
      ? `${generated ? "检索词生成 + " : ""}向量检索 + ${
          decision ? "决策服务" : "聊天模型"
        }选图`
      : `${decision ? "决策服务" : "聊天模型"}选分类`
    : semantic
    ? `${
        values["semantic.tool_probability_enabled"]
          ? "按概率尝试一次检索"
          : "主模型按需调用搜索工具"
      }；失败时只回复文字，不切换其他模式补发`
    : "无需额外调用";
  return {
    rows,
    selectionIssues,
    collectionIssues,
    serviceIssues,
    consumers,
    requestNote,
    preparationWarning:
      semantic &&
      (!embedding ||
        embeddingChanged ||
        !defaultPack ||
        !semanticStatus?.index_ready ||
        !semanticStatus?.semantic_caption_complete),
    collectionRoute,
    collectionModel: visionId
      ? chatNames.get(visionId) || `${visionId}（未在列表中找到，请核对 ID）`
      : "尚未选择",
    collectionNote: collectionEnabled
      ? collectionDecision
        ? "图片理解 + 1 次决策请求"
        : "视觉模型单次请求"
      : "未开启",
  };
}
