"""MentionClassifier 测试：DeepSeek 分类 + 规则降级 + 批量过滤。"""
from __future__ import annotations

from analyst.mention_classifier import (
    classify_comment,
    reply_needed_filter,
    _rule_classify,
)


def _fake_llm(messages, temperature=0.3, max_tokens=4096, json_mode=True):
    """模拟 DeepSeek 返回品牌投诉分类。"""
    return (
        '{"reply_needed": true, "priority": "P1", '
        '"category": "product_complaint", "product": "GU63", '
        '"sentiment": "negative", "reason": "complaint about broken grinder", '
        '"confidence": 0.9}'
    )


def test_classify_complaint_via_llm(monkeypatch):
    monkeypatch.setattr("analyst.mention_classifier.deepseek_client.chat", _fake_llm)
    monkeypatch.setattr(
        "analyst.mention_classifier.deepseek_client.is_configured", lambda: True
    )
    out = classify_comment(
        "my GU63 stopped working after a week, want a refund",
        context="Geimori GU63 review",
    )
    assert out["reply_needed"] is True
    assert out["priority"] == "P1"
    assert out["category"] == "product_complaint"
    assert out["provider"] == "deepseek"


def test_classify_rule_fallback_no_key(monkeypatch):
    """未配置 key 时降级规则：投诉词 → P1。"""
    monkeypatch.setattr(
        "analyst.mention_classifier.deepseek_client.is_configured", lambda: False
    )
    out = classify_comment("this grinder is broken and defective, want refund")
    assert out["reply_needed"] is True
    assert out["priority"] == "P1"
    assert out["provider"] == "rules"


def test_classify_rule_positive_no_reply(monkeypatch):
    monkeypatch.setattr(
        "analyst.mention_classifier.deepseek_client.is_configured", lambda: False
    )
    out = classify_comment("love this grinder, amazing and perfect")
    assert out["reply_needed"] is False
    assert out["priority"] == "P3"


def test_classify_rule_technical_question(monkeypatch):
    monkeypatch.setattr(
        "analyst.mention_classifier.deepseek_client.is_configured", lambda: False
    )
    out = classify_comment("how to adjust grind size and settings on GU63?")
    assert out["reply_needed"] is True
    assert out["priority"] == "P2"
    assert out["category"] == "technical_question"


def test_reply_needed_filter(monkeypatch):
    monkeypatch.setattr(
        "analyst.mention_classifier.deepseek_client.is_configured", lambda: False
    )
    rows = [
        {"comment_id": "c1", "body": "broken, defective, want refund"},
        {"comment_id": "c2", "body": "love it, amazing"},
        {"comment_id": "c3", "body": "how to set grind size?"},
    ]
    needed = reply_needed_filter(rows)
    ids = {r["comment_id"] for r in needed}
    assert ids == {"c1", "c3"}
    assert all(r["classification"]["reply_needed"] for r in needed)


def test_rule_classify_deterministic():
    """规则降级确定性：同一输入同一结果。"""
    a = _rule_classify("broken and defective, need refund")
    b = _rule_classify("broken and defective, need refund")
    assert a == b
    assert a["priority"] == "P1"
