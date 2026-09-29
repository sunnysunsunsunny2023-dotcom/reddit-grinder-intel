"""去重模块：同一 post_id 不重复入库（spec Definition of Done #2）。

策略：
- 新帖判断：post_id 不在 reddit_posts 中。
- 已存在帖：默认不更新（保持首次入库的 fetched_at 快照），
  避免每天覆盖 score 造成历史不可追溯；如需刷新评分可显式打开。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Set, Tuple

logger = logging.getLogger(__name__)


class DedupeFilter:
    """基于已有 post_id 集合过滤新帖。"""

    def __init__(self, known_ids: Set[str]) -> None:
        self._known = known_ids

    @classmethod
    def from_rows(cls, rows: List[Dict[str, Any]]) -> "DedupeFilter":
        return cls({str(r["post_id"]) for r in rows})

    def split(
        self, posts: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """返回 (new_posts, existing_posts)。"""
        new: List[Dict[str, Any]] = []
        existing: List[Dict[str, Any]] = []
        for post in posts:
            pid = str(post["post_id"])
            if pid in self._known:
                existing.append(post)
            else:
                new.append(post)
                self._known.add(pid)  # 同批内去重
        if existing:
            logger.info("Dedupe: %d existing posts skipped", len(existing))
        return new, existing


def collect_existing_post_ids(conn, subreddit: str) -> Set[str]:
    """从数据库读取已知 post_id 集合（按 subreddit 限定可减少内存）。

    Args:
        conn: psycopg2 连接。
        subreddit: subreddit 名。

    Returns:
        已知 post_id 集合。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT post_id FROM reddit_posts WHERE subreddit = %s",
            (subreddit,),
        )
        return {str(row[0]) for row in cur.fetchall()}
