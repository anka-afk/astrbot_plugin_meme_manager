# 开发指南

本文面向插件开发者与联动插件作者。安装和日常使用请阅读 [README](README.md)。

## 项目结构

| 路径                   | 职责                                       |
| ---------------------- | ------------------------------------------ |
| `main.py`              | 插件初始化、配置读取与 AstrBot 事件注册    |
| `mixins/`              | 消息处理、命令和 WebUI API                 |
| `backend/`             | 业务模块入口、自动收集与插件设置           |
| `backend/packs/`       | 表情包协议、选包、导入导出、分类与图片管理 |
| `backend/semantic/`    | 图片描述、向量索引、检索、元数据与任务执行 |
| `backend/meme_parser/` | 独立的完整回复与流式标签解析器             |
| `image_host/`          | 图床服务与同步                             |
| `pages/`               | 表情管理、资源广场、语义化、设置中心       |
| `pages/app/shared/`    | 四个页面共用的样式与交互源文件             |
| `tests/`               | Python 回归测试与前端预览工具              |

后端按业务职责归组，包内文件名省略重复前缀：

```text
backend/
├── packs/
│   ├── protocol.py       # Resource pack validation
│   ├── resolver.py       # Session and persona selection
│   ├── storage.py        # Installation, import, export and backup
│   ├── categories.py     # Category management
│   └── images.py         # Image operations
├── semantic/
│   ├── models.py
│   ├── caption.py
│   ├── index.py
│   ├── query.py
│   ├── storage.py
│   └── task.py
├── meme_parser/
│   ├── parser.py
│   ├── context.py
│   ├── types.py
│   └── text_safety.py
├── auto_collect.py
└── plugin_settings.py
```

测试对应放在 `tests/packs/`、`tests/semantic/`、`tests/parsing/` 和 `tests/image_host/`；跨模块的运行时与集成测试保留在 `tests/`。例如只检查解析器时，可运行 `python -m pytest tests/parsing -q`。

`main.py`、`config.py` 和 `_conf_schema.json` 保留在插件根目录。`pages/app/` 是 AstrBot 注册的统一页面入口，四个功能页作为其子目录，共享 `shared/` 中的样式、脚本和图标字体。模块位置调整不改变运行数据目录或配置文件位置。

## 配置与资源

插件参数保存在 AstrBot 的 `data/config/astrbot_plugin_meme_manager_config.json`。插件设置中心与 AstrBot 的配置页读写同一份配置；设置中心保存后通过 AstrBot 重载插件。配置字段定义在 `_conf_schema.json`，设置中心的分类与控件逻辑在 `pages/app/settings/config.js`。

当前只读取分组配置，不保留旧的平铺字段和别名。增加配置时，同时检查 schema、运行时读取、设置中心分组与配置接口测试。

运行数据位于 `data/plugin_data/meme_manager/`。启动时不再探测或迁移旧的全局表情目录，也不维护固定的默认表情目录别名；资源路径由 `backend/packs/resolver.py` 按当前规则动态解析。已有目录不会因移除旧机制而被删除。

主要文件：

- `registry.json`：已安装表情包。
- `selection_rules.json`：默认、会话和人格选包规则。
- `packs/<pack_id>/manifest.json`：表情包元信息。
- `packs/<pack_id>/memes_data.json`：分类与描述。
- `packs/<pack_id>/memes/`：按分类存放的图片。

## 解析与消息发送

`backend/meme_parser/context.py` 维护代码、链接、注释与推理区状态；`parser.py` 识别标签并按原文位置编辑；`types.py` 定义不可变结果。完整文本调用 `MemeParser.parse()`，增量文本使用每条回复独立的 `feed()` / `finish()`，不要在会话之间共享解析器实例。

解析结果包含原文偏移、被消费的标记和资源限制诊断。分类校验使用当前请求选中的表情包；语义 ID 还须经过发送层的本轮候选校验。解析模块不调用模型、不读取图片，也不承担发送职责。

流式装饰钩子在平台消费消息流前过滤文本；最终响应钩子确定选图，回复结束后发送图片。代码和有歧义的片段可能缓冲到行尾。解析器不是完整 CommonMark 渲染器，不回溯修改已发送内容；单行超过 16,384 字符或推理嵌套超过 64 层时，当前行及后续内容原样通过并记录诊断。

## 插件联动接口

- 为了兼容「其他插件自己请求 LLM 并发送消息」的场景，本插件提供了公开接口。
- 其他插件在发送前可主动调用本插件接口，自动清理 `&&happy&&` 等标记并按本插件规则发送表情包。

示例：

```python
from astrbot.api.message_components import Plain
from astrbot.core.message.message_event_result import MessageChain


async def send_with_meme_manager(context, event, text: str):
   # Get the registered plugin instance.
   md = context.get_registered_star("meme_manager")
   plugin = md.star_cls if md and md.star_cls else None

   if not plugin:
      # Send normally when the plugin is unavailable.
      await event.send(MessageChain([Plain(text)]))
      return

   # Parse and send the text and selected images.
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

   # Send the prepared text and components.
   cleaned_chain = prepared["cleaned_chain"]
   if cleaned_chain.chain:
      await event.send(cleaned_chain)

   # Send prepared images through the public API.
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

## 前端开发与检查

页面使用原生 HTML、CSS 和 JavaScript。AstrBot 将静态资源限制在注册页面的目录内，因此四个功能页统一位于 `pages/app/` 下，通过 `../shared/` 引用同一份公共资源。直接修改 `pages/app/shared/` 即可，无需复制或构建。新增资源也应放在 `app/` 内，避免越过页面目录边界。

图床教程位于 `pages/app/settings/index.html`，交互入口在 `storage-guide.js`；更新教程时核对实际配置字段及服务商官方说明，避免写死套餐额度。

本地界面预览：

```sh
node tests/frontend/preview.mjs
```

预览使用模拟接口，仅用于布局和交互检查，不能代替真实 AstrBot 联调。

## Python 验证

在安装了 AstrBot 与插件依赖的环境中，从插件根目录运行：

```sh
python -m pytest tests -q
ruff format .
ruff check .
```

测试目录若被 Ruff 配置排除，还需显式检查本次修改的测试文件。涉及 AstrBot 初始化的测试应使用隔离的数据目录，避免影响正在运行的实例配置。流式改动应覆盖任意切块、源流取消和异常、重复钩子及非文本组件；模型选图效果需要另行用真实对话验证。
