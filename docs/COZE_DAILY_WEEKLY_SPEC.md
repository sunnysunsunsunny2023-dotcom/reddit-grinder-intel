# Coze Implementation Spec — Daily Pulse + Weekly Intelligence

> 目的：把 Reddit Grinder Intelligence 从“每日摘要”升级为“可追溯、可比较、可执行”的情报系统。
>
> 技术原则：**阿里云负责抓取、存储、去重、统计、趋势计算、状态与 LLM 分析（DeepSeek 由阿里云调用）；Coze 只负责报告渲染（HTML）和飞书推送。**

> **ADR-001（2026-09-29）：LLM 分析由阿里云调用 DeepSeek。**
> 原 spec 中 Coze 负责的 LLM 语义分析全部移至阿里云侧（与 youtube-kol-monitor 同模式，`ANALYZER_*` 配置走 GitHub Secrets，不在代码库落明文）；Coze 只保留 HTML 渲染与飞书推送。所有“LLM 输出纪律”（第 9 节）对阿里云侧的 DeepSeek 分析模块同样适用。

## 1. 系统边界

### 阿里云负责

- Reddit 数据抓取
- 帖子/评论标准化
- 去重
- PostgreSQL 持久化
- fetch 状态
- analysis batch 状态
- 24h / 7d / 30d 时间窗口聚合
- mentions / engagement / trend 等确定性计算
- **LLM 分析（DeepSeek，阿里云侧调用）**：磨豆机需求分类、Geimori 品牌反馈、竞品洞察、Marketing angles、Blog topics、Product insights
- 对 Coze 提供稳定内部 API（含 analysis JSON 产出）

### Coze 负责

- 获取阿里云已整理好的分析批次与分析结果
- Daily HTML / Weekly HTML 渲染
- 飞书卡片推送

### 明确禁止

- 不让 Coze 调用 LLM 做语义分析（LLM 统一由阿里云调用 DeepSeek）
- 不让 Coze 自己维护“哪些帖子分析过”
- 不让 Coze 自己做 mentions、比例、环比、趋势等数学计算
- 不用本地 JSON state 文件作为生产主状态
- 不允许同一份日报 schema 简单扩大时间范围后当周报
- 不允许 LLM 在证据不足时生成确定性“市场结论”

---

## 2. 数据层：PostgreSQL 为 Single Source of Truth

建议至少包含：

### reddit_posts

```text
post_id PK
subreddit
title
selftext
created_utc
fetched_at
score
upvote_ratio
num_comments
permalink
flair
raw_json JSONB
```

### reddit_comments

```text
comment_id PK
post_id FK
body
score
created_utc
depth
raw_json JSONB
```

### fetch_runs

```text
run_id
started_at
finished_at
subreddit
posts_found
posts_new
comments_found
status
error
```

### analysis_batches

```text
batch_id
report_type       # daily / weekly
period_start
period_end
post_count
analysis_version
status            # pending / running / completed / failed
created_at
completed_at
```

可增加：

### analysis_items

```text
batch_id
post_id
analysis_version
analysis_status
analyzed_at
```

JSONL 只允许做 backup/export，不作为生产主数据。

---

## 3. Reddit Collector 设计

抓取逻辑必须和业务分析解耦。

建议目录：

```text
collector/
  reddit_fetcher.py
  parser.py
  comments.py
  dedupe.py
  storage.py
  scheduler.py
```

当前 Reddit Data API 申请未获批，因此**不要把官方 Data API/OAuth 作为本项目唯一前提**。保持 fetcher 可替换，当前已验证可用的获取方式独立封装；未来访问方式变化时，只替换 fetcher，不影响数据库、统计层、Coze 或报告。

不要把 Reddit 获取逻辑写死在 Coze workflow 中。

---

## 4. 阿里云 API

### GET /api/reddit/analysis-batch

建议参数：

```text
report_type=daily|weekly
start=
end=
hours=
subreddit=
limit=
analysis_version=
```

返回必须同时包含：

