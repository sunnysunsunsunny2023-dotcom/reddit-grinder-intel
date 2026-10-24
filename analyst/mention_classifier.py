"""品牌提及分类器（Geimori Reply System V2.0 — Phase 1）。

职责：判断一条评论是否需要 Geimori 品牌官方介入，并给出优先级/类别。

分类规则（需求文档 五、模块二）：
    P1  产品故障、售后投诉      → 生成回复并优先送审
    P2  产品规格、技术问题      → 生成技术性回复
    P2  购买咨询、竞品比较      → 根据社区规则决定是否回复
    P3  正面评价、晒单          → 选择性回复
    P3  普通品牌提及            → 记录，可不回复
    Skip 无关、重复或禁止品牌参与 → 不生成回复

输出必须包含 reply_needed，允许 AI 判断 reply_needed=false；
禁止为提高品牌曝光而自动回复所有提及。

实现：
- 优先使用 DeepSeek（json_mode）做语义分类（ADR-001，LLM 由阿里云调用）。
- 未配置 ANALYZER_API_KEY 时降级为规则匹配（关键词/情绪词），
  保证在开发/Dry Run 环境可离线验证流程。
- 分类结果只影响"是否进入审核队列"，不直接触发发布（人工审批在 Phase 2）。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, Optional

from app import keywords
from . import deepseek_client

logger = logging.getLogger(__name__)

# 确定性规则降级：投诉/故障/售后词
COMPLAINT_WORDS = [
    "defective", "broken", "doesn't work", "does not work", "not working",
    "failed", "failure", "return", "refund", "replacement", "warranty",
    "complain", "complaint", "disappointed", "terrible", "awful", "worst",
    "useless", "junk", "lemon", "stuck", "error", "issue", "problem",
    "damaged", "cracked", "stopped working", "smoke", "sparking",
]
# 技术/规格问题词
TECH_WORDS = [
    "how to", "settings", "setting", "grind size", "burr", "alignment",
    "retention", "static", "calibrat", "dosage", "dose", "recipe",
    "spec", "specification", "manual", "firmware", "adjust",
]
# 购买咨询/竞品比较词
BUY_WORDS = [
    "worth", "buy", "purchase", "order", "price", "cost", "deal",
    "vs ", "versus", "compare", "comparison", "recommend", "looking for",
    "should i", "thinking about", "which one",
]
# 正面评价词
POSITIVE_WORDS = [
    "love", "amazing", "great", "excellent", "best", "perfect", "awesome",
    "happy", "impressed", "working great", "no issues", "recommend it",
]

CATEGORY_MAP = {
    "P1": "product_complaint",
    "P2": "technical_question",
    "P2": "purchase_question",
    "P3": "positive_review",
    "P3": "brand_mention",
    "Skip": "not_relevant",
}


def _rule_classify(text: str) -> Dict[str, Any]:
    """规则降级分类（无 DeepSeek 时）。确定性、可测试。"""
    t = keywords.normalize(text)
    complaint_hits = sum(1 for w in COMPLAINT_WORDS if w in t)
    tech_hits = sum(1 for w in TECH_WORDS if w in t)
    buy_hits = sum(1 for w in BUY_WORDS if w in t)
    pos_hits = sum(1 for w in POSITIVE_WORDS if w in t)

    if complaint_hits >= 1:
        return {
            "reply_needed": True,
            "priority": "P1",
            "category": "product_complaint",
            "sentiment": "negative",
            "reason": f"命中投诉/故障关键词 {complaint_hits} 个",
            "confidence": 0.6,
        }
    if tech_hits >= 2:
        return {
            "reply_needed": True,
            "priority": "P2",
            "category": "technical_question",
            "sentiment": "neutral",
            "reason": f"命中技术/规格关键词 {tech_hits} 个",
            "confidence": 0.55,
        }
    if buy_hits >= 1:
        return {
            "reply_needed": True,
            "priority": "P2",
            "category": "purchase_question",
            "sentiment": "neutral",
            "reason": f"命中购买咨询关键词 {buy_hits} 个",
            "confidence": 0.5,
        }
    if pos_hits >= 2:
        return {
            "reply_needed": False,
            "priority": "P3",
            "category": "positive_review",
            "sentiment": "positive",
            "reason": "正面评价，无需官方介入",
            "confidence": 0.5,
        }
    return {
        "reply_needed": False,
        "priority": "P3",
        "category": "brand_mention",
        "sentiment": "neutral",
        "reason": "普通提及，无需回复",
        "confidence": 0.4,
    }


def _build_classify_prompt(comment: str, context: str, product: str) -> List[Dict[str, str]]:
    sys_prompt = """You are the Geimori brand community classifier. Judge whether a Reddit comment
