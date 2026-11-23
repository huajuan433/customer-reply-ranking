# -*- coding: utf-8 -*-
"""
微信客户聊天记录清洗工具

把 Moect 导出的原始 CSV 清洗成适合 AI 分析的结构化文本。
核心目标：去掉群发助手等系统噪音，保留客户真实对话，识别出值得跟进的客户。

用法:
    python clean_chats.py                          # 清洗全部会话，输出到 output/cleaned/
    python clean_chats.py --customer 2511谭兰兰      # 只清洗指定客户
    python clean_chats.py --active-days 90         # 只看最近90天有互动的客户
    python clean_chats.py --top 20                 # 生成高价值客户TOP20报告
"""
from __future__ import annotations

import argparse
import csv
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

RAW_DIR = Path(__file__).parent / "output" / "raw_csv"
OUT_DIR = Path(__file__).parent / "output" / "cleaned"

# 群发助手等无信息量的系统消息
NOISE_PATTERNS = [
    r"你通过群发助手，向[他她]发出了一条消息",
    r"以上是打招呼的消息",
    r"我通过了你的朋友验证请求",
    r"你已添加了.*为好友",
    r"你开启了朋友验证",
    r"该消息已撤回",
    r"你撤回了一条消息",
    r"对方正在输入",
]

# 这些类型整类视为噪音（可按需调整）
NOISE_TYPES = {"系统消息"}

# 媒体占位符：保留但标注，方便 AI 知道这里有图片/语音
MEDIA_LABELS = {
    "图片": "[图片]",
    "语音": "[语音]",
    "视频": "[视频]",
    "动画表情": "[表情]",
    "位置": "[位置]",
}


def is_noise(row: dict) -> bool:
    text = row.get("内容", "")
    if row.get("类型") in NOISE_TYPES:
        return True
    return any(re.search(p, text) for p in NOISE_PATTERNS)


