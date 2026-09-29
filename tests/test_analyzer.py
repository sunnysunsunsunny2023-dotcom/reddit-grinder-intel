"""analyzer 模块测试：prompt 构建与 LLM 输出纪律（不调 API）。"""
from __future__ import annotations

from analyst.analyzer import (
    DAILY_POST_LIMIT,
    _build_daily_prompt,
    _posts_for_llm,
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
