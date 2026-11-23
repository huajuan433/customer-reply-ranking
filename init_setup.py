"""
一键初始化：让这套工具能在任意一台电脑上跑起来。
做的事：查依赖 → 装依赖 → 找微信账号 → 提密钥 → 解密 → 首次扫描。
只能在「操作者自己的电脑、自己的微信账号」上运行。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).parent
MOECT = BASE / "tools" / "Moect"
TOTAL = 6


def step(n: int, msg: str) -> None:
    print(f"\n[{n}/{TOTAL}] {msg}")
    print("-" * 52)


def fail(msg: str) -> None:
    print(f"\n[失败] {msg}")
    print("修好后再双击一次「初始化.bat」。")
    sys.exit(1)


def main() -> None:
    print("=" * 52)
    print("  客户回复排行榜 - 环境初始化")
    print("=" * 52)
    print("\n本工具只读取你自己微信账号的聊天记录，不会上传任何数据。")

    # 1. Python 环境
    step(1, "检查 Python")
    print(f"Python {sys.version.split()[0]}  ({sys.executable})")
    if sys.version_info < (3, 9):
        fail("Python 版本太低，请安装 3.10 或更高版本（python.org 下载）。")

    # 2. 工具本体
    step(2, "检查导出工具文件")
    if not (MOECT / "wx4").is_dir():
        fail(f"缺少 {MOECT}\\wx4 —— 请把整个文件夹（含 tools）一起复制过来，不要只拷脚本。")
    print("导出工具就绪")

    # 3. 依赖
    step(3, "检查并安装依赖（第一次会慢一点）")
    need = {
        "psutil": "psutil",
        "pycryptodomex": "Cryptodome",
        "zstandard": "zstandard",
        "pyautogui": "pyautogui",
        "pywin32": "win32api",
        "pyperclip": "pyperclip",
        "pywebview": "webview",
    }
    missing = []
    for pip_name, mod_name in need.items():
        try:
            __import__(mod_name)
        except ImportError:
            missing.append(pip_name)
    if missing:
        print("缺少:", ", ".join(missing), "→ 正在安装…")
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", *missing])
        if r.returncode != 0:
            fail("依赖安装失败，请检查网络，或手动运行：python -m pip install " + " ".join(missing))
        print("安装完成")
    else:
        print("依赖齐全")

    # 4. 微信是否在运行
    step(4, "确认微信正在运行")
    import psutil
    running = [p.info["name"] for p in psutil.process_iter(["name"])
               if p.info["name"] and p.info["name"].lower() in ("weixin.exe", "wechat.exe")]
    if not running:
        fail("没检测到微信进程。请先打开微信并登录（保持登录状态），再运行本脚本。")
    print(f"检测到微信进程: {running[0]}")

    # 5. 自动定位账号
    step(5, "定位微信账号")
    sys.path.insert(0, str(MOECT))
    from wx4 import paths
    acc = paths.find_account(None)
    if not acc:
        fail("找不到微信数据目录。请确认装的是微信 4.x（老版本 3.x 不适用），并且登录过微信。")
    print(f"账号: {acc['wxid']}")
    print(f"数据目录: {acc['path']}")

    # 6. 提密钥 + 解密 + 首次扫描
    step(6, "提取数据库密钥并解密（需要 10-20 秒）")
    old = os.getcwd()
    try:
        os.chdir(MOECT)
        r = subprocess.run([sys.executable, "-m", "wx4", "--wxid", acc["wxid"], "key"],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if "密钥已保存" not in r.stdout:
            fail("密钥提取失败（微信需登录状态）。原始输出：\n" + (r.stdout or r.stderr)[-400:])
        print("密钥提取成功")

        code = (
            "from wx4 import decrypt as d, key as k, paths; "
            f"a=paths.find_account('{acc['wxid']}'); "
            "p=k.load_keys('wx4_keys.json'); "
            "keys={x:y for x,y in p.items() if not x.startswith('_')}; "
            "r=d.decrypt_all(keys, a['db_storage'], 'app/DataBase/decrypted'); "
            "print('DECRYPT_RESULT', len(r['ok']), len(r['failed']))"
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        if "DECRYPT_RESULT" not in r.stdout:
            fail("解密失败：\n" + (r.stdout or r.stderr)[-400:])
        print(r.stdout.strip().replace("DECRYPT_RESULT", "解密完成:"), "(成功/失败)")
    finally:
        os.chdir(old)

    # 首次扫描（先清缓存强制重扫）
    cache = BASE / "output" / "reply_ranking.json"
    if cache.exists():
        cache.unlink()
    print("\n[*] 首次扫描客户回复…")
    r = subprocess.run([sys.executable, str(BASE / "reply_ranking.py"), "--scan-only"],
                       cwd=BASE)
    if r.returncode != 0:
        fail("扫描失败，请看上面的报错。")

    print("\n" + "=" * 52)
    print("  初始化完成")
    print("=" * 52)
    print("\n下一步：双击「客户回复排行榜.bat」，浏览器会自动打开排行榜。")
    print("如果榜单是空的或客户不对，双击「设置客户规则.bat」调整识别方式。")
    print()


if __name__ == "__main__":
    main()