def parse_time(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def load_session(path: Path):
    """读一个会话CSV，返回 (客户名, 消息列表, 统计信息)"""
    msgs = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            msgs.append(row)
    if not msgs:
        return None

    customer = msgs[0].get("会话", path.stem.split("_")[0])
    # 识别群聊：文件名带 @chatroom（最可靠），或会话名/文件名含群聊特征词
    # 导出工具把 @ 转义成 _at_，所以群聊文件名是 xxx_at_chatroom.csv
    is_group = (
        "chatroom" in path.name.lower()
        or "@chatroom" in customer
        or bool(re.search(r"(群聊|交流会|推进会|分会|私域圈|研究协助)", customer))
    )
    kept = []
    for row in msgs:
        if is_noise(row):
            continue
        t = parse_time(row.get("时间", ""))
        sender = row.get("发送者", "")
        is_me = row.get("是否我发送") == "是"
        mtype = row.get("类型", "文本")
        content = row.get("内容", "").strip()

        if mtype in MEDIA_LABELS:
            content = MEDIA_LABELS[mtype] + ((" " + content) if content and content != MEDIA_LABELS[mtype] else "")
        kept.append({
            "time": t,
            "sender": sender,
            "is_me": is_me,
            "type": mtype,
            "content": content,
        })

    # 统计
    customer_msgs = [m for m in kept if not m["is_me"]]
    my_msgs = [m for m in kept if m["is_me"]]
    times = [m["time"] for m in kept if m["time"]]
    stats = {
        "total": len(msgs),
        "kept": len(kept),
        "noise": len(msgs) - len(kept),
        "customer_replies": len(customer_msgs),
        "my_msgs": len(my_msgs),
        "first": min(times) if times else None,
        "last": max(times) if times else None,
        "is_group": is_group,
    }
    return customer, kept, stats


def render_session(customer: str, msgs: list, stats: dict) -> str:
    """把一个会话渲染成适合喂给AI的纯文本"""
    lines = [
        f"# 客户：{customer}",
        f"对话跨度：{stats['first'].strftime('%Y-%m-%d') if stats['first'] else '?'} ~ "
        f"{stats['last'].strftime('%Y-%m-%d') if stats['last'] else '?'}　"
        f"客户回复 {stats['customer_replies']} 条 / 我方 {stats['my_msgs']} 条 "
        f"（已过滤 {stats['noise']} 条系统噪音）",
        "",
    ]
    for m in msgs:
        who = "我" if m["is_me"] else "客户"
        ts = m["time"].strftime("%Y-%m-%d %H:%M") if m["time"] else "?"
        text = m["content"].replace("\n", " / ")
        lines.append(f"[{ts}] {who}: {text}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="微信客户聊天记录清洗工具")
    ap.add_argument("--customer", help="只清洗指定客户（文件名或客户名关键词）")
    ap.add_argument("--active-days", type=int, default=0, help="只看最近N天有互动的客户")
    ap.add_argument("--min-replies", type=int, default=0, help="只保留客户回复数>=N的会话")
    ap.add_argument("--top", type=int, default=0, help="生成互动最多的TOP N客户汇总")
    ap.add_argument("--ai-pack", type=int, default=0,
                    help="生成AI分析包：把互动最多的N个客户对话合并成一个文件，直接拖给AI")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(RAW_DIR.glob("*.csv"))
    if args.customer:
        files = [f for f in files if args.customer in f.name]
        if not files:
            print(f"[-] 没找到匹配 '{args.customer}' 的客户文件")
            return

    cutoff = None
    if args.active_days:
        cutoff = datetime.now() - timedelta(days=args.active_days)

    results = []
    total_noise = 0
    for f in files:
        got = load_session(f)
        if not got:
            continue
        customer, msgs, stats = got
        total_noise += stats["noise"]
        if cutoff and stats["last"] and stats["last"] < cutoff:
            continue
        if stats["customer_replies"] < args.min_replies:
            continue
        results.append((customer, msgs, stats, f))

    results.sort(key=lambda x: x[2]["last"] or datetime.min, reverse=True)

    # 写每个客户的清洗文件
    written = 0
    for customer, msgs, stats, f in results:
        safe = re.sub(r'[\\/:*?"<>|]', "_", customer)[:80]
        out_path = OUT_DIR / f"{safe}.txt"
        out_path.write_text(render_session(customer, msgs, stats), encoding="utf-8")
        written += 1

    print(f"[*] 扫描 {len(files)} 个会话文件")
    print(f"[*] 过滤系统噪音 {total_noise} 条")
    print(f"[*] 输出 {written} 个清洗后的会话到 {OUT_DIR}")

    # TOP 报告
    if args.top:
        individuals = [r for r in results if not r[2]["is_group"]]
        top = sorted(individuals, key=lambda x: (x[2]["customer_replies"], x[2]["last"] or datetime.min), reverse=True)[:args.top]
        lines = ["# 高价值客户排行（按客户回复数，已排除群聊）", ""]
        lines.append("| 客户 | 客户回复 | 我方消息 | 最近互动 | 跨度 |")
        lines.append("|------|---------|---------|---------|------|")
        for customer, msgs, stats, f in top:
            lines.append(
                f"| {customer} | {stats['customer_replies']} | {stats['my_msgs']} | "
                f"{stats['last'].strftime('%Y-%m-%d') if stats['last'] else '?'} | "
                f"{stats['first'].strftime('%Y-%m-%d') if stats['first'] else '?'} |"
            )
        report = OUT_DIR / "高价值客户排行.md"
        report.write_text("\n".join(lines), encoding="utf-8")
        print(f"[*] TOP报告: {report}")

    # AI 分析包：把活跃客户的对话合并成单个文件，直接拖给 AI
    if args.ai_pack:
        individuals = [r for r in results if not r[2]["is_group"]]
        top = sorted(individuals, key=lambda x: (x[2]["customer_replies"], x[2]["last"] or datetime.min), reverse=True)[:args.ai_pack]
        parts = [
            "# 客户聊天记录分析素材",
            f"共 {len(top)} 个客户，按客户回复数排序。已过滤群聊与系统群发消息。",
            "请对每个客户分析：意向强度、关注点、异议与顾虑、下一步跟进建议。",
            "",
        ]
        for customer, msgs, stats, f in top:
            parts.append("=" * 60)
            parts.append(render_session(customer, msgs, stats))
            parts.append("")
        pack = OUT_DIR / "AI分析包.txt"
        pack.write_text("\n".join(parts), encoding="utf-8")
        size_kb = pack.stat().st_size // 1024
        print(f"[*] AI分析包: {pack} ({size_kb} KB, {len(top)} 个客户)")


if __name__ == "__main__":
    main()
