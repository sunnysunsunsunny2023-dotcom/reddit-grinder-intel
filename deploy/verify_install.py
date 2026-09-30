"""部署后验证脚本：输出 SQLite 各表行数（ADR-002）。

用法：sudo -u reddit-intel ./.venv/bin/python deploy/verify_install.py
版本：v2.3（2026-09-30，collector 失败不中断验证）
"""
import os
import sqlite3
import sys


def main() -> None:
    db_path = os.environ.get(
        "DATABASE_PATH", "/opt/reddit-intel/data/reddit_intel.db"
    )
    if not os.path.exists(db_path):
        print("ERROR: database file not found:", db_path)
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    try:
        for t in (
            "reddit_posts",
            "reddit_comments",
            "fetch_runs",
            "analysis_batches",
            "analysis_results",
        ):
            try:
                cur = conn.execute("SELECT count(*) FROM " + t)
                print(t, cur.fetchone()[0])
            except sqlite3.OperationalError as exc:
                print(t, "MISSING:", exc)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
