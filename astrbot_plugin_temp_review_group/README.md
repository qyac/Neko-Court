# astrbot_plugin_temp_review_group · 临时审核群管理

一个专门用来管理**临时审核群**的 AstrBot 插件：

| 需求 | 实现 |
| --- | --- |
| ① 每天固定时间清理所有群普通成员 | 每天 `cleanup_time` 拉取群成员列表，把普通成员全部移出（群主/群管理员/白名单/机器人自身默认保留） |
| ② 新人加入指定群聊时发送审核问题 | 监听 `group_increase` 群通知，先发**入群欢迎语**（可单独配置），随机抽一道题 @ 新人附在欢迎语里 |
| ③ 接受消息完成审核，失败 N 次踢出，成功发验证码 | 群内消息逐条判定，答错累计到 `max_attempts` 移出，答对**私聊**下发当日验证码（可切换为群内发码） |
| ④ 每天重置时生成随机验证码，支持管理员查询 | 每天 `code_reset_time` 轮换验证码；`/审核码`、`/审核 状态` 等指令可供管理员查询与管理 |
| ⑤ 审核判定可由大模型完成（可选） | `review_mode` 支持规则判定、大模型判定、两者结合（规则先命中就走规则，省额度）；模型不可用时自动回退规则 |

## 安装

1. 把整个 `astrbot_plugin_temp_review_group` 目录放到 `AstrBot/data/plugins/` 下（目录名不要改）。
2. 打开 AstrBot WebUI → 插件管理 → 重载插件。
3. 进入插件配置页填写：`review_groups`（审核群群号）、`admin_ids`（管理员 QQ）、`questions`（审核问题 + 答案）。

依赖：无第三方 Python 包，`requirements.txt` 不需要。

平台要求：踢人 / 拉取群成员列表使用 OneBot v11 接口，因此只在 **aiocqhttp** 适配器下生效（`metadata.yaml` 已声明 `support_platforms: [aiocqhttp]`）。
**机器人必须是审核群的管理员**，否则 `get_group_member_list`、`set_group_kick` 会失败（日志会给出明确提示）。

## 指令

指令需要 AstrBot 的唤醒前缀（默认 `/`）。管理员 = AstrBot 全局管理员（`admins_id`）或插件配置里的 `admin_ids`；开启 `group_admin_can_query` 后，审核群内的群主/群管理员也可以使用查询类指令。

| 指令 | 说明 |
| --- | --- |
| `/审核码`（别名 `/验证码`） | 查询当前验证码、签发/失效时间、待审核人数 |
| `/设定审核码 <验证码>`（别名 `/设定验证码`） | 手动指定当前验证码 |
| `/审核 状态` | 验证码 + 待审核成员明细 + 已通过成员明细 |
| `/审核 题库` | 查看问题库/答案库：题干、答案、匹配方式、提示、抽中与通过次数 |
| `/审核 试答 [#题号] <回答>` | 先验证「成员这样回会不会通过」，再决定怎么调答案库（不改任何记录） |
| `/审核 诊断` | 排查「新人入群没收到消息」：事件计数、配置核对、群内发送自检、B站 UID 审核状态 |
| `/审核 查UID <UID>` | 查询某个 B站 UID：昵称、等级、粉丝、按当前规则是否通过、被哪个 QQ 绑定 |
| `/审核 解绑 <UID>` | 清除 UID↔QQ 绑定（解除「一个 UID 只能绑一个 QQ」的限制） |
| `/审核 网站 [同步]` | 查看网页审核对接状态（站点、最近同步、累计接收）；加 `同步` 立刻同步一次 |
| `/审核 重置码` | 立即随机重新生成验证码（当天提前轮换） |
| `/审核 设定码 <验证码>` | 手动指定当前验证码（与顶层指令等价） |
| `/审核 放行 <QQ号> [群号]` | 手动放行并下发验证码（也支持 `@某人`） |
| `/审核 补发 <QQ号> [群号]` | 给成员补发当日验证码（私聊；私聊失败时提示改用口头转达或换码） |
| `/审核 重审 <QQ号> [群号]` | 清空该成员的审核记录并重新提问 |
| `/审核 踢出 <QQ号> [群号]` | 手动移出成员并清空记录 |
| `/审核 清理 [群号]` | 立即执行一次清理（留空 = 所有审核群），完成后在当前会话输出统计 |
| `/审核 帮助` | 指令帮助 |

不填群号时，优先用当前群（须在 `review_groups` 中），否则用第一个审核群。

手动设定验证码的规则与行为：

- 接受 1~64 个**不含空白**的字符（不受 `code_charset` / `code_length` 限制，方便用 `neko-2026` 这类自定义码）；超长或空值会被拒绝且不改变现有验证码。
- 设定后会刷新签发日期，因此**当天不会再被 `code_reset_time` 覆盖**，到下一个重置时刻照常轮换。
- 已通过审核的成员手里是旧码，回执会提示还有多少人持有旧码；需要他们改用新码时用 `/审核 放行 <QQ号>` 重新下发。
- 日志只记录「谁设定了验证码」，不记录验证码内容。

