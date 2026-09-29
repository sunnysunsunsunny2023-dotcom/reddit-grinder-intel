"""确定性聚合统计层（spec 第 4/7 节 + DoD #5）。

所有 ratio / WoW / baseline / mentions 数值都由本模块（阿里云）
计算；LLM 只分析不计算。本模块不调用任何 LLM。
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional

from . import keywords
from .trends import classify_signal, classify_trend, needs_alert

TEXT_LIMIT = 4000  # 拼接 title+selftext 用于关键词匹配的文本上限


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def window_start(hours: int) -> dt.datetime:
    return _now() - dt.timedelta(hours=hours)


def _text(post: Dict[str, Any]) -> str:
    parts = [post.get("title") or "", post.get("selftext") or ""]
    return " ".join(parts)[:TEXT_LIMIT]


# ------------------------------------------------------------
# SQL 查询
# ------------------------------------------------------------
def query_posts(
    conn,
    start: dt.datetime,
    end: Optional[dt.datetime] = None,
    subreddit: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """查询时间窗口内的帖子（只取聚合所需字段）。"""
    end = end or _now()
    sql = """
        SELECT post_id, subreddit, title, selftext, created_utc,
               score, num_comments, permalink
          FROM reddit_posts
         WHERE created_utc >= %s AND created_utc < %s
    """
    params: List[Any] = [start, end]
    if subreddit:
        sql += " AND subreddit = %s"
        params.append(subreddit)
    sql += " ORDER BY created_utc DESC"
    if limit:
        sql += " LIMIT %s"
        params.append(limit)

    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def topic_series(
    conn,
    days: int,
    subreddit: Optional[str] = None,
) -> Dict[str, List[int]]:
    """返回每个主题在最近 days 天内的每日 mentions 序列（用于 baseline）。"""
    start = _now() - dt.timedelta(days=days)
    posts = query_posts(conn, start, subreddit=subreddit)

    buckets: Dict[str, Dict[dt.date, int]] = {
        topic: {} for topic in keywords.TOPIC_KEYWORDS
    }
    for post in posts:
        day = post["created_utc"].date()
        text = _text(post)
        for topic, kws in keywords.TOPIC_KEYWORDS.items():
            if keywords.has_any(text, kws):
                buckets[topic][day] = buckets[topic].get(day, 0) + 1
    return {
        topic: [buckets[topic].get((start + dt.timedelta(days=i)).date(), 0)
                for i in range(days)]
        for topic in buckets
    }


# ------------------------------------------------------------
# 确定性统计
# ------------------------------------------------------------
def compute_kpi(posts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """日报 B. KPI（spec 5.1.B）。"""
    total = len(posts)
    grinder = 0
    geimori = 0
    competitor = 0
    high_signal_topics = 0

    for post in posts:
        text = _text(post)
        if keywords.has_any(text, keywords.GRINDER_KEYWORDS):
            grinder += 1
        if keywords.has_any(text, keywords.GEIMORI_KEYWORDS):
            geimori += 1
        if any(
            keywords.has_any(text, kws)
            for kws in keywords.COMPETITOR_KEYWORDS.values()
        ):
            competitor += 1

    grinder_ratio = round(grinder / total, 4) if total else 0.0
    return {
        "total_new_posts": total,
        "grinder_related_posts": grinder,
        "grinder_ratio": grinder_ratio,
        "geimori_mentions": geimori,
        "competitor_mentions": competitor,
        "high_signal_topics": high_signal_topics,  # 由 signal_alerts 回填
        "alert_count": 0,
    }


def mention_stats(
    posts: List[Dict[str, Any]], keyword_groups: Dict[str, List[str]]
) -> Dict[str, Dict[str, Any]]:
    """对 keyword_groups 的每个 key 统计 mentions/unique_posts/engagement。

    Returns:
        {key: {"mentions": int, "unique_posts": int, "engagement": int}}
        engagement = sum(score) + sum(num_comments) 用于排序。
    """
    out: Dict[str, Dict[str, Any]] = {}
    for name, kws in keyword_groups.items():
        mentions = 0
        unique = 0
        engagement = 0
        for post in posts:
            text = _text(post)
            if keywords.has_any(text, kws):
                mentions += 1
                unique += 1
                engagement += int(post.get("score") or 0) + int(
                    post.get("num_comments") or 0
                )
        out[name] = {
            "mentions": mentions,
            "unique_posts": unique,
            "engagement": engagement,
        }
    return out


def topic_mentions(posts: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """主题 mentions（spec 6.1 Demand Trends 口径）。"""
    return mention_stats(posts, keywords.TOPIC_KEYWORDS)


def brand_mentions(posts: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Geimori 品牌/产品线 mentions。"""
    return {"geimori": mention_stats(
        posts, {"geimori": keywords.GEIMORI_KEYWORDS}
    )["geimori"]}


