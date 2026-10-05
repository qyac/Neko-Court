"""页面渲染：加载 templates/*.html，用 string.Template 填充已转义的片段。

安全约定：
- 所有用户可控内容（QQ/UID/昵称/备注/原因/日志）都必须经过 `esc()`；
- 传进模板的 `*_html` 参数都是**已转义好的片段**，模板原样输出；
- `initial_json` 里的 `<` 会被转成 `\\u003c`，防止数据里出现 `</script>` 截断脚本块；
- 模板文件缺失时退回到内置的极简页面，保证站点不会因为缺少模板而白屏。
"""

from __future__ import annotations

import html
import json
import time
from pathlib import Path
from string import Template
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "templates"

_TEMPLATE_CACHE: dict[str, tuple[float, str]] = {}


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _load(name: str) -> str | None:
    path = TEMPLATE_DIR / name
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    cached = _TEMPLATE_CACHE.get(name)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    _TEMPLATE_CACHE[name] = (mtime, text)
    return text


def _fill(text: str, mapping: dict[str, str]) -> str:
    return Template(text).safe_substitute(mapping)


def template_exists(name: str) -> bool:
    return (TEMPLATE_DIR / name).is_file()


def _json_block(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")


def _row(label: str, value: str) -> str:
    return f'<div class="kv"><span class="kv-k">{esc(label)}</span><span class="kv-v">{value}</span></div>'


def plugin_status_html(settings: dict[str, Any]) -> str:
    synced_at = float(settings.get("plugin_synced_at") or 0)
    code = str(settings.get("plugin_code") or "")
    if not synced_at:
        return (
            '<div class="alert alert-warn">'
            "<strong>插件尚未连接。</strong>网页可以正常收单与核验，但不会显示验证码。"
            "请在 AstrBot 插件配置里填好 <code>web_review_url</code> 与 <code>web_review_token</code>，"
            "插件会在一个轮询周期内自动同步。"
            "</div>"
        )
    age = max(0, int(time.time() - synced_at))
    state = "alert-ok" if age < 180 else "alert-warn"
    lines = [
        _row("最近同步", f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(synced_at))}（{age} 秒前）"),
        _row("当前验证码", f"<code>{esc(code) or '（未同步）'}</code>"),
        _row("验证码有效期", esc(settings.get("plugin_code_expire") or "—")),
        _row("审核群", esc("、".join(settings.get("plugin_groups") or []) or "—")),
    ]
    return f'<div class="alert {state}"><strong>插件已连接</strong>{"".join(lines)}</div>'


def stats_html(counts: dict[str, Any]) -> str:
    items = [
        ("总数", counts.get("total", 0)),
        ("待审核", counts.get("pending", 0)),
        ("已通过", counts.get("approved", 0)),
        ("未通过", counts.get("rejected", 0)),
        ("待人工", counts.get("manual", 0)),
        ("拉黑名单", counts.get("blocked_list", 0)),
    ]
    cells = "".join(
        f'<div class="stat"><span class="stat-n">{esc(value)}</span><span class="stat-k">{esc(label)}</span></div>'
        for label, value in items
    )
    return f'<div class="stats">{cells}</div>'


_FALLBACK_APPLY = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$site_name</title></head>
<body>
<h1>$site_name</h1>
<p>$site_subtitle</p>
$notice_html
<form method="post" action="./api/apply">
  <p><label>QQ 号 <input name="qq" inputmode="numeric" required></label></p>
  <p><label>B站 UID <input name="uid" required></label></p>
  <p><label>备注 <input name="note"></label></p>
  <p><button type="submit">提交审核</button></p>
</form>
<p>$group_line</p><p>$rules_line</p>
<p>$footer_html</p>
</body></html>"""

_FALLBACK_LOGIN = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$site_name · 管理登录</title></head>
<body>
<h1>$site_name · 管理登录</h1>
$error_html
<form method="post" action="./api/admin/login">
  <p><label>管理员密码 <input type="password" name="password" required></label></p>
  <p><button type="submit">登录</button></p>
</form>
<p>$footer_html</p>
</body></html>"""

_FALLBACK_ADMIN = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$site_name · 管理后台</title></head>
<body>
<h1>$site_name · 管理后台</h1>
$plugin_status_html
$stats_html
<p><a href="$logout_url">退出登录</a></p>
<script type="application/json" id="initial-data">$initial_json</script>
<pre id="fallback-queue"></pre>
<script>
var data = JSON.parse(document.getElementById("initial-data").textContent);
document.getElementById("fallback-queue").textContent =
  (data.items || []).map(function (i) { return i.id + " " + i.qq + " " + i.uid + " " + i.status; }).join("\\n");
