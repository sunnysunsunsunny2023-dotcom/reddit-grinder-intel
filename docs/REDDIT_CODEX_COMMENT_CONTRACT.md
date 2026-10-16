# Reddit 评论采集契约（Codex ↔ Tracy 协作）

> 版本：v0.1（2026-10-16）
> 目的：Codex 用浏览器模式采集 **Geimori 品牌提及帖**的评论树，推送到 GitHub；
> Tracy（主 Agent）拉取数据 → 分析（DeepSeek）→ 渲染 → Pages 部署 → 飞书推送。
> 范围收窄：**只采集 Geimori 相关帖子的评论**；其余内容维持现有"标题/帖子级"分析，不爬评论。

## 1. 分工边界

| 角色 | 负责 | 不负责 |
|---|---|---|
| Codex | 浏览器模式打开 reddit.com，找 Geimori 提及帖，爬评论树，写 JSON，push GitHub | 数据分析、报告渲染、飞书推送 |
| Tracy | git pull → 解析 JSON → DeepSeek 分析（10 维度 + 证据门槛）→ 渲染日报 → Pages 部署 → 飞书推送 | 浏览器采集（无 API 权限） |

## 2. Codex 采集范围（只做 Geimori）

**触发条件**：帖子标题 **或** 正文命中任一品牌词 → 采集该帖完整评论树。

品牌词清单（与现有 `app/keywords.py` 保持一致，大小写不敏感）：

```
geimori
mywirsh
wirsh
gu63
gu64
gu38
t38
```

**不采集**：
- 未命中品牌词的帖子（即使讨论磨豆机主题，如 VS6/DF54/Comandante 等）→ 维持现有标题级分析
- 非 espresso / pourover 子版块的帖子（除非是 Geimori 官方/品牌相关帖）

**目标量**：每天 1–10 个命中帖即可，优先高评论数、近 7 天内的帖子。不求全，求准。

## 3. 输出位置与文件命名

- 仓库：`sunnysunsunsunny2023-dotcom/reddit-grinder-intel`（与 reddit_monitor 同一仓库）
- 路径：`data/comments/YYYY-MM-DD.json`（UTC 日期）
- 每天一个文件；当天无命中帖时可不产生文件，或写空数组文件 `{"posts": []}`

## 4. JSON Schema（Codex 输出格式）

```json
{
  "fetched_at": "2026-10-16T08:00:00Z",
  "source": "codex-browser",
  "schema_version": 1,
  "subreddits": ["espresso", "pourover"],
  "posts": [
    {
      "post_id": "abc123",
      "title": "Anyone using Geimori GU64?",
      "subreddit": "espresso",
      "url": "https://www.reddit.com/r/espresso/comments/abc123/...",
      "score": 42,
      "num_comments": 15,
      "matched_keywords": ["geimori", "gu64"],
      "comments": [
        {
          "comment_id": "xyz789",
          "parent_id": null,
          "body": "I have one, retention is fine but static is annoying...",
          "score": 12,
          "created_utc": "2026-10-15T20:00:00Z",
          "depth": 0
        },
        {
          "comment_id": "xyz790",
          "parent_id": "xyz789",
          "body": "Try RDT, fixed it for me",
          "score": 5,
          "created_utc": "2026-10-15T20:15:00Z",
          "depth": 1
        }
      ]
    }
  ]
}
```

**字段要求**：
- `post_id` / `comment_id`：Reddit 原始 base36 id，必须保留（去重靠它）
- `parent_id`：父评论 id，顶层评论为 `null`（评论树结构，现有 reddit_comments 表缺此字段）
- `depth`：评论层级（0 = 顶层）
- `created_utc`：ISO8601 UTC
- `body`：评论原文，**不要翻译、不要摘要**，保留原话
- `score`：评论点赞数（爬得到就填，爬不到填 0）
- `matched_keywords`：该帖命中的品牌词，方便 Tracy 侧快速过滤
- 每条评论的 `url` 可不填（可从 post url 推导），填了更好

## 5. 评论树爬取要点（Codex 侧）

1. 打开帖子页面（https://www.reddit.com/r/{sub}/comments/{post_id}/）
2. 展开评论：点击 "View all comments" / 展开 "more replies"（morechildren）
3. 滚动到底确保评论全部加载
4. 提取评论正文、点赞数、层级（回复缩进即层级）
5. 多页评论需翻页（Reddit 评论超 500 条时分页）
6. 只爬文本评论；图片/链接评论保留 body 原文即可

## 6. Tracy 侧处理（拿到 JSON 后）

1. `git pull` 拉取 `data/comments/YYYY-MM-DD.json`
2. 校验 schema（缺失字段补默认值，非法行丢弃并记录）
3. 与现有 SQLite `reddit_comments` 去重（comment_id 主键）
4. DeepSeek 分析：机主声音（正向/负向/疑问）、机会点、证据门槛（≥3 独立帖才算趋势、单帖声明小样本）
5. 渲染进日报新增模块（Owner Voice / Opportunities）
6. Pages 部署 → 飞书推送（沿用现有 reddit_daily_pulse.py 链路）

## 7. 合规备注（双方知悉）

- Codex 用浏览器模式"人工浏览"采集，属于正常浏览器访问范畴，**不调用 Reddit Data API、不使用 .json 端点、不使用代理池**
- 数据仅用于 Geimori 品牌自身的情报分析，不转售、不公开再分发、不用于 AI 训练
- 用户删除的内容在 GitHub 上的副本由 Tracy 侧定期清理（sync-and-purge）

## 8. 验收标准（第一次联调）

- Codex 产出一个 `data/comments/2026-10-16.json`（≥3 个命中帖，含评论树）
- Tracy 侧 `git pull` 能解析、能渲染出带 Owner Voice 的日报
- 飞书卡片正常推送
