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
├── collection_analysis.py # Independent evidence and collection judgments
├── decision.py           # Shared System One transport
├── selection.py          # Post-reply candidate selection
├── delivery_history.py   # Bounded confirmed-delivery references
└── plugin_settings.py
```

测试对应放在 `tests/packs/`、`tests/semantic/`、`tests/parsing/` 和 `tests/image_host/`；跨模块的运行时与集成测试保留在 `tests/`。例如只检查解析器时，可运行 `python -m pytest tests/parsing -q`。

`main.py`、`config.py` 和 `_conf_schema.json` 保留在插件根目录。`pages/app/` 是 AstrBot 注册的统一页面入口，四个功能页作为其子目录，共享 `shared/` 中的样式、脚本和图标字体。模块位置调整不改变运行数据目录或配置文件位置。

## 配置与资源

插件参数保存在 AstrBot 的 `data/config/astrbot_plugin_meme_manager_config.json`。插件设置中心与 AstrBot 的配置页读写同一份配置；设置中心保存后通过 AstrBot 重载插件。配置字段定义在 `_conf_schema.json`，设置中心的分类与控件逻辑在 `pages/app/settings/config.js`。

当前只读取分组配置，不保留旧的平铺字段和别名。增加配置时，同时检查 schema、运行时读取、设置中心分组与配置接口测试。

配图设置按标签粒度与选择者组合。`semantic.enabled` 映射分类级／图片级标签，`generation.emotion.llm.enabled` 映射主模型自选／额外辅助；不新增重复模式字段。“主模型”是原界面的“回复模型”，始终指本轮实际生成回复的 Provider/model，不等同于全局默认模型。`generation.emotion.llm.backend` 仅决定辅助选图的后端，仍在内容生成后执行。`semantic.query` 独立配置查询来源和聊天 Provider，`decision_service` 保存共享服务参数，`auto_collect.judgment_backend` 单独选择收集判断路径。旧缺省值分别为 chat、generated、vision。

设置页以可点击流程图为配图与收集参数入口，所有参数节点统一打开 `config-flow-dialog`，主流程与侧栏保持原位置。弹窗通过注释占位搬移原设置卡片或使用规则面板，关闭时恢复，禁止复制控件或维护另一份草稿。`activeFlowGroup` 标识当前弹窗分组；`config.js` 维护唯一的 fields/controls/changes 状态，保存重建表单时先恢复原组件，再搬入新组件。表单控件显式关联 `plugin-config-form`，事件同时委托给表单及弹窗；规则面板复用自身事件与独立保存按钮。公共 overlay 负责焦点约束、Escape 与滚动锁定，兼容嵌套确认弹窗；关闭后恢复流程触发点焦点，保存失败保留草稿并定位错误字段。

固定 `#decision` 导航供独立服务配置，`#models` 旧链接保持有效。`setup-state.js` 从当前草稿生成模型角色、缺项及准备状态，不发送请求或序列化密钥。配图和自动收集缺项分别显示在流程内的 `selection-required`、`collection-required`，保留原字段定位；两页均不展示独立的模型状态卡片。决策服务直接展示接口参数，语义准备提醒独立显示。保存全部更改包含所有插件配置分区，使用规则保持独立；密钥修改清单只显示替换或清除状态。

设置支持 `#config-group-<group.id>`：`applyLocationHash` 校验目标所属分区，通过 `navigateToSetting` 打开弹窗并聚焦标题或指定字段，保留卡片 fragment 及鉴权查询参数。`#config-group-rules` 打开使用规则弹窗，`#rules` 仍进入独立分区。首次加载或保存期间仅记录待跳转目标，在解除 `busy/inert` 后处理；加载失败保留目标供重试，切换普通分区或无效锚点会取消待跳转。显式打开尚未启用的模块时展示未使用说明，不自动修改功能开关；存储提供商卡片也可预先配置。保存后重建表单保留当前弹窗。语义化页模型设置链接直达 `semantic-models`，文案统一为“配图 → 语义数据准备”。

`backend/selection.py` 规范化分类／图片候选和 selected、abstained、failed 结果；`backend/decision.py` 通过异步 HTTP 执行 System One 请求，不调用 AstrBot 聊天接口，不请求流式，不自动重试。会话／事件候选校验和真实资源权限仍在调用方执行。客户端延迟创建、并发有界、取消传播，插件关闭时释放 session；返回错误只暴露稳定原因码，密钥及服务原始错误不进入页面或日志。

