"""Reddit Grinder Intelligence — FastAPI 服务（阿里云 8086）。

接口：
- GET  /healthz                          健康检查
- GET  /api/reddit/analysis-batch        取分析批次（确定性统计 + 可选 LLM 分析）
- POST /api/reddit/analysis-complete     回写批次完成状态（避免重复分析）

鉴权：X-API-Key（与 8080/8081 模式一致，从环境变量 REDDIT_API_KEY 读取）。
ADR-001：LLM 分析（DeepSeek）在本服务内执行，Coze 只渲染与推送。
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

from analyst.analyzer import analyze_batch, build_weekly_context
from analyst.mention_classifier import classify_comment
from analyst.reply_engine import generate_draft_reply
from . import aggregate
from . import db
from . import reply_store
from .trends import classify_signal

logger = logging.getLogger(__name__)

app = FastAPI(title="Reddit Grinder Intelligence API", version="0.1.0")

# ------------------------------------------------------------
# 鉴权
# ------------------------------------------------------------
def _require_key(x_api_key: Optional[str]) -> None:
    expected = os.environ.get("REDDIT_API_KEY", "")
    if not expected:
        return
    if not x_api_key or x_api_key != expected:
        raise HTTPException(status_code=401, detail="invalid x-api-key")


# ------------------------------------------------------------
# 周期规范化：同一窗口幂等（同 report_type+period_start+version 一个 batch）
# ------------------------------------------------------------
def _floor_to_hour(t: dt.datetime) -> dt.datetime:
    return t.replace(minute=0, second=0, microsecond=0)


def _resolve_period(
    report_type: str,
    hours: Optional[int],
    start: Optional[dt.datetime],
    end: Optional[dt.datetime],
) -> tuple[dt.datetime, dt.datetime]:
    now = dt.datetime.now(dt.timezone.utc)
    if start is not None:
        return start, end or now
    h = hours if hours is not None else (168 if report_type == "weekly" else 24)
    return _floor_to_hour(now - dt.timedelta(hours=h)), now


# ------------------------------------------------------------
# 批次创建 / 查询
# ------------------------------------------------------------
def _find_or_create_batch(
    conn,
    report_type: str,
    period_start: dt.datetime,
    period_end: dt.datetime,
    analysis_version: str,
    post_count: int,
) -> Dict[str, Any]:
    """按 (report_type, period_start, analysis_version) 幂等查找或创建批次。"""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT batch_id, report_type, period_start, period_end,
               post_count, analysis_version, status, created_at, completed_at
          FROM analysis_batches
         WHERE report_type = ? AND period_start = ? AND analysis_version = ?
        """,
        (report_type, period_start, analysis_version),
    )
    row = cur.fetchone()
    if row:
        cols = (
            "batch_id", "report_type", "period_start", "period_end",
            "post_count", "analysis_version", "status", "created_at",
            "completed_at",
        )
        return dict(zip(cols, row))

    cur.execute(
        """
        INSERT INTO analysis_batches
            (report_type, period_start, period_end, post_count,
             analysis_version, status)
        VALUES (?, ?, ?, ?, ?, 'pending')
        ON CONFLICT (report_type, period_start, analysis_version)
        DO NOTHING
        """,
        (report_type, period_start, period_end, post_count, analysis_version),
    )
    if cur.rowcount == 1:
        conn.commit()
        return {
            "batch_id": cur.lastrowid,
            "report_type": report_type,
            "period_start": period_start,
            "period_end": period_end,
            "post_count": post_count,
            "analysis_version": analysis_version,
            "status": "pending",
            "created_at": None,
            "completed_at": None,
        }
    # 并发插入冲突：重查
    conn.rollback()
    cur.execute(
        """
        SELECT batch_id, report_type, period_start, period_end,
               post_count, analysis_version, status, created_at, completed_at
          FROM analysis_batches
         WHERE report_type = ? AND period_start = ? AND analysis_version = ?
        """,
        (report_type, period_start, analysis_version),
    )
    cols = (
        "batch_id", "report_type", "period_start", "period_end",
        "post_count", "analysis_version", "status", "created_at", "completed_at",
    )
    return dict(zip(cols, cur.fetchone()))


