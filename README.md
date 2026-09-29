# Reddit 磨豆机情报监控（Reddit Grinder Intelligence Monitor）

> 每天从 Reddit **r/pourover** 与 **r/espresso** 拉取最新帖子，分析顾客对**磨豆机**的需求、反馈与趋势，同时监测品牌 **Geimori** 的产品口碑，产出可执行洞察（需求洞察 / 营销切入点 / 内容选题 / 产品改进方向）。

> [!IMPORTANT]
> **2026-09-29 架构更新：**后续实现以 [docs/COZE_DAILY_WEEKLY_SPEC.md](docs/COZE_DAILY_WEEKLY_SPEC.md) 为准。阿里云负责抓取、PostgreSQL 数据存储、去重、状态、统计和趋势计算；Coze 只负责 LLM 分析、Daily/Weekly HTML 报告与飞书推送。该规范优先于下方仍待重构的 JSONL / Coze state file 旧描述。

---

## 1. 项目目标

1. **磨豆机顾客需求/反馈**：痛点、预算区间、选购因素、品牌讨论、热门话题
2. **Geimori 产品反馈**：提及我们产品（Geimori / MyWirsh / GU63 / T38）的帖子及情感倾向
3. **产出可执行维度**：顾客优先级排序、营销角度、博客选题、产品改进建议

## 2. 系统架构

```
┌──────────────┐      Reddit 公开 JSON API       ┌──────────────────────────────────┐
│    Reddit    │ ◄─────────────────────────────── │  阿里云 Reddit 拉取服务 (8086)    │
│ r/pourover   │                                 │  - 每日/按需拉取 2 个 subreddit     │
│ r/espresso   │                                 │  - 增量存储 raw 数据（post id 去重） │
└──────────────┘                                 │  - API: /api/reddit/latest?hours=24 │
                                                 │  - X-API-Key 鉴权（同 8080 模式）   │
                                                 └───────────────┬──────────────────┘
                                                                 │ HTTP
                                                                 ▼
                                                 ┌──────────────────────────────────┐
                                                 │  阿里云 8086：LLM 分析（DeepSeek） │
                                                 │  - 磨豆机需求分类/痛点/趋势       │
                                                 │  - Geimori 提及检测 + 情感        │
                                                 │  - 四维度可执行洞察               │
                                                 └───────────────┬──────────────────┘
                                                                 │ analysis JSON
                                                                 ▼
                                                 ┌──────────────────────────────────┐
                                                 │  Coze 每日调度（CodeAct 脚本）     │
                                                 │  1. 取 analysis JSON（analysis-batch│
                                                 │  2. 生成 HTML 报告                 │
                                                 │  3. file_to_url 短链 → 推飞书      │
                                                 └──────────────────────────────────┘
```

**关键前提（已实测验证）**：Coze 沙箱访问不了 Reddit（连接被重置），拉取必须在**阿里云服务器**完成；LLM 分析统一由**阿里云调用 DeepSeek**（ANALYZER_* 配置走 Secrets，同 youtube-kol 模式），Coze 只渲染 HTML + 推送（遵循计算架构铁律）。

## 3. 组件设计

### 3.1 阿里云拉取服务（8086 端口）

- Python + FastAPI，走 GitHub → CI/CD → 阿里云 部署（不直接 SSH）
- Reddit 公开 JSON API 拉取（无需 API key，需自定义 User-Agent + 限速）
- 增量存储：每日新帖 JSONL，按 `post_id` 去重，保留原始数据
- 提供 `latest` 接口给 Coze 取数（如 `/api/reddit/latest?hours=24`）
- 鉴权：`X-API-Key`（同现有 8080/8081 模式）
- **独立端口 8086**，不影响现有 8080/8081/8082/8083 服务

### 3.2 分析（阿里云 DeepSeek）与 Coze 交付

