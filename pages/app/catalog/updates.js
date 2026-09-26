export function setupCatalogUpdates({ apiGet, apiPost, onDone, onBusy }) {
  const dialog = document.createElement("dialog");
  dialog.id = "update-dialog";
  dialog.setAttribute("aria-labelledby", "update-title");
  dialog.innerHTML = `
    <h2 id="update-title" class="text-h3 pa-4 pb-0 pl-6">更新表情包</h2>
    <p id="update-status" role="status" aria-live="polite"></p>
    <div id="update-review" hidden>
      <fieldset id="update-fields">
        <label>更新方式<select id="update-strategy">
          <option value="merge">合并更新，保留本地修改</option>
          <option value="merge_upstream">合并更新，保留上游更改</option>
          <option value="add">只添加新内容</option>
          <option value="replace">完整替换</option>
        </select></label>
        <p id="update-strategy-help"></p>
        <p id="update-summary"></p>
        <div id="update-images"></div>
        <details><summary>查看完整文件清单</summary><div id="update-files"></div></details>
      </fieldset>
      <label id="update-warning" class="update-warning" hidden>
        <input id="update-ack" type="checkbox" />
        <span>我确认完整替换会丢弃未选择保留的本地内容及分类调整。</span>
      </label>
    </div>
    <div id="update-recovery" hidden>
      <label>选择恢复时间<select id="update-backup"></select></label>
      <p>恢复会替换当前包的全部内容，包括更新后添加的表情。</p>
      <label class="update-warning"><input id="restore-ack" type="checkbox" />我确认恢复所选备份</label>
    </div>
    <p id="update-error" role="alert"></p>
    <div class="update-actions">
      <button id="update-cancel" class="ghost" type="button" hidden>取消下载</button>
      <button id="update-close" class="ghost" type="button">关闭</button>
      <button id="update-apply" type="button" disabled>确认更新</button>
    </div>`;
  document.body.append(dialog);
  const get = (id) => dialog.querySelector(`#update-${id}`);
  const status = get("status"),
    error = get("error"),
    submit = get("apply");
  const storageKey = "meme-community-update-job";
  let previewRequest = 0;
  let activeJobId = "";
  let data = {},
    plan = null,
    working = false,
    recovering = false;
  const imageCache = new Map();
  let imageObserver = null;

  function loadImage(image) {
    const { side, path } = image.dataset;
    const key = `${data.session}:${side}:${path}`;
    if (!imageCache.has(key)) {
      imageCache.set(
        key,
        apiGet("community/update/image_data", {
          session: data.session,
          side,
          path,
        }).then((response) => response.data_url),
      );
    }
    imageCache.get(key).then(
      (url) => {
        if (url && image.isConnected !== false) image.src = url;
      },
      () => {
        image.alt = "预览不可用";
        imageCache.delete(key);
      },
    );
  }

  async function preview({ preserveImages = false } = {}) {
    const request = ++previewRequest;
    plan = null;
    submit.disabled = true;
    if (!preserveImages) {
      get("fields").disabled = true;
      get("backup").disabled = true;
    }
    error.textContent = "";
    try {
      const response = await apiPost("community/update", {
        ...data,
        action: recovering ? "restore_preview" : "preview",
      });
      if (request !== previewRequest) return;
      plan = response;
      if (!recovering) {
        let defaultsAdded = false;
        for (const row of plan.categories) {
          if (
            !row.target &&
            data.categories[row.source] === undefined &&
            Object.hasOwn(plan.local_categories, row.source)
          ) {
            data.categories[row.source] = row.source;
            defaultsAdded = true;
          }
        }
        if (defaultsAdded) {
          await preview();
          return;
        }
      }
      if (recovering) {
        status.textContent = `当前版本 ${plan.current_version}，共 ${plan.current_images} 张表情。将恢复至 ${plan.version}。`;
        dialog.querySelector("#restore-ack").checked = false;
        return;
      }
      status.textContent = `当前 ${plan.current_version} → 广场 ${plan.latest_version}`;
      get("strategy-help").textContent = {
        merge:
          "上游改动仅自动覆盖未在本地修改的文件。冲突默认保留本地，上游删除默认保留。",
        merge_upstream:
          "优先应用上游的新增、修改和删除，同名分类描述使用上游版本；保留本地独有内容。",
        add: "仅加入本地没有的新内容；已有文件和分类描述保持不变。",
        replace: `危险操作：当前 ${plan.local_images} 张表情和 ${plan.local_category_count} 个分类将被上游内容整体替换。`,
      }[data.strategy];
      const counts = plan.counts;
      const selectedCount = plan.rows.filter(
        (row) =>
          row.kind !== "unchanged" &&
          ["add", "update", "copy"].includes(row.action),
      ).length;
      const deletedCount = plan.rows.filter(
        (row) => row.action === "delete",
      ).length;
      get(
        "summary",
      ).textContent = `上游新增 ${counts.added}，修改 ${counts.changed}，删除 ${counts.upstream_deleted}；本地新增 ${counts.local_added}，冲突 ${counts.conflicts}。本次纳入 ${selectedCount} 张，删除 ${deletedCount} 张。`;
      get("warning").hidden = data.strategy !== "replace";
      if (!preserveImages) get("ack").checked = false;
      if (preserveImages) {
        for (const section of get("images").children) {
          const category = section.dataset.category;
          if (!category) continue;
          const rows = plan.rows.filter(
            (row) =>
              row.path.split("/")[1] === category &&
              (["added", "changed", "conflict"].includes(row.kind) ||
                (row.kind === "upstream_deleted" && row.local_exists)),
          );
          const selected = rows.filter((row) =>
            ["add", "update", "copy", "delete"].includes(row.action),
          ).length;
          section.children[0].textContent = `${category}（已选 ${selected} / ${rows.length} 张）`;
          const categorySettings = Array.from(section.children).find(
            (child) => child.className === "update-conflict",
          );
          if (categorySettings) {
            categorySettings.hidden = !plan.categories.some(
              (row) => row.source === category,
            );
          }
        }
      } else {
        imageObserver?.disconnect();
        imageObserver =
          typeof IntersectionObserver === "function"
            ? new IntersectionObserver(
                (entries) => {
                  for (const entry of entries) {
                    if (!entry.isIntersecting) continue;
                    imageObserver.unobserve(entry.target);
                    loadImage(entry.target);
                  }
                },
                { root: dialog, rootMargin: "200px" },
              )
            : null;
        const images = get("images");
        images.replaceChildren();
        const groups = new Map();
        for (const row of plan.rows) {
          if (
            !["added", "changed", "conflict"].includes(row.kind) &&
            !(row.kind === "upstream_deleted" && row.local_exists)
          )
            continue;
          const category = row.path.split("/")[1];
          if (!groups.has(category)) groups.set(category, []);
          groups.get(category).push(row);
        }
        for (const [category, rows] of groups) {
          const section = document.createElement("section");
          section.className = "update-image-category";
          section.dataset.category = category;
          const heading = document.createElement("h3");
          const selected = rows.filter((row) =>
            ["add", "update", "copy", "delete"].includes(row.action),
          ).length;
          heading.textContent = `${category}（已选 ${selected} / ${rows.length} 张）`;
          const grid = document.createElement("div");
          grid.className = "update-image-grid";
          section.append(heading);
          const categoryRow = plan.categories.find(
            (row) => row.source === category,
          );
          if (categoryRow) {
            const group = document.createElement("div");
            group.className = "update-conflict";
            const description = document.createElement("p");
            description.className = "update-category-description";
            description.textContent = `分类“${
              categoryRow.source
            }”描述不同：\n本地：“${categoryRow.local_description}”\n上游：“${
              categoryRow.remote_description
            }”\n${
              data.strategy === "merge_upstream"
                ? "新内容默认放入同名分类，分类描述将使用上游版本。"
                : "请选择新内容放入的分类。"
            }`;
            const select = document.createElement("select");
            select.setAttribute(
              "aria-label",
              `${categoryRow.source} 的目标分类`,
            );
            select.add(new Option("请选择目标分类", ""));
            for (const name of Object.keys(plan.local_categories))
              select.add(new Option(name, name));
            select.add(new Option("创建不同名称的新分类", "__new__"));
            const value =
              data.categories[categoryRow.source] ??
              (categoryRow.create && categoryRow.target
                ? `new:${categoryRow.target}`
                : categoryRow.target || categoryRow.source);
            select.value = value.startsWith("new:") ? "__new__" : value;
            const input = document.createElement("input");
            input.placeholder = "输入新分类名称";
            input.setAttribute(
              "aria-label",
              `${categoryRow.source} 的新分类名称`,
            );
            input.value = value.startsWith("new:") ? value.slice(4) : "";
            input.hidden = select.value !== "__new__";
            select.addEventListener("change", () => {
              input.hidden = select.value !== "__new__";
              if (!input.hidden) {
                submit.disabled = true;
                input.focus();
                return;
              }
              data.categories[categoryRow.source] = select.value;
              void preview();
            });
            input.addEventListener("change", () => {
              data.categories[categoryRow.source] = `new:${input.value.trim()}`;
              void preview();
            });
            input.addEventListener("input", () => {
              submit.disabled = true;
            });
            group.append(description, select, input);
            section.append(group);
          }
          for (const row of rows) {
            const card = document.createElement("article");
            card.className = "update-image-card";
            const title = document.createElement("strong");
            title.textContent = row.path.split("/").at(-1);
            const kind = document.createElement("span");
            kind.className = "update-image-kind";
            kind.textContent = {
              added: "新增",
              changed: "修改",
              conflict: "冲突",
              upstream_deleted: "上游删除",
            }[row.kind];
            const comparison = document.createElement("div");
            comparison.className = "update-image-comparison";
            for (const [side, path, label] of [
              ...(row.local_exists && row.kind !== "added"
                ? [["local", row.local_path, "本地"]]
                : []),
              ...(row.kind === "upstream_deleted"
                ? []
                : [["upstream", row.path, "上游"]]),
            ]) {
              const frame = document.createElement("div");
              frame.className = "update-image-frame";
              const image = document.createElement("img");
              image.alt = `${label}：${title.textContent}`;
              image.loading = "lazy";
              image.dataset.side = side;
              image.dataset.path = path;
              const caption = document.createElement("span");
              caption.textContent = label;
              frame.append(image, caption);
              comparison.append(frame);
              if (imageObserver) imageObserver.observe(image);
              else loadImage(image);
            }
            card.append(title, kind, comparison);
            if (
              row.kind === "conflict" &&
              ["merge", "merge_upstream"].includes(data.strategy)
            ) {
              const select = document.createElement("select");
              select.setAttribute(
                "aria-label",
                `${title.textContent} 的冲突处理`,
              );
              for (const [value, text] of [
                ["local", "保留本地"],
                ["upstream", "使用上游"],
                ["both", "两份都保留"],
              ])
                select.add(new Option(text, value));
              select.value =
                data.choices[row.path] ||
                (data.strategy === "merge_upstream" ? "upstream" : "local");
              select.addEventListener("change", () => {
                data.choices[row.path] = select.value;
                data.skip = data.skip.filter((path) => path !== row.path);
                void preview();
              });
              card.append(select);
            }
            if (row.kind === "upstream_deleted") {
              const label = document.createElement("label");
              label.className = "update-image-choice";
              const check = document.createElement("input");
              check.type = "checkbox";
              check.checked = row.action === "delete";
              const text = document.createElement("span");
              text.textContent = "同步删除本地图片";
              check.addEventListener("change", () => {
                if (["merge_upstream", "replace"].includes(data.strategy)) {
                  data.keep_deleted = data.keep_deleted.filter(
                    (path) => path !== row.local_path,
                  );
                  if (!check.checked) data.keep_deleted.push(row.local_path);
                } else {
                  data.remove = data.remove.filter(
                    (path) => path !== row.local_path,
                  );
                  if (check.checked) data.remove.push(row.local_path);
                }
                void preview({ preserveImages: true });
              });
              label.append(check, text);
              card.append(label);
            } else if (row.selectable) {
              const label = document.createElement("label");
              label.className = "update-image-choice";
              const check = document.createElement("input");
              check.type = "checkbox";
              check.checked = !data.skip.includes(row.path);
              const text = document.createElement("span");
              text.textContent = "纳入本次更新";
              check.addEventListener("change", () => {
                data.skip = data.skip.filter((path) => path !== row.path);
                if (!check.checked) data.skip.push(row.path);
                void preview({ preserveImages: true });
              });
              label.append(check, text);
              card.append(label);
            } else {
              const note = document.createElement("p");
              note.className = "hint";
              note.textContent = "当前方式保留本地图片";
              card.append(note);
            }
            grid.append(card);
          }
          section.append(grid);
          images.append(section);
        }
        if (!groups.size) {
          const empty = document.createElement("p");
          empty.className = "hint";
          empty.textContent = "没有新增、修改或待删除的图片。";
          images.append(empty);
        }
      }
      const files = get("files");
      files.replaceChildren();
      const labels = {
        keep: "保留",
        add: "添加",
        update: "更新",
        copy: "保留两份",
        delete: "删除",
      };
      for (const row of plan.rows) {
        if (row.kind === "unchanged") continue;
        const line = document.createElement("p");
        line.textContent = `${labels[row.action]}：${row.path}${
          row.destination !== row.path ? ` → ${row.destination}` : ""
        }`;
        files.append(line);
      }
      submit.disabled =
        !plan.ready || (data.strategy === "replace" && !get("ack").checked);
    } catch (reason) {
      if (request !== previewRequest) return;
      plan = null;
      error.textContent = reason?.message || String(reason);
    } finally {
      if (request === previewRequest && !preserveImages) {
        get("fields").disabled = false;
        get("backup").disabled = false;
      }
    }
  }

  async function monitor(jobId) {
    activeJobId = jobId;
    working = true;
    onBusy(true);
    get("review").hidden = get("recovery").hidden = true;
    submit.disabled = true;
    get("close").textContent = "后台执行";
    try {
      sessionStorage.setItem(storageKey, jobId);
    } catch {
      /* Storage may be unavailable. */
    }
    let failures = 0;
    try {
      while (true) {
        let job;
        try {
          job = await apiGet("community/update/status", { job_id: jobId });
          failures = 0;
        } catch (reason) {
          if (String(reason?.message || reason).includes("更新任务不存在")) {
            try {
              sessionStorage.removeItem(storageKey);
            } catch {
              /* Storage may be unavailable. */
            }
            throw reason;
          }
          if (++failures >= 5) throw reason;
          status.textContent = "连接暂时中断，正在重新查询任务…";
          await new Promise((resolve) => setTimeout(resolve, 1500));
          continue;
        }
        get("cancel").hidden =
          job.action !== "prepare" || job.state !== "running";
        if (job.state === "cancelled") {
          try {
            sessionStorage.removeItem(storageKey);
          } catch {
            /* Storage may be unavailable. */
          }
          status.textContent = "已取消下载更新，本地表情包未修改。";
          break;
        }
        if (job.state === "failed") {
          try {
            sessionStorage.removeItem(storageKey);
          } catch {
            /* Storage may be unavailable. */
          }
          throw new Error(job.message);
        }
        if (job.state === "completed") {
          try {
            sessionStorage.removeItem(storageKey);
          } catch {
            /* Storage may be unavailable. */
          }
          if (job.result.session) {
            recovering = false;
            data = {
              session: job.result.session,
              strategy: "merge_upstream",
              choices: {},
              categories: {},
              remove: [],
              keep_deleted: [],
              skip: [],
            };
            get("strategy").value = data.strategy;
            get("review").hidden = false;
            submit.textContent = "确认更新";
            await preview();
          } else {
            status.textContent =
              job.action === "restore" ? "备份已恢复。" : "更新完成。";
            window.MemeUI?.toast?.(
              job.action === "restore" ? "恢复完成" : "更新完成",
              "success",
            );
          }
          await onDone();
          break;
        }
        status.textContent =
          job.action === "prepare"
            ? "正在下载更新内容…"
            : "正在更新内容，请稍候…";
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
    } catch (reason) {
      error.textContent = `${
        reason?.message || reason
      }。可关闭后重新打开检查。`;
      await onDone();
    } finally {
      working = false;
      activeJobId = "";
      get("cancel").hidden = true;
      onBusy(false);
      get("close").textContent = "关闭";
    }
  }

  get("close").addEventListener("click", () => dialog.close());
  get("cancel").addEventListener("click", async () => {
    if (!activeJobId || get("cancel").disabled) return;
    get("cancel").disabled = true;
    get("cancel").textContent = "正在取消…";
    try {
      await apiPost("community/update", {
        action: "cancel",
        job_id: activeJobId,
      });
    } catch (reason) {
      error.textContent = reason?.message || String(reason);
      get("cancel").disabled = false;
      get("cancel").textContent = "取消下载";
    }
  });
  get("strategy").addEventListener("change", () => {
    data = {
      session: data.session,
      strategy: get("strategy").value,
      choices: {},
      categories: {},
      remove: [],
      keep_deleted: [],
      skip: [],
    };
    void preview();
  });
  get("ack").addEventListener("change", () => {
    submit.disabled = !plan?.ready || !get("ack").checked;
  });
  dialog.querySelector("#restore-ack").addEventListener("change", (event) => {
    submit.disabled = !plan || !event.target.checked;
  });
  get("backup").addEventListener("change", () => {
    data = { backup_id: get("backup").value };
    void preview();
  });
  submit.addEventListener("click", async () => {
    if (working || submit.disabled || !plan) return;
    submit.disabled = true;
    error.textContent = "";
    try {
      const result = await apiPost("community/update", {
        ...data,
        action: recovering ? "restore" : "apply",
        plan_token: plan.plan_token,
        acknowledge_replace: get("ack").checked,
        acknowledge_restore: dialog.querySelector("#restore-ack").checked,
      });
      await monitor(result.job_id);
    } catch (reason) {
      await preview();
      error.textContent = reason?.message || String(reason);
    }
  });
  return {
    async open(pack, state, restore = false) {
      if (working) {
        if (!dialog.open) dialog.showModal();
        return;
      }
      recovering = restore;
      get("cancel").disabled = false;
      get("cancel").textContent = "取消下载";
      previewRequest += 1;
      plan = null;
      error.textContent = "";
      get("title").textContent = `${restore ? "恢复备份" : "更新表情包"}：${
        pack.name || pack.id
      }`;
      get("review").hidden = true;
      get("recovery").hidden = !restore;
      submit.disabled = true;
      submit.textContent = restore ? "确认恢复" : "确认更新";
      dialog.showModal();
      if (restore) {
        const backups = get("backup");
        backups.replaceChildren();
        for (const backup of state.backups || [])
          backups.add(
            new Option(
              `${new Date(backup.created_at * 1000).toLocaleString()}（${
                backup.version
              }）`,
              backup.id,
            ),
          );
        data = { backup_id: backups.value };
        await preview();
      } else {
        status.textContent = "正在准备下载更新…";
        onBusy(true);
        try {
          const result = await apiPost("community/update", {
            action: "prepare",
            pack_id: pack.id,
          });
          await monitor(result.job_id);
        } catch (reason) {
          error.textContent = reason?.message || String(reason);
          onBusy(false);
        }
      }
    },
    async resume() {
      let jobId;
      try {
        jobId = sessionStorage.getItem(storageKey);
      } catch {
        return;
      }
      if (jobId) {
        await monitor(jobId);
      }
    },
  };
}
