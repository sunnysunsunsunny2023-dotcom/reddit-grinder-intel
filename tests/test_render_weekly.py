"""render_weekly.py 单元测试：Weekly Intelligence 渲染与数据规范化。"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from coze.render_weekly import (  # noqa: E402
    build_weekly_pulse,
    render_weekly_html,
    write_outputs,
)


def _payload() -> dict:
    return {
        "batch_id": 9,
        "period": {"start": "2026-09-23T00:00:00", "end": "2026-09-30T00:00:00"},
        "statistics": {
            "total_new_posts": 1200,
            "grinder_related_posts": 180,
            "grinder_ratio": 0.15,
            "geimori_mentions": 6,
            "competitor_mentions": 40,
        },
        "analysis": {
            "executive_summary": [
                {"point": "static/RDT 讨论上升", "evidence": "14 帖中 8 帖提到，为上周 1.8 倍。"},
                {"point": "竞品 X 新机型口碑升温", "evidence": "提及从 5 升至 18。"},
            ],
            "demand_trends": [
                {
                    "topic": "static",
                    "mentions": 31,
                    "previous_week": 18,
                    "wow_change_pct": 72.2,
                    "share_of_grinder_discussion": 0.246,
                    "avg_30d": 19.4,
                    "trend": "rising",
                    "confidence": "high",
                    "interpretation": "多帖讨论 RDT 喷水问题。",
                },
                {
                    "topic": "noise",
                    "mentions": 12,
                    "previous_week": 20,
                    "wow_change_pct": -40.0,
                    "share_of_grinder_discussion": 0.095,
                    "avg_30d": 17.0,
                    "trend": "declining",
                    "confidence": "medium",
                    "interpretation": "噪音讨论降温。",
                },
            ],
            "competitor_intelligence": [
                {
                    "brand": "BrandX Pro",
                    "mentions": 18,
                    "sentiment": "mixed",
                    "common_praise": "研磨均匀",
                    "common_complaints": "静电严重",
                    "high_engagement_threads": [
                        "https://www.reddit.com/r/Coffee/comments/1zz/review/"
                    ],
                    "potential_opportunity": "静电痛点可作对比内容素材",
                    "mention_share_note": "此为 Reddit 讨论份额，非市场份额。",
                }
            ],
            "geimori_voice": {
                "mentions": 6,
                "sentiment": "positive",
                "positive_themes": ["静音表现", "首次印象好"],
                "negative_themes": [],
                "questions_objections": ["是否支持 T38 配件？"],
                "comparison_targets": ["BrandX Pro"],
                "actionable_response": "正面回帖 + 配件兼容说明",
            },
            "customer_priorities": [
                {
                    "priority": "structural_priority",
                    "topic": "static/RDT",
                    "priority_score": 92,
                    "interpretation": "连续多周高频，样本充足。",
                }
            ],
            "next_week_actions": [
                {
                    "action": "发布 RDT 使用指南内容",
                    "evidence": "本周 8/14 帖提及",
                    "business_reason": "配合搜索流量",
                    "confidence": "high",
                    "urgency": "high",
                    "category": "Blog_SEO",
                }
            ],
        },
    }


def test_render_weekly_doctype_and_single_file():
    pulse = build_weekly_pulse(_payload(), report_date="2026-09-30")
    html_text = render_weekly_html(pulse)
    assert html_text.startswith("<!doctype html>")
    assert "<html lang=" in html_text
    assert "<table>" in html_text
    assert "<script src=" not in html_text
    assert "https://www.reddit.com/r/Coffee/comments/1zz/review/" in html_text


def test_render_weekly_real_links_and_no_markdown():
    pulse = build_weekly_pulse(_payload(), report_date="2026-09-30")
    html_text = render_weekly_html(pulse)
    assert 'target="_blank"' in html_text
    assert 'rel="noopener noreferrer"' in html_text
    assert "[来源" not in html_text
    assert "[公开数据]" not in html_text


def test_render_weekly_trend_labels():
    pulse = build_weekly_pulse(_payload(), report_date="2026-09-30")
    html_text = render_weekly_html(pulse)
    assert "+72.2%" in html_text  # WoW 正值带 +
    assert "-40.0%" in html_text  # WoW 负值
    assert "rising" in html_text
    assert "declining" in html_text


def test_build_weekly_tolerates_missing_fields():
    payload = _payload()
    payload["analysis"] = {}
    pulse = build_weekly_pulse(payload, report_date="2026-09-30")
    assert pulse["executive_summary"] == []
    assert pulse["demand_trends"] == []
    assert pulse["competitor_intelligence"] == []
    assert pulse["customer_priorities"] == []
    assert pulse["next_week_actions"] == []
    assert pulse["geimori_voice"]["mentions"] is None
    assert pulse["kpi"]["total_new_posts"] == 1200


def test_write_weekly_outputs(tmp_path):
    pulse = build_weekly_pulse(_payload(), report_date="2026-09-30")
    html_text = render_weekly_html(pulse)
    paths = write_outputs(str(tmp_path), pulse, html_text, raw_payload=_payload())
    assert Path(paths["html"]).exists()
    assert Path(paths["json"]).exists()
    with open(paths["json"], "r", encoding="utf-8") as fh:
        assert json.load(fh)["batch_id"] == 9