def competitor_mentions(posts: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """竞品 mentions（mention share，禁止说成 market share）。"""
    return mention_stats(posts, keywords.COMPETITOR_KEYWORDS)


def top_posts(posts: List[Dict[str, Any]], limit: int = 5) -> List[Dict[str, Any]]:
    """按 engagement（score+comments）取 Top 帖子。"""
    ranked = sorted(
        posts,
        key=lambda p: int(p.get("score") or 0) + int(p.get("num_comments") or 0),
        reverse=True,
    )
    return [
        {
            "post_id": p["post_id"],
            "title": p["title"],
            "subreddit": p["subreddit"],
            "score": int(p.get("score") or 0),
            "comments": int(p.get("num_comments") or 0),
            "permalink": p["permalink"],
            "created_utc": p["created_utc"].isoformat() if p.get("created_utc") else None,
        }
        for p in ranked[:limit]
    ]


def signal_alerts(
    posts_24h: List[Dict[str, Any]],
    baseline_7d: Dict[str, float],
    baseline_30d: Dict[str, float],
    max_alerts: int = 5,
) -> List[Dict[str, Any]]:
    """日报 C. Signal Alerts：只突出真正异常变化（spec 5.1.C）。

    Args:
        posts_24h: 24h 窗口帖子。
        baseline_7d: {topic: 日均 mentions}。
        baseline_30d: {topic: 日均 mentions}。

    Returns:
        alert 列表（按 vs_7d 倍数降序）。
    """
    stats = topic_mentions(posts_24h)
    alerts: List[Dict[str, Any]] = []
    for topic, st in stats.items():
        m24 = st["mentions"]
        if m24 <= 0:
            continue
        avg7 = baseline_7d.get(topic, 0.0)
        avg30 = baseline_30d.get(topic, 0.0)
        status, confidence = classify_signal(
            m24, avg7, avg30, evidence_count=st["unique_posts"]
        )
        if not needs_alert(status):
            continue
        alerts.append(
            {
                "topic": topic,
                "mentions_24h": m24,
                "avg_7d": round(avg7, 2),
                "avg_30d": round(avg30, 2),
                "vs_7d": round(m24 / avg7, 2) if avg7 > 0 else None,
                "vs_30d": round(m24 / avg30, 2) if avg30 > 0 else None,
                "status": status,
                "confidence": confidence,
                "evidence_count": st["unique_posts"],
                "engagement": st["engagement"],
            }
        )
    alerts.sort(key=lambda a: a["vs_7d"] if a["vs_7d"] else 0, reverse=True)
    return alerts[:max_alerts]


def baseline_avg(series: Dict[str, List[int]], days: int) -> Dict[str, float]:
    """由 topic_series 结果算日均 mentions（7d/30d baseline）。"""
    out: Dict[str, float] = {}
    for topic, seq in series.items():
        if days <= 0:
            out[topic] = 0.0
            continue
        total = sum(seq)
        out[topic] = round(total / days, 4)
    return out


def build_daily_context(conn, subreddit: Optional[str] = None) -> Dict[str, Any]:
    """组装 Daily API 所需的确定性上下文（不含 LLM 产物）。

    Returns:
        {posts, statistics, baseline_7d, baseline_30d, topic_trends,
         brand_trends, competitor_trends, signal_alerts, top_posts}
    """
    now = _now()
    start_24h = now - dt.timedelta(hours=24)
    posts_24h = query_posts(conn, start_24h, subreddit=subreddit)

    series_7d = topic_series(conn, 7, subreddit=subreddit)
    series_30d = topic_series(conn, 30, subreddit=subreddit)
    base7 = baseline_avg(series_7d, 7)
    base30 = baseline_avg(series_30d, 30)

    stats = compute_kpi(posts_24h)
    alerts = signal_alerts(posts_24h, base7, base30)
    stats["high_signal_topics"] = len(alerts)
    stats["alert_count"] = len(alerts)

    topic_stats = topic_mentions(posts_24h)
    topic_trends = {}
    for topic, st in topic_stats.items():
        m = st["mentions"]
        if m <= 0:
            continue
        status = classify_trend(
            m,
            base7[topic] * 7.0,
            base30[topic],
            evidence_count=st["unique_posts"],
            unique_posts=st["unique_posts"],
        )
        topic_trends[topic] = {
            **st,
            "avg_7d": base7[topic],
            "avg_30d": base30[topic],
            "trend": status,
        }

    return {
        "posts": posts_24h,
        "statistics": stats,
        "baseline_7d": base7,
        "baseline_30d": base30,
        "topic_trends": topic_trends,
        "brand_trends": brand_mentions(posts_24h),
        "competitor_trends": competitor_mentions(posts_24h),
        "signal_alerts": alerts,
        "top_posts": top_posts(posts_24h),
    }
