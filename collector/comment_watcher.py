"""评论增量监控（Geimori Reply System V2.0 — Phase 1）。

背景（需求文档 4.2）：
- 现有 collector 只对"首次发现的新帖"抓评论；历史帖后续新增评论会被漏掉。
- 本模块独立回扫候选帖子（近 watch_days 内品牌相关帖 + 有评论的活跃帖），
  用 comment_id 去重后增量 upsert，返回新增评论数。

设计：
- 候选帖选择：reddit_posts 中 created_utc >= now - watch_days 的帖子，
  且（标题/正文命中 GEIMORI_KEYWORDS 或 num_comments > 0）。
  watch_days 默认 7 天（需求文档：对活跃品牌相关讨论持续跟踪、合理观察周期）。
- 请求量控制：每个 subreddit 最多处理 limit 个帖子（默认 50），
  单帖评论限制 200 条（CommentsFetcher 默认），遵守平台限速。
- 去重：读库内已有 comment_id 集合，只 upsert 新评论；返回新增数。
- 与现有 Daily/Weekly 完全解耦：只写 reddit_comments 表，不改现有逻辑。

命令行入口（供 systemd timer 调用）：
    python -m collector.comment_watcher --watch-days 7 --limit 50

权限合规：本模块只读取公开 Reddit 数据端点（同现有 collector），
不做自动发布（发布在 Phase 4，默认关闭）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import time
from typing import Any, Dict, List, Optional, Set

from app import keywords
from .comments import CommentsFetcher
from .reddit_fetcher import RedditFetchError, build_fetcher_from_env
from .storage import connect, init_schema, upsert_comments

logger = logging.getLogger(__name__)

DEFAULT_WATCH_DAYS = 7
DEFAULT_LIMIT = 50
DEFAULT_SUBREDDITS = ["pourover", "espresso"]


def collect_candidate_posts(
    conn,
    subreddit: str,
    watch_days: int = DEFAULT_WATCH_DAYS,
    limit: int = DEFAULT_LIMIT,
) -> List[Dict[str, Any]]:
    """挑选待回扫的候选帖子。

    近 watch_days 天内：
      - 标题/正文命中 GEIMORI_KEYWORDS 的帖子（品牌相关优先）
      - 其余有评论（num_comments > 0）的活跃帖
    按 created_utc 倒序，最多 limit 个（控制请求量）。

    Returns:
        帖子 dict 列表（含 post_id / subreddit / title / selftext / num_comments）。
    """
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=watch_days)
    cur = conn.execute(
        """
        SELECT post_id, subreddit, title, selftext, num_comments, created_utc
          FROM reddit_posts
         WHERE subreddit = ? AND created_utc >= ?
         ORDER BY created_utc DESC
         LIMIT ?
        """,
        (subreddit, since, limit * 4),
    )
    rows = cur.fetchall()
    brand: List[Dict[str, Any]] = []
    active: List[Dict[str, Any]] = []
    for r in rows:
        post = {
            "post_id": r[0],
            "subreddit": r[1],
            "title": r[2] or "",
            "selftext": r[3] or "",
            "num_comments": int(r[4] or 0),
            "created_utc": r[5],
        }
        text = post["title"] + " " + post["selftext"]
        if keywords.has_any(text, keywords.GEIMORI_KEYWORDS):
            brand.append(post)
        elif post["num_comments"] > 0:
            active.append(post)
    return (brand + active)[:limit]


def collect_existing_comment_ids(conn, post_ids: List[str]) -> Set[str]:
    """读取候选帖子的已知 comment_id 集合（去重）。"""
    if not post_ids:
        return set()
    placeholders = ",".join("?" * len(post_ids))
    cur = conn.execute(
        f"SELECT comment_id FROM reddit_comments WHERE post_id IN ({placeholders})",
        post_ids,
    )
    return {str(r[0]) for r in cur.fetchall()}


def watch_once(
    conn,
    subreddits: List[str] = DEFAULT_SUBREDDITS,
    watch_days: int = DEFAULT_WATCH_DAYS,
    limit: int = DEFAULT_LIMIT,
    schema_sql_path: str = "sql/schema.sql",
) -> Dict[str, Any]:
    """执行一次增量评论回扫，返回汇总统计。

    流程：
      1. init_schema（幂等，确保新 reply 表存在）
      2. 对每个 subreddit：选候选帖 → 拉评论树 → 过滤已存在 → upsert 新增
      3. 单帖失败不阻塞整体（记 warning）

    Returns:
        {"total_new_comments": int, "per_subreddit": [...]}
    """
    init_schema(conn, schema_sql_path)
    fetcher = build_fetcher_from_env()
    comments_fetcher = CommentsFetcher(fetcher)

    summary: Dict[str, Any] = {"total_new_comments": 0, "per_subreddit": []}
    for idx, subreddit in enumerate(subreddits):
        if idx > 0:
            time.sleep(120)  # 多 subreddit 间留缓冲（同 collector 策略）
        posts = collect_candidate_posts(conn, subreddit, watch_days, limit)
        if not posts:
            logger.info("r/%s: no candidate posts", subreddit)
            summary["per_subreddit"].append(
                {"subreddit": subreddit, "candidates": 0, "new_comments": 0}
            )
            continue

        known = collect_existing_comment_ids(
            conn, [p["post_id"] for p in posts]
        )
        new_total = 0
        processed = 0
        for post in posts:
            try:
                rows = comments_fetcher.fetch_comments(post["post_id"])
            except RedditFetchError as exc:
                logger.warning("r/%s post %s comments failed: %s", subreddit, post["post_id"], exc)
                continue
            fresh = [r for r in rows if r["comment_id"] not in known]
            if fresh:
                upsert_comments(conn, fresh)
                known.update(r["comment_id"] for r in fresh)
                new_total += len(fresh)
            processed += 1

        summary["total_new_comments"] += new_total
        summary["per_subreddit"].append(
            {
                "subreddit": subreddit,
                "candidates": len(posts),
                "processed": processed,
                "new_comments": new_total,
            }
        )
        logger.info(
            "r/%s: %d candidates, %d new comments",
            subreddit, len(posts), new_total,
        )
    conn.commit()
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Reddit comment incremental watcher")
    parser.add_argument("--subreddits", nargs="+", default=DEFAULT_SUBREDDITS)
    parser.add_argument("--watch-days", type=int, default=DEFAULT_WATCH_DAYS)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--schema", default="sql/schema.sql")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    database_path = os.environ.get("DATABASE_PATH", "/opt/reddit-intel/data/reddit_intel.db")
    conn = connect(database_path)
    try:
        summary = watch_once(
            conn,
            subreddits=args.subreddits,
            watch_days=args.watch_days,
            limit=args.limit,
            schema_sql_path=args.schema,
        )
        logger.info("Watch summary: %s", summary)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
