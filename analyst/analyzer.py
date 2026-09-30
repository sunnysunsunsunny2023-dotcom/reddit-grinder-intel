"""阿里云侧 LLM 分析模块（ADR-001：LLM 由阿里云调用 DeepSeek）。

职责：
- 根据确定性统计 + 原始帖子构造 prompt（spec 5 / 6 输出结构）
- 调用 DeepSeek 生成分析 JSON（只做分类/摘要/提炼/解释，不做计算）
- 结果写入 analysis_results，batch 标记 completed

LLM 输出纪律（spec 9）：
- 可以：分类、摘要、提炼痛点、识别语义、提炼用户原话、解释趋势、
        生成 marketing/content hypothesis
- 不可以：自己计算百分比/WoW/权重、把 1-2 帖说成市场、
          mention share 说成 market share、推测写成事实
"""
from __future__ import annotations

import datetime as dt
import json as _json
import logging
import os
from typing import Any, Dict, List, Optional

from app import aggregate
from app import keywords
from . import deepseek_client

logger = logging.getLogger(__name__)

DEFAULT_ANALYSIS_VERSION = "v1"
# DeepSeek 可一次处理的帖子数上限（超出的只保留 Top 由聚合层先筛）
DAILY_POST_LIMIT = 60
WEEKLY_POST_LIMIT = 200

ANALYSIS_LIMITS = {
    "daily": DAILY_POST_LIMIT,
    "weekly": WEEKLY_POST_LIMIT,
}


