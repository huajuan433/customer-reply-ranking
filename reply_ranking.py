# -*- coding: utf-8 -*-
"""
客户回复排行榜 + 点击直达聊天
==================================================
功能：
1. 扫描微信本地数据库，统计每个客户主动回复的消息数、最后回复时间与内容
2. 生成排行榜网页，点客户名即可唤起微信、搜索并打开该客户的聊天窗口
3. 只负责"打开聊天"，绝不代替你发送任何消息——打开后你自己打字、自己按发送

用法：
    python reply_ranking.py                 # 扫描(用缓存) + 起服务 + 打开排行榜网页
    python reply_ranking.py --update        # 先重新解密数据库拿最新数据，再扫描
    python reply_ranking.py --scan-only     # 只重新扫描并生成网页，不起服务
    python reply_ranking.py --port 8765     # 指定端口

安全设计：
- 打开聊天的流程：激活微信 -> Ctrl+F -> 粘贴客户名 -> 回车打开结果
- 全程不触碰"发送"；回车仅作用于搜索框（打开第一个匹配结果）
- 只有确认微信已到前台才继续按键；否则中止，绝不盲按
- 提供 --dry-run 只打印将执行的步骤，不实际模拟按键（自测用）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).parent
MOECT = BASE / "tools" / "Moect"
DECRYPT_DIR = MOECT / "app" / "DataBase" / "decrypted"
OUT_DIR = BASE / "output"
CACHE = OUT_DIR / "reply_ranking.json"
HTML_OUT = OUT_DIR / "客户回复排行榜.html"
CONFIG_PATH = BASE / "config.json"
OUT_DIR.mkdir(exist_ok=True)

PORT = 8765
SYSTEM_TYPES = {10000, 10002, 37}
_action_lock = threading.Lock()
_server = None
_dry_run = False  # 服务级 dry-run：自测时让 /open 不实际按键
_refresh = {"running": False, "last_done": "", "count": 0, "error": ""}


def refresh_data(cfg: dict | None = None) -> None:
    """后台刷新：重取密钥 → 解密 → 重新扫描 → 重写排行榜网页。供页面「刷新数据」按钮调用。"""
    global _refresh
    cfg = cfg or load_config()
    _refresh["running"] = True
    _refresh["error"] = ""
    try:
        update_snapshot(cfg)
        rows = scan(cfg)
        CACHE.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        port = int(cfg.get("port") or PORT)
        HTML_OUT.write_text(build_html(rows, port, cfg), encoding="utf-8")
        _refresh.update(count=len(rows),
                        last_done=datetime.now().strftime("%H:%M:%S"))
        print(f"[+] 刷新完成: {len(rows)} 位客户")
    except Exception as e:
        _refresh["error"] = str(e)
        print("[-] 刷新失败:", e)
    finally:
        _refresh["running"] = False

# ----------------------------- 配置（支持给别人用） -----------------------------
DEFAULT_CONFIG = {
    # auto = 自动选最近活跃的微信账号；也可填具体 wxid
    "wxid": "auto",
    # 客户判据：number_prefix=备注以数字开头 | has_remark=有备注即客户 | keyword=备注含关键词
    "customer_rule": "number_prefix",
    "customer_keyword": "",
    # 排除标记（正则）
    "blacklist": "常青藤|别联系|不联系|转移-|删除|删\\d|测试|广告|群发助手",
    "port": PORT,
    # ---- 智能价值评分 ----
    "score": {
        # 三项行为维度的权重（内部会归一化，改比例不影响整体分数水平）
        "weights": {"recency": 40, "reply": 30, "volume": 15},
        # 最近互动的时间衰减半衰期（天）：30 天不联系则该项得分减半
        "half_life_days": 30,
        # 历史消息量封顶条数（超过即满分）
        "volume_cap": 100,
        # 客户标签的加减分（绝对分，直接作用于最终价值分）
        "tag_weights": {
            "高意向": 20, "老客户": 15, "中意向": 8,
            "低意向": -10, "沉默": -15,
        },
    },
    # 客户标签：{"username 或 客户名": ["高意向", "老客户"]}
    "tags": {},
    # 标签颜色：{"标签名": "#0f9d58"}，未登记的标签由页面自动配色
    "tag_colors": {
        "高意向": "#0f9d58", "中意向": "#2563eb", "低意向": "#8a9099",
        "已报价": "#7c3aed", "老客户": "#b45309", "价格敏感": "#db2777",
        "同行": "#64748b", "沉默": "#94a3b8",
    },
    # 跟进状态预设（可改名/改色/加减；顺序即页面展示顺序）
    "statuses": [
        {"name": "待激活", "color": "#8a9099"},
        {"name": "跟进中", "color": "#2563eb"},
        {"name": "已转化", "color": "#0f9d58"},
        {"name": "暂时搁置", "color": "#d97706"},
        {"name": "已流失", "color": "#d93a2b"},
    ],
    # 每个客户的跟进状态：{"username": "跟进中"}，没有键 = 未设置
    "status": {},
    # 快捷回复模板（话术）：页面里可增删改、置顶、按场景分类
    "templates": [
        {"id": "tpl-first", "title": "首次激活 · 开场白", "cat": "首次激活", "pinned": True,
         "text": "{{客户名}}老师您好，我是[你的名字]。上次跟您聊到「{{上次话题}}」，想问下这块现在进展怎么样？我这边能提供 JCR 分区期刊的投稿支持和实验协助，需要的话随时找我。"},
        {"id": "tpl-revive", "title": "久未联系 · 重新破冰", "cat": "首次激活", "pinned": False,
         "text": "{{客户名}}老师好，好久没打扰您了（上次联系是{{最后联系}}）。最近我们这边有几个科室做的方向跟您之前提的{{上次话题}}挺接近，想跟您同步一下，看有没有能配合的地方。"},
        {"id": "tpl-progress", "title": "询问方案进度", "cat": "跟进中", "pinned": False,
         "text": "{{客户名}}老师，上次跟您说的那个方案您看过了吗？如果时间上比较紧，我可以先出一版简单的思路给您参考，不占用您太多时间。"},
        {"id": "tpl-material", "title": "补充案例与报价", "cat": "跟进中", "pinned": False,
         "text": "{{客户名}}老师，补充一份资料给您，是同类方向的案例和周期报价，您方便时看一下，有不清楚的地方我随时解释。"},
        {"id": "tpl-push", "title": "档期提醒 · 促单", "cat": "促单", "pinned": False,
         "text": "{{客户名}}老师，跟您确认下时间：这个月排版名额还剩几个，如果这周定下来，能赶在{{日期}}之后的批次。需要我先帮您占一个吗？"},
        {"id": "tpl-after", "title": "进度同步", "cat": "售后", "pinned": False,
         "text": "{{客户名}}老师，跟您同步下进度：目前已完成[当前阶段]，预计[完成时间]出结果，中间有任何情况我第一时间跟您说。"},
        {"id": "tpl-review", "title": "老客户回访", "cat": "售后", "pinned": False,
         "text": "{{客户名}}老师，上次的事都顺利吧？这段时间如果身边同事有需要，也可以介绍给我，我一样给到位。"},
    ],
    "template_cats": ["首次激活", "跟进中", "促单", "售后"],
    # 今日必跟（今日待办）
    #   manual：手动加进来的 username 列表（跨天保留）
    #   done：{username: 时间戳}，标记「今日已跟」的时间；用于避免刚跟完又被自动推荐
    #   snooze：{username: "YYYY-MM-DD"}，今天先不管
    #   rule：自动推荐规则（超过 min_days 天没互动、价值分 >= min_score、跳过指定状态）
    "todo": {
        "manual": [],
        "done": {},
        "snooze": {},
        # 当天自动生成的名单（跨天或改规则时重生成），保证「跟一个少一个」
        "today": {"date": "", "users": []},
        "rule": {
            "enabled": True,
            "min_days": 3,
            "min_score": 45,
            "limit": 15,
            "skip_status": ["已转化", "已流失", "暂时搁置"],
        },
    },
    # 手动加减分：{"username 或 客户名": 5}
    "manual": {},
    # 客户备注（多条历史）：{"username": [{"id": "ab12cd34", "text": "...", "ts": 1763...}]}
    "notes": {},
}

_DEEP_KEYS = ("score", "tags", "manual", "tag_colors", "status", "todo", "notes")


def save_config(cfg: dict) -> None:
    """原子写 config.json，避免写到一半损坏。"""
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def load_config() -> dict:
    """读 config.json；不存在就写一份默认的出来。坏文件按默认值兜底。

    score 段做深合并，保证旧版 config.json（没有评分字段）也能拿到完整默认值。
    """
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝，避免改到默认值
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(user, dict):
                for k, v in user.items():
                    if k in _DEEP_KEYS and isinstance(v, dict):
                        if k == "score" and isinstance(cfg.get("score"), dict):
                            merged = dict(cfg["score"])
                            for sk, sv in v.items():
                                if sk == "weights" or sk == "tag_weights":
                                    base = dict(merged.get(sk) or {})
                                    if isinstance(sv, dict):
                                        base.update(sv)
                                    merged[sk] = base
                                else:
                                    merged[sk] = sv
                            cfg["score"] = merged
                        elif k == "todo" and isinstance(cfg.get("todo"), dict):
                            # todo 里 rule 也要深合并，老配置（没有 rule 子项）才能拿到默认规则
                            merged = dict(cfg["todo"])
                            for tk, tv in v.items():
                                if tk == "rule" and isinstance(tv, dict):
                                    base = dict(merged.get("rule") or {})
                                    base.update(tv)
                                    merged["rule"] = base
                                else:
                                    merged[tk] = tv
                            cfg["todo"] = merged
                        else:
                            cfg[k] = v
                    elif k in DEFAULT_CONFIG or k in ("moect_dir",):
                        cfg[k] = v
        except Exception as e:
            print(f"[!] config.json 读取失败({e})，使用默认配置")
    else:
        try:
            CONFIG_PATH.write_text(
                json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2),
                encoding="utf-8")
            print(f"[*] 已生成默认配置: {CONFIG_PATH}")
        except Exception:
            pass
    return cfg


def resolve_account(cfg: dict) -> dict:
    """自动定位微信账号目录：优先当前登录/最近活跃，无需写死。"""
    sys.path.insert(0, str(MOECT))
    from wx4 import paths

    want = str(cfg.get("wxid") or "auto")
    acc = paths.find_account(None if want in ("auto", "", "None") else want)
    if not acc:
        raise RuntimeError(
            "没找到微信数据目录。请确认：1) 装的是微信 4.x；"
            "2) 至少登录过微信一次；3) 数据目录没被手动删掉。")
    return acc


# ----------------------------- 数据扫描 -----------------------------
def update_snapshot(cfg: dict | None = None) -> bool:
    """重新取密钥 + 解密，拿到最新数据库快照。纯本地操作。"""
    import os, subprocess
    cfg = cfg or load_config()
    try:
        acc = resolve_account(cfg)
    except Exception as e:
        print("[-]", e)
        return False
    wxid = acc["wxid"]
    print(f"[*] 更新数据库快照（账号 {wxid}，需微信保持登录）...")
    old = os.getcwd()
    try:
        os.chdir(MOECT)
        r = subprocess.run(
            [sys.executable, "-m", "wx4", "--wxid", wxid, "key"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if "密钥已保存" not in r.stdout:
            print("[-] 密钥提取失败（微信需登录并保持运行）:", r.stdout[-300:])
            return False
        code = (
            "from wx4 import decrypt as d, key as k, paths; "
            f"a=paths.find_account('{wxid}'); "
            "p=k.load_keys('wx4_keys.json'); "
            "keys={x:y for x,y in p.items() if not x.startswith('_')}; "
            "r=d.decrypt_all(keys, a['db_storage'], 'app/DataBase/decrypted'); "
            "print('DECRYPT_RESULT', len(r['ok']), len(r['failed']))"
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        m = re.search(r"DECRYPT_RESULT\s+(\d+)\s+(\d+)", r.stdout)
        ok = bool(m) and int(m.group(2)) == 0
        print("[+] 快照已更新（%s 个库）" % m.group(1) if ok
              else "[-] 解密有失败项:" + r.stdout[-300:])
        return ok
    finally:
        os.chdir(old)


def _is_customer(remark: str, display: str, cfg: dict) -> bool:
    """
    按配置判断某个联系人是否算"客户"。
      number_prefix  备注以编号开头（如 2505山东省立…），默认
      has_remark     有备注即算客户
      keyword        备注或显示名包含指定关键词
    """
    remark = (remark or "").strip()
    rule = cfg.get("customer_rule", "number_prefix")
    if rule == "has_remark":
        return bool(remark)
    if rule == "keyword":
        kw = str(cfg.get("customer_keyword") or "").strip()
        return bool(kw) and (kw in remark or kw in (display or ""))
    return bool(re.match(r"^\d{3,6}", remark))


def _blacklist(text: str, cfg: dict | None = None) -> bool:
    """明确的非客户标记：同事、别联系、测试号等（规则可在 config.json 改）。"""
    pat = str((cfg or DEFAULT_CONFIG).get("blacklist") or "")
    return bool(pat) and bool(re.search(pat, text or ""))


def scan(cfg: dict | None = None) -> list[dict]:
    """
    扫描会话，按 config.json 的客户口径统计回复数和最后回复。
    只读一遍数据库：先把"非群/非黑名单/有回复"的会话全收进来，再按规则筛客户。
    若按编号规则一个都没筛出来，自动降级为"有备注即客户"，避免别人拿到是空榜。
    """
    cfg = cfg or load_config()
    acc = resolve_account(cfg)
    sys.path.insert(0, str(MOECT))
    from wx4.reader import open_data

    print(f"[*] 使用账号: {acc['wxid']}")
    data = open_data(str(DECRYPT_DIR), acc["wxid"], progress=lambda m: None,
                     account_dir=acc["path"])
    now = datetime.now()
    cutoff30 = (now - timedelta(days=30)).timestamp()
    excluded = {"group": 0, "not_customer": 0, "blacklist": 0, "no_reply": 0}
    candidates = []

    for s in data.sessions():
        if s.get("is_group"):
            excluded["group"] += 1
            continue
        username = s["username"]
        contact = data.contacts.get(username) or {}
        remark = str(contact.get("remark") or "").strip()
        display = (s.get("display") or remark or username).strip()

        if _blacklist(remark, cfg) or _blacklist(display, cfg):
            excluded["blacklist"] += 1
            continue

        msgs = data.messages(username, limit=None)
        replies = [m for m in msgs
                   if not m.get("is_sender") and m.get("type") not in SYSTEM_TYPES]
        mine = [m for m in msgs
                if m.get("is_sender") and m.get("type") not in SYSTEM_TYPES]
        # 我发过、对方没回的客户也要纳入（"待跟进"和"最近我发的"需要它们）
        if not replies and not mine:
            excluded["no_reply"] += 1
            continue

        replies.sort(key=lambda m: m.get("create_time", 0))
        mine.sort(key=lambda m: m.get("create_time", 0))

        def _snippet(m):
            return re.sub(r"\s+", " ", str(m.get("text") or "")).strip()[:80] or "[无文本内容]"

        if replies:
            last = replies[-1]
            recent = sum(1 for m in replies if m.get("create_time", 0) >= cutoff30)
            last_ts = last["create_time"]
            last_time = datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d %H:%M")
            last_text = _snippet(last)
        else:
            recent, last_ts, last_time, last_text = 0, 0, "", ""

        if mine:
            my_last = mine[-1]
            my_ts = my_last["create_time"]
            my_time = datetime.fromtimestamp(my_ts).strftime("%Y-%m-%d %H:%M")
            my_text = _snippet(my_last)
        else:
            my_ts, my_time, my_text = 0, "", ""

        candidates.append({
            "name": remark or display,
            "remark": remark,
            "display": display,
            "username": username,
            "replies": len(replies),
            "recent30": recent,
            "last_time": last_time,
            "last_ts": last_ts,
            "last_text": last_text,
            "replied": bool(replies),
            "my_msgs": len(mine),
            "my_last_ts": my_ts,
            "my_last_time": my_time,
            "my_last_text": my_text,
        })

    # 按配置的客户口径筛选
    rule = cfg.get("customer_rule", "number_prefix")
    rows = [c for c in candidates if _is_customer(c["remark"], c["display"], cfg)]
    effective_rule = rule
    if not rows and rule == "number_prefix" and candidates:
        print("[!] 没有任何备注以编号开头——已自动改用「有备注即客户」口径（可在 config.json 固定）")
        rows = candidates
        effective_rule = "has_remark(自动降级)"
    excluded["not_customer"] = len(candidates) - len(rows)

    rows.sort(key=lambda r: (r["replies"], r["last_ts"]), reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    print(f"[*] 口径统计: 客户入榜 {len(rows)}（其中有我方发送 {sum(1 for r in rows if r['my_msgs'])}）"
          f" | 排除-群 {excluded['group']} -非客户 {excluded['not_customer']} "
          f"-黑名单 {excluded['blacklist']} -无任何往来 {excluded['no_reply']}")
    for r in rows:
        r.pop("remark", None)
        r.pop("display", None)
    CACHE.with_name("reply_ranking_meta.json").write_text(
        json.dumps({"scope": "客户：按客户口径筛选且非黑名单，有往来（对方回复或我方发送）",
                    "rule": effective_rule,
                    "wxid": acc["wxid"],
                    "count": len(rows),
                    "with_my_msgs": sum(1 for r in rows if r["my_msgs"]),
                    "excluded": excluded,
                    "scan_time": now.strftime("%Y-%m-%d %H:%M")},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    return rows


def load_or_scan(force: bool = False, cfg: dict | None = None) -> list[dict]:
    if not force and CACHE.exists():
        try:
            rows = json.loads(CACHE.read_text(encoding="utf-8"))
            if rows and "my_last_ts" not in rows[0]:
                print("[*] 缓存是旧格式（缺\"我发送时间\"字段），自动重扫一次…")
            else:
                print(f"[*] 读取缓存: {len(rows)} 位客户（加 --rescan 可刷新）")
                return rows
        except Exception as e:
            print("[-] 缓存读取失败，重新扫描:", e)
    print("[*] 扫描客户回复记录（约 15 秒）...")
    t0 = time.time()
    rows = scan(cfg)
    CACHE.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[+] 扫描完成: {len(rows)} 位客户有回复，耗时 {time.time()-t0:.1f}s")
    return rows


def demo_rows() -> list[dict]:
    """返回完全虚构的演示数据；不会读取微信数据库或写入用户数据。"""
    now = datetime.now()
    samples = [("陈总-设备采购报价",47,2,2,0),("林经理-年度续费确认",39,12,1,0),("周女士-定制方案沟通",31,60,1,0),("王主管-试用账号跟进",24,180,1,0),("赵先生-展会名片初联",19,360,0,1),("刘总-样品寄送确认",16,720,0,1),("何经理-合同条款沟通",14,960,0,1),("孙女士-预算审批进度",12,1320,0,1),("高工-接口方案评估",10,1560,0,1),("黄总-季度采购计划",9,1920,0,1),("郑经理-售后续费咨询",8,2280,0,1),("何女士-门店开通申请",7,2880,0,1),("吴主管-培训排期确认",6,3360,0,1),("谢总-报价单补充资料",5,4320,0,1),("许经理-竞品信息交流",4,5760,0,1),("唐女士-活动报名咨询",3,7200,0,1),("冯先生-资料下载回访",2,9600,0,1),("罗经理-初次需求了解",1,12000,0,1)]
    rows = []
    for rank, (name, replies, minutes, recent, mine) in enumerate(samples, 1):
        ts = (now - timedelta(minutes=minutes)).timestamp()
        rows.append({"name":name,"username":f"demo_{rank:02d}","replies":replies,"recent30":recent,"last_time":datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M"),"last_ts":ts,"last_text":"演示数据：客户正在沟通中","replied":True,"my_msgs":mine,"my_last_ts":ts-1800 if mine else 0,"my_last_time":datetime.fromtimestamp(ts-1800).strftime("%Y-%m-%d %H:%M") if mine else "","my_last_text":"演示数据：已发送跟进消息" if mine else "","rank":rank})
    return rows


# ----------------------- 客户详情：按需读某个人的消息 -----------------------
# 只为侧边栏的互动时间线服务：常驻一个数据库句柄，单个会话 ~0.07s，缓存 5 分钟。
_READER: dict = {"data": None, "wxid": ""}
_READER_LOCK = threading.Lock()
_TL_CACHE: dict[str, dict] = {}
_TL_LOCK = threading.Lock()
TL_TTL = 300          # 同一客户重复打开不重复读盘
TL_KEEP = 60          # 最多缓存 60 个客户


def _get_reader(cfg: dict | None = None):
    """拿一个常驻的数据库读取器（第一次约 0.7s，之后复用）。"""
    with _READER_LOCK:
        if _READER["data"] is not None:
            return _READER["data"]
        cfg = cfg or load_config()
        acc = resolve_account(cfg)
        sys.path.insert(0, str(MOECT))
        from wx4.reader import open_data
        print(f"[*] 时间线读取器已连接: {acc['wxid']}", flush=True)
        _READER["data"] = open_data(str(DECRYPT_DIR), acc["wxid"],
                                    progress=lambda m: None, account_dir=acc["path"])
        _READER["wxid"] = acc["wxid"]
        return _READER["data"]


def customer_timeline(username: str, limit: int = 200) -> dict:
    """
    取某个客户的完整互动时间线（我发 / 对方回，去掉系统消息）。
    返回 {"total": 全部条数, "msgs": 最近 limit 条（时间正序）, "kind_count": {...}}
    """
    now = time.time()
    with _TL_LOCK:
        hit = _TL_CACHE.get(username)
        if hit and now - hit["ts"] < TL_TTL and hit.get("limit") == limit:
            return hit
    try:
        data = _get_reader()
        with _READER_LOCK:
            raw = data.messages(username, limit=None)
    except Exception as e:
        return {"error": f"读取聊天记录失败：{e}", "total": 0, "msgs": []}

    msgs = []
    for m in raw:
        if m.get("type") in SYSTEM_TYPES:
            continue
        text = re.sub(r"\s+", " ", str(m.get("text") or "")).strip()
        msgs.append({
            "ts": int(m.get("create_time") or 0),
            "mine": bool(m.get("is_sender")),
            "kind": str(m.get("kind") or ""),
            "text": text[:600] or "[无文本内容]",
        })
    msgs.sort(key=lambda x: x["ts"])
    out = {"total": len(msgs), "msgs": msgs[-limit:], "limit": limit, "ts": now,
           "mine_total": sum(1 for m in msgs if m["mine"])}
    with _TL_LOCK:
        _TL_CACHE[username] = out
        if len(_TL_CACHE) > TL_KEEP:
            for k, _ in sorted(_TL_CACHE.items(), key=lambda kv: kv[1]["ts"])[:10]:
                _TL_CACHE.pop(k, None)
    return out


def add_note(cfg: dict, username: str, text: str) -> list[dict]:
    """给客户追加一条备注，返回该客户全部备注（新的在前）。"""
    text = (text or "").strip()[:800]
    notes = cfg.setdefault("notes", {})
    arr = notes.setdefault(username, [])
    if text:
        arr.append({"id": uuid.uuid4().hex[:8], "text": text, "ts": int(time.time())})
    notes[username] = arr[-200:]          # 单个客户最多留 200 条
    save_config(cfg)
    return sorted(notes[username], key=lambda x: x["ts"], reverse=True)


def del_note(cfg: dict, username: str, note_id: str) -> list[dict]:
    """删除一条备注。"""
    notes = cfg.setdefault("notes", {})
    arr = [n for n in notes.get(username, []) if str(n.get("id")) != str(note_id)]
    if arr:
        notes[username] = arr
    else:
        notes.pop(username, None)
    save_config(cfg)
    return sorted(arr, key=lambda x: x["ts"], reverse=True)


# ----------------------------- 网页生成 -----------------------------
def score_payload(cfg: dict) -> dict:
    """传给页面的评分配置（权重/参数/标签/手动分）+ 标签颜色与跟进状态。"""
    sc = cfg.get("score") or {}
    colors = dict(DEFAULT_CONFIG["tag_colors"])
    user_colors = cfg.get("tag_colors")
    if isinstance(user_colors, dict):
        colors.update({str(k): str(v) for k, v in user_colors.items() if v})
    statuses = cfg.get("statuses")
    if not isinstance(statuses, list) or not statuses:
        statuses = DEFAULT_CONFIG["statuses"]
    return {
        "weights": sc.get("weights") or {},
        "half_life_days": sc.get("half_life_days", 14),
        "volume_cap": sc.get("volume_cap", 100),
        "tag_weights": sc.get("tag_weights") or {},
        "tags": cfg.get("tags") or {},
        "manual": cfg.get("manual") or {},
        "tag_colors": colors,
        "statuses": statuses,
        "status": cfg.get("status") or {},
        "templates": cfg.get("templates") or [],
        "template_cats": cfg.get("template_cats") or ["首次激活", "跟进中", "促单", "售后"],
        "todo": cfg.get("todo") or {},
        # 备注只按客户存，新的在前，侧边栏直接用
        "notes": {k: sorted(v, key=lambda x: x.get("ts", 0), reverse=True)
                  for k, v in (cfg.get("notes") or {}).items() if isinstance(v, list)},
    }


def short_wxid(w: str) -> str:
    """微信号太长，只留前后几位，页面上一眼能认出是哪个号就行。"""
    w = (w or "").strip()
    if not w:
        return "—"
    if len(w) <= 13:
        return w
    return w[:8] + "…" + w[-4:]


def build_html(rows: list[dict], port: int = PORT, cfg: dict | None = None) -> str:
    data_json = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")
    cfg = cfg or load_config()
    score_json = json.dumps(score_payload(cfg), ensure_ascii=False).replace("</", "<\\/")
    scan_time = "—"
    wx_show = "—"
    try:
        meta = json.loads(CACHE.with_name("reply_ranking_meta.json").read_text(encoding="utf-8"))
        scan_time = meta.get("scan_time") or scan_time
        wx_show = short_wxid(meta.get("wxid") or "")
    except Exception:
        pass
    return (HTML_TEMPLATE
            .replace("__DATA__", data_json)
            .replace("__SCORECFG__", score_json)
            .replace("__PORT__", str(port))
            .replace("__WX__", wx_show)
            .replace("__SCANTIME__", scan_time))


def rewrite_html(cfg: dict | None = None) -> None:
    """按当前缓存数据 + 新配置重写网页（改权重/标签后调用，保证刷新页面设置不丢）。"""
    cfg = cfg or load_config()
    try:
        rows = json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        return
    try:
        HTML_OUT.write_text(build_html(rows, int(cfg.get("port") or PORT), cfg),
                            encoding="utf-8")
    except Exception as e:
        print("[!] 重写网页失败:", e)


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>客户回复排行榜</title>
<style>
:root{--bg:#f7f8fa;--card:#fff;--line:#e6e8eb;--text:#1f2329;--muted:#8a9099;
--green:#0f9d58;--greenbg:#e8f5ee;--red:#d93a2b;--redbg:#fdecea;--blue:#2563eb;}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--text);font-size:14px;line-height:1.5}
.wrap{max-width:1000px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:22px;font-weight:600;margin-bottom:4px}
.sub{color:var(--muted);font-size:13px;margin-bottom:20px}
.bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
.bar input{flex:1;min-width:170px;padding:9px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);font-size:14px}
.seg{display:flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:var(--card)}
.seg button{padding:9px 11px;border:0;background:transparent;cursor:pointer;color:var(--muted);font-size:13px}
.seg button.on{background:var(--greenbg);color:var(--green);font-weight:600}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}
.chips button{padding:6px 13px;border:1px solid var(--line);background:var(--card);border-radius:20px;cursor:pointer;color:var(--muted);font-size:12.5px}
.chips button.on{background:#eef2ff;border-color:#c7d2fe;color:#3730a3;font-weight:600}
.chips button .c{opacity:.7;margin-left:4px}
.refresh{padding:9px 11px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--text);cursor:pointer;font-size:13px;white-space:nowrap}
.refresh:hover{border-color:var(--green);color:var(--green)}
.refresh:disabled{opacity:.55;cursor:wait}
/* 价值分 */
.vscore{flex:none;width:52px;height:44px;border-radius:10px;display:flex;flex-direction:column;
  align-items:center;justify-content:center;cursor:pointer;font-weight:700;font-size:17px;line-height:1;
  background:#f2f3f5;color:#5b6270;border:1px solid transparent}
.vscore span{font-size:10px;font-weight:400;opacity:.75;margin-top:2px}
.vscore.hi{background:var(--greenbg);color:var(--green);border-color:#bfe6d0}
.vscore.mid{background:#fff8e6;color:#a5731a;border-color:#f0e0b8}
.vscore.lo{background:#fdecea;color:#b8352a;border-color:#f7cdc8}
.vscore:hover{filter:brightness(.97)}
.tg{display:inline-block;padding:1px 6px;border-radius:4px;font-size:11px;background:#eef2ff;color:#3730a3;margin-left:6px;font-weight:400}
.tg.pos{background:var(--greenbg);color:var(--green)}
.tg.neg{background:var(--redbg);color:var(--red)}
.tg.add{background:#f2f3f5;color:var(--muted);cursor:pointer}
.tg.add:hover{background:#e6e8eb}
/* 权重面板 */
.panel{display:none;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:16px}
.panel.show{display:block}
.ph{display:flex;align-items:center;gap:10px;margin-bottom:12px;font-weight:600;font-size:14px}
.ph .hint{font-weight:400;font-size:12px;color:var(--muted)}
.ph .x{margin-left:auto;border:1px solid var(--line);background:transparent;border-radius:6px;padding:4px 10px;cursor:pointer;color:var(--muted);font-size:12px}
.prow{display:flex;align-items:center;gap:12px;margin-bottom:9px;font-size:13px}
.prow label{width:82px;color:var(--muted);flex:none}
.prow input[type=range]{flex:1;accent-color:var(--green)}
.prow b{width:52px;text-align:right;font-size:13px}
.tagw{display:flex;gap:10px;flex-wrap:wrap}
.tagw label{width:auto;display:flex;align-items:center;gap:5px;color:var(--text)}
.tagw input{width:52px;padding:4px 6px;border:1px solid var(--line);border-radius:6px;font-size:13px;text-align:center}
.pfoot{display:flex;gap:10px;margin-top:12px;border-top:1px solid var(--line);padding-top:12px}
.pfoot button{padding:7px 14px;border:1px solid var(--line);background:var(--card);border-radius:8px;cursor:pointer;font-size:13px}
.pfoot button.primary{background:var(--green);border-color:var(--green);color:#fff}
.stat{display:flex;gap:24px;flex-wrap:wrap;margin-bottom:18px;font-size:13px;color:var(--muted)}
.stat b{color:var(--text);font-size:16px;margin-right:4px}
.row{display:flex;align-items:center;gap:14px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:8px;transition:.15s}
.row:hover{border-color:var(--green);box-shadow:0 2px 10px rgba(15,157,88,.08)}
.rank{width:34px;text-align:center;color:var(--muted);font-size:13px;flex:none}
.rank.top{color:var(--red);font-weight:700}
.info{flex:1;min-width:0}
.name{font-weight:600;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.last{color:var(--muted);font-size:12px;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.last .tag{display:inline-block;padding:0 5px;border-radius:4px;font-size:11px;margin-right:5px;background:#eef2ff;color:#3730a3}
.last .tag.rep{background:var(--greenbg);color:var(--green)}
.last .tag.none{background:#f2f3f5;color:var(--muted)}
.mine{color:#3a4250}
.badge{flex:none;text-align:center;min-width:66px}
.badge .n{font-size:17px;font-weight:700;color:var(--green)}
.badge .u{font-size:11px;color:var(--muted)}
.fresh{flex:none;font-size:12px;color:var(--muted);min-width:78px;text-align:right}
.fresh.hot{color:var(--red);font-weight:600}
.open{flex:none;padding:8px 16px;border:0;border-radius:8px;background:var(--green);color:#fff;font-size:13px;cursor:pointer}
.open:hover{filter:brightness(.94)}
.open:disabled{opacity:.5;cursor:wait}
.tip{background:#fff8e6;border:1px solid #f5e2b0;color:#8a6d1f;padding:10px 14px;border-radius:8px;font-size:12px;margin-bottom:18px}
#toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);background:#1f2329;color:#fff;padding:10px 18px;border-radius:8px;font-size:13px;opacity:0;transition:.2s;pointer-events:none;z-index:60}
#toast.show{opacity:1;pointer-events:auto}
.empty{text-align:center;color:var(--muted);padding:40px}
/* ---------- 标签 / 跟进状态 / 抽屉 / 批量操作 ---------- */
.name{display:flex;align-items:center;gap:5px;min-width:0;cursor:pointer}
.name:hover .nm{color:var(--green)}
.nm{font-weight:600;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:0 1 auto;min-width:90px}
.fbar{display:flex;gap:8px;flex-wrap:wrap;align-items:flex-start;margin-bottom:10px}
.fbar .lab{color:var(--muted);flex:none;font-size:12.5px;line-height:26px;min-width:32px}
.fbar .chips{margin-bottom:0}
.chips button .dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;vertical-align:1px}
.tagchip{display:inline-flex;align-items:center;gap:4px;flex:none;padding:0 8px;border-radius:11px;font-size:11.5px;line-height:19px;font-weight:500;white-space:nowrap}
.tagchip b.rm{cursor:pointer;opacity:.55;font-weight:700;margin-left:1px}
.tagchip b.rm:hover{opacity:1}
.stchip{display:inline-flex;align-items:center;gap:5px;flex:none;padding:0 7px;border-radius:11px;font-size:11.5px;line-height:19px;background:#f2f3f5;color:#5b6270;white-space:nowrap}
.stchip i{width:6px;height:6px;border-radius:50%;flex:none}
.ck{flex:none;width:16px;display:flex;align-items:center;opacity:0;transition:opacity .15s}
.row:hover .ck,.row[data-sel="1"] .ck{opacity:1}
.ck input{width:15px;height:15px;accent-color:var(--green);cursor:pointer}
.row[data-sel="1"]{border-color:#b7e4c9;background:#f7fdf9}
.ckall{display:inline-flex;align-items:center;gap:6px;cursor:pointer;color:var(--muted);font-size:12px}
.ckall input{accent-color:var(--green);cursor:pointer}
.overlay{position:fixed;inset:0;background:rgba(20,23,28,.32);opacity:0;pointer-events:none;transition:opacity .18s;z-index:30}
.overlay.show{opacity:1;pointer-events:auto}
.drawer{position:fixed;top:0;right:0;height:100%;width:472px;max-width:94vw;background:var(--card);
  box-shadow:-10px 0 30px rgba(0,0,0,.14);transform:translateX(102%);z-index:31;display:flex;flex-direction:column;
  transition:transform .22s cubic-bezier(.2,.8,.3,1)}
.drawer.show{transform:none}
#dbody{display:flex;flex-direction:column;height:100%;min-height:0}
.dh{display:flex;align-items:flex-start;gap:10px;padding:18px 18px 14px;border-bottom:1px solid var(--line)}
.dname{font-size:16px;font-weight:600;line-height:1.35;word-break:break-all}
.dsub{color:var(--muted);font-size:12px;margin-top:5px}
.dh .x{flex:none;margin-left:auto;border:1px solid var(--line);background:transparent;border-radius:6px;padding:4px 10px;cursor:pointer;color:var(--muted);font-size:12px}
.db{flex:1;overflow:auto;padding:16px 18px}
.sec{margin-bottom:22px}
.sech{font-size:12.5px;font-weight:600;color:#5b6270;margin-bottom:9px;display:flex;align-items:center;gap:8px}
.sech .hint{font-weight:400;color:var(--muted);font-size:11.5px}
.stbtns{display:flex;flex-wrap:wrap;gap:7px}
.stbtn{display:inline-flex;align-items:center;gap:6px;padding:7px 12px;border:1px solid var(--line);background:var(--card);border-radius:8px;cursor:pointer;font-size:12.5px;color:var(--text)}
.stbtn i{width:7px;height:7px;border-radius:50%;flex:none}
#drawer .stbtn{padding:6px 9px;font-size:12px;gap:5px}
#drawer .sec{margin-bottom:15px}
#drawer .db{padding:14px 18px}
.stbtn:hover{border-color:#c9ced6}
.stbtn.on{border-color:currentColor;background:#fbfbfc;font-weight:600}
.dchips{display:flex;flex-wrap:wrap;gap:7px;min-height:24px}
.qtag{display:inline-flex;align-items:center;gap:5px;padding:4px 10px;border:1px dashed var(--line);background:var(--card);border-radius:14px;cursor:pointer;font-size:12px;color:#5b6270}
.qtag:hover{border-color:var(--green);color:var(--green);border-style:solid}
.qtag i{width:6px;height:6px;border-radius:50%;flex:none}
.newtag{display:flex;align-items:center;gap:8px;margin-top:13px;flex-wrap:wrap}
.newtag input[type=text]{flex:1;min-width:110px;padding:7px 10px;border:1px solid var(--line);border-radius:8px;font-size:13px}
.sw{display:flex;gap:5px;align-items:center}
.sw i{width:16px;height:16px;border-radius:50%;cursor:pointer;border:2px solid transparent;box-sizing:border-box}
.sw i.on{border-color:#1f2329;transform:scale(1.12)}
.newtag .mini{padding:7px 12px;border:1px solid var(--line);background:var(--card);border-radius:8px;cursor:pointer;font-size:12.5px}
.newtag .mini.primary{background:var(--green);border-color:var(--green);color:#fff}
.df{display:flex;gap:9px;padding:12px 18px;border-top:1px solid var(--line);background:#fbfbfc}
.df button{flex:1;padding:9px 14px;border:1px solid var(--line);border-radius:8px;background:var(--card);cursor:pointer;font-size:13px}
.df button.primary{background:var(--green);border-color:var(--green);color:#fff;font-weight:600}
/* 客户详情：时间线 + 备注 */
/* 时间线限高内滚：不然 40 条会把备注挤到很下面 */
#tlc{max-height:clamp(150px,26vh,360px);overflow:auto;padding:2px 6px 2px 2px;overscroll-behavior:contain}
#tlc::-webkit-scrollbar{width:6px}
#tlc::-webkit-scrollbar-thumb{background:#dcdfe4;border-radius:3px}
.tlday{font-size:11px;color:var(--muted);margin:2px 0 8px;padding-left:2px}
.tlitem{position:relative;padding:0 0 13px 13px;border-left:2px solid #eceef1;margin-left:5px}
.tlitem:last-child{border-left-color:transparent;padding-bottom:2px}
.tlitem:before{content:"";position:absolute;left:-5px;top:4px;width:8px;height:8px;border-radius:50%;
  background:#c9ced6;box-shadow:0 0 0 2px var(--card)}
.tlitem.you:before{background:var(--green)}
.tlhead{display:flex;align-items:center;gap:7px;flex-wrap:wrap}
.tlwho{font-size:10.5px;line-height:16px;padding:0 5px;border-radius:4px;background:#f2f3f5;color:#6b7280;flex:none}
.tlwho.you{background:var(--greenbg);color:var(--green)}
.tlwhen{font-size:11.5px;color:var(--muted)}
.tltext{font-size:12.5px;line-height:1.62;color:#2b3038;margin-top:3px;word-break:break-word;white-space:pre-wrap}
.tlph{color:var(--muted);font-size:12.5px;line-height:1.7;padding:2px 0 6px}
.tlmore{border:1px solid var(--line);background:var(--card);border-radius:6px;padding:3px 9px;
  font-size:11.5px;color:#5b6270;cursor:pointer;font-weight:400;margin-left:auto}
.tlmore:hover{border-color:#c9ced6;color:var(--text)}
.ntlist{display:flex;flex-direction:column;gap:8px}
.ntitem{background:#fbfbfc;border:1px solid var(--line);border-radius:8px;padding:8px 10px;
  font-size:12.5px;line-height:1.62;color:#2b3038;word-break:break-word;white-space:pre-wrap}
.ntfoot{display:flex;align-items:center;gap:10px;margin-top:5px;color:var(--muted);font-size:11px}
.ntfoot button{border:0;background:none;padding:0;color:var(--muted);cursor:pointer;font-size:11px}
.ntfoot button:hover{color:#cf1322}
.ntfoot button.arm{color:#cf1322;font-weight:600}
.ntadd{display:flex;gap:8px;margin-top:10px;align-items:stretch}
.ntadd textarea{flex:1;min-height:56px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;
  font-size:12.5px;font-family:inherit;line-height:1.6;resize:vertical;outline:none}
.ntadd textarea:focus{border-color:var(--green)}
.ntadd button{flex:none;padding:0 14px;border:1px solid var(--green);background:var(--green);color:#fff;
  border-radius:8px;cursor:pointer;font-size:12.5px;font-weight:600}
.dchip2{display:inline-flex;align-items:center;gap:5px;font-size:11.5px;line-height:18px;padding:0 7px;
  border-radius:5px;background:#f2f3f5;color:#5b6270}
.cpick{width:12px;height:12px;padding:0;border:0;border-radius:50%;background:none;cursor:pointer;flex:none}
.cpick::-webkit-color-swatch-wrapper{padding:0}
.cpick::-webkit-color-swatch{border:none;border-radius:50%}
.batchbar{position:fixed;left:50%;bottom:22px;transform:translate(-50%,150%);transition:transform .2s;background:#1f2329;color:#fff;
  border-radius:12px;padding:10px 12px;display:flex;align-items:center;gap:10px;box-shadow:0 8px 26px rgba(0,0,0,.24);z-index:25;font-size:13px}
.batchbar.show{transform:translate(-50%,0)}
.batchbar b{color:#7ee2a8}
.batchbar button{padding:7px 13px;border:0;border-radius:8px;background:#3a4048;color:#fff;cursor:pointer;font-size:12.5px}
.batchbar button.p{background:var(--green)}
.batchbar button.q{background:transparent;color:#b9c0c8}
.modal{position:fixed;left:50%;top:50%;transform:translate(-50%,-46%) scale(.98);width:500px;max-width:94vw;max-height:82vh;overflow:auto;
  background:var(--card);border-radius:12px;box-shadow:0 18px 50px rgba(0,0,0,.22);z-index:32;opacity:0;pointer-events:none;transition:.18s}
.modal.show{opacity:1;pointer-events:auto;transform:translate(-50%,-50%)}
.mh{display:flex;align-items:center;gap:10px;padding:16px 18px 12px;font-weight:600;font-size:14.5px;border-bottom:1px solid var(--line)}
.mh .x{margin-left:auto;border:1px solid var(--line);background:transparent;border-radius:6px;padding:4px 10px;cursor:pointer;color:var(--muted);font-size:12px}
.mb{padding:16px 18px}
.mhint{color:var(--muted);font-size:12px;margin-bottom:12px;line-height:1.6}
/* ---------- 快捷话术模板 ---------- */
.tpl{flex:none;padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:#5b6270;font-size:12.5px;cursor:pointer}
.tpl:hover{border-color:var(--green);color:var(--green)}
.modal.wide{width:640px}
.tplsearch{width:100%;padding:8px 11px;border:1px solid var(--line);border-radius:8px;font-size:13px;margin-bottom:12px;box-sizing:border-box}
.catrow{font-size:11.5px;color:var(--muted);font-weight:600;margin:14px 0 7px;letter-spacing:.4px}
.catrow:first-child{margin-top:0}
.tplitem{display:flex;align-items:flex-start;gap:10px;width:100%;text-align:left;padding:10px 12px;border:1px solid var(--line);border-radius:9px;background:var(--card);cursor:pointer;margin-bottom:7px;box-sizing:border-box}
.tplitem:hover{border-color:var(--green);background:#f7fdf9}
.tplitem .tt{font-size:13.5px;font-weight:600;display:flex;align-items:center;gap:7px;flex-wrap:wrap}
.tplitem .tp{font-size:12.5px;color:#5b6270;line-height:1.55;margin-top:4px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.tplitem .go{flex:none;margin-left:auto;font-size:11.5px;color:var(--green);border:1px solid #b7e4c9;border-radius:6px;padding:4px 8px;background:#f2fbf6;white-space:nowrap;cursor:pointer}
.tplitem .go:hover{background:#e6f8ee}
.pinmark{font-size:11px;color:#b45309;background:#fdf3e7;border:1px solid #f3dcc0;border-radius:8px;padding:0 6px;line-height:16px;font-weight:500}
.mini2{padding:4px 9px;border:1px solid var(--line);border-radius:7px;background:var(--card);cursor:pointer;font-size:12px;color:#5b6270}
.mini2:hover{border-color:var(--green);color:var(--green)}
.tform label{display:block;font-size:12.5px;color:#5b6270;font-weight:600;margin:12px 0 6px}
.tform input[type=text],.tform textarea{width:100%;padding:8px 11px;border:1px solid var(--line);border-radius:8px;font-size:13px;font-family:inherit;box-sizing:border-box}
.tform textarea{min-height:150px;line-height:1.65;resize:vertical}
.varbar{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.varbar button{padding:3px 8px;border:1px dashed var(--line);border-radius:12px;background:var(--card);color:#5b6270;font-size:11.5px;cursor:pointer}
.varbar button:hover{border-color:var(--green);color:var(--green);border-style:solid}
.trow{display:flex;align-items:center;gap:8px;padding:9px 2px;border-bottom:1px solid #f0f1f3}
.trow .tcat{font-size:11px;color:var(--muted);border:1px solid var(--line);border-radius:9px;padding:0 6px;line-height:17px;white-space:nowrap;min-width:62px;text-align:center;box-sizing:border-box}
.trow .ttl{font-size:13px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:flex;align-items:center;gap:6px}
/* ---------- 今日必跟 ---------- */
.tdb{border-color:#bfe6d0;color:var(--green);font-weight:600}
.tdb:hover{background:var(--greenbg)}
.nbadge{display:inline-block;min-width:18px;padding:0 5px;margin-left:3px;border-radius:9px;background:var(--green);color:#fff;
  font-size:11px;font-weight:600;line-height:17px;text-align:center;vertical-align:1px}
.todopane{position:fixed;top:0;right:0;height:100%;width:620px;max-width:96vw;background:var(--card);
  box-shadow:-10px 0 30px rgba(0,0,0,.14);transform:translateX(102%);z-index:31;display:flex;flex-direction:column;
  transition:transform .22s cubic-bezier(.2,.8,.3,1)}
.todopane.show{transform:none}
.tdh{display:flex;align-items:flex-start;gap:10px;padding:18px 18px 14px;border-bottom:1px solid var(--line)}
.tdh .x{margin-left:auto;border:1px solid var(--line);background:transparent;border-radius:6px;padding:5px 11px;cursor:pointer;color:var(--muted);font-size:12px;flex:none}
.tdtitle{font-size:16px;font-weight:700}
.tdtitle span{color:var(--green)}
.tdsub{font-size:12px;color:var(--muted);margin-top:5px;line-height:1.6}
.tdrule{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;padding:11px 18px;background:#fbfbfc;
  border-bottom:1px solid var(--line);font-size:12.5px;color:#5b6270}
.tdrule label{display:inline-flex;align-items:center;gap:5px;cursor:pointer}
.tdrule input[type=checkbox]{accent-color:var(--green);cursor:pointer}
.tdrule input[type=number]{width:52px;padding:3px 6px;border:1px solid var(--line);border-radius:6px;font-size:12.5px;text-align:center}
.tdrule input[type=number]:disabled{opacity:.5;cursor:not-allowed}
.rlskip{display:flex;gap:7px;align-items:center;flex-wrap:wrap;padding:9px 18px;border-bottom:1px solid var(--line);font-size:12px;color:var(--muted)}
.rlskip button{padding:3px 9px;border:1px solid var(--line);border-radius:12px;background:var(--card);color:#5b6270;font-size:11.5px;cursor:pointer}
.rlskip button.skip{background:#f2f3f5;border-color:#c9ced6;color:#8a9099;text-decoration:line-through}
.tdbody{flex:1;overflow:auto;padding:12px 18px 18px}
.tdgroup{font-size:11.5px;color:var(--muted);font-weight:600;letter-spacing:.4px;margin:14px 0 8px}
.tdgroup:first-child{margin-top:0}
.tditem{display:flex;align-items:center;gap:10px;padding:10px 12px;border:1px solid var(--line);border-radius:10px;margin-bottom:8px;background:var(--card)}
.tditem:hover{border-color:#bfe6d0;box-shadow:0 2px 10px rgba(15,157,88,.07)}
.tditem .vscore{width:46px;height:40px;font-size:15px;cursor:default}
.tdinfo{flex:1;min-width:0}
.tdname{display:flex;align-items:center;gap:5px;min-width:0;font-weight:600;font-size:13.5px;white-space:nowrap;overflow:hidden}
.tdname .nm{font-size:13.5px}
.tdwhy{font-size:11.5px;color:var(--muted);margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tddone{flex:none;padding:8px 13px;border:0;border-radius:8px;background:var(--green);color:#fff;font-size:12.5px;cursor:pointer;white-space:nowrap}
.tddone:hover{filter:brightness(.94)}
.tdx{flex:none;width:26px;height:26px;border:1px solid var(--line);border-radius:7px;background:var(--card);color:var(--muted);cursor:pointer;font-size:15px;line-height:1}
.tdx:hover{border-color:#f0b4ad;color:var(--red)}
.tdf{display:flex;align-items:center;gap:10px;padding:11px 18px;border-top:1px solid var(--line);background:#fbfbfc;font-size:12px;color:var(--muted)}
.tdf button{margin-left:auto;padding:7px 14px;border:1px solid var(--line);border-radius:8px;background:var(--card);cursor:pointer;font-size:12.5px}
.tdflag{flex:none;background:var(--greenbg);border:1px solid #bfe6d0;color:var(--green);border-radius:4px;
  font-size:10.5px;line-height:16px;padding:0 5px;cursor:pointer;font-weight:600}
.row[data-td="1"]{border-color:#cbead8}
#toast .undo{margin-left:10px;color:#7ee2a8;text-decoration:underline;cursor:pointer;font-weight:600}
#envwarn{display:none;background:#fff1f0;border:1px solid #ffccc7;color:#cf1322;padding:14px 16px;border-radius:8px;font-size:13px;margin-bottom:18px;line-height:1.7}
#envwarn b{font-size:15px}
#offline{display:none;background:#fffbe6;border:1px solid #ffe58f;color:#ad6800;padding:11px 14px;border-radius:8px;
  font-size:12.5px;margin-bottom:14px;line-height:1.65;align-items:center;gap:12px}
#offline.show{display:flex}
#offline span{flex:1}
#offline b{white-space:nowrap}
#offline button{flex:none;padding:6px 13px;border:1px solid #ffd591;background:#fff;color:#ad6800;
  border-radius:7px;cursor:pointer;font-size:12.5px;font-weight:600}
#offline button:hover{background:#fff7e6}
</style>
</head>
<body>
<div class="wrap">
<h1>客户回复排行榜</h1>
<div id="envwarn"><b>⚠ 这里是静态预览，按钮无法唤起微信</b><br>请到文件夹里双击 <b>客户回复排行榜.bat</b>，会弹出独立窗口——在那个窗口里点「打开聊天」才有效。这个预览页只能看数据。</div>
<div id="offline"><span id="offtext"><b>本地服务已断开</b>：「打开聊天」「保存标签 / 状态」「今日必跟」暂时都会失败。请确认那个黑色窗口还开着；如果关了，重新双击 <b>客户回复排行榜.bat</b>，然后点右边按钮重连（会自动重新加载页面，不用手动刷新）。</span><button onclick="retryNow()">重新连接</button></div>
<div class="sub">按<b>智能价值分</b>排序（最近互动 / 回复率 / 消息量 / 标签 / 手动调整，权重可调）· 口径：备注以编号开头的客户 + 有往来 · <b>点客户名打开侧边栏打标签 / 改跟进状态</b> · 勾选多行可批量操作 · <b>顶部「今日必跟」看今天该跟谁</b> · 点右侧按钮打开该客户聊天（只打开，不代发消息）</div>
<div class="tip">客户名左侧方块即价值分（分数偏低说明久未联系，正是该盘活的对象），鼠标悬停可看评分明细。想筛人时用「状态 / 标签」两行胶囊，可和时间筛选叠加。今日必跟里点「✓ 已跟」就把人从今天的名单去掉，误点 5 秒内可撤销。改标签和状态是即点即存的，不用点保存。</div>
<div class="bar">
  <input id="q" placeholder="搜索客户名或编号，如 2503 / 曾娟">
  <div class="seg">
    <button id="s0" class="on" onclick="setSort('value')">价值分</button>
    <button id="s1" onclick="setSort('my_last_ts')">最近我发的</button>
    <button id="s2" onclick="setSort('replies')">回复最多</button>
    <button id="s3" onclick="setSort('recent30')">近30天活跃</button>
    <button id="s4" onclick="setSort('last_ts')">最近回复</button>
  </div>
  <button id="tdbtn" class="refresh tdb" onclick="openTodo()" title="今天该跟的客户，点这里">今日必跟 <span id="tdn" class="nbadge">0</span></button>
  <button id="tml" class="refresh" onclick="openTplManager()">话术模板</button>
  <button id="cfg" class="refresh" onclick="toggleCfg()">评分权重</button>
  <button id="rf" class="refresh" onclick="refreshData()">刷新数据</button>
</div>
<div id="cfgpanel" class="panel">
  <div class="ph">评分权重 <span class="hint">拖动即时重算 · 保存后下次打开仍生效</span>
    <button class="x" onclick="toggleCfg()">收起</button></div>
  <div class="prow"><label>最近互动</label><input type="range" id="w_recency" min="0" max="100" step="5" oninput="onWeight()"><b id="v_recency">40</b></div>
  <div class="prow"><label>回复率</label><input type="range" id="w_reply" min="0" max="100" step="5" oninput="onWeight()"><b id="v_reply">30</b></div>
  <div class="prow"><label>消息总量</label><input type="range" id="w_volume" min="0" max="100" step="5" oninput="onWeight()"><b id="v_volume">15</b></div>
  <div class="prow"><label>时间半衰期</label><input type="range" id="hl" min="3" max="90" step="1" oninput="onWeight()"><b id="v_hl">30天</b></div>
  <div class="prow" style="align-items:flex-start"><label style="padding-top:4px">标签加减分</label><div class="tagw" id="tagw"></div></div>
  <div class="pfoot"><button onclick="resetScore()">恢复默认</button><button class="primary" onclick="saveScore()">保存权重</button></div>
</div>
<div class="chips">
  <button id="f0" class="on" onclick="setFilter('all')">全部 <span class="c" id="c_all">0</span></button>
  <button id="f1" onclick="setFilter('today')">今天我发过 <span class="c" id="c_today">0</span></button>
  <button id="f2" onclick="setFilter('d3')">近3天我发过 <span class="c" id="c_d3">0</span></button>
  <button id="f3" onclick="setFilter('d7')">近7天我发过 <span class="c" id="c_d7">0</span></button>
  <button id="f4" onclick="setFilter('pending')">发了没回 <span class="c" id="c_pending">0</span></button>
</div>
<div class="fbar"><span class="lab">状态</span><div class="chips" id="stchips"></div></div>
<div class="fbar"><span class="lab">标签</span><div class="chips" id="tagchips"></div></div>
<div class="stat"><span>当前显示 <b id="cnt">0</b></span><span>平均价值分 <b id="avg">0</b></span><span>高价值(≥70) <b id="hv">0</b></span><span>回复消息 <b id="tot">0</b></span><span>近30天有回复 <b id="hot">0</b></span><span>今天我发过 <b id="senttoday">0</b></span>
  <label class="ckall" style="margin-left:14px"><input type="checkbox" id="ckall" onchange="toggleAll(this.checked)">全选当前筛选</label>
  <span style="margin-left:auto;font-size:12px">当前微信 <b>__WX__</b> · 数据更新于 __SCANTIME__</span></div>
<div id="list"></div>
<div id="batch" class="batchbar">
  <span>已选 <b id="seln">0</b> 位</span>
  <button class="p" onclick="batchTags()">打标签</button>
  <button class="p" onclick="batchStatus()">改状态</button>
  <button class="p" onclick="batchTodo()">加入今日</button>
  <button class="q" onclick="clearSel()">取消选择</button>
</div>
<div id="overlay" class="overlay" onclick="closeTop()"></div>
<div id="drawer" class="drawer"></div>
<div id="todo" class="todopane">
  <div class="tdh">
    <div style="min-width:0">
      <div class="tdtitle">今日必跟 <span id="tdcount">0</span> 位</div>
      <div class="tdsub">按价值分和上次互动自动挑的，也能自己加。跟完点「✓ 已跟」就从这里消失。</div>
    </div>
    <button class="x" onclick="closeTodo()">关闭</button>
  </div>
  <div class="tdrule">
    <label><input type="checkbox" id="rl_on" onchange="saveRule()">自动推荐</label>
    <label>超过 <input type="number" id="rl_days" min="1" max="90" onchange="saveRule()"> 天没互动</label>
    <label>价值分 ≥ <input type="number" id="rl_score" min="0" max="100" onchange="saveRule()"></label>
    <label>每天最多 <input type="number" id="rl_limit" min="1" max="50" onchange="saveRule()"> 位</label>
  </div>
  <div class="rlskip"><span>这些状态不推荐：</span><span id="rl_status" style="display:flex;gap:7px;flex-wrap:wrap"></span></div>
  <div class="tdbody" id="tdbody"></div>
  <div class="tdf"><span id="tdstat"></span><button onclick="closeTodo()">关闭</button></div>
</div>
<div id="modal" class="modal"><div id="mbody"></div></div>
<div id="toast"></div>
</div>
<script>
const DATA = __DATA__;
const SCORE = __SCORECFG__;
let sortKey='value', filterKey='all', query='', noBackend=false;
const SORTS=['value','my_last_ts','replies','recent30','last_ts'];
const $=id=>document.getElementById(id);
const NOW=Date.now()/1000, DAY=86400;
let _toastTimer=null;
function toast(msg,undo){
  const t=$('toast');
  t.innerHTML=esc(msg)+(undo?'<b class="undo">撤销</b>':'');
  t._undo=undo||null;
  t.classList.add('show');
  if(typeof clearTimeout==='function') clearTimeout(_toastTimer);
  _toastTimer=setTimeout(()=>t.classList.remove('show'),undo?5200:2600);
}
function startOfToday(){const d=new Date();d.setHours(0,0,0,0);return d.getTime()/1000}
/* ---- 本地服务掉线自动恢复：服务重启的那几秒不会再让人以为工具坏了 ---- */
let OFFLINE=false, _failStreak=0, _watch=null;
async function pingOnce(){
  try{
    const r=await fetch('/ping',{cache:'no-store'});
    const j=await r.json();
    return !!(j&&j.ok===true);
  }catch(e){ return false; }
}
function setOffline(on){
  if(on===OFFLINE) return;
  OFFLINE=on;
  const b=$('offline');
  if(b&&b.classList) b.classList.toggle('show',on);
  if(!on) toast('本地服务已恢复，可以继续操作了');
}
function noteFail(){ _failStreak++; setOffline(true); }
function noteOk(){ _failStreak=0; if(OFFLINE) setOffline(false); }
/* 服务重启会有一两秒空档，请求先自己悄悄重试几次，别一失败就吓人 */
async function fetchRetry(url,opts,tries,gap){
  tries=tries||3; gap=gap||700;
  let last;
  for(let i=0;i<tries;i++){
    try{ return await fetch(url,opts); }
    catch(e){
      last=e;
      if(i<tries-1) await new Promise(r=>setTimeout(r,gap));
    }
  }
  throw last;
}
async function retryNow(){
  const ok=await pingOnce();
  if(ok){ noteOk(); toast('已连上，正在重新加载…'); setTimeout(()=>location.reload(),600); }
  else { toast('还是连不上——请确认黑色窗口还开着，或重新双击「客户回复排行榜.bat」'); }
}
function startHealthWatch(){
  if(_watch) return;
  if(noBackend) return;
  if(window.pywebview && window.pywebview.api && window.pywebview.api.open_chat) return;
  if(typeof setInterval!=='function') return;
  _watch=setInterval(async()=>{
    const ok=await pingOnce();
    if(ok) noteOk(); else noteFail();
  },5000);
}
function rel(ts){
  if(!ts) return '—';
  const d=NOW-ts;
  if(d<60) return '刚刚';
  if(d<3600) return Math.max(1,Math.floor(d/60))+'分钟前';
  if(d<86400) return Math.floor(d/3600)+'小时前';
  if(ts>=startOfToday()-DAY) return '昨天';
  if(d<2592000) return Math.floor(d/86400)+'天前';
  return Math.floor(d/2592000)+'个月前';
}
function fmtDate(s){return s?s.slice(5,16):'—'}
const FILTERS={
  all:()=>true,
  today:r=>r.my_last_ts>=startOfToday(),
  d3:r=>r.my_last_ts>=NOW-3*DAY,
  d7:r=>r.my_last_ts>=NOW-7*DAY,
  pending:r=>r.my_msgs>0&&!r.replied
};

/* ---------------- 智能价值评分 ---------------- */
const SCORE_DEFAULT={weights:{recency:40,reply:30,volume:15},half_life_days:30,volume_cap:100,
  tag_weights:{'高意向':20,'老客户':15,'中意向':8,'低意向':-10,'沉默':-15}};
let W=Object.assign({},SCORE_DEFAULT.weights,SCORE.weights||{});
let HL=+(SCORE.half_life_days||30);
let VCAP=+(SCORE.volume_cap||100);
let TAGW=Object.assign({},SCORE_DEFAULT.tag_weights,SCORE.tag_weights||{});
const TAGS=Object.assign({},SCORE.tags||{});
const MANUAL=Object.assign({},SCORE.manual||{});
/* ---- 客户备注（多条历史） ---- */
const NOTES=Object.assign({},SCORE.notes||{});
function notesOf(u){const a=NOTES[u];return Array.isArray(a)?a:[]}
/* ---- 跟进状态 / 标签颜色 ---- */
const STATUS=Object.assign({},SCORE.status||{});
const STATUS_DEFAULT=[{name:'待激活',color:'#8a9099'},{name:'跟进中',color:'#2563eb'},
  {name:'已转化',color:'#0f9d58'},{name:'暂时搁置',color:'#d97706'},{name:'已流失',color:'#d93a2b'}];
const STATUSES=((SCORE.statuses&&SCORE.statuses.length)?SCORE.statuses:STATUS_DEFAULT)
  .map(s=>({name:String(s.name),color:s.color||'#8a9099'}));
const PALETTE=['#0f9d58','#2563eb','#7c3aed','#d97706','#db2777','#0891b2','#64748b','#d93a2b'];
const TAGCOLORS=Object.assign({},SCORE.tag_colors||{});
function tagsOf(r){const t=TAGS[r.username]||TAGS[r.name]||[];return Array.isArray(t)?t:(t?[String(t)]:[])}
function manualOf(r){const v=MANUAL[r.username]; if(v!==undefined) return +v||0; return +(MANUAL[r.name]||0)}
function statusOf(r){return STATUS[r.username]||STATUS[r.name]||''}
function statusColor(s){const hit=STATUSES.filter(x=>x.name===s)[0];return hit?hit.color:'#8a9099'}
function hexc(c){return /^#[0-9a-fA-F]{6}$/.test(String(c||''))?c:'#8a9099'}
function tint(hex,a){const n=parseInt(hex.slice(1),16);return 'rgba('+((n>>16)&255)+','+((n>>8)&255)+','+(n&255)+','+a+')'}
function hashColor(t){let h=0;for(let i=0;i<t.length;i++)h=(h*31+t.charCodeAt(i))>>>0;return PALETTE[h%PALETTE.length]}
function tagColor(t){return hexc(TAGCOLORS[t]||hashColor(String(t)))}
function allTagNames(){
  const out=[],push=t=>{if(t&&out.indexOf(t)<0)out.push(t)};
  Object.keys(TAGW).forEach(push);
  Object.keys(TAGCOLORS).forEach(push);
  Object.keys(TAGS).forEach(u=>tagsOf({username:u}).forEach(push));
  return out;
}
/* ---- 今日必跟（今日待办） ---- */
const TODO_CFG=SCORE.todo||{};
const TD_RULE_DEFAULT={enabled:true,min_days:3,min_score:45,limit:15,
  skip_status:['已转化','已流失','暂时搁置']};
let TD_MANUAL=Array.isArray(TODO_CFG.manual)?TODO_CFG.manual.slice():[];
let TD_DONE=Object.assign({},TODO_CFG.done||{});
let TD_SNOOZE=Object.assign({},TODO_CFG.snooze||{});
let TD_RULE=Object.assign({},TD_RULE_DEFAULT,TODO_CFG.rule||{});
let TD_TODAY=Object.assign({date:'',users:[]},TODO_CFG.today||{});
let TODO_ITEMS=[], TD_SET={};
function lastTouch(r){return Math.max(r.my_last_ts||0,r.last_ts||0)}
function daysOf(r){const t=lastTouch(r);return t?Math.floor((NOW-t)/86400):null}
function inTodo(u){return !!TD_SET[u]}
function tagChipHtml(t,opt){
  const c=tagColor(t);
  const rm=(opt&&opt.rm)?`<b class="rm" onclick="removeTag('${opt.u}','${enc(t)}')">×</b>`:'';
  const pick=(opt&&opt.rm)?`<input type="color" class="cpick" value="${c}" title="改颜色" onchange="setTagColor('${enc(t)}',this.value)">`:'';
  return `<span class="tagchip" style="background:${tint(c,.13)};color:${c};border:1px solid ${tint(c,.25)}">${pick}${esc(t)}${rm}</span>`;
}
function scoreOf(r){
  const last=Math.max(r.my_last_ts||0,r.last_ts||0);
  const days=last?Math.max(0,(NOW-last)/86400):9999;
  const rec=last?Math.exp(-days/HL):0;
  const rep=Math.min(1,(r.replies||0)/Math.max(1,r.my_msgs||0));
  const total=(r.my_msgs||0)+(r.replies||0);
  const vol=Math.min(1,Math.log(1+total)/Math.log(1+VCAP));
  const wsum=(W.recency+W.reply+W.volume)||1;
  const base=rec*(W.recency/wsum)+rep*(W.reply/wsum)+vol*(W.volume/wsum);
  const tags=tagsOf(r);
  const tagSum=tags.reduce((s,t)=>s+(+TAGW[t]||0),0);
  const man=manualOf(r);
  const value=Math.max(0,Math.min(100,Math.round(base*100+tagSum+man)));
  return {value,rec,rep,vol,tags,tagSum,man,days:last?Math.floor(days):null,total,
    parts:{recency:rec*(W.recency/wsum)*100,reply:rep*(W.reply/wsum)*100,volume:vol*(W.volume/wsum)*100}};
}
function scoreReason(r,s){
  const f=n=>Math.round(n);
  const seg=['最近互动 '+f(s.parts.recency)+'（'+(s.days===null?'无记录':(s.days<1?'今天':s.days+'天前'))+'）',
    '回复率 '+f(s.parts.reply)+'（'+r.replies+'/'+r.my_msgs+'）',
    '消息量 '+f(s.parts.volume)+'（'+s.total+'条）'];
  if(s.tagSum) seg.push('标签 '+(s.tagSum>0?'+':'')+s.tagSum+'（'+s.tags.join('·')+'）');
  if(s.man) seg.push('手动 '+(s.man>0?'+':'')+s.man);
  return '价值分 '+s.value+' = '+seg.join('  +  ').replace(/\+  -/g,'-  ');
}
function scoreClass(v){return v>=70?'hi':(v>=40?'mid':'lo')}

let view=[], tagFilter='', statusFilter='', drawerUser='', ntColor=PALETTE[0];
const sel=new Set();
function matchTag(r){if(!tagFilter)return true;const ts=tagsOf(r);return tagFilter==='__untagged'?ts.length===0:ts.indexOf(tagFilter)>=0}
function matchStatus(r){if(!statusFilter)return true;const s=statusOf(r);return statusFilter==='__none'?!s:s===statusFilter}
function render(){
  const rows=DATA.filter(FILTERS[filterKey]).filter(matchTag).filter(matchStatus)
    .filter(r=>(r.name||'').toLowerCase().includes(query.toLowerCase()));
  const scored=rows.map(r=>({r,s:scoreOf(r)}));
  if(sortKey==='value'){
    scored.sort((a,b)=>b.s.value-a.s.value || (b.r.my_last_ts||0)-(a.r.my_last_ts||0));
  }else{
    scored.sort((a,b)=>(b.r[sortKey]||0)-(a.r[sortKey]||0));
  }
  view=scored.map(x=>x.r.username);
  $('cnt').textContent=scored.length;
  TODO_ITEMS=todoItems();
  TD_SET={}; TODO_ITEMS.forEach(x=>{TD_SET[x.r.username]=1});
  $('tdn').textContent=TODO_ITEMS.length;
  $('list').innerHTML = scored.length? scored.map(({r,s},i)=>{
    const st=statusOf(r), sc=statusColor(st), tags=s.tags, on=sel.has(r.username);
    const shown=tags.slice(0,3), rest=tags.length-shown.length, td=inTodo(r.username);
    return `
    <div class="row" data-sel="${on?1:0}" data-td="${td?1:0}">
      <label class="ck" title="选中可批量打标签/改状态"><input type="checkbox" ${on?'checked':''} onchange="toggleSel('${enc(r.username)}',this.checked)"></label>
      <div class="rank ${i<3?'top':''}">${i+1}</div>
      <div class="vscore ${scoreClass(s.value)}" title="${esc(scoreReason(r,s))}" onclick="editManual('${enc(r.username)}','${enc(r.name)}')">${s.value}<span>分</span></div>
      <div class="info">
        <div class="name" onclick="openDrawer('${enc(r.username)}')" title="点击打标签 / 改跟进状态">
          <span class="nm">${esc(r.name)}</span>
          ${td?`<span class="tdflag" onclick="event.stopPropagation();openTodo()" title="在今日必跟里，点这里打开">今日</span>`:''}
          ${st?`<span class="stchip" style="background:${tint(sc,.13)};color:${sc}"><i style="background:${sc}"></i>${esc(st)}</span>`:''}
          ${shown.map(t=>tagChipHtml(t)).join('')}
          ${rest>0?`<span class="tagchip" style="background:#f2f3f5;color:#8a9099">+${rest}</span>`:''}
          <span class="tg add" style="flex:none">＋</span>
        </div>
        <div class="last mine"><span class="tag">我发</span>${r.my_last_ts?fmtDate(r.my_last_time)+' · '+rel(r.my_last_ts)+' · '+esc(r.my_last_text):'没有我发出的记录'}</div>
        <div class="last">${r.replied?'<span class="tag rep">回复</span>'+fmtDate(r.last_time)+' · '+esc(r.last_text):'<span class="tag none">未回</span>对方还没回复'}</div>
      </div>
      <div class="badge"><div class="n">${r.my_msgs}<span style="font-size:12px;color:var(--muted)">/</span><span style="color:var(--red)">${r.replies}</span></div><div class="u">我发/回复</div></div>
      <div class="fresh ${r.my_last_ts>=startOfToday()?'hot':''}">${r.my_last_ts?rel(r.my_last_ts):'—'}</div>
      <button class="tpl" onclick="openTplPicker('${enc(r.username)}')" title="选一条话术，复制到剪贴板">复制话术</button>
      <button class="open" ${noBackend?'disabled':''} onclick="openChat(this,'${enc(r.name)}')">${noBackend?'需用 bat 启动':'打开聊天'}</button>
    </div>`;}).join('') : '<div class="empty">这个筛选条件下没有客户</div>';
  $('avg').textContent = scored.length? Math.round(scored.reduce((a,x)=>a+x.s.value,0)/scored.length) : 0;
  $('hv').textContent = scored.filter(x=>x.s.value>=70).length;
  renderChips(); syncBatchBar();
  if($('todo').classList.contains('show')) renderTodo();
}
/* ---- 状态 / 标签筛选胶囊 ---- */
function renderChips(){
  const tm={};
  DATA.forEach(r=>tagsOf(r).forEach(t=>{tm[t]=(tm[t]||0)+1}));
  const known=allTagNames().filter(t=>tm[t]);
  const extra=Object.keys(tm).filter(t=>known.indexOf(t)<0).sort((a,b)=>tm[b]-tm[a]);
  $('tagchips').innerHTML=
    `<button class="${tagFilter?'':'on'}" onclick="setTagFilter('')">全部 <span class="c">${DATA.length}</span></button>`
    +known.concat(extra).map(t=>`<button class="${tagFilter===t?'on':''}" onclick="setTagFilter('${enc(t)}')"><i class="dot" style="background:${tagColor(t)}"></i>${esc(t)} <span class="c">${tm[t]}</span></button>`).join('')
    +(extra.length||known.length?'':'<span style="color:var(--muted);font-size:12px;line-height:26px">还没有任何标签——点客户名即可添加</span>');
  const sm={};
  DATA.forEach(r=>{const s=statusOf(r);sm[s]=(sm[s]||0)+1});
  $('stchips').innerHTML=
    `<button class="${statusFilter?'':'on'}" onclick="setStatusFilter('')">全部 <span class="c">${DATA.length}</span></button>`
    +STATUSES.map(x=>`<button class="${statusFilter===x.name?'on':''}" onclick="setStatusFilter('${enc(x.name)}')"><i class="dot" style="background:${x.color}"></i>${esc(x.name)} <span class="c">${sm[x.name]||0}</span></button>`).join('')
    +`<button class="${statusFilter==='__none'?'on':''}" onclick="setStatusFilter('__none')">未设置 <span class="c">${sm['']||0}</span></button>`;
}
function setTagFilter(t){tagFilter=dec(t);render()}
function setStatusFilter(s){statusFilter=dec(s);render()}
/* ---- 勾选批量 ---- */
function toggleSel(u,on){if(on)sel.add(u);else sel.delete(u);render()}
function toggleAll(on){sel.clear();if(on)view.forEach(u=>sel.add(u));render()}
function clearSel(){sel.clear();const c=$('ckall');if(c)c.checked=false;render()}
function syncBatchBar(){
  const n=sel.size;
  $('seln').textContent=n;
  $('batch').classList.toggle('show',n>0);
  const c=$('ckall');
  if(c) c.checked=n>0&&n===view.length;
}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function enc(s){return encodeURIComponent(s)}
function dec(s){try{return decodeURIComponent(s||'')}catch(e){return String(s||'')}}
function setSort(k){sortKey=k;SORTS.forEach((k2,i)=>$('s'+i).classList.toggle('on',k2===k));render()}
function setFilter(k){filterKey=k;['all','today','d3','d7','pending'].forEach((k2,i)=>$('f'+i).classList.toggle('on',k2===k));render()}

/* ---------------- 权重面板 / 手动分 / 标签 ---------------- */
function toggleCfg(){const p=$('cfgpanel');p.classList.toggle('show');if(p.classList.contains('show'))syncCfgUI()}
function syncCfgUI(){
  $('w_recency').value=W.recency; $('v_recency').textContent=Math.round(W.recency);
  $('w_reply').value=W.reply; $('v_reply').textContent=Math.round(W.reply);
  $('w_volume').value=W.volume; $('v_volume').textContent=Math.round(W.volume);
  $('hl').value=HL; $('v_hl').textContent=Math.round(HL)+'天';
  const keys=Array.from(new Set([...Object.keys(SCORE_DEFAULT.tag_weights),...Object.keys(TAGW)]));
  $('tagw').innerHTML=keys.map(k=>`<label>${esc(k)}<input type="number" data-tag="${esc(k)}" value="${(+TAGW[k]||0)}" step="5" oninput="onWeight()"></label>`).join('');
}
function onWeight(){
  W={recency:+$('w_recency').value,reply:+$('w_reply').value,volume:+$('w_volume').value};
  HL=Math.max(1,+$('hl').value);
  $('v_recency').textContent=Math.round(W.recency);
  $('v_reply').textContent=Math.round(W.reply);
  $('v_volume').textContent=Math.round(W.volume);
  $('v_hl').textContent=Math.round(HL)+'天';
  document.querySelectorAll('#tagw input').forEach(inp=>{TAGW[inp.dataset.tag]=+inp.value||0});
  render();
}
function resetScore(){
  W=Object.assign({},SCORE_DEFAULT.weights); HL=SCORE_DEFAULT.half_life_days;
  TAGW=Object.assign({},SCORE_DEFAULT.tag_weights); syncCfgUI(); render();
  toast('已恢复默认权重（点「保存权重」才会持久化）');
}
async function saveScore(){
  const ok=await postJSON('/save_score',{weights:W,half_life_days:HL,volume_cap:VCAP,tag_weights:TAGW});
  if(ok) toast('权重已保存');
}
async function postJSON(path,body){
  if(window.pywebview && window.pywebview.api && window.pywebview.api.open_chat){
    toast('桌面窗口模式不支持保存，请用 bat 启动的浏览器版'); return false;
  }
  if(noBackend){ toast('静态预览：改动只在本次页面有效，请双击「客户回复排行榜.bat」'); return false; }
  try{
    const j=await (await fetchRetry(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
    if(!j.ok) toast('保存失败：'+(j.msg||'未知原因'));
    noteOk();
    return !!j.ok;
  }catch(e){ noteFail(); toast('连不上本地服务（黑窗口可能被关了），这次改动只在页面上，关掉重开 bat 就恢复'); return false; }
}
function editManual(u,n){
  u=dec(u); n=dec(n);
  const cur=MANUAL[u]!==undefined?MANUAL[u]:0;
  const v=prompt('给「'+n+'」手动加减分\n当前 '+cur+'，输入 -50~+50 的整数（0 = 清除）：',cur);
  if(v===null) return;
  let delta=Math.round(+v||0); delta=Math.max(-50,Math.min(50,delta));
  if(delta) MANUAL[u]=delta; else delete MANUAL[u];
  postJSON('/set_manual',{username:u,delta:delta}).then(ok=>{if(ok)toast(delta?'手动分已保存：'+(delta>0?'+':'')+delta:'已清除手动分')});
  render();
}
/* ---------------- 侧边抽屉：打标签 / 改状态 ---------------- */
function findRow(u){for(let i=0;i<DATA.length;i++)if(DATA[i].username===u)return DATA[i];return null}
function openDrawer(u){
  drawerUser=dec(u); ntColor=PALETTE[0];
  renderDrawer();
  $('overlay').classList.add('show');
  $('drawer').classList.add('show');
}
function closeAll(){
  $('overlay').classList.remove('show');
  $('drawer').classList.remove('show');
  $('modal').classList.remove('show');
  $('modal').classList.remove('wide');
  drawerUser=''; NT_ARM='';
  // 今日必跟面板还在的话，遮罩要保留，否则面板像浮在半空
  if($('todo').classList.contains('show')) $('overlay').classList.add('show');
}
function renderDrawer(){
  if(!drawerUser) return;
  const r=findRow(drawerUser); if(!r){closeAll();return}
  const cur=tagsOf(r), st=statusOf(r), s=scoreOf(r), sc=statusColor(st);
  const quick=allTagNames().filter(t=>cur.indexOf(t)<0);
  const num=(String(r.name).match(/^\d{3,6}/)||[''])[0];
  $('drawer').innerHTML=`
  <div id="dbody">
    <div class="dh">
      <div style="min-width:0;flex:1">
        <div class="dname">${esc(r.name)}${st?`<span class="stchip" style="background:${tint(sc,.13)};color:${sc};margin-left:8px;vertical-align:2px"><i style="background:${sc}"></i>${esc(st)}</span>`:''}</div>
        <div class="dsub">${num?`编号 <b>${num}</b> · `:''}价值分 <b style="color:#1f2329;cursor:help" title="${esc(scoreReason(r,s))}">${s.value}</b> · 我发 ${r.my_msgs||0} / 回复 ${r.replies||0}</div>
        <div class="dsub">${r.my_last_ts?'上次我发 '+rel(r.my_last_ts):'还没给他发过'}${r.replied?' · 上次他回 '+rel(r.last_ts):' · 对方还没回过'}</div>
      </div>
      <button class="x" onclick="closeAll()">关闭</button>
    </div>
    <div class="db">
      <div class="sec">
        <div class="sech">跟进状态 <span class="hint">点一下即改 · 再点取消</span></div>
        <div class="stbtns">
          ${STATUSES.map(x=>`<button class="stbtn ${st===x.name?'on':''}" ${st===x.name?'style="color:'+x.color+';background:'+tint(x.color,.1)+';border-color:'+x.color+'"':''} onclick="setStatusToggle('${enc(r.username)}','${enc(x.name)}')"><i style="background:${x.color}"></i>${esc(x.name)}</button>`).join('')}
        </div>
      </div>
      <div class="sec">
        <div class="sech">标签 <span class="hint">点 × 移除 · 点小圆点改颜色</span></div>
        <div class="dchips">${cur.length?cur.map(t=>tagChipHtml(t,{rm:true,u:enc(r.username)})).join('')
          :'<span style="color:var(--muted);font-size:12.5px">还没有标签，下面点一下就能加</span>'}</div>
        <div class="sech" style="margin-top:16px">快速添加</div>
        <div class="dchips">${quick.length?quick.map(t=>`<button class="qtag" onclick="addTag('${enc(r.username)}','${enc(t)}')"><i style="background:${tagColor(t)}"></i>${esc(t)}</button>`).join('')
          :'<span style="color:var(--muted);font-size:12.5px">常用标签都打过了，可在下面新建</span>'}</div>
        <div class="newtag">
          <input type="text" id="ntname" placeholder="新标签名，回车创建" onkeydown="if(event.key==='Enter'){event.preventDefault();createTag()}">
          <div class="sw" id="ntcolors">${PALETTE.map(c=>`<i class="${c===ntColor?'on':''}" style="background:${c}" onclick="pickNtColor('${c}')"></i>`).join('')}</div>
          <button class="mini primary" onclick="createTag()">新建</button>
        </div>
      </div>
      <div class="sec">
        <div class="sech">互动时间线 <span class="hint" id="tlsum"></span>
          <button class="tlmore" id="tlmore" onclick="showMoreTL()" style="display:none"></button></div>
        <div id="tlc"></div>
      </div>
      <div class="sec" style="margin-bottom:4px">
        <div class="sech">备注 <span class="hint">只给自己看 · 可记多条</span></div>
        <div id="ntlist" class="ntlist"></div>
        <div class="ntadd">
          <textarea id="nttext" onkeydown="if((event.ctrlKey||event.metaKey)&&event.key==='Enter'){event.preventDefault();addNote()}" placeholder="记一句：他关心什么、答应过什么、下次什么时候找他…（Ctrl+Enter 快速添加）"></textarea>
          <button onclick="addNote()">添加</button>
        </div>
      </div>
    </div>
    <div class="df">
      <button onclick="openTplPicker('${enc(r.username)}')">复制话术</button>
      <button onclick="openChatByName('${enc(r.name)}')">打开聊天</button>
      <button onclick="toggleTodo('${enc(r.username)}')" title="今日必跟：手动加入 / 移出 / 今天先不跟">${TD_MANUAL.indexOf(r.username)>=0?'移出今日':(inTodo(r.username)?'先不跟':'加入今日')}</button>
      <button class="primary" onclick="closeAll()">完成</button>
    </div>
  </div>`;
  renderTimeline(); renderNotes();
}
/* ---------- 客户详情：互动时间线（按需从数据库读） ---------- */
const TL={}, TL_BUSY={}, TL_SHOW={};
function fmtTs(ts){const d=new Date(ts*1000),p=n=>(n<10?'0':'')+n;
  return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes())}
function fmtDay(ts){const d=new Date(ts*1000),p=n=>(n<10?'0':'')+n;
  const one=x=>x.getFullYear()+'-'+p(x.getMonth()+1)+'-'+p(x.getDate());
  const dd=one(d), t=new Date();
  if(dd===one(t)) return '今天';
  t.setDate(t.getDate()-1);
  if(dd===one(t)) return '昨天';
  return dd.slice(5)}
function timelineHtml(u){
  const d=TL[u];
  if(!d) return '<div class="tlph">正在读取聊天记录…</div>';
  if(d.error) return '<div class="tlph">'+esc(d.error)+'</div>';
  if(!d.msgs||!d.msgs.length) return '<div class="tlph">还没有可显示的聊天记录</div>';
  const show=TL_SHOW[u]||40, list=d.msgs.slice(-show).reverse();
  let out='', day='';
  list.forEach(m=>{
    const k=fmtDay(m.ts);
    if(k!==day){ out+='<div class="tlday">'+k+'</div>'; day=k }
    out+='<div class="tlitem '+(m.mine?'me':'you')+'">'+
           '<div class="tlhead"><span class="tlwho '+(m.mine?'':'you')+'">'+(m.mine?'我发':'对方')+'</span>'+
           '<span class="tlwhen" title="'+fmtTs(m.ts)+'">'+fmtTs(m.ts).slice(11)+'</span></div>'+
           '<div class="tltext">'+esc(m.text||'')+'</div></div>';
  });
  return out;
}
function tlSum(u){
  const d=TL[u]; if(!d||d.error) return '';
  return '共 '+(d.total||0)+' 条 · 我发 '+(d.mine_total||0);
}
function tlMoreLabel(u){
  const d=TL[u]; if(!d||d.error||!d.msgs) return '';
  const show=TL_SHOW[u]||40, rest=d.msgs.length-show;
  if(rest>0) return '看更早 '+Math.min(40,rest)+' 条';
  return d.total>d.msgs.length ? '只显示最近 '+d.msgs.length+' 条' : '';
}
function renderTimeline(){
  const u=drawerUser, box=$('tlc'); if(!u||!box) return;
  box.innerHTML=timelineHtml(u);
  const s=$('tlsum'); if(s) s.textContent=tlSum(u);
  const b=$('tlmore');
  if(b){ const t=tlMoreLabel(u); b.textContent=t; if(b.style) b.style.display=t?'':'none' }
  if(!TL[u] && !TL_BUSY[u]) loadTimeline(u);
}
async function loadTimeline(u){
  if(TL_BUSY[u]) return;
  TL_BUSY[u]=1;
  try{
    const r=await fetch('/timeline?username='+enc(u)+'&limit=200',{cache:'no-store'});
    const j=await r.json();
    if(j&&j.ok) TL[u]={total:j.total||0,mine_total:j.mine_total||0,msgs:j.msgs||[]};
    else TL[u]={error:(j&&j.msg)||'读取聊天记录失败'};
  }catch(e){
    TL[u]={error:'读不到聊天记录——本地服务没响应，点页面顶部「重新连接」'};
    noteFail();
  }
  TL_BUSY[u]=0;
  if(drawerUser===u) renderTimeline();
}
function showMoreTL(){
  const u=drawerUser; if(!u||!TL[u]||!TL[u].msgs) return;
  TL_SHOW[u]=(TL_SHOW[u]||40)+60;
  renderTimeline();
}
/* ---------- 客户详情：备注 ---------- */
let NT_ARM='';
function renderNotes(){
  const u=drawerUser, box=$('ntlist'); if(!u||!box) return;
  const arr=notesOf(u);
  box.innerHTML = arr.length? arr.map(n=>{
    const armed=(NT_ARM===n.id);
    return '<div class="ntitem">'+esc(n.text)+
      '<div class="ntfoot"><span>'+fmtTs(n.ts)+' · '+rel(n.ts)+'</span>'+
      '<button class="'+(armed?'arm':'')+'" onclick="delNote(\''+n.id+'\')">'+(armed?'确认删除':'删除')+'</button></div></div>';
  }).join('') : '<div class="tlph">还没有备注。记一句他关心什么，下次开口就有话说。</div>';
}
async function addNote(){
  const u=drawerUser; if(!u) return;
  const ta=$('nttext'); const text=((ta&&ta.value)||'').trim();
  if(!text){ toast('先写点内容再添加'); if(ta&&ta.focus) ta.focus(); return }
  const j=await postJSONFull('/note',{action:'add',username:u,text:text});
  if(j&&j.ok){ NOTES[u]=j.notes||[]; if(ta) ta.value=''; renderNotes(); toast('备注已保存') }
  else toast('备注没保存：本地服务没响应');
}
async function delNote(id){
  const u=drawerUser; if(!u) return;
  if(NT_ARM!==id){
    NT_ARM=id; renderNotes(); toast('再点一次确认删除');
    setTimeout(()=>{if(NT_ARM===id){NT_ARM='';renderNotes()}},3000);
    return;
  }
  NT_ARM='';
  const j=await postJSONFull('/note',{action:'delete',username:u,id:id});
  if(j&&j.ok){ NOTES[u]=j.notes||[]; renderNotes(); toast('备注已删除') }
  else toast('删除失败：本地服务没响应');
}
function setStatus(u,st){
  u=dec(u); st=dec(st);
  if(st) STATUS[u]=st; else delete STATUS[u];
  postJSON('/set_status',{username:u,status:st});
  render();
  if(drawerUser===u) renderDrawer();
  toast(st?'状态已改为「'+st+'」':'已清除跟进状态');
}
function setStatusToggle(u,name){
  u=dec(u); name=dec(name);
  const next=(STATUS[u]===name)?'':name;
  if(next) STATUS[u]=next; else delete STATUS[u];
  postJSON('/set_status',{username:u,status:next});
  render();
  if(drawerUser===u) renderDrawer();
  toast(next?'状态已改为「'+next+'」':'已清除跟进状态');
}
function applyTags(u,tags){
  if(tags.length) TAGS[u]=tags; else delete TAGS[u];
  postJSON('/set_tags',{username:u,tags:tags});
  render();
  if(drawerUser===u) renderDrawer();
}
function addTag(u,t){
  u=dec(u); t=dec(t);
  const cur=tagsOf({username:u}).slice();
  if(cur.indexOf(t)<0) cur.push(t);
  applyTags(u,cur);
  toast('已添加标签：'+t);
}
function removeTag(u,t){
  u=dec(u); t=dec(t);
  applyTags(u,tagsOf({username:u}).filter(x=>x!==t));
  toast('已移除标签：'+t);
}
function setTagColor(t,c){
  t=dec(t);
  if(!/^#[0-9a-fA-F]{6}$/.test(String(c||''))) return;
  TAGCOLORS[t]=c;
  postJSON('/set_tag_color',{name:t,color:c});
  render();
  if(drawerUser) renderDrawer();
}
function pickNtColor(c){
  ntColor=c;
  const box=$('ntcolors');
  if(box) box.innerHTML=PALETTE.map(x=>`<i class="${x===c?'on':''}" style="background:${x}" onclick="pickNtColor('${x}')"></i>`).join('');
}
function createTag(){
  const el=$('ntname');
  const name=((el&&el.value)||'').trim().replace(/[,，、;；]/g,'');
  if(!name){toast('先输入标签名');return}
  if(!TAGCOLORS[name]) TAGCOLORS[name]=ntColor;
  addTag(drawerUser,name);
  if(el) el.value='';
}
function openChatByName(n){openChat(null,n)}

/* ---------------- 今日必跟：算法 / 面板 / 标记已跟 ---------------- */
function autoCandidates(){
  const skip=TD_RULE.skip_status||[], cand=[];
  const minDays=+TD_RULE.min_days||0, minScore=+TD_RULE.min_score||0;
  DATA.forEach(r=>{
    if(skip.indexOf(statusOf(r))>=0) return;
    if(TD_SNOOZE[r.username]===todayStr()) return;
    const t=lastTouch(r), d=t?Math.floor((NOW-t)/86400):null;
    if(d!==null && d<minDays) return;
    const doneTs=+TD_DONE[r.username]||0;
    if(doneTs && doneTs>=t) return;          // 标记已跟后还没有新互动
    const s=scoreOf(r);
    if(s.value<minScore) return;
    cand.push({u:r.username,v:s.value});
  });
  cand.sort((a,b)=>b.v-a.v);
  return cand.slice(0,Math.max(1,+TD_RULE.limit||15)).map(x=>x.u);
}
/* 当天名单只在跨天/改规则时生成一次：跟一个少一个，不会刚跟完又补一个新面孔 */
function ensureToday(){
  const d=todayStr();
  const users=Array.isArray(TD_TODAY.users)?TD_TODAY.users:[];
  // 名单里的人在当前数据里找不到（换了微信号 / 客户被删），说明这份名单作废了，重新生成
  const stale=TD_TODAY.date!==d || users.some(u=>!findRow(u));
  if(!stale) return false;
  TD_TODAY={date:d,users:autoCandidates()};
  postJSON('/todo',{action:'generate',date:d,usernames:TD_TODAY.users});
  return true;
}
function todoItems(){
  const out=[], seen={};
  TD_MANUAL.forEach(u=>{
    if(seen[u]) return;
    const r=findRow(u); if(!r) return;
    seen[u]=1;
    out.push({r:r,s:scoreOf(r),kind:'manual',days:daysOf(r)});
  });
  if(TD_RULE.enabled){
    ensureToday();
    const day=(TD_TODAY.date===todayStr()&&Array.isArray(TD_TODAY.users))?TD_TODAY.users:[];
    const skip=TD_RULE.skip_status||[];
    day.forEach(u=>{
      if(seen[u]) return;
      if(TD_SNOOZE[u]===todayStr()) return;
      const r=findRow(u); if(!r) return;
      if(skip.indexOf(statusOf(r))>=0) return;   // 生成之后又改了状态，这里再兜一层
      const doneTs=+TD_DONE[u]||0, t=lastTouch(r);
      if(doneTs && doneTs>=t) return;
      const d=daysOf(r);
      if(d!==null && d<1) return;               // 今天已经聊过，不必再提醒
      seen[u]=1;
      out.push({r:r,s:scoreOf(r),kind:'auto',days:daysOf(r)});
    });
  }
  return out;
}
function todoWhy(it){
  const s=it.s;
  if(it.kind==='manual')
    return '手动加入 · 价值分 '+s.value+(it.days!==null&&it.days!==undefined?(' · 上次互动 '+it.days+' 天前'):' · 还没有往来记录');
  const seg=[(it.days===null||it.days===undefined)?'还没有往来记录':(it.days+' 天没互动'),'价值分 '+s.value];
  if(s.tags.length) seg.push(s.tags.join('、'));
  return seg.join(' · ');
}
function openTodo(){
  syncRuleUI();
  $('overlay').classList.add('show');
  $('todo').classList.add('show');
  renderTodo();
  $('tdbody').scrollTop=0;
}
function closeTodo(){
  $('todo').classList.remove('show');
  if(!$('drawer').classList.contains('show') && !$('modal').classList.contains('show'))
    $('overlay').classList.remove('show');
}
function closeTop(){
  if($('modal').classList.contains('show')){
    $('modal').classList.remove('show'); $('modal').classList.remove('wide'); return;
  }
  if($('todo').classList.contains('show')){ closeTodo(); return; }
  closeAll();
}
function renderTodo(){
  const items=TODO_ITEMS;
  $('tdcount').textContent=items.length;
  $('tdn').textContent=items.length;
  const doneToday=Object.keys(TD_DONE).filter(u=>+TD_DONE[u]>=startOfToday()).length;
  const autoN=items.filter(x=>x.kind==='auto').length;
  $('tdstat').textContent='今天已跟 '+doneToday+' 位 · 手动加入 '+TD_MANUAL.length+' 位 · 自动推荐 '+autoN+' 位';
  if(!items.length){
    $('tdbody').innerHTML='<div class="empty">今天没有需要跟进的客户<br>'
      +'<span style="font-size:12px">可以在列表里点客户名 →「加入今日」，或调低上面的门槛</span></div>';
    return;
  }
  let html='', last='';
  items.forEach(it=>{
    if(it.kind!==last){
      html+=`<div class="tdgroup">${it.kind==='manual'?'手动加入':'自动推荐'}</div>`;
      last=it.kind;
    }
    const u=enc(it.r.username), nm=enc(it.r.name);
    const st=statusOf(it.r), sc=statusColor(st), tags=it.s.tags.slice(0,2);
    html+=`<div class="tditem">
      <div class="vscore ${scoreClass(it.s.value)}" title="${esc(scoreReason(it.r,it.s))}">${it.s.value}<span>分</span></div>
      <div class="tdinfo">
        <div class="tdname"><span class="nm">${esc(it.r.name)}</span>
          ${st?`<span class="stchip" style="background:${tint(sc,.13)};color:${sc}"><i style="background:${sc}"></i>${esc(st)}</span>`:''}
          ${tags.map(t=>tagChipHtml(t)).join('')}
        </div>
        <div class="tdwhy">${esc(todoWhy(it))}</div>
      </div>
      <button class="mini2" onclick="openTplPicker('${u}')" title="选一条话术复制">话术</button>
      <button class="mini2" onclick="openChatByName('${nm}')" title="切到微信打开这个聊天">打开</button>
      <button class="tddone" onclick="markDone('${u}')" title="今天已经跟过了，从列表移除">✓ 已跟</button>
      <button class="tdx" onclick="todoSkip('${u}','${it.kind}')" title="${it.kind==='manual'?'移出今日待办':'今天先不跟'}">×</button>
    </div>`;
  });
  $('tdbody').innerHTML=html;
}
function markDone(u){
  u=dec(u);
  const r=findRow(u), wasManual=TD_MANUAL.indexOf(u)>=0;
  TD_DONE[u]=Date.now()/1000;
  TD_MANUAL=TD_MANUAL.filter(x=>x!==u);
  delete TD_SNOOZE[u];
  postJSON('/todo',{action:'done',username:u});
  render();
  toast('已跟：'+(r?r.name:u)+'（已移出今日必跟）',()=>undoDone(u,wasManual));
}
async function undoDone(u,wasManual){
  delete TD_DONE[u];
  await postJSON('/todo',{action:'undo',username:u});
  if(wasManual&&TD_MANUAL.indexOf(u)<0){
    TD_MANUAL.push(u);
    await postJSON('/todo',{action:'add',username:u});
  }
  render();
  toast('已撤销');
}
function todoSkip(u,kind){
  u=dec(u);
  if(kind==='manual'){
    TD_MANUAL=TD_MANUAL.filter(x=>x!==u);
    postJSON('/todo',{action:'remove',username:u});
    toast('已移出今日待办');
  }else{
    TD_SNOOZE[u]=todayStr();
    postJSON('/todo',{action:'snooze',username:u,date:todayStr()});
    toast('今天先不跟，明天会再出现');
  }
  render();
}
function toggleTodo(u){
  u=dec(u);
  const r=findRow(u);
  if(TD_MANUAL.indexOf(u)>=0){
    TD_MANUAL=TD_MANUAL.filter(x=>x!==u);
    postJSON('/todo',{action:'remove',username:u});
    toast('已移出今日待办');
  }else if(TD_SET[u]){
    TD_SNOOZE[u]=todayStr();
    postJSON('/todo',{action:'snooze',username:u,date:todayStr()});
    toast('今天先不跟，已从今日列表移除');
  }else{
    TD_MANUAL.push(u); delete TD_SNOOZE[u];
    postJSON('/todo',{action:'add',username:u});
    toast('已加入今日必跟'+(r?('：'+r.name):''));
  }
  render();
}
function batchTodo(){
  const users=[...sel]; if(!users.length) return;
  users.forEach(u=>{if(TD_MANUAL.indexOf(u)<0)TD_MANUAL.push(u);delete TD_SNOOZE[u]});
  postJSON('/todo',{action:'add',usernames:users});
  closeAll(); render();
  toast('已把 '+users.length+' 位客户加入今日必跟');
}
function syncRuleUI(){
  const on=!!TD_RULE.enabled;
  $('rl_on').checked=on;
  $('rl_days').value=+TD_RULE.min_days||3;
  $('rl_score').value=+TD_RULE.min_score||0;
  $('rl_limit').value=+TD_RULE.limit||15;
  ['rl_days','rl_score','rl_limit'].forEach(id=>{const el=$(id);if(el)el.disabled=!on});
  $('rl_status').innerHTML=STATUSES.map(x=>{
    const skip=(TD_RULE.skip_status||[]).indexOf(x.name)>=0;
    return `<button class="${skip?'skip':''}" onclick="toggleSkipStatus('${enc(x.name)}')" title="点一下切换是否跳过">`
      +`<i style="display:inline-block;width:6px;height:6px;border-radius:50%;background:${x.color};margin-right:4px"></i>${esc(x.name)}</button>`;
  }).join('');
}
function toggleSkipStatus(n){
  n=dec(n);
  const arr=TD_RULE.skip_status||(TD_RULE.skip_status=[]);
  const i=arr.indexOf(n);
  if(i>=0) arr.splice(i,1); else arr.push(n);
  syncRuleUI();
  saveRule(true);
}
function saveRule(quiet){
  TD_RULE.enabled=!!$('rl_on').checked;
  TD_RULE.min_days=Math.max(1,Math.min(90,+$('rl_days').value||3));
  TD_RULE.min_score=Math.max(0,Math.min(100,+$('rl_score').value||0));
  TD_RULE.limit=Math.max(1,Math.min(50,+$('rl_limit').value||15));
  TD_TODAY={date:'',users:[]};          // 规则变了，按新规则重生成当天名单
  postJSON('/todo',{action:'rule',enabled:TD_RULE.enabled,min_days:TD_RULE.min_days,
    min_score:TD_RULE.min_score,limit:TD_RULE.limit,skip_status:TD_RULE.skip_status});
  syncRuleUI(); render();
  if(!quiet) toast('提醒规则已保存');
}

/* ---------------- 批量：给选中客户打标签 / 改状态 ---------------- */
function batchTags(){
  const users=[...sel]; if(!users.length) return;
  const names=allTagNames();
  const has=u=>tagsOf({username:u});
  const all=names.filter(t=>users.every(u=>has(u).indexOf(t)>=0));
  const some=names.filter(t=>users.some(u=>has(u).indexOf(t)>=0)&&all.indexOf(t)<0);
  $('mbody').innerHTML=`
    <div class="mh">给 ${users.length} 位客户打标签<button class="x" onclick="closeAll()">关闭</button></div>
    <div class="mb">
      <div class="mhint">点一下 = 加到所有选中客户；再点一下（实心）= 从所有人身上移除。实心表示所有人都已有，描边表示部分人已有。</div>
      <div class="dchips">${names.map(t=>{
        const on=all.indexOf(t)>=0, part=some.indexOf(t)>=0, c=tagColor(t);
        return `<button class="qtag" style="${on||part?'border-style:solid':''};${on?'background:'+tint(c,.14)+';border-color:'+c+';':(part?'border-color:'+c+';':'')}color:${on||part?c:'#5b6270'}" onclick="batchToggleTag('${enc(t)}')"><i style="background:${c}"></i>${esc(t)}</button>`;
      }).join('')}</div>
      <div class="newtag">
        <input type="text" id="btname" placeholder="新建标签并加给所有选中客户" onkeydown="if(event.key==='Enter'){event.preventDefault();batchCreateTag()}">
        <button class="mini primary" onclick="batchCreateTag()">新建并添加</button>
      </div>
    </div>`;
  $('overlay').classList.add('show');
  $('modal').classList.add('show');
}
function batchToggleTag(t){
  t=dec(t);
  const users=[...sel]; if(!users.length) return;
  const haveAll=users.every(u=>tagsOf({username:u}).indexOf(t)>=0);
  users.forEach(u=>{
    let cur=tagsOf({username:u}).slice();
    if(haveAll) cur=cur.filter(x=>x!==t);
    else if(cur.indexOf(t)<0) cur.push(t);
    if(cur.length) TAGS[u]=cur; else delete TAGS[u];
  });
  postJSON('/batch',{usernames:users,tags_add:haveAll?[]:[t],tags_remove:haveAll?[t]:[]});
  render(); batchTags();
  toast(haveAll?('已移除标签：'+t):('已给 '+users.length+' 位客户加上：'+t));
}
function batchCreateTag(){
  const el=$('btname');
  const name=((el&&el.value)||'').trim().replace(/[,，、;；]/g,'');
  if(!name){toast('先输入标签名');return}
  if(!TAGCOLORS[name]) TAGCOLORS[name]=PALETTE[allTagNames().length%PALETTE.length];
  const users=[...sel];
  users.forEach(u=>{
    const cur=tagsOf({username:u}).slice();
    if(cur.indexOf(name)<0) cur.push(name);
    TAGS[u]=cur;
  });
  postJSON('/batch',{usernames:users,tags_add:[name],tag_colors:TAGCOLORS});
  render(); batchTags();
  toast('已新建标签「'+name+'」并加给 '+users.length+' 位客户');
}
function batchStatus(){
  const users=[...sel]; if(!users.length) return;
  $('mbody').innerHTML=`
    <div class="mh">把 ${users.length} 位客户的状态改为<button class="x" onclick="closeAll()">关闭</button></div>
    <div class="mb">
      <div class="mhint">点一下立即生效，列表会刷新。</div>
      <div class="stbtns">
        ${STATUSES.map(x=>`<button class="stbtn" style="color:${x.color}" onclick="applyBatchStatus('${enc(x.name)}')"><i style="background:${x.color}"></i>${esc(x.name)}</button>`).join('')}
        <button class="stbtn" onclick="applyBatchStatus('')"><i style="background:#c9ced6"></i>未设置</button>
      </div>
    </div>`;
  $('overlay').classList.add('show');
  $('modal').classList.add('show');
}
function applyBatchStatus(st){
  st=dec(st);
  const users=[...sel]; if(!users.length) return;
  users.forEach(u=>{if(st)STATUS[u]=st; else delete STATUS[u]});
  postJSON('/batch',{usernames:users,status:st});
  closeAll(); render();
  toast('已把 '+users.length+' 位客户状态改为「'+(st||'未设置')+'」');
}

/* ---------------- 快捷话术模板 ---------------- */
const TEMPLATES=(SCORE.templates||[]).map(t=>Object.assign({pinned:false,cat:'未分类'},t));
let TPL_CATS=(SCORE.template_cats||[]).slice();
let tplUser='', tplQuery='', tplEditId='';
function clip(s,n){s=String(s||'').replace(/\s+/g,' ').trim();return s.length>n?s.slice(0,n)+'…':s}
function todayStr(){const d=new Date(),p=n=>(n<10?'0':'')+n;return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())}
function tplVars(r){
  return {'客户名':r.name||'',
    '编号':((String(r.name||'').match(/^\d{3,6}/))||[''])[0],
    '上次话题':clip(r.last_text,60)||'（对方还没回复过）',
    '我上次说的':clip(r.my_last_text,60)||'（我还没发过消息）',
    '最后联系':r.my_last_ts?rel(r.my_last_ts):(r.last_ts?rel(r.last_ts):'（无记录）'),
    '日期':todayStr()};
}
function renderTpl(text,r){
  const v=tplVars(r||{});
  return String(text||'').replace(/\{\{\s*([^{}]{1,20}?)\s*\}\}/g,(m,k)=>v[k]!==undefined?v[k]:m);
}
function sortTpls(arr){
  const order=TPL_CATS.slice();
  arr.forEach(t=>{if(order.indexOf(t.cat)<0)order.push(t.cat)});
  return arr.slice().sort((a,b)=>(b.pinned?1:0)-(a.pinned?1:0)
    ||(+b.pinned_at||0)-(+a.pinned_at||0)
    ||order.indexOf(a.cat)-order.indexOf(b.cat)
    ||String(a.title).localeCompare(String(b.title),'zh'));
}
async function copyText(txt){
  try{
    if(navigator&&navigator.clipboard&&navigator.clipboard.writeText){
      await navigator.clipboard.writeText(txt); return true;
    }
  }catch(e){}
  return await postJSON('/copy',{text:txt});
}
function openTplPicker(u){
  tplUser=dec(u); tplQuery=''; tplEditId='';
  const r=findRow(tplUser)||{name:''};
  $('modal').classList.add('wide');
  $('mbody').innerHTML=`
    <div class="mh">给「${esc(r.name)}」选一条话术
      <button class="x" onclick="openTplManager()">管理模板</button>
      <button class="x" onclick="closeAll()">关闭</button>
    </div>
    <div class="mb">
      <input class="tplsearch" id="tplq" placeholder="搜索话术（标题 / 分类 / 内容）" oninput="tplQuery=this.value;renderTplList()">
      <div id="tpllist"></div>
    </div>`;
  $('overlay').classList.add('show');
  $('modal').classList.add('show');
  renderTplList();
  const q=$('tplq'); if(q&&q.focus) q.focus();
}
function tplItemHtml(t,r){
  return `<div class="tplitem" onclick="useTpl('${t.id}',false)">
    <div style="min-width:0;flex:1">
      <div class="tt">${t.pinned?'<span class="pinmark">置顶</span>':''}${esc(t.title)}<span class="tcat" style="font-size:11px;color:var(--muted);border:1px solid var(--line);border-radius:9px;padding:0 6px">${esc(t.cat)}</span></div>
      <div class="tp">${esc(renderTpl(t.text,r))}</div>
    </div>
    <button class="go" title="复制并直接打开该客户聊天" onclick="event.stopPropagation();useTpl('${t.id}',true)">复制并打开</button>
  </div>`;
}
function renderTplList(){
  const box=$('tpllist'); if(!box) return;
  const r=findRow(tplUser)||{};
  const q=(tplQuery||'').trim().toLowerCase();
  const items=TEMPLATES.filter(t=>!q||((t.title||'')+(t.text||'')+(t.cat||'')).toLowerCase().includes(q));
  if(!items.length){ box.innerHTML='<div class="empty" style="padding:26px">没有匹配的话术，点右上「管理模板」可新建</div>'; return; }
  const order=TPL_CATS.slice();
  TEMPLATES.forEach(t=>{if(order.indexOf(t.cat)<0)order.push(t.cat)});
  const pinned=sortTpls(items.filter(t=>t.pinned));
  let html='';
  if(pinned.length) html+=`<div class="catrow">置顶</div>`+pinned.map(t=>tplItemHtml(t,r)).join('');
  order.forEach(c=>{
    const arr=items.filter(t=>!t.pinned&&t.cat===c);
    if(arr.length) html+=`<div class="catrow">${esc(c)} <span style="font-weight:400">${arr.length}</span></div>`+arr.map(t=>tplItemHtml(t,r)).join('');
  });
  const rest=items.filter(t=>!t.pinned&&order.indexOf(t.cat)<0);
  if(rest.length) html+=`<div class="catrow">其它</div>`+rest.map(t=>tplItemHtml(t,r)).join('');
  box.innerHTML=html;
}
async function useTpl(id, alsoOpen){
  const t=TEMPLATES.filter(x=>x.id===id)[0]; if(!t) return;
  const r=findRow(tplUser)||{};
  const txt=renderTpl(t.text,r);
  const ok=await copyText(txt);
  if(ok){
    toast('已复制，去微信粘贴：'+t.title);
    closeAll();
    if(alsoOpen) openChat(null,enc(r.name||''));
  }else{
    toast('复制失败，请手动选中文本复制');
    $('mbody').innerHTML=`<div class="mh">手动复制<button class="x" onclick="closeAll()">关闭</button></div>
      <div class="mb"><div class="mhint">浏览器不让自动写剪贴板，下面这段已选中，按 Ctrl+C 即可。</div>
      <textarea id="manualtxt" style="width:100%;min-height:170px;padding:10px;border:1px solid var(--line);border-radius:8px;font-size:13px;line-height:1.6;box-sizing:border-box">${esc(txt)}</textarea></div>`;
    const el=$('manualtxt');
    if(el&&el.select){ el.focus(); el.select(); }
  }
}
function openTplManager(){
  tplEditId='';
  $('modal').classList.add('wide');
  $('mbody').innerHTML=`
    <div class="mh">话术模板管理<button class="x" onclick="closeAll()">关闭</button></div>
    <div class="mb">
      <div class="mhint">话术里的 <b>{{客户名}}</b>、<b>{{上次话题}}</b>、<b>{{我上次说的}}</b> 等变量，复制时会自动换成该客户的信息。常用话术点「置顶」会排在最前面。</div>
      <button class="mini2" style="border-color:var(--green);color:var(--green)" onclick="tplForm('')">+ 新建话术</button>
      <div style="margin-top:10px">
      ${TEMPLATES.length?sortTpls(TEMPLATES).map(t=>`
        <div class="trow">
          <span class="ttl">${t.pinned?'<span class="pinmark">置顶</span>':''}${esc(t.title)}</span>
          <span class="tcat">${esc(t.cat)}</span>
          <button class="mini2" style="min-width:64px" onclick="togglePin('${t.id}')">${t.pinned?'取消置顶':'置顶'}</button>
          <button class="mini2" onclick="tplForm('${t.id}')">编辑</button>
          <button class="mini2" onclick="delTpl('${t.id}')">删除</button>
        </div>`).join(''):'<div class="empty" style="padding:24px">还没有话术，点上面「新建话术」</div>'}
      </div>
    </div>`;
  $('overlay').classList.add('show');
  $('modal').classList.add('show');
}
function tplForm(id){
  tplEditId=id||'';
  const t=id?(TEMPLATES.filter(x=>x.id===id)[0]||{}):{title:'',cat:TPL_CATS[0]||'首次激活',text:'',pinned:false};
  $('mbody').innerHTML=`
    <div class="mh">${id?'编辑话术':'新建话术'}<button class="x" onclick="openTplManager()">返回列表</button></div>
    <div class="mb tform">
      <label>标题</label>
      <input type="text" id="tf_title" value="${esc(t.title||'')}" placeholder="例如：首次激活 · 开场白">
      <label>场景分类</label>
      <input type="text" id="tf_cat" list="tf_cats" value="${esc(t.cat||'')}" placeholder="首次激活 / 跟进中 / 促单 / 售后，可自己新建">
      <datalist id="tf_cats">${TPL_CATS.map(c=>`<option value="${esc(c)}">`).join('')}</datalist>
      <label>话术内容</label>
      <textarea id="tf_text" placeholder="在这里写话术，可用变量...">${esc(t.text||'')}</textarea>
      <div class="varbar">${['客户名','编号','上次话题','我上次说的','最后联系','日期'].map(v=>`<button onclick="insertVar('${v}')">{{${v}}}</button>`).join('')}</div>
      <label style="display:flex;align-items:center;gap:7px;font-weight:400;margin-top:14px"><input type="checkbox" id="tf_pin" ${t.pinned?'checked':''}> 置顶这条话术</label>
      <div class="df" style="padding:14px 0 0;border:0;background:transparent">
        <button onclick="openTplManager()">取消</button>
        <button class="primary" onclick="saveTpl()">保存</button>
      </div>
    </div>`;
  const el=$('tf_text'); if(el&&el.focus) el.focus();
}
function insertVar(v){
  const el=$('tf_text'); if(!el) return;
  const token='{{'+v+'}}';
  const st=(el.selectionStart===undefined||el.selectionStart===null)?el.value.length:el.selectionStart;
  const en=(el.selectionEnd===undefined||el.selectionEnd===null)?el.value.length:el.selectionEnd;
  el.value=el.value.slice(0,st)+token+el.value.slice(en);
  if(el.setSelectionRange){ el.setSelectionRange(st+token.length,st+token.length); if(el.focus) el.focus(); }
}
async function postJSONFull(path,body){
  if(window.pywebview&&window.pywebview.api&&window.pywebview.api.open_chat){ toast('桌面窗口模式不支持保存，请用 bat 启动的浏览器版'); return null; }
  if(noBackend){ return null; }
  try{
    const j=await (await fetchRetry(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
    if(!j.ok) toast('保存失败：'+(j.msg||'未知原因'));
    noteOk();
    return j.ok?j:null;
  }catch(e){ noteFail(); toast('连不上本地服务（黑窗口可能被关了），这次改动只在页面上，关掉重开 bat 就恢复'); return null; }
}
async function saveTpl(){
  const g=id=>((($(id)||{}).value)||'').trim();
  const body={id:tplEditId,title:g('tf_title').slice(0,60),cat:g('tf_cat')||'未分类',
    text:(($('tf_text')||{}).value)||'',pinned:!!((($('tf_pin')||{}).checked))};
  if(!body.title||!body.text.trim()){ toast('标题和内容都要填'); return; }
  if(body.cat&&TPL_CATS.indexOf(body.cat)<0) TPL_CATS.push(body.cat);
  const local=TEMPLATES.filter(x=>x.id===body.id)[0];
  const isNew=!local;
  if(local){ Object.assign(local,body); }
  else { TEMPLATES.push(Object.assign({},body,{id:'tmp'+Date.now(),pinned:!!body.pinned,pinned_at:body.pinned?Date.now()/1000:0})); }
  const j=await postJSONFull('/save_template',{template:body});
  if(j&&j.templates){ TEMPLATES.splice(0,TEMPLATES.length,...j.templates); }
  toast(isNew?'话术已新建':'话术已保存');
  openTplManager();
}
async function delTpl(id){
  const t=TEMPLATES.filter(x=>x.id===id)[0];
  if(!confirm('删除话术「'+(t?t.title:'')+'」？删除后不可恢复。')) return;
  const j=await postJSONFull('/delete_template',{id:id});
  if(j&&j.templates){ TEMPLATES.splice(0,TEMPLATES.length,...j.templates); }
  else { const left=TEMPLATES.filter(x=>x.id!==id); TEMPLATES.splice(0,TEMPLATES.length,...left); }
  toast('已删除');
  openTplManager();
}
async function togglePin(id){
  const t=TEMPLATES.filter(x=>x.id===id)[0]; if(!t) return;
  t.pinned=!t.pinned;
  t.pinned_at=t.pinned?Date.now()/1000:0;
  await postJSONFull('/pin_template',{id:id,pinned:t.pinned});
  toast(t.pinned?'已置顶':'已取消置顶');
  openTplManager();
}

$('q').addEventListener('input',e=>{query=e.target.value;render()});
$('tot').textContent=DATA.reduce((s,r)=>s+r.replies,0);
$('hot').textContent=DATA.filter(r=>r.recent30>0).length;
$('senttoday').textContent=DATA.filter(FILTERS.today).length;
$('c_all').textContent=DATA.length;
$('c_today').textContent=DATA.filter(FILTERS.today).length;
$('c_d3').textContent=DATA.filter(FILTERS.d3).length;
$('c_d7').textContent=DATA.filter(FILTERS.d7).length;
$('c_pending').textContent=DATA.filter(FILTERS.pending).length;
async function refreshData(){
  const b=$('rf'), old=b.textContent;
  if(window.pywebview && window.pywebview.api && window.pywebview.api.open_chat){
    toast('桌面窗口模式请关闭后重新双击「客户回复排行榜.bat」刷新'); return;
  }
  b.disabled=true; b.textContent='刷新中…';
  try{
    const j=await (await fetch('/refresh',{cache:'no-store'})).json();
    if(!j.ok){ toast(j.msg||'刷新失败'); b.disabled=false; b.textContent=old; return; }
    toast('正在重新读取微信数据，约需 30-60 秒…');
    const timer=setInterval(async()=>{
      try{
        const s=await (await fetch('/refresh_status',{cache:'no-store'})).json();
        if(s.running) return;
        clearInterval(timer);
        if(s.error){ toast('刷新失败：'+s.error); b.disabled=false; b.textContent=old; }
        else { toast('已更新 '+s.count+' 位客户，正在重新加载…'); setTimeout(()=>location.reload(),700); }
      }catch(e){}
    },1500);
  }catch(e){ noteFail(); toast('本地服务没响应——点页面顶部的「重新连接」'); b.disabled=false; b.textContent=old; }
}
async function openChat(btn,name){
  const old=btn?btn.textContent:'';
  if(btn){btn.disabled=true;btn.textContent='唤起中…'}
  toast('正在切到微信并打开聊天…');
  try{
    let j;
    if(window.pywebview && window.pywebview.api && window.pywebview.api.open_chat){
      j=await window.pywebview.api.open_chat(dec(name));   // 桌面窗口：直接调本地Python
    }else{
      let resp;
      try{ resp=await fetchRetry('/open?name='+name,undefined,3,800); }  // 浏览器：走本地服务（服务端会解码）
      catch(e1){ throw e1; }
      j=await resp.json();
    }
    noteOk();
    toast(j.ok?'已打开：'+j.name:'打开失败：'+(j.msg||'未知原因'));
  }catch(e){
    noteFail();
    toast('本地服务没响应——页面顶部已给出重连按钮，点一下就好');
  }
  if(btn){btn.disabled=false;btn.textContent=old}
}
// Esc 关闭抽屉/弹窗（WebView 与浏览器都生效）
if(document.addEventListener){
  document.addEventListener('keydown',e=>{
    if(e.key==='Escape') closeTop();
    if(e.key==='/'&&document.activeElement&&document.activeElement.id!=='q'){
      const q=$('q'); if(q&&q.focus){e.preventDefault();q.focus()}
    }
  });
  const _tt=$('toast');
  if(_tt&&_tt.addEventListener) _tt.addEventListener('click',()=>{
    const f=_tt._undo; if(!f) return;
    _tt._undo=null; _tt.classList.remove('show'); f();
  });
}
render();
// 环境探测：桌面接口 > 本页真后端 > 本机正式服务(自动跳转) > 静态预览(禁用按钮)
const SRV_PORT = __PORT__;
(async function probe(){
  if(window.pywebview && window.pywebview.api && window.pywebview.api.open_chat) return; // 桌面窗口

  // 1) 本页背后是不是真后端——必须校验返回是 JSON，静态服务返回200不算数
  try{
    const r = await fetch('/ping', {cache:'no-store'});
    if(r.ok){
      const j = await r.json().catch(()=>null);
      if(j && j.ok === true){ startHealthWatch(); return; }   // 真后端，按钮可用并开始心跳
    }
  }catch(e){}

  // 2) 不是真后端：本机正式服务若在跑，自动跳过去，避免在旧预览页上白点
  try{
    const r2 = await fetch(`http://127.0.0.1:${SRV_PORT}/ping`, {cache:'no-store'});
    if(r2.ok){
      const j2 = await r2.json().catch(()=>null);
      if(j2 && j2.ok === true){
        $('envwarn').innerHTML = '<b>检测到正式版正在运行，正在跳转…</b>';
        $('envwarn').style.display = 'block';
        setTimeout(()=>{ location.href = `http://127.0.0.1:${SRV_PORT}/`; }, 1200);
        return;
      }
    }
  }catch(e){}

  // 3) 都没有 = 服务没启动，禁用按钮并提示
  noBackend = true;
  $('envwarn').style.display = 'block';
  render();
})();
</script>
</body>
</html>"""


