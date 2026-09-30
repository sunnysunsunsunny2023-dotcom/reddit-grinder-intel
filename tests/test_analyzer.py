"""analyzer 模块测试：prompt 构建与 LLM 输出纪律（不调 API）。"""
from __future__ import annotations

import datetime as dt
import json

from analyst.analyzer import (
    DAILY_POST_LIMIT,
    _build_daily_prompt,
    _posts_for_llm,
    analyze_batch,
)

from tests.conftest import make_post


def test_posts_for_llm_limits_text_and_count():
    long_text = "x" * 2000
    posts = [make_post(f"p{i}", f"title {i}", long_text) for i in range(100)]
    out = _posts_for_llm(posts, DAILY_POST_LIMIT)
    assert len(out) == DAILY_POST_LIMIT
    assert all(len(p["selftext"]) <= 800 for p in out)


def test_daily_prompt_contains_discipline():
    posts = [make_post("p1", "Static issue", "rdt retention")]
    context = {
        "posts": posts,
        "statistics": {
            "total_new_posts": 1, "grinder_related_posts": 1,
            "grinder_ratio": 1.0, "geimori_mentions": 0,
            "competitor_mentions": 0, "high_signal_topics": 0,
            "alert_count": 0,
        },
        "signal_alerts": [],
        "topic_trends": {},
        "brand_trends": {"geimori": {"mentions": 0}},
        "competitor_trends": {},
        "top_posts": [],
    }
    messages = _build_daily_prompt(context)
    user_content = messages[-1]["content"]
    assert "禁止自己计算" in messages[0]["content"]
    assert "grinder_related_posts: 1" in user_content
    assert "今日" not in user_content  # 保持英文 prompt 结构（可选）


def test_daily_prompt_no_llm_computation_instruction():
    messages = _build_daily_prompt(
        {
            "posts": [],
            "statistics": {
                "total_new_posts": 0, "grinder_related_posts": 0,
                "grinder_ratio": 0.0, "geimori_mentions": 0,
                "competitor_mentions": 0, "high_signal_topics": 0,
                "alert_count": 0,
            },
            "signal_alerts": [],
            "topic_trends": {},
            "brand_trends": {"geimori": {"mentions": 0}},
            "competitor_trends": {},
            "top_posts": [],
        }
    )
    system = messages[0]["content"]
    assert "禁止自己计算百分比" in system
    assert "mention share 不等于 market share" in system


def test_analyze_batch_serializes_result_json(tmp_path, monkeypatch):
    """analyze_batch 把 LLM dict 结果序列化写入 analysis_results（防 sqlite dict 绑定错误）。"""
    import analyst.analyzer as analyzer
    from analyst import deepseek_client
    from collector import storage

    fake_result = {"summary": "fake summary", "topics": [{"topic": "grinder", "posts": ["p1"]}]}
    monkeypatch.setattr(deepseek_client, "is_configured", lambda: True)
    monkeypatch.setattr(
        deepseek_client, "chat",
        lambda messages, temperature=0.3, max_tokens=4096, json_mode=True: json.dumps(fake_result),
    )

    db_path = tmp_path / "a.db"
    conn = storage.connect(str(db_path))
    storage.init_schema(conn, "sql/schema.sql")
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO analysis_batches"
        " (report_type, period_start, period_end, analysis_version, status)"
        " VALUES ('daily', '2026-01-01T00:00:00', '2026-01-02T00:00:00', 'v1', 'pending')"
    )
    conn.commit()
    batch_id = cur.lastrowid

    context = {
        "posts": [],
        "statistics": {
            "total_new_posts": 0, "grinder_related_posts": 0,
            "grinder_ratio": 0.0, "geimori_mentions": 0,
            "competitor_mentions": 0, "high_signal_topics": 0,
            "alert_count": 0,
        },
        "signal_alerts": [],
        "topic_trends": {},
        "brand_trends": {"geimori": {"mentions": 0}},
        "competitor_trends": {},
        "top_posts": [],
        "period_start": dt.datetime(2026, 1, 1, 0, 0, 0),
        "period_end": dt.datetime(2026, 1, 2, 0, 0, 0),
    }
    result = analyze_batch(conn, batch_id, "daily", context)
    assert result == fake_result

    cur.execute(
        "SELECT result_json FROM analysis_results WHERE batch_id=?",
        (batch_id,),
    )
    row = cur.fetchone()
    assert row is not None
    assert json.loads(row[0])["summary"] == "fake summary"
    conn.close()


