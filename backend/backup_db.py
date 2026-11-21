"""数据库快照工具（只用标准库）。

为什么需要它：SQLite 是「单文件数据库」，运行时只有 yuejiao.db 一个文件在生效。
项目里曾出现 `yuejiao.db.bak_20261117_153211` 这类手工备份，散落在 backend 目录里既容易误读，
又没有保留策略。本脚本把备份统一到 backend/backups/，并按数量滚动清理。

用法（在 backend 目录）：
    python backup_db.py            # 生成一份快照，并只保留最近 5 份
    python backup_db.py --keep 10  # 自定义保留份数
    python backup_db.py --list     # 只看现有快照
    python backup_db.py --restore backups/yuejiao_20261117_190500.db
                                   # 用快照覆盖当前库（会先把当前库再备份一次）
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "yuejiao.db")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")


def _connect_ro(path):
    return sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)


def snapshot(keep=5, quiet=False):
    """在线备份（不锁库，运行中也能备份），返回快照路径。"""
    if not os.path.exists(DB_PATH):
        raise SystemExit("找不到数据库：" + DB_PATH)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = "yuejiao_%s.db" % datetime.now().strftime("%Y%m%d_%H%M%S")
    target = os.path.join(BACKUP_DIR, name)

    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)          # 官方备份 API，保证事务一致性
    finally:
        dst.close()
        src.close()

    size = os.path.getsize(target) / 1024
    if not quiet:
        print("已生成快照：backups/%s（%.1f KB）" % (name, size))

    # 滚动清理：只保留最近 keep 份
    files = sorted(
        (f for f in os.listdir(BACKUP_DIR) if f.startswith("yuejiao_") and f.endswith(".db")),
        reverse=True,
    )
    for old in files[keep:]:
        os.remove(os.path.join(BACKUP_DIR, old))
        if not quiet:
            print("  已清理旧快照：" + old)
    return target


def list_backups():
    if not os.path.isdir(BACKUP_DIR):
        print("还没有任何快照。")
        return
    files = sorted(f for f in os.listdir(BACKUP_DIR) if f.endswith(".db"))
    if not files:
        print("还没有任何快照。")
        return
    print("现有快照（%d 份）：" % len(files))
    for f in files:
        p = os.path.join(BACKUP_DIR, f)
        print("  %-34s %7.1f KB  %s" % (
            f, os.path.getsize(p) / 1024,
            datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M:%S")))


def restore(path):
    if not os.path.isabs(path):
        path = os.path.join(BASE_DIR, path)
    if not os.path.exists(path):
        raise SystemExit("快照不存在：" + path)
    check = _connect_ro(path)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SystemExit("快照文件损坏，已中止恢复。")
    finally:
        check.close()
    print("恢复前先备份当前库……")
    snapshot(quiet=True)
    src = sqlite3.connect(path)
    dst = sqlite3.connect(DB_PATH)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    print("已用 %s 覆盖当前库。" % os.path.basename(path))
    print("提醒：如果后端正在运行，请重启服务，避免连接仍持有旧数据页。")


def main():
    ap = argparse.ArgumentParser(description="yuejiao.db 快照工具")
    ap.add_argument("--keep", type=int, default=5, help="保留最近几份快照，默认 5")
    ap.add_argument("--list", action="store_true", help="只列出已有快照")
    ap.add_argument("--restore", metavar="FILE", help="用指定快照覆盖当前库")
    args = ap.parse_args()

    if args.list:
        list_backups()
    elif args.restore:
        restore(args.restore)
    else:
        snapshot(keep=max(1, args.keep))


if __name__ == "__main__":
    sys.exit(main())