# ----------------------------- 微信自动化 -----------------------------
def find_wechat_main() -> tuple[int | None, bool]:
    """
    找到微信主窗口句柄，返回 (hwnd, 是否最小化)。
    注意：微信最小化时窗口不可见、rect 变成屏幕外小方块，不能按可见性/尺寸过滤，
    否则用户点"打开聊天"时（微信常处于最小化）会找不到窗口。
    微信 4.x 主窗口特征：进程 Weixin.exe + 类名含 Qt + 标题"微信"。
    """
    import win32gui, win32process, psutil
    wx_pids = set()
    for p in psutil.process_iter(["pid", "name"]):
        n = p.info["name"] or ""
        if n.lower() in ("weixin.exe", "wechat.exe"):
            wx_pids.add(p.info["pid"])
    best = None          # 首选：标题正好是"微信"的主窗口
    fallback = None      # 次选：标题含微信/Qt 的较大窗口
    def cb(hwnd, _):
        nonlocal best, fallback
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            return
        if pid not in wx_pids:
            return
        cls = win32gui.GetClassName(hwnd)
        if "Qt" not in cls:
            return
        title = win32gui.GetWindowText(hwnd)
        iconic = bool(win32gui.IsIconic(hwnd))
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        w, h = r - l, b - t
        if title == "微信":
            best = (hwnd, iconic)
        elif (w > 300 and h > 200) or iconic:
            fallback = (hwnd, iconic)
    win32gui.EnumWindows(cb, None)
    return best or fallback or (None, False)


