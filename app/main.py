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
import json
import logging
import os
from typing import Any, Dict, List, Optional

from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel

from analyst.analyzer import analyze_batch, build_weekly_context
from analyst.mention_classifier import classify_comment
from analyst.reply_engine import generate_draft_reply
from . import aggregate
from . import db
from . import feishu_review
from . import reply_store
from .trends import classify_signal

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """启动时确保 schema 就绪（幂等迁移：新增列/新表对线上库自动补齐）。

    历史教训（v0.2.10）：API 服务此前不执行 init_schema，schema.sql 对
    已存在的表不会自动补列（CREATE TABLE IF NOT EXISTS 仅建新表），
    导致 reply_drafts 新增 reason 列后 insert_draft 直接 500。
    """
    try:
        from collector.storage import connect, init_schema

        path = db.get_database_path()
        conn = connect(path)
        schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sql", "schema.sql")
        if not os.path.exists(schema_path):
            schema_path = "sql/schema.sql"
        init_schema(conn, schema_path)
        conn.close()
        logger.info("Schema ensured at startup (migrations applied)")
    except Exception as exc:  # noqa: BLE001
        # 不阻断启动：大多数读接口不依赖新列；写接口在列缺失时会返回明确错误
        logger.error("startup schema init failed: %s", exc)
    yield


app = FastAPI(title="Reddit Grinder Intelligence API", version="0.1.0", lifespan=_lifespan)

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
            "geimori_mention_posts": context.get("geimori_mention_posts", []),
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


# ------------------------------------------------------------
# Phase 2：飞书审批闭环
# 状态机：draft_ready → approved/rejected/skipped → published
# 并发锁：仅 draft_ready 可被审批；重复操作返回 conflict
# 回调：签名（HMAC-SHA256）+ verification token 双重校验
# 红线：未授权不发布；仅卡片链接不视为审批
# ------------------------------------------------------------

_ALLOWED_APPROVAL_ACTIONS = ("approve", "edit_approve", "regenerate", "reject", "skip")


class ApproveRequest(BaseModel):
    task_id: int
    action: str
    draft_version: int = 1
    reviewer_id: str = ""
    reviewer_name: Optional[str] = None
    approved_text: Optional[str] = None


@app.post("/api/reddit/reply-approve")
def reply_approve(
    req: ApproveRequest,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
) -> Dict[str, Any]:
    """执行一次审批动作（人工/Coze 调用，带鉴权）。

    approve:     批准当前草稿，锁定 approved_text = draft_text
    edit_approve:批准修改后的文本（approved_text 必填）
    regenerate:  记录审计，调用方随后可重新生成草稿（新版本回 draft_ready）
    reject/skip: 拒绝/跳过，任务进入终态
    """
    _require_key(x_api_key)
    if req.action not in _ALLOWED_APPROVAL_ACTIONS:
        raise HTTPException(status_code=422, detail=f"invalid action: {req.action}")
    if not req.reviewer_id:
        raise HTTPException(status_code=422, detail="reviewer_id is required")
    conn = db.get_conn()
    try:
        result = reply_store.apply_approval(
            conn,
            task_id=req.task_id,
            draft_version=req.draft_version,
            action=req.action,
            reviewer_id=req.reviewer_id,
            reviewer_name=req.reviewer_name,
            approved_text=req.approved_text,
        )
        if not result["ok"]:
            status_code = 409 if result.get("conflict") else 422
            raise HTTPException(status_code=status_code, detail=result.get("error"))
        return result
    finally:
        db.put_conn(conn)


@app.post("/api/geimori/review-push")
def review_push(
    status: str = Query("draft_ready", description="task status to push"),
    limit: int = Query(5, ge=1, le=20),
    dry_run: bool = Query(False, description="dry_run 不发送真实卡片"),
    webhook_url: Optional[str] = Query(None, description="覆盖 FEISHU_WEBHOOK_URL"),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
) -> Dict[str, Any]:
    """把待审核队列渲染为飞书交互卡片并推送（群机器人 webhook）。

    dry_run=True 时不发真实请求，返回卡片 JSON 供人工预览/测试。
    """
    _require_key(x_api_key)
    conn = db.get_conn()
    try:
        tasks = reply_store.queue_tasks(conn, status=status, limit=limit)
        cards = []
        for t in tasks:
            draft = reply_store.latest_draft(conn, t["task_id"])
            cards.append(feishu_review.build_review_card(t, draft))
        if not cards:
            return {"ok": True, "count": 0, "message": "queue empty"}
        url = webhook_url or os.environ.get("FEISHU_WEBHOOK_URL", "")
        result = feishu_review.push_review_cards(cards, webhook_url=url, dry_run=dry_run)
        result["count"] = len(cards)
        return result
    finally:
        db.put_conn(conn)


