# 临时审核群 · 审核网站

一个**入群审核网站**：可以**内置在插件里随插件运行**（推荐，装一个插件就有网站），
也可以用仓库里的 `review-site/serve.py` 独立部署成单独进程（两种方式共用这一份实现）。

：申请人在网页上填 QQ + B站 UID，站点核验 B站 账号后**直接把当日验证码显示在网页上**；
管理员在 `/admin` 看队列、手动通过/拒绝/拉黑、导出 CSV、改规则与站点设置。

- **零第三方依赖**：只用 Python 标准库（`http.server` + `sqlite3` + `urllib`），不需要 Flask/Django/Node。
- **站点永远不需要反向访问 AstrBot**：由插件主动出站同步与拉取（见下），所以 AstrBot 在内网/NAT/没有公网 IP 时也能用。

## 它是怎么和 QQ 插件联动的

```
申请人 ──填表──▶ 审核网站（本程序，SQLite）
                    ▲              │
        插件主动出站 │              │ 插件主动出站
        POST /api/plugin/sync      │ GET /api/plugin/applications → POST /api/plugin/ack
                    │              ▼
              AstrBot 插件（验证码/规则/UID 绑定的唯一权威）
```

1. **同步（插件 → 站点）**：插件每隔 `web_review_poll_seconds` 秒把当前**验证码**、有效期、审核群号、
   B站 审核规则、UID↔QQ 绑定、待审核/已通过统计推给站点。站点因此能：
   - 把验证码直接显示给通过的人；
   - 用插件的那套规则做判定（`use_plugin_rules=true` 时）；
   - 识别"这个 UID 已经被别的 QQ 用过"。
2. **拉取（插件 → 站点）**：插件把站点上"已通过但还没通知插件"的记录拉走，写入自己的 `approved` 名单并记住 B站 UID，
   然后 **ack** 告知站点（两阶段，插件半路崩了下一轮会重拉，写入是幂等的）。
3. **入群免提问**：网页通过的人进群时，插件不再提问，直接私发当日验证码（`web_review_auto_approve=true`，可关）。

站点侧只做**核验与展示**，验证码的生成与轮换始终在插件里，两边永远只有一份码。

## 方式一：内置（推荐）

在 AstrBot 插件配置里打开这三项即可，**不需要单独部署、也不需要填 url/token**：

| 配置 | 建议值 |
| --- | --- |
| `web_site_enabled` | 打开 |
| `web_site_host` | `127.0.0.1`（配反代）或 `0.0.0.0`（让群友直接访问） |
| `web_site_port` | `8787` |
| `web_site_trust_proxy` | 放在 nginx/caddy 后面时打开 |

插件启动时会在后台线程里把站点跑起来，并**自动**把验证码/规则同步过去；首次启动的管理员密码会打印在 AstrBot 日志里。
QQ 里用 `/审核 网站` 看状态与地址、`/审核 网站 地址` 拿可分享链接、`/审核 网站 密码 <新密码>` 改后台密码。
站点数据放在 `<AstrBot>/data/plugin_data/astrbot_plugin_temp_review_group/review-web/review-site.db`（与插件状态文件同目录，一起备份即可）。

## 方式二：独立部署

```bash
# 1) 启动（首次会打印随机管理员密码与插件 token，只显示一次）
python review-site/serve.py --host 0.0.0.0 --port 8787

# 想自己指定：
python review-site/serve.py --host 0.0.0.0 --port 8787 \
  --admin-password '你的管理员密码（≥8位）' \
  --plugin-token '至少16位的随机串'

# 只看当前配置（含插件要填的两项）：
python review-site/serve.py --print-config
```

启动后：

- 申请页 `http://<地址>:8787/` —— 把这个链接发到群里/公告里；
- 管理后台 `http://<地址>:8787/admin` —— 用管理员密码登录（首次登录后请改密码）；
- 数据文件：独立部署时在 `review_web/data/review-site.db`；内置模式在插件数据目录的 `review-web/review-site.db`。

