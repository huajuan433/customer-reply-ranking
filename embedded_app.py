# -*- coding: utf-8 -*-
"""启动本地服务并在应用窗口内显示排行榜。"""
from __future__ import annotations
import subprocess, sys, time, urllib.request
from pathlib import Path
BASE = Path(__file__).resolve().parent
URL = "http://127.0.0.1:8765/"
def main() -> int:
    try:
        import webview
    except ImportError:
        print("缺少内嵌窗口组件，请运行：python -m pip install --user pywebview")
        return 2
    server = subprocess.Popen([sys.executable, str(BASE / "reply_ranking.py"), "--update", "--no-browser", "--port", "8765"], cwd=BASE)
    try:
        for _ in range(180):
            try:
                with urllib.request.urlopen(URL + "ping", timeout=1) as response:
                    if response.status == 200: break
            except Exception: time.sleep(0.5)
        else:
            print("本地服务启动超时，请查看上方错误信息。"); return 1
        webview.create_window("客户回复排行榜", URL, width=1440, height=900, min_size=(1000, 650))
        webview.start(); return 0
    finally:
        if server.poll() is None: server.terminate()
if __name__ == "__main__": raise SystemExit(main())
