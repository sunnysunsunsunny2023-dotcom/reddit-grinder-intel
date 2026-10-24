"""Phase 2 飞书审批闭环测试：状态机 / 并发锁 / 签名校验 / 幂等 / 人工发布。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app

API_KEY = "test-key"
APP_SECRET = "test-app-secret"
VER_TOKEN = "test-ver-token"


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
    system = messages[0]["content"] if messages else ""
    if "community classifier" in system:
        return (
            '{"reply_needed": true, "priority": "P1", '
            '"category": "product_complaint", "product": "GU63", '
            '"sentiment": "negative", "reason": "complaint", "confidence": 0.9}'
        )
    return '{"draft_reply": "Sorry to hear that! Please DM us so we can sort it out."}'


def _seed_task(client, post_id="p1", comment_id="c1", comment="broken grinder, want refund"):
    return client.post(
        "/api/geimori/draft-reply",
        headers={"X-API-Key": API_KEY},
        json={
            "post_id": post_id,
            "comment_id": comment_id,
            "comment": comment,
            "context": "GU63 review",
        },
    ).json()


def test_approve_flow(client, monkeypatch):
    monkeypatch.setattr("analyst.mention_classifier.deepseek_client.chat", _fake_llm)
    monkeypatch.setattr("analyst.mention_classifier.deepseek_client.is_configured", lambda: True)
    monkeypatch.setattr("analyst.reply_engine.deepseek_client.chat", _fake_llm)

    d = _seed_task(client)
    task_id = d["task_id"]
    assert d["status"] == "draft_ready"

    # approve
    r = client.post("/api/reddit/reply-approve", headers={"X-API-Key": API_KEY}, json={
        "task_id": task_id, "action": "approve", "draft_version": 1,
        "reviewer_id": "ou_reviewer1", "reviewer_name": "Alice",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["task"]["status"] == "approved"
    assert body["approval"]["action"] == "approve"
    assert body["approval"]["status"] == "effective"
    assert body["approval"]["reviewer_name"] == "Alice"
    assert body["approval"]["approved_text"] == "Sorry to hear that! Please DM us so we can sort it out."

    # 重复 approve → 并发冲突 409
    r2 = client.post("/api/reddit/reply-approve", headers={"X-API-Key": API_KEY}, json={
        "task_id": task_id, "action": "approve", "draft_version": 1,
        "reviewer_id": "ou_reviewer2",
    })
    assert r2.status_code == 409
    assert r2.json()["detail"] == "task no longer in draft_ready (handled by someone else)"


def test_edit_approve_requires_text(client, monkeypatch):
    monkeypatch.setattr("analyst.mention_classifier.deepseek_client.chat", _fake_llm)
    monkeypatch.setattr("analyst.mention_classifier.deepseek_client.is_configured", lambda: True)
    monkeypatch.setattr("analyst.reply_engine.deepseek_client.chat", _fake_llm)

    d = _seed_task(client)
    task_id = d["task_id"]

    # edit_approve 不带文本 → 422
    r = client.post("/api/reddit/reply-approve", headers={"X-API-Key": API_KEY}, json={
        "task_id": task_id, "action": "edit_approve", "draft_version": 1,
        "reviewer_id": "ou_reviewer1",
    })
    assert r.status_code == 422

    # edit_approve 带文本 → approved + 锁定自定义文本
    r2 = client.post("/api/reddit/reply-approve", headers={"X-API-Key": API_KEY}, json={
        "task_id": task_id, "action": "edit_approve", "draft_version": 1,
        "reviewer_id": "ou_reviewer1", "reviewer_name": "Bob",
        "approved_text": "We're sorry for the trouble. Please DM us your order ID.",
    })
    assert r2.status_code == 200
    body = r2.json()
    assert body["task"]["status"] == "approved"
    assert body["approval"]["action"] == "edit_approve"
    assert body["approval"]["approved_text"] == "We're sorry for the trouble. Please DM us your order ID."


def test_reject_and_skip(client, monkeypatch):
    monkeypatch.setattr("analyst.mention_classifier.deepseek_client.chat", _fake_llm)
    monkeypatch.setattr("analyst.mention_classifier.deepseek_client.is_configured", lambda: True)
    monkeypatch.setattr("analyst.reply_engine.deepseek_client.chat", _fake_llm)

    d = _seed_task(client, post_id="p2", comment_id="c2")
    task_id = d["task_id"]
    r = client.post("/api/reddit/reply-approve", headers={"X-API-Key": API_KEY}, json={
        "task_id": task_id, "action": "reject", "draft_version": 1,
        "reviewer_id": "ou_r", "reviewer_name": "Carol",
    })
    assert r.status_code == 200
    assert r.json()["task"]["status"] == "rejected"

    d2 = _seed_task(client, post_id="p3", comment_id="c3")
    task_id2 = d2["task_id"]
    r2 = client.post("/api/reddit/reply-approve", headers={"X-API-Key": API_KEY}, json={
        "task_id": task_id2, "action": "skip", "draft_version": 1,
        "reviewer_id": "ou_r2",
    })
    assert r2.status_code == 200
    assert r2.json()["task"]["status"] == "skipped"


def test_manual_publish_only_approved(client, monkeypatch):
    monkeypatch.setattr("analyst.mention_classifier.deepseek_client.chat", _fake_llm)
    monkeypatch.setattr("analyst.mention_classifier.deepseek_client.is_configured", lambda: True)
    monkeypatch.setattr("analyst.reply_engine.deepseek_client.chat", _fake_llm)

    # 未审批任务不可发布
    d = _seed_task(client, post_id="p4", comment_id="c4")
    task_id = d["task_id"]
    r = client.post("/api/geimori/manual-publish", headers={"X-API-Key": API_KEY}, json={
        "task_id": task_id, "operator_id": "op1",
        "reddit_reply_id": "r1",
    })
    assert r.status_code == 422
    assert "expected approved" in r.json()["detail"]

    # 审批后发布成功
    r2 = client.post("/api/reddit/reply-approve", headers={"X-API-Key": API_KEY}, json={
        "task_id": task_id, "action": "approve", "draft_version": 1,
        "reviewer_id": "ou_r", "reviewer_name": "Dave",
    })
    assert r2.status_code == 200
    r3 = client.post("/api/geimori/manual-publish", headers={"X-API-Key": API_KEY}, json={
        "task_id": task_id, "operator_id": "op1", "operator_name": "Dave",
        "reddit_reply_id": "r1", "published_url": "https://reddit.com/r/x/comments/p4/r1",
    })
    assert r3.status_code == 200
    body = r3.json()
    assert body["task"]["status"] == "published"
    assert body["final_text"] == "Sorry to hear that! Please DM us so we can sort it out."

    # 重复发布 → 409
    r4 = client.post("/api/geimori/manual-publish", headers={"X-API-Key": API_KEY}, json={
        "task_id": task_id, "operator_id": "op2", "reddit_reply_id": "r2",
    })
    assert r4.status_code == 409
    assert r4.json()["detail"] == "already published"


def test_regenerate_audit_only(client, monkeypatch):
    monkeypatch.setattr("analyst.mention_classifier.deepseek_client.chat", _fake_llm)
    monkeypatch.setattr("analyst.mention_classifier.deepseek_client.is_configured", lambda: True)
    monkeypatch.setattr("analyst.reply_engine.deepseek_client.chat", _fake_llm)

    d = _seed_task(client, post_id="p5", comment_id="c5")
    task_id = d["task_id"]
    r = client.post("/api/reddit/reply-approve", headers={"X-API-Key": API_KEY}, json={
        "task_id": task_id, "action": "regenerate", "draft_version": 1,
        "reviewer_id": "ou_r", "reviewer_name": "Eve",
    })
    assert r.status_code == 200
    # regenerate 不改变任务状态（调用方生成新草稿后回 draft_ready）
    assert r.json()["task"]["status"] == "draft_ready"
    assert r.json()["approval"]["action"] == "regenerate"


def test_audit_log(client, monkeypatch):
    monkeypatch.setattr("analyst.mention_classifier.deepseek_client.chat", _fake_llm)
    monkeypatch.setattr("analyst.mention_classifier.deepseek_client.is_configured", lambda: True)
    monkeypatch.setattr("analyst.reply_engine.deepseek_client.chat", _fake_llm)

    d = _seed_task(client, post_id="p6", comment_id="c6")
    task_id = d["task_id"]
    client.post("/api/reddit/reply-approve", headers={"X-API-Key": API_KEY}, json={
        "task_id": task_id, "action": "reject", "draft_version": 1,
        "reviewer_id": "ou_r", "reviewer_name": "Frank",
    })
    r = client.get("/api/geimori/audit-log", headers={"X-API-Key": API_KEY})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 1
    assert any(x["action"] == "reject" and x["reviewer_name"] == "Frank" for x in body["log"])


def test_review_push_dry_run(client, monkeypatch):
    monkeypatch.setattr("analyst.mention_classifier.deepseek_client.chat", _fake_llm)
    monkeypatch.setattr("analyst.mention_classifier.deepseek_client.is_configured", lambda: True)
    monkeypatch.setattr("analyst.reply_engine.deepseek_client.chat", _fake_llm)

    _seed_task(client, post_id="p7", comment_id="c7")
    r = client.post("/api/geimori/review-push", params={"dry_run": "true"},
                    headers={"X-API-Key": API_KEY})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["dry_run"] is True
    assert body["count"] >= 1
    card = body["results"][0]["card"]
    assert card["header"]["title"]["content"].startswith("Geimori 回复审核")
    actions = card["elements"][1]["actions"]
    acts = {a["value"]["action"] for a in actions}
    assert acts == {"approve", "edit_approve", "regenerate", "reject", "skip"}
    # 卡片包含评论原文与 AI 草稿
    md = card["elements"][0]["text"]["content"]
    assert "broken grinder" in md
    assert "Please DM us" in md


def _signed_headers(body_bytes: str, secret: str = APP_SECRET) -> dict:
    ts, nonce = "1700000000", "abc123"
    msg = f"{ts}\n{nonce}\n{body_bytes}".encode()
    sig = base64.b64encode(hmac.new(secret.encode(), msg, hashlib.sha256).digest()).decode()
    return {
        "X-Lark-Signature": sig,
        "X-Lark-Request-Timestamp": ts,
        "X-Lark-Request-Nonce": nonce,
    }


def test_feishu_callback_unconfigured_returns_503(client, monkeypatch):
    """未配置 FEISHU_APP_SECRET / VERIFICATION_TOKEN 时回调返回 503，不做审批。"""
    monkeypatch.setenv("FEISHU_APP_SECRET", "")
    monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", "")
    r = client.post("/api/feishu/reply-callback", json={"token": "x"})
    assert r.status_code == 503
    assert "未配置" in r.json()["detail"]


def test_feishu_callback_bad_signature(client, monkeypatch):
    monkeypatch.setenv("FEISHU_APP_SECRET", APP_SECRET)
    monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", VER_TOKEN)
    body = json.dumps({"token": VER_TOKEN, "type": "card.action.trigger"})
    headers = _signed_headers(body, secret="wrong-secret")
    r = client.post("/api/feishu/reply-callback", content=body, headers=headers)
    assert r.status_code == 403
    assert "signature mismatch" in r.json()["detail"]


def test_feishu_callback_url_verification_challenge(client, monkeypatch):
    monkeypatch.setenv("FEISHU_APP_SECRET", APP_SECRET)
    monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", VER_TOKEN)
    body = json.dumps({"token": VER_TOKEN, "type": "url_verification",
                       "challenge": "ch-123"})
    headers = _signed_headers(body)
    r = client.post("/api/feishu/reply-callback", content=body, headers=headers)
    assert r.status_code == 200
    assert r.json()["challenge"] == "ch-123"


def test_feishu_callback_card_action_approve(client, monkeypatch):
    """校验通过的回调可触发真实审批（并发锁同样生效）。"""
    monkeypatch.setenv("FEISHU_APP_SECRET", APP_SECRET)
    monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", VER_TOKEN)
    monkeypatch.setattr("analyst.mention_classifier.deepseek_client.chat", _fake_llm)
    monkeypatch.setattr("analyst.mention_classifier.deepseek_client.is_configured", lambda: True)
    monkeypatch.setattr("analyst.reply_engine.deepseek_client.chat", _fake_llm)

    d = _seed_task(client, post_id="p8", comment_id="c8")
    task_id = d["task_id"]

    body = json.dumps({
        "token": VER_TOKEN,
        "type": "card.action.trigger",
        "header": {"operator_id": "ou_alice", "operator_name": "Alice"},
        "event": {"action": {"value": {
            "task_id": task_id, "action": "approve", "draft_version": 1,
        }}},
    })
    headers = _signed_headers(body)
    r = client.post("/api/feishu/reply-callback", content=body, headers=headers)
    assert r.status_code == 200
    assert r.json()["task_status"] == "approved"

    # 回调也防并发：再次触发 → 409
    r2 = client.post("/api/feishu/reply-callback", content=body, headers=headers)
    assert r2.status_code == 409


def test_verify_callback_missing_headers(client, monkeypatch):
    monkeypatch.setenv("FEISHU_APP_SECRET", APP_SECRET)
    monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", VER_TOKEN)
    body = json.dumps({"token": VER_TOKEN})
    r = client.post("/api/feishu/reply-callback", content=body, headers={
        "X-Lark-Signature": "x", "X-Lark-Request-Timestamp": "", "X-Lark-Request-Nonce": "",
    })
    assert r.status_code == 403
    assert "missing" in r.json()["detail"]
