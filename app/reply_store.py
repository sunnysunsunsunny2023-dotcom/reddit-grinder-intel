"""回复任务存储（Geimori Reply System V2.0 — Phase 1）。

reply_tasks / reply_drafts 的幂等创建与查询。
唯一约束：UNIQUE(platform, post_id, comment_id) 保证同一评论不重复建任务。
状态机：new → classified → draft_ready → pending_approval → approved/rejected/skipped → published
"""
from __future__ import annotations

import datetime as dt
import json as _json
import logging
import sqlite3
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

TASK_STATUSES = (
    "new", "classified", "draft_ready", "pending_approval",
    "approved", "rejected", "skipped", "published", "failed",
)


def _now_naive() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def find_task(
    conn: sqlite3.Connection,
    platform: str,
    post_id: str,
    comment_id: str,
) -> Optional[Dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT task_id, platform, subreddit, post_id, comment_id,
               original_text, context, permalink, product, category,
               priority, reply_needed, status, created_at, updated_at
          FROM reply_tasks
         WHERE platform = ? AND post_id = ? AND comment_id = ?
        """,
        (platform, post_id, comment_id),
    )
    row = cur.fetchone()
    if not row:
        return None
    cols = (
        "task_id", "platform", "subreddit", "post_id", "comment_id",
        "original_text", "context", "permalink", "product", "category",
        "priority", "reply_needed", "status", "created_at", "updated_at",
    )
    return dict(zip(cols, row))


def create_task(
    conn: sqlite3.Connection,
    platform: str,
    subreddit: Optional[str],
    post_id: str,
    comment_id: str,
    original_text: str,
    context: Optional[str] = None,
    permalink: Optional[str] = None,
    product: Optional[str] = None,
) -> Dict[str, Any]:
    """幂等创建 reply_task；已存在则直接返回现有任务。"""
    existing = find_task(conn, platform, post_id, comment_id)
    if existing:
        return existing

    cur = conn.execute(
        """
        INSERT INTO reply_tasks
            (platform, subreddit, post_id, comment_id, original_text,
             context, permalink, product, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new')
        ON CONFLICT (platform, post_id, comment_id) DO NOTHING
        """,
        (platform, subreddit, post_id, comment_id, original_text,
         context, permalink, product),
    )
    conn.commit()
    if cur.rowcount == 1:
        task_id = cur.lastrowid
        return {
            "task_id": task_id,
            "platform": platform,
            "subreddit": subreddit,
            "post_id": post_id,
            "comment_id": comment_id,
            "original_text": original_text,
            "context": context,
            "permalink": permalink,
            "product": product,
            "category": None,
            "priority": None,
            "reply_needed": 0,
            "status": "new",
            "created_at": _now_naive(),
            "updated_at": _now_naive(),
        }
    conn.rollback()
    return find_task(conn, platform, post_id, comment_id)  # type: ignore[return-value]


def update_task_classification(
    conn: sqlite3.Connection,
    task_id: int,
    classification: Dict[str, Any],
) -> None:
    conn.execute(
        """
        UPDATE reply_tasks
           SET category = ?, priority = ?, reply_needed = ?,
               product = COALESCE(?, product),
               status = 'classified', updated_at = ?
         WHERE task_id = ?
        """,
        (
            classification.get("category"),
            classification.get("priority"),
            1 if classification.get("reply_needed") else 0,
            classification.get("product") or None,
            _now_naive(),
            task_id,
        ),
    )
    conn.commit()


def mark_draft_ready(conn: sqlite3.Connection, task_id: int) -> None:
    conn.execute(
        "UPDATE reply_tasks SET status='draft_ready', updated_at=? WHERE task_id=?",
        (_now_naive(), task_id),
    )
    conn.commit()


def insert_draft(
    conn: sqlite3.Connection,
    task_id: int,
    draft_text: str,
    model_provider: str,
    model_name: str,
    brand_voice_version: str,
    knowledge_version: str,
    reason: Optional[str] = None,
) -> int:
    cur = conn.execute(
        """
        SELECT COALESCE(MAX(draft_version), 0) + 1
          FROM reply_drafts WHERE task_id = ?
        """,
        (task_id,),
    )
    draft_version = cur.fetchone()[0]
    cur = conn.execute(
        """
        INSERT INTO reply_drafts
            (task_id, draft_text, reason, model_provider, model_name,
             brand_voice_version, knowledge_version, draft_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (task_id, draft_text, reason, model_provider, model_name,
         brand_voice_version, knowledge_version, draft_version),
    )
    conn.commit()
    return draft_version


def latest_draft(
    conn: sqlite3.Connection, task_id: int
) -> Optional[Dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT draft_id, task_id, draft_text, reason, model_provider, model_name,
               brand_voice_version, knowledge_version, draft_version, created_at
          FROM reply_drafts
         WHERE task_id = ?
         ORDER BY draft_version DESC
         LIMIT 1
        """,
        (task_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    cols = (
        "draft_id", "task_id", "draft_text", "reason", "model_provider", "model_name",
        "brand_voice_version", "knowledge_version", "draft_version", "created_at",
    )
    return dict(zip(cols, row))


def queue_tasks(
    conn: sqlite3.Connection,
    status: str = "draft_ready",
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """获取指定状态的待处理任务（审核队列 Phase 2 用）。"""
    cur = conn.execute(
        """
        SELECT task_id, platform, subreddit, post_id, comment_id,
               original_text, context, permalink, product, category,
               priority, reply_needed, status, created_at, updated_at
          FROM reply_tasks
         WHERE status = ?
         ORDER BY CASE priority WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END,
                  created_at ASC
         LIMIT ?
        """,
        (status, limit),
    )
    cols = (
        "task_id", "platform", "subreddit", "post_id", "comment_id",
        "original_text", "context", "permalink", "product", "category",
        "priority", "reply_needed", "status", "created_at", "updated_at",
    )
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    """回复系统统计（Dashboard 用，需求文档 十一）。"""
    cur = conn.execute(
        """
        SELECT status, COUNT(*) FROM reply_tasks GROUP BY status
        """
    )
    by_status = {r[0]: r[1] for r in cur.fetchall()}
    cur = conn.execute(
        "SELECT COUNT(*) FROM reply_tasks WHERE priority = 'P1'"
    )
    p1 = cur.fetchone()[0]
    cur = conn.execute("SELECT COUNT(*) FROM reply_drafts")
    drafts = cur.fetchone()[0]
    cur = conn.execute("SELECT COUNT(*) FROM reply_publications WHERE status = 'published'")
    published = cur.fetchone()[0]
    return {
        "total_tasks": sum(by_status.values()),
        "by_status": by_status,
        "p1_complaints": p1,
        "drafts_generated": drafts,
        "published": published,
    }


# ------------------------------------------------------------
# Phase 2：审批状态机 / 并发锁 / 审计 / 人工发布交接
# 状态机：draft_ready --approve/edit_approve--> approved
#         draft_ready --reject--> rejected
#         draft_ready --skip--> skipped
#         draft_ready --regenerate--> 新草稿版本 -> draft_ready
#         approved --(人工/授权发布)--> published
# 并发：仅 draft_ready 可被审批；条件 UPDATE rowcount=0 说明已被他人处理（409）
# 安全：批准文本锁定；已批准后修改必须重新审批（regenerate 新版本）
# ------------------------------------------------------------


def get_task_by_id(conn: sqlite3.Connection, task_id: int) -> Optional[Dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT task_id, platform, subreddit, post_id, comment_id,
               original_text, context, permalink, product, category,
               priority, reply_needed, status, created_at, updated_at
          FROM reply_tasks
         WHERE task_id = ?
        """,
        (task_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    cols = (
        "task_id", "platform", "subreddit", "post_id", "comment_id",
        "original_text", "context", "permalink", "product", "category",
        "priority", "reply_needed", "status", "created_at", "updated_at",
    )
    return dict(zip(cols, row))


def _set_task_status(conn: sqlite3.Connection, task_id: int, status: str) -> int:
    """条件更新状态；返回受影响行数（0=并发冲突或状态不允许）。"""
    cur = conn.execute(
        "UPDATE reply_tasks SET status=?, updated_at=? WHERE task_id=? AND status='draft_ready'",
        (status, _now_naive(), task_id),
    )
    conn.commit()
    return cur.rowcount


def insert_approval(
    conn: sqlite3.Connection,
    task_id: int,
    draft_version: int,
    action: str,
    reviewer_id: str,
    reviewer_name: Optional[str] = None,
    approved_text: Optional[str] = None,
) -> int:
    """记录一次审批（对 draft_version 的审批）。

    同一 task 同一 action 的旧记录置 superseded（regenerate/re-approve 场景）。
    """
    cur = conn.execute(
        """
        INSERT INTO reply_approvals
            (task_id, draft_version, reviewer_id, reviewer_name,
             action, approved_text, status)
        VALUES (?, ?, ?, ?, ?, ?, 'effective')
        """,
        (task_id, draft_version, reviewer_id, reviewer_name, action, approved_text),
    )
    conn.commit()
    return cur.lastrowid


def supersede_approvals(conn: sqlite3.Connection, task_id: int) -> None:
    conn.execute(
        "UPDATE reply_approvals SET status='superseded' WHERE task_id=? AND status='effective'",
        (task_id,),
    )
    conn.commit()


def effective_approval(
    conn: sqlite3.Connection, task_id: int
) -> Optional[Dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT approval_id, task_id, draft_version, reviewer_id, reviewer_name,
               action, approved_text, status, approved_at, created_at
          FROM reply_approvals
         WHERE task_id = ? AND status = 'effective'
         ORDER BY approval_id DESC
         LIMIT 1
        """,
        (task_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    cols = (
        "approval_id", "task_id", "draft_version", "reviewer_id", "reviewer_name",
        "action", "approved_text", "status", "approved_at", "created_at",
    )
    return dict(zip(cols, row))


def apply_approval(
    conn: sqlite3.Connection,
    task_id: int,
    draft_version: int,
    action: str,
    reviewer_id: str,
    reviewer_name: Optional[str] = None,
    approved_text: Optional[str] = None,
) -> Dict[str, Any]:
    """执行一次审批动作（带并发锁）。

    Args:
        action: approve | edit_approve | reject | skip | regenerate
                （regenerate 只落审计，不改变 approved 文本）

    Returns:
        {"ok": True, "task": ..., "approval": ..., "conflict": False}
        并发冲突时 {"ok": False, "conflict": True, "task": ..., "error": ...}
    """
    task = get_task_by_id(conn, task_id)
    if not task:
        return {"ok": False, "conflict": False, "error": "task not found"}

    if action == "approve":
        draft = latest_draft(conn, task_id)
        if not draft or draft["draft_version"] != draft_version:
            return {
                "ok": False, "conflict": False, "task": task,
                "error": f"draft_version mismatch: latest={draft['draft_version'] if draft else None}",
            }
        if _set_task_status(conn, task_id, "approved") != 1:
            return {"ok": False, "conflict": True, "task": get_task_by_id(conn, task_id),
                    "error": "task no longer in draft_ready (handled by someone else)"}
        insert_approval(conn, task_id, draft_version, "approve", reviewer_id,
                        reviewer_name, approved_text=draft["draft_text"])
        task = get_task_by_id(conn, task_id)
        return {"ok": True, "task": task,
                "approval": effective_approval(conn, task_id)}

    if action == "edit_approve":
        draft = latest_draft(conn, task_id)
        if not draft or draft["draft_version"] != draft_version:
            return {
                "ok": False, "conflict": False, "task": task,
                "error": f"draft_version mismatch: latest={draft['draft_version'] if draft else None}",
            }
        if not approved_text or not approved_text.strip():
            return {"ok": False, "conflict": False, "task": task,
                    "error": "approved_text required for edit_approve"}
        if _set_task_status(conn, task_id, "approved") != 1:
            return {"ok": False, "conflict": True, "task": get_task_by_id(conn, task_id),
                    "error": "task no longer in draft_ready"}
        insert_approval(conn, task_id, draft_version, "edit_approve", reviewer_id,
                        reviewer_name, approved_text=approved_text.strip())
        task = get_task_by_id(conn, task_id)
        return {"ok": True, "task": task,
                "approval": effective_approval(conn, task_id)}

    if action in ("reject", "skip"):
        target = "rejected" if action == "reject" else "skipped"
        if _set_task_status(conn, task_id, target) != 1:
            return {"ok": False, "conflict": True, "task": get_task_by_id(conn, task_id),
                    "error": "task no longer in draft_ready"}
        insert_approval(conn, task_id, draft_version, action, reviewer_id, reviewer_name)
        task = get_task_by_id(conn, task_id)
        return {"ok": True, "task": task,
                "approval": effective_approval(conn, task_id)}

    if action == "regenerate":
        # 只记录审计，不改变状态；调用方生成新草稿后任务回 draft_ready
        insert_approval(conn, task_id, draft_version, "regenerate", reviewer_id,
                        reviewer_name)
        return {"ok": True, "task": get_task_by_id(conn, task_id),
                "approval": effective_approval(conn, task_id)}

    return {"ok": False, "conflict": False, "task": task,
            "error": f"unknown action: {action}"}


def manual_publish(
    conn: sqlite3.Connection,
    task_id: int,
    operator_id: str,
    operator_name: Optional[str] = None,
    reddit_account: Optional[str] = None,
    reddit_reply_id: Optional[str] = None,
    published_url: Optional[str] = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """人工发布交接（Phase 2 无授权时的最终交付方式）。

    仅允许 approved 状态的任务进入发布；防止重复发布：
    若已有 published 记录，返回 conflict 且不重复。
    """
    task = get_task_by_id(conn, task_id)
    if not task:
        return {"ok": False, "error": "task not found"}
    existing = conn.execute(
        "SELECT COUNT(*) FROM reply_publications WHERE task_id=? AND status='published'",
        (task_id,),
    ).fetchone()[0]
    if existing:
        return {"ok": False, "task": task, "error": "already published"}
    if task["status"] != "approved":
        return {"ok": False, "task": task,
                "error": f"task status is {task['status']}, expected approved"}

    approval = effective_approval(conn, task_id)
    final_text = approval["approved_text"] if approval else None
    cur = conn.execute(
        """
        INSERT INTO reply_publications
            (task_id, approved_draft_version, reddit_account, target_comment_id,
             reddit_reply_id, published_text, published_url, status, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            approval["draft_version"] if approval else 0,
            reddit_account,
            task["comment_id"],
            reddit_reply_id,
            final_text,
            published_url,
            "published",
            note,
        ),
    )
    conn.execute(
        "UPDATE reply_tasks SET status='published', updated_at=? WHERE task_id=?",
        (_now_naive(), task_id),
    )
    conn.commit()
    return {
        "ok": True,
        "publication_id": cur.lastrowid,
        "task": get_task_by_id(conn, task_id),
        "final_text": final_text,
    }


def audit_log(conn: sqlite3.Connection, limit: int = 50) -> List[Dict[str, Any]]:
    """审核日志（含审批+发布，Dashboard/审计用）。"""
    cur = conn.execute(
        """
        SELECT a.approval_id, a.task_id, a.draft_version, a.reviewer_id,
               a.reviewer_name, a.action, a.status, a.approved_at,
               t.post_id, t.subreddit, t.priority, t.original_text
          FROM reply_approvals a
          JOIN reply_tasks t ON t.task_id = a.task_id
         ORDER BY a.approval_id DESC
         LIMIT ?
        """,
        (limit,),
    )
    cols = ("approval_id", "task_id", "draft_version", "reviewer_id",
            "reviewer_name", "action", "status", "approved_at",
            "post_id", "subreddit", "priority", "original_text")
    return [dict(zip(cols, row)) for row in cur.fetchall()]