- 阿里云侧（8086）：抓取入库后由 LLM 分析模块（DeepSeek，`ANALYZER_*` 配置）生成结构化 analysis JSON（需求/品牌/竞品/洞察），写入 analysis_batches，随 `analysis-batch` API 提供给 Coze
- Coze 侧（CodeAct 脚本）每天定时运行：
  1. 调 8086 拿分析批次（analysis JSON，含 evidence/confidence）
  2. 生成 HTML 报告 + 状态文件
  3. `file_to_url` 生成 coze.cn 短链 → 推飞书卡片（含 `google ad analysis` 关键词）

### 3.3 报告与推送

- **HTML 报告**：概览 KPI（新帖数/热门帖/提及数）→ 需求洞察分类 → 品牌反馈明细（帖子+链接+情感）→ 原文链接汇总
- **飞书卡片**：摘要版（KPI + 热门帖 + 需求洞察 + 品牌反馈 + 完整报告按钮）

## 4. 数据结构（三层）

### 4.1 原始帖子数据（阿里云 8086 存储，JSONL）

```
post_id          # 去重主键
subreddit        # pourover / espresso
title
selftext         # 正文（可能为空）
created_utc      # 发布时间戳
score / upvote_ratio / num_comments
permalink        # 原文链接
flair            # 帖子标签（如 [Gear]/[Question]）
top_comments     # Top3 评论（可选）
```

> 作者名不落库，只保留帖子本身，避免 PII 合规问题。

### 4.2 每日分析结果（Coze 侧结构化 JSON）

```json
{
  "date": "2026-09-26",
  "stats": {
    "pourover_new": 32, "espresso_new": 28,
    "hot_posts": [{"title": "...", "score": 127, "permalink": "..."}]
  },
  "grinder_insights": [
    {
      "category": "手冲磨豆机",
      "sentiment": "positive",
      "pain_points": ["细粉控制", "静电飞粉"],
      "budget_bands": ["$300-500"],
      "mentioned_models": ["ZP6", "K-Ultra", "DF64"],
      "evidence_posts": ["post_id_1", "post_id_2"]
    }
  ],
  "brand_feedback": {
    "mentions": [
      {"post_id": "...", "title": "...", "sentiment": "positive",
       "summary": "静电控制不错", "quote": "..."}
    ],
    "positive": 1, "negative": 0, "neutral": 0,
    "key_themes": ["静电控制", "细粉表现"]
  }
}
```

> 每条洞察都带 `evidence_posts` 回链到原始帖子，可追溯、可复核。

### 4.3 最终报告（HTML + 飞书卡片）

- HTML：概览 → 需求洞察分类 → 品牌反馈明细 → 原文链接汇总
- 飞书卡片：摘要版

## 5. 分析维度（v2 Demo 已实现）

| 维度 | 说明 |
|---|---|
| **stats / hot_posts** | 当日新帖数、磨豆机关联占比、热门帖（score/comments/topic/summary） |
| **grinder_insights** | 磨豆机需求分类洞察：每类带 signal_strength、mentions、要点、evidence_posts |
| **brand_feedback** | Geimori 品牌提及：情感分布、关键主题、逐条明细（帖子/情感/摘要/原文引用） |
| **customer_priorities** | 顾客优先级排序：rank/priority/weight/detail/signal（如静电控制、预算区间、风味取向） |
| **marketing_angles** | 营销切入点：hook/basis/usage（如"DF64 静电痛点 → Geimori 抗静电卖点"） |
| **blog_topics** | 内容选题：title/angle/target（如"为什么咖啡粉会到处飞？静电物理课"） |
| **product_improvements** | 产品改进建议：priority/suggestion/detail |
| **deep_analysis** | 贴切帖深度分析：relevance/key_user_voices/pain_points/ai_insights/actionable |

## 6. 数据存储位置