```json
{
  "batch_id": "...",
  "period": {
    "start": "...",
    "end": "..."
  },
  "posts": [],
  "statistics": {},
  "baseline_7d": {},
  "baseline_30d": {},
  "topic_trends": {},
  "brand_trends": {},
  "competitor_trends": {}
}
```

### POST /api/reddit/analysis-complete

Coze 完成后回写：

```json
{
  "batch_id": "...",
  "analysis_version": "v1",
  "status": "completed"
}
```

避免重复分析。

---

# 5. Daily Pulse

日报不是“完整市场报告”，而是**异常检测 + 当日行动提示**。

目标问题：

> 过去 24 小时发生了什么？今天有什么值得关注或行动？

建议名称：

**Reddit Grinder Daily Pulse**

## 5.1 日报必须包含

### A. Today in one sentence

只写 1–2 句。

例如：

> Static discussion jumped sharply today, driven by two high-engagement single-dose grinder threads; no meaningful Geimori brand risk detected.

### B. KPI

- total_new_posts
- grinder_related_posts
- grinder_ratio
- geimori_mentions
- competitor_mentions
- high_signal_topics
- alert_count

### C. Signal Alerts

只突出真正异常变化。

每个 signal 至少包含：

```json
{
  "topic": "static",
  "mentions_24h": 7,
  "avg_7d": 2.1,
  "avg_30d": 1.8,
  "vs_7d": 3.33,
  "vs_30d": 3.89,
  "status": "spike",
  "confidence": "high",
  "evidence_count": 7
}
```

日报重点是：

- spike
- emerging
- sudden negative brand mention
- competitor launch / issue
- unusually high engagement thread

不要每天重复长期不变的“静电、低残留、价格”常识。

### D. Top Posts

最多 3–5 篇。

字段：

- post_id
- title
- subreddit
- score
- comments
- topic
- why_it_matters
- permalink

### E. Geimori Watch

如果没有品牌提及：

> No meaningful Geimori mention in the last 24 hours.

不要为了填报告硬写。

如果有：

- product
- sentiment
- issue/theme
- evidence
- severity
- recommended_response

### F. Today Actions

最多 3 条。

分类可包含：

- Ad
- Content
- Product
- Community

每条必须包含：

```json
{
  "action": "...",
  "why": "...",
  "evidence_count": 5,
  "confidence": "medium"
}
```

如果没有强信号，允许输出：

> No immediate action required today.

这是正确结果，不要强行生成建议。

---

# 6. Weekly Intelligence

周报不是 7 份日报拼接。

目标问题：

> 这一周用户需求、竞争格局和品牌反馈发生了什么变化？下周我们应该优先做什么？

建议名称：

**Reddit Grinder Weekly Intelligence**

比较窗口：

- Current week
- Previous week
- 30-day baseline

## 6.1 周报固定结构

### 1. Executive Summary

控制在 5 条以内：

- 最大上升趋势
- 最大下降趋势
- 最大竞品变化
- Geimori 品牌变化
- 最重要的下周行动

### 2. Demand Trends

按主题聚合，例如：

- static
- retention
- grind consistency
- burr
- noise
- workflow
- portability
- price/value
- espresso
- pourover

每项必须包含：

```json
{
  "topic": "static",
  "mentions": 31,
  "previous_week": 18,
  "wow_change_pct": 72.2,
  "share_of_grinder_discussion": 0.246,
  "avg_30d": 19.4,
  "trend": "rising",
  "confidence": "high"
}
```

### 3. Competitor Intelligence

按品牌/机型：

- mentions
- WoW
- sentiment
- common praise
- common complaints
- high-engagement threads
- potential opportunity

禁止只按绝对 mentions 排名后直接说“市场份额”。

应明确这是：

> Reddit discussion share / mention share

不是实际市场份额。

### 4. Geimori Voice

覆盖：

- Geimori
- MyWirsh
- GU63
- GU38
- T38 / T38 Plus / T38 Battery Gen2
- GU64（后续如进入讨论）

输出：

