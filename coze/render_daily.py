#!/usr/bin/env python3
"""Reddit Grinder Daily Pulse 渲染模块（Coze 侧，只渲染不计算）。

数据来源：阿里云 8086 /api/analysis-batch?report_type=daily&hours=24
（header: X-API-Key）。
产物：
  - daily_reports/reddit_pulse_YYYY-MM-DD.html  单文件 HTML（内联 CSS，无外部依赖）
  - daily_reports/reddit_pulse_YYYY-MM-DD.json  原始 payload 留档

用法：
  python3 -B coze/render_daily.py --api-base http://127.0.0.1:8086 --api-key <KEY> \
      --output-dir daily_reports [--report-date 2026-09-30] [--hours 24] [--input-json <file>]
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

DEFAULT_API_BASE = os.environ.get("REDDIT_API_BASE", "http://127.0.0.1:8086")
DEFAULT_API_KEY = os.environ.get("REDDIT_API_KEY", "")

# 时区（报告日期采用北京时间，与服务器数据均为 UTC 存储后按北京呈现）
_UTC = dt.timezone.utc
_CST = dt.timezone(dt.timedelta(hours=8))


# ---------------------------------------------------------------- 数据获取
def fetch_payload(
    api_base: str = DEFAULT_API_BASE,
    api_key: str = DEFAULT_API_KEY,
    report_type: str = "daily",
    hours: int = 24,
) -> Dict[str, Any]:
    """调用 8086 analysis-batch API，返回 payload 字典。"""
    url = f"{api_base.rstrip('/')}/api/reddit/analysis-batch?report_type={report_type}&hours={hours}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("X-API-Key", api_key)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"API {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"API 连接失败: {exc.reason}") from exc
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"API 返回不是对象: {type(data).__name__}")
    return data


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


# ---------------------------------------------------------------- 数据规范化
def build_pulse(payload: Dict[str, Any], report_date: Optional[str] = None) -> Dict[str, Any]:
    """把 API payload 规范化为 Daily Pulse 渲染结构（对 LLM 字段宽容）。"""
    analysis = _as_dict(payload.get("analysis"))
    statistics = _as_dict(payload.get("statistics"))
    period = _as_dict(payload.get("period"))
    posts = _as_list(payload.get("posts"))

    # post_id → 帖子（用于给 LLM top_posts 补真实链接与互动数据）
    post_index: Dict[str, Dict[str, Any]] = {}
    for p in posts:
        pid = p.get("post_id") if isinstance(p, dict) else None
        if pid:
            post_index[str(pid)] = _as_dict(p)

    top_posts: List[Dict[str, Any]] = []
    for tp in _as_list(analysis.get("top_posts")):
        tp = _as_dict(tp)
        pid = str(tp.get("post_id") or "")
        src = post_index.get(pid, {})
        top_posts.append(
            {
                "post_id": pid,
                "title": str(tp.get("title") or src.get("title") or "(untitled)"),
                "why_it_matters": str(tp.get("why_it_matters") or ""),
                "subreddit": str(src.get("subreddit") or ""),
                "score": src.get("score"),
                "comments": src.get("comments") or src.get("num_comments"),
                "permalink": str(src.get("permalink") or ""),
            }
        )

    alerts: List[Dict[str, Any]] = []
    for a in _as_list(analysis.get("signal_alerts")):
        a = _as_dict(a)
        permalinks = _as_list(a.get("permalink"))
        alerts.append(
            {
                "topic": str(a.get("topic") or "Unnamed signal"),
                "status": str(a.get("status") or "stable"),
                "confidence": str(a.get("confidence") or "low"),
                "evidence_count": a.get("evidence_count"),
                "what_happened": str(a.get("what_happened") or ""),
                "permalinks": [str(x) for x in permalinks if str(x).startswith("http")],
                "weak_signal": bool(a.get("weak_signal")),
            }
        )

    geimori = _as_dict(analysis.get("geimori_watch"))
    actions: List[Dict[str, Any]] = []
    for act in _as_list(analysis.get("today_actions")):
        act = _as_dict(act)
        actions.append(
            {
                "action": str(act.get("action") or ""),
                "why": str(act.get("why") or ""),
                "evidence_count": act.get("evidence_count"),
                "confidence": str(act.get("confidence") or "low"),
                "category": str(act.get("category") or "Other"),
            }
        )

    if not report_date:
        ts = period.get("end") or period.get("start") or payload.get("report_date")
        report_date = _normalize_date(ts)

    return {
        "report_date": report_date,
        "period": {
            "start": period.get("start"),
            "end": period.get("end"),
        },
        "batch_id": payload.get("batch_id"),
        "today_in_one_sentence": str(
            analysis.get("today_in_one_sentence") or "过去 24h 未发现显著变化。"
        ),
        "kpi_summary": str(analysis.get("kpi_summary") or ""),
        "kpi": {
            "total_new_posts": statistics.get("total_new_posts"),
            "grinder_related_posts": statistics.get("grinder_related_posts"),
            "grinder_ratio": statistics.get("grinder_ratio"),
            "geimori_mentions": statistics.get("geimori_mentions"),
            "competitor_mentions": statistics.get("competitor_mentions"),
            "high_signal_topics": statistics.get("high_signal_topics"),
            "alert_count": statistics.get("alert_count", len(alerts)),
        },
        "signal_alerts": alerts,
        "top_posts": top_posts[:5],
        "geimori_watch": {
            "has_mention": bool(geimori.get("has_mention")),
            "detail": str(
                geimori.get("detail")
                or (
                    "No meaningful Geimori mention in the last 24 hours."
                    if not geimori.get("has_mention")
                    else ""
                )
            ),
        },
        "today_actions": actions,
        "generated_at": dt.datetime.now(_CST).strftime("%Y-%m-%d %H:%M:%S"),
    }


def _normalize_date(value: Any) -> str:
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(float(value), _UTC).astimezone(_CST).strftime("%Y-%m-%d")
    s = str(value or "")
    if not s:
        return dt.datetime.now(_CST).strftime("%Y-%m-%d")
    s = s.replace("T", " ").replace("Z", "").strip()
    try:
        return dt.datetime.fromisoformat(s).astimezone(_CST).strftime("%Y-%m-%d")
    except ValueError:
        return dt.datetime.now(_CST).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- HTML 渲染
def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _fmt_num(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return _e(value)


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return _e(value)


_STATUS_COLOR = {
    "spike": "var(--status-spike)",
    "emerging": "var(--status-emerging)",
    "rising": "var(--status-rising)",
    "stable": "var(--status-stable)",
    "declining": "var(--status-declining)",
}


def render_html(pulse: Dict[str, Any]) -> str:
    """把 Daily Pulse 结构渲染为单文件 HTML（内联 CSS、无外部依赖）。"""
    kpi = pulse["kpi"]
    kpi_cards = [
        ("总新增帖", _fmt_num(kpi.get("total_new_posts")), "过去 24h 抓取帖子总数", "kpi"),
        ("磨豆机相关", _fmt_num(kpi.get("grinder_related_posts")), "命中磨豆机关键词的帖子", "kpi"),
        ("相关占比", _fmt_pct(kpi.get("grinder_ratio")), "磨豆机相关帖 / 总帖", "kpi"),
        ("Geimori 提及", _fmt_num(kpi.get("geimori_mentions")), "品牌名被讨论的次数", "kpi"),
        ("竞品提及", _fmt_num(kpi.get("competitor_mentions")), "竞品关键词命中数", "kpi"),
        ("高信号话题", _fmt_num(kpi.get("high_signal_topics")), "spike / emerging 级主题数", "kpi"),
        ("告警数", _fmt_num(kpi.get("alert_count")), "本轮 signal alerts 数量", "kpi"),
    ]
    kpi_html = "\n".join(
        f'<div class="kpi-card"><div class="kpi-label">{_e(label)}</div>'
        f'<div class="kpi-value">{_e(val)}</div>'
        f'<div class="kpi-hint">{_e(hint)}</div></div>'
        for label, val, hint, _cls in kpi_cards
    )

    # Signal alerts
    if pulse["signal_alerts"]:
        alerts_html = []
        for a in pulse["signal_alerts"]:
            color = _STATUS_COLOR.get(a["status"], "var(--status-stable)")
            status_label = a["status"].upper()
            conf = a["confidence"].upper()
            evidence = (
                f'<span class="pill">{_e(str(a["evidence_count"]))} 帖</span>'
                if a["evidence_count"] is not None
                else ""
            )
            weak = '<span class="pill pill-weak">weak signal</span>' if a["weak_signal"] else ""
            links = "".join(
                f'<a href="{_e(link)}" target="_blank" rel="noopener noreferrer">原帖 ↗</a>'
                for link in a["permalinks"][:3]
            )
            alerts_html.append(
                f'<article class="alert-card" style="--ac: {color}">'
                f'<div class="alert-head"><span class="alert-status">{_e(status_label)}</span>'
                f'<h3>{_e(a["topic"])}</h3>'
                f'<span class="pill">{_e(conf)}</span>{evidence}{weak}</div>'
                f'<p>{_e(a["what_happened"])}</p>'
                f'<div class="alert-links">{links}</div></article>'
            )
        alerts_html = "\n".join(alerts_html)
    else:
        alerts_html = '<div class="empty">过去 24h 未检出需要标记的信号主题。</div>'

    # Top posts
    if pulse["top_posts"]:
        posts_html = []
        for i, tp in enumerate(pulse["top_posts"], start=1):
            meta = []
            if tp["subreddit"]:
                meta.append(f'r/{_e(tp["subreddit"])}')
            if tp["score"] is not None:
                meta.append(f'{_fmt_num(tp["score"])} score')
            if tp["comments"] is not None:
                meta.append(f'{_fmt_num(tp["comments"])} comments')
            link_html = (
                f'<a class="post-link" href="https://www.reddit.com{_e(tp["permalink"])}" '
                f'target="_blank" rel="noopener noreferrer">查看原帖 ↗</a>'
                if tp["permalink"]
                else ""
            )
            why = (
                f'<p class="post-why">{_e(tp["why_it_matters"])}</p>'
                if tp["why_it_matters"]
                else ""
            )
            posts_html.append(
                f'<article class="post-item"><div class="post-rank">{i}</div>'
                f'<div><h3>{_e(tp["title"])}</h3>{why}'
                f'<div class="post-meta"><span>{_e(" / ".join(meta))}</span>{link_html}</div>'
                f'</div></article>'
            )
        posts_html = "\n".join(posts_html)
    else:
        posts_html = '<div class="empty">无高互动帖子入选。</div>'

    # Geimori watch
    gw = pulse["geimori_watch"]
    if gw["has_mention"]:
        geimori_html = (
            '<section class="panel geimori geimori-hit">'
            '<h2>Geimori Watch</h2>'
            f'<p class="geimori-detail">{_e(gw["detail"])}</p></section>'
        )
    else:
        geimori_html = (
            '<section class="panel geimori geimori-clear">'
            '<h2>Geimori Watch</h2>'
            f'<p class="geimori-detail">{_e(gw["detail"])}</p></section>'
        )

    # Actions
    if pulse["today_actions"]:
        actions_html = []
        for act in pulse["today_actions"]:
            badges = [
                f'<span class="pill pill-cat">{_e(act["category"])}</span>',
                f'<span class="pill">{_e(act["confidence"].upper())}</span>',
            ]
            if act["evidence_count"] is not None:
                badges.append(f'<span class="pill">{_e(str(act["evidence_count"]))} 帖</span>')
            why = (
                f'<p class="action-why">{_e(act["why"])}</p>' if act["why"] else ""
            )
            actions_html.append(
                f'<article class="action-item"><h3>{_e(act["action"])}</h3>{why}'
                f'<div class="action-badges">{"".join(badges)}</div></article>'
            )
        actions_html = "\n".join(actions_html)
    else:
        actions_html = (
            '<div class="empty">No immediate action required today.</div>'
        )

    period_txt = ""
    if pulse["period"].get("start") and pulse["period"].get("end"):
        period_txt = (
            f'窗口 {_e(str(pulse["period"]["start"])[:16])} → '
            f'{_e(str(pulse["period"]["end"])[:16])}'
        )
    footer_note = (
        f'<div class="foot-meta">生成时间 {_e(pulse["generated_at"])}'
        + (f' · Batch #{_e(str(pulse["batch_id"]))}' if pulse["batch_id"] is not None else "")
        + (f' · {period_txt}' if period_txt else "")
        + "</div>"
        '<div class="foot-source">数据来源：Reddit 公开社区帖子，由阿里云侧抓取与 LLM 分析生成，'
        "仅供内部情报参考；原帖链接均指向 reddit.com 真实来源。</div>"
    )

    kpi_summary_html = (
        f'<p class="kpi-summary">{_e(pulse["kpi_summary"])}</p>'
        if pulse["kpi_summary"]
        else ""
    )

    return _PAGE_TEMPLATE.format(
        report_date=_e(pulse["report_date"]),
        one_sentence=_e(pulse["today_in_one_sentence"]),
        kpi_summary=kpi_summary_html,
        kpi_cards=kpi_html,
        alerts=alerts_html,
        posts=posts_html,
        geimori=geimori_html,
        actions=actions_html,
        footer_note=footer_note,
    )


_PAGE_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reddit 磨豆机情报 · Daily Pulse — {report_date}</title>
<style>
  :root {{
    --canvas: #f6f0e4;
    --surface: #fffdf7;
    --surface-soft: #efe5d3;
    --ink: #3b2a1b;
    --ink-soft: #7a624c;
    --line: #e2d5bf;
    --accent: #a96a3b;
    --accent-strong: #7c4a24;
    --accent-ink: #fffaf1;
    --status-spike: #b0402e;
    --status-emerging: #c2701e;
    --status-rising: #a96a3b;
    --status-stable: #5f7a46;
    --status-declining: #8a857c;
    --font-body: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    --font-mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
    --pad-page: clamp(16px, 4vw, 40px);
    --r-sm: 6px;
    --r-md: 12px;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--canvas);
    color: var(--ink);
    font-family: var(--font-body);
    line-height: 1.55;
  }}
  .wrap {{ max-width: 1080px; margin: 0 auto; padding: var(--pad-page); }}
  header.hero {{
    background: linear-gradient(135deg, #3b2a1b 0%, #5a4026 100%);
    color: var(--accent-ink);
    border-radius: var(--r-md);
    padding: clamp(20px, 4vw, 34px);
    margin-bottom: 20px;
  }}
  header.hero .kicker {{
    font-size: 12px; letter-spacing: 0.18em; text-transform: uppercase;
    color: #e8c89a; margin: 0 0 6px; font-family: var(--font-mono);
  }}
  header.hero h1 {{ margin: 0 0 10px; font-size: clamp(24px, 4vw, 34px); line-height: 1.2; }}
  header.hero .one-liner {{
    font-size: clamp(15px, 2.2vw, 18px); color: #f5ead6; max-width: 64ch; margin: 0;
  }}
  h2 {{ font-size: clamp(18px, 2.4vw, 22px); margin: 0 0 12px; color: var(--ink); }}
  section.panel {{
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: var(--r-md);
    padding: clamp(16px, 3vw, 24px);
    margin-bottom: 18px;
  }}
  .kpi-summary {{
    background: var(--surface-soft); border-radius: var(--r-sm);
    padding: 10px 14px; margin: 0 0 16px; color: var(--ink-soft); font-size: 14px;
  }}
  .kpi-grid {{
    display: grid; gap: 12px;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  }}
  .kpi-card {{
    background: var(--surface); border: 1px solid var(--line);
    border-top: 3px solid var(--accent); border-radius: var(--r-md);
    padding: 14px 16px;
  }}
  .kpi-label {{ font-size: 12px; color: var(--ink-soft); margin-bottom: 4px; }}
  .kpi-value {{ font-size: clamp(22px, 3vw, 30px); font-weight: 700; font-family: var(--font-mono); color: var(--accent-strong); }}
  .kpi-hint {{ font-size: 11px; color: var(--ink-soft); margin-top: 6px; }}
  .alert-card {{
    border: 1px solid var(--line); border-left: 4px solid var(--ac, var(--accent));
    border-radius: var(--r-sm); padding: 12px 14px; margin-bottom: 12px;
    background: var(--surface);
  }}
  .alert-card:last-child {{ margin-bottom: 0; }}
  .alert-head {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }}
  .alert-head h3 {{ margin: 0; font-size: 16px; }}
  .alert-status {{
    font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.08em;
    color: var(--ac, var(--accent)); font-weight: 700;
  }}
  .alert-card p {{ margin: 8px 0 6px; color: var(--ink); }}
  .alert-links a {{ color: var(--accent-strong); text-decoration: none; font-size: 13px; }}
  .alert-links a:hover {{ text-decoration: underline; }}
  .pill {{
    display: inline-block; background: var(--surface-soft); color: var(--ink-soft);
    border-radius: 999px; padding: 2px 10px; font-size: 11px; font-family: var(--font-mono);
  }}
  .pill-weak {{ background: #f3e9d0; color: #8a6d3b; }}
  .pill-cat {{ background: #e9f0dd; color: #5f7a46; }}
  .post-item {{
    display: grid; grid-template-columns: 34px 1fr; gap: 12px;
    padding: 12px 0; border-bottom: 1px solid var(--line);
  }}
  .post-item:last-child {{ border-bottom: 0; }}
  .post-rank {{
    width: 28px; height: 28px; border-radius: 50%;
    background: var(--accent); color: var(--accent-ink);
    display: flex; align-items: center; justify-content: center;
    font-family: var(--font-mono); font-size: 13px; font-weight: 700;
  }}
  .post-item h3 {{ margin: 0 0 4px; font-size: 15px; line-height: 1.35; }}
  .post-why {{ margin: 0 0 6px; color: var(--ink-soft); font-size: 13px; }}
  .post-meta {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; font-size: 12px; color: var(--ink-soft); font-family: var(--font-mono); }}
  .post-link {{ color: var(--accent-strong); text-decoration: none; font-family: var(--font-body); }}
  .post-link:hover {{ text-decoration: underline; }}
  .geimori {{ border-left-width: 4px; }}
  .geimori-hit {{ border-left: 4px solid var(--status-emerging); background: #fbf3e6; }}
  .geimori-clear {{ border-left: 4px solid var(--status-stable); }}
  .geimori-detail {{ margin: 0; }}
  .action-item {{
    padding: 12px 0; border-bottom: 1px solid var(--line);
  }}
  .action-item:last-child {{ border-bottom: 0; }}
  .action-item h3 {{ margin: 0 0 4px; font-size: 15px; }}
  .action-why {{ margin: 0 0 8px; color: var(--ink-soft); font-size: 13px; }}
  .action-badges {{ display: flex; flex-wrap: wrap; gap: 6px; }}
  .empty {{
    background: var(--surface-soft); color: var(--ink-soft);
    border-radius: var(--r-sm); padding: 14px 16px; font-size: 14px;
  }}
  footer.foot {{
    color: var(--ink-soft); font-size: 12px; line-height: 1.6; margin-top: 8px;
  }}
  .foot-meta {{ font-family: var(--font-mono); }}
  .foot-source {{ margin-top: 4px; }}
  a:focus-visible, summary:focus-visible {{
    outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 2px;
  }}
  @media (max-width: 520px) {{
    .post-item {{ grid-template-columns: 26px 1fr; }}
    .post-rank {{ width: 24px; height: 24px; font-size: 11px; }}
  }}
  @media print {{
    body {{ background: #fff; }}
    .wrap {{ max-width: none; padding: 0; }}
    section.panel, .kpi-card, .alert-card, header.hero {{ break-inside: avoid; }}
    .post-link, .alert-links a {{ color: inherit; text-decoration: underline; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <header class="hero">
    <p class="kicker">Reddit Grinder Intelligence · Daily Pulse</p>
    <h1>磨豆机社区日报 — {report_date}</h1>
    <p class="one-liner">{one_sentence}</p>
  </header>

  <section class="panel" aria-label="今日 KPI">
    <h2>今日 KPI</h2>
    {kpi_summary}
    <div class="kpi-grid">
      {kpi_cards}
    </div>
  </section>

  <section class="panel" aria-label="信号告警">
    <h2>Signal Alerts</h2>
    {alerts}
  </section>

  <section class="panel" aria-label="热门帖子">
    <h2>Top Posts</h2>
    {posts}
  </section>

  {geimori}

  <section class="panel" aria-label="今日行动">
    <h2>Today Actions</h2>
    {actions}
  </section>

  <footer class="foot">
    {footer_note}
  </footer>
</div>
</body>
</html>
"""


