// All fixture mutations are confined to memory and reset on navigation.
(() => {
  const params = new URLSearchParams(location.search);
  document.addEventListener("DOMContentLoaded", () => {
    const label = document.createElement("div");
    label.textContent = "界面预览（模拟数据）";
    label.style.cssText =
      "position:fixed;bottom:10px;left:12px;z-index:15000;padding:4px 9px;border:1px solid #dce8e2;border-radius:6px;background:#fff;color:#657a72;font:11px sans-serif;pointer-events:none";
    document.body.append(label);
  });
  const packs = [
    {
      id: "official-basic",
      name: "日常表情",
      is_default: true,
      image_count: 18,
      category_count: 3,
      semantic_caption_complete: true,
    },
    {
      id: "weekend",
      name: "周末快乐",
      is_default: false,
      image_count: 6,
      category_count: 2,
    },
  ];
  const emojis = {
    happy: ["开心.png", "好耶.png", "收到.png", "赞.png", "出发.png", "耶.png"],
    sad: [
      "委屈.png",
      "哭哭.png",
      "难过.png",
      "累了.png",
      "叹气.png",
      "抱抱.png",
    ],
    surprise: [
      "震惊.png",
      "啊这.png",
      "好家伙.png",
      "真的吗.png",
      "离谱.png",
      "呆住.png",
    ],
  };
  const descriptions = {
    happy: "开心，快乐的每一天",
    sad: "难过，偶尔也需要抱抱",
    surprise: "惊讶，意料之外的瞬间",
  };
  const transferPacks = {
    "official-basic": { images: emojis, descriptions },
    weekend: {
      images: { happy: [], relax: [] },
      descriptions: { happy: "周末专属开心分类", relax: "放松休息" },
    },
  };
  let rules = [
    {
      id: "rule-persona",
      scope: "persona",
      target: "assistant",
      pack_id: "weekend",
      enabled: true,
    },
    {
      id: "rule-session",
      scope: "session",
      target: "example-chat",
      pack_id: "official-basic",
      enabled: true,
    },
    {
      id: "rule-default",
      scope: "default",
      target: "",
      pack_id: "official-basic",
      enabled: true,
    },
  ];
  let job = null;
  let updateJob = null;
  let updated = false;
  const updateBackups = [
    { id: "fixture-backup", created_at: 1789884000, version: "1.0" },
  ];
  let polls = 0;
  let taskStatus = "idle";
  let configSnapshot = null;
  let imageSyncTask = null;
  let imageSyncStarted = 0;
  let imageSyncConflicts =
    params.get("sync") === "conflicts"
      ? [
          { relative_path: "happy/开心.png", reason: "both_changed" },
          { relative_path: "surprise/意外.png", reason: "unverified" },
        ]
      : [];
  const subscriptions = new Map();
  const imageData = (category, filename = "") => {
    const faces = { happy: "😊", sad: "🥺", surprise: "😮" };
    return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(
      `<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><rect width="256" height="256" rx="24" fill="${
        category === "sad"
          ? "#eef3fb"
          : category === "surprise"
            ? "#fff6e9"
            : "#eef8ef"
      }"/><text x="128" y="142" font-size="92" text-anchor="middle">${
        faces[category] || "😺"
      }</text><text x="128" y="207" font-size="22" font-family="sans-serif" fill="#415d50" text-anchor="middle">${
        filename.replace(/\.png$/, "") || "表情收藏"
      }</text></svg>`,
    )}`;
  };
  const catalog = {
    packs: [
      {
        id: "official-basic",
        name: "日常表情",
        description: "收藏每个小情绪，让聊天更有温度。",
        version: "2.0",
        maintainer: "AstrBot",
        tags: ["official", "semantic"],
        cover_url: imageData("happy"),
        source: { type: "github", repo: "example/daily", ref: "main" },
      },
      {
        id: "official-cats",
        name: "猫猫日记",
        description: "今天的心情，就交给猫猫表达。",
        version: "1.2",
        maintainer: "AstrBot",
        tags: ["official"],
        cover_url: imageData("sad"),
        source: { type: "github", repo: "example/cats", ref: "main" },
      },
      {
        id: "weekend",
        name: "周末快乐",
        description: "生活需要一点松弛感。",
        version: "1.0",
        maintainer: "社区",
        tags: ["semantic"],
        cover_url: imageData("surprise"),
        source: { type: "github", repo: "example/weekend", ref: "main" },
      },
    ],
  };
  let collected = Array.from({ length: 7 }, (_, index) => ({
    id: `collected-${index}`,
    suggested_category: ["happy", "sad", "surprise"][index % 3],
    category_confidence: [0.98, 0.92, 0.87, 0.73, 0.6, 0.45, 0.28][index],
    meme_confidence: 0.95,
    caption: ["好耶，今天也要开心！", "不想上班，想躺平。", "这也太突然了吧！"][
      index % 3
    ],
    near_duplicate: index === 5 ? "surprise/震惊.png" : "",
    received_at: new Date().toISOString(),
  }));
  async function request(endpoint, body = {}) {
    await new Promise((done) => setTimeout(done, 90));
    if (
      (params.get("preview_failure") || "").split(",").includes(endpoint) ||
      params.get("preview_failure") === "all"
    )
      throw new Error("预览中的模拟连接失败");
    switch (endpoint) {
      case "auto-collect/inbox":
        return {
          count: body.pack_id === "weekend" ? 0 : collected.length,
          items: body.pack_id === "weekend" ? [] : collected,
          categories: descriptions,
        };
      case "auto-collect/inbox/image_data": {
        const item = collected.find((entry) => entry.id === body.id);
        return { data_url: imageData(item?.suggested_category, item?.caption) };
      }
      case "auto-collect/inbox/accept": {
        const ids = new Set(body.items.map((item) => item.id));
        const imported = collected.filter((item) => ids.has(item.id)).length;
        collected = collected.filter((item) => !ids.has(item.id));
        return { imported, duplicates: 0, failed: 0 };
      }
      case "auto-collect/inbox/discard": {
        const ids = new Set(body.ids);
        const discarded = collected.filter((item) => ids.has(item.id)).length;
        collected = collected.filter((item) => !ids.has(item.id));
        return { discarded };
      }
      case "bridge/auth_token":
        return { token: "fixture-token" };
      case "packs":
        return { packs: params.has("preview_empty") ? [] : packs };
      case "packs/create": {
        const pack_id = `local-${packs.length}`;
        packs.push({
          id: pack_id,
          name: body.name,
          image_count: 0,
          category_count: 1,
        });
        transferPacks[pack_id] = {
          images: { 默认分类: [] },
          descriptions: { 默认分类: "请添加描述" },
        };
        return { pack_id, name: body.name };
      }
      case "community/updates":
        return {
          packs: {
            "official-basic": {
              installed: true,
              available: !updated,
              current_version: updated ? "2.0" : "1.0",
              latest_version: "2.0",
              has_baseline: true,
              backups: updateBackups,
            },
          },
        };
      case "community/update": {
        if (body.action === "restore_preview")
          return {
            current_version: "2.0",
            version: "1.0",
            current_images: 22,
            plan_token: "fixture-restore",
          };
        if (body.action === "preview") {
          const conflict = body.strategy !== "replace";
          return {
            strategy: body.strategy,
            current_version: "1.0",
            latest_version: "2.0",
            has_baseline: true,
            counts: {
              added: 3,
              changed: 2,
              upstream_deleted: 1,
              local_added: 4,
              conflicts: 1,
            },
            local_categories: descriptions,
            local_images: 22,
            local_category_count: 3,
            categories: conflict
              ? [
                  {
                    source: "happy",
                    local_description: "我的开心分类",
                    remote_description: "上游开心描述",
                    target: body.categories?.happy ?? "happy",
                  },
                ]
              : [],
            ready: !conflict || Boolean(body.categories?.happy ?? "happy"),
            plan_token: "fixture-plan",
            rows: [
              {
                path: "memes/happy/好耶.png",
                local_path: "memes/happy/好耶.png",
                destination: "memes/happy/好耶.png",
                kind: "conflict",
                action: "keep",
                options: ["local", "upstream", "both"],
              },
              {
                path: "memes/sad/哭哭.png",
                local_path: "memes/sad/哭哭.png",
                destination: "memes/sad/哭哭.png",
                kind: "upstream_deleted",
                action: "keep",
                options: [],
              },
              {
                path: "memes/happy/新表情.png",
                local_path: "memes/happy/新表情.png",
                destination: "memes/happy/新表情.png",
                kind: "added",
                action: "add",
                options: [],
              },
            ],
          };
        }
        updateJob = {
          state: "completed",
          action: body.action,
          result:
            body.action === "prepare"
              ? { session: "fixture-session", has_baseline: true }
              : { backup_id: "fixture-backup" },
        };
        if (body.action === "apply") updated = true;
        return { job_id: "fixture-update-job" };
      }
      case "community/update/status":
        return updateJob || { state: "failed", message: "测试任务已过期" };
      case "emoji":
        return params.has("preview_empty")
          ? {}
          : transferPacks[body.managed_pack_id]?.images || emojis;
      case "packs/move-images": {
        const source = transferPacks[body.source_pack_id],
          target = transferPacks[body.target_pack_id];
        const groups = new Map();
        for (const item of body.items)
          groups.set(item.category, [
            ...(groups.get(item.category) || []),
            item,
          ]);
        const plan = [...groups].map(([category, items]) => {
          const chosen =
            body.mode === "target"
              ? body.target_category
              : body.category_mapping?.[category];
          const destination =
            chosen || (body.mode === "preserve" ? category : "");
          const exists = Object.hasOwn(target.descriptions, destination);
          const conflict =
            !destination ||
            (!chosen &&
              exists &&
              source.descriptions[category] !==
                target.descriptions[destination]);
          return {
            source_category: category,
            target_category: destination,
            count: items.length,
            action: conflict ? "choose" : exists ? "existing" : "create",
            conflict,
            source_description: source.descriptions[category] || "",
            target_description: target.descriptions[destination] || "",
          };
        });
        const plan_token = JSON.stringify(plan);
        if (body.preview)
          return {
            plan,
            ready: plan.every((row) => !row.conflict),
            plan_token,
            target_categories: target.descriptions,
          };
        if (body.plan_token !== plan_token || plan.some((row) => row.conflict))
          throw new Error("分类信息已变化，请重新检查");
        const moved = [],
          failed = [];
        for (const row of plan) {
          if (row.action === "create")
            target.descriptions[row.target_category] = row.source_description;
          const images = (target.images[row.target_category] ||= []);
          for (const item of groups.get(row.source_category)) {
            if (images.includes(item.emoji))
              failed.push({ ...item, reason: "目标分类已有同名图片" });
            else {
              images.push(item.emoji);
              source.images[item.category] = source.images[
                item.category
              ].filter((name) => name !== item.emoji);
              moved.push({ ...item, target_category: row.target_category });
            }
          }
        }
        return { moved, failed, warnings: [] };
      }
      case "emoji/batch_move": {
        const source = emojis[body.source_category] || [];
        const target = (emojis[body.target_category] ||= []);
        const moved_files = [],
          missing_files = [],
          conflicting_files = [];
        for (const filename of new Set(body.image_files)) {
          if (!source.includes(filename)) missing_files.push(filename);
          else if (target.includes(filename)) conflicting_files.push(filename);
          else {
            source.splice(source.indexOf(filename), 1);
            target.push(filename);
            moved_files.push(filename);
          }
        }
        return {
          moved_files,
          missing_files,
          conflicting_files,
          moved_count: moved_files.length,
        };
      }
      case "emotions":
        return (
          transferPacks[body.managed_pack_id]?.descriptions || descriptions
        );
      case "semantic/reviews":
        return {
          available: true,
          items: [],
          statistics: { total: 18, unchecked: 18 },
        };
      case "meme_image_data":
        return {
          data_url: imageData(body.category, body.filename || body.emoji),
          mime_type: "image/svg+xml",
        };
      case "preview/manifest":
        return {
          pack_id: body.managed_pack_id || "builtin-default",
          concurrency: 2,
          versions: {},
        };
      case "meme_image_semantic":
        return {
          caption: "表达轻松和开心的心情。",
          tags: ["可爱", "开心"],
          category: body.category,
          category_review_status: "unchecked",
          embedding_status: "completed",
        };
      case "packs/export/status":
        return {
          pack_id: body.pack_id,
          image_count: 18,
          share_available: true,
          backup_available: true,
          vector_ready: true,
          can_export_vectors: true,
          index_ready: true,
        };
      case "semantic/status":
        return {
          task_status: taskStatus,
          task_phase: taskStatus === "running" ? "captioning" : "finished",
          worker_alive: true,
          total_tasks: 18,
          file_total: 18,
          unique_total: 18,
          caption_done: 12,
          embedding_done: 10,
          failed_tasks: 1,
          caption_failed: 1,
          embedding_failed: 0,
          queued_caption_tasks: 5,
          queued_embedding_tasks: 2,
          active_request_count: taskStatus === "running" ? 1 : 0,
          concurrency: 1,
          embedding_provider_ready: true,
          vision_provider_ready: true,
          index_ready: true,
          queue_status: taskStatus === "running" ? "running" : "waiting",
          embedding_model: "Example embedding",
          embedding_configured_dimension: 1024,
          index_embedding_dimension: 1024,
          vision_model: "Example vision",
          elapsed_seconds: 84,
          semantic_caption_complete: false,
          semantic_enabled: true,
          semantic_config_ready: true,
        };
      case "semantic/items":
        return {
          items: params.has("preview_empty")
            ? []
            : [
                {
                  relative_path: "happy/开心.png",
                  category: "happy",
                  filename: "开心.png",
                  status: "completed",
                  caption: "表达轻松和开心的心情。",
                  updated_at: 1788624000,
                },
                {
                  relative_path: "sad/哭哭.png",
                  category: "sad",
                  filename: "哭哭.png",
                  status: "failed",
                  error: "请求超时，请重试",
                  updated_at: 1788624000,
                },
              ],
          total: 2,
          page: 1,
          total_pages: 1,
          page_size: 20,
        };
      case "semantic/start":
      case "semantic/resume":
      case "semantic/retry-failed":
        taskStatus = "running";
        return { message: "任务已开始" };
      case "semantic/pause":
        taskStatus = "paused";
        return { message: "任务已暂停" };
      case "settings/config":
        if (!configSnapshot)
          configSnapshot = await (await fetch("/__preview/settings")).json();
        if (body.changes) {
          if (params.has("preview_conflict"))
            throw new Error(
              "配置已在其他页面更新。请重新加载后再保存，避免覆盖新的设置。",
            );
          for (const field of configSnapshot.fields) {
            if (!Object.hasOwn(body.changes, field.path)) continue;
            if (field.secret)
              field.configured = Boolean(body.changes[field.path]);
            else field.value = body.changes[field.path];
          }
          configSnapshot.revision = `fixture-${Date.now()}`;
        }
        return structuredClone({
          ...configSnapshot,
          applied: true,
          message: "设置已保存并生效。",
        });
      case "settings/rules":
        if (body.rules) rules = body.rules;
        return { rules, default_pack_id: "official-basic" };
      case "settings/targets":
        return {
          persona_targets: ["assistant"],
          session_targets: ["example-chat"],
        };
      case "community/index/cache":
      case "community/index/fetch":
        return {
          index: params.has("preview_empty") ? { packs: [] } : catalog,
          count: params.has("preview_empty") ? 0 : catalog.packs.length,
        };
      case "community/install/start":
        job = {
          job_id: "fixture-job",
          status: "running",
          pack_name: "猫猫日记",
          progress: 0,
          result: { pack_id: body.pack_id || "custom-demo", version: "1.0" },
        };
        polls = 0;
        return job;
      case "community/install/status":
        if (!job) return { status: "idle" };
        if (job.status === "cancelled") return job;
        polls++;
        job = {
          ...job,
          progress: Math.min(100, polls * 20),
          phase: "downloading",
          downloaded_bytes: polls * 1024,
          total_bytes: 5120,
          status: polls >= 5 ? "succeeded" : "running",
        };
        if (
          job.status === "succeeded" &&
          !packs.some((pack) => pack.id === job.result.pack_id)
        )
          packs.push({
            id: job.result.pack_id,
            name: job.pack_name,
            image_count: 6,
          });
        return job;
      case "community/install/cancel":
        job = { ...job, status: "cancelled" };
        return job;
      case "sync/status":
        return {
          status: "success",
          differences: { missing_in_config: [], missing_on_disk: [] },
        };
      case "img_host/sync/status":
        return {
          provider_label: "预览图床",
          remote_image_count: 18,
          remote_total_bytes: 1800000,
          upload_count: 2,
          download_count: 1,
          conflicts: imageSyncConflicts,
          to_overwrite_remote: [{}, {}, {}],
          to_overwrite_local: [{}, {}, {}],
          to_delete_remote: [{}],
          to_delete_local: [{}],
        };
      case "img_host/sync/upload":
      case "img_host/sync/download":
      case "img_host/sync/overwrite_to_remote":
      case "img_host/sync/overwrite_from_remote":
        imageSyncStarted = Date.now();
        imageSyncTask = {
          task_id: "fixture-sync",
          task: endpoint.split("/").pop(),
          running: true,
          completed: false,
          success: null,
          phase: "planning",
          total: 3,
          processed: 0,
          succeeded: 0,
          failed: 0,
          conflicts: 0,
          errors: [],
          managed_pack_id: "official-basic",
        };
        return { success: true, task: imageSyncTask };
      case "img_host/sync/task_status":
        if (!imageSyncTask)
          return { running: false, completed: true, success: null };
        if (imageSyncTask.running) {
          const processed = Math.min(
            3,
            Math.floor((Date.now() - imageSyncStarted) / 1500),
          );
          const mirror = imageSyncTask.task.startsWith("overwrite");
          const conflicts = mirror ? 0 : imageSyncConflicts.length;
          imageSyncTask = {
            ...imageSyncTask,
            processed,
            succeeded: processed,
            current_file: "happy/开心.png",
            phase: imageSyncTask.task.includes("download")
              ? "download"
              : "upload",
            running: processed < 3,
            completed: processed >= 3,
            conflicts,
            success: processed < 3 ? null : !conflicts,
          };
          if (processed >= 3 && mirror) imageSyncConflicts = [];
        }
        return imageSyncTask;
      case "img_host/sync/cancel":
        imageSyncTask = {
          ...imageSyncTask,
          running: false,
          completed: true,
          success: false,
          phase: "cancelled",
          message: "Sync cancelled",
        };
        return imageSyncTask;
      default:
        return { status: "success", message: "预览操作已完成" };
    }
  }
  window.AstrBotPluginPage = {
    ready: async () => {},
    getContext: () => ({ plugin_id: "fixture", page_name: "manage" }),
    apiGet: request,
    apiPost: request,
    upload: async () => {
      throw new Error("预览不处理真实文件");
    },
    download: async () => {},
    subscribeSSE: async (endpoint, handlers) => {
      const id = String(Date.now());
      handlers.onOpen?.();
      subscriptions.set(
        id,
        setInterval(async () => {
          handlers.onMessage?.({
            parsed: await request("img_host/sync/task_status"),
          });
        }, 500),
      );
      return id;
    },
    unsubscribeSSE: async (id) => {
      clearInterval(subscriptions.get(id));
      subscriptions.delete(id);
    },
  };
})();
