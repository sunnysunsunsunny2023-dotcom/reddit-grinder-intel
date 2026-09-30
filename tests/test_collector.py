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


def test_rss_fallback_when_json_403(monkeypatch):
    """JSON 403 时自动 fallback 到 RSS（curl 拉取）；RSS 也失败时保留原始 403 错误。"""

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

    curl_calls = []

    def fake_run(cmd, capture_output=None, text=None, timeout=None):
        curl_calls.append(cmd)
        proc = type("Proc", (), {"returncode": 0, "stdout": SAMPLE_RSS + "\n__REDDIT_HTTP_200__", "stderr": ""})
        return proc

    monkeypatch.setattr("collector.reddit_fetcher.subprocess.run", fake_run)
    f = RedditFetcher(session=FakeSession())
    posts = f.fetch_new_posts("pourover", limit=5)
    assert len(posts) == 2
    assert f._session.calls[0].endswith(".json")
    assert len(curl_calls) == 1
    assert ".rss" in curl_calls[0][-1]


def test_prefer_rss_skips_json(monkeypatch):
    """prefer_rss=True 时直接走 RSS（curl），不先打 JSON（避免 403 试探污染 IP 限速窗口）。"""

    class FakeSession:
        def __init__(self):
            self.calls = []
            self.headers = {}

        def get(self, url, params=None, timeout=None):
            self.calls.append(url)
            raise AssertionError("JSON should not be called in prefer_rss mode")

    curl_calls = []

    def fake_run(cmd, capture_output=None, text=None, timeout=None):
        curl_calls.append(cmd)
        proc = type("Proc", (), {"returncode": 0, "stdout": SAMPLE_RSS + "\n__REDDIT_HTTP_200__", "stderr": ""})
        return proc

    monkeypatch.setattr("collector.reddit_fetcher.subprocess.run", fake_run)
    f = RedditFetcher(session=FakeSession(), prefer_rss=True)
    posts = f.fetch_new_posts("pourover", limit=5)
    assert len(posts) == 2
    assert len(curl_calls) == 1
    assert ".rss" in curl_calls[0][-1]


def test_load_env_file(tmp_path, monkeypatch):
    """load_env_file 读取 .env 注入环境变量（不覆盖已有值）。"""
    from collector.scheduler import load_env_file

    env_file = tmp_path / "test.env"
    env_file.write_text(
        'REDDIT_PREFER_RSS=1\nREDDIT_USER_AGENT="custom ua"\nALREADY_SET=old\n'
    )
    monkeypatch.setenv("ALREADY_SET", "keepme")
    monkeypatch.delenv("REDDIT_PREFER_RSS", raising=False)
    load_env_file(str(env_file))
    assert __import__("os").environ["REDDIT_PREFER_RSS"] == "1"
    assert __import__("os").environ["REDDIT_USER_AGENT"] == "custom ua"
    assert __import__("os").environ["ALREADY_SET"] == "keepme"


def test_upsert_posts_serializes_raw_json(tmp_path):
    """upsert_posts 把 raw_json dict 序列化为 JSON 文本入库（RSS fallback 的 raw_json 是 dict）。"""
    from collector import storage

    db = tmp_path / "test.db"
    conn = storage.connect(str(db))
    storage.init_schema(conn, "sql/schema.sql")
    rows = [
        {
            "post_id": "t3_rssx1",
            "subreddit": "pourover",
            "title": "Title",
            "selftext": "Body",
            "created_utc": 1790755200,
            "score": 0,
            "upvote_ratio": None,
            "num_comments": 0,
            "permalink": "/r/pourover/comments/rssx1/title",
            "flair": "Review",
            "raw_json": {"source": "rss", "title": "Title"},
        }
    ]
    inserted = storage.upsert_posts(conn, rows)
    assert inserted == 1
    cur = conn.execute(
        "SELECT raw_json FROM reddit_posts WHERE post_id = 't3_rssx1'"
    )
    raw = cur.fetchone()[0]
    assert '"source": "rss"' in raw
    conn.close()


def test_upsert_posts_created_utc_queryable(tmp_path):
    """upsert_posts 把 epoch created_utc 转 naive UTC datetime 入库，
    查询 TIMESTAMP 列返回 datetime 而不触发 convert_timestamp 报错。"""
    from collector import storage

    db = tmp_path / "test.db"
    conn = storage.connect(str(db))
    storage.init_schema(conn, "sql/schema.sql")
    rows = [
        {
            "post_id": "t3_rssc1",
            "subreddit": "pourover",
            "title": "Title",
            "selftext": "Body",
            "created_utc": 1790755200,
            "score": 0,
            "upvote_ratio": None,
            "num_comments": 0,
            "permalink": "/r/pourover/comments/rssc1/title",
            "flair": None,
            "raw_json": {"source": "rss"},
        }
    ]
    storage.upsert_posts(conn, rows)

    import datetime as dt

    rows_out = conn.execute(
        "SELECT created_utc FROM reddit_posts WHERE post_id = 't3_rssc1'"
    ).fetchall()
    val = rows_out[0][0]
    assert isinstance(val, dt.datetime)
    assert val == dt.datetime(2026, 9, 30, 8, 0)
    conn.close()