def _get_analysis_result(conn, batch_id: int) -> Optional[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(
        "SELECT result_json FROM analysis_results WHERE batch_id = ?",
        (batch_id,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _mark_batch_failed(conn, batch_id: int, error: str) -> None:
    cur = conn.cursor()
    cur.execute(
        "UPDATE analysis_batches SET status='failed' WHERE batch_id=?",
        (batch_id,),
    )
    conn.commit()
    logger.error("Batch %s marked failed: %s", batch_id, error)


# ------------------------------------------------------------
# API
# ------------------------------------------------------------
@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/health")
def health() -> Dict[str, str]:
    """CI Health check 使用 /health（与 /healthz 等价）。"""
    return healthz()


@app.get("/api/reddit/analysis-batch")
def analysis_batch(
    report_type: str = Query(..., pattern="^(daily|weekly)$"),
    hours: Optional[int] = Query(None, ge=1, le=24 * 31),
    start: Optional[dt.datetime] = Query(None),
    end: Optional[dt.datetime] = Query(None),
    subreddit: Optional[str] = Query(None),
    limit: Optional[int] = Query(None, ge=1, le=500),
    analysis_version: str = Query("v1"),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
) -> Dict[str, Any]:
    """取分析批次：确定性统计 +（如已配置 DeepSeek）LLM 分析结果。"""
    _require_key(x_api_key)
    conn = db.get_conn()
    try:
        period_start, period_end = _resolve_period(report_type, hours, start, end)

        if report_type == "weekly":
            context = build_weekly_context(conn, subreddit=subreddit)
        else:
            context = aggregate.build_daily_context(
                conn,
                subreddit=subreddit,
                start=period_start,
                end=period_end,
            )

        if limit:
            context["posts"] = context["posts"][:limit]

        batch = _find_or_create_batch(
            conn,
            report_type,
            period_start,
            period_end,
            analysis_version,
            post_count=len(context["posts"]),
        )
        batch_id = batch["batch_id"]

        result = _get_analysis_result(conn, batch_id)
        if result is None and batch["status"] in ("pending", "running", "failed"):
            # 同步执行阿里云侧 DeepSeek 分析（ADR-001）
            try:
                context["period_start"] = period_start
                context["period_end"] = period_end
                result = analyze_batch(conn, batch_id, report_type, context)
            except Exception as exc:  # noqa: BLE001
                _mark_batch_failed(conn, batch_id, str(exc))
                raise HTTPException(status_code=502, detail=f"analysis failed: {exc}")

        return {
            "batch_id": str(batch_id),
            "period": {
                "start": period_start.isoformat(),
                "end": period_end.isoformat(),
            },
            "posts": context.get("posts", []),
            "statistics": context.get("statistics", {}),
            "baseline_7d": context.get("baseline_7d", {}),
            "baseline_30d": context.get("baseline_30d", {}),
            "topic_trends": context.get("topic_trends", {}),
            "brand_trends": context.get("brand_trends", {}),
            "competitor_trends": context.get("competitor_trends", {}),
            "analysis": result,
        }
    finally:
        db.put_conn(conn)


class AnalysisCompleteRequest(BaseModel):
    batch_id: int
    analysis_version: str
    status: str  # completed / failed


@app.post("/api/reddit/analysis-complete")
def analysis_complete(
    req: AnalysisCompleteRequest,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
) -> Dict[str, str]:
    """回写批次完成状态（spec 第 4 节，避免重复分析）。

    本实现中分析默认在 analysis-batch 内同步完成；此接口保留兼容：
    外部分析完成后回写时也会被 analysis-batch 复用（不再重复分析）。
    """
    _require_key(x_api_key)
    conn = db.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE analysis_batches
               SET status = ?,
                   completed_at = CASE WHEN ? = 'completed'
                                       THEN COALESCE(completed_at, CURRENT_TIMESTAMP)
                                       ELSE completed_at END
             WHERE batch_id = ? AND analysis_version = ?
            """,
            (req.status, req.status, req.batch_id, req.analysis_version),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="batch not found")
        conn.commit()
        return {"batch_id": str(req.batch_id), "status": req.status}
    finally:
        db.put_conn(conn)


# ------------------------------------------------------------
# Geimori Reply System — Phase 1：draft-reply / 审核队列 / 统计
# （需求文档 五、模块三；四张 reply 表见 sql/schema.sql）
# 仅生成草稿，绝不自动发布（发布在 Phase 4，默认关闭）
# ------------------------------------------------------------
class DraftReplyRequest(BaseModel):
    platform: str = "reddit"
    subreddit: Optional[str] = None
    post_id: str
    comment_id: str
    comment: str
    context: Optional[str] = None
    permalink: Optional[str] = None
    product: Optional[str] = None
    dry_run: bool = False  # dry_run 时不落库（模拟数据测试）


@app.post("/api/geimori/draft-reply")
def draft_reply(
    req: DraftReplyRequest,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
) -> Dict[str, Any]:
    """为一条评论生成品牌草稿回复（幂等：同一评论只建一个 task）。

    流程：
      1. 校验评论非空
      2. 幂等创建 reply_task（UNIQUE platform+post_id+comment_id）
      3. 分类（reply_needed + priority）
      4. 若需回复 → 生成草稿（Brand Voice）→ 落 reply_drafts
      5. 返回草稿 + 元数据（requires_human_review 恒为 True）
    """
    _require_key(x_api_key)
    if not req.comment or not req.comment.strip():
        raise HTTPException(status_code=422, detail="comment is required")
    conn = db.get_conn()
    try:
        if req.dry_run:
            cls = classify_comment(req.comment, context=req.context or "", product=req.product or "")
            if not cls.get("reply_needed"):
                return {
                    "dry_run": True, "reply_needed": False,
                    "priority": cls.get("priority", "P3"),
                    "category": cls.get("category", "brand_mention"),
                    "draft_reply": "", "requires_human_review": True,
                }
            draft = generate_draft_reply(
                req.comment, context=req.context or "",
                product=req.product or "", classification=cls,
            )
            return {
                "dry_run": True, "reply_needed": True,
                "priority": cls.get("priority", "P2"),
                "category": cls.get("category", "technical_question"),
                "draft_reply": draft["draft_reply"],
                "requires_human_review": True,
                "classification": cls,
            }

        task = reply_store.create_task(
            conn,
            platform=req.platform,
            subreddit=req.subreddit,
            post_id=req.post_id,
            comment_id=req.comment_id,
            original_text=req.comment,
            context=req.context,
            permalink=req.permalink,
            product=req.product,
        )
        task_id = task["task_id"]

        # 幂等：已有草稿且状态处于已生成/审核阶段时直接返回，不重复创建
        existing_draft = reply_store.latest_draft(conn, task_id)
        if existing_draft and task.get("status") in (
            "draft_ready", "pending_approval",
        ):
            return {
                "task_id": task_id,
                "reply_needed": True,
                "priority": task.get("priority", "P2"),
                "category": task.get("category", "technical_question"),
                "draft_reply": existing_draft["draft_text"],
                "draft_version": existing_draft["draft_version"],
                "brand_voice_version": existing_draft["brand_voice_version"],
                "knowledge_version": existing_draft["knowledge_version"],
                "requires_human_review": True,
                "status": task.get("status"),
            }

        cls = classify_comment(
            req.comment, context=req.context or "", product=req.product or ""
        )
        reply_store.update_task_classification(conn, task_id, cls)

        if not cls.get("reply_needed"):
            return {
                "task_id": task_id,
                "reply_needed": False,
                "priority": cls.get("priority", "P3"),
                "category": cls.get("category", "brand_mention"),
                "draft_reply": "",
                "requires_human_review": True,
                "status": task.get("status") or "classified",
            }

        draft = generate_draft_reply(
            req.comment, context=req.context or "",
            product=req.product or "", classification=cls,
        )
        draft_version = reply_store.insert_draft(
            conn, task_id, draft["draft_reply"],
            draft["model_provider"], draft["model_name"],
            draft["brand_voice_version"], draft["knowledge_version"],
        )
        reply_store.mark_draft_ready(conn, task_id)

        return {
            "task_id": task_id,
            "reply_needed": True,
            "priority": cls.get("priority", "P2"),
            "category": cls.get("category", "technical_question"),
            "draft_reply": draft["draft_reply"],
            "draft_version": draft_version,
            "brand_voice_version": draft["brand_voice_version"],
            "knowledge_version": draft["knowledge_version"],
            "requires_human_review": True,
            "status": "draft_ready",
            "classification": cls,
        }
    finally:
        db.put_conn(conn)


@app.get("/api/geimori/reply-queue")
def reply_queue(
    status: str = Query("draft_ready", description="task status"),
    limit: int = Query(50, ge=1, le=200),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
) -> Dict[str, Any]:
    """获取待审核任务队列（Phase 2 飞书审批消费）。"""
    _require_key(x_api_key)
    if status not in reply_store.TASK_STATUSES:
        raise HTTPException(status_code=422, detail=f"invalid status: {status}")
    conn = db.get_conn()
    try:
        tasks = reply_store.queue_tasks(conn, status=status, limit=limit)
        out = []
        for t in tasks:
            draft = reply_store.latest_draft(conn, t["task_id"])
            out.append({"task": t, "latest_draft": draft})
        return {"status": status, "count": len(out), "tasks": out}
    finally:
        db.put_conn(conn)


@app.get("/api/geimori/reply-stats")
def reply_stats(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
) -> Dict[str, Any]:
    """回复系统统计（Dashboard 用）。"""
    _require_key(x_api_key)
    conn = db.get_conn()
    try:
        return reply_store.stats(conn)
    finally:
        db.put_conn(conn)
