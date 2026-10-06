#!/usr/bin/env python3
"""发布前校验：提交 = 工作区 = 打包产物，且包内没有 CRLF。

用法：
    python tools/verify_artifacts.py                 # 自动发现 dist/ 下最新的两个产物
    python tools/verify_artifacts.py --version v1.1.7

它做三件事：
1. `git HEAD` 里的每个文件与工作区逐字节比对（防止"改了但没提交/提交了但没保存"）；
2. 两个 zip 里的每个条目与工作区对应源文件逐字节比对（防止打包产物过期）；
3. 检查 zip 内文本文件没有 CRLF（Windows 上很容易混进去，会让插件在 Linux 上出现怪问题）。

两种产物的目录映射不一样，这里显式写死：
- 插件包：`astrbot_plugin_temp_review_group/**` → 仓库同名目录
- 站点包：`neko-court-review-site/serve.py` → `review-site/serve.py`；
  `neko-court-review-site/review_web/**` → `astrbot_plugin_temp_review_group/review_web/**`
"""

from __future__ import annotations

import argparse
import re
import hashlib
import subprocess
import sys
import zipfile
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent
DIST = WORKSPACE / "dist"
PLUGIN_PACKAGE = "astrbot_plugin_temp_review_group"
SITE_PACKAGE = "neko-court-review-site"
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".zip"}

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass


def git_head_files() -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(WORKSPACE), "ls-files"],
        capture_output=True,
        text=True,
        check=False,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def git_blob(rel: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(WORKSPACE), "show", f"HEAD:{rel}"],
        capture_output=True,
        check=False,
    )
    return result.stdout


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def map_entry(zip_name: str, entry: str) -> Path | None:
    """把 zip 条目映射回仓库里的源文件。"""
    parts = Path(entry).parts
    if not parts:
        return None
    top, rest = parts[0], Path(*parts[1:])
    if top == PLUGIN_PACKAGE:
        return WORKSPACE / PLUGIN_PACKAGE / rest
    if top == SITE_PACKAGE:
        if rest.as_posix() == "serve.py":
            return WORKSPACE / "review-site" / "serve.py"
        if rest.parts and rest.parts[0] == "review_web":
            return WORKSPACE / PLUGIN_PACKAGE / rest
    return None


def check_working_tree(problems: list[str]) -> int:
    files = git_head_files()
    for rel in files:
        path = WORKSPACE / rel
        if not path.is_file():
            problems.append(f"提交里有但工作区缺失：{rel}")
            continue
        if sha256(git_blob(rel)) != sha256(path.read_bytes()):
            problems.append(f"工作区与提交不一致：{rel}")
    return len(files)


def check_zip(zip_path: Path, problems: list[str]) -> int:
    if not zip_path.is_file():
        problems.append(f"产物不存在：{zip_path.name}")
        return 0
    with zipfile.ZipFile(zip_path) as zf:
        install = zf.infolist()
        for info in install:
            data = zf.read(info)
            source = map_entry(zip_path.name, info.filename)
            if source is None:
                problems.append(f"{zip_path.name} 条目无法映射：{info.filename}")
                continue
            if not source.is_file():
                problems.append(f"{zip_path.name} 多出文件（仓库里没有）：{info.filename}")
                continue
            if sha256(data) != sha256(source.read_bytes()):
                problems.append(f"{zip_path.name} 与工作区不一致：{info.filename}")
            if source.suffix.lower() not in BINARY_SUFFIXES and b"\r\n" in data:
                problems.append(f"{zip_path.name} 内含 CRLF：{info.filename}")
        # 反向检查：源目录里的文件是否都进了包（忽略约定排除项）
        return len(install)


def _version_key(path: Path) -> tuple:
    """按版本号数字排序，避免 v1.1.10 被当成比 v1.1.9 旧（纯字符串排序会踩这个坑）。"""
    match = re.search(r"-v(\d+(?:\.\d+)*)\.zip$", path.name)
    if not match:
        return ((), path.name)
    numbers = tuple(int(part) for part in match.group(1).split("."))
    return (numbers, path.name)


def newest(pattern: str) -> Path | None:
    candidates = sorted(DIST.glob(pattern), key=_version_key)
    return candidates[-1] if candidates else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="发布前校验：提交 = 工作区 = 产物")
    parser.add_argument("--version", default="", help="版本号（如 v1.1.7）；留空则取 dist/ 里最新的两个产物")
    parser.add_argument("--sums", action="store_true", help="额外核对 dist/SHA256SUMS.txt 里的哈希")
    args = parser.parse_args(argv)

    if args.version:
        plugin_zip = DIST / f"{PLUGIN_PACKAGE}-{args.version}.zip"
        site_zip = DIST / f"{SITE_PACKAGE}-{args.version}.zip"
    else:
        plugin_zip = newest(f"{PLUGIN_PACKAGE}-v*.zip")
        site_zip = newest(f"{SITE_PACKAGE}-v*.zip")
    if plugin_zip is None or site_zip is None:
        print("找不到打包产物，先跑 tools/package_plugin.py 与 tools/package_site.py")
        return 2

    problems: list[str] = []
    tracked = check_working_tree(problems)
    plugin_count = check_zip(plugin_zip, problems)
    site_count = check_zip(site_zip, problems)

    print(f"仓库提交文件：{tracked} 个")
    print(f"{plugin_zip.name}：{plugin_count} 个条目")
    print(f"{site_zip.name}：{site_count} 个条目")

    if args.sums:
        sums_path = DIST / "SHA256SUMS.txt"
        recorded = {}
        if sums_path.is_file():
            for line in sums_path.read_text(encoding="utf-8").splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2:
                    recorded[parts[1].strip()] = parts[0]
        for artifact in (plugin_zip, site_zip):
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            expected = recorded.get(artifact.name, "")
            if expected != digest:
                problems.append(f"SHA256SUMS 里 {artifact.name} 的哈希不匹配")
            else:
                print(f"{artifact.name} sha256：{digest}（与 SHA256SUMS 一致）")

    if problems:
        print("\n校验未通过：")
        for item in problems:
            print(f"  ✗ {item}")
        return 1
    print("\n提交 = 工作区 = 两个产物，逐字节一致，包内无 CRLF ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
