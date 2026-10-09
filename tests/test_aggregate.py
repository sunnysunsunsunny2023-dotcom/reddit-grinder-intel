"""aggregate / trends 确定性统计测试（纯函数，不依赖 PG）。"""
from __future__ import annotations

import datetime as dt

from app import aggregate
from app import keywords
from app.trends import (
    classify_signal,
    classify_trend,
    needs_alert,
    WEAK_EVIDENCE,
)
from tests.conftest import make_post


def test_compute_kpi(sample_posts):
    kpi = aggregate.compute_kpi(sample_posts)
    assert kpi["total_new_posts"] == 5
    # p5 无关 → grinder 4
    assert kpi["grinder_related_posts"] == 4
    assert abs(kpi["grinder_ratio"] - 0.8) < 1e-6
    # p2 含 Geimori/GU63
    assert kpi["geimori_mentions"] == 1
    # p1(1zpresso/comandante) p3(df64/niche) p4(timemore)
    assert kpi["competitor_mentions"] == 3


def test_topic_mentions(sample_posts):
    topics = aggregate.topic_mentions(sample_posts)
    assert topics["static"]["mentions"] == 1      # p1
    assert topics["retention"]["mentions"] == 2   # p1, p2
    assert topics["portability"]["mentions"] == 1  # p4
    assert topics["espresso"]["mentions"] == 1     # p3
    assert topics["price_value"]["mentions"] == 0


def test_competitor_mentions(sample_posts):
    comp = aggregate.competitor_mentions(sample_posts)
    assert comp["1zpresso"]["mentions"] == 1
    assert comp["comandante"]["mentions"] == 1
    assert comp["df_grinders"]["mentions"] == 1
    assert comp["niche"]["mentions"] == 1
    assert comp["timemore"]["mentions"] == 1
    assert comp["fellow"]["mentions"] == 0


def test_top_posts_ranking(sample_posts):
    top = aggregate.top_posts(sample_posts, limit=2)
    assert top[0]["post_id"] == "p1"  # 120+45=165 最高
    assert top[1]["post_id"] == "p2"  # 80+20=100
    assert len(top) == 2


def test_signal_alert_spike():
    posts = [aggregate_query_helper("static", n=6) for n in range(6)]
    base7 = {"static": 1.0}
    base30 = {"static": 0.8}
    alerts = aggregate.signal_alerts(posts, base7, base30)
    assert any(a["topic"] == "static" and a["status"] == "spike" for a in alerts)


def aggregate_query_helper(topic: str, n: int):
    """构造命中指定主题的帖子（复用 sample 风格）。"""
    from tests.conftest import make_post

    text = {
        "static": "static and RDT water droplet",
        "retention": "retention and popcorning",
        "espresso": "espresso dial in shots",
        "portability": "portable travel lightweight",
    }[topic]
    return make_post(f"{topic}_{n}", f"{topic} post {n}", text)


# ------------------------------------------------------------
# trends 规则测试
# ------------------------------------------------------------
def test_classify_spike():
    assert classify_trend(9.0, 2.0, 1.0, evidence_count=4) == "spike"


def test_classify_rising():
    assert classify_trend(5.0, 3.0, 2.0, evidence_count=6) == "rising"


def test_classify_declining():
    # 持续下降：current 明显低于上周，且上周不高于 30d baseline
    assert classify_trend(1.0, 4.0, 4.0, evidence_count=2) == "declining"
    # 高位急跌（prev 高于 avg_30d）不算 declining
    assert classify_trend(1.0, 5.0, 4.0, evidence_count=2) == "stable"


def test_classify_stable():
    assert classify_trend(2.0, 2.0, 2.0, evidence_count=3) == "stable"


def test_classify_structural_needs_two_weeks():
    # 仅 1 周高频不标 structural（spec 7：不要因为一天爆帖标 structural）
    assert classify_trend(
        30.0, 2.0, 2.0, evidence_count=20, unique_posts=15, weeks_high=1
    ) != "structural_priority"
    assert classify_trend(
        30.0, 28.0, 10.0, evidence_count=20, unique_posts=15, weeks_high=3
    ) == "structural_priority"