| 数据 | 位置 | 保留策略 |
|---|---|---|
| 原始帖子 JSONL | 阿里云 8086（`/opt/data/reddit/`） | 保留 30 天，可追溯 |
| 每日分析 JSON | Coze 工作区 `codeact/output/reddit_insights_YYYY-MM-DD.json` | 长期保留（云盘） |
| HTML 报告 | Coze 工作区 `daily_reports/reddit_insights_YYYY-MM-DD.html` | 长期保留，短链 30 天 |
| 状态文件 | Coze 工作区 `codeact/output/reddit_monitor_state.json` | 记录游标/上次拉取，防重复 |
| 代码/测试 | GitHub（同步铁律） | 永久 |
| 每日卡片 | 飞书群 | 即时消息 |

设计原则：**原始数据在阿里云（近数据源、大文件不占云盘）、分析结果和报告在云盘（LLM 分析产物）、代码进 GitHub（防丢失）**，与现有 GA4/Ads/youtube-kol 架构一致。

## 7. 风险与应对

| 风险 | 应对 |
|---|---|
| 阿里云能否访问 Reddit 未验证 | 部署前先在阿里云实测连通性；若被墙需走代理/镜像方案 |
| Reddit 未认证 API 限速（~10 req/min） | 服务端每小时缓存一次，Coze 取缓存而非实时拉 |
| LLM 成本 | 每日约 30-80 帖，控制在标题+正文+Top3 评论，可按需降级 |
| 隐私合规 | 只取公开数据，报告不暴露发帖人身份 |
| 推送纪律 | 正式调度才推飞书，测试一律 dry-run |
| 代码纪律 | 所有代码/测试/文档同步 GitHub，禁止裸改 |

## 8. Demo

- `demo/reddit_insights_demo_2026-09-26.html`：正式 HTML 报告样式 demo（模拟数据 + 贴切帖子深度分析基于真实 Reddit 口碑素材）
- `demo/insights_demo_2026-09-26.json`：每日分析输出的结构 demo（字段结构与正式运行一致）

## 9. 待确认参数（开工前）

1. **推送时间**：每日几点推送飞书卡片？
2. **分析范围**：是否包含评论（Top3）深度分析？
3. **交付形式**：HTML 报告 + 飞书卡片摘要是否满足，是否需要额外输出（如周报聚合）？
4. **demo 卡片文案风格**：是否需要调整？

## 10. 代码纪律（沿用现有铁律）

- 所有代码/测试/文档同步 GitHub
- 严禁裸改生产脚本（Git → GitHub → CI/CD → 阿里云）
- 计算在阿里云，LLM 只分析不计算（ADR-001：LLM 也由阿里云调用 DeepSeek），本地只渲染 HTML
- 测试/修复一律 dry-run，不得真实推送飞书

## 11. 部署 GitHub Secrets（CI/CD 需要，命名与现有仓库一致）

| Secret | 说明 |
|--------|------|
| `SERVER_IP` / `SERVER_USER` / `SSH_PRIVATE_KEY` / `DEPLOY_PAT` | 阿里云 SSH + 部署 PAT（同 Wirsh/youtube-kol 仓库） |
| `REDDIT_DATABASE_URL` | PostgreSQL 连接串，如 `postgresql://reddit_intel:xxx@127.0.0.1:5432/reddit_intel` |
| `REDDIT_API_KEY` | 8086 API 鉴权（`X-API-Key`，同 8080/8081 模式） |
| `ANALYZER_API_KEY` | DeepSeek API Key（ADR-001，阿里云侧调用） |
| `ANALYZER_API_BASE` / `ANALYZER_MODEL` | 默认 `https://api.deepseek.com` / `deepseek-v4-pro` |
| `REDDIT_USER_AGENT` | Reddit 自定义 UA（防 429） |
| `REDDIT_SLEEP_SECONDS` | 请求间隔，默认 `2.0` |

**重要**：以上 Secrets 一旦在 GitHub 配置，CI 自动部署到阿里云；`.env` 只在服务器本地生成，绝不入库。`SSH_PRIVATE_KEY` 与 Wirsh 仓库共用（私钥只存 Secrets，本地无备份）。

