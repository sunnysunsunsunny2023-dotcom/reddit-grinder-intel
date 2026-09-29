"""评论拉取模块（可选增强）。

当前最小可行版本：collector 默认只拉帖子列表，不逐个拉评论树
（避免对 Reddit 过多请求）。本模块保留接口，未来需要评论级
evidence 时启用。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .reddit_fetcher import RedditFetcher, RedditFetchError
from .parser import parse_comment

logger = logging.getLogger(__name__)

COMMENTS_BASE = "https://www.reddit.com/comments/{post_id}.json"


class CommentsFetcher:
    """按帖子拉取评论（默认关闭，按需启用）。"""

    def __init__(self, fetcher: RedditFetcher) -> None:
        self._fetcher = fetcher

    def fetch_comments(self, post_id: str, limit_threads: int = 200) -> List[Dict[str, Any]]:
        """拉取一个帖子的评论并标准化。

        Args:
            post_id: Reddit 帖子 base36 id。
            limit_threads: 限制顶层评论数（默认 200）。

        Returns:
            可入库的 reddit_comments 行列表（可能为空）。
        """
        url = COMMENTS_BASE.format(post_id=post_id)
        try:
            resp = self._fetcher._session.get(
                url, params={"limit": limit_threads}, timeout=self._fetcher.timeout
            )
        except Exception as exc:  # noqa: BLE001
            raise RedditFetchError(f"GET {url} failed: {exc}") from exc

        if resp.status_code != 200:
            logger.warning("Comments fetch %s -> %s", url, resp.status_code)
            return []

        try:
            payload = resp.json()
        except ValueError:
            return []

        rows: List[Dict[str, Any]] = []
        # payload = [post_listing, comments_listing]
        if not isinstance(payload, list) or len(payload) < 2:
            return rows

        def walk(node: Dict[str, Any], depth: int) -> None:
            kind = node.get("kind")
            data = node.get("data") or {}
            if kind == "t1":
                row = parse_comment(data, post_id)
                if row["comment_id"]:
                    rows.append(row)
            children = data.get("children") or []
            for child in children:
                walk(child, depth + 1)
            replies = data.get("replies")
            if replies and isinstance(replies, dict):
                walk(replies, depth + 1)

        for listing in payload[1:]:
            for child in (listing.get("data") or {}).get("children") or []:
                walk(child, 0)

        logger.info("Fetched %d comments for post %s", len(rows), post_id)
        self._fetcher._throttle()
        return rows
