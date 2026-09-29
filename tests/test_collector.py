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