然后在 **AstrBot 插件配置**里填两项（插件侧配置 → 网页审核对接）：

| 配置 | 值 |
| --- | --- |
| `web_review_enabled` | 打开 |
| `web_review_url` | 站点地址，例如 `http://127.0.0.1:8787`（AstrBot 能访问到的地址；同一台机器就用 127.0.0.1） |
| `web_review_token` | 启动时打印的那串（也可以在后台"设置"里看到/重置：站点只用于校验，不再展示给申请人） |
| `web_review_poll_seconds` | 同步间隔，默认 30 秒 |
| `web_review_auto_approve` | 网页通过者入群免提问，默认开 |

一个轮询周期内，QQ 里用 `/审核 网站` 就能看到"最近同步 / 累计接收 / 待投递"，`/审核 网站 同步` 可立刻同步一次。

## 申请人流程

1. 打开申请页，填 **QQ 号** 与 **B站 UID**（纯数字 `12345678`、`UID:12345678`、`UID = 12345678`，
   或直接粘贴 `https://space.bilibili.com/12345678`，四种写法都认；**1~15 位数字都支持**，长 UID 不会被截断，超长（16 位以上）的数字不会被当成 UID），可加备注；
2. 站点调用 B站 公开接口核验（不需要登录 Cookie）：账号真实存在 → 等级 ≥ 下限 → 粉丝 ≥ 下限 → 昵称含关键词 → UID 未被别的 QQ 用过；
3. **（可选）回答审核问题**：题库里只要有一条可用题目，页面就会出现一道题（管理员可随时开关）。
   站点**只把题干和提示发给浏览器，绝不发送参考答案**；判定在服务端完成，答错时只回「回答不正确」，
   不会把答案提示出来（判定详情只记在后台给管理员看）。
4. 三态结果：
   - **通过**：网页直接显示**大号验证码 + 一键复制 + 有效期**，提示把码发给群里的机器人；
   - **未通过**：显示具体原因（等级不够/昵称不符/UID 不存在/UID 已被使用……）；
   - **待人工**：B站 接口被风控、或本站关闭了自动核验时，转给管理员在后台处理。

规则说明：**B站 明确返回"用户不存在"（`-404`）时永远不通过**；只有风控（`-799`/`-352`/HTTP 412）或网络失败才按
`bili_on_error` 处理（`reject` 直接不通过 / `manual` 转人工 / `pass` 放行但标记未核验）。

## 网页答题与题库

**题目以谁的题库为准**由插件配置 `web_question_mode` 决定（插件会把这个设置同步过来，站点只读显示）：

| 模式 | 出题用哪套题 | QS 侧用什么 |
| --- | --- | --- |
| `plugin`（默认） | 插件题库（站点题库不参与出题） | 插件题库 |
| `site` | 站点题库 | 插件每轮把站点题库**反向拉回插件配置**，所以 QQ 与网页共用一套题 |

**答题错误次数限制**：同一个 QQ 在窗口内答错到上限后，再提交会被直接拒绝（提示联系管理员）。
默认「24 小时内最多 3 次」，可在后台题库面板调整，填 0 表示不限。

**后台一键通过后立即发码**：管理员在后台点「通过」后，插件在下一个轮询周期就会尝试把当日验证码
私发给该 QQ（临时会话优先，失败退回好友私聊）；发不出去也不要紧，他入群时会再发一次。
这个行为由插件的 `web_review_push_code` 开关控制（默认开）。


网页可以像 QQ 里一样出题，题库有两个来源，**站点题库优先**：

| 来源 | 说明 |
| --- | --- |
| **站点题库** | 在管理后台的「题库」里新增/编辑的题目，存在站点自己的数据库里 |
| **插件题库** | QQ 插件每隔一个轮询周期同步过来的题目（含每题答案、提示、匹配方式） |