- mentions
- sentiment
- positive themes
- negative themes
- questions/objections
- comparison targets
- actionable response

### 5. Customer Priorities

不能由 LLM 随意生成权重。

服务器根据确定性数据计算 priority_score，例如：

```text
priority_score =
  mention_score
+ unique_post_score
+ engagement_score
+ recency_score
+ trend_score
```

LLM 只负责解释。

优先级至少区分：

- structural priority
- emerging trend
- temporary spike
- weak signal

### 6. Next Week Actions

最多 5 项，并按业务类型组织：

- Ads
- Blog / SEO
- Product
- Community / Reddit
- Research / Validation

每项必须包含：

- action
- evidence
- business_reason
- confidence
- urgency

不要生成超过团队实际执行能力的大量任务。

---

# 7. Trend Classification

建议由阿里云先计算数值，再由规则/LLM解释。

至少支持：

### spike
短期异常暴涨，但持续性未知。

### emerging
连续多个观察窗口上升，并达到最低 evidence threshold。

### rising
相较上周和 30d baseline 均明显上升。

### stable
在正常波动范围内。

### declining
持续下降。

### structural_priority
连续至少多个周窗口保持高频，并有足够 unique posts / engagement 支撑。

不要因为一天一篇爆帖，就把主题标记为 structural priority。

---

# 8. Evidence / Confidence 规则

所有重要洞察必须至少有：

```text
evidence_count
unique_posts
permalink[]
time_window
confidence
```

重要结论禁止只使用：

> “用户普遍认为”

必须改为类似：

> In 14 grinder-related threads this week, 8 mentioned static/RDT issues; discussion volume was 1.8× the previous-week level.

如果样本小：

> Weak signal — based on 2 threads only.

---

# 9. LLM 输出纪律

LLM 可以：

- 分类
- 摘要
- 提炼痛点
- 识别语义
- 提炼用户原话
- 解释趋势
- 生成 marketing/content hypothesis

LLM 不可以：

- 自己计算百分比
- 自己计算 WoW
- 自己生成没有来源的 customer weight
- 把 1–2 个帖子说成整体市场
- 把 Reddit mention share 说成 market share
- 把推测写成事实

---

# 10. Report Delivery

## Daily

飞书卡片优先。

卡片包含：

- Today in one sentence
- 3–5 KPI
- 最多 3 个 alerts
- 最多 3 个 actions
- HTML 详情按钮

HTML 作为 drill-down。

## Weekly

周报 HTML 为主，飞书卡片做 Executive Summary + 报告入口。

周报适合作为广告、SEO、内容、产品讨论的决策材料。

---

# 11. 推荐调度

具体时间做成配置，不写死在代码。

建议：

```text
daily_report_enabled=true
weekly_report_enabled=true
daily_report_timezone=Asia/Shanghai
weekly_report_day=MON
```

日报和周报必须是两套独立 schema：

```text
schemas/daily_pulse.schema.json
schemas/weekly_intelligence.schema.json
```

不要共用一个 JSON schema。

---

# 12. 第一阶段实施顺序

1. PostgreSQL schema
2. Collector 数据入库
3. 去重 / fetch_runs
4. 24h / 7d / 30d 聚合
5. Daily API
6. Daily Pulse schema
7. Coze Daily HTML + Feishu
8. Weekly aggregates
9. Weekly Intelligence schema
10. Coze Weekly HTML + Feishu
11. confidence/evidence validation
12. dry-run
13. 正式调度

---

# 13. Definition of Done

只有满足以下条件才能认为上线：

- 数据进入 PostgreSQL，而不是依赖本地 JSON state
- 同一 post 不重复入库
- 同一 analysis_version 不重复分析
- Daily/Weekly 使用独立 schema
- 所有 ratio / WoW / baseline 数值来自阿里云
- 所有关键洞察可回链原帖
- 弱样本明确标 weak signal
- 无强信号时允许“无需行动”
- 测试不真实推飞书
- 代码全部 GitHub 管理
- GitHub → CI/CD → 阿里云，禁止裸改生产