@app.post("/api/feishu/reply-callback")
async def feishu_reply_callback(
    request: Request,
) -> Dict[str, Any]:
    """飞书事件订阅回调（卡片按钮 action 触发）。

    校验顺序：
      1. 签名校验（X-Lark-Signature / X-Lark-Request-Timestamp / X-Lark-Request-Nonce）
      2. verification token 校验（body.token）
      3. 解析 card.action.value → 执行审批（task_id/action/draft_version）
      4. 校验通过才调用 apply_approval；校验失败一律 403/503，不做审批

    未配置 FEISHU_APP_SECRET/FEISHU_VERIFICATION_TOKEN 时返回 503 明确提示。
    """
    signature = request.headers.get("X-Lark-Signature", "")
    ts = request.headers.get("X-Lark-Request-Timestamp", "")
    nonce = request.headers.get("X-Lark-Request-Nonce", "")
    body_raw = (await request.body()).decode("utf-8", errors="replace")
    try:
        body = json.loads(body_raw) if body_raw else {}
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="invalid JSON body")

    ok, err = feishu_review.verify_callback(ts, nonce, body_raw, signature, body)
    if not ok:
        if "未配置" in err:
            raise HTTPException(status_code=503, detail=err)
        raise HTTPException(status_code=403, detail=err)

    # URL 校验挑战（飞书事件订阅首次配置）
    if body.get("type") == "url_verification" or body.get("challenge"):
        return {"challenge": body.get("challenge")}

    # 解析卡片 action
    header = body.get("header") or {}
    event = body.get("event") or {}
    action = event.get("action") or {}
    value = action.get("value") or {}
    task_id = value.get("task_id")
    act = value.get("action")
    draft_version = int(value.get("draft_version") or 1)
    if not task_id or act not in _ALLOWED_APPROVAL_ACTIONS:
        raise HTTPException(status_code=422, detail="invalid card action value")

    reviewer_id = header.get("operator_id") or event.get("operator", {}).get("open_id") or ""
    reviewer_name = header.get("operator_name") or ""
    if not reviewer_id:
        raise HTTPException(status_code=403, detail="missing reviewer identity from callback")

    conn = db.get_conn()
    try:
        result = reply_store.apply_approval(
            conn,
            task_id=int(task_id),
            draft_version=draft_version,
            action=act,
            reviewer_id=reviewer_id,
            reviewer_name=reviewer_name,
        )
        if not result["ok"]:
            status_code = 409 if result.get("conflict") else 422
            raise HTTPException(status_code=status_code, detail=result.get("error"))
        return {"ok": True, "task_id": int(task_id), "action": act,
                "task_status": result["task"]["status"]}
    finally:
        db.put_conn(conn)


@app.get("/api/geimori/audit-log")
def audit_log(
    limit: int = Query(50, ge=1, le=200),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
) -> Dict[str, Any]:
    """审核日志（审批 + 发布记录）。"""
    _require_key(x_api_key)
    conn = db.get_conn()
    try:
        rows = reply_store.audit_log(conn, limit=limit)
        return {"count": len(rows), "log": rows}
    finally:
        db.put_conn(conn)


class ManualPublishRequest(BaseModel):
    task_id: int
    operator_id: str
    operator_name: Optional[str] = None
    reddit_account: Optional[str] = None
    reddit_reply_id: Optional[str] = None
    published_url: Optional[str] = None
    note: Optional[str] = None


@app.post("/api/geimori/manual-publish")
def manual_publish(
    req: ManualPublishRequest,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
) -> Dict[str, Any]:
    """人工发布交接（Phase 2：无自动发帖授权时的最终交付方式）。

    仅 approved 任务可发布；已 published 拒绝重复；final_text 使用锁定的批准文本。
    """
    _require_key(x_api_key)
    if not req.operator_id:
        raise HTTPException(status_code=422, detail="operator_id is required")
    conn = db.get_conn()
    try:
        result = reply_store.manual_publish(
            conn,
            task_id=req.task_id,
            operator_id=req.operator_id,
            operator_name=req.operator_name,
            reddit_account=req.reddit_account,
            reddit_reply_id=req.reddit_reply_id,
            published_url=req.published_url,
            note=req.note,
        )
        if not result["ok"]:
            status_code = 409 if "already published" in result.get("error", "") else 422
            raise HTTPException(status_code=status_code, detail=result.get("error"))
        return result
    finally:
        db.put_conn(conn)
