"""把 AstrBot 插件目录打包成可分发的 zip，并自检产物。

用法：
    python tools/package_plugin.py
    python tools/package_plugin.py --out-dir dist --keep-extracted

约定（与 AstrBot 插件仓库布局一致）：
- zip 内只有一个顶层目录 ``<插件名>/``，把它解压到 ``AstrBot/data/plugins/`` 就能得到
  ``AstrBot/data/plugins/<插件名>/main.py``；
- 排除 ``__pycache__`` / ``*.pyc`` / 编辑器与测试产物；
- 用固定时间戳写条目，同样内容生成同样的 zip，便于用 sha256 校验分发一致性；
- 打包后会把 zip 解压到临时目录跑一遍检查（py_compile + JSON 解析 + 关键文件存在性）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import py_compile
import re
import shutil
import sys
import zipfile
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent
DEFAULT_PLUGIN_DIR = WORKSPACE / "astrbot_plugin_temp_review_group"
DEFAULT_OUT_DIR = WORKSPACE / "dist"

# Windows 控制台常是 GBK，直接 print "✓" 会抛 UnicodeEncodeError；强制切 UTF-8（失败也不影响功能）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

EXCLUDE_DIRS = {
    "__pycache__",
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".selftest_data",
}
EXCLUDE_FILES = {".DS_Store", "Thumbs.db"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}
# 固定时间戳，保证可复现
FIXED_DATE = (2026, 1, 1, 0, 0, 0)
REQUIRED_FILES = ("main.py", "metadata.yaml", "_conf_schema.json")
JSON_FILES = ("_conf_schema.json", ".astrbot-plugin/i18n/zh-CN.json", ".astrbot-plugin/i18n/en-US.json")



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

def read_version(plugin_dir: Path) -> str:
    text = (plugin_dir / "metadata.yaml").read_text(encoding="utf-8")
    match = re.search(r"^version:\s*(\S+)\s*$", text, re.MULTILINE)
    return match.group(1) if match else "v0.0.0"


def read_plugin_name(plugin_dir: Path) -> str:
    text = (plugin_dir / "metadata.yaml").read_text(encoding="utf-8")
    match = re.search(r"^name:\s*(\S+)\s*$", text, re.MULTILINE)
    name = match.group(1) if match else plugin_dir.name
    if name != plugin_dir.name:
        raise SystemExit(f"metadata.yaml 的 name={name} 与目录名 {plugin_dir.name} 不一致，请先统一。")
    return name


def iter_files(plugin_dir: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(plugin_dir.rglob("*")):
        if path.is_dir():
            continue
        if any(part in EXCLUDE_DIRS for part in path.parts):
            continue
        if path.name in EXCLUDE_FILES or path.suffix in EXCLUDE_SUFFIXES:
            continue
        files.append(path)
    return files


def build_zip(plugin_dir: Path, out_path: Path) -> list[str]:
    files = iter_files(plugin_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in files:
            # 相对 plugin_dir 的父目录，保留顶层目录名
            name = path.relative_to(plugin_dir.parent).as_posix()
            info = zipfile.ZipInfo(name, date_time=FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())
    return [path.relative_to(plugin_dir.parent).as_posix() for path in files]


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(zip_path: Path, plugin_name: str, extract_dir: Path) -> list[str]:
    """把 zip 解压到 extract_dir 并检查，返回检查项描述（失败直接抛异常）。"""
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        zf.extractall(extract_dir)

    problems: list[str] = []
    tops = {name.split("/", 1)[0] for name in names}
    if tops != {plugin_name}:
        problems.append(f"zip 顶层目录应只有 {plugin_name!r}，实际 {sorted(tops)}")

    root = extract_dir / plugin_name
    for rel in REQUIRED_FILES:
        if not (root / rel).is_file():
            problems.append(f"缺少必需文件 {rel}")
    if not (root / "pages" / "settings" / "index.html").is_file():
        problems.append("缺少 Pages 入口 pages/settings/index.html")
    for rel in JSON_FILES:
        target = root / rel
        if not target.is_file():
            problems.append(f"缺少 {rel}")
            continue
        try:
            json.loads(target.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{rel} 不是合法 JSON：{exc}")
    if any(name.startswith(f"{plugin_name}/__pycache__") or name.endswith(".pyc") for name in names):
        problems.append("包内混入了 __pycache__/.pyc")

    try:
        py_compile.compile(str(root / "main.py"), doraise=True, cfile=str(extract_dir / "main.pyc"))
    except py_compile.PyCompileError as exc:
        problems.append(f"main.py 无法编译：{exc}")

    if problems:
        raise SystemExit("打包产物检查失败：\n" + "\n".join(f"· {item}" for item in problems))
    return [
        f"顶层目录 {plugin_name}/",
        f"必需文件 {', '.join(REQUIRED_FILES)} 齐全",
        f"Pages 入口与 {len(JSON_FILES)} 个 JSON 配置/国际化文件正常",
        "main.py 通过 py_compile",
        "无 __pycache__/.pyc 混入",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 AstrBot 插件为可分发 zip")
    parser.add_argument("--plugin-dir", type=Path, default=DEFAULT_PLUGIN_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--keep-extracted", action="store_true", help="保留解压后的校验目录")
    args = parser.parse_args()

    plugin_dir = args.plugin_dir.resolve()
    if not plugin_dir.is_dir():
        raise SystemExit(f"插件目录不存在：{plugin_dir}")

    plugin_name = read_plugin_name(plugin_dir)
    version = read_version(plugin_dir)
    out_path = args.out_dir.resolve() / f"{plugin_name}-{version}.zip"

    entries = build_zip(plugin_dir, out_path)
    digest = sha256_of(out_path)
    checks = verify(out_path, plugin_name, args.out_dir.resolve() / "_verify" / plugin_name)

    sums = args.out_dir.resolve() / "SHA256SUMS.txt"
    update_sums(sums, out_path.name, digest)

    print(f"插件：{plugin_name} {version}")
    print(f"产物：{out_path}  ({out_path.stat().st_size} 字节)")
    print(f"sha256：{digest}（已写入 {sums.name}）")
    print(f"\n包内文件（{len(entries)} 个）：")
    for name in entries:
        print(f"  {name}")
    print("\n产物自检：")
    for item in checks:
        print(f"  ✓ {item}")

    if args.keep_extracted:
        print(f"\n解压校验目录保留在：{args.out_dir.resolve() / '_verify'}")
    else:
        shutil.rmtree(args.out_dir.resolve() / "_verify", ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
