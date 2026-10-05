# Neko-Court

AstrBot 插件项目。目前包含一个专门用于管理**临时审核群**的插件。

## 插件一览

| 插件 | 版本 | 说明 |
| --- | --- | --- |
| [astrbot_plugin_temp_review_group](astrbot_plugin_temp_review_group/README.md) | v1.1.9 | 临时审核群管理：每日定时清理普通成员、新人入群自动提问、答错 N 次踢出、答对私聊下发当日验证码；判定可选规则或大模型；验证码每日轮换，支持管理员查询/设定/重置/补发 |

`astrbot_plugin_temp_review_group` 的能力：

- **每日清理**：到点把审核群里的普通成员全部移出，保留群主/群管理员/白名单/机器人自身；拉取成员列表失败的群会自动重试；
- **入群审核**：新人入群先发**可单独配置的欢迎语**（默认合成一条：欢迎语 + 问题 + 提示，@ 新人），答错累计到上限移出，答对下发当日验证码（默认走 **QQ 群临时会话**私发，成员无需加机器人好友；可切换群内/两处，两条私聊通道都失败时可回退群内或用指令补发）；
- **判定方式可选**：`rule` 关键词/精确/正则/**模糊相似度**、`llm` 交给大模型判同义表述、`hybrid` 规则先命中省额度；模型超时/报错/不可用时自动回退规则，不会因模型故障误踢人；
- **问题库 / 答案库**：每题可单独启用停用、加提示、配答案库与匹配方式，另有通用答案库（邀请码）与模糊阈值；`/审核 题库` 看抽中/通过统计，`/审核 试答 [#题号] <回答>` 先验证某句话会不会通过再调库；
- **网页答题（可选）**：入群问题会随同步下发到网站，申请人要在网页上答对才通过（网站后台还能另配一套题库、通用答案库与匹配方式），判定逻辑与 QQ 侧逐字一致；
- **审核网站（可选）**：**内置在插件里**（打开 `web_site_enabled` 即可，随插件启动、自动同步验证码与规则），也可用 `review-site/serve.py` 独立部署；申请人在网页上填 QQ + B站 UID，通过后网页直接显示当日验证码；插件主动出站同步验证码/规则并拉取通过名单，这些人入群时不再被提问；
- **B站 UID 审核（可选）**：回答必须是成员的 B站 UID，插件调 B站接口核验账号（等级/粉丝/昵称关键词/一 UID 一 QQ），判定确定且不消耗模型额度；提供 `/审核 查UID`、`/审核 解绑`；
- **验证码轮换**：每天定时用 `secrets` 重新生成，跨天以「最近一次重置时刻」为界；机器人停机期间错过的重置/清理会在启动后补做；
- **管理指令**：`/审核码`、`/设定审核码`、`/审核 状态|题库|试答|诊断|重置码|设定码|放行|补发|重审|踢出|清理|帮助`；
- **Dashboard 页面**：`settings` 页把 52 个配置项分组列出、可编辑保存（含模型提供商下拉框），字段级 + 后端双重校验；`questions` 页是展开式题库列表，可逐题写答案，也能把一段文本交给大模型自动解析成题目+答案填入。

详细功能、配置项说明与行为约定见插件目录下的 [README](astrbot_plugin_temp_review_group/README.md)。

## 安装

要求：AstrBot ≥ 4.10.4，消息平台为 **aiocqhttp（OneBot v11）**，且机器人是审核群的管理员。

方式一（推荐）：从 [Releases](https://github.com/qyac/Neko-Court/releases) 下载 `astrbot_plugin_temp_review_group-v<版本>.zip`，解压到 `AstrBot/data/plugins/`，确认解压后存在 `AstrBot/data/plugins/astrbot_plugin_temp_review_group/main.py`，然后在 WebUI 的插件管理里重载。

方式二：把本仓库的 `astrbot_plugin_temp_review_group/` 目录整体复制到 `AstrBot/data/plugins/` 下。

插件没有任何第三方 Python 依赖，因此不需要 `requirements.txt`。

## 目录结构

```text
Neko-Court/
├─ astrbot_plugin_temp_review_group/   # 插件本体（可直接拷进 data/plugins/）
│  ├─ main.py                          # 入口：审核流程、定时清理、验证码、Pages 后端 API
│  ├─ _conf_schema.json                # 52 项配置定义（WebUI 配置页与设置页的唯一来源）
│  ├─ metadata.yaml                    # 插件元数据
│  ├─ logo.png                         # 256x256 图标
│  ├─ pages/settings/                  # Dashboard 插件设置页（全部配置项）
│  ├─ pages/questions/                 # Dashboard 题库管理页（展开式列表 + 大模型自动填入）
│  ├─ review_web/                      # 审核网站实现（内置：随插件启动；也可独立部署）
│  └─ .astrbot-plugin/i18n/            # 页面标题/描述国际化
├─ selftest_temp_review_group.py       # 插件后端自检（桩替 astrbot.*，472 项断言）
├─ selftest_settings_page.mjs          # 设置页自检（Node，12 项断言）
├─ selftest_questions_page.mjs         # 题库页自检（Node，10 项断言）
├─ selftest_review_site.py             # 审核网站后端自检（196 项，含安全回归、诊断脚本与网页答题）
├─ selftest_review_site_ui.mjs         # 审核网站前端自检（Node，74 项断言）
├─ review-site/serve.py                # 站点独立部署的启动器（实现就是插件里的 review_web/）
├─ tools/package_plugin.py             # 插件打包脚本：排除缓存、固定时间戳、产物自检
├─ tools/package_site.py               # 站点打包脚本（同样固定时间戳、产物自检）
├─ tools/verify_artifacts.py           # 发布前校验：提交 = 工作区 = 产物，包内无 CRLF
├─ tools/diagnose_site.py              # 「网站打不开」诊断（可在跑 AstrBot 的机器上单独跑）
└─ dist/                               # 打包产物（已 gitignore，见 Release）
```

## 开发

自检（都只用标准库，不需要装 AstrBot）：

```bash
python selftest_temp_review_group.py     # 后端：审核流程 / 清理 / 验证码 / 指令 / Web API 与题库接口
node   selftest_settings_page.mjs        # 设置页：分组覆盖 schema、校验与 diff 语义、资源引用
node   selftest_questions_page.mjs       # 题库页：脏数据兜底、校验、diff、草稿转换、i18n 保留
python selftest_review_site.py           # 审核网站后端：接口、鉴权、CSRF、限流、核验、同步/拉取
node   selftest_review_site_ui.mjs       # 审核网站前端：纯函数边界、模板契约、渲染模拟
```

打包并自检产物：

```bash
python tools/package_plugin.py    # → dist/astrbot_plugin_temp_review_group-<版本>.zip
python tools/package_site.py      # → dist/neko-court-review-site-<版本>.zip
```

它会生成 `dist/astrbot_plugin_temp_review_group-<版本>.zip`、写入 `dist/SHA256SUMS.txt`，并把 zip 解压到临时目录检查（顶层目录唯一、必需文件齐全、JSON 可解析、`main.py` 可编译、无 `__pycache__` 混入）。用固定时间戳写条目，所以同样内容永远得到同样的 sha256。

校验某个已发布的包（解压后指向插件目录即可）：

```bash
TEMP_REVIEW_PLUGIN_DIR=/path/to/extracted/astrbot_plugin_temp_review_group python selftest_temp_review_group.py
TEMP_REVIEW_PLUGIN_DIR=/path/to/extracted/astrbot_plugin_temp_review_group node   selftest_settings_page.mjs
```

## 发布

约定：`metadata.yaml` 的 `version` = git 标签名 = 产物文件名里的版本号（当前都是 `v1.1.9`）。

```powershell
# 1) 推送（Git Credential Manager 会弹一次浏览器登录）
git push -u origin main --tags

# 2) 建 Release + 上传产物，两种方式任选
#    方式一：Web UI
#      https://github.com/qyac/Neko-Court/releases/new?tag=v1.1.9
#      标题：astrbot_plugin_temp_review_group v1.1.9
#      说明：粘贴 dist/RELEASE_NOTES-v1.1.9.md
#      附件：dist/astrbot_plugin_temp_review_group-v1.1.9.zip
#    方式二：脚本（需要一个 Contents 读写权限的 token，仅从环境变量读取，不落盘、不回显）
$env:GITHUB_TOKEN = "<PAT>"
python tools/create_release.py --tag v1.1.9 `
  --notes-file dist/RELEASE_NOTES-v1.1.9.md `
  --asset dist/astrbot_plugin_temp_review_group-v1.1.9.zip
```

`tools/create_release.py` 支持 `--dry-run`（只打印计划）、`--draft`、`--prerelease`、
`--replace-asset`（同名产物幂等重传）；执行前会确认远端已存在该标签，避免 GitHub 用默认分支
自建轻量标签、和本地附注标签冲突。

## 版本

当前：**v1.1.9**（与 `astrbot_plugin_temp_review_group/metadata.yaml` 的 `version` 一致，打包文件名也取自它）。

## 许可

本仓库尚未附带开源许可证；如需开源分发，请先添加 `LICENSE`。