def test_migrate_created_utc_old_epoch_rows(tmp_path):
    """旧库中 created_utc 存成 epoch 整数的行被迁移为 ISO 时间后查询正常。"""
    from collector import storage

    db = tmp_path / "test.db"
    conn = storage.connect(str(db))
    storage.init_schema(conn, "sql/schema.sql")
    # 绕过 _bind_row 直接插入旧格式（epoch int）
    conn.execute(
        "INSERT INTO reddit_posts"
        " (post_id, subreddit, title, selftext, created_utc, score,"
        "  upvote_ratio, num_comments, permalink, flair, raw_json)"
        " VALUES ('t3_old1', 'espresso', 'Old', 'Body', 1790755200, 0,"
        "         NULL, 0, '/r/espresso/comments/old1/x', NULL, '{}')"
    )
    # 同时插入 TEXT 类型的 epoch（部分旧库可能以文本存储）
    conn.execute(
        "INSERT INTO reddit_posts"
        " (post_id, subreddit, title, selftext, created_utc, score,"
        "  upvote_ratio, num_comments, permalink, flair, raw_json)"
        " VALUES ('t3_old2', 'espresso', 'Old2', 'Body', '1790755201', 0,"
        "         NULL, 0, '/r/espresso/comments/old2/x', NULL, '{}')"
    )
    conn.commit()
    # 重新 init（实际部署时旧库会走 init_schema 迁移）
    storage.init_schema(conn, "sql/schema.sql")

    import datetime as dt

    v1 = conn.execute(
        "SELECT created_utc FROM reddit_posts WHERE post_id = 't3_old1'"
    ).fetchone()[0]
    v2 = conn.execute(
        "SELECT created_utc FROM reddit_posts WHERE post_id = 't3_old2'"
    ).fetchone()[0]
    assert isinstance(v1, dt.datetime) and v1 == dt.datetime(2026, 9, 30, 8, 0)
    assert isinstance(v2, dt.datetime) and v2 == dt.datetime(2026, 9, 30, 8, 0, 1)
    conn.close()


def test_migrate_created_utc_iso_t_rows(tmp_path):
    """旧库中 created_utc 存成带 'T' 的 ISO 文本（无空格）时迁移后查询正常。

    服务器真实数据形态是 '2026-09-27T02:42:49'：convert_timestamp 的
    val.split(b' ') 只得到 1 段，同样触发 ValueError。迁移应把 'T' 换成空格。
    """
    import datetime as dt

    from collector import storage

    db = tmp_path / "test.db"
    conn = storage.connect(str(db))
    storage.init_schema(conn, "sql/schema.sql")
    conn.execute(
        "INSERT INTO reddit_posts"
        " (post_id, subreddit, title, selftext, created_utc, score,"
        "  upvote_ratio, num_comments, permalink, flair, raw_json)"
        " VALUES ('t3_iso1', 'pourover', 'Iso', 'Body',"
        "         '2026-09-27T02:42:49', 0, NULL, 0,"
        "         '/r/pourover/comments/iso1/x', NULL, '{}')"
    )
    conn.execute(
        "INSERT INTO reddit_posts"
        " (post_id, subreddit, title, selftext, created_utc, score,"
        "  upvote_ratio, num_comments, permalink, flair, raw_json)"
        " VALUES ('t3_iso2', 'pourover', 'Iso2', 'Body',"
        "         '2026-09-27T02:42:49.123456', 0, NULL, 0,"
        "         '/r/pourover/comments/iso2/x', NULL, '{}')"
    )
    conn.commit()
    storage.init_schema(conn, "sql/schema.sql")

    v1 = conn.execute(
        "SELECT created_utc FROM reddit_posts WHERE post_id = 't3_iso1'"
    ).fetchone()[0]
    v2 = conn.execute(
        "SELECT created_utc FROM reddit_posts WHERE post_id = 't3_iso2'"
    ).fetchone()[0]
    assert isinstance(v1, dt.datetime) and v1 == dt.datetime(2026, 9, 27, 2, 42, 49)
    assert isinstance(v2, dt.datetime) and v2 == dt.datetime(2026, 9, 27, 2, 42, 49)
    conn.close()


def test_upsert_posts_created_utc_iso_t_bind(tmp_path):
    """upsert 传入带 'T' 的 ISO 文本 created_utc 时，入库后查询返回 datetime。"""
    import datetime as dt

    from collector import storage

    db = tmp_path / "test.db"
    conn = storage.connect(str(db))
    storage.init_schema(conn, "sql/schema.sql")
    row = {
        "post_id": "t3_bind1",
        "subreddit": "espresso",
        "title": "Bind",
        "selftext": "Body",
        "created_utc": "2026-09-27T02:42:49",
        "score": 3,
        "upvote_ratio": None,
        "num_comments": 1,
        "permalink": "/r/espresso/comments/bind1/x",
        "flair": None,
        "raw_json": {},
    }
    assert storage.upsert_posts(conn, [row]) == 1
    v = conn.execute(
        "SELECT created_utc FROM reddit_posts WHERE post_id = 't3_bind1'"
    ).fetchone()[0]
    assert isinstance(v, dt.datetime) and v == dt.datetime(2026, 9, 27, 2, 42, 49)
    conn.close()
