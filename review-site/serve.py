#!/usr/bin/env python3
"""审核网站 · 独立部署入口。

网站实现放在插件的子包里（`astrbot_plugin_temp_review_group/review_web/`），
"内置"（插件自己起站点）与"独立"（这个脚本单独起进程）两种跑法**共用同一份代码**，
不存在两份实现需要同步的问题。

    # 独立跑（对外服务）
    python review-site/serve.py --host 0.0.0.0 --port 8787

    # 放在反向代理后面时
    python review-site/serve.py --host 127.0.0.1 --port 8787 --trust-proxy

    # 只看配置与插件要填的两项
    python review-site/serve.py --print-config

脚本会自动寻找 `review_web`：站点 zip 解压后是 `./review_web`，在仓库里则是
`../astrbot_plugin_temp_review_group/review_web`。详细文档见 `review_web/README.md`。
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CANDIDATES = (
    HERE / "review_web",  # 站点 zip 解压后的布局
    HERE.parent / "astrbot_plugin_temp_review_group" / "review_web",  # 仓库布局
)


def find_package() -> Path | None:
    for candidate in CANDIDATES:
        if (candidate / "cli.py").is_file():
            return candidate
    return None


def main(argv: list[str] | None = None) -> int:
    package = find_package()
    if package is None:
        print("找不到 review_web 目录，检查解压是否完整：")
        for candidate in CANDIDATES:
            print(f"  · 期望位置：{candidate}")
        return 2
    sys.path.insert(0, str(package.parent))
    from review_web.cli import main as site_main  # noqa: PLC0415

    return site_main(list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    sys.exit(main())