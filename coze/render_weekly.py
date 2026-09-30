#!/usr/bin/env python3
"""Reddit Grinder Weekly Intelligence 渲染模块（Coze 侧，只渲染不计算）。

数据来源：阿里云 8086 /api/analysis-batch?report_type=weekly&hours=168
（header: X-API-Key）。产物：
  - daily_reports/reddit_weekly_YYYY-MM-DD.html  单文件 HTML
  - daily_reports/reddit_weekly_YYYY-MM-DD.json  原始 payload 留档

用法：
  python3 -B coze/render_weekly.py --api-base http://127.0.0.1:8086 --api-key <KEY> \
      --output-dir daily_reports [--report-date 2026-09-30] [--hours 168] [--input-json <file>]
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
from typing import Any, Dict, List, Optional

from coze.render_daily import (
    _e,
    _fmt_num,
    _fmt_pct,
    _normalize_date,
    _as_dict,
    _as_list,
    fetch_payload,
)


# ---------------------------------------------------------------- 数据规范化
def build_weekly_pulse(payload: Dict[str, Any], report_date: Optional[str] = None) -> Dict[str, Any]:
    """把 API payload 规范化为 Weekly Intelligence 渲染结构（对 LLM 字段宽容）。"""
    analysis = _as_dict(payload.get("analysis"))
    statistics = _as_dict(payload.get("statistics"))
    period = _as_dict(payload.get("period"))

    exec_summary: List[Dict[str, str]] = []
    for item in _as_list(analysis.get("executive_summary")):
        item = _as_dict(item)
        exec_summary.append(
            {
                "point": str(item.get("point") or ""),
                "evidence": str(item.get("evidence") or ""),
            }
        )

    demand_trends: List[Dict[str, Any]] = []
    for t in _as_list(analysis.get("demand_trends")):
        t = _as_dict(t)
        demand_trends.append(
            {
                "topic": str(t.get("topic") or "Unnamed topic"),
                "mentions": t.get("mentions"),
                "previous_week": t.get("previous_week"),
                "wow_change_pct": t.get("wow_change_pct"),
                "share_of_grinder_discussion": t.get("share_of_grinder_discussion"),
                "avg_30d": t.get("avg_30d"),
                "trend": str(t.get("trend") or "stable"),
                "confidence": str(t.get("confidence") or "low"),
                "interpretation": str(t.get("interpretation") or ""),
            }
        )

    competitor: List[Dict[str, Any]] = []
    for c in _as_list(analysis.get("competitor_intelligence")):
        c = _as_dict(c)
        competitor.append(
            {
                "brand": str(c.get("brand") or "Unknown"),
                "mentions": c.get("mentions"),
                "sentiment": str(c.get("sentiment") or "neutral"),
                "common_praise": str(c.get("common_praise") or ""),
                "common_complaints": str(c.get("common_complaints") or ""),
                "threads": [
                    str(x) for x in _as_list(c.get("high_engagement_threads"))
                    if str(x).startswith("http")
                ],
                "potential_opportunity": str(c.get("potential_opportunity") or ""),
                "mention_share_note": str(c.get("mention_share_note") or ""),
            }
        )

    gv = _as_dict(analysis.get("geimori_voice"))
    geimori_voice = {
        "mentions": gv.get("mentions"),
        "sentiment": str(gv.get("sentiment") or "neutral"),
        "positive_themes": [str(x) for x in _as_list(gv.get("positive_themes"))],
        "negative_themes": [str(x) for x in _as_list(gv.get("negative_themes"))],
        "questions_objections": [str(x) for x in _as_list(gv.get("questions_objections"))],
        "comparison_targets": [str(x) for x in _as_list(gv.get("comparison_targets"))],
        "actionable_response": str(gv.get("actionable_response") or ""),
    }

    priorities: List[Dict[str, Any]] = []
    for p in _as_list(analysis.get("customer_priorities")):
        p = _as_dict(p)
        priorities.append(
            {
                "priority": str(p.get("priority") or "weak_signal"),
                "topic": str(p.get("topic") or ""),
                "priority_score": p.get("priority_score"),
                "interpretation": str(p.get("interpretation") or ""),
            }
        )

    actions: List[Dict[str, Any]] = []
    for a in _as_list(analysis.get("next_week_actions")):
        a = _as_dict(a)
        actions.append(
            {
                "action": str(a.get("action") or ""),
                "evidence": str(a.get("evidence") or ""),
                "business_reason": str(a.get("business_reason") or ""),
                "confidence": str(a.get("confidence") or "low"),
                "urgency": str(a.get("urgency") or "low"),
                "category": str(a.get("category") or "Research"),
            }
        )

    if not report_date:
        ts = period.get("start") or period.get("end") or payload.get("report_date")
        report_date = _normalize_date(ts)

    return {
        "report_date": report_date,
        "period": {"start": period.get("start"), "end": period.get("end")},
        "batch_id": payload.get("batch_id"),
        "kpi": {
            "total_new_posts": statistics.get("total_new_posts"),
            "grinder_related_posts": statistics.get("grinder_related_posts"),
            "grinder_ratio": statistics.get("grinder_ratio"),
            "geimori_mentions": statistics.get("geimori_mentions"),
            "competitor_mentions": statistics.get("competitor_mentions"),
        },
        "executive_summary": exec_summary,
        "demand_trends": demand_trends,
        "competitor_intelligence": competitor,
        "geimori_voice": geimori_voice,
        "customer_priorities": priorities,
        "next_week_actions": actions[:5],
        "generated_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone(__import__("datetime").timedelta(hours=8))
        ).strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---------------------------------------------------------------- HTML 渲染
_PRIORITY_LABEL = {
    "structural_priority": ("结构性优先级", "var(--status-spike)"),
    "emerging_trend": ("新兴趋势", "var(--status-emerging)"),
    "temporary_spike": ("短期脉冲", "var(--status-rising)"),
    "weak_signal": ("弱信号", "var(--status-stable)"),
}
_SENTIMENT_LABEL = {
    "positive": "正面",
    "negative": "负面",
    "mixed": "混合",
    "neutral": "中性",
}


def render_weekly_html(pulse: Dict[str, Any]) -> str:
    """把 Weekly Intelligence 结构渲染为单文件 HTML。"""
    # Executive summary
    if pulse["executive_summary"]:
        exec_html = "\n".join(
            f'<article class="exec-item"><h3>{_e(i + 1)}. {_e(item["point"])}</h3>'
            f'<p>{_e(item["evidence"])}</p></article>'
            for i, item in enumerate(pulse["executive_summary"])
        )
    else:
        exec_html = '<div class="empty">本周无显著变化。</div>'

    # Demand trends table
    if pulse["demand_trends"]:
        rows = []
        for t in pulse["demand_trends"]:
            wow = _fmt_wow(t["wow_change_pct"])
            share = _fmt_pct(t["share_of_grinder_discussion"]) if t["share_of_grinder_discussion"] is not None else "—"
            avg30 = _fmt_num(t["avg_30d"]) if t["avg_30d"] is not None else "—"
            interp = f'<div class="td-note">{_e(t["interpretation"])}</div>' if t["interpretation"] else ""
            rows.append(
                f"<tr><td><strong>{_e(t['topic'])}</strong>{interp}</td>"
                f'<td class="num">{_fmt_num(t["mentions"])}</td>'
                f'<td class="num">{_fmt_num(t["previous_week"])}</td>'
                f'<td class="num">{wow}</td>'
                f'<td class="num">{share}</td>'
                f'<td class="num">{avg30}</td>'
                f'<td><span class="trend trend-{_e(t["trend"])}">{_e(t["trend"])}</span></td>'
                f'<td>{_e(t["confidence"].upper())}</td></tr>'
            )
        trends_html = (
            '<div class="table-scroll"><table>'
            "<caption>本周 vs 上周 vs 30 天基线（数值来自阿里云确定性统计）</caption>"
            "<thead><tr>"
            "<th scope=\"col\">主题</th><th scope=\"col\">提及</th><th scope=\"col\">上周</th>"
            "<th scope=\"col\">WoW</th><th scope=\"col\">讨论份额</th><th scope=\"col\">30d 均值</th>"
            "<th scope=\"col\">趋势</th><th scope=\"col\">置信</th>"
            "</tr></thead><tbody>" + "\n".join(rows) + "</tbody></table></div>"
        )
    else:
        trends_html = '<div class="empty">本周无主题趋势数据。</div>'

    # Competitor
    if pulse["competitor_intelligence"]:
        comp_html = []
        for c in pulse["competitor_intelligence"]:
            sentiment = _SENTIMENT_LABEL.get(c["sentiment"], c["sentiment"])
            threads = "".join(
                f'<a href="{_e(link)}" target="_blank" rel="noopener noreferrer">高互动帖 ↗</a> '
                for link in c["threads"][:3]
            )
            items = []
            if c["common_praise"]:
                items.append(f'<div class="c-row"><span class="c-label">好评</span>{_e(c["common_praise"])}</div>')
            if c["common_complaints"]:
                items.append(f'<div class="c-row"><span class="c-label">槽点</span>{_e(c["common_complaints"])}</div>')
            if c["potential_opportunity"]:
                items.append(f'<div class="c-row"><span class="c-label">机会</span>{_e(c["potential_opportunity"])}</div>')
            if c["mention_share_note"]:
                items.append(f'<div class="c-note">{_e(c["mention_share_note"])}</div>')
            comp_html.append(
                f'<article class="comp-card"><div class="comp-head">'
                f'<h3>{_e(c["brand"])}</h3>'
                f'<span class="pill pill-sent">{_e(sentiment)}</span>'
                f'<span class="pill">{_fmt_num(c["mentions"])} 提及</span></div>'
                + "".join(items)
                + f'<div class="c-links">{threads}</div></article>'
            )
        comp_html = "\n".join(comp_html)
    else:
        comp_html = '<div class="empty">本周无竞品讨论数据。</div>'

    # Geimori voice
    gv = pulse["geimori_voice"]
    def _themes(title: str, values: List[str]) -> str:
        if not values:
            return ""
        lis = "".join(f"<li>{_e(v)}</li>" for v in values)
        return f'<div class="gv-col"><h4>{_e(title)}</h4><ul>{lis}</ul></div>'
    gv_html = (
        f'<div class="gv-head"><h3>Geimori / MyWirsh / GU 系列</h3>'
        f'<span class="pill pill-sent">{_e(_SENTIMENT_LABEL.get(gv["sentiment"], gv["sentiment"]))}</span>'
        f'<span class="pill">{_fmt_num(gv["mentions"])} 提及</span></div>'
        + _themes("正面主题", gv["positive_themes"])
        + _themes("负面主题", gv["negative_themes"])
        + _themes("疑问 / 异议", gv["questions_objections"])
        + _themes("对比对象", gv["comparison_targets"])
        + (f'<p class="gv-action"><span class="c-label">建议回应</span>{_e(gv["actionable_response"])}</p>' if gv["actionable_response"] else "")
    )

    # Customer priorities
    if pulse["customer_priorities"]:
        pri_html = []
        for p in pulse["customer_priorities"]:
            label, color = _PRIORITY_LABEL.get(p["priority"], (p["priority"], "var(--status-stable)"))
            score = f'<span class="pill">{_fmt_num(p["priority_score"])} 分</span>' if p["priority_score"] is not None else ""
            pri_html.append(
                f'<article class="pri-item" style="--pc: {color}">'
                f'<div class="pri-head"><span class="pri-tag">{_e(label)}</span>'
                f'<h3>{_e(p["topic"])}</h3>{score}</div>'
                f'<p>{_e(p["interpretation"])}</p></article>'
            )
        pri_html = "\n".join(pri_html)
    else:
        pri_html = '<div class="empty">本周无优先级数据。</div>'

    # Next week actions
    if pulse["next_week_actions"]:
        act_html = []
        for a in pulse["next_week_actions"]:
            act_html.append(
                f'<article class="act-item"><div class="act-head">'
                f'<span class="pill pill-cat">{_e(a["category"])}</span>'
                f'<h3>{_e(a["action"])}</h3></div>'
                f'<p class="act-ev">{_e(a["evidence"])}</p>'
                + (f'<p class="act-why">{_e(a["business_reason"])}</p>' if a["business_reason"] else "")
                + f'<div class="act-badges"><span class="pill">置信 {_e(a["confidence"].upper())}</span>'
                f'<span class="pill">紧急 {_e(a["urgency"].upper())}</span></div></article>'
            )
        act_html = "\n".join(act_html)
    else:
        act_html = '<div class="empty">下周无建议行动。</div>'

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
        '<div class="foot-source">数据来源：Reddit 公开社区帖子，由阿里云侧抓取与 LLM 分析生成；'
        "原帖链接均指向 reddit.com 真实来源。Reddit 讨论份额 ≠ 市场份额。</div>"
    )

    return _WEEKLY_TEMPLATE.format(
        report_date=_e(pulse["report_date"]),
        kpi_total=_fmt_num(pulse["kpi"]["total_new_posts"]),
        kpi_grinder=_fmt_num(pulse["kpi"]["grinder_related_posts"]),
        kpi_ratio=_fmt_pct(pulse["kpi"]["grinder_ratio"]),
        kpi_geimori=_fmt_num(pulse["kpi"]["geimori_mentions"]),
        kpi_comp=_fmt_num(pulse["kpi"]["competitor_mentions"]),
        exec_html=exec_html,
        trends_html=trends_html,
        comp_html=comp_html,
        gv_html=gv_html,
        pri_html=pri_html,
        act_html=act_html,
        footer_note=footer_note,
    )


def _fmt_wow(value: Any) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
        sign = "+" if v >= 0 else ""
        return f"{sign}{v:.1f}%"
    except (TypeError, ValueError):
        return _e(value)


_WEEKLY_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reddit 磨豆机情报 · Weekly Intelligence — {report_date}</title>
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
    margin: 0; background: var(--canvas); color: var(--ink);
    font-family: var(--font-body); line-height: 1.55;
  }}
  .wrap {{ max-width: 1080px; margin: 0 auto; padding: var(--pad-page); }}
  header.hero {{
    background: linear-gradient(135deg, #3b2a1b 0%, #5a4026 100%);
    color: var(--accent-ink); border-radius: var(--r-md);
    padding: clamp(20px, 4vw, 34px); margin-bottom: 20px;
  }}
  header.hero .kicker {{
    font-size: 12px; letter-spacing: 0.18em; text-transform: uppercase;
    color: #e8c89a; margin: 0 0 6px; font-family: var(--font-mono);
  }}
  header.hero h1 {{ margin: 0 0 10px; font-size: clamp(24px, 4vw, 34px); line-height: 1.2; }}
  .kpi-strip {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }}
  .kpi-chip {{
    background: rgba(255,250,241,0.12); border: 1px solid rgba(232,200,154,0.35);
    border-radius: 999px; padding: 4px 12px; font-size: 13px; color: #f5ead6;
    font-family: var(--font-mono);
  }}
  h2 {{ font-size: clamp(18px, 2.4vw, 22px); margin: 0 0 12px; }}
  section.panel {{
    background: var(--surface); border: 1px solid var(--line);
    border-radius: var(--r-md); padding: clamp(16px, 3vw, 24px); margin-bottom: 18px;
  }}
  .exec-item {{ padding: 8px 0; border-bottom: 1px solid var(--line); }}
  .exec-item:last-child {{ border-bottom: 0; }}
  .exec-item h3 {{ margin: 0 0 4px; font-size: 15px; }}
  .exec-item p {{ margin: 0; color: var(--ink-soft); font-size: 13px; }}
  .table-scroll {{ overflow-x: auto; }}
  table {{ border-collapse: collapse; width: 100%; min-width: 760px; font-size: 13px; }}
  caption {{ text-align: left; color: var(--ink-soft); font-size: 12px; padding: 0 0 8px; }}
  th, td {{ border-bottom: 1px solid var(--line); padding: 8px 10px; text-align: left; vertical-align: top; }}
  th {{ font-size: 12px; color: var(--ink-soft); font-weight: 600; }}
  td.num {{ font-family: var(--font-mono); text-align: right; white-space: nowrap; }}
  .td-note {{ color: var(--ink-soft); font-size: 12px; margin-top: 2px; max-width: 260px; }}
  .trend {{
    display: inline-block; border-radius: 999px; padding: 1px 10px;
    font-size: 11px; font-family: var(--font-mono); background: var(--surface-soft);
  }}
  .trend-rising, .trend-spike {{ background: #f9e6e0; color: #b0402e; }}
  .trend-emerging {{ background: #fbe9d6; color: #c2701e; }}
  .trend-declining {{ background: #efece6; color: #8a857c; }}
  .trend-stable {{ background: #e9f0dd; color: #5f7a46; }}
  .comp-card {{ border: 1px solid var(--line); border-radius: var(--r-sm); padding: 12px 14px; margin-bottom: 12px; }}
  .comp-card:last-child {{ margin-bottom: 0; }}
  .comp-head, .gv-head, .act-head, .pri-head {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }}
  .comp-head h3, .act-head h3, .pri-head h3 {{ margin: 0; font-size: 15px; }}
  .c-row {{ margin: 6px 0; font-size: 13px; }}
  .c-label {{
    display: inline-block; background: var(--surface-soft); color: var(--ink-soft);
    border-radius: 4px; padding: 1px 8px; font-size: 11px; margin-right: 8px;
  }}
  .c-note {{ margin-top: 6px; color: var(--ink-soft); font-size: 12px; font-style: italic; }}
  .c-links {{ margin-top: 6px; }}
  .c-links a {{ color: var(--accent-strong); text-decoration: none; font-size: 13px; margin-right: 10px; }}
  .c-links a:hover {{ text-decoration: underline; }}
  .gv-head {{ margin-bottom: 8px; }}
  .gv-col {{ margin: 8px 0; }}
  .gv-col h4 {{ margin: 0 0 4px; font-size: 13px; color: var(--ink-soft); }}
  .gv-col ul {{ margin: 0; padding-left: 20px; font-size: 13px; }}
  .gv-action {{ margin: 10px 0 0; font-size: 13px; }}
  .pri-item {{ border-left: 4px solid var(--pc, var(--accent)); border-radius: var(--r-sm); padding: 10px 14px; margin-bottom: 10px; background: var(--surface); border-top: 1px solid var(--line); border-right: 1px solid var(--line); border-bottom: 1px solid var(--line); }}
  .pri-tag {{
    font-family: var(--font-mono); font-size: 11px; font-weight: 700;
    color: var(--pc, var(--accent));
  }}
  .pri-item p {{ margin: 6px 0 0; color: var(--ink-soft); font-size: 13px; }}
  .act-item {{ padding: 12px 0; border-bottom: 1px solid var(--line); }}
  .act-item:last-child {{ border-bottom: 0; }}
  .act-ev {{ margin: 6px 0 4px; font-size: 13px; }}
  .act-why {{ margin: 0 0 8px; color: var(--ink-soft); font-size: 13px; }}
  .act-badges {{ display: flex; flex-wrap: wrap; gap: 6px; }}
  .pill {{
    display: inline-block; background: var(--surface-soft); color: var(--ink-soft);
    border-radius: 999px; padding: 2px 10px; font-size: 11px; font-family: var(--font-mono);
  }}
  .pill-sent {{ background: #e9f0dd; color: #5f7a46; }}
  .pill-cat {{ background: #e9f0dd; color: #5f7a46; }}
  .empty {{
    background: var(--surface-soft); color: var(--ink-soft);
    border-radius: var(--r-sm); padding: 14px 16px; font-size: 14px;
  }}
  footer.foot {{ color: var(--ink-soft); font-size: 12px; line-height: 1.6; margin-top: 8px; }}
  .foot-meta {{ font-family: var(--font-mono); }}
  .foot-source {{ margin-top: 4px; }}
  a:focus-visible, summary:focus-visible {{
    outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 2px;
  }}
  @media print {{
    body {{ background: #fff; }}
    .wrap {{ max-width: none; padding: 0; }}
    section.panel, .comp-card, .pri-item {{ break-inside: avoid; }}
    a {{ color: inherit; text-decoration: underline; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <header class="hero">
    <p class="kicker">Reddit Grinder Intelligence · Weekly</p>
    <h1>磨豆机社区周报 — {report_date}</h1>
    <div class="kpi-strip">
      <span class="kpi-chip">总帖 {kpi_total}</span>
      <span class="kpi-chip">磨豆机相关 {kpi_grinder}</span>
      <span class="kpi-chip">相关占比 {kpi_ratio}</span>
      <span class="kpi-chip">Geimori 提及 {kpi_geimori}</span>
      <span class="kpi-chip">竞品提及 {kpi_comp}</span>
    </div>
  </header>

  <section class="panel" aria-label="执行摘要">
    <h2>Executive Summary</h2>
    {exec_html}
  </section>

  <section class="panel" aria-label="需求趋势">
    <h2>Demand Trends</h2>
    {trends_html}
  </section>

  <section class="panel" aria-label="竞品情报">
    <h2>Competitor Intelligence</h2>
    {comp_html}
  </section>

  <section class="panel" aria-label="品牌声音">
    <h2>Geimori Voice</h2>
    {gv_html}
  </section>

  <section class="panel" aria-label="客户优先级">
    <h2>Customer Priorities</h2>
    {pri_html}
  </section>

  <section class="panel" aria-label="下周行动">
    <h2>Next Week Actions</h2>
    {act_html}
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
    import os as _os

    _os.makedirs(output_dir, exist_ok=True)
    date_str = pulse["report_date"]
    html_path = _os.path.join(output_dir, f"reddit_weekly_{date_str}.html")
    json_path = _os.path.join(output_dir, f"reddit_weekly_{date_str}.json")
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(html_text)
    if raw_payload is not None:
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(raw_payload, fh, ensure_ascii=False, indent=2)
    return {"html": html_path, "json": json_path}


# ---------------------------------------------------------------- CLI
def _parse_args(argv: List[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Render Reddit Grinder Weekly Intelligence HTML")
    ap.add_argument("--api-base", default=os.environ.get("REDDIT_API_BASE", "http://127.0.0.1:8086"))
    ap.add_argument("--api-key", default=os.environ.get("REDDIT_API_KEY", ""))
    ap.add_argument("--report-type", default="weekly", choices=["weekly", "daily"])
    ap.add_argument("--hours", type=int, default=168)
    ap.add_argument("--report-date", default=None)
    ap.add_argument("--output-dir", default="daily_reports")
    ap.add_argument("--input-json", default=None, help="跳过 API，读取本地 payload JSON（测试用）")
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
    pulse = build_weekly_pulse(payload, report_date=args.report_date)
    html_text = render_weekly_html(pulse)
    paths = write_outputs(args.output_dir, pulse, html_text, raw_payload=payload)
    print(json.dumps({"report_date": pulse["report_date"], **paths}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
