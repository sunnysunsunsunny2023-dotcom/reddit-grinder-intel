"""Collector 主入口 / 调度器。

流程（spec 第 3 节 + Definition of Done）：
  1. 对每个 subreddit 建立 fetch_runs 记录
  2. RedditFetcher 拉取最新帖子（公开 JSON API）
  3. parser 标准化
  4. dedupe 过滤（同一 post 不重复入库）
  5. storage 入库（ON CONFLICT DO NOTHING）
  6. 更新 fetch_runs 状态

本脚本由 systemd timer / cron 触发；不处理 LLM 分析
（分析在 analyst/ 模块，ADR-001：阿里云调用 DeepSeek）。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import List

from .comments import CommentsFetcher
from .dedupe import DedupeFilter, collect_existing_post_ids
from .parser import parse_post
from .reddit_fetcher import RedditFetchError, build_fetcher_from_env
from .storage import (
    connect,
    finish_fetch_run,
    init_schema,
    start_fetch_run,
    upsert_comments,
    upsert_posts,
)

logger = logging.getLogger(__name__)

DEFAULT_SUBREDDITS = ["pourover", "espresso"]


def run_once(
    subreddits: List[str],
    limit: int = 100,
    fetch_comments: bool = False,
    schema_sql_path: str = "sql/schema.sql",
) -> dict:
    """执行一次完整抓取流程，返回汇总统计。"""
    database_path = os.environ.get(
        "DATABASE_PATH", "/opt/reddit-intel/data/reddit_intel.db"
    )
    conn = connect(database_path)
    try:
        init_schema(conn, schema_sql_path)
        fetcher = build_fetcher_from_env()
        comments_fetcher = CommentsFetcher(fetcher)

        summary: dict = {"runs": [], "total_posts_new": 0}
        for subreddit in subreddits:
            run_id = start_fetch_run(conn, subreddit)
            try:
                raw_posts = fetcher.fetch_new_posts(subreddit, limit=limit)
                posts = [parse_post(r, subreddit) for r in raw_posts]
                posts = [p for p in posts if p["post_id"]]

                known = collect_existing_post_ids(conn, subreddit)
                dedupe = DedupeFilter(known)
                new_posts, existing_posts = dedupe.split(posts)

                inserted = upsert_posts(conn, new_posts)

                comment_count = 0
                if fetch_comments:
                    for post in new_posts:
                        rows = comments_fetcher.fetch_comments(post["post_id"])
                        if rows:
                            upsert_comments(conn, rows)
                            comment_count += len(rows)

                finish_fetch_run(
                    conn,
                    run_id,
                    posts_found=len(posts),
                    posts_new=inserted,
                    comments_found=comment_count,
                )
                summary["runs"].append(
                    {
                        "subreddit": subreddit,
                        "found": len(posts),
                        "new": inserted,
                        "existing": len(existing_posts),
                        "comments": comment_count,
                    }
                )
                summary["total_posts_new"] += inserted
            except RedditFetchError as exc:
                finish_fetch_run(
                    conn, run_id, posts_found=0, posts_new=0,
                    comments_found=0, status="failed", error=str(exc),
                )
                logger.error("Fetch failed for r/%s: %s", subreddit, exc)
                raise
        conn.commit()
        return summary
    finally:
        conn.close()


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reddit Grinder collector")
    parser.add_argument(
        "--subreddits",
        nargs="+",
        default=DEFAULT_SUBREDDITS,
        help="subreddit 列表，默认 pourover espresso",
    )
    parser.add_argument("--limit", type=int, default=100, help="每个 subreddit 拉取条数")
    parser.add_argument(
        "--comments", action="store_true", help="同时拉取评论（默认关闭）"
    )
    parser.add_argument(
        "--schema", default="sql/schema.sql", help="schema.sql 路径"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        summary = run_once(
            subreddits=args.subreddits,
            limit=args.limit,
            fetch_comments=args.comments,
            schema_sql_path=args.schema,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Collector run failed")
        return 1

    for run in summary["runs"]:
        logger.info(
            "r/%s: found=%d new=%d existing=%d comments=%d",
            run["subreddit"], run["found"], run["new"],
            run["existing"], run["comments"],
        )
    logger.info("Total new posts inserted: %d", summary["total_posts_new"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
