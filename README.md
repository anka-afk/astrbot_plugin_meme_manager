# 🌟 AstrBot 表情包管理器

![Banner](.github/img/Banner.png)

<div align="center">

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
![Python Version](https://img.shields.io/badge/Python-3.10.14%2B-blue)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)
[![PRs Welcome](https://img.shields.io/badge/PRs-Welcome-brightgreen)](CONTRIBUTING.md)
[![Contributors](https://img.shields.io/github/contributors/anka-afk/astrbot_plugin_meme_manager?color=green)](https://github.com/anka-afk/astrbot_plugin_meme_manager/graphs/contributors)
[![Last Commit](https://img.shields.io/github/last-commit/anka-afk/astrbot_plugin_meme_manager)](https://github.com/anka-afk/astrbot_plugin_meme_manager/commits/main)

</div>

<div align="center">

[![Moe Counter](https://count.getloli.com/get/@GalChat?theme=moebooru)](https://github.com/anka-afk/astrbot_plugin_meme_manager)

</div>

## 📑 目录

- [📢 通知](#-通知)
- [❓ 常见问题](#-常见问题)
- [🚀 功能特点](#-功能特点)
- [📚 协议与创作指引](#-协议与创作指引)
- [🛠️ 第一次使用](#️-第一次使用)
- [🖥️ WebUI 管理界面](#️-webui-管理界面)
- [🤖 自动收集表情包（BETA）](#-自动收集表情包beta)
- [☁️ 图床配置](#️-图床配置)
- [⚠️ 兼容性](#️-兼容性)
- [📝 使用指令](#-使用指令)
- [🔗 联动](#-联动)
- [📜 更新日志](#-更新日志)
- [🛠️ 问题反馈](#️-问题反馈)
- [📄 许可证](#-许可证)

一个功能强大的 AstrBot 表情包管理插件，支持 🤖 AI 智能发送与自动收集表情、🖥️ WebUI 管理界面、☁️ 云端同步等特性。

## 📢 通知

4.0 版本起，插件首次启动不再自动导入默认表情包，初始状态为“空表情包”。请先在 WebUI 里安装一个官方包或自行配置。

## ❓ 常见问题

1. **Q: 如何快速开始使用这个插件？**
   - A: 安装并重启后，请先进入 [🖥️ WebUI 管理界面](#️-webui-管理界面)，在资源广场下载一个官方包再开始使用。插件会自动配置所需提示词，无需修改人格设置。

2. **Q: 管理界面如何访问？**
   - A: 管理界面已集成到 AstrBot WebUI 中。进入 WebUI → 插件页面 → 点击「表情包管理器」页面即可访问。

3. **Q: 是否必须配置图床才能使用？**
   - A: 不需要。除了云端同步功能外，其他所有功能（包括表情管理后台）都可以正常使用。图床配置是可选的。

4. **Q: 如何管理表情包？**
   - A: 通过 AstrBot WebUI 的插件页面访问管理界面，在管理界面中您可以：
     - 添加/删除表情包
     - 创建/修改表情分类
     - 编辑表情描述（用于指导 bot 使用场景）
     - 拖拽移动表情包、批量选择删除/移动/复制/粘贴
     - 查看图床服务商、云端图片数量和占用空间
     - 查看资源广场，下载官方/社区表情包
     - 查看表情细分配置页面，**为不同会话/不同人格单独配置表情包**

       所有修改都会实时生效，无需重启或额外配置。

5. **Q: 插件是否包含预设表情包？**
   - A: 插件不再内置默认表情资源，首次进入是空表情包。你可以在资源广场下载官方包，或使用命令 `/表情管理 恢复默认表情包` 一键安装官方包。

6. **Q: 最佳实践是什么？**
   - A: 推荐以下使用流程：
     1. 安装插件后先参考 [🖥️ WebUI 管理界面](#️-webui-管理界面) 下载官方包，再进行分类等配置
     2. 使用 `/reset` 重置当前对话
     3. 开始使用表情包功能，发送消息时 bot 会根据场景自动选择合适的表情
     4. 需要更多自定义设置时，请参考 [🛠️ 第一次使用](#️-第一次使用) 章节

7. **Q: 不访问 WebUI 也能使用和管理表情包吗？**
   - A: 可以。你可以按下面方式使用：
     1. 需要手动管理分类与描述时，可查看 `data/plugin_data/meme_manager/packs/<pack_id>/`：
        - `manifest.json` 为包元信息
        - `memes_data.json` 为分类与描述映射
        - `memes/` 目录下各子文件夹即分类，图片即该分类表情
     2. 多包场景下，可在 `data/plugin_data/meme_manager/` 查看：
        - `registry.json`（包注册信息）
        - `selection_rules.json`（default/session/persona 选包规则）
     3. v4.0 起默认不再内置仓库 memes 资源，首次进入为空表情包；建议优先在资源广场安装官方包，或使用 `/表情管理 恢复默认表情包` 一键安装官方包。

8. **Q: 能否自动收集群聊或私聊中收到的表情包？**
   - A: 可以。在插件设置的「自动收集表情包」分组中开启功能，并选择一个支持图片输入的视觉模型。你可以限制允许收集的群聊/个人 ID、识别间隔、抽样概率和每日识图次数。普通模式会直接保存到目标表情包；启用语义检索后，图片会先进入独立的待语义化桶，需要在 WebUI 的「语义化」页面手动确认。

## 🚀 功能特点

| 功能                    | 描述                                                                   |
| ----------------------- | ---------------------------------------------------------------------- |
| 🤖 AI 智能识别          | 自动识别对话场景，发送合适的表情                                       |
| 📥 AI 自动收集          | 对收到的图片进行表情包判断、分类、去重和限流，支持独立待语义化桶       |
| 🖼️ 快速上传和管理表情包 | 通过命令快速上传和管理表情包，WebUI 管理界面可直接看到上传进度与结果   |
| 🖥️ WebUI 管理界面       | 集成于 AstrBot WebUI，无需单独端口，支持拖拽管理、批量操作和移动端适配 |
| 🛍️ 资源广场安装         | 内置资源广场，支持安装官方/社区表情包，空包时可一键安装官方包          |
| 📦 多表情包运行时       | 支持多包安装、导入导出与卸载，运行时按包隔离管理表情资源               |
| 🧭 会话与人格选包       | 支持 default、session、persona 规则选包，不同场景可使用不同表情包      |
| ☁️ 云端图床同步         | 支持与云端图床同步，方便多设备使用，并展示当前图床服务商与云端统计信息 |
| 🎯 精确的表情分类系统   | 通过类别管理表情，提升使用体验，并支持一键安装官方默认包               |
| 📊 表情发送控制         | 可以控制每次发送的表情数量和频率                                       |
| 🔄 自动维护 Prompt      | 所有 prompt 会根据修改的表情包文件夹目录自动维护，无需手动添加！       |

## 📚 协议与创作指引

### 协议文档

- 协议文档见 [anka-afk/astrbot-meme-pack-index](https://github.com/anka-afk/astrbot-meme-pack-index)

### 如何创建并分享自己的表情包（Issue / PR）

1. 前往索引仓库 README 阅读完整提交指引：
   [anka-afk/astrbot-meme-pack-index](https://github.com/anka-afk/astrbot-meme-pack-index)
2. 按协议准备你的表情包仓库结构（manifest、memes、previews），并确保仓库可访问。
3. 如果你会编辑索引文件：
   - 直接在索引仓库提交 PR
4. 如果你不会编辑索引文件：
   - 在索引仓库提 Issue，按模板填写信息
5. 你也可以先参考示例仓库模板：
   [anka-afk/astrbot-meme-pack-example](https://github.com/anka-afk/astrbot-meme-pack-example)

> 建议：先在本地或测试环境通过“资源广场安装”验证一次，确保索引条目可安装、可预览、分类描述清晰。

## 🛠️ 第一次使用

注意：v4.0 起首次进入为“空表情包”。推荐先在 WebUI 里安装一个官方包。

推荐顺序：

1. 进入 [🖥️ WebUI 管理界面](#️-webui-管理界面)
2. 打开资源广场并安装一个官方包（或使用 `/表情管理 恢复默认表情包`）
3. 确认分类与预览正常后，再配置表情包

配置步骤如下：

1. **打开设置**：在 Astrbot Webui 左侧栏中点击插件选项展开，进入 Astrbot 插件界面，找到表情包管理器，打开设置，如图所示：
   ![打开设置](.github/img/打开设置.png)

2. **进行设置**：根据配置页中的说明进行配置。

3. **打开WebUI管理界面**：配置完成后，按照下方章节说明，即可访问管理界面。

## 🖥️ WebUI 管理界面

管理界面已集成到 AstrBot WebUI 中，进入 WebUI → 插件页面 → 表情包管理器 即可访问。

具体步骤如下：

![访问WebUI](.github/img/访问WebUI.png)

新版页面与功能概览：

- 资源广场：支持官方包/社区包浏览与安装
- 支持从 github 仓库地址安装社区包索引外的表情包
  ![资源广场预览](.github/img/资源广场.png)

- 表情包管理界面：支持表情包的分类预览、上传下载、分类管理、编辑描述、切换表情包
- 支持切换不同表情包组进行管理

![表情包管理预览](.github/img/表情包管理.png)

- 表情包设置中心：支持表情包按不同人格/会话进行配置绑定（即不同场景使用不同的表情包），配置使用覆盖层级形式。
- 支持导入/导出表情包全量备份，便于迁移

![表情包设置中心](.github/img/表情包设置中心.png)

- 语义表情包页面：用于语义任务编排与状态观测，支持一键完整语义化、继续队列、失败重试、按当前维度重建向量、重建索引、清空队列等能力。
- 页面内可直接查看当前包的任务状态与最近记录；结合情感模型辅助后，可在不暴露工具调用细节的情况下完成候选选图。

![语义表情包页面](.github/img/语义表情包页面.png)

### 语义表情包（MVP）

在 WebUI 的「语义化」页面选择资源包后，可以按图片 SHA-256 去重生成描述、标签并建立本地索引；GIF 会等间隔提取最多五帧，用来理解完整动作。语义描述以“这张图在聊天中回复什么”为中心，综合原图、后期贴图、文字梗、动作变化、说话视角、行为归属、复合语气和触发场景，而不是只生成普通画面图注；当所选 Provider 确实提供联网搜索能力时，模型还会按需核实会影响理解的人物、作品或梗模板身份。批量生成图片描述只需要视觉模型；“一键完整语义化”、建立索引和语义查询必须选择 AstrBot 核心的 Embedding Provider，插件配置页会按模型类型提供选择器。任务支持暂停、继续和失败重试；向量由 AstrBot 核心 Embedding Provider 生成，并使用本地 FAISS 精确索引保存。首页“分享版”会保留可复用的语义描述与标签，但强制移除本机向量；“自用备份”会在索引完整时连同向量保存。完成后在插件设置中打开 `semantic.enabled`，回复模型会通过 `search_memes` 获取少量候选，并用 `&&meme:候选ID&&` 精确选择图片。未配置语义模型时不会影响原有粗分类模式，旧资源包无需添加语义文件即可继续使用。

## 🤖 自动收集表情包（BETA）

自动收集功能会监听机器人收到的图片，在后台调用指定视觉模型判断图片是否适合作为聊天表情包，并从目标表情包的现有分类中选择最合适的分类。功能默认关闭，启用前必须配置一个支持图片输入的聊天模型。

> 自动收集使用独立后台队列，不会阻塞消息回复。

### 工作流程

1. 收到包含图片的群聊或私聊消息。
2. 检查来源范围、抽样概率、同一来源冷却时间和每日识图上限。
3. 读取消息中的第一张直接图片，校验真实格式、文件大小和像素数量。
4. 使用 SHA-256 检查目标表情包是否已经存在相同图片。
5. 调用所选视觉模型判断图片是否为表情包，并给出分类及置信度。
6. 根据当前模式保存：
   - **未启用语义检索**：直接保存到目标表情包的建议分类。
   - **已启用语义检索**：先保存到独立的「自动收集待语义化桶」，不会立即修改正在使用的语义包。

模型认为图片不是表情包，或表情包置信度低于配置阈值时，会直接忽略。图片被确认是表情包但分类置信度不足时，会进入 `needs_review` 分类，等待人工整理。

### 配置说明

在 AstrBot WebUI → 插件设置 → 表情包管理器 →「自动收集表情包」中配置：

| 配置项                    | 默认值  | 说明                                                                                                  |
| ------------------------- | ------- | ----------------------------------------------------------------------------------------------------- |
| `enabled`                 | `false` | 自动收集总开关。                                                                                      |
| `vision_provider_id`      | 空      | 自动收集专用视觉模型，必须支持图片输入；配置页提供模型下拉选择。                                      |
| `scope`                   | `[]`    | 允许收集的群聊或个人 ID 列表；空列表表示全部来源。 例如：123456789, group:123456789, user:987654321。 |
| `target_pack_id`          | 空      | 固定目标表情包；留空时使用当前会话按 `session/persona/default` 规则选中的表情包。                     |
| `sampling_probability`    | `100`   | 对符合其他条件的图片消息进行识别的概率，单位为百分比；调低可进一步减少模型调用。                      |
| `cooldown_seconds`        | `20`    | 同一个群聊或个人来源两次提交识图之间的最短间隔，单位为秒。                                            |
| `daily_recognition_limit` | `100`   | 每日最多调用视觉模型的次数；设为 `0` 表示不限制。                                                     |
| `min_meme_confidence`     | `0.85`  | 最低表情包置信度，低于该值不收录。                                                                    |
| `min_category_confidence` | `0.65`  | 最低分类置信度，低于该值时进入 `needs_review`。                                                       |

`vision_provider_id` 与语义化配置中的视觉模型互相独立：前者负责自动收集时的初步判断和分类，后者负责生成完整语义描述。它们可以选择同一个模型，也可以分别配置。

### 语义模式下的人工确认

启用语义包检索后，自动收集到的图片不会直接写入语义表情包，而是按照目标表情包分别记录在独立待整理桶中。在 WebUI 的「语义化」页面选择对应表情包后，会显示「自动收集待语义化桶」：

1. 查看待处理数量、建议分类、来源和接收时间。
2. 点击「合入并语义化」。
3. 插件先将图片合入当前选择的表情包，再启动完整语义化任务。
4. 完成语义描述与向量索引后，图片才参与语义检索。

待整理桶只在启用语义检索时显示。切换不同表情包时，页面仅展示目标为当前表情包的待处理图片。

### 动图、负载与隐私

- 默认支持 JPEG、PNG、GIF 和 WebP；GIF、动态 WebP/APNG 等多帧图片会采样最多五帧供视觉模型判断，无需额外开启动图选项。
- 单张图片最大 20 MiB，最多 4000 万像素；插件会校验实际图片内容，而不是只相信文件扩展名。
- 自动收集使用单个后台工作任务和有界队列，并结合来源冷却、抽样、每日限额和识别结果缓存控制模型负载。
- 保存前会按图片内容进行 SHA-256 去重，避免重复收录相同文件。
- 图片会发送给你选择的视觉模型服务。请根据所在平台规则、群聊约定和模型服务隐私政策合理设置 `scope`，不要收集无权处理的私人图片。

## ☁️ 图床配置

本插件支持 **Cloudflare R2**、**Stardots** 和 **WebDAV** 三种图床。由于 Stardots 图床政策更新，免费用户可存储空间较小, 推荐使用另外两种方案。

### 方案一：Cloudflare R2 图床

1. **创建 Cloudflare 账号**：如果还没有账号，请先注册 Cloudflare

2. **创建 R2 存储桶**：
   - 登录 Cloudflare 控制台
   - 进入 R2 页面
   - 点击 "Create bucket" 创建存储桶
   - 记住存储桶名称，填入配置中的 `bucket_name`

3. **获取 R2 API 凭证**：
   - 在 R2 页面，点击 "Manage R2 API Tokens"
   - 点击 "Create API Token"
   - 记录生成的 `Access Key ID` 和 `Secret Access Key`
   - 在 R2 页面右上角可以找到 `Account ID`

4. **配置插件**：在插件设置中选择 `cloudflare_r2` 并填写：

   ```yaml
   # Cloudflare Account ID (account_id)
   account_id: "your_account_id"
   # R2 Access Key ID (access_key_id)
   access_key_id: "your_access_key_id"
   # R2 Secret Access Key (secret_access_key)
   secret_access_key: "your_secret_access_key"
   # R2 Bucket 名称 (bucket_name)
   bucket_name: "your_bucket_name"
   # 自定义CDN域名 (可选) (public_url)
   # 例如: https://你的域名.com
   public_url: "https://你的域名.com"
   ```

5. **开启公共访问**（可选）：
   - 在存储桶设置中，可以绑定自定义域名
   - 或者使用默认的 R2.dev 域名（`https://<bucket>.<account_id>.r2.dev`）
   - 将域名填入 `public_url` 配置项

6. **使用图床功能**：
   - 发送 `/表情管理 同步状态` 查看同步状态
   - 发送 `/表情管理 同步到云端` 上传表情包到R2
   - 发送 `/表情管理 从云端同步` 从R2下载表情包

> **Cloudflare R2 优势**：
>
> - 每月10GB免费存储
> - 每月100万次免费A类操作
> - 全球CDN加速
> - 支持自定义域名
> - 智能上传记录，避免重复上传相同文件

### 方案二：WebDAV 图床/云存储

WebDAV 适合用于 NAS、Alist、Nextcloud、坚果云、群晖等服务，可作为表情包云端同步存储。若 WebDAV 服务本身不提供公开外链，也可以只用于备份和多设备同步。

1. **准备 WebDAV 服务**：确认你的服务支持 WebDAV，并记录 WebDAV 根地址、用户名和密码/应用密码。

2. **配置插件**：在插件设置中选择 `webdav` 并填写：

   ```yaml
   # WebDAV 根地址 (url)
   url: "https://example.com/dav"
   # WebDAV 用户名 (username)
   username: "your_username"
   # WebDAV 密码或应用密码 (password)
   password: "your_password"
   # 远端目录 (base_path)
   base_path: "memes"
   # 公开访问根地址（可选）(public_url)
   public_url: "https://cdn.example.com/memes"
   # 是否校验 SSL 证书 (verify_ssl)
   verify_ssl: true
   # 请求超时时间，单位秒 (timeout)
   timeout: 30
   ```

3. **使用图床功能**：
   - 发送 `/表情管理 同步状态` 查看同步状态
   - 发送 `/表情管理 同步到云端` 上传表情包到 WebDAV
   - 发送 `/表情管理 从云端同步` 从 WebDAV 下载表情包

> **WebDAV 注意事项**：
>
> - `base_path` 是 WebDAV 内保存表情包的目录，插件会自动创建缺失目录
> - `public_url` 可选；不填写时仍可同步，但生成的 URL 可能需要登录才能访问
> - 自签名证书服务可将 `verify_ssl` 设置为 `false`

### 方案三：Stardots 图床

> 目前该图床容量与 api 限额严重不足，不建议使用。

1. **注册账号**：如果没有账号，你需要先注册一个 Stardots 账号，或直接使用其他方式登录。

   > Stardots 图床免费账户支持 1 个空间，约 200 张原图像、单图 3MB 限制，每月 10GB 流量传输。如果需要更多空间, 请考虑其他方案。

2. **建立空间**：注册账号后，你需要先建立一个空间。

   > 记住你建立的空间的名字，将其填入插件设置中的图床配置信息的空间名称中。

3. **获取 API Key 和 API Secret**：在同样的界面，点击左侧的"开放 API" -> "密钥"，点击生成密钥，将其中的 API Key 和 API Secret 填入插件设置中的图床配置信息中，点击保存配置即可。

## ⚠️ 兼容性

**分段回复兼容性：**

- 如果您在 AstrBot 配置中开启了 **分段回复** 功能，回复带图功能可能会失效
- 如果打开流式传输兼容模式，回复带图功能会失效
- 如需完整的回复带图体验，请考虑关闭分段回复功能

**流式传输兼容性：**

- 当前插件已经完全兼容流式传输，但是视觉效果上会看见表情标签，在流式传输完毕后插件会清理标签并额外发送表情。
- 如果您在 AstrBot 配置中开启了 **流式传输** 功能，并使用支持流式传输的平台，请打开流式传输兼容模式（默认开启）

**插件间兼容接口：**

- 为了兼容「其他插件自己请求 LLM 并发送消息」的场景，本插件提供了公开接口。
- 其他插件在发送前可主动调用本插件接口，自动清理 `&&happy&&` 等标记并按本插件规则发送表情包。

示例：

```python
from astrbot.api.message_components import Plain
from astrbot.core.message.message_event_result import MessageChain


async def send_with_meme_manager(context, event, text: str):
   # 1) 获取 meme_manager 插件实例
   md = context.get_registered_star("meme_manager")
   plugin = md.star_cls if md and md.star_cls else None

   if not plugin:
      # 未安装或未启用时，走原始发送逻辑
      await event.send(MessageChain([Plain(text)]))
      return

   # 2) 一步发送（清理标记 + 文本发送 + 表情图发送）
   await plugin.compat_send_message(event, text)
```

如果你希望自己控制发送时机，也可以使用两段式接口：

```python
async def send_in_two_steps(context, event, chain: MessageChain):
   md = context.get_registered_star("meme_manager")
   plugin = md.star_cls if md and md.star_cls else None
   if not plugin:
      await event.send(chain)
      return

   prepared = await plugin.compat_prepare_message(event, chain)

   # 先发清理后的文本/组件
   cleaned_chain = prepared["cleaned_chain"]
   if cleaned_chain.chain:
      await event.send(cleaned_chain)

   # 再调用公开接口发送准备好的表情图
   await plugin.compat_send_prepared_message(
      event,
      prepared,
      send_text=False,
      send_images=True,
   )
```

接口说明：

- `compat_prepare_message(event, message)`：仅做处理，不发送，返回清理后的消息链与待发送图片。
- `compat_send_message(event, message, send_images=True)`：直接完成处理与发送。
- `compat_send_prepared_message(event, prepared, send_text=True, send_images=True)`：发送预处理结果（适合两段式流程）。
- `message` 支持 `str` / `list` / `MessageChain`。

## 📝 使用指令

当前大部分功能都可以通过 AstrBot WebUI 管理界面操作，无需使用指令。以下为指令列表，供 CLI 用户参考：

| 指令                                   | 描述                                                |
| -------------------------------------- | --------------------------------------------------- |
| `/表情管理 查看图库`                   | 📚 列出所有可用表情类别                             |
| `/表情管理 添加分类 [类别名称] [描述]` | ➕ 创建新的表情包分类，可只输入名称后按提示补充描述 |
| `/表情管理 添加表情 [类别]`            | ➕ 通过聊天上传表情到指定类别                       |
| `/表情管理 恢复默认表情包`             | ♻️ 从官方仓库一键安装首个官方表情包并设为默认       |
| `/表情管理 清空指定类型 [类别]`        | ⚠️ 清空指定类别中的表情包，保留类型本身             |
| `/表情管理 清空全部`                   | ⚠️ 清空全部表情包，保留所有类型和描述配置           |
| `/表情管理 删除类型本身 [类别]`        | ⚠️ 删除指定类型及其描述配置                         |
| `/表情管理 同步状态`                   | 🔄 检查同步状态                                     |
| `/表情管理 同步到云端`                 | ☁️ 将本地表情同步到云端                             |
| `/表情管理 从云端同步`                 | ⬇️ 从云端同步表情到本地                             |
| `/表情管理 覆盖到云端`                 | ⚠️ 让云端与本地完全一致                             |
| `/表情管理 从云端覆盖`                 | ⚠️ 让本地与云端完全一致                             |

> 说明：
>
> - `清空指定类型`、`清空全部`、`删除类型本身` 都需要在 30 秒内二次确认。
> - `恢复默认表情包` 会从官方仓库安装首个官方包；若同名包已存在，可先卸载后重试。

## 🔗 联动

插件现在已经内置“自动收集群聊/私聊表情包 + 日常主动发图”的完整流程，通常不再需要额外桥接插件。推荐优先使用上方的 [🤖 自动收集表情包](#-自动收集表情包beta) 功能。

如果你已经在使用其他图片收集插件，或需要迁移其历史图库，仍可参考以下社区方案：

- [astrbot_plugin_smart_imagechat_hub](https://github.com/QingchenWait/astrbot_plugin_smart_imagechat_hub)：负责自动收集群聊表情包并进行 AI 标签整理（建议关闭主动发图能力）
- [astrbot_plugin_meme_manager](https://github.com/anka-afk/astrbot_plugin_meme_manager)：负责日常场景中的主动发图
- [astrbot_plugin_meme_bridge](https://github.com/konley/astrbot_plugin_meme_bridge)：定时读取 `image_index.json`，按标签映射 + LLM 辅助分类，将图片同步到 meme_manager 的表情包中，并更新分类映射。(2026-07-26 目前并未完全适配 v4.0+ 的多包体系，临时使用[astrbot_plugin_meme_bridge_fork](https://github.com/anka-afk/astrbot_plugin_meme_bridge))

第三方桥接方案不属于本插件内置功能，无法保证其长期兼容性与效果；接入前请自行备份表情包数据。

## 📜 更新日志

当前版本：v4.15.4

- 🧠 修复 Gemini 视觉模型执行语义化及人工复审时，工具 schema 因空字符串枚举被拒绝的问题。
- 🛡️ 分类建议仍在模型响应后经过现有分类白名单校验，未知分类不会被采用。
- ✅ 新增工具 schema 回归测试并完成完整插件测试。

完整版本记录请查看 [CHANGELOG.md](./CHANGELOG.md)。

## 🛠️ 问题反馈

如果遇到问题或有功能建议，欢迎在 GitHub 提交 Issue。

## 📄 许可证

本项目基于 MIT 许可证开源。
