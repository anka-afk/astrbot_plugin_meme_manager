# 🌟 AstrBot 表情包管理器

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
  - [🛠️ 第一次使用](#️-第一次使用)
  - [☁️ 图床配置](#️-图床配置)
  - [⚙️ 配置说明](#️-配置说明)
  - [📝 使用指令](#-使用指令)
  - [🖥️ WebUI 功能预览](#️-webui-功能预览)
  - [📜 更新日志](#-更新日志)
    - [v3.20](#v320)
    - [v3.1x](#v31x)
    - [v3.0](#v30)
    - [v2.2](#v22)
    - [v2.1](#v21)
    - [v2.0](#v20)
    - [v1.x](#v1x)
  - [⚠️ 注意事项](#️-注意事项)
  - [🛠️ 问题反馈](#️-问题反馈)
  - [📄 许可证](#-许可证)

一个功能强大的 AstrBot 表情包管理插件，支持 🤖 AI 智能发送表情、🌐 WebUI 管理界面、☁️ 云端同步等特性。

## 📢 通知

我正在准备**考研**，因此未来的两个月(包括以往的半年)不会维护此插件, 12月以后会重构一下。

### 比较多的问题:
关于不访问webui的使用插件并管理表情包的方法: 

1. 安装插件, 对bot使用reset命令, 此时已经可以使用
2. 如果需要修改表情包以及对应的llm提示, 查看 `data/plugin_data/meme_manager/` 目录下的结构, `memes_data.json` 即对应的文件夹分类和描述的映射关系, `memes` 目录下的文件夹名称即分类名称, 里面的图片即对应类别的表情包, 你可以新建一个类别的文件夹, 并在 json 文件中添加对应的描述来添加新的表情类别, 也可以直接往对应目录上传新的表情
3. 插件自带一套表情包, 如果不想折腾可以直接使用

## ❓ 常见问题

1. **Q: 如何快速开始使用这个插件？**

   - A: 只需安装插件并重启 AstrBot 即可，无需修改任何人格设置。插件会自动配置所需的提示词和初始表情包。(⚠️ 重要：请勿在人格设置中添加任何表情相关提示词)

2. **Q: WebUI 无法访问怎么解决？**

   - A: 请按以下步骤排查：
     1. Docker 部署用户请先确保已映射端口，详见：[ISSUE#1](https://github.com/anka-afk/astrbot_plugin_meme_manager/issues/1)
     2. 使用内网穿透的用户需配置 NAT 转发，将内网 5000 端口映射到外网
     3. 云服务器用户请检查安全组是否已放行 5000 端口的入站规则

3. **Q: 是否必须配置图床才能使用？**

   - A: 不需要。除了云端同步功能外，其他所有功能（包括表情管理后台）都可以正常使用。图床配置是可选的。

4. **Q: 如何管理表情包？**

   - A: 请先私聊机器人发送命令 `/表情管理 开启管理后台` 启动 WebUI，在管理界面中您可以：
     - 添加/删除表情包
     - 创建/修改表情分类
     - 编辑表情描述（用于指导 bot 使用场景）
     - 拖拽移动表情包、批量选择删除/移动/复制/粘贴
     - 查看图床服务商、云端图片数量和占用空间
       所有修改都会实时生效，无需重启或额外配置。

5. **Q: 插件是否包含预设表情包？**

   - A: 是的。首次启动时，插件会自动导入一套默认表情包，后续更新不会再次自动补回已删除的默认内容；如果需要，可以通过命令手动恢复默认表情包。

6. **Q: 最佳实践是什么？**

   - A: 推荐以下使用流程：
     1. 安装插件后直接使用 `/reset` 重置当前对话
     2. 无需修改任何人格设置或添加额外提示词
     3. 需要更多自定义设置时，请参考[🛠️ 第一次使用](#️-第一次使用)章节

## 🚀 功能特点

| 功能                    | 描述                                                                 |
| ----------------------- | -------------------------------------------------------------------- |
| 🤖 AI 智能识别          | 自动识别对话场景，发送合适的表情                                     |
| 🖼️ 快速上传和管理表情包 | 通过命令快速上传和管理表情包，WebUI 上传时可直接看到上传进度与结果    |
| 🌐 WebUI 管理界面       | 提供便捷的 WebUI 管理界面，支持拖拽管理、批量操作和移动端适配         |
| ☁️ 云端图床同步         | 支持与云端图床同步，方便多设备使用，并展示当前图床服务商与云端统计信息 |
| 🎯 精确的表情分类系统   | 通过类别管理表情，提升使用体验，并支持按类别恢复默认表情包           |
| 🔒 安全的访问控制机制   | 管理后台仅允许私聊开启，危险命令与危险操作均带确认流程               |
| 📊 表情发送控制         | 可以控制每次发送的表情数量和频率                                     |
| 🔄 自动维护 Prompt      | 所有 prompt 会根据修改的表情包文件夹目录自动维护，无需手动添加！     |

## 📦 安装方法

1. 确保已安装 AstrBot
2. 将插件复制到 AstrBot 的插件目录（你也可以使用 Astrbot 的插件管理器安装，或下载本项目上传压缩包）
3. 重启 AstrBot 或使用热加载命令

## 🛠️ 第一次使用

注意：第一次使用请先进行配置，配置步骤如下：

1. **打开设置**：进入设置界面，如图所示：
   ![打开设置](.github/img/open_setting.png)

2. **进行设置**：根据以下说明进行配置，你也可以点击问号了解配置说明：
   ![设置插件](.github/img/setting.png)

   > **注意**：你需要设置好图床的 API Key、API Secret 和空间名称，才能正常使用图床同步等功能。如果不设置图床信息，默认无法使用图床功能，其他功能如 WebUI 可以正常使用。

## ☁️ 图床配置

本插件支持 **stardots** 和 **Cloudflare R2** 两种图床。

### 方案一：Stardots 图床（国内访问友好）

1. **注册账号**：如果没有账号，你需要先注册一个 stardots 账号，或直接使用其他方式登录。

   > Stardots 图床免费账户支持 1 个空间，2024 张图像（对于表情包来说是足够的），每月 10GB 流量传输。免费账户对于我们同步存储表情包的需求来看是足够的。

2. **建立空间**：注册账号后，你需要先建立一个空间，操作如图所示：
   ![建立空间](.github/img/stardots_step2.png)

   > 记住你建立的空间的名字，将其填入插件设置中的图床配置信息的空间名称中。

3. **获取 API Key 和 API Secret**：在同样的界面，点击左侧的"开放 API" -> "密钥"，点击生成密钥：
   ![生成密钥](.github/img/stardots_step3.png)

   你会看到如下画面：
   ![获得密钥](.github/img/stardots_step4.png)

   将其中的 API Key 和 API Secret 填入插件设置中的图床配置信息中，点击保存配置，AstrBot 将会重启。

### 方案二：Cloudflare R2 图床（国际访问友好）

1. **创建 Cloudflare 账号**：如果还没有账号，请先注册 Cloudflare

2. **创建 R2 存储桶**：
   - 登录 Cloudflare Dashboard
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
> - 每月10GB免费存储
> - 每月100万次免费A类操作
> - 全球CDN加速
> - 支持自定义域名
> - 智能上传记录，避免重复上传相同文件

## ⚙️ 配置说明

插件配置项包括：

- `image_host`: 选择图床服务 (支持 stardots 和 cloudflare_r2)
- `image_host_config`: 图床配置信息（根据选择的图床服务填写相应配置）
- `webui_port`: WebUI 服务端口号
- `max_emotions_per_message`: 每条消息最大表情数量
- `emotions_probability`: 表情触发概率 (0-100)
- `enable_mixed_message`: 启用回复带图功能
- `mixed_message_probability`: 回复带图概率 (0-100)
- `strict_max_emotions_per_message`: 是否严格限制表情数量
- `enable_loose_emotion_matching`: 是否启用宽松的表情匹配
- `enable_alternative_markup`: 是否启用备用标记
- `remove_invalid_alternative_markup`: 是否移除无法识别的备用标记内容
- `enable_repeated_emotion_detection`: 是否启用重复表情检测
- `high_confidence_emotions`: 高置信度表情列表

**括号内容被误删的处理：**
如果遇到括号/方括号内容被过滤（参考 Issue #45），请关闭 `remove_invalid_alternative_markup`。

### ⚠️ 重要提示

**分段回复兼容性：**
- 如果您在 AstrBot 配置中开启了 **分段回复** 功能，回复带图功能可能会失效
- 这是由于分段回复机制会将消息组件逐个发送导致的
- 如需完整的回复带图体验，请考虑关闭分段回复功能

## 📝 使用指令

| 指令                              | 描述                                        |
| --------------------------------- | ------------------------------------------- |
| `/表情管理 查看图库`              | 📚 列出所有可用表情类别                     |
| `/表情管理 添加分类 [类别名称] [描述]` | ➕ 创建新的表情包分类，可只输入名称后按提示补充描述 |
| `/表情管理 开启管理后台`          | 🚀 启动 WebUI 服务，仅支持私聊使用          |
| `/表情管理 关闭管理后台`          | 🔒 关闭 WebUI 服务                          |
| `/表情管理 恢复默认表情包 [类别]` | ♻️ 恢复内置默认表情包，可指定单个类别       |
| `/表情管理 清空指定类型 [类别]`   | ⚠️ 清空指定类别中的表情包，保留类型本身     |
| `/表情管理 清空全部`              | ⚠️ 清空全部表情包，保留所有类型和描述配置   |
| `/表情管理 删除类型本身 [类别]`   | ⚠️ 删除指定类型及其描述配置                 |
| `/表情管理 同步状态`              | 🔄 检查同步状态                             |
| `/表情管理 同步到云端`            | ☁️ 将本地表情同步到云端                     |
| `/表情管理 从云端同步`            | ⬇️ 从云端同步表情到本地                     |
| `/表情管理 覆盖到云端`            | ⚠️ 让云端与本地完全一致                     |
| `/表情管理 从云端覆盖`            | ⚠️ 让本地与云端完全一致                     |

> 说明：
> - `开启管理后台` 只能在私聊中执行；重复执行时会直接返回当前访问信息，不会重复启动。
> - `清空指定类型`、`清空全部`、`删除类型本身` 都需要在 30 秒内二次确认。
> - `恢复默认表情包` 不会覆盖现有文件；同内容文件会跳过，同名不同内容会自动补序号。

## 🖥️ WebUI 功能预览

以下是 WebUI 的功能预览：

| 功能           | 预览图示                                                      |
| -------------- | ------------------------------------------------------------- |
| 登录界面       | ![登录界面](.github/img/login_screenshot.png)                 |
| 表情包管理界面 | ![表情包管理界面](.github/img/meme_management_screenshot.png) |

## 📜 更新日志

### v3.20

- 🗂️ 插件大文件存储切换到 AstrBot 规范的 `data/plugin_data/meme_manager`
- 🔄 兼容旧版 `data/memes_data` 目录并在首次加载时安全迁移
- ✅ WebUI 新增批量删除、分类清空、全量清空与 5 秒二次确认
- 💬 将主要 `alert/confirm` 交互替换为页内提示与统一确认弹层
- 🔐 管理后台改为仅允许私聊开启，重复开启/关闭时只返回单次最终结果
- 🧾 命令组新增 `清空指定类型`、`清空全部`、`删除类型本身`，并接入 AstrBot 会话控制二次确认
- 📤 WebUI 上传新增可见进度、批次状态提示与批量内去重；同内容文件会跳过，同名不同内容会自动补序号
- 🖱️ WebUI 支持批量右键菜单、拖拽移动、批量复制粘贴、分类编辑弹窗与移动端侧栏/滚动适配
- ☁️ 图床状态面板新增当前服务商、云端图片数量与云端占用展示
- 🛠️ 修复添加分类后同步状态检查异常，兼容不同同步状态返回结构
- 🧰 默认表情包仅在首次初始化时自动导入一次，后续更新不再自动补回已删除的默认内容
- ♻️ 新增 `/表情管理 恢复默认表情包 [类别]`，支持按类别或全部恢复内置默认表情包

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

## ⚠️ 注意事项

1. WebUI 服务需要管理员权限才能开启
2. 使用云端同步功能前需要正确配置图床信息
3. 请勿将 WebUI 访问密钥分享给未授权用户

## 🛠️ 问题反馈

如果遇到问题或有功能建议，欢迎在 GitHub 提交 Issue。

## 📄 许可证

本项目基于 MIT 许可证开源。