def activate_wechat(hwnd: int, minimized: bool) -> bool:
    """把微信主窗口恢复到前台，成功返回 True。"""
    import win32gui, win32con
    if minimized:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    else:
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
    for attempt in range(3):
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            import pyautogui
            pyautogui.press("alt")  # 解除 Windows 前台锁定
        time.sleep(0.35)
        if win32gui.GetForegroundWindow() == hwnd:
            return True
    return win32gui.GetForegroundWindow() == hwnd


def open_chat(name: str, dry_run: bool = False) -> dict:
    """
    打开指定客户的聊天：激活微信 -> Ctrl+F -> 粘贴名字 -> 回车打开结果。
    绝不发送任何消息；回车只作用于搜索框。
    """
    steps = [
        "1. 定位并激活微信主窗口",
        "2. Ctrl+F 聚焦搜索框",
        "3. Ctrl+A 清空 + Ctrl+V 粘贴客户名：" + name,
        "4. 等待搜索结果",
        "5. Enter 打开第一个匹配结果（不发送）",
    ]
    if dry_run:
        print("[DRY-RUN] 将执行以下步骤（不实际按键）：")
        for s in steps:
            print("   ", s)
        return {"ok": True, "name": name, "dry_run": True}

    with _action_lock:
        try:
            import pyautogui, pyperclip
            pyautogui.PAUSE = 0.1
            pyautogui.FAILSAFE = True  # 鼠标甩到左上角可紧急中止

            hwnd, minimized = find_wechat_main()
            if hwnd is None:
                return {"ok": False, "name": name, "msg": "没找到微信主窗口，请先打开并登录微信"}
            if not activate_wechat(hwnd, minimized):
                return {"ok": False, "name": name,
                        "msg": "无法把微信切到前台，请手动点一下微信窗口再重试"}
            time.sleep(0.4)

            pyautogui.hotkey("ctrl", "f")   # 聚焦搜索框
            time.sleep(0.45)
            pyperclip.copy(name)             # 中文用剪贴板，避开输入法
            pyautogui.hotkey("ctrl", "a")
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.9)                  # 等搜索结果出现
            pyautogui.press("enter")         # 打开第一个匹配（搜索框内，非发送）
            time.sleep(0.5)
            return {"ok": True, "name": name}
        except Exception as e:
            return {"ok": False, "name": name, "msg": f"自动化出错: {e}"}