## 验证码怎么发（默认群临时会话私发）

`code_send_mode` 决定验证码的送达方式：

| 取值 | 行为 |
| --- | --- |
| `private`（默认） | 私发给通过审核的成员；群里只发一条不含验证码的通过提示 |
| `group` | 只在群里发码（`success_message` 里需要有 `{code}`，没有的话会自动补一行「（验证码：xxx）」） |
| `both` | 私聊 + 群内都发 |

私聊默认走 **QQ 群临时会话**（OneBot 的 `send_private_msg` 带 `group_id`），**成员不需要添加机器人为好友**，只要和机器人在同一个群里。`private_send_channel` 控制私聊通道：

| 取值 | 行为 |
| --- | --- |
| `auto`（默认） | 先走群临时会话；失败再退回好友私聊 |
| `temp_session` | 只用群临时会话（不想给好友发私聊消息时用） |
| `friend` | 只用好友私聊（旧行为） |

- 群临时会话的可用前提：机器人与该成员在同一个群，且多数协议端要求**成员近期在群里发过言**。本插件正是在成员群里答完题后立刻发码，所以正常流程下可用；若管理员用 `/审核 补发` 给一个很久没说话的人补发，可能失败并给出提示。
- 两条私聊通道都失败时会记录日志；开启 `code_fallback_to_group` 可自动改为群内发码兜底（默认关闭，避免验证码意外出现在群里）。
- 管理员可随时用 `/审核 补发 <QQ号> [群号]` 重新私发。
- 反向提醒：`code_send_mode=private` 时如果 `success_message` 里还留着 `{code}`，验证码会出现在群里；插件检测到这种情况会打一条 WARNING 日志提醒你删掉。

## 排查「新人入群没有收到消息」

先确认插件真的被 AstrBot 加载了——**加载成功会生成两个文件**，这是最快的判据：

- `AstrBot/data/config/astrbot_plugin_temp_review_group_config.json`（配置）
- `AstrBot/data/plugin_data/astrbot_plugin_temp_review_group/state.json`（状态，含当日验证码）

两个都不存在，就是没装/没加载/被禁用，先解决安装问题（见「安装」）。插件目录名必须是 `astrbot_plugin_temp_review_group`。

插件已加载后，在审核群里发 `/审核 诊断`，它会给出事件计数与结论：

| 诊断现象 | 原因 | 处理 |
| --- | --- | --- |
| 群通知 0 且群消息 0 | 事件没到插件 | 重载插件；确认机器人已在群内；确认协议端（NapCat/Lagrange）上报群事件 |
| 群消息在涨、群通知一直是 0 | 协议端没有上报群事件（`group_increase`） | 检查协议端的事件上报开关 |
| 非配置群 > 0、入群 0 | 通知来自未配置的群 | 核对 `review_groups` 里的群号 |
| 入群 > 0、题目不可用 > 0 | `questions` 里有条目缺问题或缺答案 | 每条题目都要同时填问题与答案 |
| 入群 > 0、发送失败 > 0 | 机器人被禁言/被风控/协议端异常 | 处理禁言，或看诊断里的「发送自检」结果 |
| 入群 0 | 测试期间没有真正的入群事件 | 让一个人**重新进群**；已在群里的人不会有通知 |
| 已发送问题 > 0 | 流程正常 | 新人没看到时注意：机器人入群前就在群里的人需先发一句话（`auto_enroll_on_speak`），或用 `/审核 重审 <QQ号>` 手动提问 |

几个容易踩的点：

- **入群通知只在"有人真的进群"时产生**；机器人加入前就在群里的人不会有通知，这正是"补发问题"存在的原因。
- 管理员/白名单（`admin_ids`、`exempt_user_ids`）里的账号会被有意跳过。
- 机器人必须在审核群里且没被禁言；发送失败会记进诊断的 `send_fail` 计数。
- 每次重载插件后诊断计数会清零（计数不落盘）。

## 问题库与答案库

题库由**问题库**和**答案库**两部分组成，都可以在设置页里改：

| 配置 | 作用 |
| --- | --- |
| `questions` | **问题库**。每条包含：`enabled` 是否启用、`question` 题干、`hint` 该题提示（可选，会附在提问后面）、`answers` 该题的答案库、`match_mode` 该题的匹配方式 |
| `common_answers` | **通用答案库**。任何题目下命中都直接通过，适合放邀请码、口令这类万能答案 |
| `match_mode` + `fuzzy_threshold` | 全局默认匹配方式与模糊阈值；每道题可以用自己的 `match_mode` 覆盖（`inherit` 表示跟随全局） |

