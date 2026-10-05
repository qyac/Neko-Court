#!/usr/bin/env python3
"""审核网站「打不开」诊断脚本（在**运行 AstrBot 的那台机器**上执行）。

用法：
    python tools/diagnose_site.py                      # 自动找 AstrBot 目录
    python tools/diagnose_site.py --astrbot D:\\AstrBot  # 指定 AstrBot 根目录
    python tools/diagnose_site.py --plugin /opt/AstrBot/data/plugins/astrbot_plugin_temp_review_group

它会顺着"插件装了吗 → 配置开了吗 → 站点起来过吗 → 端口在听吗 → HTTP 真通吗"这条链逐项检查，
每项都给出结论与下一步动作。只用标准库，不会修改任何东西（除了可能创建 1 个临时数据库？不会：只读）。
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

PLUGIN_NAME = "astrbot_plugin_temp_review_group"
SITE_KEYS = (
    "web_site_enabled",
    "web_site_host",
    "web_site_port",
    "web_site_trust_proxy",
    "web_review_enabled",
    "web_review_url",
    "web_review_poll_seconds",
    "web_review_auto_approve",
)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

OK = "  ✅ "
BAD = "  ❌ "
WARN = "  ⚠️  "
INFO = "  · "


def candidate_astrbot_dirs() -> list[Path]:
    here = Path(__file__).resolve().parent.parent
    guesses = [
        here.parent / "AstrBot",
        here.parent / "AstrBot-master",
        here.parent / "astrbot",
        Path.cwd(),
        Path.home() / "AstrBot",
        Path("/opt/AstrBot"),
        Path("/root/AstrBot"),
    ]
    return [path for path in guesses if path.is_dir()]


def find_plugin(astrbot_dir: Path | None) -> Path | None:
    if astrbot_dir:
        candidate = astrbot_dir / "data" / "plugins" / PLUGIN_NAME
        return candidate if candidate.is_dir() else None
    for base in candidate_astrbot_dirs():
        candidate = base / "data" / "plugins" / PLUGIN_NAME
        if candidate.is_dir():
            return candidate
    return None


def local_ips() -> list[str]:
    ips: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = str(info[4][0])
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("223.5.5.5", 80))
            ip = probe.getsockname()[0]
            if ip not in ips:
                ips.insert(0, ip)
        finally:
            probe.close()
    except Exception:
        pass
    return ips[:5]


def port_state(host: str, port: int) -> tuple[bool, str]:
    """返回（能否连上, 说明）。"""
    target = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    sock = socket.socket()
    sock.settimeout(2.0)
    try:
        sock.connect((target, port))
        return True, f"{target}:{port} 可以连接"
    except ConnectionRefusedError:
        return False, f"{target}:{port} 拒绝连接（没有程序在监听）"
    except socket.timeout:
        return False, f"{target}:{port} 连接超时（可能被防火墙丢弃）"
    except Exception as exc:
        return False, f"{target}:{port} 连接失败（{type(exc).__name__}: {exc}）"
    finally:
        sock.close()


def http_get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read().decode("utf-8", "replace")[:200]
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="审核网站打不开诊断")
    parser.add_argument("--astrbot", default="", help="AstrBot 根目录（默认自动猜）")
    parser.add_argument("--plugin", default="", help="直接指定插件目录")
    parser.add_argument("--port", type=int, default=0, help="覆盖配置里的端口")
    parser.add_argument("--remote", action="store_true", help="我是从另一台机器访问的（此时监听 127.0.0.1 会被判为问题）")
    args = parser.parse_args(argv)

    problems: list[str] = []
    print("=" * 62)
    print("审核网站诊断（在运行 AstrBot 的机器上执行）")
    print("=" * 62)

    plugin_dir = Path(args.plugin) if args.plugin else find_plugin(Path(args.astrbot) if args.astrbot else None)
    if plugin_dir is None or not plugin_dir.is_dir():
        print(BAD + "找不到插件目录")
        print(INFO + "期望位置：<AstrBot>/data/plugins/" + PLUGIN_NAME)
        print(INFO + "说明：插件没装（或装在了别的 AstrBot 目录）——没有插件就没有内置网站。")
        print(INFO + "解决：把 Release 里的插件 zip 解压到 <AstrBot>/data/plugins/，重启/重载插件。")
        return 1
    # AstrBot 根目录：显式给了就用它，否则从插件目录推算（<AstrBot>/data/plugins/<插件>）
    if args.astrbot:
        astrbot_dir = Path(args.astrbot)
    elif plugin_dir.parent.name == "plugins" and plugin_dir.parent.parent.name == "data":
        astrbot_dir = plugin_dir.parent.parent.parent
    else:
        astrbot_dir = plugin_dir
    print(OK + f"插件目录：{plugin_dir}")
    print(INFO + f"AstrBot 根目录：{astrbot_dir}")

    # 1) 插件内容是否完整（内置站点需要 review_web/）
    metadata = plugin_dir / "metadata.yaml"
    version = ""
    if metadata.is_file():
        for line in metadata.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("version:"):
                version = line.split(":", 1)[1].strip()
    print(INFO + f"插件版本：{version or '未知'}")
    review_web = plugin_dir / "review_web"
    if review_web.is_dir():
        print(OK + f"内置站点实现存在（review_web/，{len(list(review_web.rglob('*.py')))} 个 py 文件）")
    else:
        print(BAD + "没有 review_web/ 目录 → 这是 v1.1.6 或更早的版本，**没有内置站点功能**")
        problems.append("插件版本过旧：请升级到 v1.1.7+ 才能用内置站点（或改用 review-site/serve.py 独立部署）")

    # 2) 配置
    config_path = astrbot_dir / "data" / "config" / f"{PLUGIN_NAME}_config.json"
    settings: dict = {}
    if config_path.is_file():
        try:
            # AstrBot 自己就用 utf-8-sig 读写插件配置（会带 BOM），这里必须跟着用它
            settings = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            print(BAD + f"配置读取失败：{exc}")
        print(OK + f"配置文件：{config_path}")
    else:
        print(BAD + f"没有配置文件：{config_path}")
        print(INFO + "说明：插件可能装了但从未被加载过（AstrBot 生成配置是在插件加载之后）。")
        problems.append("插件没被加载过：重启 AstrBot 或在插件管理里重载，让它生成配置")

    if settings:
        print("  ── 站点相关配置 ──")
        for key in SITE_KEYS:
            if key in settings:
                print(INFO + f"{key} = {json.dumps(settings[key], ensure_ascii=False)}")

    enabled = bool(settings.get("web_site_enabled", False))
    host = str(settings.get("web_site_host") or "127.0.0.1")
    port = int(settings.get("web_site_port") or 8787)
    if args.port:
        port = args.port
    standalone = bool(settings.get("web_review_enabled", False)) and bool(settings.get("web_review_url"))

    if enabled:
        print(OK + "内置站点开关：已打开（web_site_enabled=true）")
    elif standalone:
        print(WARN + "内置站点没开，但你配了独立站点（web_review_enabled + web_review_url）")
        print(INFO + "独立站点必须自己在跑：`python review-site/serve.py --host 0.0.0.0 --port 8787`")
    else:
        print(BAD + "内置站点开关是关的（web_site_enabled=false）→ 根本没有启动网站")
        problems.append("打开 web_site_enabled（插件配置 → 内置审核网站），然后重载插件")

    # 3) 站点数据（起来过就会有）
    data_db = astrbot_dir / "data" / "plugin_data" / PLUGIN_NAME / "review-web" / "review-site.db"
    if data_db.is_file():
        size = data_db.stat().st_size
        print(OK + f"站点数据库存在（{data_db.name}，{size} 字节）→ 站点至少成功启动过一次")
    else:
        print(WARN + f"没有站点数据库（{data_db}）→ 站点可能从未成功启动")
        if enabled:
            problems.append("开了开关但没有数据文件：看 AstrBot 日志里有没有「内置审核网站启动失败」（通常是端口被占用）")

    # 3.5) 题库与网页答题
    print("  ── 网页答题 ──")
    ask_on = bool(settings.get("web_ask_questions", True))
    print(("  ✅ 网页答题：开" if ask_on else "  ⚠️  网页答题：关（申请人只需填 QQ 与 B站 UID）"))
    site_questions = 0
    plugin_questions = 0
    if data_db.is_file():
        try:
            import sqlite3

            conn = sqlite3.connect(f"file:{data_db}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            site_questions = int(conn.execute("SELECT COUNT(*) AS n FROM questions WHERE enabled = 1").fetchone()["n"])
            row = conn.execute("SELECT value FROM settings WHERE key = 'plugin_questions'").fetchone()
            if row is not None:
                try:
                    plugin_questions = len([item for item in json.loads(row["value"]) if isinstance(item, dict) and item.get("enabled", True)])
                except Exception:
                    plugin_questions = 0
            conn.close()
        except Exception as exc:
            print(WARN + f"题库读取失败：{exc}")
    synced_at = float(settings.get("plugin_synced_at") or 0)
    print(INFO + f"站点题库：{site_questions} 条（在网站后台「题库」里维护）")
    if synced_at:
        print(INFO + f"插件题库：{plugin_questions} 条（插件同步过来的，站点题库为空时用它）")
    else:
        print(WARN + "插件题库：尚未同步过（插件还没成功连上本站；先看上面的同步状态）")
    effective = site_questions or plugin_questions
    # 题库为空只是"不会出题"，不影响站点可用，所以算提示不算错误
    if ask_on and effective == 0:
        print(WARN + "题库是空的 → 网页不会出题（申请人只需填 QQ 与 B站 UID）")
        print(INFO + "想让人在网页上答题：网站后台「题库」新增题目，或在插件配置里配好 questions 并重载插件")
    elif ask_on:
        print(OK + f"网页会随机出一道题（可用题目约 {effective} 条）")

    # 4) 端口
    print("  ── 端口检查 ──")
    reachable, note = port_state(host, port)
    print((OK if reachable else BAD) + note)
    if not reachable:
        if enabled or standalone:
            problems.append(
                f"端口 {port} 上没有监听：查 AstrBot 日志的「内置审核网站已启动 / 启动失败」；"
                "若失败原因是端口占用，把 web_site_port 换个值"
            )
    else:
        status, body = http_get(f"http://127.0.0.1:{port}/healthz")
        if status == 200 and '"ok"' in body:
            print(OK + f"HTTP 健康检查通过：{body.strip()[:80]}")
        else:
            print(WARN + f"端口有人在听，但 /healthz 返回 {status} {body[:80]}（可能是别的程序占用了这个端口）")
            problems.append(f"端口 {port} 上的服务不是审核网站：换个端口，或确认占用它的程序")
        status, body = http_get(f"http://127.0.0.1:{port}/")
        print((OK if status == 200 else WARN) + f"申请页 HTTP {status}")

    # 5) 访问方式
    print("  ── 应该怎么访问 ──")
    if host in ("127.0.0.1", "localhost"):
        if args.remote:
            print(BAD + f"web_site_host = {host} → 你说了是从别的机器访问，这样就一定打不开")
            problems.append("web_site_host 是 127.0.0.1：改成 0.0.0.0 并放行防火墙端口，然后重载插件")
        else:
            print(WARN + f"web_site_host = {host} → 只有这台机器能打开（本机访问 OK；"
                  "如果你其实是从另一台机器访问，请加 --remote 重新跑，问题会定位到这里）")
    else:
        print(OK + f"web_site_host = {host}（允许从其它机器访问）")
    print(INFO + f"本机自测：http://127.0.0.1:{port}/   （后台 /admin）")
    for ip in local_ips():
        print(INFO + f"局域网/公网：http://{ip}:{port}/")
    if sys.platform.startswith("win"):
        print(INFO + f"Windows 放行端口（管理员 PowerShell）：")
        print(INFO + f'  New-NetFirewallRule -DisplayName "review-site {port}" -Direction Inbound -Protocol TCP -LocalPort {port} -Action Allow')
    else:
        print(INFO + f"Linux 放行端口：sudo ufw allow {port}/tcp   （或用安全组/云防火墙放行）")
    if str(settings.get("web_site_trust_proxy", False)).lower() in ("true", "1"):
        print(INFO + "已开启 web_site_trust_proxy（放在反代后面时才对）")

    print("=" * 62)
    if problems:
        print("结论：还有以下问题需要处理")
        for index, item in enumerate(problems, start=1):
            print(f"  {index}. {item}")
        return 1
    print("结论：链路正常——端口在听、/healthz 通过。若浏览器仍打不开，检查客户端网络/防火墙/是否用了正确 IP。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
