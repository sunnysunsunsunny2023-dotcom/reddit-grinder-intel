"""趋势分类规则（spec 第 7 节）。

全部是确定性规则函数，输入必须是阿里云算好的数值；
LLM 只负责解释（ADR-001）。不要把一天一篇爆帖标成 structural。
"""
from __future__ import annotations

from typing import Tuple

# ---- 阈值（可调，全部集中于此）----
SPIKE_RATIO_7D = 3.0          # 24h >= 3x avg_7d → spike
RISING_RATIO_WEEK = 1.2        # 环比上周 >= 1.2x
RISING_RATIO_30D = 1.2         # 对比 30d baseline >= 1.2x
DECLINING_RATIO_WEEK = 0.8     # 环比上周 <= 0.8x
STRUCTURAL_MIN_WEEKLY = 10.0   # 周 mentions 下限
STRUCTURAL_MIN_UNIQUE = 5      # 周 unique posts 下限
SPIKE_MIN_EVIDENCE = 2         # spike 最少 evidence（帖数）
WEAK_EVIDENCE = 3              # 样本小于等于此数 → weak signal


def classify_trend(
    current: float,
    prev_week: float,
    avg_30d: float,
    evidence_count: int = 0,
    unique_posts: int = 0,
    weeks_high: int = 1,
) -> str:
    """按 spec 7 返回趋势状态。

    Args:
        current: 当前窗口 mentions。
        prev_week: 上一周 mentions。
        avg_30d: 30 天日均 mentions（调用方按需乘 7 转周均）。
        evidence_count: 当前窗口证据帖数。
        unique_posts: 当前窗口独立发帖数。
        weeks_high: 连续保持高频的周数（默认 1，供 structural 判定）。
    """
    if current <= 0:
        return "stable"

    # structural_priority：连续多周高频 + 足够 unique posts / engagement
    if (
        weeks_high >= 2
        and current >= STRUCTURAL_MIN_WEEKLY
        and unique_posts >= STRUCTURAL_MIN_UNIQUE
    ):
        return "structural_priority"

    # spike：短期异常暴涨，持续性未知
    if (
        avg_7d := max(avg_30d, 0.001)
    ) and current >= SPIKE_RATIO_7D * avg_7d and evidence_count >= SPIKE_MIN_EVIDENCE:
        return "spike"

    # rising：相比上周和 30d baseline 均明显上升
    if prev_week > 0 and avg_30d > 0:
        if (
            current >= prev_week * RISING_RATIO_WEEK
            and current >= avg_30d * RISING_RATIO_30D
        ):
            return "rising"

    # declining：持续下降
    if prev_week > 0 and avg_30d > 0:
        if current <= prev_week * DECLINING_RATIO_WEEK and prev_week <= avg_30d:
            return "declining"

    return "stable"


def classify_signal(
    mentions_24h: float,
    avg_7d: float,
    avg_30d: float,
    evidence_count: int,
) -> Tuple[str, str]:
    """返回 (status, confidence)，供 Daily Pulse Signal Alerts 使用。

    当样本过小时，status 保持规则判定但 confidence 降为 low/medium，
    并在报告层标注 weak signal（spec 第 8 节）。
    """
    if mentions_24h <= 0 or avg_7d <= 0:
        return "stable", "high"

    status = classify_trend(
        mentions_24h,
        avg_7d * 7.0,  # 把 7d 日均转成周口径与 current 比较
        avg_30d,
        evidence_count=evidence_count,
        unique_posts=evidence_count,
        weeks_high=1,
    )

    if evidence_count <= WEAK_EVIDENCE:
        confidence = "low"
    elif evidence_count <= 8:
        confidence = "medium"
    else:
        confidence = "high"
    return status, confidence


def needs_alert(status: str) -> bool:
    """Daily Pulse 只突出真正异常变化（spec 5.1.C）。"""
    return status in {"spike", "emerging", "rising", "declining"}
