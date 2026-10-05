#!/usr/bin/env python3
"""审核网站的自检（离线，不联网）。

跑法：
    python selftest_review_site.py

做法：
- 在 127.0.0.1 的随机端口上真起一个服务器（ThreadingHTTPServer），用 urllib 真发请求；
- B站 接口用假的 `bili._fetch_json` 替换，所以不依赖外网，也不会打到 B站；
- 数据放在工作区 `.selftest_data/` 下（沙箱不保证系统临时目录可写）。
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent
SITE_DIR = WORKSPACE / "review-site"
sys.path.insert(0, str(SITE_DIR))

import app as app_module  # noqa: E402
import bili  # noqa: E402
import render  # noqa: E402
from store import Store  # noqa: E402

PASSED = 0
FAILED: list[str] = []
DATA_DIR = WORKSPACE / ".selftest_data" / "review-site"


def check(condition, label):
    global PASSED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL  {label}")


def card(name="测试UP", level=3, fans=100, uid="12345678", code=0, message="OK"):
    if code != 0:
        return {"code": code, "message": message}
    return {
        "code": 0,
        "message": "OK",
        "data": {
            "card": {"mid": str(uid), "name": name, "fans": fans, "level_info": {"current_level": level}, "sign": "签名"},
            "follower": fans,
        },
    }


class Client:
    """带 cookie/token 的极简 HTTP 客户端。"""

    def __init__(self, base: str):
        self.base = base
        self.cookie = ""

    def __call__(self, path, *, method="GET", payload=None, token="", form=None, cookie=None):
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            content_type = "application/x-www-form-urlencoded"
        elif payload is not None:
            data = json.dumps(payload).encode()
            content_type = "application/json"
        else:
            content_type = ""
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if content_type:
            request.add_header("Content-Type", content_type)
        if token:
            request.add_header("X-Review-Token", token)
        jar = self.cookie if cookie is None else cookie
        if jar:
            request.add_header("Cookie", jar)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = response.read().decode("utf-8", "replace")
                if response.headers.get("Set-Cookie"):
                    self.cookie = str(response.headers["Set-Cookie"]).split(";")[0]
                return response.status, body, response.headers
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.headers.get("Set-Cookie"):
                self.cookie = str(exc.headers["Set-Cookie"]).split(";")[0]
            return exc.code, body, exc.headers

    def json(self, path, **kwargs):
        status, body, headers = self(path, **kwargs)
        try:
            return status, json.loads(body), headers
        except ValueError:
            return status, {"_raw": body}, headers


def main() -> int:
    global PASSED
    if DATA_DIR.exists():
        shutil.rmtree(DATA_DIR, ignore_errors=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    store = Store(DATA_DIR / "test.db")
    store.set_setting("plugin_token", "test-token-0123456789abcdef")
    store.set_setting("site_name", "自检审核站")
    store.set_setting("apply_per_ip", 200)  # 自检要打很多次，IP 额度单独验证
    store.set_setting("group_line", "QQ 群 123456")
    server = app_module.make_server(
        host="127.0.0.1",
        port=0,
        store=store,
        admin_password_hash=app_module.hash_password("pw-12345678"),
    )
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    http = Client(f"http://127.0.0.1:{port}")

    # 假 B站：默认返回一个正常账号，按需改
    state = {"payload": card(), "calls": []}

    def fake_fetch(url, timeout, referer):
        state["calls"].append(url)
        payload = state["payload"]
        if isinstance(payload, Exception):
            raise payload
        return payload

    bili._fetch_json = fake_fetch
    bili.RETRY_DELAY = 0
    bili.clear_cache()

    try:
        print("[1] 站点启动与页面")
        status, body, _ = http("/")
        check(status == 200 and "自检审核站" in body, "GET / 渲染申请页")
        check("QQ 群 123456" in body, "页面显示群信息行")
        status, body, _ = http("/healthz")
        check(status == 200 and json.loads(body)["ok"] is True, "GET /healthz 正常")
        status, body, _ = http("/static/style.css")
        check(status == 200 and "text/css" in str(_), "静态样式表可访问")
        status, body, _ = http("/static/../app.py")
        check(status == 404, "拒绝路径穿越读取源码")
        status, body, _ = http("/api/nope")
        check(status == 404, "未知接口返回 404 JSON")
        status, body, _ = http("/nope")
        check(status == 404 and "<html" in body.lower(), "未知页面返回 404 HTML")
        status, body, _ = http("/", method="POST")
        check(status == 405, "不支持的 POST 页面返回 405")

        print("\n[2] 申请人提交与 B站 核验")
        state["payload"] = card(name="小明", level=4, fans=520)
        status, data, _ = http.json("/api/apply", method="POST", payload={"qq": "12345678", "uid": "12345678"})
        check(status == 200 and data["ok"] is True, f"提交成功（{status}）")
        check(data["status"] == "approved", "核验通过 -> approved")
        check(data["uid_name"] == "小明", "回填 B站 昵称")
        check(data["code"] == "", "插件未同步时不返回验证码")
        check(data["plugin_connected"] is False, "标记插件未连接")
        ticket = data["ticket"]
        status, data, _ = http.json(f"/api/apply/{ticket}")
        check(status == 200 and data["ticket"] == ticket, "按 ticket 轮询状态")
        status, data, _ = http.json("/api/apply/" + "f" * 32)
        check(status == 404, "不存在的 ticket 返回 404")

        state["payload"] = card(code=-404, message="啥都木有")
        bili.clear_cache()
        status, data, _ = http.json("/api/apply", method="POST", payload={"qq": "12345001", "uid": "9999999999"})
        check(status == 200 and data["status"] == "rejected" and "不存在" in data["reason"], "账号不存在 -> rejected")

        state["payload"] = card(name="低等级", level=1, fans=999)
        bili.clear_cache()
        store.set_setting("bili_min_level", 3)
        status, data, _ = http.json("/api/apply", method="POST", payload={"qq": "12345002", "uid": "11112222"})
        check(data["status"] == "rejected" and "等级" in data["reason"], "等级不足 -> rejected")
        store.set_setting("bili_min_level", 0)

        state["payload"] = card(name="没粉丝", level=6, fans=9)
        bili.clear_cache()
        store.set_setting("bili_min_fans", 100)
        status, data, _ = http.json("/api/apply", method="POST", payload={"qq": "12345003", "uid": "33334444"})
        check(data["status"] == "rejected" and "粉丝" in data["reason"], "粉丝不足 -> rejected")
        store.set_setting("bili_min_fans", 0)

        state["payload"] = card(name="狗子", level=6, fans=100)
        bili.clear_cache()
        store.set_setting("bili_name_keywords", ["猫"])
        status, data, _ = http.json("/api/apply", method="POST", payload={"qq": "12345004", "uid": "55556666"})
        check(data["status"] == "rejected" and "昵称" in data["reason"], "昵称不含关键词 -> rejected")
        store.set_setting("bili_name_keywords", [])

        state["payload"] = card(code=-799, message="请求过于频繁")
        bili.clear_cache()
        status, data, _ = http.json("/api/apply", method="POST", payload={"qq": "12345005", "uid": "77778888"})
        check(data["status"] == "manual" and "人工" in data["reason"], "风控默认转人工（manual）")
        store.set_setting("bili_on_error", "pass")
        bili.clear_cache()
        status, data, _ = http.json("/api/apply", method="POST", payload={"qq": "12345006", "uid": "99990000"})
        check(data["status"] == "approved" and "放行" in data["reason"], "bili_on_error=pass 时风控放行")
        store.set_setting("bili_on_error", "reject")
        bili.clear_cache()
        status, data, _ = http.json("/api/apply", method="POST", payload={"qq": "12345007", "uid": "99990001"})
        check(data["status"] == "rejected", "bili_on_error=reject 时风控拒绝")
        store.set_setting("bili_on_error", "manual")

        print("\n[3] UID 唯一性与隐私")
        state["payload"] = card(name="复用者", level=6, fans=100)
        bili.clear_cache()
        status, data, _ = http.json("/api/apply", method="POST", payload={"qq": "12345008", "uid": "12345678"})
        check(status == 400, "同一 UID 被别的 QQ 提交 -> 直接拒绝（400）")
        check(not re.search(r"\d{5,}", str(data.get("error") or "")), f"错误信息里不含任何号码（{data.get('error')}）")
        check(len(state["calls"]) >= 1, "核验确实调用过 B站 接口")
        # 同一个 QQ 重用自己的 UID 也应当被拒绝（已有 pending/approved 记录）
        status, data, _ = http.json("/api/apply", method="POST", payload={"qq": "12345678", "uid": "12345678"})
        check(status == 400, "同一个 QQ 重复提交同一 UID 也被拒")
        logs = store.recent_logs(20)
        check(any(row["action"] == "apply_uid_taken" for row in logs), "UID 占用尝试被记入日志供管理员查看")

        print("\n[4] 输入校验与限流")
        status, data, _ = http.json("/api/apply", method="POST", payload={"qq": "12", "uid": "12345678"})
        check(status == 400 and "QQ" in data["error"], "QQ 太短被拒")
        status, data, _ = http.json("/api/apply", method="POST", payload={"qq": "1234567890123", "uid": "1"})
        check(status == 400, "QQ 太长 / UID 太短被拒")
        status, data, _ = http.json("/api/apply", method="POST", payload={"qq": "12345679", "uid": "不认识中文"})
        check(status == 400 and "UID" in data["error"], "无法识别 UID 时给出明确提示")
        status, data, _ = http.json("/api/apply", method="POST", payload={"qq": "12345679", "uid": "12345678", "note": "x" * 201})
        check(status == 400 and "备注" in data["error"], "备注超长被拒")
        status, data, _ = http.json("/api/apply", method="POST", payload={"qq": "12345679", "uid": "  UID：12345678901 "})
        check(status == 400, "11 位数字不算 UID")
        # 限流：每 QQ 3 次 / 10 分钟（前面的 12345678 已用掉 2 次）
        status, data, _ = http.json("/api/apply", method="POST", payload={"qq": "12345678", "uid": "12345678"})
        check(status in (400, 429), f"触发限流或重复提交保护（{status}）")
        # 按 IP 的限流：临时压到 1 次/窗口
        store.set_setting("apply_per_ip", 1)
        server.app.apply_limiter.reset("ip:127.0.0.1")
        first_ip = http.json("/api/apply", method="POST", payload={"qq": "12345088", "uid": "71000001"})
        second_ip = http.json("/api/apply", method="POST", payload={"qq": "12345089", "uid": "71000002"})
        check(first_ip[0] == 200 and second_ip[0] == 429, f"按 IP 限流生效（{first_ip[0]}/{second_ip[0]}）")
        store.set_setting("apply_per_ip", 200)
        server.app.apply_limiter.reset("ip:127.0.0.1")
        status, data, _ = http.json("/api/apply", method="POST", payload={"qq": "12345681", "uid": "76543210"})
        check(status == 200, "换一个 QQ/UID 仍可正常提交")

        print("\n[5] 管理后台")
        status, body, _ = http("/admin")
        check(status == 200 and "密码" in body, "未登录时 /admin 显示登录页")
        status, data, _ = http.json("/api/admin/applications")
        check(status == 401, "未登录访问管理接口 -> 401")
        status, data, _ = http.json("/api/admin/applications", method="POST")
        check(status in (401, 404, 405), "管理接口方法受限")
        for _ in range(app_module.LOGIN_MAX_FAILURES):
            status, data, _ = http.json("/api/admin/login", method="POST", payload={"password": "wrong"})
        check(status == 401, f"密码错误返回 401（{status}）")
        status, data, _ = http.json("/api/admin/login", method="POST", payload={"password": "wrong"})
        check(status == 429, f"超过错误次数后被锁定（{status}）")
        # 锁定是按 IP 的，这里直接重置限流器以继续测试
        http_app = server.app
        http_app.login_limiter.reset("login:127.0.0.1")
        status, data, headers = http.json("/api/admin/login", method="POST", payload={"password": "pw-12345678"})
        check(status == 200 and http.cookie.startswith("review_admin="), "正确密码登录成功并下发 cookie")
        check("HttpOnly" in str(headers.get("Set-Cookie")) and "SameSite=Lax" in str(headers.get("Set-Cookie")), "cookie 带 HttpOnly 与 SameSite=Lax")

        status, body, _ = http("/admin")
        check(status == 200 and 'id="initial-data"' in body, "/admin 已登录时渲染后台页")
        match = re.search(r'id="initial-data">(.*?)</script>', body, re.S)
        initial = json.loads(match.group(1).replace("\\u003c", "<")) if match else {}
        csrf = str(initial.get("csrf") or "")
        check(bool(csrf), "后台页里带 CSRF token")
        check(bool(initial.get("items")), "首屏数据里有申请列表")
        check("plugin_token" not in json.dumps(initial.get("settings") or {}).replace('"plugin_token": ""', ""), "首屏设置里不回显插件 token")

        status, data, _ = http.json("/api/admin/applications", cookie=http.cookie)
        check(status == 200 and data["total"] >= 5 and "counts" in data, "管理列表可读")
        first_id = data["items"][0]["id"]
        check("ip" in data["items"][0] and "delivered" in data["items"][0], "管理列表字段完整")
        status, data, _ = http.json("/api/admin/applications?status=approved&q=12345678", cookie=http.cookie)
        check(status == 200 and all(row["status"] == "approved" for row in data["items"]), "按状态与关键词筛选")
        status, data, _ = http.json("/api/admin/applications?status=approved&offset=999", cookie=http.cookie)
        check(status == 200 and data["items"] == [], "偏移量超出时返回空列表")

        status, data, _ = http.json("/api/admin/decision", method="POST", payload={"csrf": "", "id": first_id, "action": "reject"}, cookie=http.cookie)
        check(status == 403, "空 CSRF 不能绕过校验（必须 403）")
        status, data, _ = http.json("/api/admin/decision", method="POST", payload={"csrf": "bad", "id": first_id, "action": "reject"}, cookie=http.cookie)
        check(status == 403, "错误 CSRF -> 403")
        status, data, _ = http.json("/api/admin/decision", method="POST", payload={"csrf": csrf, "id": 999999, "action": "reject"}, cookie=http.cookie)
        check(status == 404, "不存在的记录 -> 404")
        status, data, _ = http.json("/api/admin/decision", method="POST", payload={"csrf": csrf, "id": first_id, "action": "nonsense"}, cookie=http.cookie)
        check(status == 400, "未知操作 -> 400")

        target = next((row for row in data.get("items", []) if row["id"] == first_id), None)
        status, data, _ = http.json("/api/admin/decision", method="POST", payload={"csrf": csrf, "id": first_id, "action": "reject"}, cookie=http.cookie)
        check(status == 200 and data["item"]["status"] == "rejected", "手动拒绝生效")
        status, data, _ = http.json("/api/admin/decision", method="POST", payload={"csrf": csrf, "id": first_id, "action": "approve"}, cookie=http.cookie)
        check(status == 200 and data["item"]["status"] == "approved", "手动通过生效")
        blocked_qq = data["item"]["qq"]
        status, data, _ = http.json("/api/admin/decision", method="POST", payload={"csrf": csrf, "id": first_id, "action": "block"}, cookie=http.cookie)
        check(status == 200 and data["item"]["status"] == "blocked", "拉黑生效")
        status, data, _ = http.json("/api/apply", method="POST", payload={"qq": blocked_qq, "uid": "11223344"})
        check(status == 400 and "拉黑" in data.get("error", ""), "被拉黑的 QQ 无法再提交")
        status, data, _ = http.json("/api/admin/decision", method="POST", payload={"csrf": csrf, "id": first_id, "action": "unblock"}, cookie=http.cookie)
        check(status == 200 and data["item"]["status"] == "manual", "解除拉黑后回到待人工")
        status, data, _ = http.json("/api/admin/delete", method="POST", payload={"csrf": csrf, "id": first_id}, cookie=http.cookie)
        check(status == 200, "删除记录成功")
        status, data, _ = http.json("/api/admin/delete", method="POST", payload={"csrf": csrf, "id": first_id}, cookie=http.cookie)
        check(status == 404, "重复删除 -> 404")

        status, body, headers = http("/api/admin/export.csv", cookie=http.cookie)
        check(status == 200 and body.lstrip("\ufeff").startswith("id,ticket,qq,uid"), "CSV 导出带表头与 BOM")
        check("attachment" in str(headers.get("Content-Disposition")), "CSV 带下载文件名")

        status, data, _ = http.json("/api/admin/settings", cookie=http.cookie)
        check(status == 200 and data["settings"]["plugin_token"] == "", "读设置不回显 token")
        status, data, _ = http.json(
            "/api/admin/settings",
            method="POST",
            payload={"csrf": csrf, "site_name": "改名后的站点", "bili_min_level": 2, "bili_name_keywords": "猫, neko"},
            cookie=http.cookie,
        )
        check(status == 200 and data["settings"]["site_name"] == "改名后的站点", "保存站点设置")
        check(data["settings"]["bili_name_keywords"] == ["猫", "neko"], "关键词字符串被拆成列表")
        status, data, _ = http.json("/api/admin/settings", method="POST", payload={"csrf": csrf, "bili_min_level": 9}, cookie=http.cookie)
        check(status == 200 and data["settings"]["bili_min_level"] == 6, "等级越界被夹到 6")
        status, data, _ = http.json("/api/admin/settings", method="POST", payload={"csrf": csrf, "admin_password": "short"}, cookie=http.cookie)
        check(status == 400, "过短的新密码被拒绝")
        status, data, _ = http.json("/api/admin/settings", method="POST", payload={"csrf": csrf, "plugin_token": "short"}, cookie=http.cookie)
        check(status == 400, "过短的插件 token 被拒绝")
        # 收尾：把站点自己的规则与站点名恢复，避免影响后面的用例
        status, data, _ = http.json(
            "/api/admin/settings",
            method="POST",
            payload={"csrf": csrf, "site_name": "自检审核站", "bili_min_level": 0, "bili_min_fans": 0, "bili_name_keywords": []},
            cookie=http.cookie,
        )
        check(data["settings"]["bili_min_level"] == 0 and data["settings"]["bili_name_keywords"] == [], "收尾：站点规则已复位")
        status, body, _ = http("/static/../app.py", cookie=http.cookie)
        check(status == 404, "登录后依然不能穿越目录")

        print("\n[6] 插件同步与名单拉取")
        token = "test-token-0123456789abcdef"
        status, data, _ = http.json("/api/plugin/sync", method="POST", payload={"token": "bad", "code": "X"})
        check(status == 401, "错误 token 被拒")
        status, data, _ = http.json("/api/plugin/ping")
        check(status == 401, "ping 也需要 token")
        status, data, _ = http.json("/api/plugin/ping?token=" + token)
        check(status == 200 and data["ok"] is True, "带 token 的 ping 正常")
        status, data, _ = http.json(
            "/api/plugin/sync",
            method="POST",
            payload={
                "token": token,
                "code": "CODE01",
                "code_expire": "2026-01-01 00:00",
                "groups": ["123456"],
                "bili": {"bili_min_level": 1, "bili_min_fans": 0, "bili_unique": True},
                "bindings": {"12345678": "12345678"},
                "stats": {"pending": 1, "approved": 1},
            },
        )
        check(status == 200 and data["ok"] is True, "同步成功")
        check(data["pending_deliveries"] >= 1, f"同步后有待投递记录（{data['pending_deliveries']}）")
        check(len(store.all_approved()) >= 1, "站点侧确实有已通过记录")
        status, data, _ = http.json(f"/api/plugin/applications?token={token}&status=approved")
        check(status == 200 and data["count"] >= 1, "插件能拉到已通过名单")
        item = data["items"][0]
        check(set(item) >= {"id", "qq", "uid", "code", "decided_at"}, "投递项字段完整")
        check(item["code"] == "CODE01", "投递项带当前验证码")
        ids = [row["id"] for row in data["items"]]
        status, data, _ = http.json("/api/plugin/ack", method="POST", payload={"token": token, "ids": ids})
        check(status == 200 and data["acked"] == len(ids), "ack 全部成功")
        status, data, _ = http.json(f"/api/plugin/applications?token={token}")
        check(data["count"] == 0, "ack 之后不再重复投递（幂等）")
        status, data, _ = http.json("/api/plugin/ack", method="POST", payload={"token": token, "ids": "notalist"})
        check(status == 400, "ack 的 ids 必须是数组")
        status, data, _ = http.json(f"/api/plugin/applications?token={token}&status=rejected")
        check(status == 400, "只允许拉取 approved")
        # 插件同步的规则要生效（use_plugin_rules 默认 true）
        status, data, _ = http.json("/api/admin/settings", cookie=http.cookie)
        check(data["plugin"]["rules"]["source"] == "plugin", "判定规则来源标记为插件")
        check(data["plugin"]["rules"]["bili_min_level"] == 1, "用的是插件同步来的最低等级")
        bili.clear_cache()
        state["payload"] = card(name="等级刚好", level=1, fans=0)
        status, data, _ = http.json("/api/apply", method="POST", payload={"qq": "12345099", "uid": "72000001"})
        check(data["status"] == "approved", "插件规则（最低等级 1）生效后通过")
        bili.clear_cache()
        state["payload"] = card(name="零级", level=0, fans=0)
        status, data, _ = http.json("/api/apply", method="POST", payload={"qq": "12345098", "uid": "72000002"})
        check(data["status"] == "rejected", "插件规则下等级 0 被拒")
        # 换码后网页上的旧码要刷新
        state["payload"] = card(name="新人", level=6, fans=50)
        bili.clear_cache()
        http.json("/api/plugin/sync", method="POST", payload={"token": token, "code": "CODE02"})
        status, data, _ = http.json("/api/apply/" + ticket)
        check(data["code"] == "CODE02", "插件换码后网页显示新码")
        status, data, _ = http.json("/api/apply", method="POST", payload={"qq": "12345097", "uid": "72000003"})
        check(data["status"] == "approved" and data["code"] == "CODE02", "新申请直接用同步来的码")

        print("\n[7] 无 JS 的表单提交（内置降级页）")
        status, body, _ = http("/api/apply", method="POST", form={"qq": "12345096", "uid": "73000001"})
        check(status == 200 and "<html" in body.lower(), "表单编码提交返回 HTML 结果页")
        check("已通过" in body or "未通过" in body or "待" in body, "结果页包含状态文案")
        status, body, _ = http("/api/apply", method="POST", form={"qq": "bad", "uid": "73000002"})
        check(status == 400 and "QQ" in body, "表单提交的错误也渲染成页面")

        print("\n[8] 前端资源结构")
        for name in ("apply.html", "login.html", "admin.html"):
            check((SITE_DIR / "templates" / name).is_file(), f"templates/{name} 存在")
        for name in ("style.css", "app.js", "form.js"):
            check((SITE_DIR / "static" / name).is_file(), f"static/{name} 存在")
        apply_html = (SITE_DIR / "templates" / "apply.html").read_text(encoding="utf-8")
        admin_html = (SITE_DIR / "templates" / "admin.html").read_text(encoding="utf-8")
        login_html = (SITE_DIR / "templates" / "login.html").read_text(encoding="utf-8")
        for name, text in (("apply", apply_html), ("admin", admin_html), ("login", login_html)):
            check("./static/style.css" in text and "./static/app.js" in text, f"{name}.html 用相对路径引资源")
            check('type="module"' in text, f"{name}.html 用 module 脚本")
            check("bridge-sdk" not in text and "cdn" not in text.lower(), f"{name}.html 不外链第三方资源")
        check("$site_name" in apply_html and "$notice_html" in apply_html, "apply.html 占位符齐全")
        check("$csrf" in admin_html and "$initial_json" in admin_html, "admin.html 占位符齐全")
        check("$error_html" in login_html, "login.html 占位符齐全")

        status, data, _ = http.json("/api/admin/logout", cookie=http.cookie)
        check(status == 200, "退出登录")
        status, data, _ = http.json("/api/admin/applications", cookie=http.cookie)
        check(status == 401, "退出后管理接口再次 401")
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(DATA_DIR, ignore_errors=True)

    print("\n" + "=" * 60)
    print(f"自检结果：{PASSED}/{PASSED + len(FAILED)} 通过")
    if FAILED:
        print("失败项：")
        for item in FAILED:
            print(f"  - {item}")
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
