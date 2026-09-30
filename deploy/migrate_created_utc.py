#!/usr/bin/env python3
"""部署诊断：检查并强制迁移 created_utc（convert_timestamp 500 根因防护）。

用法：sudo -u reddit-intel ./.venv/bin/python deploy/migrate_created_utc.py
打印迁移前后 created_utc 的类型分布，并调用 storage._migrate_created_utc。
"""
import os
import sqlite3
import sys

# 脚本位于 deploy/ 下，运行时 python 会把脚本目录加入 sys.path，
# 需要显式把仓库根目录（/opt/reddit-intel）加入才能 import collector.*
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from collector.storage import _migrate_created_utc


def main() -> None:
    db = os.environ.get("DATABASE_PATH", "/opt/reddit-intel/data/reddit_intel.db")
    conn = sqlite3.connect(db)
    try:
        before = conn.execute(
            "SELECT typeof(created_utc), COUNT(*) FROM reddit_posts"
            " GROUP BY typeof(created_utc)"
        ).fetchall()
        print("created_utc before:", before)
        _migrate_created_utc(conn)
        after = conn.execute(
            "SELECT typeof(created_utc), substr(created_utc, 1, 19), COUNT(*)"
            " FROM reddit_posts GROUP BY typeof(created_utc), substr(created_utc, 1, 19)"
        ).fetchall()
        print("created_utc after:", after)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