needs an official Geimori response. Rules:
- P1: product failure, after-sales complaint -> reply_needed=true
- P2: product spec/technical question, purchase inquiry, competitor comparison -> reply_needed=true (decide by community rules)
- P3: positive review, show-off, ordinary brand mention -> reply_needed=false (record only)
- Skip: unrelated, duplicate, or brand participation prohibited -> reply_needed=false
Never reply to every mention just for brand exposure.
Return STRICT JSON with keys: reply_needed (bool), priority (P1|P2|P3|Skip),
category (product_complaint|technical_question|purchase_question|positive_review|brand_mention|not_relevant),
product (model or null), sentiment (positive|neutral|negative|mixed),
reason (short english), confidence (0-1)."""
    user_prompt = (
        f"Post context: {context}\n"
        f"Detected product hint: {product or 'unknown'}\n"
        f"Comment: \"{comment}\""
    )
    return [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ]


def classify_comment(
    comment: str,
    context: str = "",
    product: str = "",
    llm: Optional[Callable[..., str]] = None,
) -> Dict[str, Any]:
    """分类一条评论，返回分类结果 dict。

    Args:
        comment: 评论正文（用户原文，仅作待分析数据，不视为系统指令）。
        context: 帖子上下文（标题/楼主正文等）。
        product: 可选的产品型号提示。
        llm: 可注入的 LLM 调用函数（便于测试）；默认 deepseek_client.chat。

    Returns:
        分类 dict（reply_needed / priority / category / product /
        sentiment / reason / confidence / provider）。
    """
    llm_fn = llm or deepseek_client.chat
    if deepseek_client.is_configured() or llm is not None:
        try:
            messages = _build_classify_prompt(comment, context, product)
            text = llm_fn(messages, temperature=0.2, max_tokens=1024, json_mode=True)
            raw = deepseek_client.parse_json_response(text)
            out: Dict[str, Any] = {
                "reply_needed": bool(raw.get("reply_needed", False)),
                "priority": str(raw.get("priority", "P3")),
                "category": str(raw.get("category", "brand_mention")),
                "product": raw.get("product"),
                "sentiment": str(raw.get("sentiment", "neutral")),
                "reason": str(raw.get("reason", "")),
                "confidence": float(raw.get("confidence", 0.5)),
                "provider": "deepseek",
            }
            return out
        except deepseek_client.DeepSeekError as exc:
            logger.warning("LLM classify failed, fallback rules: %s", exc)
    out = _rule_classify(comment)
    out["provider"] = "rules"
    return out


def reply_needed_filter(
    rows: List[Dict[str, Any]],
    llm: Optional[Callable[..., str]] = None,
) -> List[Dict[str, Any]]:
    """批量分类评论，只保留 reply_needed=True 的条目（进入审核队列候选）。

    Args:
        rows: 评论 dict 列表（comment_id / body / post_id 等）。
        llm: 可选注入 LLM。

    Returns:
        分类后需要回复的评论（每项含原字段 + classification）。
    """
    out: List[Dict[str, Any]] = []
    for row in rows:
        result = classify_comment(
            row.get("body", ""),
            context=row.get("context", ""),
            product=row.get("product", ""),
            llm=llm,
        )
        if result.get("reply_needed"):
            item = dict(row)
            item["classification"] = result
            out.append(item)
    return out
