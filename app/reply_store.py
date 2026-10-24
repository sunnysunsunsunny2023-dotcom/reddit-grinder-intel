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
            (task_id, draft_text, model_provider, model_name,
             brand_voice_version, knowledge_version, draft_version)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (task_id, draft_text, model_provider, model_name,
         brand_voice_version, knowledge_version, draft_version),
    )
    conn.commit()
    return draft_version


def latest_draft(
    conn: sqlite3.Connection, task_id: int
) -> Optional[Dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT draft_id, task_id, draft_text, model_provider, model_name,
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
        "draft_id", "task_id", "draft_text", "model_provider", "model_name",
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
