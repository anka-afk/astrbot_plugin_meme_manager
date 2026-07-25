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

- [🌟 AstrBot 表情包管理器](#-astrbot-表情包管理器)
  - [📑 目录](#-目录)
  - [📢 通知](#-通知)
  - [❓ 常见问题](#-常见问题)
  - [🚀 功能特点](#-功能特点)
  - [📦 安装方法](#-安装方法)
  - [📚 协议与创作指引](#-协议与创作指引)
  - [🛠️ 第一次使用](#️-第一次使用)
  - [☁️ 图床配置](#️-图床配置)
  - [⚙️ 配置说明](#️-配置说明)
  - [📝 使用指令](#-使用指令)
  - [🖥️ WebUI 管理界面](#️-webui-管理界面)
  - [🔗 联动](#-联动)
  - [📜 更新日志](#-更新日志)
    - [v4.1](#v404)
    - [v4.0](#v40)
    - [v3.21](#v321)
    - [v3.1x](#v31x)
    - [v3.0](#v30)
    - [v2.2](#v22)
    - [v2.1](#v21)
    - [v2.0](#v20)
    - [v1.x](#v1x)
  - [⚠️ 注意事项](#️-注意事项)
  - [🛠️ 问题反馈](#️-问题反馈)
  - [📄 许可证](#-许可证)

一个功能强大的 AstrBot 表情包管理插件，支持 🤖 AI 智能发送表情、🖥️ WebUI 管理界面、☁️ 云端同步等特性。

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

## 🚀 功能特点

| 功能                    | 描述                                                                   |
| ----------------------- | ---------------------------------------------------------------------- |
| 🤖 AI 智能识别          | 自动识别对话场景，发送合适的表情                                       |
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
- 首页支持直接导入与导出当前表情包：
  - 分享版不包含向量，是分享和上传资源广场时应使用的格式
  - 自用备份包含已校验的向量索引，仅建议恢复到自己的相同向量模型环境
  - 导入前会显示图片、分类、语义描述和向量状态；旧版只有 `memes` 与 `memes_data.json` 的无语义压缩包也可自动转换

![表情包管理预览](.github/img/表情包管理.png)

- 表情包设置中心：支持表情包按不同人格/会话进行配置绑定（即不同场景使用不同的表情包），配置使用覆盖层级形式，初始状态为 default，session/persona 可覆盖 default 配置。
- 支持导入/导出表情包全量备份，便于迁移

![表情包设置中心](.github/img/表情包设置中心.png)

- 语义表情包页面：用于语义任务编排与状态观测，支持一键完整语义化、继续队列、失败重试、按当前维度重建向量、重建索引、清空队列等能力。
- 页面内可直接查看当前包的任务状态与最近记录；结合情感模型辅助后，可在不暴露工具调用细节的情况下完成候选选图。

![语义表情包页面](.github/img/语义表情包页面.png)

### 语义表情包（MVP）

在 WebUI 的「语义化」页面选择资源包后，可以按图片 SHA-256 去重生成描述、标签并建立本地索引；GIF 会等间隔提取最多五帧，用来理解完整动作。语义描述以“这张图在聊天中回复什么”为中心，综合原图、后期贴图、文字梗、动作变化、说话视角、行为归属、复合语气和触发场景，而不是只生成普通画面图注；当所选 Provider 确实提供联网搜索能力时，模型还会按需核实会影响理解的人物、作品或梗模板身份。批量生成图片描述只需要视觉模型；“一键完整语义化”、建立索引和语义查询必须选择 AstrBot 核心的 Embedding Provider，插件配置页会按模型类型提供选择器。任务支持暂停、继续和失败重试；向量由 AstrBot 核心 Embedding Provider 生成，并使用本地 FAISS 精确索引保存。首页“分享版”会保留可复用的语义描述与标签，但强制移除本机向量；“自用备份”会在索引完整时连同向量保存。完成后在插件设置中打开 `semantic.enabled`，回复模型会通过 `search_memes` 获取少量候选，并用 `&&meme:候选ID&&` 精确选择图片。未配置语义模型时不会影响原有粗分类模式，旧资源包无需添加语义文件即可继续使用。

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

如果你有“自动收集群聊表情包 + 日常主动发图”这类组合需求，可以参考社区桥接方案：

- [astrbot_plugin_smart_imagechat_hub](https://github.com/QingchenWait/astrbot_plugin_smart_imagechat_hub)：负责自动收集群聊表情包并进行 AI 标签整理（建议关闭主动发图能力）
- [astrbot_plugin_meme_manager](https://github.com/anka-afk/astrbot_plugin_meme_manager)：负责日常场景中的主动发图
- [astrbot_plugin_meme_bridge](https://github.com/konley/astrbot_plugin_meme_bridge)：定时读取 `image_index.json`，按标签映射 + LLM 辅助分类，将图片同步到 meme_manager 的表情包中，并更新分类映射。(2026-07-26 目前并未完全适配 v4.0+ 的多包体系，临时使用[astrbot_plugin_meme_bridge_fork](https://github.com/anka-afk/astrbot_plugin_meme_bridge))

不保证桥接方案的长期可用性与效果，若你有兴趣，可自行 fork 并维护。

我不太信任 ai 生成的标签，新的表情包增长方案正在筹备中。

## 📜 更新日志

### v4.1

- 🔀 新增语义向量检索表情包功能。
- 🧠 情感模型注入增强：支持配置注入上下文轮数（追加在提示词末尾）与可选注入当前人格设定，提升语义选图稳定性与缓存友好性。
- 🌐 社区安装下载链路支持 GitHub 加速配置：新增可编辑加速链接，默认启用；加速不可用时自动回退原生 GitHub 链接。
- 🎨 页面资源加载优化：图标资源改为本地化加载方案，减少外链波动对 WebUI 可用性的影响。

### v4.0

> ⚠️ 破坏性更新
>
> - 插件仓库根目录 `memes/` 已从运行时依赖中解耦，不再作为默认资源来源。
> - 首次启动不再自动导入默认表情包，初始状态为“空表情包”。
> - 调整了插件配置及其结构，提供了部分向后兼容，升级版本建议重新配置

- 🛍️ 资源广场：支持官方包/社区包浏览与安装，支持从 github 仓库地址安装社区包索引外的表情包。
- 📦 多表情包（pack）运行时体系完善：默认包、导入/导出、安装/卸载、规则选包（default/session/persona）联动。
- ☁️ 云同步全面 pack-aware：按当前管理包动态绑定同步目录，避免多包场景串目录。
- 🖼️ 表情包设置中心：支持表情包按不同人格/会话进行配置绑定（即不同场景使用不同的表情包），配置使用覆盖层级形式，初始状态为 default，session/persona 可覆盖 default 配置。
- 🗂️ 支持导入/导出表情包全量备份，便于迁移。
- 🧮 修复远端/本地 ID 归一化差异。
- 🚦 限流治理：限制重试、失败快速返回与分页容错。
- ⚡ 状态查询降压：图床同步状态接口加入短 TTL 缓存并在任务启动前主动失效。
- 📜 协议文档落地：新增 AstrBot 表情包协议草案中英文版本，并补充创作者与索引提交流程。
- 🛠️ 提供了公开接口，方便其他插件在发送消息前调用本插件处理表情标记并发送表情包。

### v3.21

- 🖥️ 管理界面迁移至 AstrBot WebUI 插件页面，无需单独端口和密钥
- 🗂️ 插件大文件存储切换到 AstrBot 规范的 `data/plugin_data/meme_manager`
- 🔄 兼容旧版 `data/memes_data` 目录并在首次加载时安全迁移
- ✅ WebUI 新增批量删除、分类清空、全量清空与 5 秒二次确认
- 💬 将主要 `alert/confirm` 交互替换为页内提示与统一确认弹层
- 🔐 管理后台改为仅允许私聊开启，重复开启/关闭时只返回单次最终结果
- 🧾 命令组新增 `清空指定类型`、`清空全部`、`删除类型本身`，并接入 AstrBot 会话控制二次确认
- 📤 WebUI 上传新增可见进度、批次状态提示与批量内去重；同内容文件会跳过，同名不同内容会自动补序号
- 🖱️ WebUI 支持批量右键菜单、拖拽移动、批量复制粘贴、分类编辑弹窗与移动端侧栏/滚动适配
- ☁️ 图床状态面板新增当前服务商、云端图片数量、云端占用、待上传、待下载、云端多出与本地多出展示
- ⚠️ WebUI 新增强制同步云端按钮，执行前需勾选确认并等待 5 秒倒计时，可删除云端多出的图片
- 🖼️ WebUI 图片预览改为插件接口加载，支持懒加载、大图预览、原图加载和失败重试
- 📂 目录面板改为独立布局，由侧边栏按钮统一控制；长按拖拽进入时间调整为 2 秒
- 🔄 图床同步进度新增任务状态轮询兜底，避免实时进度通道异常时按钮一直停留在同步中
- 🛠️ 修复添加分类后同步状态检查异常，兼容不同同步状态返回结构
- 🧰 取消首次自动导入内置默认表情包，插件初始状态为“空表情包”
- ♻️ `/表情管理 恢复默认表情包` 改为从官方仓库安装首个官方包并设为默认

### v3.1x

- 🛠️ 修复 AstrBot 4.5.0+ 版本兼容性问题，解决表情标签过滤异常
- 💡 新增宽松匹配模式, 备用标记匹配, 重复表情检测, 高置信度表情设置
- 🛠️ 修复 webui 中的上传, 我是猪鼻
- 🛠️ 提供 webp 格式支持
- ☁️ 新增 Cloudflare R2 图床支持（智能上传记录，避免重复上传）
- 🖼️ 新增回复带图功能：文本和表情图片可在同一条消息中发送
- 🎛️ 新增回复带图概率控制，让表情包行为更自然
- 📊 增强同步状态命令，支持详细参数查看文件分类统计
- 🔄 修复 MessageChain 迭代错误和 R2 图床同步前缀问题

### v3.0x

- 🛠️ 修复消息类型不支持查看问题
- 🎉 移除了 imghdr 依赖, 现在兼容更高版本 python

### v3.0

- 🔄 完全重构代码架构
- 🌟 新增 WebUI 管理界面
- ☁️ 添加图床同步功能
- 🤖 优化表情识别算法

### v2.2

- 🎉 增加更多表情包
- 🛠️ 修复 TTS 兼容性问题

### v2.1

- ⚡ 优化消息发送逻辑
- ✉️ 文本和表情分开发送

### v2.0

- 🌐 支持网络图片上传
- 🔧 优化上传流程

### v1.x

- 🚀 初始版本发布
- 📦 基础表情管理功能
- 🖼️ 多图上传支持

## 🛠️ 问题反馈

如果遇到问题或有功能建议，欢迎在 GitHub 提交 Issue。

## 📄 许可证

本项目基于 MIT 许可证开源。