规则与判定（**与 QQ 侧逐字一致**，插件自检里有「两边判定完全一致」的对照用例）：

- 匹配方式：`contains`（关键词包含）/ `exact`（归一化后完全相等）/ `regex`（正则）/ `fuzzy`（先包含、再算相似度）；
  每道题可以单独覆盖（`inherit` = 跟随题库默认）；
- 归一化：全角转半角、转小写、去掉空白与标点，所以「２」「2。」「 2 」都能匹配答案「2」；
- **通用答案库**：站点自己的通用答案（后台可配）与插件同步过来的通用答案都会生效，任何题目下命中即通过；
- 出题策略：从**启用且可用**（有题干，且该题有答案或配了通用答案库）的题目里随机抽一道；`total > 1` 时页面有「换一题」；
- 关闭 `web_ask_questions`（后台「题库」顶部，或 `POST /api/admin/questions` 的 `set_ask`）后网页不再出题，
  申请人只需填 QQ 与 B站 UID。

## 管理后台

- 搜索（QQ/UID/昵称/备注，防抖）+ 状态筛选（全部/待审核/已通过/未通过/待人工/已拉黑）+ 分页；
- 表格：时间、QQ、B站 UID（可点开主页）、B站 昵称、等级/粉丝、状态、验证码、备注、操作；
- 操作：**通过 / 拒绝 / 拉黑 / 解除拉黑 / 删除记录**（危险操作二次确认）；
- 顶部：**插件连接状态**（最近同步时间、当前验证码、已拉取条数）与统计；
- 设置：站点名、群信息行、B站 规则（等级/粉丝/昵称关键词/一 UID 一 QQ/风控策略）、是否沿用插件规则、
  插件地址、**重置插件 token**、**修改管理员密码**；
- **导出 CSV**（带 BOM，Excel 直接打开不乱码）。

## HTTP 接口

| 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| GET | `/` | 无 | 申请页 |
| GET | `/admin` | 无（未登录显示登录页） | 管理后台 |
| GET | `/healthz` | 无 | 健康检查（版本 + 时间） |
| POST | `/api/apply` | 无（按 IP/QQ 限流） | 提交申请；支持 JSON 与表单编码（无 JS 时返回 HTML 结果页） |
| GET | `/api/apply/<ticket>` | 无 | 按 ticket 轮询自己的申请状态 |
| GET | `/api/questions` | 无 | 取一道题给申请页（**只回题干与提示，不回答案**） |
| GET | `/api/admin/questions` | 会话 | 读题库（站点题 + 插件题 + 开关与匹配设置） |
| POST | `/api/admin/questions` | 会话 + CSRF | `upsert`/`delete`/`toggle`/`clear`/`set_ask`/`set_common`/`set_mode`/`set_attempts` |
| GET | `/api/plugin/questions` | 插件 token | 插件反向拉取站点题库（`plugin` 模式下不会调用） |
| POST | `/api/admin/login` | 密码 | 登录（失败多次按 IP 锁定 60 秒） |
| GET | `/api/admin/applications` | 会话 | 列表（`status`/`q`/`offset`，每页 50） |
| POST | `/api/admin/decision` | 会话 + CSRF | `approve`/`reject`/`manual`/`block`/`unblock` |
| POST | `/api/admin/settings` | 会话 + CSRF | 保存站点设置（密码/token 留空表示不修改） |
| POST | `/api/admin/delete` | 会话 + CSRF | 删除记录 |
| GET | `/api/admin/export.csv` | 会话 | 导出 CSV |
| GET | `/api/admin/logout` | 会话 | 退出登录 |
| POST | `/api/plugin/sync` | 插件 token | 插件推送验证码/规则/绑定 |
| GET | `/api/plugin/applications` | 插件 token | 插件拉取"已通过未投递"名单 |
| POST | `/api/plugin/ack` | 插件 token | 插件确认已接收 |
| GET | `/api/plugin/ping` | 插件 token | 连通性检查 |

