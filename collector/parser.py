"""帖子/评论原始 JSON 标准化为入库行。

只做字段映射与类型转换，不做任何业务计算（计算架构铁律：
数据计算在阿里云，LLM 只分析不计算）。
"""
from __future__ import annotations

from typing import Any, Dict, List

# spec 第 2 节：reddit_posts 字段
POST_FIELDS = (
    "post_id",
    "subreddit",
    "title",
    "selftext",
    "created_utc",
    "score",
    "upvote_ratio",
    "num_comments",
    "permalink",
    "flair",
    "raw_json",
)

# spec 第 2 节：reddit_comments 字段
COMMENT_FIELDS = (
    "comment_id",
    "post_id",
    "body",
    "score",
    "created_utc",
    "depth",
    "raw_json",
)


def _epoch_to_utc(epoch: Any) -> Any:
    """epoch 秒 → ISO8601 UTC 字符串；非法值返回 None。"""
    if epoch is None:
        return None
    try:
        import datetime as _dt

        return _dt.datetime.fromtimestamp(int(epoch), tz=_dt.timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def parse_post(raw: Dict[str, Any], subreddit: str) -> Dict[str, Any]:
    """把 Reddit 帖子原始 JSON 标准化为 reddit_posts 行。

    Args:
        raw: listing child 的 data 字段。
        subreddit: 期望的 subreddit（防御脏数据）。

    Returns:
        可入库 dict，键与 POST_FIELDS 一致。
    """
    return {
        "post_id": str(raw.get("id") or raw.get("name") or ""),
        "subreddit": subreddit,
        "title": raw.get("title") or "",
        "selftext": raw.get("selftext") or None,
        "created_utc": _epoch_to_utc(raw.get("created_utc")),
        "score": int(raw.get("score") or 0),
        "upvote_ratio": raw.get("upvote_ratio"),
        "num_comments": int(raw.get("num_comments") or 0),
        "permalink": raw.get("permalink") or "",
        "flair": raw.get("link_flair_text") or None,
        "raw_json": raw,
    }


def parse_comment(raw: Dict[str, Any], post_id: str) -> Dict[str, Any]:
    """把 Reddit 评论原始 JSON 标准化为 reddit_comments 行。

    Args:
        raw: comment 的 data 字段。
        post_id: 所属帖子的 post_id。

    Returns:
        可入库 dict，键与 COMMENT_FIELDS 一致。
    """
    return {
        "comment_id": str(raw.get("id") or ""),
        "post_id": post_id,
        "body": raw.get("body") or "",
        "score": int(raw.get("score") or 0),
        "created_utc": _epoch_to_utc(raw.get("created_utc")),
        "depth": int(raw.get("depth") or 0),
        "raw_json": raw,
    }


def extract_comments_from_thread(thread_json: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """从帖子详情 JSON（listing 数组）提取全部评论原始 data。

    注意：当前 collector 默认从 posts listing 拉取，不拉取每个帖子的
    评论树；此函数保留给未来增强（comments.py 使用）。
    """
    out: List[Dict[str, Any]] = []

    def walk(node: Dict[str, Any]) -> None:
        kind = node.get("kind")
        data = node.get("data") or {}
        if kind == "t1":
            out.append(data)
        for child in data.get("children", []) or []:
            walk(child)
        if data.get("replies"):
            walk({"kind": "t1", "data": data["replies"]})

    for item in thread_json or []:
        if item.get("kind") == "t1":
            out.append(item.get("data", {}))
        walk(item)
    return out
