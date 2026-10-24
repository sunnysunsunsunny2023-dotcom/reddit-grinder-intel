"""飞书审核闭环（Geimori Reply System V2.0 — Phase 2）。

职责：
1. build_review_card(task, draft, approval_status)：构造飞书交互审核卡片
   （优先级 / Subreddit / 产品 / 原帖链接 / 评论原文 / 上下文 / AI 草稿 /
    推荐理由 / 状态 + Approve / Edit&Approve / Regenerate / Reject / Skip）
2. verify_callback(timestamp, nonce, body_raw, signature)：
   飞书事件订阅回调签名校验（HMAC-SHA256，key=FEISHU_APP_SECRET），
   并校验 body 内 verification token（FEISHU_VERIFICATION_TOKEN）。
3. push_review_cards(webhook_url, cards)：群机器人 webhook 推送（复用
   youtube-kol-monitor/app/feishu_notify.py 的 msg_type=interactive 模式）。

安全边界：
- 未配置 FEISHU_APP_SECRET / FEISHU_VERIFICATION_TOKEN 时回调一律 503 明确提示，
  不做任何审批动作（需求 7.2：签名/令牌校验必须生效）。
- 仅展示卡片链接不视为审批；审批动作必须携带有效 reviewer_id 并经由
  POST /api/reddit/reply-approve 或校验通过的回调触发（需求红线）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json as _json
import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_PRIORITY_LABEL = {"P1": "P1 紧急", "P2": "P2 重要", "P3": "P3 普通", "Skip": "Skip"}


def _clip(text: str, limit: int = 500) -> str:
    if not text:
        return ""
    text = str(text)
    return text[:limit] + ("..." if len(text) > limit else "")


def build_review_card(
    task: Dict[str, Any],
    draft: Optional[Dict[str, Any]],
    approval_status: str = "待审核",
    reviewer_hint: Optional[str] = None,
) -> Dict[str, Any]:
    """构造一张飞书交互审核卡片（card JSON，用于 msg_type=interactive）。"""
    priority = task.get("priority") or "P3"
    label = _PRIORITY_LABEL.get(priority, priority)
    template = "red" if priority == "P1" else ("orange" if priority == "P2" else "blue")

    subreddit = task.get("subreddit") or "-"
    product = task.get("product") or "-"
    permalink = task.get("permalink") or ""
    original_text = _clip(task.get("original_text") or "", 800)
    context = _clip(task.get("context") or "", 400)
    reason = "-"
    category = task.get("category") or ""
    if draft:
        reason = draft.get("reason") or draft.get("category_reason") or "-"
    draft_text = _clip(draft.get("draft_text") if draft else None, 800)

    content_lines = [
        f"**优先级**：{label}　|　**Subreddit**：r/{subreddit}　|　**产品**：{product}",
        f"**状态**：{approval_status}　|　**类别**：{category}",
    ]
    if permalink:
        content_lines.append(f"[原帖链接]({permalink})")
    content_lines.append(f"**评论原文**：\n{original_text}")
    if context:
        content_lines.append(f"**上下文**：\n{context}")
    content_lines.append(f"**AI 草稿**：\n{draft_text if draft_text else '（尚未生成）'}")
    content_lines.append(f"**推荐理由**：{reason}")
    if reviewer_hint:
        content_lines.append(f"**操作人**：{reviewer_hint}")
    md_content = "\n".join(content_lines)

    card: Dict[str, Any] = {
        "config": {"wide_screen_mode": True, "enable_forward": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": f"Geimori 回复审核 · {label}"},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": md_content}},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "Approve ✅"},
                        "type": "primary",
                        "value": {
                            "task_id": task.get("task_id"),
                            "action": "approve",
                            "draft_version": draft.get("draft_version") if draft else 1,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "Edit & Approve ✏️"},
                        "type": "default",
                        "value": {
                            "task_id": task.get("task_id"),
                            "action": "edit_approve",
                            "draft_version": draft.get("draft_version") if draft else 1,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "Regenerate 🔄"},
                        "type": "default",
                        "value": {
                            "task_id": task.get("task_id"),
                            "action": "regenerate",
                            "draft_version": draft.get("draft_version") if draft else 1,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "Reject ⛔"},
                        "type": "danger",
                        "value": {
                            "task_id": task.get("task_id"),
                            "action": "reject",
                            "draft_version": draft.get("draft_version") if draft else 1,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "Skip ⏭️"},
                        "type": "default",
                        "value": {
                            "task_id": task.get("task_id"),
                            "action": "skip",
                            "draft_version": draft.get("draft_version") if draft else 1,
                        },
                    },
                ],
            },
            {
                "tag": "note",
                "elements": [
                    {"tag": "lark_md", "content": "审批经签名校验与审核人权限校验后生效；未授权自动发布时以人工发布交接为准。"}
                ],
            },
        ],
    }
    return card


def verify_callback(
    timestamp: str,
    nonce: str,
    body_raw: str,
    signature: str,
    body_json: Optional[Dict[str, Any]] = None,
) -> tuple[bool, str]:
    """飞书事件订阅回调签名 + 令牌双重校验。

    签名算法（飞书官方）：signature = base64(HMAC-SHA256(
        key=FEISHU_APP_SECRET, message=f"{timestamp}\\n{nonce}\\n{body_raw}"))
    令牌校验：body 内 token == FEISHU_VERIFICATION_TOKEN。

    返回 (ok, err_msg)。未配置密钥时返回 (False, 明确提示)。
    """
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    ver_token = os.environ.get("FEISHU_VERIFICATION_TOKEN", "").strip()
    if not app_secret or not ver_token:
        return False, (
            "飞书回调校验未配置：需设置 FEISHU_APP_SECRET 与 FEISHU_VERIFICATION_TOKEN"
            "（以及 FEISHU_ENCRYPT_KEY 用于加密场景）。配置前回调接口返回 503，不做审批。"
        )

    if not signature or not timestamp or not nonce:
        return False, "missing signature/timestamp/nonce headers"

    try:
        msg = f"{timestamp}\n{nonce}\n{body_raw}".encode("utf-8")
        expect = base64.b64encode(
            hmac.new(app_secret.encode("utf-8"), msg, hashlib.sha256).digest()
        ).decode("utf-8")
        if not hmac.compare_digest(expect, signature):
            return False, "signature mismatch"
    except Exception as exc:  # noqa: BLE001
        return False, f"signature verify error: {exc}"

    if body_json is None:
        try:
            body_json = _json.loads(body_raw) if body_raw else {}
        except Exception as exc:  # noqa: BLE001
            return False, f"invalid callback body: {exc}"
    body_token = (body_json or {}).get("token", "")
    if body_token and body_token != ver_token:
        return False, "verification token mismatch"
    if not body_token:
        return False, "missing verification token in body"
    return True, "ok"


def send_feishu_card(webhook_url: str, card: Dict[str, Any], timeout: int = 10) -> bool:
    """推一张交互卡片到飞书群机器人 webhook。

    与 youtube-kol-monitor 封装行为一致：任何异常/非 0 code 返回 False，不抛出。
    """
    if not webhook_url:
        return False
    try:
        import requests

        resp = requests.post(
            webhook_url,
            json={"msg_type": "interactive", "card": card},
            timeout=timeout,
        )
        data = resp.json() if resp.content else {}
        return resp.status_code == 200 and data.get("code") == 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("send_feishu_card failed: %s", exc)
        return False


def push_review_cards(
    cards: List[Dict[str, Any]],
    webhook_url: Optional[str] = None,
    dry_run: bool = False,
    limit: int = 5,
) -> Dict[str, Any]:
    """批量推送审核卡片。

    dry_run=True 时不发真实请求，返回卡片 JSON 供测试/人工预览。
    """
    if not webhook_url and not dry_run:
        return {"ok": False, "error": "FEISHU_WEBHOOK_URL not configured"}
    sent = 0
    results = []
    for card in cards[:limit]:
        if dry_run:
            results.append({"dry_run": True, "card": card})
            sent += 1
            continue
        ok = send_feishu_card(webhook_url, card)
        results.append({"sent": ok})
        if ok:
            sent += 1
        time.sleep(0.2)
    return {
        "ok": True,
        "dry_run": dry_run,
        "sent": sent,
        "total_cards": len(cards[:limit]),
        "results": results,
    }
