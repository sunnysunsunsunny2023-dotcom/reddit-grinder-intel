"""reply_store 测试：任务幂等 / 分类更新 / 草稿版本 / 队列 / 统计。"""
from __future__ import annotations

import datetime as dt

from collector import storage
from app import reply_store


def _conn(tmp_path):
    conn = storage.connect(str(tmp_path / "t.db"))
    storage.init_schema(conn, "sql/schema.sql")
    return conn


def test_create_task_idempotent(tmp_path):
    conn = _conn(tmp_path)
    t1 = reply_store.create_task(
        conn, "reddit", "pourover", "p1", "c1",
        "broken grinder", context="GU63 review", product="GU63",
    )
    t2 = reply_store.create_task(
        conn, "reddit", "pourover", "p1", "c1",
        "broken grinder", context="GU63 review", product="GU63",
    )
    assert t1["task_id"] == t2["task_id"]
    total = conn.execute("SELECT COUNT(*) FROM reply_tasks").fetchone()[0]
    assert total == 1
    conn.close()


def test_update_classification_and_draft(tmp_path):
    conn = _conn(tmp_path)
    t = reply_store.create_task(conn, "reddit", "pourover", "p1", "c1", "broken")
    reply_store.update_task_classification(
        conn, t["task_id"],
        {"reply_needed": True, "priority": "P1", "category": "product_complaint",
         "product": "GU63"},
    )
    v = reply_store.insert_draft(
        conn, t["task_id"], "Thanks for flagging...", "deepseek",
        "deepseek-chat", "v1", "v1",
    )
    assert v == 1
    v2 = reply_store.insert_draft(
        conn, t["task_id"], "Draft two", "deepseek", "deepseek-chat", "v1", "v1",
    )
    assert v2 == 2
    reply_store.mark_draft_ready(conn, t["task_id"])

    latest = reply_store.latest_draft(conn, t["task_id"])
    assert latest["draft_version"] == 2
    assert latest["draft_text"] == "Draft two"

    q = reply_store.queue_tasks(conn, status="draft_ready")
    assert len(q) == 1
    assert q[0]["priority"] == "P1"
    conn.close()


def test_stats(tmp_path):
    conn = _conn(tmp_path)
    t = reply_store.create_task(conn, "reddit", "espresso", "p2", "c9", "help me")
    reply_store.update_task_classification(
        conn, t["task_id"],
        {"reply_needed": True, "priority": "P1", "category": "product_complaint",
         "product": None},
    )
    s = reply_store.stats(conn)
    assert s["total_tasks"] == 1
    assert s["p1_complaints"] == 1
    assert s["by_status"].get("classified") == 1
    conn.close()


def test_find_task_missing(tmp_path):
    conn = _conn(tmp_path)
    assert reply_store.find_task(conn, "reddit", "p9", "c9") is None
    conn.close()
