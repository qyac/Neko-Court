"""用 GitHub REST API 创建 Release 并上传打包产物（替代 gh CLI）。

用法（在本机已登录/持 token 的终端里跑）：

    # PowerShell
    $env:GITHUB_TOKEN = "<fine-grained PAT: Contents=Read/Write, 或 classic PAT: repo>"
    python tools/create_release.py --tag v1.2.0 `
        --notes-file dist/RELEASE_NOTES-v1.2.0.md `
        --asset dist/astrbot_plugin_temp_review_group-v1.2.0.zip

    # 先看要做什么，不发请求
    python tools/create_release.py --dry-run

前置条件：标签已经推到远端（先 `git push -u origin main --tags`）。脚本会先校验远端存在该标签，
避免 GitHub 用默认分支 HEAD 自建一个轻量标签、导致之后 `git push --tags` 冲突。

Token 只从环境变量读，不会被打印、不会写进任何文件。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent
SITE_VERSION_FILE = WORKSPACE / "review-site" / "VERSION"
DEFAULT_REPO = "qyac/Neko-Court"
DEFAULT_TAG = "v1.2.0"
API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
USER_AGENT = "neko-court-release-script"

# Windows 控制台常是 GBK，直接 print "✓" 会抛 UnicodeEncodeError；强制切 UTF-8（失败也不影响功能）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass


def api_request(
    method: str,
    url: str,
    token: str,
    payload: dict | None = None,
    body: bytes | None = None,
    content_type: str = "application/json",
) -> tuple[int, dict | list | None]:
    data = body
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", USER_AGENT)
    if data is not None:
        request.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        hint = ""
        if exc.code == 401:
            hint = "\n（token 无效或已过期）"
        elif exc.code == 403:
            hint = "\n（token 权限不足：fine-grained 需要 Contents 读写；classic 需要 repo 范围）"
        elif exc.code == 404:
            hint = "\n（仓库/资源不存在，或 token 无权访问该仓库）"
        raise SystemExit(f"GitHub API {method} {url} 失败：HTTP {exc.code}{hint}\n{detail}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="创建 GitHub Release 并上传产物")
    parser.add_argument("--repo", default=DEFAULT_REPO, help=f"owner/name，默认 {DEFAULT_REPO}")
    parser.add_argument("--tag", default=DEFAULT_TAG, help=f"已推送到远端的标签，默认 {DEFAULT_TAG}")
    parser.add_argument("--name", default="", help="Release 标题，默认用标签名")
    parser.add_argument("--notes-file", type=Path, default=None, help="Release 说明的 Markdown 文件")
    parser.add_argument(
        "--asset",
        type=Path,
        action="append",
        default=None,
        help="要上传的产物文件（可以重复传多个）",
    )
    parser.add_argument("--token-env", default="GITHUB_TOKEN", help="存放 token 的环境变量名")
    parser.add_argument("--draft", action="store_true", help="创建为草稿")
    parser.add_argument("--prerelease", action="store_true", help="标记为预发布")
    parser.add_argument("--replace-asset", action="store_true", help="同名产物已存在时先删除再上传")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不发任何请求")
    args = parser.parse_args()

    notes_path = args.notes_file.resolve() if args.notes_file else None
    asset_paths = [item.resolve() for item in (args.asset or [])]
    if notes_path and not notes_path.is_file():
        raise SystemExit(f"说明文件不存在：{notes_path}")
    for item in asset_paths:
        if not item.is_file():
            raise SystemExit(f"产物文件不存在：{item}")

    title = args.name or f"astrbot_plugin_temp_review_group {args.tag}"
    plan = [
        f"仓库：{args.repo}",
        f"标签：{args.tag}（需已在远端）",
        f"标题：{title}",
        f"说明：{notes_path.name if notes_path else '（无）'}",
    ]
    if asset_paths:
        for index, item in enumerate(asset_paths):
            prefix = "产物" if index == 0 else "    "
            plan.append(f"{prefix}：{item.name}  {item.stat().st_size} 字节")
    else:
        plan.append("产物：（无）")
    plan.append(f"草稿：{args.draft} / 预发布：{args.prerelease} / 替换同名产物：{args.replace_asset}")
    print("发布计划：")
    for line in plan:
        print(f"  · {line}")

    token = os.environ.get(args.token_env, "").strip()
    if args.dry_run:
        print(f"\n[dry-run] 未发请求；正式执行需要环境变量 {args.token_env}")
        return 0
    if not token:
        raise SystemExit(
            f"未找到环境变量 {args.token_env}。\n"
            "请先设置一个具备 Contents 读写权限的 GitHub token（不要写进仓库文件）。",
        )

    # 1) 标签必须已在远端，否则 GitHub 会用默认分支自建标签
    ref_url = f"{API}/repos/{args.repo}/git/ref/tags/{urllib.parse.quote(args.tag)}"
    status, _ = api_request("GET", ref_url, token)
    print(f"\n✓ 远端标签已存在（HTTP {status}）")

    body = notes_path.read_text(encoding="utf-8") if notes_path else ""

    # 2) 已存在同标签 Release 就直接复用，避免重复创建报 422
    existing = None
    try:
        status, data = api_request(
            "GET",
            f"{API}/repos/{args.repo}/releases/tags/{urllib.parse.quote(args.tag)}",
            token,
        )
        existing = data if isinstance(data, dict) else None
    except SystemExit as exc:
        if "HTTP 404" not in str(exc):
            raise
    if existing:
        release = existing
        status, release = api_request(
            "PATCH",
            f"{API}/repos/{args.repo}/releases/{release['id']}",
            token,
            payload={
                "name": title,
                "body": body,
                "draft": args.draft,
                "prerelease": args.prerelease,
            },
        )
        print(f"✓ 已更新既有 Release #{release['id']}")
    else:
        status, release = api_request(
            "POST",
            f"{API}/repos/{args.repo}/releases",
            token,
            payload={
                "tag_name": args.tag,
                "name": title,
                "body": body,
                "draft": args.draft,
                "prerelease": args.prerelease,
            },
        )
        print(f"✓ 已创建 Release #{release['id']}")
    assert isinstance(release, dict)

    # 3) 上传产物（同名先删，保证幂等；支持多个产物）
    if asset_paths:
        upload_url = release.get("upload_url", "").split("{")[0]
        if not upload_url:
            raise SystemExit("Release 响应里没有 upload_url，无法上传产物。")
        existing = {item.get("name"): item for item in (release.get("assets") or [])}
        for asset_path in asset_paths:
            old = existing.get(asset_path.name)
            if old is not None:
                if not args.replace_asset:
                    raise SystemExit(f"同名产物已存在：{asset_path.name}（加 --replace-asset 覆盖）")
                api_request("DELETE", f"{API}/repos/{args.repo}/releases/assets/{old['id']}", token)
                print(f"✓ 已删除同名旧产物 {asset_path.name}")
            query = urllib.parse.urlencode({"name": asset_path.name})
            status, asset = api_request(
                "POST",
                f"{upload_url}?{query}",
                token,
                body=asset_path.read_bytes(),
                content_type="application/zip",
            )
            assert isinstance(asset, dict)
            print(f"✓ 已上传产物 {asset.get('name')}（{asset.get('size')} 字节）")

    print(f"\nRelease 页面：{release.get('html_url')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