插件 token 优先用 `X-Review-Token` **请求头**（插件默认就是这么发的，URL 里不会出现密钥）；
为兼容也接受 `?token=` 查询参数或请求体 `token` 字段——但查询串会进浏览器历史与反代日志，不推荐。

## 数据与隐私

- 只保存：QQ 号、B站 UID、B站 公开昵称/等级/粉丝、备注、状态与原因、验证码、IP（用于限流与排查）、时间；
- **申请人看不到别人的信息**：轮询用的是随机 32 位十六进制 ticket，无法枚举；"UID 已被使用"的错误里**不包含占用者的 QQ 号**；
- UID 唯一性由数据库的**部分唯一索引**保证（只有 pending/approved 占用），被拒绝的人可以改 UID 再提交；
- 想清理数据：直接在后台删除记录，或停服后删除上面那个 `review-site.db`。

## 安全建议

已经做了这些（都有回归测试）：

- **不信任 X-Forwarded-For**：默认按 TCP 源地址统计限流与记录日志。`X-Forwarded-For` 是客户端可伪造的头，
  无条件信任它既能绕过节流、也能伪造成管理员 IP 去触发登录锁定；只有**确实放在反向代理后面**时才加
  `--trust-proxy`（此时取最右一项，即最靠近本站的代理写入的地址）。
- **登录双重限流**：单 IP 连续 5 次失败锁定 60 秒，另有**全局**阈值（5 分钟 60 次）挡住换 IP 的撞库。
- **改密码即踢线**：修改管理员密码会立刻作废其它所有会话（怀疑泄露时最有用），当前会话保留。
- **动态响应 `Cache-Control: no-store`**：验证码所在的结果页不会被浏览器或中间缓存留下来。
- **CSV 公式注入防护**：B站 昵称与备注是外部可控内容，导出时对 `= + - @` 开头的单元格加前导单引号，
  避免管理员用 Excel 打开时执行 `=cmd|...` 这类公式。
- **请求体要么读完、要么关连接**：避免 keep-alive 连接上残留字节被当成下一个请求（请求错位/走私）。
- **错误不外泄**：内部异常只回"错误编号"，细节只写进服务器端日志；日志本身会隐藏查询串（token 可能出现在 `?token=` 里）。
- **其它**：`nosniff` + `X-Frame-Options: DENY` + CSP + `Permissions-Policy`；HTTPS 场景自动加 `Secure` cookie 与 HSTS；
  会话 12 小时过期并定期清理；B站 查询缓存有上限；站点日志表只保留最近 2000 条。

部署时还要注意：

- 密码用 **PBKDF2-SHA256（20 万次迭代 + 随机盐）**存储，登录失败按 IP 锁定；会话是随机 token（`HttpOnly` + `SameSite=Lax`，12 小时），
  所有写操作还要校验 **CSRF**；
- 提交有体积上限（16 KB）与频率限制（每 IP 10 次 / 每 QQ 3 次 / 10 分钟，可在后台调整）；
- **对外暴露时请放在 HTTPS 反代后面**（下面有 nginx 例子）并加 `--trust-proxy`，务必改掉初始密码；`plugin_token` 建议直接用启动时生成的那串；
- 站点不需要也不应该暴露 `data/` 目录（它本来就不在静态白名单里）。

### systemd（Linux）

```ini
[Unit]
Description=Neko Court review site
After=network.target

[Service]
WorkingDirectory=/opt/neko-court
ExecStart=/usr/bin/python3 review-site/serve.py --host 127.0.0.1 --port 8787
Restart=always
User=www-data

[Install]
WantedBy=multi-user.target
```

### nginx 反代（HTTPS）

反代部署时记得给站点加 `--trust-proxy`，否则限流会把所有请求算在代理 IP 上：

