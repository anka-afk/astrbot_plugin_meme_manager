export function setupPackTransfer({ apiGet, apiPost, onComplete, onBusy }) {
  const dialog = document.getElementById("pack-transfer-dialog");
  const packSelect = document.getElementById("pack-transfer-pack");
  const modeSelect = document.getElementById("pack-transfer-mode");
  const categorySelect = document.getElementById("pack-transfer-category");
  const categoryField = document.getElementById("pack-transfer-category-field");
  const rows = document.getElementById("pack-transfer-plan");
  const status = document.getElementById("pack-transfer-status");
  const confirm = document.getElementById("pack-transfer-confirm");
  const cancel = document.getElementById("pack-transfer-cancel");
  const fields = document.getElementById("pack-transfer-fields");
  let sourcePackId = "",
    items = [],
    mapping = Object.create(null),
    plan = null,
    sequence = 0,
    busy = false;

  async function preview() {
    const current = ++sequence;
    plan = null;
    confirm.disabled = true;
    rows.replaceChildren();
    categoryField.hidden = modeSelect.value !== "target";
    if (!packSelect.value) return;
    status.textContent = "正在检查分类…";
    const payload = {
      source_pack_id: sourcePackId,
      target_pack_id: packSelect.value,
      mode: modeSelect.value,
      target_category: categorySelect.value,
      category_mapping: { ...mapping },
      items,
    };
    try {
      const data = await apiPost("packs/move-images", {
        ...payload,
        preview: true,
      });
      if (current !== sequence || !dialog.open) return;
      const categories = Object.keys(data.target_categories).sort((a, b) =>
        a.localeCompare(b, "zh-CN"),
      );
      categorySelect.replaceChildren(
        new Option(
          categories.length
            ? "请选择目标分类"
            : "没有已有分类，可选择保留原分类",
          "",
        ),
      );
      for (const category of categories)
        categorySelect.add(new Option(category, category));
      categorySelect.value = payload.target_category;
      for (const row of data.plan) {
        const card = document.createElement("div");
        card.className = `pack-transfer-row${row.conflict ? " conflict" : ""}`;
        const title = document.createElement("strong");
        title.textContent = `${row.source_category}（${row.count} 张）→ ${row.target_category || "待选择"}`;
        const detail = document.createElement("p");
        detail.textContent = row.conflict
          ? row.target_category
            ? "同名分类描述不同，请手动指定目标分类"
            : "请在上方选择目标分类"
          : row.action === "create"
            ? "新建分类并复制来源描述"
            : payload.mode === "target"
              ? "使用指定分类，忽略来源分类及描述"
              : "使用目标包已有分类";
        card.append(title, detail);
        if (payload.mode === "preserve") {
          const descriptions = document.createElement("details");
          const summary = document.createElement("summary");
          summary.textContent = "查看分类描述";
          const text = document.createElement("p");
          text.textContent = `来源：${row.source_description || "（空）"}\n目标：${row.action === "create" ? "（将新建）" : row.target_description || "（空）"}`;
          descriptions.append(summary, text);
          card.append(descriptions);
          if (row.conflict || mapping[row.source_category]) {
            const select = document.createElement("select");
            select.setAttribute(
              "aria-label",
              `${row.source_category} 的目标分类`,
            );
            select.add(new Option("请选择已有分类", ""));
            categories.forEach((category) =>
              select.add(new Option(category, category)),
            );
            select.value = mapping[row.source_category] || "";
            select.addEventListener("change", () => {
              mapping[row.source_category] = select.value;
              void preview();
            });
            card.append(select);
          }
        }
        rows.append(card);
      }
      plan = { ...payload, plan_token: data.plan_token };
      confirm.disabled = !data.ready;
      const targetName =
        packSelect.selectedOptions[0]?.textContent || packSelect.value;
      confirm.textContent = `移动 ${items.length} 张到「${targetName}${payload.mode === "target" && payload.target_category ? ` / ${payload.target_category}` : ""}」`;
      status.textContent = data.ready
        ? "请确认以上去向。重名图片将跳过，源分类会保留。"
        : "请先选择目标分类并解决所有分类冲突。";
    } catch (error) {
      if (current === sequence && dialog.open)
        status.textContent = `无法预览：${error.message}`;
    }
  }

  packSelect.addEventListener("change", () => {
    categorySelect.value = "";
    mapping = Object.create(null);
    void preview();
  });
  modeSelect.addEventListener("change", () => {
    mapping = Object.create(null);
    void preview();
  });
  categorySelect.addEventListener("change", () => void preview());
  document
    .getElementById("pack-transfer-retry")
    .addEventListener("click", () => {
      if (!busy) void preview();
    });
  cancel.addEventListener("click", () => dialog.close());
  dialog.addEventListener("cancel", (event) => {
    if (busy) event.preventDefault();
  });
  dialog.addEventListener("close", () => {
    sequence++;
    plan = null;
  });
  confirm.addEventListener("click", async () => {
    if (busy || !plan || confirm.disabled) return;
    busy = true;
    fields.disabled = true;
    confirm.disabled = true;
    cancel.disabled = true;
    onBusy(true);
    status.textContent = "正在移动图片，请稍候…";
    try {
      const result = await apiPost("packs/move-images", {
        ...plan,
        preview: false,
      });
      dialog.close();
      await onComplete(result, sourcePackId);
    } catch (error) {
      plan = null;
      status.textContent = `移动未完成：${error.message}。请点击“重新检查”后再确认。`;
    } finally {
      busy = false;
      fields.disabled = false;
      cancel.disabled = false;
      onBusy(false);
    }
  });

  return {
    async open(packId, selectedItems) {
      if (busy || dialog.open || !selectedItems.length) return;
      sourcePackId = packId;
      items = selectedItems;
      mapping = Object.create(null);
      plan = null;
      modeSelect.value = "target";
      categorySelect.replaceChildren(new Option("请选择目标分类", ""));
      packSelect.replaceChildren();
      rows.replaceChildren();
      confirm.disabled = true;
      confirm.textContent = "确认移动";
      status.textContent = "正在加载其他表情包…";
      dialog.showModal();
      const current = ++sequence;
      try {
        const data = await apiGet("packs");
        if (current !== sequence || !dialog.open) return;
        const packs = (data.packs || []).filter((pack) => pack.id !== packId);
        packs.forEach((pack) =>
          packSelect.add(
            new Option(`${pack.name || pack.id} (${pack.id})`, pack.id),
          ),
        );
        if (!packs.length) {
          status.textContent = "没有其他已安装的表情包，请先在资源广场添加。";
          return;
        }
        await preview();
      } catch (error) {
        if (current === sequence && dialog.open)
          status.textContent = `加载失败：${error.message}。请关闭后重试。`;
      }
    },
  };
}
