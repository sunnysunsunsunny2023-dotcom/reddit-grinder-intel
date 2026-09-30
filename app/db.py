"""数据库访问封装（SQLite，ADR-002）。

DATABASE_PATH 从环境变量读取（部署时写入 .env，走 GitHub Secrets）。
SQLite 单机低频场景不需要连接池：get_conn 每次新建连接，put_conn 关闭。
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from typing import Optional

logger = logging.getLogger(__name__)

# 统一 datetime 序列化：tz-aware 转 naive UTC 存储，避免
# PARSE_DECLTYPES convert_timestamp 遇到时区字符串报错（同 collector/storage.py）。


def _adapt_datetime_utc(value: dt.datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value.isoformat(sep=" ", timespec="microseconds")


sqlite3.register_adapter(dt.datetime, _adapt_datetime_utc)


def get_database_path() -> str:
    path = os.environ.get("DATABASE_PATH")
    if not path:
        raise RuntimeError("DATABASE_PATH is not set")
    return path


def get_conn() -> sqlite3.Connection:
    """获取一个新连接（调用方负责 close/归还）。"""
    path = get_database_path()
    conn = sqlite3.connect(
        path,
        detect_types=sqlite3.PARSE_DECLTYPES,
        check_same_thread=False,
        timeout=30,
    )
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def put_conn(conn) -> None:
    conn.close()