`POST settings/decision-models` 是插件页面 bridge 接口，仅接收草稿 `base_url`，读取公开 `GET <base>/models`，不保存配置、不重载、不转发密钥或发起推理。使用 8 秒超时、响应大小上限及禁止重定向；兼容基础地址末尾已有 `/systemone`。解析官方 `{models:[{name,displayName,...}]}`，按 `name` 返回精确 ID，去重并跳过无效条目、明确不支持 Choice 的条目。前端通过请求序号与节点连接状态丢弃改地址或重建后的迟到结果；仅用户选择才修改模型值。聊天／向量提供商列表读取异常不阻断配置 GET／POST，`provider_list_errors` 提供非敏感提示，模型输入保留手填 Provider ID。此接口由插件原生 JS 调用，不使用宿主 dashboard 的生成式 API 客户端。

隔离预览增加 `preview_model_list_failure`、`preview_model_list_empty`、`preview_provider_list_failure` 场景；均只使用模拟目录，不连接实际服务。模型列表入口与手填保存可在 `#decision` 检查。

`backend/semantic/query_builder.py` 只构造检索词。决策后端的查询生成仍使用聊天 Provider，不把决策模型当成自由文本生成器；显式 Provider 不继承回复模型覆盖。回复后选择忽略原回复中意外残留的选图标记，主动弃选不能被这些标记绕过。

近期配图参考来自实际成功发送记录，按会话有界保存。宿主 after_message_sent 钩子可能在发送异常被吞掉后执行，不能仅凭该钩子写成功记录；使用成功返回的 event.send／独立图片发送边界，兼容混合消息、格式转换和部分成功。

自动收集的默认视觉路径继续一次请求完成识别、审核、分类及描述。决策路径先进行独立图片理解，再进行收录／分类判断；两阶段缓存分别以分析版本、Provider 配置、图片内容及规则／分类指纹隔离。不确定与决策失败产生强制人工复核记录，所有自动接收入口均检查该状态。新描述未满足语义分类锚定契约时只保存草稿，不标为已完成；人工改分类后重新核对复用条件。旧待审记录的新增来源、复核和概率字段允许缺失。

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

流式装饰钩子在平台消费消息流前过滤文本；最终响应钩子确定选图，平台正常消费完消息流并完成发送后补发图片。补发不依赖 `STREAMING_FINISH` 装饰阶段，兼容插件通过 `event.request_llm()` 发起的流式回复；失败、取消或未消费完的流不补发，后续结束钩子也不会重复发送已处理的选择。普通歧义片段可能缓冲到行尾；待确认的行内反引号片段暂存至闭合或回复结束，未闭合时按普通文本重新解析。待确认片段累计超过 16,384 字符时停止推测并记录 `inline_code_limit_exceeded`，常规模式保留该片段及后续原文，最终标签过滤模式继续清理标记。解析器不是完整 CommonMark 渲染器，不回溯修改已发送内容；单行超过 16,384 字符或推理嵌套超过 64 层时，当前行及后续内容原样通过并记录诊断。

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

图包管理还提供两个异步接口：`await plugin.get_pack_status(pack_id)` 查询本地图包状态（`missing` / `ready` / `error`），`await plugin.install_pack(source, expected_pack_id=None)` 从调用方提供的 GitHub 来源安装。`source` 使用 `{"type": "github", "repo": "owner/repo", "ref": "v1.0.0", "subpath": "."}` 格式。安装返回实际图包 ID 等存储结果，不覆盖已有图包、不修改使用规则、不清理空图包；来源、安装时机及选包策略由调用方负责。指定 `expected_pack_id` 时，清单不符或同 ID 已存在会在写入前抛出异常；未指定时沿用现有导入器的重名编号规则。取消调用会等待现有安装线程收尾。

## 前端开发与检查

页面使用原生 HTML、CSS 和 JavaScript。AstrBot 将静态资源限制在注册页面的目录内，因此四个功能页统一位于 `pages/app/` 下，通过 `../shared/` 引用同一份公共资源。直接修改 `pages/app/shared/` 即可，无需复制或构建。新增资源也应放在 `app/` 内，避免越过页面目录边界。

图床教程位于 `pages/app/settings/index.html`，交互入口在 `storage-guide.js`；更新教程时核对实际配置字段及服务商官方说明，避免写死套餐额度。

主题颜色统一定义在 `pages/app/shared/ui.css`，各页使用语义变量设置背景、文字、边框和状态色。AstrBot 在加载页面和切换主题时更新根元素的 `data-theme="light/dark"`；没有该属性时才跟随系统的深浅色偏好。实心主按钮使用 `--primary` / `--on-primary`，链接使用 `--link`，成功、警告和错误使用各自独立的状态变量。修改颜色时同时检查普通、悬停、禁用和弹窗状态，图片资源保持原色。

公共层同时维护间距、圆角、字号和控件尺寸：间距使用 `--space-4` 至 `--space-32` 的既有档位；常规控件 40px、紧凑控件 32px、触控目标至少 44px。页面主标题、分区标题和卡片标题分别使用 `--font-title`、`--font-subtitle` 和 `--font-section`。按钮外观优先使用公共主按钮及 `secondary`、`ghost`、`danger`，紧凑尺寸使用 `compact`，图标按钮使用 `icon-button`。页面样式只补充业务布局和确有必要的状态，不再通过全局 `button` 或末尾覆盖块重定义公共外观。

