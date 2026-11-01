const reasonLabels = {
  meme_uncertain: "表情用途不确定",
  content_review_required: "内容规则需要人工复核",
  category_unknown: "无法确定现有分类",
  category_probability_missing: "分类概率缺失",
  category_confidence_low: "分类概率不足",
  decision_not_ready: "决策服务尚未就绪",
  decision_failed: "决策判断失败",
};

export function collectionConfidence(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1
    ? `${Math.round(value * 100)}%`
    : "未知";
}

export function collectionReviewReason(item) {
  if (!item?.review_required) return "";
  return item.review_reason || (item.review_reasons || [])
    .map((reason) => reasonLabels[reason] || "需要人工复核")
    .join("；") || "需要人工复核";
}

export function collectionStages(item) {
  if (item?.judgment_backend === "vision") return "视觉模型合并判断";
  if (item?.judgment_backend !== "decision") return "判断来源未知（旧记录）";
  const status = item.stage_status || {};
  const labels = {
    completed: "完成", failed: "失败", accepted: "通过", rejected: "拒绝",
    uncertain: "不确定", selected: "已选", allow: "通过", reject: "拒绝",
    review: "需复核", not_used: "未启用",
  };
  const stages = [["understanding", "图片理解"], ["judgment", "决策"],
    ["meme_usage", "表情用途"], ["content_rule", "内容规则"], ["category", "分类"]];
  const text = stages.filter(([key]) => status[key])
    .map(([key, title]) => `${title}：${labels[status[key]] || "未知"}`);
  return `决策服务 · ${text.join(" · ") || (item.decision_status === "failed" ? "判断失败" : "阶段状态未知")}`;
}

export function collectionAvailability(data) {
  if (data?.ready !== false) return "";
  const reasons = {
    disabled: "自动收集已关闭", closed: "自动收集已停止",
    missing_vision_provider: "尚未选择视觉模型",
    vision_provider_pending: "正在等待视觉模型加载",
    vision_provider_unavailable: "视觉模型当前不可用",
    vision_provider_not_visual: "所选模型不支持图片输入",
    missing_token: "决策服务尚未配置密钥", invalid_token: "决策服务密钥配置无效",
    missing_model: "决策服务尚未配置模型", invalid_model: "决策模型配置无效",
    missing_base_url: "决策服务地址为空", invalid_base_url: "决策服务地址无效",
    invalid_timeout: "决策服务超时配置无效", invalid_max_concurrency: "决策服务并发配置无效",
  };
  return reasons[data.reason] || "自动收集暂未就绪，请到设置中心检查模型配置";
}