def test_classify_signal_weak_evidence():
    status, confidence = classify_signal(5.0, 1.0, 1.0, evidence_count=2)
    assert status == "spike"
    assert confidence == "low"  # 样本 <= WEAK_EVIDENCE → low


def test_needs_alert():
    assert needs_alert("spike")
    assert needs_alert("emerging")
    assert not needs_alert("stable")


def test_keywords_normalize():
    assert keywords.has_any("I love my GRINDER", keywords.GRINDER_KEYWORDS)
    assert not keywords.has_any("cat kettle", keywords.GRINDER_KEYWORDS)


# ------------------------------------------------------------
# RGI-009 回归：build_daily_context 窗口起点必须与 _resolve_period 一致
# ------------------------------------------------------------
def _make_db(tmp_path, posts: list) -> Any:
    """建内存 SQLite：插入 posts（created_utc 为 naive UTC，与生产一致）。"""
    import sqlite3
    from collector import storage

    db_path = tmp_path / "rgi009.db"
    conn = storage.connect(str(db_path))
    storage.init_schema(conn, "sql/schema.sql")
    cur = conn.cursor()
    for p in posts:
        cur.execute(
            "INSERT INTO reddit_posts"
            " (post_id, subreddit, title, selftext, created_utc, score,"
            "  num_comments, permalink)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                p["post_id"], p["subreddit"], p["title"], p["selftext"],
                p["created_utc"], p["score"], p["num_comments"], p["permalink"],
            ),
        )
    conn.commit()
    return conn


def test_daily_context_window_matches_batch_period(tmp_path):
    """边界帖（created 在 floor(now-24h) 与 now-24h 之间）必须进入窗口。

    根因复现：batch period_start=floor(now-24h)=11:00，而旧 build_daily_context
    用 now-24h=11:11 起查，11:02 的 Geimori GU64 帖被漏 → geimori_mentions=0。
    修复后传 start=floor → 该帖被包含。
    """
    import datetime as dt

    now = dt.datetime.now(dt.timezone.utc)
    floor_start = now.replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=24)
    edge_post_time = floor_start + dt.timedelta(minutes=2)  # 如 11:02

    # 确保边界帖不在旧窗口内（now-24h 之前）
    old_start = now - dt.timedelta(hours=24)
    assert edge_post_time < old_start, "测试前提：边界帖必须早于旧的 now-24h 起点"

    edge_post = make_post(
        "edge_geimori", "Geimori GU64 Gen 2 got delivered today",
        "first impressions, retention, burr",
        created=edge_post_time,
    )
    conn = _make_db(tmp_path, [edge_post])
    try:
        ctx = aggregate.build_daily_context(
            conn, start=floor_start, end=now,
        )
        kpi = ctx["statistics"]
        assert kpi["total_new_posts"] == 1, f"边界帖必须被包含, got {kpi}"
        assert kpi["geimori_mentions"] == 1
    finally:
        conn.close()


def test_daily_context_default_start_floors_to_hour(tmp_path):
    """不传 start 时默认起点 = floor(now-24h)，与 _resolve_period 一致。"""
    import datetime as dt

    now = dt.datetime.now(dt.timezone.utc)
    floor_start = now.replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=24)
    edge_post_time = floor_start + dt.timedelta(minutes=2)

    # 该帖在默认 floor 窗口内
    edge_post = make_post(
        "edge_geimori2", "Geimori GU63 question",
        "what's the real story",
        created=edge_post_time,
    )
    conn = _make_db(tmp_path, [edge_post])
    try:
        ctx = aggregate.build_daily_context(conn)
        kpi = ctx["statistics"]
        assert kpi["total_new_posts"] == 1, f"默认窗口必须含边界帖, got {kpi}"
        assert kpi["geimori_mentions"] == 1
    finally:
        conn.close()