匹配方式：

| 取值 | 行为 | 适用 |
| --- | --- | --- |
| `contains` | 答案作为关键词，出现在回答里即通过 | 答案是一两个关键词（最常用） |
| `exact` | 归一化（全角转半角、去大小写/空白/标点）后完全相等 | 答案唯一且要求严格 |
| `regex` | 把答案当正则表达式匹配 | 需要匹配编号、日期等模式 |
| `fuzzy` | 先按 `contains` 命中；没命中再算相似度，达到 `fuzzy_threshold` 即通过 | 答案较长、成员容易多写或少写一两个字 |

设计要点：

- **抽题时快照**：抽中某道题后，该题的答案库与匹配方式会固化进这条待审核记录，之后你改配置**不会影响正在进行的审核**。
- **停用而不是删除**：`enabled=false` 的题目保留配置但不参与抽题，方便临时下架某道题。
- **可用性判定**：题目可用 = 有题干 且（该题有答案 或 配了 `common_answers`）。可用题目为 0 时新人入群不会收到问题，`/审核 题库` 会直接告诉你"可用题目：0 条"。
- **题库统计**：每题会累计「抽中 / 通过」次数（按题干文本记录，改题面即重新计数），存在状态文件里、不会被每日清理清掉。通过率异常低通常说明答案库写窄了。
- **调库流程**：`/审核 题库` 看现状 → `/审核 试答 #2 我朋友的邀请码1234` 验证某句话会不会过 → 回设置页改答案库或换 `fuzzy`。

> 开启 **B站 UID 审核**（`bili_uid_enabled`）后，判定改由 UID 核验决定：规则匹配与大模型都不再参与，
> 通过与否完全取决于「回答里的 UID 是否真实、是否满足等级/粉丝/昵称要求、是否被别的 QQ 用过」。

## B站 UID 审核（可选）

开启 `bili_uid_enabled` 后，入群提问的「答案」就是成员的 B站 UID：

1. 机器人提问（并自动附上 `bili_uid_prompt` 提示，例如「请把你的 B站 UID 发给我」）；
2. 成员发来 UID——支持纯数字 `12345678`、`UID:12345678`、`UID = 12345678`，或直接发 `https://space.bilibili.com/12345678`；**1~15 位数字都认**（B站 mid 是 64 位整数，长 UID 不会被截断成前几位）；
3. 插件调用 B站公开接口 `x/web-interface/card` 核验账号（带正常 UA/Referer，结果缓存 10 分钟）；
4. 按配置校验：账号真实存在 → 等级 ≥ `bili_uid_min_level` → 粉丝 ≥ `bili_uid_min_fans` → 昵称含 `bili_uid_name_keywords` 之一 → 该 UID 没有被别的 QQ 用过（`bili_uid_unique`）；
5. 全部满足才算通过，照常下发验证码；不满足按答错计次，并在提示里**写明原因**（例如「B站 UID 审核·该 UID 在 B站不存在」）。

要点与取舍：

- **判定是确定性的**：开启后 `review_mode`（规则/大模型）不参与判定，也不会调用大模型——核验结果就是结论。
- **"账号不存在"与"查不到"是两回事**：B站明确返回不存在（`-404`）时**一定判不通过**；只有风控（`-799`/`-352`/HTTP 412）或网络失败才会走 `bili_uid_on_error`（默认 `reject`，严谨；网络环境差时可选 `pass` 避免误伤真人，此时记录会标记为未核验）。
- **防一码多用**：UID↔QQ 绑定持久化在状态文件里，别的 QQ 再用同一个 UID 会被拒；管理员可用 `/审核 解绑 <UID>` 清除（例如成员换号）。
- **审计**：通过记录的 `approved` 里会保存 UID 与 B站昵称，`/审核 状态` 直接显示；`/审核 诊断` 会报告已绑定数量与本次启动的缓存条数。
- **依赖外网**：需要机器能访问 `api.bilibili.com`。B站接口不需要登录 Cookie，但风控敏感，插件只在成员回答时查询一次（命中缓存不重复请求）。
- **粉丝数不是判断真人的可靠指标**，按需使用；`bili_uid_name_keywords` 常用于"要求成员先把昵称改成指定内容"的场景。

## 审核判定：规则 / 大模型

`review_mode` 决定"这句话算不算答对"：

| 取值 | 行为 | 成本 |
| --- | --- | --- |
| `rule`（默认） | 只用 `match_mode` + 参考答案做关键词/精确/正则匹配 | 无 |
| `llm` | 把审核问题、参考答案、成员回答交给大模型判定 | 每次不通过规则的回答调用一次 |
| `hybrid` | 规则先命中就直接通过；没命中才交给大模型 | 只对"疑似答错"的调用，最省 |

