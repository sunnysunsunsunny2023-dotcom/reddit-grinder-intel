"""数据库连接池（psycopg2 SimpleConnectionPool）。

DATABASE_URL 从环境变量读取（部署时写入 .env，走 GitHub Secrets）。
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import psycopg2
import psycopg2.pool

logger = logging.getLogger(__name__)

_pool: Optional[psycopg2.pool.SimpleConnectionPool] = None


def get_database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


def get_pool(minconn: int = 1, maxconn: int = 10) -> psycopg2.pool.SimpleConnectionPool:
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.SimpleConnectionPool(
            minconn, maxconn, dsn=get_database_url()
        )
        logger.info("Database pool created (min=%d max=%d)", minconn, maxconn)
    return _pool


def get_conn():
    """获取一个连接（调用方负责 close/归还）。"""
    return get_pool().getconn()


def put_conn(conn) -> None:
    if _pool is not None:
        _pool.putconn(conn)
    else:
        conn.close()
