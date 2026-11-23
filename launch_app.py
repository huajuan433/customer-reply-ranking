# -*- coding: utf-8 -*-
"""Friendly first-run and launch wrapper for the desktop shortcut."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
KEY_FILE = BASE / "tools" / "Moect" / "wx4_keys.json"


def run_child(args: list[str]) -> int:
    return subprocess.run(args, cwd=BASE).returncode


def main() -> int:
    print("=" * 54)
    print("  客户回复排行榜")
    print("=" * 54)
    print(f"Python {sys.version.split()[0]} · {sys.executable}")
    if sys.version_info < (3, 10):
        print("\n需要 Python 3.10 或更高版本，并加入 PATH。")
        return 2

    first_run = not KEY_FILE.exists()
    if first_run:
        print("\n首次使用：初始化当前电脑上的微信数据。")
        print("请保持微信电脑版 4.x 已登录；首次安装依赖需要网络。\n")
        code = run_child([sys.executable, str(BASE / "init_setup.py")])
        if code:
            print("\n初始化未完成。根据上方错误修复后，再双击桌面快捷方式重试。")
            return code

    print("\n正在启动排行榜并刷新微信数据，请稍候……")
    args = [sys.executable, str(BASE / "embedded_app.py")]
    code = run_child(args)
    if code:
        print("\n启动未完成。请查看上方错误信息，或阅读《使用指南_标准版.txt》。")
    return code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已退出。")
