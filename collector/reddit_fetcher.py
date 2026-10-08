"""Reddit 公开 API 拉取模块。

当前 Reddit Data API 应用未获批，因此 fetcher 保持模块化可替换：
- 现在：先试公开 JSON endpoint（https://www.reddit.com/r/{subreddit}/new.json），
  服务器数据中心 IP 常被 Reddit 对 JSON 端点 403（实测所有 UA 均 403），
  自动 fallback 到 RSS 端点（https://www.reddit.com/r/{subreddit}/new/.rss，实测 200）。
- 未来：官方 Data API / OAuth 获批后，只替换本模块，不影响
  数据库、统计层、LLM 分析或 Coze 报告。

铁律：
- 必须带自定义 User-Agent（Reddit 会 429 匿名默认 UA）
- 必须限速（默认 sleep 2s/请求），并发拉多个 subreddit 时串行
"""
from __future__ import annotations

import logging
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

REDDIT_JSON_BASE = "https://www.reddit.com/r/{subreddit}/new.json"
REDDIT_RSS_BASE = "https://www.reddit.com/r/{subreddit}/new/.rss"
REDDIT_SEARCH_JSON_BASE = "https://www.reddit.com/r/{subreddit}/search.json"
REDDIT_SEARCH_RSS_BASE = "https://www.reddit.com/r/{subreddit}/search/.rss"
DEFAULT_USER_AGENT = (
    "linux:reddit-grinder-intel:v0.1.0 (by /u/reddit_grinder_intel; "
    "internal monitoring script)"
)
_NS = {"atom": "http://www.w3.org/2005/Atom"}
_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_TAG_CLEAN_RE = re.compile(r"<!--.*?-->")


def _rss_text_to_plain(html_text: Optional[str]) -> Optional[str]:
    """把 RSS content 的 HTML 剥成纯文本（去掉 SC_OFF 注释与标签）。"""
    if not html_text:
        return None
    import html as _html

    txt = _TAG_CLEAN_RE.sub("", html_text)
    txt = _TAG_STRIP_RE.sub("", txt)
    return _html.unescape(txt).strip() or None