def _posts_for_llm(posts: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """截断帖子文本并限制数量，控制 token 成本。"""
    out = []
    for p in posts[:limit]:
        out.append(
            {
                "post_id": p["post_id"],
                "subreddit": p["subreddit"],
                "title": p["title"],
                "selftext": (p.get("selftext") or "")[:800],
                "score": int(p.get("score") or 0),
                "comments": int(p.get("num_comments") or 0),
                "permalink": p["permalink"],
                "created_utc": (
                    p["created_utc"].isoformat() if p.get("created_utc") else None
                ),
            }
        )
    return out


def _context_md(context: Dict[str, Any]) -> str:
    """把确定性上下文格式化为 Markdown 给 LLM（数值全部来自阿里云）。"""
    stats = context["statistics"]
    lines = [
        "## 确定性统计（阿里云计算，禁止重新计算）",
        f"- total_new_posts: {stats['total_new_posts']}",
        f"- grinder_related_posts: {stats['grinder_related_posts']}",
        f"- grinder_ratio: {stats['grinder_ratio']}",
        f"- geimori_mentions: {stats['geimori_mentions']}",
        f"- competitor_mentions: {stats['competitor_mentions']}",
        f"- alert_count: {stats['alert_count']}",
        "",
        "## Signal Alerts（阿里云已判定）",
    ]
    if context.get("signal_alerts"):
        for a in context["signal_alerts"]:
            lines.append(
                f"- {a['topic']}: mentions_24h={a['mentions_24h']} "
                f"avg_7d={a['avg_7d']} avg_30d={a['avg_30d']} "
                f"vs_7d={a['vs_7d']} vs_30d={a['vs_30d']} "
                f"status={a['status']} confidence={a['confidence']} "
                f"evidence_count={a['evidence_count']}"
            )
    else:
        lines.append("- (无异常信号)")
    lines.append("")

    if context.get("topic_trends"):
        lines.append("## Topic trends（阿里云已判定）")
        for topic, t in context["topic_trends"].items():
            lines.append(
                f"- {topic}: mentions={t['mentions']} unique_posts={t['unique_posts']} "
                f"engagement={t['engagement']} avg_7d={t['avg_7d']} "
                f"avg_30d={t['avg_30d']} trend={t['trend']}"
            )
        lines.append("")

    if context.get("brand_trends"):
        lines.append(
            f"## Geimori mentions: {context['brand_trends'].get('geimori', {})}"
        )
        lines.append("")

    if context.get("competitor_trends"):
        lines.append("## Competitor mentions（mention share，非 market share）")
        for name, st in context["competitor_trends"].items():
            if st["mentions"]:
                lines.append(f"- {name}: {st}")
        lines.append("")

    if context.get("top_posts"):
        lines.append("## Top posts（按 engagement 排序）")
        for p in context["top_posts"]:
            lines.append(
                f"- {p['title']} | r/{p['subreddit']} | score={p['score']} "
                f"comments={p['comments']} | https://www.reddit.com{p['permalink']}"
            )
        lines.append("")
    return "\n".join(lines)


DAILY_SYSTEM_PROMPT = """你是 Reddit 磨豆机社区情报分析师，服务于 Geimori（Wirsh）品牌。

你的任务：根据给定的确定性统计和帖子列表，生成《Daily Pulse》日报 JSON。

【铁律 - LLM 输出纪律】
- 只能分类、摘要、提炼、解释，禁止自己计算百分比、WoW、权重、平均值。
- 所有数值必须直接引用输入中已给出的数值，不得重新计算。
- 不要根据 1-2 个帖子就得出"市场"级结论；样本小时明确标 weak signal。
- mention share 不等于 market share。
- 没有强信号时，Today Actions 允许输出 "No immediate action required today"。

【输出 JSON 结构】（必须严格符合）
{
  "today_in_one_sentence": "1-2 句话概括过去 24h 最重要变化（含证据帖数）",
  "kpi_summary": "用输入里的 KPI 数值原样引用",
  "signal_alerts": [
    {
      "topic": "主题名",
      "status": "spike|emerging|rising|stable|declining",
      "confidence": "high|medium|low",
      "evidence_count": 数值,
      "what_happened": "1-2 句解释（引用原帖）",
      "permalink": ["https://www.reddit.com/..."],
      "weak_signal": true/false
    }
  ],
  "top_posts": [
    {"post_id": "...", "title": "...", "why_it_matters": "..."}
  ],
  "geimori_watch": {
    "has_mention": true/false,
    "detail": "若有提及：产品/情感/主题/严重度/建议回应；若无：'No meaningful Geimori mention in the last 24 hours.'"
  },
  "today_actions": [
    {
      "action": "行动",
      "why": "理由（含证据）",
      "evidence_count": 数值,
      "confidence": "high|medium|low",
      "category": "Ad|Content|Product|Community"
    }
  ]
}

不要输出除 JSON 以外的任何内容。
"""

WEEKLY_SYSTEM_PROMPT = """你是 Reddit 磨豆机社区情报分析师，服务于 Geimori（Wirsh）品牌。

你的任务：根据给定的确定性统计和帖子列表，生成《Weekly Intelligence》周报 JSON。

【铁律 - LLM 输出纪律】
- 只能分类、摘要、提炼、解释，禁止自己计算百分比、WoW、权重、平均值。
- 所有数值必须直接引用输入中已给出的数值（如 mentions、previous_week、avg_30d、trend 状态）。
- 不要根据 1-2 个帖子就得出"市场"级结论；样本小时明确标 weak signal。
- mention share 不等于 market share，禁止说"市场份额"。
- Next Week Actions 最多 5 项，不要生成超过执行能力的任务。

【输出 JSON 结构】（必须严格符合）
{
  "executive_summary": [
    {"point": "最大上升趋势", "evidence": "..."},
    {"point": "最大下降趋势", "evidence": "..."},
    {"point": "最大竞品变化", "evidence": "..."},
    {"point": "Geimori 品牌变化", "evidence": "..."},
    {"point": "最重要的下周行动", "evidence": "..."}
  ],
  "demand_trends": [
    {
      "topic": "主题",
      "mentions": 引用输入数值,
      "previous_week": 引用输入数值,
      "wow_change_pct": 引用输入数值（如未给出则填 null 并解释）,
      "share_of_grinder_discussion": 引用输入数值,
      "avg_30d": 引用输入数值,
      "trend": "rising|stable|declining|spike|emerging|structural_priority",
      "confidence": "high|medium|low",
      "interpretation": "1-2 句解释，引用原帖"
    }
  ],
  "competitor_intelligence": [
    {
      "brand": "品牌/机型",
      "mentions": 数值,
      "sentiment": "positive|negative|mixed|neutral",
      "common_praise": "引用用户原话或概括",
      "common_complaints": "...",
      "high_engagement_threads": ["permalink"],
      "potential_opportunity": "...",
      "mention_share_note": "明确这是 Reddit 讨论份额，不是市场份额"
    }
  ],
  "geimori_voice": {
    "mentions": 数值,
    "sentiment": "...",
    "positive_themes": [],
    "negative_themes": [],
    "questions_objections": [],
    "comparison_targets": [],
    "actionable_response": "..."
  },
  "customer_priorities": [
    {
      "priority": "structural_priority|emerging_trend|temporary_spike|weak_signal",
      "topic": "主题",
      "priority_score": 引用输入数值（未给出则 null）,
      "interpretation": "LLM 只解释，不生成权重"
    }
  ],
  "next_week_actions": [
    {
      "action": "...",
      "evidence": "...",
      "business_reason": "...",
      "confidence": "high|medium|low",
      "urgency": "high|medium|low",
      "category": "Ads|Blog_SEO|Product|Community|Research"
    }
  ]
}

不要输出除 JSON 以外的任何内容。
"""


def _build_daily_prompt(context: Dict[str, Any]) -> List[Dict[str, str]]:
    posts_md = "\n".join(
        f"- [{p['title']}] (r/{p['subreddit']}, score={p['score']}, "
        f"comments={p['comments']}) {p['selftext'][:300]} "
        f"https://www.reddit.com{p['permalink']}"
        for p in _posts_for_llm(context["posts"], DAILY_POST_LIMIT)
    )
    user_msg = (
        "请生成 Daily Pulse 日报 JSON。\n\n"
        + _context_md(context)
        + "\n## 过去 24h 帖子列表\n"
        + (posts_md or "- (无帖子)")
    )
    return [
        {"role": "system", "content": DAILY_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]


def _build_weekly_prompt(context: Dict[str, Any]) -> List[Dict[str, str]]:
    posts_md = "\n".join(
        f"- [{p['title']}] (r/{p['subreddit']}, score={p['score']}, "
        f"comments={p['comments']}) {p['selftext'][:300]} "
        f"https://www.reddit.com{p['permalink']}"
        for p in _posts_for_llm(context["posts"], WEEKLY_POST_LIMIT)
    )
    user_msg = (
        "请生成 Weekly Intelligence 周报 JSON。\n\n"
        + _context_md(context)
        + "\n## 本周帖子列表\n"
        + (posts_md or "- (无帖子)")
    )
    return [
        {"role": "system", "content": WEEKLY_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]


def build_weekly_context(
    conn, subreddit: Optional[str] = None
) -> Dict[str, Any]:
    """组装周报确定性上下文（当前周 vs 上周 vs 30d baseline）。"""
    now = dt.datetime.now(dt.timezone.utc)
    week_start = now - dt.timedelta(days=7)
    prev_week_start = now - dt.timedelta(days=14)

    posts_week = aggregate.query_posts(conn, week_start, subreddit=subreddit)
    posts_prev = aggregate.query_posts(conn, prev_week_start, week_start, subreddit=subreddit)

    series_30d = aggregate.topic_series(conn, 30, subreddit=subreddit)
    base30 = aggregate.baseline_avg(series_30d, 30)

    week_stats = aggregate.mention_stats(
        posts_week, keywords.TOPIC_KEYWORDS
    )
    prev_stats = aggregate.mention_stats(
        posts_prev, keywords.TOPIC_KEYWORDS
    )

    demand_trends = {}
    for topic, st in week_stats.items():
        m = st["mentions"]
        if m <= 0:
            continue
        prev = prev_stats[topic]["mentions"]
        wow = round((m - prev) / prev * 100, 1) if prev > 0 else None
        status = aggregate_trend_for_weekly(m, prev, base30[topic], st["unique_posts"])
        demand_trends[topic] = {
            **st,
            "previous_week": prev,
            "wow_change_pct": wow,
            "avg_30d": base30[topic],
            "trend": status,
        }

    # mention share：本周 grinder 讨论中各主题占比
    grinder_total = max(sum(st["mentions"] for st in week_stats.values()), 1)
    for topic, t in demand_trends.items():
        t["share_of_grinder_discussion"] = round(t["mentions"] / grinder_total, 4)

    return {
        "posts": posts_week,
        "posts_previous_week": posts_prev,
        "statistics": aggregate.compute_kpi(posts_week),
        "baseline_7d": aggregate.baseline_avg(
            aggregate.topic_series(conn, 7, subreddit=subreddit), 7
        ),
        "baseline_30d": base30,
        "topic_trends": demand_trends,
        "brand_trends": aggregate.brand_mentions(posts_week),
        "competitor_trends": aggregate.competitor_mentions(posts_week),
        "signal_alerts": [],
        "top_posts": aggregate.top_posts(posts_week),
    }


def aggregate_trend_for_weekly(
    current: float, prev: float, avg_30d: float, unique_posts: int
) -> str:
    """周报趋势状态（复用 app.trends 规则，但周口径）。"""
    from app.trends import classify_trend

    return classify_trend(
        current, prev, avg_30d * 7.0,
        evidence_count=unique_posts, unique_posts=unique_posts, weeks_high=1,
    )


def analyze_batch(
    conn,
    batch_id: int,
    report_type: str,
    context: Dict[str, Any],
) -> Dict[str, Any]:
    """执行一个 batch 的 LLM 分析（DeepSeek），结果写入 analysis_results。

    Args:
        conn: sqlite3 连接。
        batch_id: analysis_batches.batch_id。
        report_type: daily / weekly。
        context: aggregate 确定性上下文。

    Returns:
        分析结果 dict。

    Raises:
        DeepSeekError: 调用或解析失败。
    """
    if not deepseek_client.is_configured():
        raise deepseek_client.DeepSeekError(
            "ANALYZER_API_KEY not configured; cannot run LLM analysis"
        )

    if report_type == "weekly":
        messages = _build_weekly_prompt(context)
    else:
        messages = _build_daily_prompt(context)

    logger.info("Calling DeepSeek for batch %s (%s)", batch_id, report_type)
    text = deepseek_client.chat(messages, temperature=0.3, max_tokens=4096, json_mode=True)
    result = deepseek_client.parse_json_response(text)

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO analysis_results
            (batch_id, report_type, period_start, period_end, result_json)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (batch_id) DO UPDATE
           SET result_json = excluded.result_json
        """,
        (
            batch_id,
            report_type,
            context.get("period_start"),
            context.get("period_end"),
            _json.dumps(result, ensure_ascii=False),
        ),
    )
    cur.execute(
        """
        UPDATE analysis_batches
           SET status = 'completed', completed_at = CURRENT_TIMESTAMP
         WHERE batch_id = ?
        """,
        (batch_id,),
    )
    conn.commit()
    logger.info("Batch %s analysis completed", batch_id)
    return result
