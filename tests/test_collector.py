"""collector 模块测试：parser / dedupe / storage（mock，不真拉 Reddit）。"""
from __future__ import annotations

from collector.dedupe import DedupeFilter
from collector.parser import parse_comment, parse_post


def test_parse_post_fields():
    raw = {
        "id": "abc123",
        "title": "Hello grinder",
        "selftext": "body text",
        "created_utc": 1700000000,
        "score": 42,
        "upvote_ratio": 0.9,
        "num_comments": 7,
        "permalink": "/r/pourover/comments/abc123/hello/",
        "link_flair_text": "Review",
        "extra": "ignored",
    }
    row = parse_post(raw, "pourover")
    assert row["post_id"] == "abc123"
    assert row["subreddit"] == "pourover"
    assert row["score"] == 42
    assert row["flair"] == "Review"
    assert row["raw_json"] == raw
    assert row["created_utc"] is not None


def test_parse_post_empty_id():
    row = parse_post({}, "pourover")
    assert row["post_id"] == ""
    assert row["score"] == 0


def test_parse_comment_fields():
    raw = {"id": "c1", "body": "nice", "score": 3, "created_utc": 1700000001, "depth": 1}
    row = parse_comment(raw, "abc123")
    assert row["comment_id"] == "c1"
    assert row["post_id"] == "abc123"
    assert row["depth"] == 1


def test_dedupe_split():
    posts = [
        {"post_id": "a"},
        {"post_id": "b"},
        {"post_id": "a"},
    ]
    df = DedupeFilter(known_ids=set())
    new, existing = df.split(posts)
    assert len(new) == 2  # a, b（同批 a 去重）
    assert len(existing) == 1


def test_dedupe_from_known():
    df = DedupeFilter(known_ids={"x"})
    new, existing = df.split([{"post_id": "x"}, {"post_id": "y"}])
    assert new == [{"post_id": "y"}]
    assert existing == [{"post_id": "x"}]

# ---------------- RSS fallback（数据中心 IP 对 JSON 403，ADR-002 部署实测） ----------------
from collector.reddit_fetcher import RedditFetcher, parse_rss_feed

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">
  <title>r/pourover - new</title>
  <entry>
    <author><name>u/geimori_fan</name></author>
    <category term="Review"/>
    <content type="html">&lt;!-- SC_OFF --&gt;&lt;div class="md"&gt;&lt;p&gt;Geimori GU63 is amazing for pour over.&lt;/p&gt;&lt;/div&gt;&lt;!-- SC_ON --&gt;</content>
    <id>t3_rssabc1</id>
    <link href="https://www.reddit.com/r/pourover/comments/rssabc1/geimori_gu63_review/"/>
    <published>2026-09-30T08:00:00+00:00</published>
    <title>Geimori GU63 review</title>
  </entry>
  <entry>
    <content type="html">&lt;!-- SC_OFF --&gt;&lt;p&gt;DF64 vs Niche help&lt;/p&gt;&lt;!-- SC_ON --&gt;</content>
    <id>t3_rssabc2</id>
    <link href="https://www.reddit.com/r/espresso/comments/rssabc2/df64_vs_niche/"/>
    <published>2026-09-30T06:30:00+00:00</published>
    <title>DF64 vs Niche</title>
  </entry>
</feed>
"""


def test_parse_rss_feed():
    posts = parse_rss_feed(SAMPLE_RSS)
    assert len(posts) == 2
    p = posts[0]
    assert p["id"] == "rssabc1"
    assert p["title"] == "Geimori GU63 review"
    assert "Geimori GU63 is amazing" in p["selftext"]
    assert p["permalink"] == "/r/pourover/comments/rssabc1/geimori_gu63_review"
    assert p["link_flair_text"] == "Review"
    assert p["created_utc"] == 1790755200  # 2026-09-30T08:00:00+00:00 epoch
    assert p["score"] == 0


def test_rss_fallback_when_json_403():
    """JSON 403 时自动 fallback 到 RSS；RSS 也失败时保留原始 403 错误。"""

    class FakeResp:
        def __init__(self, status, text=""):
            self.status_code = status
            self.text = text

    class FakeSession:
        def __init__(self):
            self.calls = []
            self.headers = {}

        def get(self, url, params=None, timeout=None):
            self.calls.append(url)
            if url.endswith(".json"):
                return FakeResp(403)
            return FakeResp(200, SAMPLE_RSS)

    f = RedditFetcher(session=FakeSession())
    posts = f.fetch_new_posts("pourover", limit=5)
    assert len(posts) == 2
    assert f._session.calls[0].endswith(".json")
    assert f._session.calls[1].endswith(".rss")


def test_prefer_rss_skips_json():
    """prefer_rss=True 时直接走 RSS，不先打 JSON（避免 403 试探污染 IP 限速窗口）。"""

    class FakeResp:
        def __init__(self, status, text=""):
            self.status_code = status
            self.text = text

    class FakeSession:
        def __init__(self):
            self.calls = []
            self.headers = {}

        def get(self, url, params=None, timeout=None):
            self.calls.append(url)
            if url.endswith(".json"):
                return FakeResp(403)
            return FakeResp(200, SAMPLE_RSS)

    f = RedditFetcher(session=FakeSession(), prefer_rss=True)
    posts = f.fetch_new_posts("pourover", limit=5)
    assert len(posts) == 2
    assert len(f._session.calls) == 1
    assert f._session.calls[0].endswith(".rss")