def test_analyze_batch_retries_on_empty_content(tmp_path, monkeypatch):
    """DeepSeek 偶发空 content 时 analyze_batch 重试后成功（B120 经验落地）。"""
    import analyst.analyzer as analyzer
    from analyst import deepseek_client
    from collector import storage

    fake_result = {"summary": "retried ok"}
    calls = {"n": 0}

    def flaky_chat(messages, temperature=0.3, max_tokens=4096, json_mode=True):
        calls["n"] += 1
        if calls["n"] == 1:
            raise deepseek_client.DeepSeekError("DeepSeek returned empty content")
        return json.dumps(fake_result)

    monkeypatch.setattr(deepseek_client, "is_configured", lambda: True)
    monkeypatch.setattr(deepseek_client, "chat", flaky_chat)
    monkeypatch.setattr(analyzer.time, "sleep", lambda s: None)

    db_path = tmp_path / "a.db"
    conn = storage.connect(str(db_path))
    storage.init_schema(conn, "sql/schema.sql")
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO analysis_batches"
        " (report_type, period_start, period_end, analysis_version, status)"
        " VALUES ('daily', '2026-01-01T00:00:00', '2026-01-02T00:00:00', 'v1', 'pending')"
    )
    conn.commit()
    batch_id = cur.lastrowid

    context = {
        "posts": [],
        "statistics": {
            "total_new_posts": 0, "grinder_related_posts": 0,
            "grinder_ratio": 0.0, "geimori_mentions": 0,
            "competitor_mentions": 0, "high_signal_topics": 0,
            "alert_count": 0,
        },
        "signal_alerts": [],
        "topic_trends": {},
        "brand_trends": {"geimori": {"mentions": 0}},
        "competitor_trends": {},
        "top_posts": [],
        "period_start": dt.datetime(2026, 1, 1, 0, 0, 0),
        "period_end": dt.datetime(2026, 1, 2, 0, 0, 0),
    }
    result = analyze_batch(conn, batch_id, "daily", context)
    assert result == fake_result
    assert calls["n"] == 2
    conn.close()


def test_analyze_batch_gives_up_after_3_failures(tmp_path, monkeypatch):
    """连续 3 次 DeepSeekError 时最终抛错，不伪造成功。"""
    import pytest

    import analyst.analyzer as analyzer
    from analyst import deepseek_client
    from collector import storage

    def always_fail(messages, temperature=0.3, max_tokens=4096, json_mode=True):
        raise deepseek_client.DeepSeekError("DeepSeek returned empty content")

    monkeypatch.setattr(deepseek_client, "is_configured", lambda: True)
    monkeypatch.setattr(deepseek_client, "chat", always_fail)
    monkeypatch.setattr(analyzer.time, "sleep", lambda s: None)

    db_path = tmp_path / "a.db"
    conn = storage.connect(str(db_path))
    storage.init_schema(conn, "sql/schema.sql")
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO analysis_batches"
        " (report_type, period_start, period_end, analysis_version, status)"
        " VALUES ('daily', '2026-01-01T00:00:00', '2026-01-02T00:00:00', 'v1', 'pending')"
    )
    conn.commit()
    batch_id = cur.lastrowid

    context = {"posts": [], "statistics": {
        "total_new_posts": 0, "grinder_related_posts": 0, "grinder_ratio": 0.0,
        "geimori_mentions": 0, "competitor_mentions": 0, "high_signal_topics": 0,
        "alert_count": 0,
    }, "signal_alerts": [], "topic_trends": {}, "brand_trends": {"geimori": {"mentions": 0}},
        "competitor_trends": {}, "top_posts": []}
    with pytest.raises(deepseek_client.DeepSeekError):
        analyze_batch(conn, batch_id, "daily", context)
    conn.close()


def test_analyze_batch_weekly_uses_larger_max_tokens(tmp_path, monkeypatch):
    """回归 RGI-008：weekly 调用 chat 必须传 8192 max_tokens，防止 JSON 被截断。"""
    import analyst.analyzer as analyzer
    from analyst import deepseek_client
    from collector import storage

    calls: dict = {}

    def record_chat(messages, temperature=0.3, max_tokens=4096, json_mode=True):
        calls["max_tokens"] = max_tokens
        return json.dumps(
            {
                "executive_summary": [],
                "demand_trends": [],
                "competitor_intelligence": [],
                "geimori_voice": {"mentions": 0, "sentiment": "neutral"},
                "customer_priorities": [],
                "next_week_actions": [],
            }
        )

    monkeypatch.setattr(deepseek_client, "is_configured", lambda: True)
    monkeypatch.setattr(deepseek_client, "chat", record_chat)

    db_path = tmp_path / "w.db"
    conn = storage.connect(str(db_path))
    storage.init_schema(conn, "sql/schema.sql")
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO analysis_batches"
        " (report_type, period_start, period_end, analysis_version, status)"
        " VALUES ('weekly', '2026-01-01T00:00:00', '2026-01-08T00:00:00', 'v1', 'pending')"
    )
    conn.commit()
    batch_id = cur.lastrowid

    context = {
        "posts": [],
        "posts_previous_week": [],
        "statistics": {
            "total_new_posts": 0, "grinder_related_posts": 0,
            "grinder_ratio": 0.0, "geimori_mentions": 0,
            "competitor_mentions": 0, "high_signal_topics": 0,
            "alert_count": 0,
        },
        "baseline_7d": {},
        "baseline_30d": {},
        "topic_trends": {},
        "brand_trends": {"geimori": {"mentions": 0}},
        "competitor_trends": {},
        "signal_alerts": [],
        "top_posts": [],
        "period_start": dt.datetime(2026, 1, 1, 0, 0, 0),
        "period_end": dt.datetime(2026, 1, 8, 0, 0, 0),
    }
    analyzer.analyze_batch(conn, batch_id, "weekly", context)
    assert calls["max_tokens"] == 8192
