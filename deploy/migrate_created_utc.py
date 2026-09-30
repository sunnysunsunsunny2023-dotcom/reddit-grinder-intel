#!/usr/bin/env python3
"""部署诊断：检查并强制迁移 created_utc（convert_timestamp 500 根因防护）。

用法：sudo -u reddit-intel ./.venv/bin/python deploy/migrate_created_utc.py
打印迁移前后 created_utc 的类型分布，并调用 storage._migrate_created_utc。
"""
import os
import sqlite3

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
