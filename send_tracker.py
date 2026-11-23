# -*- coding: utf-8 -*-
"""
微信客户消息发送进度追踪器

解决的问题：挨个客户发消息时，发着发着忘了"我刚发到谁了"。
本工具只读微信本地数据库，找出每个会话里"我方最后发出的消息"，
按时间倒序排列，让你一眼看到最近发给谁、发完有没有回复、

用法:
    python send_tracker.py                # 显示最近发送的客户（默认最近2小时）
    python send_tracker.py --hours 24     # 最近24小时
    python send_tracker.py --today        # 只看今天
    python send_tracker.py --all          # 所有客户的最近一次发送
    python send_tracker.py --update       # 先重新解密数据库再分析（推荐每天用）

工作原理：只读，不模拟任何按键，不影响微信运行。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).parent
MOECT = BASE / "tools" / "Moect"
DECRYPT_DIR = MOECT / "app" / "DataBase" / "decrypted"
WXID = "wxid_x6m0ejmrbyjx22"
ACCOUNT_DIR = "D:/Users/Lenovo/Documents/xwechat_files/wxid_x6m0ejmrbyjx22_928a"


def update_snapshot():
    """重新提取密钥+解密，拿到最新数据库快照。纯本地操作。"""
    print("[*] 更新数据库快照（需微信保持登录）...")
    old = os.getcwd()
    try:
        os.chdir(MOECT)
        # 重新取密钥
        r = subprocess.run(
            [sys.executable, "-m", "wx4", "--wxid", WXID, "key"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if "密钥已保存" not in r.stdout:
            print("[-] 密钥提取失败：", r.stdout[-300:])
            return False
        # 直接调解密函数（绕过CLI的out-dir bug）
        code = (
            "from wx4 import decrypt as d, key as k, paths; "
            f"a=paths.find_account('{WXID}'); "
            "p=k.load_keys('wx4_keys.json'); "
            "keys={x:y for x,y in p.items() if not x.startswith('_')}; "
            "d.decrypt_all(keys, a['db_storage'], 'app/DataBase/decrypted')"
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        ok = "失败: 0" in r.stdout
        print("[+] 快照已更新" if ok else "[-] 解密有失败项：" + r.stdout[-300:])
        return ok
    finally:
        os.chdir(old)


def collect_last_sends():
    """扫描所有会话，找每个会话里我方最后一条消息。"""
    sys.path.insert(0, str(MOECT))
    from wx4.reader import open_data

    data = open_data(str(DECRYPT_DIR), WXID, progress=lambda m: None,
                     account_dir=ACCOUNT_DIR)
    results = []
    for s in data.sessions():
        username = s["username"]
        display = s["display"]
        # 只取最后若干条，找我方最后一条 + 其后是否有客户回复
        msgs = data.messages(username, limit=100)
        if not msgs:
            continue
        last_send = None
        replied_after = False
        for m in msgs:  # messages 按时间正序
            if m.get("is_sender"):
                last_send = m
                replied_after = False  # 遇到新的我方消息，重置
            elif last_send is not None:
                replied_after = True   # 我方发完之后客户有回复
        if last_send:
            results.append({
                "customer": display,
                "time": datetime.fromtimestamp(last_send["create_time"]),
                "text": (last_send.get("text") or "").replace("\n", " ")[:60],
                "replied": replied_after,
            })
    return results


def fmt_time(dt: datetime) -> str:
    today = datetime.now().date()
    if dt.date() == today:
        return f"今天 {dt.strftime('%H:%M')}"
    if dt.date() == today - timedelta(days=1):
        return f"昨天 {dt.strftime('%H:%M')}"
    return dt.strftime("%m-%d %H:%M")


def main():
    ap = argparse.ArgumentParser(description="微信客户消息发送进度追踪器")
    ap.add_argument("--hours", type=float, default=2, help="显示最近N小时（默认2）")
    ap.add_argument("--today", action="store_true", help="只看今天")
    ap.add_argument("--all", action="store_true", help="显示所有客户最近一次发送")
    ap.add_argument("--limit", type=int, default=20, help="最多显示几条（默认20）")
    ap.add_argument("--update", action="store_true", help="分析前先更新数据库快照")
    args = ap.parse_args()

    if args.update:
        if not update_snapshot():
            print("[-] 快照更新失败，仍用旧数据分析")
        print()

    print("[*] 扫描发送记录...")
    results = collect_last_sends()

    if args.today:
        cutoff = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    elif args.all:
        cutoff = datetime.min
    else:
        cutoff = datetime.now() - timedelta(hours=args.hours)

    filtered = [r for r in results if r["time"] >= cutoff]
    filtered.sort(key=lambda x: x["time"], reverse=True)

    print(f"\n{'='*70}")
    if args.all:
        print(f"所有客户的最近一次发送（共 {len(filtered)} 个）")
    elif args.today:
        print(f"今天发过消息的客户（共 {len(filtered)} 个）")
    else:
        print(f"最近 {args.hours} 小时发过消息的客户（共 {len(filtered)} 个）")
    print("=" * 70)

    if not filtered:
        print("这个时间段没有发送记录。试试 --hours 24 或 --all")
        return

    print(f"{'时间':<14} {'客户':<28} {'回复':<6} 消息内容")
    print("-" * 70)
    for r in filtered[:args.limit]:
        reply = "已回复" if r["replied"] else "未回"
        print(f"{fmt_time(r['time']):<14} {r['customer'][:26]:<28} {reply:<6} {r['text'][:40]}")

    print("-" * 70)
    print(f">>> 你最后一个发消息的客户是：{filtered[0]['customer']}（{fmt_time(filtered[0]['time'])}）")

    # 未回复的（发了没回音，值得关注）
    no_reply = [r for r in filtered if not r["replied"]]
    if no_reply:
        print(f">>> 其中 {len(no_reply)} 位发完还没回复，可优先跟进")


if __name__ == "__main__":
    main()
