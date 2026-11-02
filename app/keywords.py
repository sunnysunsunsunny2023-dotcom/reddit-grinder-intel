"""关键词配置：磨豆机主题 / Geimori 品牌 / 竞品。

只做确定性匹配（大小写不敏感、子串匹配），不做语义计算。
语义解释交给阿里云 DeepSeek 分析模块（ADR-001）。

这些配置是启动参数不是硬编码语义，后续可按需扩展。
"""
from __future__ import annotations

from typing import Dict, List

# ------------------------------------------------------------
# 磨豆机讨论判定（帖子只要命中任一关键词即视为 grinder-related）
# ------------------------------------------------------------
GRINDER_KEYWORDS: List[str] = [
    "grinder",
    "grinding",
    "grinds",
    "burr",
    "retention",
    "static",
    "wdt",
    "rdt",
    "clump",
    "dose",
    "dosing",
    "espresso grind",
    "pourover grind",
    "grind size",
    "grind setting",
    "bean hopper",
    "hopper",
    "stepless",
    "fines",
    "distribution",
    "anti-popcorning",
]

# ------------------------------------------------------------
# 需求主题（spec 6.1 Demand Trends 列举 + 扩展）
# 每个主题 = 一组关键词，命中任一即计入该主题 mentions
# ------------------------------------------------------------
TOPIC_KEYWORDS: Dict[str, List[str]] = {
    "static": ["static", "rdt", "water droplet", "spritz", "anti-static"],
    "retention": ["retention", "retain", "leftover", "popcorning"],
    "grind_consistency": [
        "consistent", "consistency", "uniform", "uniformity", "fines",
        "particle", "distribution",
    ],
    "burr": ["burr", "flat burr", "conical burr", "burr alignment"],
    "noise": ["noise", "loud", "quiet", "sound"],
    "workflow": ["workflow", "easy", "convenient", "mess", "clean", "cup"],
    "portability": ["portable", "travel", "camping", "lightweight", "size"],
    "price_value": [
        "price", "value", "cheap", "expensive", "worth", "budget", "cost",
    ],
    "espresso": ["espresso", "espresso grind", "dial in", "dial-in", "shot"],
    "pourover": ["pourover", "pour over", "v60", "chemex", "kettle", "filter"],
    "durability": ["durable", "built", "build quality", "metal", "plastic", "quality"],
    "grind_speed": ["fast", "speed", "slow", "seconds", "quick"],
}

# ------------------------------------------------------------
# Geimori 品牌 / 产品线（spec 6.1 Geimori Voice 覆盖范围）
# ------------------------------------------------------------
GEIMORI_KEYWORDS: List[str] = [
    "geimori",
    "mywirsh",
    "wirsh",
    "gu63",
    "gu38",
    "gu64",
    "t38",
    "t38 plus",
    "t38 battery gen2",
]

# ------------------------------------------------------------
# 竞品品牌/机型（mention share 统计用，禁止说成市场份额）
# ------------------------------------------------------------
COMPETITOR_KEYWORDS: Dict[str, List[str]] = {
    "1zpresso": ["1zpresso", "1z espresso", "zp6", "x-pro", "q2", "k-ultra", "j-ultra"],
    "comandante": ["comandante", "c40", "c60"],
    "timemore": ["timemore", "chestnut", "slim plus", "s3", "x3"],
    "kingrinder": ["kingrinder", "k6", "k4", "p1", "p2"],
    "fellow": ["fellow", "opus", "ode"],
    "baratza": ["baratza", "encore", "virtuoso", "vario", "forte", "sette"],
    "df_grinders": ["df54", "df64", "df83", "df64 gen2", "df64gen2"],
    "niche": ["niche", "niche zero"],
    "lagom": ["lagom", "p64", "p100", "mini"],
    "mazzer": ["mazzer", "philos", "super jolly"],
    "eureka": ["eureka", "specialita", "mignon", "filtro", "silenzio"],
    "wilfa": ["wilfa", "uniform"],
    "kinu": ["kinu", "m47"],
    "lido": ["lido", "lido 3"],
    "weber": ["weber", "lyn weber", "hg-1", "workshop"],
    "pietro": ["pietro"],
    "heihox": ["heihox"],
}

# ------------------------------------------------------------
# 情感词（仅用于非常粗略的辅助计数；准确情感由 LLM 判定）
# ------------------------------------------------------------
POSITIVE_WORDS: List[str] = [
    "love", "great", "excellent", "amazing", "best", "perfect", "happy",
    "satisfied", "impressed", "solid", "flawless",
]
NEGATIVE_WORDS: List[str] = [
    "bad", "terrible", "awful", "worst", "hate", "disappointed", "broken",
    "defective", "return", "refund", "complaint", "noise", "static",
    "frustrating", "problem", "issue", "fail", "fails", "lemon",
]


import re

# Geimori 品牌词：几乎无歧义，词边界匹配即可
GEIMORI_BRAND_KEYWORDS: List[str] = [
    "geimori",
    "mywirsh",
    "wirsh",
]

# Geimori 机型词：歧义大（Mikuni T38 化油器、Yamaha Virago 等），
# 需词边界 + 排除机械/化油器语境
GEIMORI_MODEL_KEYWORDS: List[str] = [
    "gu63",
    "gu38",
    "gu64",
    "t38",
    "t38 plus",
    "t38 battery gen2",
]

# 机械/化油器/摩托车语境词：命中任一词时，机型词不作为 Geimori 提及
# （RGI-016：Mikuni T38 化油器帖曾被 "t38" 子串误判为品牌提及）
MECHANICAL_AMBIGUITY_TERMS: List[str] = [
    "mikuni",
    "carburetor",
    "carburettor",
    "virago",
    "yamaha",
    "motorcycle",
    "throttle",
    "bogging",
    "pilot jet",
    "main jet",
]

_BRAND_PATTERNS = [
    re.compile(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", re.IGNORECASE)
    for k in GEIMORI_BRAND_KEYWORDS
]
_MODEL_PATTERNS = [
    re.compile(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", re.IGNORECASE)
    for k in GEIMORI_MODEL_KEYWORDS
]


def normalize(text: str) -> str:
    """小写化用于子串匹配。"""
    return (text or "").lower()


def has_geimori(text: str) -> bool:
    """Geimori 品牌/机型提及判定（RGI-016）。

    - 品牌词（geimori / mywirsh / wirsh）：词边界匹配，几乎无歧义。
    - 机型词（gu63 / gu38 / gu64 / t38 ...）：词边界匹配，且文本不含
      化油器/摩托车机械语境词（防止 "Mikuni T38" 化油器帖被误判）。
    """
    t = normalize(text)
    if any(p.search(t) for p in _BRAND_PATTERNS):
        return True
    if any(term in t for term in MECHANICAL_AMBIGUITY_TERMS):
        return False
    return any(p.search(t) for p in _MODEL_PATTERNS)


def count_keywords(text: str, keywords: List[str]) -> int:
    """统计命中关键词数量（同一词多次出现只计 1）。"""
    t = normalize(text)
    return sum(1 for kw in keywords if kw in t)


def has_any(text: str, keywords: List[str]) -> bool:
    t = normalize(text)
    return any(kw in t for kw in keywords)
