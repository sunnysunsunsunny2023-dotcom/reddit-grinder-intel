-- ============================================================
-- Reddit Grinder Intelligence — PostgreSQL Schema
-- 唯一数据源 (Single Source of Truth)
-- 依据 docs/COZE_DAILY_WEEKLY_SPEC.md 第 2 节
-- ADR-001: LLM 分析（DeepSeek）在阿里云侧执行
-- ============================================================

BEGIN;

-- ------------------------------------------------------------
-- 帖子表
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reddit_posts (
    post_id       TEXT PRIMARY KEY,
    subreddit     TEXT NOT NULL,
    title         TEXT NOT NULL,
    selftext      TEXT,
    created_utc   TIMESTAMPTZ NOT NULL,
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    score         INTEGER NOT NULL DEFAULT 0,
    upvote_ratio  NUMERIC(5,4),
    num_comments  INTEGER NOT NULL DEFAULT 0,
    permalink     TEXT NOT NULL,
    flair         TEXT,
    raw_json      JSONB NOT NULL DEFAULT '{}'::jsonb
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
    created_utc   TIMESTAMPTZ NOT NULL,
    depth         INTEGER NOT NULL DEFAULT 0,
    raw_json      JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_reddit_comments_post_id
    ON reddit_comments (post_id);

-- ------------------------------------------------------------
-- 抓取运行记录
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fetch_runs (
    run_id         BIGSERIAL PRIMARY KEY,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ,
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
    batch_id         BIGSERIAL PRIMARY KEY,
    report_type      TEXT NOT NULL
                     CHECK (report_type IN ('daily','weekly')),
    period_start     TIMESTAMPTZ NOT NULL,
    period_end       TIMESTAMPTZ NOT NULL,
    post_count       INTEGER NOT NULL DEFAULT 0,
    analysis_version TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending','running','completed','failed')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at     TIMESTAMPTZ
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
    batch_id         BIGINT NOT NULL REFERENCES analysis_batches(batch_id) ON DELETE CASCADE,
    post_id          TEXT NOT NULL REFERENCES reddit_posts(post_id) ON DELETE CASCADE,
    analysis_version TEXT NOT NULL,
    analysis_status  TEXT NOT NULL DEFAULT 'pending'
                     CHECK (analysis_status IN ('pending','analyzed','skipped','failed')),
    analyzed_at      TIMESTAMPTZ,
    PRIMARY KEY (batch_id, post_id)
);

-- ------------------------------------------------------------
-- 分析结果（阿里云 DeepSeek LLM 产物，JSONB 存储）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS analysis_results (
    batch_id     BIGINT PRIMARY KEY REFERENCES analysis_batches(batch_id) ON DELETE CASCADE,
    report_type  TEXT NOT NULL,
    period_start TIMESTAMPTZ NOT NULL,
    period_end   TIMESTAMPTZ NOT NULL,
    result_json  JSONB NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------
-- JSONL 只允许 backup/export，不作为生产主数据。
-- ------------------------------------------------------------

COMMIT;