- `review_llm_provider` 留空表示跟随会话当前模型；建议在 WebUI 里显式选一个小而快的模型（设置页会给成下拉框）。
- `llm_review_prompt` 是判定提示词，占位符 `{question}` `{answers}` `{answer}` `{user}` `{group}`；模型被要求只输出 `PASS` 或 `FAIL`。
- **任何异常都会回退规则判定**，成员不会被卡住：模型超时（`llm_review_timeout_seconds`）、调用报错、返回无法解析、Provider 不存在、当前会话没有可用模型——这些情况都会退回 `match_mode` 的判定结果并记录日志。
- 解析顺序是"先严格匹配回复首词，再全文扫描，且先看否定词"——所以「该回答不通过」不会被「通过」误判为通过。
- 隐私与计费：成员的回答会发送给所选模型服务商；开启前请确认这符合你群里的预期，并留意额度消耗。

## WebUI 设置页（插件 Pages）

除了 AstrBot 根据 `_conf_schema.json` 自动生成的配置表单，本插件还带**两个页面**，出现在**插件详情页**里（WebUI → 插件管理 → 本插件 → 详情页；页面清单有缓存，看不到时刷新一下或重载插件）：

- `settings`：全部配置项的分组表单（见下）；
- `questions`：**题库管理**——可展开的问题列表 + 大模型自动填入（见「题库管理页」）。

### settings 页

页面内容：

- 顶部：插件名/版本 + 只读运行状态（当前验证码、失效时间、待审核与已通过人数、审核群数量、下一次清理时间、服务器时间、状态文件路径）；
- 中部：**33 个配置项按 5 组列出**（基础设置 / 审核流程 / 验证码 / 每日清理 / 其他），每项显示当前值、说明文字（`hint`）与对应控件：
  - `bool` 开关；`int`/`float` 数字框（有 `slider` 时带滑块与上下限）；`string` 下拉（有 `options` 时）、提供商下拉（`_special: select_provider`）或文本框；`text` 多行文本；`list` 标签（chips）编辑器；`template_list` 可增删的卡片编辑器；
  - 答案、群号这类列表支持回车/逗号添加、粘贴多行自动拆分去重；
  - 题库也可以在这里以卡片形式编辑；想用更好用的展开式列表与"大模型自动填入"，用 `questions` 页；
- 底部操作：`保存`（只提交发生变化的项）、`重新加载`、`恢复默认值`（仅回填表单，仍需点保存）；
- 字段级校验在页面内先做一遍，后端再校验一遍；保存成功后会提示 `warnings`（例如 `review_groups` 为空、题目缺答案）。
- 允许"清空"语义：清空 `review_groups` / `admin_ids` / `exempt_user_ids` / 题库都能正常保存（后端分别给出 `warnings`），页面不会卡住；但列表项不能是空字符串、审核问题的内容不能留空（标了 `allow_empty` 的可选字段如"该题提示"除外）。

### 题库管理页（`questions`）

- **可展开的问题列表**：折叠态一行显示序号、启用状态、题干、匹配方式、`抽中 N / 通过 M`、以及"不会被抽中"的警示；展开后可编辑题干、该题提示、**答案库（chips）**、匹配方式、启用开关，也能删除该题。支持全部展开/全部折叠，新增题目默认展开。
- **通用答案库**：同一页编辑，任何题目下命中都直接通过（邀请码/口令）。
- **大模型自动填入**：把招新公告、题库文档、聊天记录等**一段文本**粘进去，点「让大模型解析」，模型会提取出"题目 + 答案（含同义说法）"的**草稿列表**；草稿可勾选、可在加入前直接改，再点「加入题库」追加到列表末尾——**只有点「保存题库」才真正写入配置**。
- 顶部状态条显示题目总数、可用题目数、通用答案数、判定方式与解析用的模型；无可用模型时解析按钮会禁用并说明原因（先在 AstrBot 配置模型，或在插件配置里指定 `review_llm_provider`）。

后端接口（路由必须带插件名前缀，页面里用不带前缀的 endpoint）：

| 方法 | 路由 | 说明 |
| --- | --- | --- |
| GET | `/astrbot_plugin_temp_review_group/settings` | 返回 schema、当前配置值、运行状态、可选模型、插件元信息 |
| POST | `/astrbot_plugin_temp_review_group/settings` | 校验并保存；返回最新全量值与 `warnings` |
| GET | `/astrbot_plugin_temp_review_group/questions` | 返回全部题目（含停用）与每题统计、通用答案库、可选匹配方式 |
| POST | `/astrbot_plugin_temp_review_group/questions` | 校验并保存题库；只读字段（`picks`/`passes`/`usable`）会被忽略 |
| POST | `/astrbot_plugin_temp_review_group/questions/parse` | 把 `{"text": "…"}` 交给大模型解析成题目草稿（不写配置） |

行为与安全约定：

