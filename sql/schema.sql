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

-- ------------------------------------------------------------
-- Geimori Reply System V2.0 — Phase 1：回复任务/草稿/审批/发布
-- （新增四张表，不影响现有 Daily/Weekly 六张表）
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS reply_tasks (
    task_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    platform       TEXT NOT NULL DEFAULT 'reddit',
    subreddit      TEXT,
    post_id        TEXT NOT NULL,
    comment_id     TEXT NOT NULL,
    original_text  TEXT NOT NULL,
    context        TEXT,
    permalink      TEXT,
    product        TEXT,
    category       TEXT,
    priority       TEXT CHECK (priority IN ('P1','P2','P3','Skip')),
    reply_needed   INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'new'
                   CHECK (status IN ('new','classified','draft_ready',
                                     'pending_approval','approved','rejected',
                                     'skipped','published','failed')),
    created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (platform, post_id, comment_id)
);

CREATE INDEX IF NOT EXISTS idx_reply_tasks_status
    ON reply_tasks (status, priority);
CREATE INDEX IF NOT EXISTS idx_reply_tasks_post
    ON reply_tasks (post_id);

CREATE TABLE IF NOT EXISTS reply_drafts (
    draft_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id           INTEGER NOT NULL REFERENCES reply_tasks(task_id) ON DELETE CASCADE,
    draft_text        TEXT NOT NULL,
    reason            TEXT,
    model_provider    TEXT NOT NULL DEFAULT 'deepseek',
    model_name        TEXT,
    brand_voice_version TEXT NOT NULL DEFAULT 'v1',
    knowledge_version TEXT NOT NULL DEFAULT 'v1',
    draft_version     INTEGER NOT NULL DEFAULT 1,
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_reply_drafts_task
    ON reply_drafts (task_id, draft_version);

CREATE TABLE IF NOT EXISTS reply_approvals (
    approval_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       INTEGER NOT NULL REFERENCES reply_tasks(task_id) ON DELETE CASCADE,
    draft_version INTEGER NOT NULL DEFAULT 1,
    reviewer_id   TEXT,
    reviewer_name TEXT,
    action        TEXT NOT NULL
                  CHECK (action IN ('approve','edit_approve','regenerate',
                                    'reject','skip')),
    approved_text TEXT,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','effective','superseded','rejected')),
    approved_at   TIMESTAMP,
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_reply_approvals_task
    ON reply_approvals (task_id, status);

CREATE TABLE IF NOT EXISTS reply_publications (
    publication_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id                INTEGER NOT NULL REFERENCES reply_tasks(task_id) ON DELETE CASCADE,
    approved_draft_version INTEGER NOT NULL,
    reddit_account         TEXT,
    target_comment_id      TEXT,
    reddit_reply_id        TEXT,
    published_text         TEXT,
    published_url          TEXT,
    status                 TEXT NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending','published','failed',
                                             'no_permission','skipped')),
    error_message          TEXT,
    published_at           TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_reply_publications_task
    ON reply_publications (task_id, status);
