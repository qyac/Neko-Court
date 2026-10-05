#!/usr/bin/env python3
"""审核网站的命令行入口。

用法：
    python review-site/serve.py --host 0.0.0.0 --port 8787
    python review-site/serve.py --admin-password '你的密码' --plugin-token '至少16位的随机串'
    python review-site/serve.py --print-config        # 只打印当前配置（含插件要填的两项）

首次启动会自动生成管理员密码与插件 token，并打印在控制台上（密码只显示这一次，
登录后可在后台修改）。数据默认放在 review-site/data/ 下。
"""

from __future__ import annotations

import argparse
import secrets
import sys
import time
from pathlib import Path

import app as app_module
import render
from store import Store

BASE_DIR = Path(__file__).resolve().parent


def ensure_defaults(store: Store, *, admin_password: str = "", plugin_token: str = "") -> dict[str, str]:
    """补齐管理员密码与插件 token，返回本次新生成的值。"""
    created: dict[str, str] = {}
    if admin_password:
        store.set_setting("admin_password_hash", app_module.hash_password(admin_password))
    if not store.get_setting("admin_password_hash"):
        password = secrets.token_urlsafe(9)
        store.set_setting("admin_password_hash", app_module.hash_password(password))
        created["admin_password"] = password
    if plugin_token:
        store.set_setting("plugin_token", plugin_token.strip())
    if not store.get_setting("plugin_token"):
        token = secrets.token_urlsafe(24)
        store.set_setting("plugin_token", token)
        created["plugin_token"] = token
    return created


def print_banner(store: Store, host: str, port: int, *, trust_proxy: bool = False) -> None:
    settings = store.all_settings()
    shown_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print("=" * 66)
    print(f"临时审核群 · 审核网站  {app_module.SITE_VERSION}")
    print("=" * 66)
    print(f"申请页（发给群友）：   http://{shown_host}:{port}/")
    print(f"管理后台：             http://{shown_host}:{port}/admin")
    print(f"数据文件：             {store.path}")
    if host in ("0.0.0.0", "::"):
        print("提示：监听 0.0.0.0，外网/局域网可用本机 IP 访问；B站 核验需要服务器能出网。")
    if not trust_proxy:
        print("提示：未开启 --trust-proxy，限流与日志按 TCP 源地址统计；放在反代后面时请打开它。")
    if not render.template_exists("apply.html"):
        print("提示：templates/apply.html 不存在，正在使用内置的极简页面。")
    print("-" * 66)
    print("把下面两项填进 AstrBot 插件配置（web_review_*）：")
    print(f"  web_review_url:   http://<AstrBot 能访问到的地址>:{port}")
    print(f"  web_review_token: {store.get_setting('plugin_token')}")
    print("插件会在一个轮询周期内自动同步验证码与规则，并拉取网页已通过的名单。")
    print("-" * 66)
    print(f"站点自己的 B站 规则来源：{'插件同步的规则（use_plugin_rules=true）' if settings.get('use_plugin_rules', True) else '站点设置'}")
    if store.get_setting("plugin_synced_at"):
        age = int(time.time() - float(store.get_setting("plugin_synced_at")))
        print(f"插件最近同步：{age} 秒前 ｜ 当前验证码：{store.get_setting('plugin_code') or '（空）'}")
    else:
        print("插件尚未同步过（正常，先启动 AstrBot 插件即可）。")
    print("=" * 66)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="临时审核群 · 审核网站（零第三方依赖）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1；对外服务用 0.0.0.0")
    parser.add_argument("--port", type=int, default=8787, help="监听端口，默认 8787")
    parser.add_argument("--data", default=str(BASE_DIR / "data"), help="数据目录（SQLite 文件放在这里）")
    parser.add_argument("--admin-password", default="", help="设置/重置管理员密码（留空则首次启动随机生成）")
    parser.add_argument("--plugin-token", default="", help="设置/重置插件共享 token（留空则首次启动随机生成）")
    parser.add_argument("--print-config", action="store_true", help="只打印配置与插件需要填写的内容，然后退出")
    parser.add_argument(
        "--trust-proxy",
        action="store_true",
        help="部署在反向代理后面时打开：只有这时才读 X-Forwarded-For（否则客户端可以伪造 IP 绕过限流）",
    )
    args = parser.parse_args(argv)

    store = Store(Path(args.data) / "review-site.db")
    created = ensure_defaults(store, admin_password=args.admin_password, plugin_token=args.plugin_token)

    if created.get("admin_password"):
        print("-" * 66)
        print(f"已生成管理员密码（只显示这一次）：{created['admin_password']}")
        print("登录 /admin 后请在设置里改成你自己的密码。")
    if created.get("plugin_token"):
        print(f"已生成插件 token：{created['plugin_token']}")

    print_banner(store, args.host, args.port, trust_proxy=args.trust_proxy)
    if args.print_config:
        return 0

    server = app_module.make_server(host=args.host, port=args.port, store=store, trust_proxy=args.trust_proxy)
    print("服务已启动，Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n收到中断，正在停止…")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