原生弹窗使用 `ui-dialog`，标题使用 `ui-dialog-title`。包含独立头部和底部的弹窗额外使用 `ui-dialog-sectioned`，头部和底部使用 `ui-dialog-heading`、`ui-dialog-actions`；页面特例应限定在对应弹窗内。清理旧样式时同时核对 HTML、脚本生成的 DOM 和动态状态类，保留加载、审核、拖拽及移动端抽屉行为。

本地界面预览：

```sh
node tests/frontend/preview.mjs
```

预览使用模拟接口，仅用于布局和交互检查，不能代替真实 AstrBot 联调。URL 可添加 `?theme=light` 或 `?theme=dark` 模拟宿主的初始主题，预览工具中的按钮可以实时切换深浅色；这些控件仅存在于模拟预览中。

设置预览可组合 `preview_selection=category-reply|category-post|semantic-reply|semantic-post`、`preview_backend=decision`、`preview_query_source=reply_text`。`preview_no_models` 模拟缺模型，`preview_service_missing` 模拟服务不完整，`preview_apply_failure` 模拟已保存但应用失败，`preview_conflict` 模拟 revision 冲突。管理与语义页面共享待审字段样例，包括旧记录、未知概率、决策失败及强制人工复核；预览数据在页面内存中保存，刷新页面后重置，绝不保存真实凭据。

## Python 验证

在安装了 AstrBot 与插件依赖的环境中，从插件根目录运行：

```sh
python -m pytest tests -q
ruff format .
ruff check .
```

测试目录若被 Ruff 配置排除，还需显式检查本次修改的测试文件。涉及 AstrBot 初始化的测试应使用隔离的数据目录，避免影响正在运行的实例配置。流式改动应覆盖任意切块、源流取消和异常、重复钩子及非文本组件；模型选图效果需要另行用真实对话验证。

自动收集的完整模拟流程可单独运行：

```sh
python -m pytest tests/test_auto_collect_flow.py -q
node --test tests/frontend/collection-review.test.mjs
```

流程测试使用临时 PNG/GIF 文件、真实后台队列、审核 API、表情包写入及语义存储，覆盖消息临时文件释放、处理中去重、改分类接收、拒收后重启、两种模式自动接收及模型失败恢复。仅外部视觉模型返回值使用可控替身，不会调用真实模型或写入运行中的图库；模型识别质量和平台实际消息格式仍需真实环境验证。

## 概率门控回归验证

`send_meme(category)` 是独立的即时发送动作，`generation.emotion.send_tool_enabled` 默认开启。请求钩子仅在原请求已包含该工具、来源范围允许且主模型支持工具时保留它，分类目录来自当前绑定图包的实际有图分类；不注入绕过宿主工具权限的新工具。工具只接收精确分类键，会话由事件提供，执行前及转换后检查资源仍属于当前图包。工具忽略自动配图概率和数量上限，允许同轮多次调用；使用 `meme_manager_direct_send_attempted` 阻止同轮自动附件或辅助模型再次选图。失败返回稳定原因码，取消传播，转换临时文件始终清理，实际发送复用 `_send_meme_image` 和成功历史记录。

工具说明参考 [OpenAI Function calling 的定义最佳实践](https://developers.openai.com/api/docs/guides/function-calling#best-practices-for-defining-functions)：说明用途、适用与不适用场景、参数格式、调用副作用与返回字段含义；动态分类目录和调用策略放在可移除的系统提示块中。会话、文件路径和发送概率不作为模型参数。

发送判定保存在事件的 `meme_manager_send_probability_hit` 中。Tool 概率策略和命中结果也按事件保存，不按会话全局缓存；重复请求钩子不清空候选或再次抽样。前置请求只包含当前请求中可用的 `search_memes`，使用相同回复 Provider 与 `ProviderRequest.model`，不修改共享工具集或普通回复的 `tool_choice`。执行端最多执行首个声明的调用，并保留工具权限代理。取消会向上传播，失败不会转成分类选图。

```sh
python -m pytest tests/test_probability_gates.py tests/test_streaming_pipeline.py tests/test_plugin_settings.py -q
node --test tests/frontend/settings-mode.test.mjs
```

验证时将 `ASTRBOT_ROOT` 指向临时目录，避免写入正在运行的 AstrBot。所有测试使用模拟 Provider；不要为测试调用实际收费模型。完整测试及代码检查仍按上文执行。本插件没有单独的前端构建或 TypeScript 项目，界面预览使用 `node tests/frontend/preview.mjs`，以模拟设置验证开关、独立数值和重新加载；这不能替代真实模型、平台和网络接口的兼容性验证。
