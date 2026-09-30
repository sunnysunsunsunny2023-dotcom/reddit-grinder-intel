"""部署后验证脚本：输出数据库各表行数。

用法：sudo -u reddit-intel ./.venv/bin/python deploy/verify_install.py
"""
import os

import psycopg2


def main() -> None:
    c = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with c.cursor() as cur:
            for t in (
                "reddit_posts",
                "reddit_comments",
                "fetch_runs",
                "analysis_batches",
                "analysis_results",
            ):
                cur.execute("SELECT count(*) FROM " + t)
                print(t, cur.fetchone()[0])
    finally:
        c.close()


if __name__ == "__main__":
    main()