```bash
python review-site/serve.py --host 127.0.0.1 --port 8787 --trust-proxy
```

```nginx
server {
    listen 443 ssl;
    server_name review.example.com;
    ssl_certificate     /etc/letsencrypt/live/review.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/review.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;  # 限流按真实 IP 统计
    }
}
```

Windows 上可以用 `pythonw.exe review-site/serve.py --host 0.0.0.0` 配合任务计划程序/NSSM 常驻。

## 自检

```bash
python selftest_review_site.py     # 224 项：接口、鉴权、CSRF、限流、核验分支、同步/拉取、模板结构、安全回归
node   selftest_review_site_ui.mjs # 74 项：纯函数边界、模板契约、渲染模拟、脚本语法
```

两个自检都**离线**：B站 接口被替换成假实现，自检自己起一个真实 HTTP 服务并真发请求。

## 网站打不开？按这个顺序查

在**运行 AstrBot 的那台机器**上跑仓库自带的诊断脚本，它会逐项给结论：

```bash
python tools/diagnose_site.py                     # 自动找 AstrBot 目录
python tools/diagnose_site.py --astrbot /opt/AstrBot --remote   # 从别的机器访问时加 --remote
```

（不在仓库里也没关系：这个脚本只用标准库，单独拷过去就能跑。）

手工排查的话，按出现频率：

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `/审核 网站` 说"未启用" | `web_site_enabled` 还是 `false`（默认就是关的），或插件版本 ≤ v1.1.6（那时没有内置站点） | 打开开关并重载插件；老版本请升级 |
| 只有本机能开，手机/别的电脑打不开 | `web_site_host = 127.0.0.1`（默认），只监听本机 | 改成 `0.0.0.0`，**并在防火墙放行端口**（Windows: `New-NetFirewallRule -Direction Inbound -Protocol TCP -LocalPort 8787 -Action Allow`；Linux: `ufw allow 8787/tcp`） |
| 浏览器"拒绝连接" | 端口上没人监听：插件没加载、开关没开、或站点启动失败 | 看 AstrBot 日志里 `内置审核网站已启动` / `内置审核网站启动失败`；后者通常是端口被占用，改 `web_site_port` |
| 打开了却是别的页面 | 记错了端口：审核网站是**独立端口**（默认 8787），不是 AstrBot 控制台端口 | 申请页 `http://IP:8787/`，后台 `http://IP:8787/admin` |
| 端口有人在听但页面报错 | 那个端口被别的程序占了 | 换 `web_site_port` |
| 改了密码却登不上 | 旧版本缓存了密码哈希（v1.2.0 已修） | 升级到 v1.2.0+，或重启插件 |

卡片里最快的一步：在机器人那台机器上访问 `http://127.0.0.1:8787/healthz`，返回 `{"ok": true, "version": ...}` 就说明站点本身没问题，问题在"怎么访问"（host/防火墙/IP）。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 后台显示"插件尚未连接" | 检查插件里的 `web_review_url` 是否是 AstrBot 能访问到的地址（同机用 `127.0.0.1`），以及 `web_review_token` 是否一致 |
| 网页显示"已通过但没验证码" | 插件还没同步（刚启动/网络不通），等一个轮询周期或点后台的"重新加载"；也可以先在 QQ 里 `/审核 网站 同步` |
| 申请老是"待人工" | B站 接口被风控。可以让站点与插件共用规则并设 `bili_on_error=pass`，或在后台手动通过 |
| 提示"UID 已被使用"但那是本人 | 后台**解绑**：QQ 里 `/审核 解绑 <UID>`，或删除旧记录 |
| 页面样式没生效 | `templates/` 缺失时后端会用内置极简页（功能仍在）；确认 `static/` 三个文件都在 |
| 端口打不开 | `--host 127.0.0.1` 只监听本机；要局域网/公网访问用 `--host 0.0.0.0`，并检查防火墙 |
