# 表情包资源广场 · GitHub Pages

独立静态展示页，源代码位于 `gh-pages` 分支的 `docs/`。无需安装 Node 依赖或运行构建命令，不调用插件私有 API。

## 本地预览

在仓库根目录运行 `python -m http.server 4173 --directory docs --bind 127.0.0.1`，打开 `http://127.0.0.1:4173`。

## 发布

在确认页面后推送 `gh-pages` 分支，并在仓库 Settings → Pages 选择 **Deploy from a branch**，分支 **gh-pages**、目录 **/docs**。

所有资源路径均为相对路径，适配项目站点 `/astrbot_plugin_meme_manager/`。本分支保留插件源文件，展示页集中于 `docs/`，无需合入 `main`。

## 数据

启动时先展示 `data/community-index.json` 快照，随后请求官方社区索引最新数据。实时请求失败时保留已有列表。可从 https://raw.githubusercontent.com/anka-afk/astrbot-meme-pack-index/main/community-index.json 更新随站快照。

预览图使用各包在索引中声明的地址；图片加载失败有替代提示。筛选依据索引标签，不推断未声明的图片格式。最近收录按索引追加顺序倒序展示，并非按包版本发布时间排序。

审核状态仅展示索引的 `verified` 字段，不对素材权利做额外背书。页面提供来源链接、仓库 ZIP 下载与安装来源复制；安装由 AstrBot 插件完成。
