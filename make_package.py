"""
生成一份"可以给别人用"的干净分发包，自动剔除所有私人聊天数据。

剔除的东西（这些只属于你，绝对不能外传）：
  output/                    导出与清洗后的全部聊天记录
  tools/Moect/app/DataBase/  112MB 解密后的微信数据库
  tools/Moect/wx4_keys.json  数据库密钥
  .workbuddy/                项目记忆
  config.json                你自己的账号/口径配置
"""
from __future__ import annotations

import shutil
from pathlib import Path

BASE = Path(__file__).parent
DST = BASE / "分发包"

# 目录名命中即整目录跳过
SKIP_DIRS = {
    "output", ".workbuddy", "__pycache__", ".git",
    "DataBase", "分发包", ".idea", ".vscode",
}
# 文件名命中即跳过
SKIP_FILES = {
    "wx4_keys.json",        # 密钥
    "config.json",          # 你的账号与口径
    "reply_ranking.json",
    "reply_ranking_meta.json",
    "交接文档.md",           # 内部交接材料，不给同事看
}


def ignore(_dir, names):
    out = []
    for n in names:
        p = Path(_dir) / n
        if p.is_dir() and n in SKIP_DIRS:
            out.append(n)
        elif p.is_file() and (n in SKIP_FILES or n.endswith((".pyc", ".zip"))):
            out.append(n)
    return out


def dir_size(p: Path) -> float:
    total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    return total / 1024 / 1024


def main() -> None:
    print("=" * 52)
    print("  生成分发包（自动剔除私人数据）")
    print("=" * 52)

    if DST.exists():
        print("清理旧的分发包…")
        shutil.rmtree(DST)

    print("复制程序文件…")
    shutil.copytree(BASE, DST, ignore=ignore)

    # 安全自检：确认敏感内容确实没被带进去
    print("\n安全检查：")
    problems = []
    for bad_dir in ("output", "tools/Moect/app/DataBase", ".workbuddy"):
        if (DST / bad_dir).exists():
            problems.append(f"  [危险] 仍包含 {bad_dir}")
    for bad_file in ("tools/Moect/wx4_keys.json", "config.json"):
        if (DST / bad_file).exists():
            problems.append(f"  [危险] 仍包含 {bad_file}")
    if problems:
        print("\n".join(problems))
        print("\n[中止] 打包失败，请勿分发。")
        return
    print("  [通过] 不含聊天记录、数据库、密钥、配置")

    # 检查必须存在的运行文件
    must = ["reply_ranking.py", "init_setup.py", "初始化.bat",
            "客户回复排行榜.bat", "设置客户规则.bat", "tools/Moect/wx4"]
    missing = [m for m in must if not (DST / m).exists()]
    if missing:
        print("  [警告] 缺少:", ", ".join(missing))
    else:
        print("  [通过] 运行文件齐全")

    size = dir_size(DST)
    print(f"\n分发包位置: {DST}")
    print(f"体积: {size:.1f} MB（原目录含 112MB 解密数据库，已剔除）")
    print("\n把这个「分发包」文件夹压缩后发给同事，对方解压后先双击「初始化.bat」。")
    print("注意：不要连同你自己的 output 目录一起发出去。")


if __name__ == "__main__":
    main()
