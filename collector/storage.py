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
    _migrate_created_utc(conn)
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


def _norm_created_utc(v: Any) -> Any:
    """把 created_utc 归一为 naive UTC datetime（TIMESTAMP 列语义）。

    覆盖三种真实形态：epoch 数字（int/float）、epoch 文本（"1790755200"）、
    ISO 文本（"2026-09-27T02:42:49" / 带 Z / 带毫秒）。返回 None 原样。
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return dt.datetime.fromtimestamp(v, tz=dt.timezone.utc).replace(tzinfo=None)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        # epoch 文本：纯数字（可带小数点）
        if s.replace(".", "", 1).isdigit():
            return dt.datetime.fromtimestamp(
                float(s), tz=dt.timezone.utc
            ).replace(tzinfo=None)
        # ISO 文本：把 'T' 换成空格（convert_timestamp 只认空格分隔），去 Z
        if "T" in s:
            s = s.replace("T", " ").rstrip("Z")
        if s.endswith("Z"):
            s = s[:-1]
        if "+" in s or "-" in s[10:]:
            try:
                parsed = dt.datetime.fromisoformat(s)
                if parsed.tzinfo is not None:
                    return parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
            except ValueError:
                pass
        try:
            return dt.datetime.fromisoformat(s)
        except ValueError:
            # 已是带空格的 ISO 文本，原样保留（convert_timestamp 可解析）
            return s
    return v


def _bind_row(row: Dict[str, Any], columns) -> List[Any]:
    """把 row 转成绑定参数：raw_json（dict）序列化为 JSON 文本；
    created_utc 归一为 naive UTC datetime（TIMESTAMP 列语义）。"""
    out = []
    for col in columns:
        v = row.get(col)
        if col == "raw_json" and isinstance(v, dict):
            v = _json.dumps(v, ensure_ascii=False)
        if col == "created_utc":
            v = _norm_created_utc(v)
        out.append(v)
    return out


def _migrate_created_utc(conn) -> None:
    """把旧库中非标准 created_utc 迁移为 "YYYY-MM-DD HH:MM:SS"（幂等）。

    早期版本存在三种形态，TIMESTAMP 列在 PARSE_DECLTYPES 下都会触发
    convert_timestamp 的 split 报错（not enough values to unpack）：
    - epoch 数字（integer/real，如 1790755200）
    - epoch 文本（text "1790755200"）
    - ISO 文本（text "2026-09-27T02:42:49"，带 'T' 无空格）
    迁移规则：
    - 含 'T' → datetime(replace(T,' '))（无空格值转成可解析格式）
    - 纯数字（无空格无连字符）→ datetime(x, 'unixepoch')
    已迁移的 "YYYY-MM-DD HH:MM:SS"（带空格、带连字符）两个分支都不命中，幂等。
    """
    for table in ("reddit_posts", "reddit_comments"):
        try:
            cur = conn.execute(
                f"UPDATE {table} SET created_utc = CASE"
                "   WHEN created_utc LIKE '%T%'"
                "       THEN datetime(replace(created_utc, 'T', ' '))"
                "   ELSE datetime(created_utc, 'unixepoch') END"
                " WHERE created_utc IS NOT NULL"
                "   AND (created_utc LIKE '%T%'"
                "        OR (created_utc GLOB '[0-9]*'"
                "            AND created_utc NOT LIKE '% %'"
                "            AND created_utc NOT LIKE '%-%'))"
            )
        except sqlite3.OperationalError as exc:
            logger.warning("Skip migrate on %s: %s", table, exc)
            continue
        if cur.rowcount:
            logger.info("Migrated %d created_utc rows in %s", cur.rowcount, table)
    conn.commit()


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