- 只接受 schema 里存在的配置项（未知 key 直接拒绝）；`int`/`float` 越界、`options` 之外的值、非法 `HH:MM`、不可用的时区、字符集少于 4 个字符都会被拒绝并给出中文原因，**不会静默回退**；
- `template_list` 会校验 `__template_key` 并在写回前丢弃未知子键（题库页面每条题目都会带上它）；
- 写回使用 `AstrBotConfig.save_config_async()`（原子上写），配置对象不支持保存时返回 500 并提示改用自带配置页，绝不假装保存成功；
- 保存后立即生效（后台循环每轮都重新读取配置），并记录操作者（Dashboard 用户名），日志里不出现验证码、密钥等内容；
- LLM 解析有边界：文本上限 8000 字、一次最多产出 30 条草稿、超时按 `max(30s, llm_review_timeout_seconds)`；模型返回不是 JSON 时返回 400 并把**原始输出**一并给出，方便人工整理；
- `secret: true` 的配置项在页面上不回显真实值（留空表示沿用旧值），本插件当前没有此类配置项。

兼容性：页面依赖 `astrbot.api.web`（AstrBot 4.10+ 均已提供）。若运行环境没有它，插件照常工作，只是两个页面不可用（日志会给出提示）。

## 审核网站（可选，两种用法）

网站实现**内置在插件里**（`review_web/`），所以有两种开启方式，共用同一份代码：

| 方式 | 怎么开 | 适合 |
| --- | --- | --- |
| **内置（推荐）** | 插件配置里打开 `web_site_enabled`（可调 `web_site_host` / `web_site_port` / `web_site_trust_proxy`） | 只想装一个插件就有网站；插件启动时自己在后台起站点，**不用填 url/token**，验证码/规则自动同步 |
| **独立部署** | 打开 `web_review_enabled` 并填 `web_review_url` + `web_review_token` | 想把站点放在另一台机器/另一套反代后面；用 `review-site/serve.py` 或站点 zip 单独跑 |

打不开网页时，在运行 AstrBot 的机器上跑 `python tools/diagnose_site.py --remote` 会逐项告诉你卡在哪（开关没开 / 只监听 127.0.0.1 / 防火墙 / 端口被占 / 版本过旧）。

网页也能出题：插件会把**题库与通用答案库一起同步给网站**（网站没有自己的题库时就用这套），申请人答对才通过；管理员也可以在网站后台另配一套站点题库（站点题库优先）。

QQ 里的管理指令：`/审核 网站`（状态）、`/审核 网站 同步`（立刻同步）、`/审核 网站 地址`（可分享链接）、
`/审核 网站 token`（改用独立部署时要的密钥）、`/审核 网站 密码 <新密码>`（改站点后台密码）。

## 网页审核对接原理

插件可以和仓库里的 [审核网站](../review-site/README.md) 联动：申请人在网页上填 QQ + B站 UID，
核验通过后**网页直接显示当日验证码**；插件把网页通过的人接进来，他们入群时不再被提问。

- **方向是插件主动出站**：插件定期把验证码、有效期、审核群、B站 规则、UID↔QQ 绑定推给站点
  （`POST /api/plugin/sync`），再把站点上"已通过未通知"的记录拉走并 ack
  （`GET /api/plugin/applications` → `POST /api/plugin/ack`）。站点**不需要**反向访问 AstrBot，
  所以 AstrBot 在内网/NAT 后面也能用。
- **两阶段投递**：拉取只读取，插件处理完再 ack；中途崩溃下一轮会重拉，写入是幂等的（不会重复放行）。
- **免提问**：网页通过的人入群时，插件直接私发当日验证码（`web_review_auto_approve=true`，可关）。
- **一份码**：验证码始终由插件生成与轮换，站点只是展示，两边不会出现两个码。
- **配置**：`web_review_enabled` + `web_review_url` + `web_review_token`（token 在站点启动时打印），
  同步间隔 `web_review_poll_seconds` 默认 30 秒。可用 `/审核 网站` 查看状态、`/审核 网站 同步` 立即同步。

## 工作流程

```
新人入群 ──► 随机抽题、@ 新人提问 ──► 待审核记录 attempts=0
                                        │
        群内回答 ──► 判定（规则 / 大模型）──► 通过 ──► 记录"已通过" ──► 私聊下发当日验证码
                 │                                    （可选 group/both：群里也发码）
                 └─ 未通过 ──► attempts+1 ──► 未达上限：提示剩余次数
                                            └─ 达到上限：set_group_kick 移出并提示
每天 cleanup_time ──► 遍历审核群 ──► 移出所有普通成员 ──► 复位待审核/已通过记录
每天 code_reset_time ──► secrets 随机生成新验证码（记录签发日期）
```

要点：

