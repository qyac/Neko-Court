#!/usr/bin/env python3
"""把审核网站打包成可分发的 zip。

用法：
    python tools/package_site.py                 # 输出 dist/neko-court-review-site-v<版本>.zip
    python tools/package_site.py --out dist     # 指定输出目录

约定（与 tools/package_plugin.py 保持一致）：
- 只为**确定性的产物**打包：固定时间戳、UTF-8、LF 换行，保证同样内容得到同样的 sha256；
- 不含本地数据（`data/`）、不含 `__pycache__`、不含自检脚本；
- 包内顶层目录固定为 `neko-court-review-site/`，解压即用；
- 打包后自检产物：文件清单、无 CRLF、无本地数据、启动脚本存在。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent
LAUNCHER_DIR = WORKSPACE / "review-site"
# 实现放在插件里（内置模式与独立部署共用同一份代码）
SITE_DIR = WORKSPACE / "astrbot_plugin_temp_review_group" / "review_web"
DEFAULT_OUT_DIR = WORKSPACE / "dist"
PACKAGE_NAME = "neko-court-review-site"
FIXED_TIME = (2026, 1, 1, 0, 0, 0)
PLUGIN_METADATA = WORKSPACE / "astrbot_plugin_temp_review_group" / "metadata.yaml"

# Windows 控制台常是 GBK，直接 print "✓" 会抛 UnicodeEncodeError；强制切 UTF-8（失败也不影响功能）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

EXCLUDE_DIRS = {"__pycache__", "data", ".pytest_cache", ".mypy_cache", "node_modules"}
EXCLUDE_FILES = {".DS_Store", "Thumbs.db"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".log"}

# 分发时必须存在的文件
REQUIRED = (
    "serve.py",
    "review_web/VERSION",
    "review_web/__init__.py",
    "review_web/cli.py",
    "review_web/app.py",
    "review_web/store.py",
    "review_web/bili.py",
    "review_web/render.py",
    "review_web/README.md",
    "review_web/templates/apply.html",
    "review_web/templates/login.html",
    "review_web/templates/admin.html",
    "review_web/static/style.css",
    "review_web/static/app.js",
    "review_web/static/form.js",
)



def update_sums(sums_path: Path, name: str, digest: str) -> None:
    """按文件名更新 SHA256SUMS.txt：保留其它仍存在的产物，清掉已被删除的旧行。"""
    directory = sums_path.parent
    lines: list[str] = []
    if sums_path.is_file():
        for line in sums_path.read_text(encoding="utf-8").splitlines():
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            listed = parts[1].strip()
            if listed == name:
                continue
            if not (directory / listed).is_file():
                continue  # 产物已被删除（例如旧版本），不再留在清单里
            lines.append(line.rstrip())
    lines.append(f"{digest}  {name}")
    sums_path.write_text("\n".join(sorted(lines)) + "\n", encoding="utf-8", newline="\n")

def plugin_version() -> str:
    """站点版本：读 review-site/VERSION（单一来源；缺失时退回插件 metadata）。"""
    version_file = SITE_DIR / "VERSION"
    try:
        text = version_file.read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    try:
        text = PLUGIN_METADATA.read_text(encoding="utf-8")
    except OSError:
        return "v0.0.0"
    match = re.search(r"^version:\s*(\S+)\s*$", text, re.M)
    return match.group(1) if match else "v0.0.0"


def collect_files() -> list[tuple[Path, str]]:
    """站点包 = 启动器（review-site/serve.py）+ 插件里的实现（review_web/）。"""
    items: list[tuple[Path, str]] = []
    launcher = LAUNCHER_DIR / "serve.py"
    if launcher.is_file():
        items.append((launcher, "serve.py"))
    for path in sorted(SITE_DIR.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(SITE_DIR)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if path.name in EXCLUDE_FILES or path.suffix.lower() in EXCLUDE_SUFFIXES:
            continue
        items.append((path, f"review_web/{rel.as_posix()}"))
    return items


def build_zip(out_dir: Path) -> Path:
    version = plugin_version()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{PACKAGE_NAME}-{version}.zip"
    files = collect_files()
    if not files:
        raise SystemExit(f"没有找到可打包的文件：{SITE_DIR}")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path, rel in files:
            data = path.read_bytes().replace(b"\r\n", b"\n")
            info = zipfile.ZipInfo(f"{PACKAGE_NAME}/{rel}", date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
    return target


def verify_zip(target: Path) -> list[str]:
    """产物自检：缺失文件、CRLF、混进本地数据都会报错。"""
    problems: list[str] = []
    with zipfile.ZipFile(target) as zf:
        names = zf.namelist()
        for required in REQUIRED:
            if f"{PACKAGE_NAME}/{required}" not in names:
                problems.append(f"缺少必需文件：{required}")
        for name in names:
            data = zf.read(name)
            if b"\r\n" in data:
                problems.append(f"包含 CRLF：{name}")
            parts = Path(name).parts
            if any(part in {"data", "__pycache__"} for part in parts):
                problems.append(f"包含不该分发的目录：{name}")
            if name.endswith((".db", ".db-wal", ".db-shm")):
                problems.append(f"包含本地数据库：{name}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打包审核网站")
    parser.add_argument("--out", default=str(DEFAULT_OUT_DIR), help="输出目录，默认 dist/")
    args = parser.parse_args(argv)

    version = plugin_version()
    target = build_zip(Path(args.out))
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    problems = verify_zip(target)
    sums = Path(args.out).resolve() / "SHA256SUMS.txt"
    update_sums(sums, target.name, digest)

    print(f"审核网站：{PACKAGE_NAME} {version}")
    print(f"产物：{target}  ({target.stat().st_size} 字节)")
    print(f"sha256：{digest}")
    with zipfile.ZipFile(target) as zf:
        print(f"包内文件（{len(zf.namelist())} 个）：")
        for name in sorted(zf.namelist()):
            print(f"  {name}")
    if problems:
        print("\n产物自检未通过：")
        for item in problems:
            print(f"  ✗ {item}")
        return 1
    print("\n产物自检通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
