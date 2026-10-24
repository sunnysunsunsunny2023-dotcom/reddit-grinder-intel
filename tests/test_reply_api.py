"""draft-reply API 测试：鉴权 / 幂等 / dry-run / 落库。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

API_KEY = "test-key"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("REDDIT_API_KEY", API_KEY)
    from collector import storage

    conn = storage.connect(str(tmp_path / "api.db"))
    storage.init_schema(conn, "sql/schema.sql")
    conn.close()
    return TestClient(app)


def _fake_llm(messages, temperature=0.3, max_tokens=4096, json_mode=True):
    """根据 prompt 角色区分分类/回复，返回对应 JSON。"""
    system = messages[0]["content"] if messages else ""
    if "community classifier" in system:
        return (
            '{"reply_needed": true, "priority": "P1", '
            '"category": "product_complaint", "product": "GU63", '
            '"sentiment": "negative", "reason": "complaint", "confidence": 0.9}'
        )
    return '{"draft_reply": "Sorry to hear that! Please DM us so we can sort it out."}'


def test_draft_reply_requires_key(client):
    r = client.post("/api/geimori/draft-reply", json={
        "post_id": "p1", "comment_id": "c1", "comment": "broken",
    })
    assert r.status_code == 401


def test_draft_reply_dry_run_complaint(client, monkeypatch):
    monkeypatch.setattr(
        "analyst.mention_classifier.deepseek_client.chat", _fake_llm
    )
    monkeypatch.setattr(
        "analyst.mention_classifier.deepseek_client.is_configured", lambda: True
    )
    monkeypatch.setattr(
        "analyst.reply_engine.deepseek_client.chat", _fake_llm
    )
    r = client.post("/api/geimori/draft-reply", headers={"X-API-Key": API_KEY}, json={
        "post_id": "p1", "comment_id": "c1", "comment": "broken grinder, want refund",
        "context": "GU63 review", "dry_run": True,
    })
    assert r.status_code == 200
    d = r.json()
    assert d["dry_run"] is True
    assert d["reply_needed"] is True
    assert d["priority"] == "P1"
    assert "DM us" in d["draft_reply"]


def test_draft_reply_persists_and_idempotent(client, monkeypatch):
    monkeypatch.setattr(
        "analyst.mention_classifier.deepseek_client.chat", _fake_llm
    )
    monkeypatch.setattr(
        "analyst.mention_classifier.deepseek_client.is_configured", lambda: True
    )
    monkeypatch.setattr(
        "analyst.reply_engine.deepseek_client.chat", _fake_llm
    )
    headers = {"X-API-Key": API_KEY}
    body = {
        "post_id": "p10", "comment_id": "c10", "comment": "gu63 defective, refund",
        "context": "title", "subreddit": "pourover", "product": "GU63",
    }
    r1 = client.post("/api/geimori/draft-reply", headers=headers, json=body)
    r2 = client.post("/api/geimori/draft-reply", headers=headers, json=body)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["task_id"] == r2.json()["task_id"]
    assert r1.json()["status"] == "draft_ready"

    import os
    from collector import storage

    conn = storage.connect(os.environ["DATABASE_PATH"])
    tasks = conn.execute("SELECT COUNT(*) FROM reply_tasks").fetchone()[0]
    drafts = conn.execute("SELECT COUNT(*) FROM reply_drafts").fetchone()[0]
    assert tasks == 1
    assert drafts == 1
    conn.close()


def test_draft_reply_no_reply_needed(client, monkeypatch):
    """无关/正面评论不生成草稿。"""
    monkeypatch.setattr(
        "analyst.mention_classifier.deepseek_client.is_configured", lambda: False
    )
    r = client.post("/api/geimori/draft-reply", headers={"X-API-Key": API_KEY}, json={
        "post_id": "p11", "comment_id": "c11", "comment": "love this grinder, amazing",
    })
    assert r.status_code == 200
    d = r.json()
    assert d["reply_needed"] is False
    assert d["draft_reply"] == ""
    assert d["requires_human_review"] is True


def test_reply_queue_and_stats(client, monkeypatch):
    monkeypatch.setattr(
        "analyst.mention_classifier.deepseek_client.chat", _fake_llm
    )
    monkeypatch.setattr(
        "analyst.mention_classifier.deepseek_client.is_configured", lambda: True
    )
    monkeypatch.setattr(
        "analyst.reply_engine.deepseek_client.chat", _fake_llm
    )
    headers = {"X-API-Key": API_KEY}
    client.post("/api/geimori/draft-reply", headers=headers, json={
        "post_id": "p20", "comment_id": "c20", "comment": "broken, refund",
        "subreddit": "espresso",
    })
    q = client.get("/api/geimori/reply-queue", headers=headers)
    assert q.status_code == 200
    assert q.json()["count"] == 1
    assert q.json()["tasks"][0]["latest_draft"]["draft_text"] != ""

    s = client.get("/api/geimori/reply-stats", headers=headers)
    assert s.json()["total_tasks"] == 1
    assert s.json()["by_status"].get("draft_ready") == 1