- **答案匹配**（`match_mode`）：`contains` 关键词包含 / `exact` 归一化后完全相等 / `regex` 正则匹配。归一化会做全角转半角、转小写、去空白与标点，所以「２」「 2 」「2。」都能匹配答案「2」。
- **不误吞指令**：命中任何指令过滤器（含其它插件的指令/正则监听）的消息不会被当作答题。
- **入群即欢迎**：新人入群（或首次发言触发补发）时先发 `welcome_message` 欢迎语，问题按 `join_message_mode` 附在后面——默认 `merge` 合成一条消息（欢迎语 → 问题 → 次数提示）；合成时若欢迎语与问题里都写了 `{at}`，只会保留一个 @。管理员用 `/审核 重审` 手动补发时不重复发欢迎语。
- **补发问题**：机器人漏掉入群事件（例如重启）时，普通成员在审核群里发言会补发问题，且**不消耗**答题次数（`auto_enroll_on_speak`，可关闭）。
- **每日复位**：跨天以「最近一次重置时间」为界——过了午夜但还没到重置时刻，验证码仍是上一轮的有效码。
- **停机补做**：启动后若发现当天的清理/重置时刻已过且当天没做过，会立即补做一次（依据状态文件里的 `last_cleanup_date`、`code_date`）。
- **清理容错**：拉取成员列表失败的群会每 5 分钟重试，最多 3 次；自动清理还会在完成后复位审核记录。

## 配置项

| 配置 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `review_groups` | list | `[]` | 审核群群号；留空则插件不做任何事 |
| `admin_ids` | list | `[]` | 插件管理员 QQ（可用管理指令、免清理免审核） |
| `exempt_user_ids` | list | `[]` | 免清理免审核，但**不**获得管理指令权限 |
| `questions` | template_list | 3 条示例（1 条默认停用） | **问题库**；每条含 `enabled` / `question` / `hint` / `answers` / `match_mode`，见上文 |
| `common_answers` | list | `[]` | **通用答案库**：任何题目下命中即通过（邀请码/口令） |
| `match_mode` | string | `contains` | 全局默认：`contains` / `exact` / `regex` / `fuzzy` |
| `fuzzy_threshold` | float | `0.8` | fuzzy 模式的相似度阈值（0.5~1.0） |
| `review_mode` | string | `rule` | `rule` / `llm` / `hybrid`，见上文 |
| `review_llm_provider` | string | `""` | 审核用模型提供商 id；留空跟随会话模型 |
| `llm_review_prompt` | text | 见配置页 | 判定提示词，占位符 `{question}` `{answers}` `{answer}` `{user}` `{group}` |
| `llm_review_timeout_seconds` | float | `20` | 模型判定超时，超时回退规则 |
| `max_attempts` | int | `3` | 允许答错次数 |
| `kick_on_fail` | bool | `true` | 答错次数用尽后移出 |
| `reject_add_request` | bool | `false` | 踢人时同时拒绝再次加群（清理与审核失败都生效） |
| `auto_enroll_on_speak` | bool | `true` | 补发问题（不消耗次数） |
| `welcome_message` | text | 见配置页 | **入群欢迎语**，占位符 `{at}` `{user}` `{group}` `{question}` `{max_attempts}`；留空=不发欢迎语 |
| `join_message_mode` | string | `merge` | `merge` 欢迎语+问题合成一条 / `separate` 先欢迎语再问题 / `question_only` 只发问题 |
| `question_message` | text | 见配置页 | 提问文案（问题部分），占位符 `{at}` `{user}` `{question}` `{max_attempts}` `{hint}` |
| `retry_message` | text | 见配置页 | 占位符 `{at}` `{user}` `{remaining}` `{max_attempts}` |
| `success_message` | text | 见配置页 | 占位符 `{at}` `{user}` `{code}` `{expire}` |
| `kick_message` | text | 见配置页 | 占位符 `{user}` `{max_attempts}`（踢人提示默认不 @，需要就在模板里加 `{at}`） |
| `bili_uid_enabled` | bool | `false` | 开启后回答必须是（含）B站 UID，判定只由 UID 核验决定 |
| `bili_uid_prompt` | text | 见配置页 | 自动附在提问后的索取 UID 提示（提问里已提到 UID 则不重复附加） |
| `bili_uid_min_level` | int | `0` | 要求的最低 B站等级（0=不检查） |
| `bili_uid_min_fans` | int | `0` | 要求的最低粉丝数（0=不检查） |
| `bili_uid_name_keywords` | list | `[]` | B站昵称必须包含的关键词（留空=不检查） |
| `bili_uid_unique` | bool | `true` | 一个 UID 只能绑一个 QQ（防一码多用） |
| `bili_uid_on_error` | string | `reject` | 接口被风控/网络失败时：`reject` 判不通过 / `pass` 放行 |
| `bili_uid_timeout_seconds` | float | `10` | B站接口单次超时（风控时会自动重试一次） |
| `web_review_enabled` | bool | `false` | 启用与审核网站的对接 |
| `web_review_url` | string | `""` | 审核网站地址（AstrBot 能访问到的），如 `http://127.0.0.1:8787` |
| `web_review_token` | string（secret） | `""` | 站点启动时打印的共享 token；**不回显**，留空表示不修改 |
| `web_review_poll_seconds` | int | `30` | 同步/拉取间隔（10~600 秒） |
| `web_review_auto_approve` | bool | `true` | 网页通过者入群免提问，直接私发验证码 |
| `web_site_enabled` | bool | `false` | **把审核网站内置在插件里**（随插件启动，无需填 url/token） |
| `web_site_host` | string | `127.0.0.1` | 内置站点监听地址；`0.0.0.0` 才能被群友直接访问 |
| `web_site_port` | int | `8787` | 内置站点端口（申请页 `http://IP:端口/`，后台 `/admin`） |
| `web_site_trust_proxy` | bool | `false` | 内置站点放在反向代理后面时打开（否则不信任 X-Forwarded-For） |
| `code_send_mode` | string | `private` | `private` / `group` / `both`，见上文 |
| `private_code_message` | text | 见配置页 | 私聊发码文案，占位符 `{code}` `{expire}` `{user}` `{group}` |
| `code_fallback_to_group` | bool | `false` | 私聊失败时是否改为群内发码兜底 |
| `private_send_channel` | string | `auto` | `auto` / `temp_session` / `friend`，见上文 |
| `code_length` | int | `6` | 4~32 位 |
| `code_charset` | string | 去混淆字符集 | 至少 4 个不同字符 |
| `code_reset_time` | string | `00:00` | 每日重置验证码时刻；`off` / 留空 = 不自动重置 |
| `cleanup_time` | string | `00:00` | 每日清理时刻；`off` / 留空 = 关闭定时清理 |
| `timezone` | string | `""` | IANA 时区（如 `Asia/Shanghai`）；留空用系统本地时区 |
| `cleanup_keep_approved` | bool | `false` | 清理时保留已通过审核的成员 |
| `cleanup_notice` | text | `""` | 清理后的群公告，占位符 `{count}` `{group}`；留空不发 |
| `kick_interval_seconds` | float | `1.0` | 踢人间隔，降低协议端风控概率 |
| `keep_group_admins` | bool | `true` | 保留群主/群管理员 |
| `group_admin_can_query` | bool | `true` | 允许审核群群主/管理员查询验证码 |

