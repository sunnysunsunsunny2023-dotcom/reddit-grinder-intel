-- ============================================================
-- Reddit Grinder Intelligence — SQLite Schema
-- 唯一数据源 (Single Source of Truth)
-- 依据 docs/COZE_DAILY_WEEKLY_SPEC.md 第 2 节
-- ADR-001: LLM 分析（DeepSeek）在阿里云侧执行
-- ADR-002: 存储由 PostgreSQL 改为 SQLite（单机低频、服务器 409Mi 内存、零运维）
-- ============================================================

PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------
-- 帖子表
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reddit_posts (
    post_id       TEXT PRIMARY KEY,
    subreddit     TEXT NOT NULL,
    title         TEXT NOT NULL,
    selftext      TEXT,
    created_utc   TIMESTAMP NOT NULL,
    fetched_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    score         INTEGER NOT NULL DEFAULT 0,
    upvote_ratio  REAL,
    num_comments  INTEGER NOT NULL DEFAULT 0,
    permalink     TEXT NOT NULL,
    flair         TEXT,
    raw_json      TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_reddit_posts_subreddit_created
    ON reddit_posts (subreddit, created_utc DESC);

CREATE INDEX IF NOT EXISTS idx_reddit_posts_created
    ON reddit_posts (created_utc DESC);

-- ------------------------------------------------------------
-- 评论表
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reddit_comments (
    comment_id    TEXT PRIMARY KEY,
    post_id       TEXT NOT NULL REFERENCES reddit_posts(post_id) ON DELETE CASCADE,
    body          TEXT NOT NULL,
    score         INTEGER NOT NULL DEFAULT 0,
    created_utc   TIMESTAMP NOT NULL,
    depth         INTEGER NOT NULL DEFAULT 0,
    raw_json      TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_reddit_comments_post_id
    ON reddit_comments (post_id);

-- ------------------------------------------------------------
-- 抓取运行记录
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fetch_runs (
    run_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at    TIMESTAMP,
    subreddit      TEXT NOT NULL,
    posts_found    INTEGER NOT NULL DEFAULT 0,
    posts_new      INTEGER NOT NULL DEFAULT 0,
    comments_found INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'running'
                   CHECK (status IN ('running','completed','failed')),
    error          TEXT
);

CREATE INDEX IF NOT EXISTS idx_fetch_runs_started
    ON fetch_runs (started_at DESC);

-- ------------------------------------------------------------
-- 分析批次（daily / weekly 独立批次）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS analysis_batches (
    batch_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    report_type      TEXT NOT NULL
                     CHECK (report_type IN ('daily','weekly')),
    period_start     TIMESTAMP NOT NULL,
    period_end       TIMESTAMP NOT NULL,
    post_count       INTEGER NOT NULL DEFAULT 0,
    analysis_version TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending','running','completed','failed')),
    created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at     TIMESTAMP
);

-- 同一 report_type + 同一周期 + 同一 analysis_version 不重复分析
CREATE UNIQUE INDEX IF NOT EXISTS uq_analysis_batches_report_period_version
    ON analysis_batches (report_type, period_start, analysis_version);

CREATE INDEX IF NOT EXISTS idx_analysis_batches_report_period
    ON analysis_batches (report_type, period_start DESC);

-- ------------------------------------------------------------
-- 分析明细（每个 post 在批次内的分析状态）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS analysis_items (
    batch_id         INTEGER NOT NULL REFERENCES analysis_batches(batch_id) ON DELETE CASCADE,
    post_id          TEXT NOT NULL REFERENCES reddit_posts(post_id) ON DELETE CASCADE,
    analysis_version TEXT NOT NULL,
    analysis_status  TEXT NOT NULL DEFAULT 'pending'
                     CHECK (analysis_status IN ('pending','analyzed','skipped','failed')),
    analyzed_at      TIMESTAMP,
    PRIMARY KEY (batch_id, post_id)
);

-- ------------------------------------------------------------
-- 分析结果（阿里云 DeepSeek LLM 产物，JSON 文本存储）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS analysis_results (
    batch_id     INTEGER PRIMARY KEY REFERENCES analysis_batches(batch_id) ON DELETE CASCADE,
    report_type  TEXT NOT NULL,
    period_start TIMESTAMP NOT NULL,
    period_end   TIMESTAMP NOT NULL,
    result_json  TEXT NOT NULL,
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ------------------------------------------------------------
-- JSONL 只允许 backup/export，不作为生产主数据。
-- ------------------------------------------------------------
