"""Weekly 上下文回归测试：RGI-006 avg_7d 缺失导致 502。"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analyst.analyzer import _context_md, build_weekly_context  # noqa: E402
from app import db  # noqa: E402
from collector.storage import init_schema, upsert_posts  # noqa: E402
from tests.conftest import make_post  # noqa: E402


@pytest.fixture
def weekly_db(tmp_path, monkeypatch):
    """临时 SQLite：最近 14 天内有磨豆机相关帖子（当前周 + 上周）。"""
    db_path = str(tmp_path / "weekly_test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    conn = db.get_conn()
    init_schema(conn, str(ROOT / "sql" / "schema.sql"))
    now = dt.datetime.now(dt.timezone.utc)
    rows = [
        make_post(
            "w1", "Static is driving me crazy with my new grinder",
            selftext="RDT helps but retention is bad. espresso workflow.",
            created=now - dt.timedelta(days=1),
            score=120, comments=45,
        ),
        make_post(
            "w2", "Just got my Geimori GU63",
            selftext="quiet, consistent grind, no retention",
            created=now - dt.timedelta(days=2),
            score=80, comments=20,
        ),
        make_post(
            "w3", "DF64 vs Niche Zero for espresso",
            selftext="dial in shots, burr alignment, grinder noise",
            subreddit="espresso",
            created=now - dt.timedelta(days=9),
            score=60, comments=30,
        ),
        make_post(
            "w4", "Camping trip grinder portable",
            selftext="need portable lightweight, timemore chestnut",
            created=now - dt.timedelta(days=10),
            score=15, comments=4,
        ),
    ]
    for r in rows:
        r["raw_json"] = {}
    upsert_posts(conn, rows)
    yield conn
    conn.close()


def test_context_md_tolerates_missing_avg_7d():
    """回归 RGI-006：topic_trends 缺 avg_7d 时 _context_md 不抛 KeyError。"""
    context = {
        "statistics": {
            "total_new_posts": 100,
            "grinder_related_posts": 51,
            "grinder_ratio": 0.51,
            "geimori_mentions": 1,
            "competitor_mentions": 34,
            "alert_count": 5,
        },
        "signal_alerts": [],
        "topic_trends": {
            "static": {
                "mentions": 8,
                "unique_posts": 8,
                "engagement": 200,
                "avg_30d": 4.0,
                "trend": "rising",
            }
        },
        "brand_trends": {},
        "competitor_trends": {},
        "posts": [],
    }
    md = _context_md(context)
    assert "static" in md
    assert "avg_7d" not in md or "—" in md


def test_build_weekly_context_includes_avg_7d(weekly_db):
    """回归 RGI-006：build_weekly_context 的 topic_trends 必须含 avg_7d。"""
    ctx = build_weekly_context(weekly_db)
    assert ctx["topic_trends"]
    for topic, t in ctx["topic_trends"].items():
        assert "avg_7d" in t, f"{topic} 缺 avg_7d（_context_md 会 502）"
        assert "share_of_grinder_discussion" in t
        assert "previous_week" in t
        assert "wow_change_pct" in t
    # _context_md 全量渲染不抛错
    md = _context_md(ctx)
    assert "## Topic trends" in md
