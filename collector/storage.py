"""SQLite 存储模块。

所有生产数据只进 SQLite（spec 第 2 节：Single Source of Truth；ADR-002）。
JSONL 只允许 backup/export，不作为生产主数据。

设计要点：
- 单机低频读写，不建连接池；每次操作独立连接（write lock 由 SQLite 保证）。
- detect_types=PARSE_DECLTYPES：TIMESTAMP 列自动转 Python datetime。
- WAL 模式：读写不互斥（对少量并发更稳）。
"""
from __future__ import annotations

import datetime as dt
import json as _json
import logging
import sqlite3
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# SQLite 默认 adapter 会把 tz-aware datetime 序列化为带时区字符串，
# PARSE_DECLTYPES 的 convert_timestamp 无法解析（如 "23+00"）。
# 统一：所有 datetime 存为 naive UTC（"YYYY-MM-DD HH:MM:SS.ffffff"），读回 naive（语义 UTC）。


def _adapt_datetime_utc(value: dt.datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value.isoformat(sep=" ", timespec="microseconds")


sqlite3.register_adapter(dt.datetime, _adapt_datetime_utc)

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


def connect(db_path: str) -> sqlite3.Connection:
    """建立 SQLite 连接（配置来自环境变量 DATABASE_PATH）。"""
    conn = sqlite3.connect(
        db_path,
        detect_types=sqlite3.PARSE_DECLTYPES,
        check_same_thread=False,
        timeout=30,
    )
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn, schema_sql_path: str) -> None:
    """执行 sql/schema.sql（幂等）。"""
    with open(schema_sql_path, "r", encoding="utf-8") as fh:
        sql = fh.read()
    conn.executescript(sql)
    conn.commit()
    logger.info("Schema initialized from %s", schema_sql_path)


def start_fetch_run(
    conn, subreddit: str, started_at: Optional[str] = None
) -> int:
    """插入 fetch_runs 记录并返回 run_id。

    兼容老版本 SQLite（无 RETURNING，3.35 以下）：用 lastrowid。
    """
    cur = conn.execute(
        """
        INSERT INTO fetch_runs (started_at, subreddit, status)
        VALUES (COALESCE(?, CURRENT_TIMESTAMP), ?, 'running')
        """,
        (started_at, subreddit),
    )
    run_id = cur.lastrowid
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
    conn.execute(
        """
        UPDATE fetch_runs
           SET finished_at = CURRENT_TIMESTAMP,
               posts_found = ?,
               posts_new = ?,
               comments_found = ?,
               status = ?,
               error = ?
         WHERE run_id = ?
        """,
        (posts_found, posts_new, comments_found, status, error, run_id),
    )
    conn.commit()


def _bind_row(row: Dict[str, Any], columns) -> List[Any]:
    """把 row 转成绑定参数：raw_json（dict）序列化为 JSON 文本。"""
    out = []
    for col in columns:
        v = row.get(col)
        if col == "raw_json" and isinstance(v, dict):
            v = _json.dumps(v, ensure_ascii=False)
        out.append(v)
    return out


def upsert_posts(conn, rows: List[Dict[str, Any]]) -> int:
    """批量写入新帖（ON CONFLICT DO NOTHING），返回实际插入数。"""
    if not rows:
        return 0
    values = [_bind_row(row, POST_COLUMNS) for row in rows]
    insert_sql = f"""
        INSERT INTO reddit_posts ({", ".join(POST_COLUMNS)})
        VALUES ({", ".join(["?"] * len(POST_COLUMNS))})
        ON CONFLICT (post_id) DO NOTHING
    """
    conn.executemany(insert_sql, values)
    conn.commit()
    return len(values)


def upsert_comments(conn, rows: List[Dict[str, Any]]) -> int:
    """批量写入评论（ON CONFLICT DO NOTHING），返回实际插入数。"""
    if not rows:
        return 0
    values = [_bind_row(row, COMMENT_COLUMNS) for row in rows]
    insert_sql = f"""
        INSERT INTO reddit_comments ({", ".join(COMMENT_COLUMNS)})
        VALUES ({", ".join(["?"] * len(COMMENT_COLUMNS))})
        ON CONFLICT (comment_id) DO NOTHING
    """
    conn.executemany(insert_sql, values)
    conn.commit()
    return len(values)
