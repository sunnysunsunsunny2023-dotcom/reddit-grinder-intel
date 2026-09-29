"""PostgreSQL 存储模块。

所有生产数据只进 PostgreSQL（spec 第 2 节：Single Source of Truth）。
JSONL 只允许 backup/export，不作为生产主数据。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

POST_COLUMNS = (
    "post_id",
    "subreddit",
    "title",
    "selftext",
    "created_utc",
    "score",
    "upvote_ratio",
    "num_comments",
    "permalink",
    "flair",
    "raw_json",
)

COMMENT_COLUMNS = (
    "comment_id",
    "post_id",
    "body",
    "score",
    "created_utc",
    "depth",
    "raw_json",
)


def connect(database_url: str) -> "psycopg2.connection":
    """建立数据库连接（配置来自环境变量 DATABASE_URL）。"""
    conn = psycopg2.connect(database_url)
    conn.autocommit = False
    return conn


def init_schema(conn, schema_sql_path: str) -> None:
    """执行 sql/schema.sql（幂等）。"""
    with open(schema_sql_path, "r", encoding="utf-8") as fh:
        sql = fh.read()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    logger.info("Schema initialized from %s", schema_sql_path)


def start_fetch_run(
    conn, subreddit: str, started_at: Optional[str] = None
) -> int:
    """插入 fetch_runs 记录并返回 run_id。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fetch_runs (started_at, subreddit, status)
            VALUES (COALESCE(%s, now()), %s, 'running')
            RETURNING run_id
            """,
            (started_at, subreddit),
        )
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


def finish_fetch_run(
    conn,
    run_id: int,
    posts_found: int,
    posts_new: int,
    comments_found: int,
    status: str = "completed",
    error: Optional[str] = None,
) -> None:
    """结束 fetch_runs 记录。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE fetch_runs
               SET finished_at = now(),
                   posts_found = %s,
                   posts_new = %s,
                   comments_found = %s,
                   status = %s,
                   error = %s
             WHERE run_id = %s
            """,
            (posts_found, posts_new, comments_found, status, error, run_id),
        )
    conn.commit()


def upsert_posts(conn, rows: List[Dict[str, Any]]) -> int:
    """批量写入新帖（ON CONFLICT DO NOTHING），返回实际插入数。"""
    if not rows:
        return 0
    values = [
        [row.get(col) for col in POST_COLUMNS] for row in rows
    ]
    insert_sql = f"""
        INSERT INTO reddit_posts ({", ".join(POST_COLUMNS)})
        VALUES ({", ".join(["%s"] * len(POST_COLUMNS))})
        ON CONFLICT (post_id) DO NOTHING
    """
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, insert_sql, values)
    conn.commit()
    return len(values)


def upsert_comments(conn, rows: List[Dict[str, Any]]) -> int:
    """批量写入评论（ON CONFLICT DO NOTHING），返回实际插入数。"""
    if not rows:
        return 0
    values = [
        [row.get(col) for col in COMMENT_COLUMNS] for row in rows
    ]
    insert_sql = f"""
        INSERT INTO reddit_comments ({", ".join(COMMENT_COLUMNS)})
        VALUES ({", ".join(["%s"] * len(COMMENT_COLUMNS))})
        ON CONFLICT (comment_id) DO NOTHING
    """
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, insert_sql, values)
    conn.commit()
    return len(values)
