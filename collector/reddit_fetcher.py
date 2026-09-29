"""Reddit 公开 JSON API 拉取模块。

当前 Reddit Data API 应用未获批，因此 fetcher 保持模块化可替换：
- 现在：公开 JSON endpoint（https://www.reddit.com/r/{subreddit}/new.json）
- 未来：官方 Data API / OAuth 获批后，只替换本模块，不影响
  数据库、统计层、LLM 分析或 Coze 报告。

铁律：
- 必须带自定义 User-Agent（Reddit 会 429 匿名默认 UA）
- 必须限速（默认 sleep 2s/请求），并发拉多个 subreddit 时串行
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

REDDIT_JSON_BASE = "https://www.reddit.com/r/{subreddit}/new.json"
DEFAULT_USER_AGENT = (
    "linux:reddit-grinder-intel:v0.1.0 (by /u/reddit_grinder_intel; "
    "internal monitoring script)"
)


class RedditFetchError(RuntimeError):
    """Reddit 拉取失败。"""


class RedditFetcher:
    """从 Reddit 公开 JSON API 拉取最新帖子（可替换实现）。"""

    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: int = 30,
        sleep_seconds: float = 2.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.timeout = timeout
        self.sleep_seconds = sleep_seconds
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": user_agent})

    def fetch_new_posts(
        self, subreddit: str, limit: int = 100, after: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """拉取某个 subreddit 的最新帖子原始 JSON。

        Args:
            subreddit: subreddit 名（不含 r/ 前缀）。
            limit: 最多拉多少条（Reddit 上限 100）。
            after: 分页游标（base36 post id）。

        Returns:
            帖子原始 JSON 列表（listing children 的 data 字段）。

        Raises:
            RedditFetchError: 网络或 HTTP 错误。
        """
        url = REDDIT_JSON_BASE.format(subreddit=subreddit)
        params: Dict[str, Any] = {"limit": min(limit, 100)}
        if after:
            params["after"] = after

        try:
            resp = self._session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise RedditFetchError(f"GET {url} failed: {exc}") from exc

        if resp.status_code == 429:
            raise RedditFetchError(
                f"Reddit rate-limited (429) for r/{subreddit}; "
                "respect User-Agent and backoff."
            )
        if resp.status_code != 200:
            raise RedditFetchError(
                f"Reddit returned {resp.status_code} for r/{subreddit}"
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            raise RedditFetchError(f"Invalid JSON from Reddit for r/{subreddit}") from exc

        children = payload.get("data", {}).get("children", [])
        posts = [child.get("data", {}) for child in children if child.get("kind") == "t3"]
        logger.info("Fetched %d posts from r/%s", len(posts), subreddit)
        self._throttle()
        return posts

    def _throttle(self) -> None:
        if self.sleep_seconds > 0:
            time.sleep(self.sleep_seconds)


def build_fetcher_from_env() -> RedditFetcher:
    """从环境变量构建 fetcher（供 scheduler 使用）。"""
    import os

    ua = os.environ.get(
        "REDDIT_USER_AGENT",
        DEFAULT_USER_AGENT,
    )
    sleep_s = float(os.environ.get("REDDIT_SLEEP_SECONDS", "2.0"))
    return RedditFetcher(user_agent=ua, sleep_seconds=sleep_s)