## 数据与接口

- 状态文件：`AstrBot/data/plugin_data/astrbot_plugin_temp_review_group/state.json`
  （验证码、签发日期、待审核记录、已通过记录、群↔平台适配器映射、机器人自身 ID；原子写入，重启可恢复）。
- 使用的 OneBot v11 接口：`get_group_member_list`、`get_group_member_info`、`set_group_kick`、`get_login_info`。
- 用到的 AstrBot 能力：`StarTools.get_data_dir`、`context.send_message`、`context.platform_manager`、`context.register_web_api`、`context.get_registered_star`、`astrbot.api.web`、`AstrBotConfig.save_config_async`、`event.send`/`chain_result`、`filter.event_message_type`、`filter.command`、`filter.command_group`。
- 插件 Pages：`pages/settings/`（配置表单）与 `pages/questions/`（题库管理：展开式列表 + 大模型自动填入），国际化在 `.astrbot-plugin/i18n/{zh-CN,en-US}.json`；后端接口 `settings`、`questions`、`questions/parse`。

## 注意事项与已知取舍

- **清理语义**：默认「普通成员全部移出」，已通过审核的人也会被移出（他们在通过时已拿到验证码）。若希望保留他们，请开启 `cleanup_keep_approved`。
- **验证码默认走群临时会话私发**：`code_send_mode=private`（默认）时群里不会出现验证码，成员**无需加机器人好友**；`private_send_channel` 可选 `auto`/`temp_session`/`friend`。想改成群内发码就切 `group`，并确认 `success_message` 里有 `{code}`。
- **从 v1.0 升级**：旧配置项 `send_code_private` 已被 `code_send_mode` 取代，AstrBot 更新 schema 时会自动移除旧项，无需手工清理。
- **大模型审核的边界**：LLM 判定只用于"这句话算不算答对"，踢人/清理/发码仍由插件按确定性逻辑执行；模型不可用永远回退规则，不会因为模型故障误踢人。
- **验证码不写日志**：日志只记录「已验证码已重置」，验证码本身只在管理员指令里返回。
- **唤醒前缀后的内容**：群成员用 `/xxx` 唤醒机器人时，如果 `xxx` 不是任何已注册指令，该内容会被当成答题内容（这是有意为之，方便成员带前缀作答）。
- **不做超时踢人**：未作答的成员会留到当天的每日清理。若需要「M 分钟未作答自动移出」，可在此基础上扩展。
- **`template_list` 需要 AstrBot ≥ 4.10.4**（`metadata.yaml` 已声明 `astrbot_version: ">=4.10.4"`）。
- 本插件只在 Unofficial OneBot v11（NapCat / Lagrange 等）下验证过接口语义；QQ 官方机器人没有踢人/成员列表能力。