# ----------------------------- 本地服务 -----------------------------
def _num(v):
    """整数就别写成 40.0，保持 config.json 清爽。"""
    f = float(v)
    return int(f) if f.is_integer() else f


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # 静默

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_POST(self):
        """保存评分权重 / 标签 / 手动分。前端改完即时重算，无需重扫数据库。"""
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception as e:
            self._json({"ok": False, "msg": f"请求体解析失败: {e}"})
            return
        cfg = load_config()
        try:
            if parsed.path == "/note":
                username = str(body.get("username") or "").strip()
                if not username:
                    self._json({"ok": False, "msg": "缺少 username"}, 400)
                elif body.get("action") == "delete":
                    notes = del_note(cfg, username, str(body.get("id") or ""))
                    rewrite_html(cfg)
                    self._json({"ok": True, "notes": notes})
                else:
                    notes = add_note(cfg, username, str(body.get("text") or ""))
                    rewrite_html(cfg)
                    self._json({"ok": True, "notes": notes})
            elif parsed.path == "/save_score":
                sc = cfg.setdefault("score", {})
                w = body.get("weights") or {}
                sc["weights"] = {k: _num(w.get(k, sc.get("weights", {}).get(k, 0)))
                                 for k in ("recency", "reply", "volume")}
                if body.get("half_life_days"):
                    sc["half_life_days"] = _num(max(1.0, float(body["half_life_days"])))
                if body.get("volume_cap"):
                    sc["volume_cap"] = max(10, int(body["volume_cap"]))
                if isinstance(body.get("tag_weights"), dict):
                    sc["tag_weights"] = {str(k): _num(v) for k, v in body["tag_weights"].items()}
                save_config(cfg)
                rewrite_html(cfg)
                self._json({"ok": True})
            elif parsed.path == "/set_tags":
                key = str(body.get("username") or body.get("name") or "")
                tags = [str(t).strip() for t in (body.get("tags") or []) if str(t).strip()]
                tags_map = cfg.setdefault("tags", {})
                if tags:
                    tags_map[key] = tags
                else:
                    tags_map.pop(key, None)
                # 顺手登记新标签的颜色（页面新建标签时一并带上）
                colors = body.get("tag_colors")
                if isinstance(colors, dict):
                    tc = cfg.setdefault("tag_colors", {})
                    for k, v in colors.items():
                        if str(k).strip() and str(v).startswith("#"):
                            tc[str(k).strip()] = str(v)
                save_config(cfg)
                rewrite_html(cfg)
                self._json({"ok": True, "tags": tags})
            elif parsed.path == "/set_tag_color":
                name = str(body.get("name") or "").strip()
                color = str(body.get("color") or "").strip()
                if not name or not re.match(r"^#[0-9a-fA-F]{6}$", color):
                    self._json({"ok": False, "msg": "标签名或颜色不合法"})
                    return
                cfg.setdefault("tag_colors", {})[name] = color
                save_config(cfg)
                rewrite_html(cfg)
                self._json({"ok": True, "name": name, "color": color})
            elif parsed.path == "/set_status":
                key = str(body.get("username") or body.get("name") or "")
                st = str(body.get("status") or "").strip()
                status_map = cfg.setdefault("status", {})
                if st:
                    status_map[key] = st
                else:
                    status_map.pop(key, None)
                save_config(cfg)
                rewrite_html(cfg)
                self._json({"ok": True, "status": st})
            elif parsed.path == "/batch":
                users = [str(x) for x in (body.get("usernames") or []) if str(x).strip()]
                add = [str(t).strip() for t in (body.get("tags_add") or []) if str(t).strip()]
                remove = [str(t).strip() for t in (body.get("tags_remove") or []) if str(t).strip()]
                st = body.get("status", None)
                tags_map = cfg.setdefault("tags", {})
                status_map = cfg.setdefault("status", {})
                for u in users:
                    cur = [t for t in (tags_map.get(u) or []) if t not in remove]
                    for t in add:
                        if t not in cur:
                            cur.append(t)
                    if cur:
                        tags_map[u] = cur
                    else:
                        tags_map.pop(u, None)
                    if st is not None:
                        if str(st).strip():
                            status_map[u] = str(st).strip()
                        else:
                            status_map.pop(u, None)
                colors = body.get("tag_colors")
                if isinstance(colors, dict):
                    tc = cfg.setdefault("tag_colors", {})
                    for k, v in colors.items():
                        if str(k).strip() and str(v).startswith("#"):
                            tc[str(k).strip()] = str(v)
                save_config(cfg)
                rewrite_html(cfg)
                self._json({"ok": True, "count": len(users)})
            elif parsed.path == "/set_manual":
                key = str(body.get("username") or body.get("name") or "")
                try:
                    delta = float(body.get("delta") or 0)
                except (TypeError, ValueError):
                    delta = 0
                man = cfg.setdefault("manual", {})
                if delta:
                    man[key] = _num(delta)
                else:
                    man.pop(key, None)
                save_config(cfg)
                rewrite_html(cfg)
                self._json({"ok": True, "manual": man.get(key, 0)})
            elif parsed.path == "/save_template":
                tpl = body.get("template") or {}
                title = str(tpl.get("title") or "").strip()[:60]
                text = str(tpl.get("text") or "")
                cat = str(tpl.get("cat") or "未分类").strip()[:20] or "未分类"
                if not title or not text.strip():
                    self._json({"ok": False, "msg": "标题和内容不能为空"})
                    return
                items = cfg.setdefault("templates", [])
                tid = str(tpl.get("id") or "").strip() or uuid.uuid4().hex[:8]
                pin = bool(tpl.get("pinned"))
                rec = {"id": tid, "title": title, "cat": cat, "text": text,
                       "pinned": pin,
                       "pinned_at": float(tpl.get("pinned_at") or (time.time() if pin else 0))}
                hit = next((x for x in items if isinstance(x, dict) and x.get("id") == tid), None)
                if hit is not None:
                    hit.update(rec)
                else:
                    items.append(rec)
                cats = cfg.setdefault("template_cats", [])
                if cat not in cats:
                    cats.append(cat)
                save_config(cfg)
                rewrite_html(cfg)
                self._json({"ok": True, "id": tid, "templates": items})
            elif parsed.path == "/delete_template":
                tid = str(body.get("id") or "")
                items = [x for x in (cfg.get("templates") or [])
                         if not (isinstance(x, dict) and x.get("id") == tid)]
                cfg["templates"] = items
                save_config(cfg)
                rewrite_html(cfg)
                self._json({"ok": True, "templates": items})
            elif parsed.path == "/pin_template":
                tid = str(body.get("id") or "")
                pinned = bool(body.get("pinned"))
                items = cfg.get("templates") or []
                hit = next((x for x in items if isinstance(x, dict) and x.get("id") == tid), None)
                if hit is None:
                    self._json({"ok": False, "msg": "模板不存在"})
                    return
                hit["pinned"] = pinned
                hit["pinned_at"] = time.time() if pinned else 0
                save_config(cfg)
                rewrite_html(cfg)
                self._json({"ok": True, "pinned": pinned})
            elif parsed.path == "/todo":
                # 今日必跟：加入/移出待办、标记已跟、撤销、今天先不管、改提醒规则
                todo = cfg.setdefault("todo", {})
                if not isinstance(todo, dict):
                    todo = {}
                    cfg["todo"] = todo
                todo.setdefault("manual", [])
                todo.setdefault("done", {})
                todo.setdefault("snooze", {})
                todo.setdefault("rule", dict(DEFAULT_CONFIG["todo"]["rule"]))
                act = str(body.get("action") or "")
                users = [str(x).strip() for x in (body.get("usernames") or []) if str(x).strip()]
                u = str(body.get("username") or body.get("name") or "").strip()
                if u and u not in users:
                    users.append(u)
                out = {"ok": True, "action": act}
                if act == "add":
                    man = [x for x in todo["manual"] if isinstance(x, str)]
                    for x in users:
                        if x not in man:
                            man.append(x)
                        todo["done"].pop(x, None)
                        todo["snooze"].pop(x, None)
                    todo["manual"] = man
                    out["count"] = len(users)
                elif act == "remove":
                    todo["manual"] = [x for x in todo["manual"] if x not in users]
                    out["count"] = len(users)
                elif act == "done":
                    now = time.time()
                    for x in users:
                        todo["done"][x] = now
                        todo["snooze"].pop(x, None)
                    todo["manual"] = [x for x in todo["manual"] if x not in users]
                    # 清理 30 天前的已跟记录，避免文件无限增长
                    cutoff = now - 30 * 86400
                    todo["done"] = {k: v for k, v in todo["done"].items()
                                    if isinstance(v, (int, float)) and v >= cutoff}
                    out["count"] = len(users)
                elif act == "undo":
                    todo["done"].pop(u, None)
                elif act == "snooze":
                    todo["snooze"][u] = str(body.get("date") or time.strftime("%Y-%m-%d"))
                elif act == "generate":
                    todo["today"] = {
                        "date": str(body.get("date") or time.strftime("%Y-%m-%d")),
                        "users": [x for x in users],
                    }
                    out["today"] = todo["today"]
                elif act == "rule":
                    rule = todo.setdefault("rule", {})
                    if "enabled" in body:
                        rule["enabled"] = bool(body["enabled"])
                    for k in ("min_days", "min_score", "limit"):
                        if k in body:
                            rule[k] = int(_num(body[k]))
                    if isinstance(body.get("skip_status"), list):
                        rule["skip_status"] = [str(x) for x in body["skip_status"]]
                    out["rule"] = rule
                else:
                    self._json({"ok": False, "msg": "未知操作"}, 400)
                    return
                save_config(cfg)
                rewrite_html(cfg)
                out["todo"] = todo
                self._json(out)
            elif parsed.path == "/copy":
                text = str(body.get("text") or "")
                if not text:
                    self._json({"ok": False, "msg": "内容为空"})
                    return
                try:
                    import pyperclip
                    pyperclip.copy(text)
                    self._json({"ok": True})
                except Exception as e:
                    self._json({"ok": False, "msg": f"复制失败: {e}"})
            else:
                self._json({"ok": False, "msg": "未知接口"}, 404)
        except Exception as e:
            self._json({"ok": False, "msg": str(e)})

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            html = HTML_OUT.read_text(encoding="utf-8")
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        elif parsed.path == "/ping":
            self._send(200, b'{"ok":true}', "application/json; charset=utf-8")
        elif parsed.path == "/refresh":
            if _refresh["running"]:
                self._send(200, json.dumps({"ok": False, "msg": "正在刷新中，请稍候"}).encode("utf-8"),
                           "application/json; charset=utf-8")
            else:
                threading.Thread(target=refresh_data, daemon=True).start()
                self._send(200, json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
        elif parsed.path == "/refresh_status":
            body = dict(_refresh)
            body["ok"] = True
            self._send(200, json.dumps(body, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif parsed.path == "/open":
            qs = parse_qs(parsed.query)
            name = qs.get("name", [""])[0]
            print(f"[*] 请求打开聊天: {name}")
            result = open_chat(name, dry_run=_dry_run)
            print("   ", "OK" if result.get("ok") else "FAIL: " + str(result.get("msg")))
            self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif parsed.path == "/timeline":
            qs = parse_qs(parsed.query)
            username = qs.get("username", [""])[0]
            try:
                limit = max(20, min(500, int(qs.get("limit", ["200"])[0])))
            except ValueError:
                limit = 200
            if not username:
                self._json({"ok": False, "msg": "缺少 username"}, 400)
            else:
                tl = customer_timeline(username, limit)
                if tl.get("error"):
                    self._json({"ok": False, "msg": tl["error"]})
                else:
                    self._json({"ok": True, "total": tl["total"],
                                "mine_total": tl["mine_total"], "msgs": tl["msgs"]})
        elif parsed.path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._send(404, b"not found", "text/plain")


class DesktopApi:
    """桌面窗口暴露给网页的本地接口：点击客户名直接唤起微信。"""
    def open_chat(self, name: str) -> dict:
        result = open_chat(name)
        print(f"[*] 桌面点击: {name} -> " + ("OK" if result.get("ok") else result.get("msg")))
        return result


def run_desktop(html: str):
    """用系统 WebView2 开原生桌面窗口，加载排行榜；点击直接调本地函数。"""
    import webview
    window = webview.create_window(
        "客户回复排行榜 · 点击直达聊天",
        html=html,
        js_api=DesktopApi(),
        width=1180, height=820, min_size=(900, 600),
        background_color="#f7f8fa",
    )
    print("[+] 桌面窗口已启动；点客户右侧「打开聊天」即可直达（关闭窗口即退出）")
    webview.start()


def _free_port(port: int) -> None:
    """端口被上一次没关的实例占着时先结束它。否则双击 bat 会直接报错退出，
    网页还停在旧页面，看着就像「数据不动」。只结束 python 进程，别的一概不碰。"""
    if not sys.platform.startswith("win"):
        return
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             errors="ignore", timeout=10).stdout
    except Exception:
        return
    pids = set()
    for line in out.splitlines():
        if "LISTENING" in line and re.search(r":%d\s" % port, line):
            tail = line.split()[-1].strip()
            if tail.isdigit():
                pids.add(tail)
    pids.discard(str(os.getpid()))
    for pid in pids:
        try:
            info = subprocess.run(["tasklist", "/FI", "PID eq " + pid, "/FO", "CSV", "/NH"],
                                  capture_output=True, text=True, errors="ignore",
                                  timeout=10).stdout.lower()
        except Exception:
            continue
        if "python" not in info:
            continue
        try:
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=10)
            print(f"[*] 已结束占用 {port} 端口的旧实例 (PID {pid})")
        except Exception:
            pass
    time.sleep(0.6)


def main():
    global _server, _dry_run
    ap = argparse.ArgumentParser(description="客户回复排行榜 + 点击直达聊天")
    ap.add_argument("--update", action="store_true", help="先重新解密数据库刷新数据")
    ap.add_argument("--rescan", action="store_true", help="忽略缓存重新扫描（不改数据库）")
    ap.add_argument("--scan-only", action="store_true", help="只扫描生成网页，不起服务")
    ap.add_argument("--desktop", action="store_true", help="原生桌面窗口模式（推荐，点击直达）")
    ap.add_argument("--port", type=int, default=None, help=f"服务端口（默认 {PORT}）")
    ap.add_argument("--no-browser", action="store_true", help="仅启动本地服务，不打开外部浏览器")
    ap.add_argument("--dry-run", action="store_true", help="只打印自动化步骤，不实际按键（自测用）")
    ap.add_argument("--demo", action="store_true", help="使用虚构演示数据，不读取微信数据库")
    ap.add_argument("--rule", choices=["number_prefix", "has_remark", "keyword"],
                    help="设置客户识别规则并保存到 config.json")
    ap.add_argument("--keyword", default=None, help="配合 --rule keyword 使用的关键词")
    args = ap.parse_args()
    _dry_run = args.dry_run

    cfg = load_config()
    if args.rule:
        cfg["customer_rule"] = args.rule
        if args.keyword is not None:
            cfg["customer_keyword"] = args.keyword
        elif args.rule != "keyword":
            cfg["customer_keyword"] = ""   # 切回非关键词规则时清空，避免残留
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        print(f"[*] 客户识别规则已保存: {args.rule}"
              + (f"（关键词: {cfg.get('customer_keyword')}）" if args.rule == "keyword" else ""))
    port = args.port or int(cfg.get("port") or PORT)
    print(f"[*] 当前客户口径: {cfg.get('customer_rule')}"
          + (f" / 关键词 {cfg.get('customer_keyword')}" if cfg.get("customer_rule") == "keyword" else ""))

    if args.demo:
        rows = demo_rows()
        print(f"[*] 演示模式：使用 {len(rows)} 条虚构客户数据，不读取微信数据库")
    else:
        if args.update:
            update_snapshot(cfg)
        rows = load_or_scan(force=args.update or args.rescan, cfg=cfg)
    html = build_html(rows, port, cfg)
    HTML_OUT.write_text(html, encoding="utf-8")
    print(f"[+] 排行榜网页: {HTML_OUT}")

    if args.dry_run:
        open_chat(rows[0]["name"] if rows else "测试客户", dry_run=True)
        # 继续启动服务，让 /open 也以 dry-run 模式跑，便于端到端自测
    if args.scan_only and not args.dry_run:
        return

    if args.desktop:
        run_desktop(html)
        return

    url = f"http://127.0.0.1:{port}/"
    _free_port(port)
    try:
        _server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        print(f"[!] 端口 {port} 起不来：{e}")
        print("    可能是别的程序占着这个端口，换个端口试试：python reply_ranking.py --port 8766")
        return
    print(f"[+] 服务已启动: {url}")
    print("[*] 浏览器将自动打开排行榜；关闭本窗口即停止服务")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        _server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] 服务已停止")


if __name__ == "__main__":
    main()
