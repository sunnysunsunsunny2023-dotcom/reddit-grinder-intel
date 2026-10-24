"""评论增量 watcher 测试：候选帖选择 / Comment ID 去重（mock fetcher，不真拉 Reddit）。"""
from __future__ import annotations

import datetime as dt

import pytest

from collector import storage
from collector.comment_watcher import (
    collect_candidate_posts,
    collect_existing_comment_ids,
    watch_once,
)


def _seed_post(conn, post_id, subreddit="pourover", title="Geimori GU63 issue",
               num_comments=3, created_days_ago=1):
    conn.execute(
        """
        INSERT INTO reddit_posts
            (post_id, subreddit, title, selftext, score, upvote_ratio,
             num_comments, permalink, flair, raw_json, created_utc)
        VALUES (?, ?, ?, 'body', 10, 0.9, ?, '/r/pourover/comments/x',
                NULL, '{}', ?)
        ON CONFLICT (post_id) DO NOTHING
        """,
        (
            post_id, subreddit, title, num_comments,
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=created_days_ago),
        ),
    )
    conn.commit()


def test_collect_candidate_posts_prefers_brand(tmp_path):
    db = tmp_path / "t.db"
    conn = storage.connect(str(db))
    storage.init_schema(conn, "sql/schema.sql")
    _seed_post(conn, "p1", title="Geimori GU63 issue", num_comments=0)
    _seed_post(conn, "p2", title="DF64 vs Niche", num_comments=10)
    posts = collect_candidate_posts(conn, "pourover", watch_days=7, limit=50)
    ids = [p["post_id"] for p in posts]
    # 品牌帖优先（p1），随后是活跃帖（p2）
    assert ids == ["p1", "p2"]
    conn.close()


def test_collect_candidate_posts_ignores_old(tmp_path):
    db = tmp_path / "t.db"
    conn = storage.connect(str(db))
    storage.init_schema(conn, "sql/schema.sql")
    _seed_post(conn, "old1", title="Geimori old", num_comments=0, created_days_ago=30)
    posts = collect_candidate_posts(conn, "pourover", watch_days=7, limit=50)
    assert posts == []
    conn.close()


def test_collect_existing_comment_ids(tmp_path):
    db = tmp_path / "t.db"
    conn = storage.connect(str(db))
    storage.init_schema(conn, "sql/schema.sql")
    _seed_post(conn, "p1")
    conn.execute(
        "INSERT INTO reddit_comments (comment_id, post_id, body, score, created_utc, depth, raw_json)"
        " VALUES ('c1', 'p1', 'hi', 1, ?, 0, '{}')",
        (dt.datetime.now(dt.timezone.utc),),
    )
    conn.commit()
    known = collect_existing_comment_ids(conn, ["p1", "p9"])
    assert "c1" in known
    conn.close()


class _FakeFetcher:
    """模拟 RedditFetcher：仅用于 CommentsFetcher 构造（测试不打网络）。"""

    def __init__(self, comments_by_post):
        self._comments = comments_by_post
        self.timeout = 5

    def _throttle(self):
        pass

    def _curl_get_text(self, url):
        raise AssertionError("should not fallback in fake")


class _FakeComments:
    """模拟 CommentsFetcher.fetch_comments。"""

    def __init__(self, fetcher):
        self._fetcher = fetcher

    def fetch_comments(self, post_id, limit_threads=200):
        return self._fetcher._comments.get(post_id, [])


def test_watch_once_incremental_dedupe(tmp_path, monkeypatch):
    """第二轮 watcher 只插入新增评论（Comment ID 去重）。"""
    db = tmp_path / "t.db"
    conn = storage.connect(str(db))
    storage.init_schema(conn, "sql/schema.sql")
    _seed_post(conn, "p1", num_comments=2)

    comments1 = [
        {
            "comment_id": "c1", "post_id": "p1", "body": "broken grinder",
            "score": 5, "created_utc": 1790755300, "depth": 0, "raw_json": {},
        },
        {
            "comment_id": "c2", "post_id": "p1", "body": "love it",
            "score": 3, "created_utc": 1790755400, "depth": 1, "raw_json": {},
        },
    ]
    fake = _FakeFetcher({"p1": comments1})
    monkeypatch.setattr("collector.comment_watcher.build_fetcher_from_env", lambda: fake)
    monkeypatch.setattr(
        "collector.comment_watcher.CommentsFetcher", _FakeComments
    )

    s1 = watch_once(conn, subreddits=["pourover"], watch_days=7, limit=50,
                    schema_sql_path="sql/schema.sql")
    assert s1["total_new_comments"] == 2

    comments2 = comments1 + [
        {
            "comment_id": "c3", "post_id": "p1", "body": "new comment",
            "score": 1, "created_utc": 1790755500, "depth": 0, "raw_json": {},
        },
    ]
    fake._comments["p1"] = comments2
    s2 = watch_once(conn, subreddits=["pourover"], watch_days=7, limit=50,
                    schema_sql_path="sql/schema.sql")
    assert s2["total_new_comments"] == 1

    total = conn.execute("SELECT COUNT(*) FROM reddit_comments").fetchone()[0]
    assert total == 3
    conn.close()


def test_watch_once_reply_tables_exist(tmp_path, monkeypatch):
    """watch_once 会幂等创建 reply 四表。"""
    db = tmp_path / "t.db"
    conn = storage.connect(str(db))
    storage.init_schema(conn, "sql/schema.sql")
    fake = _FakeFetcher({})
    monkeypatch.setattr("collector.comment_watcher.build_fetcher_from_env", lambda: fake)
    monkeypatch.setattr(
        "collector.comment_watcher.CommentsFetcher", _FakeComments
    )
    watch_once(conn, subreddits=["pourover"], watch_days=7, limit=50,
               schema_sql_path="sql/schema.sql")
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"reply_tasks", "reply_drafts", "reply_approvals",
            "reply_publications"} <= tables
    conn.close()
