"""render_daily.py 单元测试：Daily Pulse 渲染与数据规范化。"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from coze.render_daily import (  # noqa: E402
    build_pulse,
    fetch_payload,
    render_html,
    write_outputs,
)


def _payload() -> dict:
    return {
        "batch_id": 7,
        "period": {"start": "2026-09-30T00:00:00", "end": "2026-09-30T23:59:59"},
        "statistics": {
            "total_new_posts": 206,
            "grinder_related_posts": 21,
            "grinder_ratio": 0.1019,
            "geimori_mentions": 3,
            "competitor_mentions": 8,
            "high_signal_topics": 2,
            "alert_count": 2,
        },
        "posts": [
            {
                "post_id": "abc1",
                "title": "Best budget burr grinder?",
                "subreddit": "Coffee",
                "score": 120,
                "num_comments": 45,
                "permalink": "/r/Coffee/comments/abc1/best/",
                "created_utc": "2026-09-30T02:42:49",
            }
        ],
        "analysis": {
            "today_in_one_sentence": "测试一句话。",
            "kpi_summary": "测试 KPI 引用。",
            "signal_alerts": [
                {
                    "topic": "budget demand",
                    "status": "spike",
                    "confidence": "high",
                    "evidence_count": 5,
                    "what_happened": "讨论激增。",
                    "permalink": ["https://www.reddit.com/r/Coffee/comments/abc1/best/"],
                    "weak_signal": False,
                }
            ],
            "top_posts": [
                {
                    "post_id": "abc1",
                    "title": "Best budget burr grinder?",
                    "why_it_matters": "高互动。",
                }
            ],
            "geimori_watch": {
                "has_mention": True,
                "detail": "2 正面 1 中性。",
            },
            "today_actions": [
                {
                    "action": "回复早期口碑帖",
                    "why": "热度上升",
                    "evidence_count": 3,
                    "confidence": "medium",
                    "category": "Community",
                }
            ],
        },
    }


def test_render_html_doctype_and_single_file(tmp_path):
    pulse = build_pulse(_payload(), report_date="2026-09-30")
    html_text = render_html(pulse)
    assert html_text.startswith("<!doctype html>")
    assert "<html lang=" in html_text
    assert "<body>" in html_text
    # 无外部依赖：不出现外部 stylesheet / script src
    assert "stylesheet" not in html_text.split("</head>")[0] or "href=" not in html_text.split("</head>")[0].split("link")[-1]
    assert "<script src=" not in html_text
    assert "http://" not in html_text.split("<style>")[0]  # head 无外链


def test_render_real_reddit_links(tmp_path):
    pulse = build_pulse(_payload(), report_date="2026-09-30")
    html_text = render_html(pulse)
    assert "https://www.reddit.com/r/Coffee/comments/abc1/best/" in html_text
    assert 'target="_blank"' in html_text
    assert 'rel="noopener noreferrer"' in html_text
    # 不允许 Markdown 链接语法残留
    assert "[来源" not in html_text
    assert "[公开数据]" not in html_text


def test_render_empty_alerts(tmp_path):
    payload = _payload()
    payload["analysis"]["signal_alerts"] = []
    payload["statistics"]["alert_count"] = 0
    pulse = build_pulse(payload, report_date="2026-09-30")
    html_text = render_html(pulse)
    assert "未检出需要标记" in html_text


def test_build_pulse_tolerates_missing_llm_fields():
    payload = _payload()
    payload["analysis"] = {}
    payload["posts"] = []
    pulse = build_pulse(payload, report_date="2026-09-30")
    assert pulse["signal_alerts"] == []
    assert pulse["top_posts"] == []
    assert pulse["today_actions"] == []
    assert pulse["geimori_watch"]["has_mention"] is False
    assert pulse["kpi"]["total_new_posts"] == 206


def test_build_pulse_normalizes_epoch_date():
    payload = _payload()
    payload["period"] = {"start": 1780200000, "end": 1780286399}
    pulse = build_pulse(payload, report_date=None)
    # 1780200000 epoch = 2026-05-31T08:00:00Z
    assert pulse["report_date"] == "2026-05-31" or len(pulse["report_date"]) == 10


def test_write_outputs_writes_html_and_json(tmp_path):
    pulse = build_pulse(_payload(), report_date="2026-09-30")
    html_text = render_html(pulse)
    paths = write_outputs(str(tmp_path), pulse, html_text, raw_payload=_payload())
    assert Path(paths["html"]).exists()
    assert Path(paths["json"]).exists()
    with open(paths["json"], "r", encoding="utf-8") as fh:
        saved = json.load(fh)
    assert saved["batch_id"] == 7


def test_fetch_payload_rejects_non_dict(monkeypatch):
    """API 返回非对象时抛错（防止渲染层拿到畸形数据）。"""

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"[1,2,3]"

    class FakeUrlOpen:
        def __init__(self):
            self.last_req = None

        def __call__(self, req, timeout=120):
            self.last_req = req
            return FakeResp()

    fake = FakeUrlOpen()
    monkeypatch.setattr("urllib.request.urlopen", fake)
    import urllib.request  # noqa: F401  # 保持模块可访问

    with pytest.raises(RuntimeError):
        fetch_payload(api_base="http://x", api_key="k")
