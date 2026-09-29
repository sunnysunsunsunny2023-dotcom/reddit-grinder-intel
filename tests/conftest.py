"""共享测试 fixtures：mock 帖子数据（不真拉 Reddit，不真推飞书）。"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List

import pytest


def make_post(
    post_id: str,
    title: str,
    selftext: str = "",
    subreddit: str = "pourover",
    score: int = 10,
    comments: int = 3,
    permalink: str = "/r/pourover/comments/abc/title/",
    created: dt.datetime | None = None,
) -> Dict[str, Any]:
    return {
        "post_id": post_id,
        "subreddit": subreddit,
        "title": title,
        "selftext": selftext,
        "created_utc": created or dt.datetime.now(dt.timezone.utc),
        "score": score,
        "num_comments": comments,
        "permalink": permalink,
    }


@pytest.fixture
def sample_posts() -> List[Dict[str, Any]]:
    """5 个样本帖：4 个磨豆机相关 + 1 个无关。"""
    return [
        make_post(
            "p1", "Static is driving me crazy with my new grinder",
            "RDT helps but still clumps. retention is bad. "
            "1zpresso zp6 vs comandante?",
            score=120, comments=45,
        ),
        make_post(
            "p2", "Just got my Geimori GU63",
            "love it, quiet, consistent grind, no retention at all",
            score=80, comments=20,
        ),
        make_post(
            "p3", "DF64 vs Niche Zero for espresso",
            "dial in shots, burr alignment issues, grinder noise",
            subreddit="espresso", score=60, comments=30,
        ),
        make_post(
            "p4", "Camping trip grinder",
            "need portable lightweight, timemore chestnut slim",
            score=15, comments=4,
        ),
        make_post(
            "p5", "My cat knocked over my kettle",
            "totally unrelated, just sad",
            score=5, comments=2,
        ),
    ]
