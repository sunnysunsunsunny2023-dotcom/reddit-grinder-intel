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
import time
from typing import Any, Dict, List

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
# 品牌/产品关键词搜索补抓（Reddit search 端点按词检索，可捞回 new 前100 之外的历史帖）
DEFAULT_SEARCH_KEYWORDS = [
    "geimori",
    "mywirsh",
    "wirsh",
    "gu63",
    "gu64",
    "gu38",
    "t38",
]


def load_env_file(path: str = "/opt/reddit-intel/.env") -> None:
    """轻量加载 .env（不覆盖已存在的环境变量）。

    systemd 服务通过 EnvironmentFile 注入 env，但 CI 手动
    `sudo -u reddit-intel python -m collector.scheduler` 是裸环境，
    需要自己读 .env（REDDIT_PREFER_RSS / REDDIT_USER_AGENT 等）。
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


def run_once(
    subreddits: List[str],
    limit: int = 100,
    fetch_comments: bool = False,
    schema_sql_path: str = "sql/schema.sql",
    search_keywords: Optional[List[str]] = None,
) -> dict:
    """执行一次完整抓取流程，返回汇总统计。

    Args:
        search_keywords: 关键词搜索补抓列表；非空时对每个 subreddit
            按词补抓 new 前 100 之外的历史帖（Reddit search 端点）。
            传入空列表/None 则关闭搜索补抓。
    """
    database_path = os.environ.get(
        "DATABASE_PATH", "/opt/reddit-intel/data/reddit_intel.db"
    )
    conn = connect(database_path)
    try:
        init_schema(conn, schema_sql_path)
        fetcher = build_fetcher_from_env()
        comments_fetcher = CommentsFetcher(fetcher)

        summary: dict = {"runs": [], "total_posts_new": 0, "total_search_new": 0}
        for idx, subreddit in enumerate(subreddits):
            # Reddit RSS 端点对同 IP 短窗口限速（实测：4 分钟间隔没问题，
            # 30s 间隔第二次 429），多 subreddit 之间留 120s 缓冲。
            if idx > 0:
                time.sleep(120)
            run_id = start_fetch_run(conn, subreddit)
            try:
                raw_posts = fetcher.fetch_new_posts(subreddit, limit=limit)
                posts = [parse_post(r, subreddit) for r in raw_posts]
                posts = [p for p in posts if p["post_id"]]

                known = collect_existing_post_ids(conn, subreddit)
                dedupe = DedupeFilter(known)
                new_posts, existing_posts = dedupe.split(posts)

                inserted = upsert_posts(conn, new_posts)

                # 搜索补抓：按品牌词补回 new 列表外历史帖
                search_new_posts: List[Dict[str, Any]] = []
                search_found = 0
                if search_keywords:
                    search_new_posts, search_found = _run_search_backfill(
                        conn, fetcher, subreddit, search_keywords, dedupe
                    )
                search_new = len(search_new_posts)

                # 评论抓取（默认关闭；service 以 --comments 显式开启）
                comment_count = 0
                if fetch_comments:
                    all_new = new_posts + search_new_posts
                    for post in all_new:
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
                        "search_found": search_found,
                        "search_new": search_new,
                        "comments": comment_count,
                    }
                )
                summary["total_posts_new"] += inserted
                summary["total_search_new"] += search_new
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


def _run_search_backfill(
    conn,
    fetcher,
    subreddit: str,
    keywords: List[str],
    dedupe: DedupeFilter,
) -> tuple[List[Dict[str, Any]], int]:
    """按关键词搜索补抓，返回 (新帖原始 dict 列表, 搜索到的帖子总数)。

    Reddit search 端点对同 IP 请求限速严格（实测 search RSS 429 / JSON 403），
    因此把全部关键词合并为一个 OR 查询，每 subreddit 仅打 1 次搜索请求，
    减少命中限速窗口的概率。搜索到的历史帖走同一 dedupe，不重复入库。
    搜索失败不影响整体（记录 warning 后返回空列表）。
    """
    query = " OR ".join(keywords)
    try:
        raw = fetcher.search_posts(query, subreddit, limit=100, sort="new")
    except RedditFetchError as exc:
        logger.warning("Search backfill failed for r/%s q=%r: %s", subreddit, query, exc)
        return [], 0
    posts = [parse_post(r, subreddit) for r in raw]
    posts = [p for p in posts if p["post_id"] and p["created_utc"]]
    if not posts:
        return [], 0
    new_posts, _ = dedupe.split(posts)
    if new_posts:
        upsert_posts(conn, new_posts)
    return new_posts, len(posts)


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
        "--search-keywords",
        nargs="+",
        default=None,
        help="关键词搜索补抓列表；默认 None（关闭），传值如：--search-keywords geimori wirsh gu64",
    )
    parser.add_argument(
        "--schema", default="sql/schema.sql", help="schema.sql 路径"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    load_env_file()
    try:
        summary = run_once(
            subreddits=args.subreddits,
            limit=args.limit,
            fetch_comments=args.comments,
            schema_sql_path=args.schema,
            search_keywords=args.search_keywords,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Collector run failed")
        return 1

    for run in summary["runs"]:
        logger.info(
            "r/%s: found=%d new=%d existing=%d search_found=%d search_new=%d comments=%d",
            run["subreddit"], run["found"], run["new"],
            run["existing"], run["search_found"], run["search_new"],
            run["comments"],
        )
    logger.info("Total new posts inserted: %d", summary["total_posts_new"])
    logger.info("Total search-backfill new posts inserted: %d", summary["total_search_new"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