# ---------------------------------------------------------------- 落盘
def write_outputs(
    output_dir: str,
    pulse: Dict[str, Any],
    html_text: str,
    raw_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    os.makedirs(output_dir, exist_ok=True)
    date_str = pulse["report_date"]
    html_path = os.path.join(output_dir, f"reddit_pulse_{date_str}.html")
    json_path = os.path.join(output_dir, f"reddit_pulse_{date_str}.json")
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(html_text)
    if raw_payload is not None:
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(raw_payload, fh, ensure_ascii=False, indent=2)
    return {"html": html_path, "json": json_path}


# ---------------------------------------------------------------- CLI
def _parse_args(argv: List[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Render Reddit Grinder Daily Pulse HTML")
    ap.add_argument("--api-base", default=DEFAULT_API_BASE)
    ap.add_argument("--api-key", default=DEFAULT_API_KEY)
    ap.add_argument("--report-type", default="daily", choices=["daily", "weekly"])
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--report-date", default=None)
    ap.add_argument("--output-dir", default="daily_reports")
    ap.add_argument(
        "--input-json",
        default=None,
        help="跳过 API，直接读取本地 payload JSON（本地渲染/测试用）",
    )
    return ap.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    if args.input_json:
        with open(args.input_json, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    else:
        if not args.api_key:
            print("错误：需要 --api-key 或环境变量 REDDIT_API_KEY", file=sys.stderr)
            return 2
        payload = fetch_payload(
            api_base=args.api_base,
            api_key=args.api_key,
            report_type=args.report_type,
            hours=args.hours,
        )
    pulse = build_pulse(payload, report_date=args.report_date)
    html_text = render_html(pulse)
    paths = write_outputs(args.output_dir, pulse, html_text, raw_payload=payload)
    print(json.dumps({"report_date": pulse["report_date"], **paths}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