## 自检

仓库根目录有五个自检脚本（插件后端 + 两个 Pages + 审核网站后端 + 审核网站前端），都只用标准库、不依赖 AstrBot 本体：

1. `selftest_temp_review_group.py`（Python）用桩模块替换 `astrbot.*`，直接导入本插件的 `main.py`，覆盖答案匹配、验证码生成、时间解析、入群提问、答错踢出、答对发码、指令消息不误判、每日清理（含白名单与保留已通过）、状态持久化、权限判定、主动发送、查询/设定/重置验证码与放行/补发、重审、踢出、清理等全部管理指令，**LLM 审核**（PASS/FAIL 解析、否定词优先、超时/报错/无 Provider/提供商不存在时的规则回退、hybrid 省额度、提示词占位符），**发码渠道**（private/group/both、群临时会话 `send_private_msg`、auto/temp_session/friend 三种通道、临时会话失败退回好友、私聊失败回退群内、文案缺 `{code}` 自动补码），**排查能力**（群通知/入群/退群/非配置群/群消息事件计数、跳过原因、发送失败计数、重复入群重新提问、`/审核 诊断` 结论与群内发送自检），**问题库/答案库**（停用题目不参与抽题、单题匹配方式覆盖全局、fuzzy 相似度与阈值边界、通用答案库、题干快照进记录、该题提示自动附加、抽中/通过统计与不被每日清理清掉、`/审核 题库` 与 `/审核 试答` 的输出与"不改动记录"特性），以及 **WebUI 后端 API**（GET/POST 契约、类型与范围校验、各类拒绝路径、保存失败处理）和 Pages 资源结构，共 **496 项断言**：

   ```bash
   python selftest_temp_review_group.py
   ```

2. `selftest_settings_page.mjs`（Node，12 项）校验设置页 `pages/settings/settings.js` 的纯函数与静态资源引用，其中最关键的两条是：**页面分组覆盖了 `_conf_schema.json` 里的全部配置项**（防止以后加配置项却忘了加到页面）、**清空列表/题库必须能进入保存 payload**（防止页面比后端更严格导致配置改不动）：

   ```bash
   node selftest_settings_page.mjs
   ```

3. `selftest_questions_page.mjs`（Node，10 项）校验题库页 `pages/questions/bank.js`：脏数据兜底、题目校验（空题干拒绝 / 非法匹配方式拒绝 / 空答案允许）、0 条可用题目属警告而非错误、`diffBank` 正确识别增删改与顺序变化并忽略 `picks/passes/usable`、草稿转题目、以及两个页面的 i18n 都还在（防止覆盖 `pages.settings`）：

   ```bash
   node selftest_questions_page.mjs
   ```

这些脚本都只是开发辅助，位于**开发仓库根目录**（不在插件目录内，因此不随插件包分发），可以随时删除，不影响插件运行。

## 打包与分发

插件目录本身就是可分发的包（等于 AstrBot 插件仓库的仓库根）：

```text
astrbot_plugin_temp_review_group/
├─ main.py                 # 插件入口（含 Pages 后端 API）
├─ metadata.yaml           # 插件元数据
├─ _conf_schema.json       # 24 项配置定义
├─ logo.png                # 256x256 插件图标
├─ README.md
├─ pages/settings/         # Dashboard 设置页（index.html / app.js / settings.js / style.css）
└─ .astrbot-plugin/i18n/   # 页面标题与描述的国际化
```

没有任何第三方依赖，因此**不需要 `requirements.txt`**。打包时注意排除 `__pycache__`（本仓库的 `tools/package_plugin.py` 已自动排除，并用固定时间戳生成可复现的 zip）。

两种安装方式：

1. **WebUI 安装**：把 zip 上传/或把仓库地址交给插件的安装入口（AstrBot 会把它放到 `data/plugins/<插件名>/`）；
2. **手动安装**：解压后确认目录层级为 `AstrBot/data/plugins/astrbot_plugin_temp_review_group/main.py`，然后在 WebUI 插件管理里重载。

校验打包产物：把 zip 解压到任意目录后，用环境变量指向它跑自检（插件后端与 Pages 脚本支持）：

```bash
TEMP_REVIEW_PLUGIN_DIR=/path/to/extracted/astrbot_plugin_temp_review_group python selftest_temp_review_group.py
TEMP_REVIEW_PLUGIN_DIR=/path/to/extracted/astrbot_plugin_temp_review_group node selftest_settings_page.mjs
```
