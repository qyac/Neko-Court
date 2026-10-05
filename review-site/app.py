"""审核网站的主程序：HTTP 路由 + 业务逻辑（只用标准库）。

角色与数据流：
- **申请人**（`/`）：提交 QQ + B站 UID → 站点自己核验 B站 → 通过则显示插件同步过来的当日验证码；
- **管理员**（`/admin`）：密码登录，看队列、手动通过/拒绝/拉黑、导出 CSV、改站点设置；
- **插件**（`/api/plugin/*`，共享 token）：主动出站同步（验证码/规则/绑定）与拉取"网页已通过"名单，
  再回 ack。站点永远不需要反向访问 AstrBot，所以插件在内网/NAT 后面也能用。

安全：
- 管理员密码用 PBKDF2-SHA256 加盐存储，登录失败有按 IP 的锁定；
- 会话是随机 token（HttpOnly + SameSite=Lax，12 小时过期），写操作还要校验 CSRF；
- 申请人拿到的 ticket 是随机 32 位十六进制，无法枚举别人的申请；
- 所有输出经过 HTML 转义；提交有体积上限、按 IP/QQ 的频率限制。
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import bili
import render
from store import STATUSES, Store

SITE_VERSION = "1.0.0"
MAX_BODY_BYTES = 16 * 1024
NOTE_MAX = 200
PAGE_SIZE = 50
SESSION_TTL = 12 * 3600
LOGIN_MAX_FAILURES = 5
LOGIN_LOCK_SECONDS = 60
RATE_WINDOW = 600.0
STATIC_FILES = {
    "style.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "form.js": "text/javascript; charset=utf-8",
    "favicon.svg": "image/svg+xml",
}

_QQ_RE = re.compile(r"^\d{5,12}$")
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
}


# --------------------------------------------------------------------------- 密码与会话


def hash_password(password: str, *, iterations: int = 200_000) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_hex, digest_hex = str(stored).split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


class RateLimiter:
    """滑动窗口限流（按 key 记时间戳）。"""

    def __init__(self, limit: int, window: float):
        self.limit = int(limit)
        self.window = float(window)
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, *, limit: int | None = None) -> bool:
        """记一次并返回是否仍然允许（False = 超限）。"""
        now = time.time()
        cap = int(limit if limit is not None else self.limit)
        with self._lock:
            hits = [item for item in self._hits.get(key, []) if now - item < self.window]
            if len(hits) >= cap:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            if len(self._hits) > 4096:  # 防内存膨胀
                for stale in [k for k, v in self._hits.items() if not v or now - v[-1] > self.window]:
                    self._hits.pop(stale, None)
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


class App:
    """把配置、存储、限流、会话集中在一个对象里，便于测试直接构造。"""

    def __init__(self, store: Store, *, admin_password_hash: str = ""):
        self.store = store
        self.admin_password_hash = admin_password_hash or str(store.get_setting("admin_password_hash") or "")
        self.sessions: dict[str, dict[str, Any]] = {}
        self._session_lock = threading.Lock()
        self.login_limiter = RateLimiter(LOGIN_MAX_FAILURES, LOGIN_LOCK_SECONDS)
        self.apply_limiter = RateLimiter(10, RATE_WINDOW)
        self.plugin_limiter = RateLimiter(120, 60.0)

    # ---------------------------------------------------------------- 设置与规则

    def settings(self) -> dict[str, Any]:
        return self.store.all_settings()

    def plugin_token(self) -> str:
        return str(self.store.get_setting("plugin_token") or "")

    def effective_rules(self, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        """判定规则：默认用站点自己的；开了 use_plugin_rules 且插件同步过规则则用插件的。"""
        data = settings or self.settings()
        own = {
            "bili_enabled": bool(data.get("bili_enabled", True)),
            "bili_min_level": int(data.get("bili_min_level") or 0),
            "bili_min_fans": int(data.get("bili_min_fans") or 0),
            "bili_name_keywords": list(data.get("bili_name_keywords") or []),
            "bili_unique": bool(data.get("bili_unique", True)),
            "bili_on_error": str(data.get("bili_on_error") or "manual"),
        }
        plugin_rules = data.get("plugin_rules") or {}
        if data.get("use_plugin_rules", True) and isinstance(plugin_rules, dict) and plugin_rules:
            merged = dict(own)
            for key in ("bili_min_level", "bili_min_fans", "bili_name_keywords", "bili_unique"):
                if key in plugin_rules:
                    merged[key] = plugin_rules[key]
            merged["source"] = "plugin"
            return merged
        own["source"] = "site"
        return own

    # ---------------------------------------------------------------- 会话

    def new_session(self, *, ip: str) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        with self._session_lock:
            self.sessions[token] = {"csrf": csrf, "created": time.time(), "ip": ip}
        return token, csrf

    def get_session(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        with self._session_lock:
            data = self.sessions.get(token)
            if not data:
                return None
            if time.time() - float(data.get("created") or 0) > SESSION_TTL:
                self.sessions.pop(token, None)
                return None
            return dict(data)

    def drop_session(self, token: str) -> None:
        with self._session_lock:
            self.sessions.pop(token, None)


# --------------------------------------------------------------------------- 处理器


class Handler(BaseHTTPRequestHandler):
    server_version = f"ReviewSite/{SITE_VERSION}"
    protocol_version = "HTTP/1.1"

    app: App  # 由 serve.py 注入（子类或实例属性）

    # ------------------------------------------------------------ 基础工具

    def log_message(self, fmt: str, *args: Any) -> None:  # 收敛默认日志
        if os.environ.get("REVIEW_SITE_QUIET"):
            return
        print(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}", flush=True)

    @property
    def store(self) -> Store:
        return self.app.store

    def settings(self) -> dict[str, Any]:
        """站点设置的便捷读取（渲染与业务逻辑都用它）。"""
        return self.app.settings()

    def _client_ip(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For") or ""
        if forwarded:
            return forwarded.split(",")[0].strip()[:45]
        return str(self.client_address[0])[:45]

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in _SECURITY_HEADERS.items():
            self.send_header(key, value)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, Any], extra: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", extra)

    def _html(self, status: int, text: str, extra: dict[str, str] | None = None) -> None:
        self._send(status, text.encode("utf-8"), "text/html; charset=utf-8", extra)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"ok": False, "error": message})

    def _body(self) -> dict[str, Any]:
        """读取请求体：支持 JSON 与表单编码。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        content_type = (self.headers.get("Content-Type") or "").lower()
        if "application/json" in content_type:
            try:
                data = json.loads(raw.decode("utf-8", "replace"))
            except ValueError as exc:
                raise ValueError(f"JSON 解析失败：{exc}") from exc
            if not isinstance(data, dict):
                raise ValueError("请求体必须是 JSON 对象")
            return data
        if "form-urlencoded" in content_type or not content_type:
            return {key: values[0] for key, values in urllib.parse.parse_qs(raw.decode("utf-8", "replace")).items()}
        if "multipart/form-data" in content_type:
            raise ValueError("不支持 multipart/form-data")
        return {}

    def _is_json_request(self) -> bool:
        return "application/json" in (self.headers.get("Content-Type") or "").lower()

    # ------------------------------------------------------------ 路由

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/":
                return self._page_apply()
            if path == "/admin":
                return self._page_admin()
            if path == "/healthz":
                return self._json(200, {"ok": True, "version": SITE_VERSION, "time": time.time()})
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path.startswith("/api/apply/"):
                return self._api_apply_status(path.rsplit("/", 1)[-1])
            if path == "/api/admin/applications":
                return self._api_admin_applications(query)
            if path == "/api/admin/settings":
                return self._api_admin_get_settings()
            if path == "/api/admin/export.csv":
                return self._api_admin_export()
            if path == "/api/admin/logout":
                return self._api_admin_logout()
            if path == "/api/plugin/applications":
                return self._api_plugin_applications(query)
            if path == "/api/plugin/ping":
                return self._api_plugin_ping(query)
            if path.startswith("/api/"):
                return self._error(404, "接口不存在")
            return self._html(404, render.render_error_page(self.settings(), title="页面不存在", message="检查地址是否正确。", status=404))
        except BrokenPipeError:
            return
        except Exception as exc:  # 兜底：不让异常打死连接
            self.log_message("处理 GET %s 出错：%s", self.path, exc)
            return self._error(500, f"服务器内部错误：{exc}")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/api/apply":
                return self._api_apply()
            if path == "/api/admin/login":
                return self._api_admin_login()
            if path == "/api/admin/decision":
                return self._api_admin_decision()
            if path == "/api/admin/settings":
                return self._api_admin_save_settings()
            if path == "/api/admin/delete":
                return self._api_admin_delete()
            if path == "/api/admin/logout":
                return self._api_admin_logout()
            if path == "/api/plugin/sync":
                return self._api_plugin_sync()
            if path == "/api/plugin/ack":
                return self._api_plugin_ack()
            if path.startswith("/api/"):
                return self._error(404, "接口不存在")
            return self._error(405, "不支持该请求")
        except ValueError as exc:
            return self._error(400, str(exc))
        except BrokenPipeError:
            return
        except Exception as exc:
            self.log_message("处理 POST %s 出错：%s", self.path, exc)
            return self._error(500, f"服务器内部错误：{exc}")

    # ------------------------------------------------------------ 页面

    def _page_apply(self) -> None:
        settings = self.settings()
        self._html(200, render.render_apply(settings))

    def _page_admin(self) -> None:
        settings = self.settings()
        session = self._session()
        if not self.app.admin_password_hash:
            self._html(
                200,
                render.render_login(
                    settings,
                    error_html='<div class="alert alert-error" role="alert">还没有设置管理员密码：'
                    "请用 <code>--admin-password</code> 启动一次，或用启动日志里打印的初始密码登录后修改。</div>",
                ),
            )
            return
        if session is None:
            return self._html(200, render.render_login(settings))
        items, total = self.store.list_applications(status="all", offset=0, limit=PAGE_SIZE)
        self._html(
            200,
            render.render_admin(
                settings,
                csrf=str(session["csrf"]),
                items=[self.store.admin_item(row) for row in items],
                total=total,
                counts=self.store.counts(),
            ),
            extra={"Set-Cookie": self._session_cookie(self._token(), max_age=SESSION_TTL)},
        )

    def _static(self, name: str) -> None:
        safe = os.path.basename(name)
        if safe != name or safe not in STATIC_FILES:
            return self._error(404, "资源不存在")
        path = render.BASE_DIR / "static" / safe
        if not path.is_file():
            return self._error(404, "资源不存在")
        self._send(200, path.read_bytes(), STATIC_FILES[safe], extra={"Cache-Control": "no-cache"})

    # ------------------------------------------------------------ 申请人接口

    def _validate_apply(self, payload: dict[str, Any]) -> tuple[str, str, str]:
        qq = str(payload.get("qq") or "").strip().replace(" ", "")
        # 容忍"QQ:12345"这类写法
        qq = qq.split(":")[-1].strip() if not qq.isdigit() else qq
        if not _QQ_RE.match(qq):
            raise ValueError("QQ 号看起来不对（应为 5~12 位数字）")
        uid = bili.extract_uid(str(payload.get("uid") or ""))
        if not uid:
            raise ValueError("没有识别到 B站 UID（可以填纯数字，或直接粘贴你的 B站主页链接）")
        note = str(payload.get("note") or "").strip()
        if len(note) > NOTE_MAX:
            raise ValueError(f"备注太长了（最多 {NOTE_MAX} 字）")
        return qq, uid, note

    def _apply_result(self, row: dict[str, Any]) -> dict[str, Any]:
        settings = self.settings()
        item = self.store.public_item(row)
        item.update(
            {
                "ok": True,
                "expire": str(settings.get("plugin_code_expire") or ""),
                "plugin_connected": bool(settings.get("plugin_synced_at")),
            }
        )
        return item

    def _api_apply(self) -> None:
        ip = self._client_ip()
        wants_json = self._is_json_request()
        try:
            payload = self._body()
            qq, uid, note = self._validate_apply(payload)

            if self.store.is_blocked(qq):
                return self._apply_refused("该 QQ 已被管理员拉黑，请联系管理员。", wants_json)

            settings = self.settings()
            per_ip = int(settings.get("apply_per_ip") or 10)
            per_qq = int(settings.get("apply_per_qq") or 3)
            if not self.app.apply_limiter.hit(f"ip:{ip}", limit=per_ip):
                return self._apply_refused("提交太频繁了，请过几分钟再试。", wants_json, status=429)
            if not self.app.apply_limiter.hit(f"qq:{qq}", limit=per_qq):
                return self._apply_refused("这个 QQ 短时间内提交太多次了，请稍后再试。", wants_json, status=429)

            rules = self.app.effective_rules(settings)
            taken = ""
            if rules.get("bili_unique", True):
                taken = self.store.uid_taken_by(uid, exclude_qq=qq)
                if not taken:
                    bindings = settings.get("plugin_bindings") or {}
                    if isinstance(bindings, dict):
                        bound_qq = str(bindings.get(uid) or "")
                        if bound_qq and bound_qq != qq:
                            taken = bound_qq
                if taken:
                    # 不把占用者的 QQ 号告诉申请人（避免泄露），只记日志给管理员看
                    self.store.log("apply_uid_taken", actor=f"ip:{ip}", detail=f"qq={qq} uid={uid} taken_by={taken}")
                    return self._apply_refused(
                        "这个 B站 UID 已经被其他申请用过了。如果这是你的账号，请联系管理员处理。",
                        wants_json,
                    )

            if not rules.get("bili_enabled", True):
                outcome = {"outcome": "manual", "reason": "本站未启用 B站 核验，等待管理员人工审核", "info": None, "unverified": False}
            else:
                outcome = bili.verify(uid, rules=rules, taken_by=taken, timeout=float(os.environ.get("REVIEW_BILI_TIMEOUT", 10.0)))
                if outcome["outcome"] == "approved" and rules.get("bili_unique", True) and not taken:
                    # 复核一次：防止并发提交抢占同一个 UID
                    taken = self.store.uid_taken_by(uid, exclude_qq=qq)
                    if taken:
                        outcome = {
                            "outcome": "rejected",
                            "reason": f"该 UID 已被 QQ {taken} 使用过",
                            "info": outcome.get("info"),
                            "unverified": False,
                        }

            info = outcome.get("info") or {}
            status = str(outcome["outcome"])
            code = ""
            if status == "approved":
                code = str(settings.get("plugin_code") or "")
            try:
                app_id = self.store.create_application(
                    qq=qq,
                    uid=uid,
                    status=status,
                    reason=str(outcome.get("reason") or ""),
                    uid_name=str(info.get("name") or ""),
                    level=int(info.get("level") or 0),
                    fans=int(info.get("fans") or 0),
                    note=note,
                    code=code,
                    source="auto",
                    ip=ip,
                )
            except sqlite3.IntegrityError:
                # uid 部分唯一索引冲突：同一 UID 已有 pending/approved 记录（并发提交）
                return self._apply_refused("这个 B站 UID 已经提交过申请了，请不要重复提交。", wants_json)

            row = self.store.get_application(app_id) or {}
            self.store.log("apply", actor=f"ip:{ip}", detail=f"qq={qq} uid={uid} -> {status}")
            result = self._apply_result(row)
            if not wants_json:
                return self._html(200, self._render_result_page(result))
            return self._json(200, result)
        except ValueError as exc:
            if wants_json:
                return self._error(400, str(exc))
            return self._html(400, render.render_error_page(self.settings(), title="提交有误", message=str(exc)), )

    def _apply_refused(self, message: str, wants_json: bool, status: int = 400) -> None:
        if wants_json:
            return self._error(status, message)
        return self._html(status, render.render_error_page(self.settings(), title="无法提交", message=message))

    def _render_result_page(self, result: dict[str, Any]) -> str:
        """无 JS 时的结果页（也让 curl/脚本提交后能看到结论）。"""
        status = str(result.get("status") or "")
        label = {"approved": "已通过", "rejected": "未通过", "manual": "待人工审核", "pending": "待核验"}.get(status, status)
        rows = [
            f"<p><strong>状态：{render.esc(label)}</strong></p>",
            f"<p>QQ：{render.esc(result.get('qq'))} ｜ B站 UID：{render.esc(result.get('uid'))}"
            f" ｜ 昵称：{render.esc(result.get('uid_name') or '—')}</p>",
        ]
        if result.get("reason"):
            rows.append(f"<p>说明：{render.esc(result['reason'])}</p>")
        if status == "approved" and result.get("code"):
            rows.append(f"<p class='code-big'>{render.esc(result['code'])}</p>")
            rows.append(f"<p>有效期至 {render.esc(result.get('expire') or '—')}</p>")
        elif status == "approved":
            rows.append("<p>已通过核验，但插件还没同步验证码，请联系管理员获取。</p>")
        link = f"/api/apply/{render.esc(result.get('ticket'))}"
        body = (
            "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>审核结果</title><link rel='stylesheet' href='/static/style.css'></head>"
            f"<body class='page'><main class='card'><h1>审核结果</h1>{''.join(rows)}"
            f"<p class='muted'>这个页面可以随时刷新查看最新状态：<a href='{link}'>{link}</a></p>"
            "<p><a class='btn' href='/'>再提交一次</a></p></main></body></html>"
        )
        return body

    def _api_apply_status(self, ticket: str) -> None:
        row = self.store.find_by_ticket(ticket)
        if row is None:
            return self._error(404, "编号不存在")
        return self._json(200, self._apply_result(row))

    # ------------------------------------------------------------ 管理接口

    def _token(self) -> str:
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "review_admin":
                return value
        return ""

    def _session_cookie(self, token: str, *, max_age: int) -> str:
        return f"review_admin={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={int(max_age)}"

    def _session(self) -> dict[str, Any] | None:
        return self.app.get_session(self._token())

    def _require_admin(self, *, csrf: str | None = None) -> dict[str, Any] | None:
        """校验登录；`csrf` 传字符串时**必须**匹配（传空串也算校验失败，不能靠省略字段绕过）。"""
        session = self._session()
        if session is None:
            self._error(401, "未登录")
            return None
        if csrf is not None:
            provided = str(csrf)
            expected = str(session.get("csrf") or "")
            if not provided or not expected or not hmac.compare_digest(expected, provided):
                self._error(403, "CSRF 校验失败")
                return None
        return session

    def _api_admin_login(self) -> None:
        ip = self._client_ip()
        if not self.app.login_limiter.hit(f"login:{ip}"):
            return self._error(429, f"密码错误次数过多，请 {LOGIN_LOCK_SECONDS} 秒后再试")
        payload = self._body()
        password = str(payload.get("password") or "")
        stored = self.app.admin_password_hash or str(self.store.get_setting("admin_password_hash") or "")
        if not stored or not verify_password(password, stored):
            self.store.log("login_failed", actor=f"ip:{ip}", detail="")
            if not self._is_json_request():
                settings = self.settings()
                return self._html(
                    401,
                    render.render_login(
                        settings, error_html='<div class="alert alert-error" role="alert">密码不正确。</div>'
                    ),
                )
            return self._error(401, "密码不正确")
        self.app.login_limiter.reset(f"login:{ip}")
        token, _csrf = self.app.new_session(ip=ip)
        self.store.log("login_ok", actor=f"ip:{ip}", detail="")
        if not self._is_json_request():
            return self._html(
                303,
                "",
                extra={"Location": "/admin", "Set-Cookie": self._session_cookie(token, max_age=SESSION_TTL)},
            )
        return self._json(
            200,
            {"ok": True},
            extra={"Set-Cookie": self._session_cookie(token, max_age=SESSION_TTL)},
        )

    def _api_admin_logout(self) -> None:
        self.app.drop_session(self._token())
        if not self._is_json_request():
            return self._html(303, "", extra={"Location": "/admin", "Set-Cookie": "review_admin=; Path=/; Max-Age=0"})
        return self._json(200, {"ok": True}, extra={"Set-Cookie": "review_admin=; Path=/; Max-Age=0"})

    def _api_admin_applications(self, query: dict[str, list[str]]) -> None:
        if self._require_admin() is None:
            return
        status = (query.get("status") or ["all"])[0]
        if status not in STATUSES and status != "all":
            status = "all"
        keyword = (query.get("q") or [""])[0][:64]
        try:
            offset = max(0, int((query.get("offset") or ["0"])[0]))
        except ValueError:
            offset = 0
        rows, total = self.store.list_applications(status=status, query=keyword, offset=offset, limit=PAGE_SIZE)
        return self._json(
            200,
            {
                "ok": True,
                "total": total,
                "offset": offset,
                "page_size": PAGE_SIZE,
                "items": [self.store.admin_item(row) for row in rows],
                "counts": self.store.counts(),
            },
        )

    def _api_admin_get_settings(self) -> None:
        if self._require_admin() is None:
            return
        settings = self.settings()
        return self._json(
            200,
            {
                "ok": True,
                "settings": render.public_settings(settings),
                "plugin": {
                    "connected": bool(settings.get("plugin_synced_at")),
                    "last_sync_at": float(settings.get("plugin_synced_at") or 0),
                    "code": str(settings.get("plugin_code") or ""),
                    "expire": str(settings.get("plugin_code_expire") or ""),
                    "groups": list(settings.get("plugin_groups") or []),
                    "rules": self.app.effective_rules(settings),
                    "pull_cursor": str(settings.get("plugin_pull_cursor") or ""),
                    "pulled": int(settings.get("plugin_pulled") or 0),
                },
                "stats": self.store.counts(),
            },
        )

    def _api_admin_save_settings(self) -> None:
        payload = self._body()
        session = self._require_admin(csrf=str(payload.get("csrf") or ""))
        if session is None:
            return
        allowed_text = ("site_name", "site_subtitle", "group_line", "rules_line", "plugin_url")
        for key in allowed_text:
            if key in payload:
                self.store.set_setting(key, str(payload.get(key) or "")[:200])
        if "bili_min_level" in payload:
            self.store.set_setting("bili_min_level", max(0, min(6, int(payload.get("bili_min_level") or 0))))
        if "bili_min_fans" in payload:
            self.store.set_setting("bili_min_fans", max(0, int(payload.get("bili_min_fans") or 0)))
        if "bili_name_keywords" in payload:
            raw = payload.get("bili_name_keywords")
            if isinstance(raw, str):
                raw = [item for item in re.split(r"[\s,，;；、]+", raw) if item]
            keywords = [str(item).strip()[:40] for item in (raw or []) if str(item).strip()]
            self.store.set_setting("bili_name_keywords", keywords[:20])
        if "bili_unique" in payload:
            self.store.set_setting("bili_unique", bool(payload.get("bili_unique")))
        if "bili_enabled" in payload:
            self.store.set_setting("bili_enabled", bool(payload.get("bili_enabled")))
        if "use_plugin_rules" in payload:
            self.store.set_setting("use_plugin_rules", bool(payload.get("use_plugin_rules")))
        if "bili_on_error" in payload:
            value = str(payload.get("bili_on_error") or "manual").lower()
            self.store.set_setting("bili_on_error", value if value in ("reject", "manual", "pass") else "manual")
        if payload.get("admin_password"):
            password = str(payload["admin_password"])
            if len(password) < 8:
                return self._error(400, "管理员密码至少 8 位")
            self.store.set_setting("admin_password_hash", hash_password(password))
            self.app.admin_password_hash = str(self.store.get_setting("admin_password_hash"))
            self.store.log("password_changed", actor="admin", detail="")
        if payload.get("plugin_token"):
            token = str(payload["plugin_token"]).strip()
            if len(token) < 16:
                return self._error(400, "插件 token 至少 16 位（建议直接用启动时生成的那个）")
            self.store.set_setting("plugin_token", token)
            self.store.log("plugin_token_changed", actor="admin", detail="")
        self.store.log("settings_saved", actor="admin", detail="")
        settings = self.settings()
        return self._json(200, {"ok": True, "settings": render.public_settings(settings)})

    def _api_admin_decision(self) -> None:
        payload = self._body()
        if self._require_admin(csrf=str(payload.get("csrf") or "")) is None:
            return
        try:
            app_id = int(payload.get("id") or 0)
        except (TypeError, ValueError):
            return self._error(400, "id 不合法")
        action = str(payload.get("action") or "")
        note = str(payload.get("note") or "")[:NOTE_MAX]
        row = self.store.get_application(app_id)
        if row is None:
            return self._error(404, "记录不存在")
        settings = self.settings()
        if action == "approve":
            code = str(settings.get("plugin_code") or "")
            reason = "管理员手动通过" + ("（插件未同步验证码，请让插件同步后再补发）" if not code else "")
            self.store.decide(app_id, "approved", reason=reason, actor="admin", note=note)
            if code:
                with self.store._connect() as conn:  # noqa: SLF001 - 同一模块内的简单更新
                    conn.execute("UPDATE applications SET code = ? WHERE id = ?", (code, app_id))
                    conn.commit()
        elif action == "reject":
            self.store.decide(app_id, "rejected", reason="管理员手动拒绝", actor="admin", note=note)
        elif action == "manual":
            self.store.decide(app_id, "manual", reason="管理员标记为待人工", actor="admin", note=note)
        elif action == "block":
            self.store.block(str(row["qq"]), note=note)
            self.store.decide(app_id, "blocked", reason="已拉黑该 QQ", actor="admin", note=note)
        elif action == "unblock":
            self.store.unblock(str(row["qq"]))
            self.store.decide(app_id, "manual", reason="已解除拉黑，等待人工处理", actor="admin", note=note)
        else:
            return self._error(400, "未知操作")
        self.store.log(f"decision:{action}", actor="admin", detail=f"id={app_id} qq={row.get('qq')}")
        updated = self.store.get_application(app_id) or {}
        return self._json(200, {"ok": True, "item": self.store.admin_item(updated), "counts": self.store.counts()})

    def _api_admin_delete(self) -> None:
        payload = self._body()
        if self._require_admin(csrf=str(payload.get("csrf") or "")) is None:
            return
        try:
            app_id = int(payload.get("id") or 0)
        except (TypeError, ValueError):
            return self._error(400, "id 不合法")
        if not self.store.delete_application(app_id):
            return self._error(404, "记录不存在")
        self.store.log("delete", actor="admin", detail=f"id={app_id}")
        return self._json(200, {"ok": True, "counts": self.store.counts()})

    def _api_admin_export(self) -> None:
        if self._require_admin() is None:
            return
        rows, _total = self.store.list_applications(status="all", offset=0, limit=100000)
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow(["id", "ticket", "qq", "uid", "bili_name", "level", "fans", "status", "reason", "code", "source", "created_at", "decided_at", "delivered", "note"])
        for row in rows:
            writer.writerow(
                [
                    row.get("id"),
                    row.get("ticket"),
                    row.get("qq"),
                    row.get("uid"),
                    row.get("uid_name"),
                    row.get("level"),
                    row.get("fans"),
                    row.get("status"),
                    row.get("reason"),
                    row.get("code"),
                    row.get("source"),
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(row.get("created_at") or 0))),
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(row["decided_at"]))) if row.get("decided_at") else "",
                    row.get("delivered"),
                    row.get("note"),
                ]
            )
        body = ("\ufeff" + buffer.getvalue()).encode("utf-8")  # BOM 让 Excel 正确识别 UTF-8
        self._send(
            200,
            body,
            "text/csv; charset=utf-8",
            extra={"Content-Disposition": 'attachment; filename="review-applications.csv"'},
        )

    # ------------------------------------------------------------ 插件接口

    def _plugin_ok(self, payload: dict[str, Any] | None = None) -> bool:
        token = str(self.app.plugin_token() or "")
        if not token:
            self._error(503, "站点还没有生成插件 token")
            return False
        header = str(self.headers.get("X-Review-Token") or "")
        query_token = ""
        parsed = urllib.parse.urlparse(self.path)
        query_token = (urllib.parse.parse_qs(parsed.query).get("token") or [""])[0]
        body_token = ""
        if payload:
            body_token = str(payload.get("token") or "")
        provided = header or body_token or query_token
        if not provided or not hmac.compare_digest(provided, token):
            self._error(401, "插件 token 不正确")
            return False
        return True

    def _api_plugin_ping(self, query: dict[str, list[str]]) -> None:
        if not self._plugin_ok():
            return
        return self._json(200, {"ok": True, "site_time": time.time(), "version": SITE_VERSION})

    def _api_plugin_sync(self) -> None:
        payload = self._body()
        if not self._plugin_ok(payload):
            return
        if not self.app.plugin_limiter.hit(f"plugin:{self._client_ip()}"):
            return self._error(429, "同步过于频繁")
        settings_changed: list[str] = []
        if "code" in payload:
            self.store.set_setting("plugin_code", str(payload.get("code") or "")[:64])
            settings_changed.append("code")
        if "code_expire" in payload:
            self.store.set_setting("plugin_code_expire", str(payload.get("code_expire") or "")[:64])
        if "groups" in payload:
            groups = [str(item)[:20] for item in (payload.get("groups") or [])][:50]
            self.store.set_setting("plugin_groups", groups)
        if "bili" in payload and isinstance(payload.get("bili"), dict):
            self.store.set_setting("plugin_rules", payload["bili"])
        if "bindings" in payload and isinstance(payload.get("bindings"), dict):
            bindings = {str(k)[:20]: str(v)[:20] for k, v in list(payload["bindings"].items())[:5000]}
            self.store.set_setting("plugin_bindings", bindings)
        if "stats" in payload and isinstance(payload.get("stats"), dict):
            self.store.set_setting("plugin_stats", payload["stats"])
        self.store.set_setting("plugin_synced_at", time.time())
        self.store.set_setting("plugin_pull_cursor", str(payload.get("cursor") or self.store.get_setting("plugin_pull_cursor") or ""))
        if payload.get("pulled") is not None:
            try:
                self.store.set_setting("plugin_pulled", int(payload["pulled"]))
            except (TypeError, ValueError):
                pass
        # 插件换了验证码：把页面上已通过记录的验证码一起刷新，避免网页显示旧码
        code = str(payload.get("code") or "")
        if code and "code" in settings_changed:
            with self.store._connect() as conn:  # noqa: SLF001
                conn.execute("UPDATE applications SET code = ? WHERE status = 'approved'", (code,))
                conn.commit()
        pending = len(self.store.pull_approved(limit=200))
        self.store.log("plugin_sync", actor=f"ip:{self._client_ip()}", detail=f"code={code} pending={pending}")
        return self._json(200, {"ok": True, "site_time": time.time(), "pending_deliveries": pending})

    def _api_plugin_applications(self, query: dict[str, list[str]]) -> None:
        if not self._plugin_ok():
            return
        status = (query.get("status") or ["approved"])[0]
        if status != "approved":
            return self._error(400, "目前只支持 status=approved")
        try:
            limit = max(1, min(200, int((query.get("limit") or ["50"])[0])))
        except ValueError:
            limit = 50
        rows = self.store.pull_approved(limit=limit)
        return self._json(
            200,
            {
                "ok": True,
                "count": len(rows),
                "items": [
                    {
                        "id": int(row["id"]),
                        "qq": str(row["qq"]),
                        "uid": str(row["uid"]),
                        "uid_name": str(row["uid_name"] or ""),
                        "code": str(row["code"] or ""),
                        "status": str(row["status"]),
                        "decided_at": row.get("decided_at"),
                        "created_at": row.get("created_at"),
                    }
                    for row in rows
                ],
            },
        )

    def _api_plugin_ack(self) -> None:
        payload = self._body()
        if not self._plugin_ok(payload):
            return
        ids = payload.get("ids")
        if not isinstance(ids, list):
            return self._error(400, "ids 必须是数组")
        acked = self.store.ack_delivered([int(item) for item in ids if str(item).isdigit()])
        self.store.log("plugin_ack", actor=f"ip:{self._client_ip()}", detail=f"acked={acked}")
        return self._json(200, {"ok": True, "acked": acked})


def make_server(
    *,
    host: str,
    port: int,
    store: Store,
    admin_password_hash: str = "",
    handler_cls: type[Handler] | None = None,
) -> ThreadingHTTPServer:
    """构造服务器（测试里可以直接用它起在 127.0.0.1:0）。"""
    app = App(store, admin_password_hash=admin_password_hash)
    cls = handler_cls or Handler

    class BoundHandler(cls):  # type: ignore[misc, valid-type]
        pass

    BoundHandler.app = app
    server = ThreadingHTTPServer((host, port), BoundHandler)
    server.daemon_threads = True
    server.app = app  # type: ignore[attr-defined]
    return server
