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
from . import aggregate
from . import db
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
        RETURNING batch_id
        """,
        (report_type, period_start, period_end, post_count, analysis_version),
    )
    row = cur.fetchone()
    if row:
        conn.commit()
        return {
            "batch_id": row[0],
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
            context = aggregate.build_daily_context(conn, subreddit=subreddit)

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