def _rss_published_to_epoch(published: Optional[str]) -> Optional[int]:
    """RSS published ISO 时间 → epoch 秒（parse_post 需要 epoch）。"""
    if not published:
        return None
    try:
        import datetime as _dt

        return int(_dt.datetime.fromisoformat(published.replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError, OSError):
        return None


def parse_rss_feed(xml_text: str) -> List[Dict[str, Any]]:
    """解析 RSS feed 为与 JSON listing data 兼容的 dict 列表。

    RSS 没有 score / upvote_ratio / num_comments，置 0 占位；
    后续如需要热门度排序，可再为高价值帖单独补充拉取。
    """
    root = ET.fromstring(xml_text)
    posts: List[Dict[str, Any]] = []
    for entry in root.findall("atom:entry", _NS):
        post_id = (entry.findtext("atom:id", default="", namespaces=_NS) or "").strip()
        if post_id.startswith("t3_"):
            post_id = post_id[len("t3_"):]
        link_el = entry.find("atom:link", _NS)
        href = (link_el.get("href") if link_el is not None else "") or ""
        permalink = ""
        if href:
            m = re.search(r"^https?://(?:www\.)?reddit\.com(/r/[^/]+/comments/[^/]+/[^/]+)", href)
            permalink = m.group(1) if m else href
        if not post_id and permalink:
            m = re.search(r"/comments/([A-Za-z0-9]+)", permalink)
            if m:
                post_id = m.group(1)
        title = (entry.findtext("atom:title", default="", namespaces=_NS) or "").strip()
        content_el = entry.find("atom:content", _NS)
        content = content_el.text if content_el is not None else None
        published = entry.findtext("atom:published", default="", namespaces=_NS) or ""
        author = ""
        author_el = entry.find("atom:author/atom:name", _NS)
        if author_el is not None:
            author = (author_el.text or "").strip()
        category = ""
        cat_el = entry.find("atom:category", _NS)
        if cat_el is not None:
            category = (cat_el.get("term") or "").strip()
        posts.append(
            {
                "id": post_id,
                "title": title,
                "selftext": _rss_text_to_plain(content),
                "created_utc": _rss_published_to_epoch(published),
                "score": 0,
                "upvote_ratio": None,
                "num_comments": 0,
                "permalink": permalink,
                "link_flair_text": category or None,
                "raw_json": {
                    "source": "rss",
                    "title": title,
                    "author": author,
                    "published": published,
                    "href": href,
                },
            }
        )
    return posts


class RedditFetchError(RuntimeError):
    """Reddit 拉取失败。"""


class RedditFetcher:
    """从 Reddit 公开 JSON API 拉取最新帖子（可替换实现）。"""

    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: int = 30,
        sleep_seconds: float = 2.0,
        prefer_rss: bool = False,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.timeout = timeout
        self.sleep_seconds = sleep_seconds
        self.prefer_rss = prefer_rss
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": user_agent})

    def search_posts(
        self,
        query: str,
        subreddit: str,
        limit: int = 100,
        sort: str = "new",
    ) -> List[Dict[str, Any]]:
        """按关键词搜索某个 subreddit 的帖子（用于补抓 new 列表外的历史帖）。

        与 fetch_new_posts 同构：优先 RSS（服务器 JSON 端点常 403），
        RSS 失败时 fallback JSON；两者均失败抛 RedditFetchError。
        搜索结果格式与 new listing 兼容（RSS 走 parse_rss_feed）。

        Args:
            query: 搜索关键词（如 "geimori" / "gu64"），可含 Reddit 搜索语法。
            subreddit: subreddit 名（不含 r/ 前缀）。
            limit: 最多拉多少条（Reddit 上限 100）。
            sort: 排序，默认 new（搜最新）；可选 relevance/top/comments。

        Returns:
            帖子原始 dict 列表（与 JSON listing data 兼容）。

        Raises:
            RedditFetchError: 网络或 HTTP 错误（RSS 与 JSON 均失败）。
        """
        if self.prefer_rss:
            # 数据中心 IP 上 search JSON 端点已知 403（与 new.json 同），
            # RSS 失败后再 fallback JSON 只会多打一个 403 请求污染限速窗口，
            # 导致后续 RSS 也 429。因此 prefer_rss 时 RSS 失败直接抛错。
            return self._fetch_search_rss(query, subreddit, limit=limit, sort=sort)
        try:
            return self._fetch_search_json(query, subreddit, limit=limit, sort=sort)
        except RedditFetchError as json_exc:
            if "403" not in str(json_exc):
                raise
            logger.warning(
                "Search JSON 403 for r/%s q=%r (数据中心 IP 常被 JSON 端点拒绝)，fallback RSS: %s",
                subreddit, query, json_exc,
            )
            if self.sleep_seconds > 0:
                time.sleep(max(self.sleep_seconds, 3))
            try:
                return self._fetch_search_rss(query, subreddit, limit=limit, sort=sort)
            except RedditFetchError as rss_exc:
                raise RedditFetchError(
                    f"Search JSON 403 and RSS fallback failed for r/{subreddit} "
                    f"q={query!r}: json={json_exc}; rss={rss_exc}"
                ) from None

    def _fetch_search_json(
        self, query: str, subreddit: str, limit: int = 100, sort: str = "new"
    ) -> List[Dict[str, Any]]:
        """从公开 search.json 端点拉取搜索结果（原逻辑同 new.json）。"""
        url = REDDIT_SEARCH_JSON_BASE.format(subreddit=subreddit)
        params: Dict[str, Any] = {
            "q": query,
            "limit": min(limit, 100),
            "sort": sort,
        }
        try:
            resp = self._session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise RedditFetchError(f"GET {url} q={query!r} failed: {exc}") from exc

        if resp.status_code == 429:
            raise RedditFetchError(
                f"Reddit rate-limited (429) for search r/{subreddit} q={query!r}; "
                "respect User-Agent and backoff."
            )
        if resp.status_code != 200:
            raise RedditFetchError(
                f"Reddit search returned {resp.status_code} for r/{subreddit} q={query!r}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise RedditFetchError(
                f"Invalid search JSON from Reddit for r/{subreddit} q={query!r}"
            ) from exc

        children = payload.get("data", {}).get("children", [])
        posts = [child.get("data", {}) for child in children if child.get("kind") == "t3"]
        logger.info(
            "Search fetched %d posts from r/%s q=%r (json)",
            len(posts), subreddit, query,
        )
        self._throttle()
        return posts

    def _fetch_search_rss(
        self, query: str, subreddit: str, limit: int = 100, sort: str = "new"
    ) -> List[Dict[str, Any]]:
        """从 search RSS 端点拉取并解析（curl 拉取，与 _fetch_rss 同策略）。"""
        import urllib.parse as _up

        encoded = _up.quote(query)
        url = (
            f"{REDDIT_SEARCH_RSS_BASE.format(subreddit=subreddit)}"
            f"?q={encoded}&sort={sort}&limit={min(limit, 100)}"
        )
        try:
            status, xml_text = self._curl_get_text(url)
        except RedditFetchError as exc:
            raise RedditFetchError(f"GET {url} failed: {exc}") from exc
        if status == "429":
            # search 端点限速严格（实测比 new 严）；429 后退避重试一次
            logger.warning(
                "Search RSS 429 for r/%s q=%r, backoff retry", subreddit, query
            )
            time.sleep(20)
            try:
                status, xml_text = self._curl_get_text(url)
            except RedditFetchError as exc:
                raise RedditFetchError(f"GET {url} retry failed: {exc}") from exc
        if status != "200":
            raise RedditFetchError(
                f"Reddit search RSS returned {status} for r/{subreddit} q={query!r}"
            )
        try:
            posts = parse_rss_feed(xml_text)
        except ET.ParseError as exc:
            raise RedditFetchError(
                f"Invalid search RSS XML from Reddit for r/{subreddit} q={query!r}: {exc}"
            ) from exc
        logger.info(
            "Search fetched %d posts from r/%s q=%r (rss)",
            len(posts), subreddit, query,
        )
        self._throttle()
        return posts

    def fetch_new_posts(
        self, subreddit: str, limit: int = 100, after: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """拉取某个 subreddit 的最新帖子。

        优先 JSON 公开端点；服务器 IP 被 Reddit 对 JSON 403 时
        自动 fallback 到 RSS 端点（实测 200）。

        Args:
            subreddit: subreddit 名（不含 r/ 前缀）。
            limit: 最多拉多少条（Reddit 上限 100）。
            after: 分页游标（仅 JSON 端点支持；RSS 不支持分页）。

        Returns:
            帖子原始 dict 列表（与 JSON listing data 兼容）。

        Raises:
            RedditFetchError: 网络或 HTTP 错误（JSON 与 RSS 均失败）。
        """
        # 数据中心 IP 上 Reddit JSON 端点长期 403（实测所有 UA），
        # 且先打 JSON 的 403 请求会污染 IP 限速窗口，导致紧接着的 RSS 也 429。
        # prefer_rss=True（服务器部署默认）时直接走 RSS，绕开 JSON 试探。
        if self.prefer_rss:
            try:
                return self._fetch_rss(subreddit, limit=limit)
            except RedditFetchError as rss_exc:
                logger.warning("RSS failed for r/%s, fallback JSON: %s", subreddit, rss_exc)
                try:
                    return self._fetch_json(subreddit, limit=limit, after=after)
                except RedditFetchError as json_exc:
                    raise RedditFetchError(
                        f"RSS and JSON fallback failed for r/{subreddit}: "
                        f"rss={rss_exc}; json={json_exc}"
                    ) from None
        try:
            return self._fetch_json(subreddit, limit=limit, after=after)
        except RedditFetchError as json_exc:
            if "403" not in str(json_exc):
                raise
            logger.warning(
                "Reddit JSON 403 for r/%s (数据中心 IP 常被 JSON 端点拒绝)，fallback RSS: %s",
                subreddit,
                json_exc,
            )
            # 403 后稍作停顿再打 RSS：同一秒连续请求易被 Reddit 限速(429)
            if self.sleep_seconds > 0:
                time.sleep(max(self.sleep_seconds, 3))
            try:
                return self._fetch_rss(subreddit, limit=limit)
            except RedditFetchError as rss_exc:
                raise RedditFetchError(
                    f"JSON 403 and RSS fallback failed for r/{subreddit}: "
                    f"json={json_exc}; rss={rss_exc}"
                ) from None

    def _fetch_json(
        self, subreddit: str, limit: int = 100, after: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """从公开 JSON 端点拉取（原逻辑）。"""
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
        logger.info("Fetched %d posts from r/%s (json)", len(posts), subreddit)
        self._throttle()
        return posts

    def _curl_get_text(self, url: str) -> tuple[str, str]:
        """用 curl 拉取文本。

        Reddit 对 python-requests 的 TLS/header 指纹在 RSS 端点返回 429，
        而同 IP 下 curl 稳定 200（实测多轮）。RSS 是纯文本获取，curl 足够。
        返回 (http_status, body)。
        """
        ua = self._session.headers.get("User-Agent") or DEFAULT_USER_AGENT
        marker = "__REDDIT_HTTP_%{http_code}__"
        try:
            proc = subprocess.run(
                [
                    "curl", "-sS", "--max-time", str(int(self.timeout)),
                    "-A", ua, "-w", "\n" + marker, url,
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout + 5,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise RedditFetchError(f"curl GET {url} failed: {exc}") from exc
        if proc.returncode != 0:
            raise RedditFetchError(
                f"curl GET {url} failed: {proc.stderr.strip() or proc.returncode}"
            )
        body = proc.stdout
        m = re.search(r"__REDDIT_HTTP_(\d{3})__\s*$", body)
        if not m:
            raise RedditFetchError(f"curl GET {url}: cannot parse http status")
        return m.group(1), body[: m.start()].rstrip("\n")

    def _fetch_rss(self, subreddit: str, limit: int = 100) -> List[Dict[str, Any]]:
        """从 RSS 端点拉取并解析为与 JSON 兼容的结构（curl 拉取，规避 requests 指纹限速）。"""
        url = f"{REDDIT_RSS_BASE.format(subreddit=subreddit)}?limit={min(limit, 100)}"
        try:
            status, xml_text = self._curl_get_text(url)
        except RedditFetchError as exc:
            raise RedditFetchError(f"GET {url} failed: {exc}") from exc
        if status != "200":
            raise RedditFetchError(
                f"Reddit RSS returned {status} for r/{subreddit}"
            )
        try:
            posts = parse_rss_feed(xml_text)
        except ET.ParseError as exc:
            raise RedditFetchError(
                f"Invalid RSS XML from Reddit for r/{subreddit}: {exc}"
            ) from exc
        logger.info("Fetched %d posts from r/%s (rss)", len(posts), subreddit)
        self._throttle()
        return posts

    def _throttle(self) -> None:
        if self.sleep_seconds > 0:
            time.sleep(self.sleep_seconds)


def build_fetcher_from_env() -> RedditFetcher:
    """从环境变量构建 fetcher（供 scheduler 使用）。"""
    import os

    ua = os.environ.get("REDDIT_USER_AGENT", "").strip() or DEFAULT_USER_AGENT
    sleep_s = float(os.environ.get("REDDIT_SLEEP_SECONDS", "2.0"))
    prefer_rss = os.environ.get("REDDIT_PREFER_RSS", "").strip().lower() in ("1", "true", "yes")
    return RedditFetcher(user_agent=ua, sleep_seconds=sleep_s, prefer_rss=prefer_rss)
