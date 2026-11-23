# -*- coding: utf-8 -*-
"""
微信"发完自动切下一个客户 + 记日志"工具

设计原则：
1. 绝不自动模拟"发送"——避免误发消息给错误的人。发送永远由你手动按。
2. 只在你按下自定义热键后，才执行"切下一个 + 记日志"。
3. 切换键位可配置，附自检模式，先验证再启用。

热键（全局，微信在前台时生效）：
    Ctrl + Alt + N   = 记录当前已发送 → 切到下一个客户
    Ctrl + Alt + P   = 跳回上一个客户（撤销切换/回看）
    Ctrl + Alt + L   = 显示发送日志（最近发的客户）
    Ctrl + Alt + S   = 停止程序

日志位置：output/发送日志.txt

用法：
    python auto_next.py --check        # 自检：探测微信窗口与切换键（不实际发送）
    python auto_next.py --test-switch  # 实测切换键：只切会话对比标题，不发消息
    python auto_next.py                # 启动全局热键监听
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
LOG_FILE = BASE / "output" / "发送日志.txt"
LOG_FILE.parent.mkdir(exist_ok=True)

# 可配置的切换键（pyautogui/keyboard 组合）
# 默认 alt+down：微信内置的"按会话列表顺序切到下一位"，正好用于逐个客户往下处理
# 这是窗口级快捷键，鼠标停在右侧聊天内容上直接按即可，不需要先把焦点点到左边列表
# 若你这版无效，可改成 ("ctrl", "tab") 切最近会话
NEXT_KEYS = ("alt", "down")
PREV_KEYS = ("alt", "up")

state = {"history": [], "running": True}


def log_line(text: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {text}\n")
    print(f"[{ts}] {text}", flush=True)


def get_wechat_title() -> str | None:
    """读取微信当前窗口标题（通常是当前聊天对象名字）。"""
    try:
        import win32gui, win32process, psutil
        # 找出所有 Weixin.exe 的 PID
        wx_pids = set()
        for p in psutil.process_iter(["pid", "name"]):
            if p.info["name"] and p.info["name"].lower() in ("weixin.exe", "wechat.exe"):
                wx_pids.add(p.info["pid"])
        titles = []
        def cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                return
            if pid not in wx_pids:
                return
            t = win32gui.GetWindowText(hwnd)
            if t:
                # 跳过屏幕外的最小化占位窗口
                l, tp, r, bt = win32gui.GetWindowRect(hwnd)
                if l < -10000:
                    return
                titles.append((hwnd, t))
        win32gui.EnumWindows(cb, None)
        for hwnd, t in titles:
            if t == "微信":
                continue  # 主界面，未打开具体会话
            return t
    except Exception as e:
        print("读取窗口标题失败:", e)
    return None


def self_check():
    """只读自检：确认微信窗口存在、报告配置，不模拟任何按键。"""
    print("=" * 60)
    print("自检模式（不模拟任何按键，不影响微信）")
    print("=" * 60)
    print(f"当前配置的切换键: {NEXT_KEYS[0].upper()} + {NEXT_KEYS[1].upper()}")
    print(f"日志位置: {LOG_FILE}")
    title = get_wechat_title()
    print(f"微信窗口标题: {title or '未检测到（微信可能未打开或最小化）'}")
    print()
    print("使用步骤：")
    print("1. 打开微信，点开某个客户聊天")
    print("2. 手动运行本程序的自检，确认标题能读到客户名")
    print("3. 启动监听: python auto_next.py")
    print("4. 发完消息后按 Ctrl+Alt+N，程序会记日志并尝试切下一个")
    print()
    print("若切换无效，编辑本文件顶部 NEXT_KEYS / PREV_KEYS：")
    print("   微信4.x 可选组合: ('alt','down') / ('ctrl','tab') / ('pagedown',)")
    print("=" * 60)


def _front_pid() -> int:
    """当前前台窗口的进程 PID。"""
    import win32gui, win32process
    fg = win32gui.GetForegroundWindow()
    try:
        return win32process.GetWindowThreadProcessId(fg)[1]
    except Exception:
        return 0


def test_switch():
    """
    实测切换键是否生效（只切换会话，绝不发送任何消息）。
    步骤：读当前标题 -> 按一次切换键 -> 读新标题 -> 对比。
    只有当前台进程确实是微信时才模拟按键，避免误操作其它窗口。
    """
    import time, pyautogui, win32process, psutil
    print("=" * 60)
    print("切换键实测（只切换会话，不发送任何内容）")
    print("=" * 60)

    wx_pids = set()
    for p in psutil.process_iter(["pid", "name"]):
        if p.info["name"] and p.info["name"].lower() in ("weixin.exe", "wechat.exe"):
            wx_pids.add(p.info["pid"])

    fpid = _front_pid()
    if fpid not in wx_pids:
        title_hint = get_wechat_title()
        print(f"当前前台不是微信（前台PID={fpid}），为避免误操作其它窗口，中止实测。")
        print("请先：打开微信 -> 点开某个客户聊天 -> 保持微信在最前台 -> 再运行本命令。")
        if title_hint:
            print(f"（微信窗口当前标题: {title_hint}）")
        print("=" * 60)
        return

    before = get_wechat_title()
    if not before:
        print("前台是微信，但没读到具体客户标题。请点开一个客户聊天窗口再试。")
        print("=" * 60)
        return

    print(f"切换前客户: {before}")
    print(f"即将按一次: {NEXT_KEYS[0].upper()} + {NEXT_KEYS[1].upper()}")
    print("（1 秒后执行，只切会话不发送）")
    time.sleep(1.0)

    for k in ("alt", "ctrl", "shift"):
        pyautogui.keyUp(k)
    pyautogui.hotkey(*NEXT_KEYS, interval=0.08)
    time.sleep(0.6)

    after = get_wechat_title()
    print(f"切换后客户: {after or '（未读到标题）'}")
    if after and after != before:
        print("[OK] 实测成功！这个键能切到下一个客户，键盘方案可用。")
    else:
        print("[提示] 标题没变化——可能是该键在当前版本无效，或已到列表末尾。")
        print("       可编辑本文件顶部 NEXT_KEYS，改成 ('ctrl','tab') 再测。")
    print("=" * 60)


def do_next():
    """记录当前客户 + 切到下一个。"""
    import pyautogui
    title = get_wechat_title() or "（未知客户）"
    if title == "微信":
        title = "（主界面，未打开具体会话）"
    state["history"].append(title)
    log_line(f"已发送 → 切走: {title}")

    # 释放修饰键，避免上次残留导致组合键错乱
    for k in ("alt", "ctrl", "shift"):
        pyautogui.keyUp(k)
    try:
        pyautogui.hotkey(*NEXT_KEYS, interval=0.08)
    except Exception as e:
        log_line(f"切换按键失败: {e}")


def do_prev():
    """跳回上一个客户。"""
    import pyautogui
    try:
        for k in ("alt", "ctrl", "shift"):
            pyautogui.keyUp(k)
        pyautogui.hotkey(*PREV_KEYS, interval=0.08)
        log_line("跳回上一个客户")
    except Exception as e:
        log_line(f"回跳按键失败: {e}")


def show_log():
    print("\n" + "=" * 60)
    print("最近发送日志")
    print("=" * 60)
    if LOG_FILE.exists():
        lines = LOG_FILE.read_text(encoding="utf-8").strip().split("\n")
        for line in lines[-20:]:
            print(line)
    else:
        print("（暂无日志）")
    print("=" * 60 + "\n")


def listen():
    """用 keyboard 库监听全局热键。"""
    try:
        import keyboard
    except ImportError:
        print("缺少 keyboard 库，正在安装...")
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "keyboard"], check=True)
        import keyboard

    print("=" * 60)
    print("发送进度工具已启动（全局热键）")
    print("=" * 60)
    print("  Ctrl+Alt+N  = 记日志 + 切下一个客户")
    print("  Ctrl+Alt+P  = 跳回上一个客户")
    print("  Ctrl+Alt+L  = 显示发送日志")
    print("  Ctrl+Alt+S  = 退出")
    print("=" * 60)
    log_line("工具启动")

    keyboard.add_hotkey("ctrl+alt+n", do_next)
    keyboard.add_hotkey("ctrl+alt+p", do_prev)
    keyboard.add_hotkey("ctrl+alt+l", show_log)

    try:
        keyboard.wait("ctrl+alt+s")
    finally:
        log_line("工具退出")


def main():
    ap = argparse.ArgumentParser(description="微信发完自动切下一个客户+记日志")
    ap.add_argument("--check", action="store_true", help="自检模式（只读，不模拟按键）")
    ap.add_argument("--test-switch", action="store_true", help="实测切换键（只切会话对比标题，不发消息）")
    args = ap.parse_args()
    if args.check:
        self_check()
    elif args.test_switch:
        test_switch()
    else:
        listen()


if __name__ == "__main__":
    main()