</script>
<p>$footer_html</p>
</body></html>"""


def _template(name: str, fallback: str) -> str:
    return _load(name) or fallback


def default_footer() -> str:
    return (
        '<span class="footer-note">本站只保存你提交的 QQ 号与 B站 UID，用于入群审核；'
        "核验通过后请把验证码发给群里的机器人。</span>"
    )


def render_apply(settings: dict[str, Any], *, notice_html: str = "") -> str:
    rules = settings.get("rules_line") or _auto_rules_line(settings)
    return _fill(
        _template("apply.html", _FALLBACK_APPLY),
        {
            "site_name": esc(settings.get("site_name") or "入群审核"),
            "site_subtitle": esc(settings.get("site_subtitle") or ""),
            "group_line": esc(settings.get("group_line") or ""),
            "rules_line": esc(rules),
            "notice_html": notice_html,
            "footer_html": default_footer(),
        },
    )


def _auto_rules_line(settings: dict[str, Any]) -> str:
    if not settings.get("bili_enabled", True):
        return "本站当前不校验 B站 账号。"
    parts = []
    if int(settings.get("bili_min_level") or 0):
        parts.append(f"B站等级 ≥ {int(settings['bili_min_level'])}")
    if int(settings.get("bili_min_fans") or 0):
        parts.append(f"粉丝数 ≥ {int(settings['bili_min_fans'])}")
    keywords = [str(item) for item in (settings.get("bili_name_keywords") or []) if str(item).strip()]
    if keywords:
        parts.append(f"昵称包含 {'、'.join(keywords)}")
    if settings.get("bili_unique", True):
        parts.append("一个 B站 UID 只能绑一个 QQ")
    return "核验要求：" + "；".join(parts) if parts else "核验要求：B站账号真实存在"


def render_login(settings: dict[str, Any], *, error_html: str = "") -> str:
    return _fill(
        _template("login.html", _FALLBACK_LOGIN),
        {
            "site_name": esc(settings.get("site_name") or "入群审核"),
            "error_html": error_html,
            "footer_html": default_footer(),
        },
    )


def render_admin(
    settings: dict[str, Any],
    *,
    csrf: str,
    items: list[dict[str, Any]],
    total: int,
    counts: dict[str, Any],
) -> str:
    initial = {
        "items": items,
        "total": total,
        "counts": counts,
        "csrf": csrf,
        "settings": public_settings(settings),
    }
    return _fill(
        _template("admin.html", _FALLBACK_ADMIN),
        {
            "site_name": esc(settings.get("site_name") or "入群审核"),
            "plugin_status_html": plugin_status_html(settings),
            "stats_html": stats_html(counts),
            "csrf": esc(csrf),
            "logout_url": "./api/admin/logout",
            "initial_json": _json_block(initial),
            "footer_html": default_footer(),
        },
    )


def public_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """给前端的设置：绝不包含密码哈希与插件 token。"""
    return {
        "site_name": settings.get("site_name") or "",
        "site_subtitle": settings.get("site_subtitle") or "",
        "group_line": settings.get("group_line") or "",
        "rules_line": settings.get("rules_line") or "",
        "bili_enabled": bool(settings.get("bili_enabled", True)),
        "bili_min_level": int(settings.get("bili_min_level") or 0),
        "bili_min_fans": int(settings.get("bili_min_fans") or 0),
        "bili_name_keywords": list(settings.get("bili_name_keywords") or []),
        "bili_unique": bool(settings.get("bili_unique", True)),
        "bili_on_error": str(settings.get("bili_on_error") or "manual"),
        "use_plugin_rules": bool(settings.get("use_plugin_rules", True)),
        "plugin_url": settings.get("plugin_url") or "",
        "plugin_token": "",  # 永不回显
        "apply_per_ip": int(settings.get("apply_per_ip") or 10),
        "apply_per_qq": int(settings.get("apply_per_qq") or 3),
    }


def render_error_page(settings: dict[str, Any], *, title: str, message: str, status: int = 404) -> str:
    body = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<link rel="stylesheet" href="/static/style.css">
</head><body class="page">
<main class="card"><h1>{esc(title)}</h1><p class="muted">{esc(message)}</p>
<p><a class="btn" href="/">返回申请页</a></p></main>
</body></html>"""
    return body
