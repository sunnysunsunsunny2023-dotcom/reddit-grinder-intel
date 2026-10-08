# Reddit Grinder Intelligence — 回归测试记录

> 规则：修 bug → 补 unit/regression 测试 → 更新本文件 → 打 tag → push（GitHub 是唯一资产源）。
> 单元与回归测试全部在 `tests/`（GitHub 管理），CI（deploy.yml）每次 push/workflow_dispatch 先跑 `python -m pytest tests/ -q`，全过才部署。

## 当前状态
- 测试数：**39 项**（2026-09-30 v0.1.8 全绿）
- 运行方式：`python -m pytest tests/ -q`（本地 + CI 一致）

## 已登记 Bug 与回归用例

### RGI-001：analysis-batch HTTP 500 — result dict 未序列化
- **现象**：`analyze_batch` INSERT `analysis_results` 时把 dict 直接绑定 → `sqlite3.ProgrammingError: type 'dict' is not supported`
- **根因**：sqlite3 绑定参数不支持 dict
- **修复**：`analyst/analyzer.py` 对 result 用 `_json.dumps(result, ensure_ascii=False)`
- **回归用例**：`tests/test_analyzer.py::test_analyze_batch_serializes_result_json`
- **版本**：v0.1.1（ef071c0）

### RGI-002：analysis-batch HTTP 500 — created_utc convert_timestamp
- **现象**：`aggregate.query_posts` 读 TIMESTAMP 列触发 `sqlite3.dbapi2.convert_timestamp: val.split(b" ") ValueError: not enough values to unpack (expected 2, got 1)`
- **根因**：`detect_types=PARSE_DECLTYPES` 对 TIMESTAMP 列用 convert_timestamp 解析，要求值含空格（`YYYY-MM-DD HH:MM:SS`）；旧数据存在三种形态：
  1. epoch 整数（integer/real，如 1790755200）
  2. epoch 文本（text `"1790755200"`）
  3. **ISO 文本带 T（text `"2026-09-27T02:42:49"`，服务器真实形态）**——无空格同样触发
- **修复**：`collector/storage.py`
  - `_bind_row` 增加 `_norm_created_utc`：epoch int/float、epoch 文本、ISO 带 T 文本 → naive UTC datetime
  - `_migrate_created_utc`：CASE 两分支（`LIKE '%T%'` → `datetime(replace(T,' '))`；纯数字无空格无连字符 → `datetime(x,'unixepoch')`），幂等
  - 部署时 `deploy/migrate_created_utc.py` 强制迁移（注意 sys.path 需插入仓库根，见 RGI-004）
- **回归用例**：
  - `tests/test_collector.py::test_upsert_posts_created_utc_queryable`（epoch int 绑定）
  - `tests/test_collector.py::test_upsert_posts_created_utc_iso_t_bind`（ISO 带 T 绑定）
  - `tests/test_collector.py::test_migrate_created_utc_old_epoch_rows`（integer + text epoch 迁移）
  - `tests/test_collector.py::test_migrate_created_utc_iso_t_rows`（text ISO 带 T 迁移）
- **版本**：v0.1.2（622cc91）/ v0.1.6（5981244）

### RGI-003：analysis-batch HTTP 502 — DeepSeek 空 content / 非 JSON
- **现象**：`{"detail":"analysis failed: Model output is not valid JSON: "}`（content 为空）或只输出 `{`
- **根因**：服务器 `ANALYZER_MODEL` 配成 reasoner 类模型（deepseek-v4-pro），json_mode 下推理内容占满输出、`content` 为空
- **修复**：
  - GitHub Secret `ANALYZER_MODEL` 改为 **deepseek-chat**（本地全链路验证通过）
  - `analyst/deepseek_client.py`：content 空/None 时明确 `raise DeepSeekError`（带 reasoning 前缀便于排查）
  - `analyst/analyzer.py`：`analyze_batch` 对 DeepSeekError 最多重试 3 次（间隔 2s/4s）
- **回归用例**：
  - `tests/test_analyzer.py::test_analyze_batch_retries_on_empty_content`（第 1 次失败第 2 次成功）
  - `tests/test_analyzer.py::test_analyze_batch_gives_up_after_3_failures`（3 次失败最终抛错，不伪造成功）
- **版本**：v0.1.7（95f42dd）+ Secret 修改

### RGI-004：部署脚本 migrate_created_utc.py ModuleNotFoundError
- **现象**：`python deploy/migrate_created_utc.py` 报 `No module named 'collector'` → `set -e` 中断 job
- **根因**：运行时 sys.path 首项是脚本所在目录 `deploy/`，`from collector.storage import ...` 失败
- **修复**：脚本内 `sys.path.insert(0, repo_root)`；deploy.yml 迁移步骤加 `|| echo` 容错
- **回归用例**：无单测（部署脚本），由 Full verification（workflow_dispatch）覆盖
- **版本**：v0.1.5（4dd344c）

### RGI-005：deploy.yml YAML ScannerError
- **现象**：部署 run 直接 failure，jobs API 返回空
- **根因**：`script: |` 块内多行 `python -c "..."` 内容顶格，破坏 block scalar 缩进
- **修复**：改为独立脚本 `deploy/migrate_created_utc.py` + 一行命令调用
- **回归用例**：无单测（CI 语法解析覆盖），由每次 workflow 解析保证
- **版本**：v0.1.4（71ee6b5）

### RGI-006：Weekly analysis-batch 502 — `KeyError: 'avg_7d'`
- **现象**：`{"detail":"analysis failed: 'avg_7d'"}`（report_type=weekly 必现）
- **根因**：`build_weekly_context` 的 `topic_trends`（demand_trends 结构）只含 `avg_30d`、无 `avg_7d`，而 `_context_md` 渲染 `t['avg_7d']` 直接 KeyError；Full verification 只验过 daily，weekly 从未跑通
- **修复**：
  - `analyst/analyzer.py::build_weekly_context`：补 `series_7d/base7`，demand_trends 每项加 `avg_7d`（7d 日均基线），`baseline_7d` 复用 base7
  - `_context_md` 对 topic_trends 的 `avg_7d/avg_30d` 改为 `t.get(..., '—')` 容错
- **回归用例**：
  - `tests/test_weekly_context.py::test_context_md_tolerates_missing_avg_7d`
  - `tests/test_weekly_context.py::test_build_weekly_context_includes_avg_7d`
- **版本**：v0.2.2

### RGI-007：Coze 渲染 API 路径 404 — `/api/analysis-batch` vs `/api/reddit/analysis-batch`
- **现象**：Coze 侧调 `http://<IP>:8086/api/analysis-batch` 返回 404，真实路由带 `/reddit/` 前缀
- **根因**：main.py 路由定义为 `/api/reddit/analysis-batch`，渲染模块写成了无前缀路径
- **修复**：`coze/render_daily.py::fetch_payload` 改 `api/reddit/analysis-batch`
- **回归用例**：`tests/test_render_daily.py::test_fetch_payload_rejects_non_dict`（HTTP 层）＋真实公网 dry-run 200
- **版本**：v0.2.2

### RGI-008：Weekly analysis-batch 502 — DeepSeek JSON 被 max_tokens 截断
- **现象**：`{"detail":"analysis failed: Model output is not valid JSON: {\n ... \"最大竞品变化\" ...`（weekly 必现，daily 正常）
- **根因**：weekly 输出 6 个板块更长，4096 max_tokens 下 JSON 在末尾被截断、无闭合 `}`，`parse_json_response` 无法容错
- **修复**：`analyst/analyzer.py::analyze_batch` 按 report_type 区分 max_tokens：weekly=8192，daily=4096
- **回归用例**：`tests/test_analyzer.py::test_analyze_batch_weekly_uses_larger_max_tokens`
- **版本**：v0.2.3

### RGI-010：Geimori GU64 帖子漏抓 — new 前100 外历史帖无回溯
- **现象**：r/espresso 帖子「Geimori GU64 Gen 2 got delivered today」（u/sah4r，10-06 发布）完全未入库，日报 Geimori 0 提及；标题含 geimori/gu64 关键词，非关键词漏判
- **根因**：采集器每天 00:30/12:30 只抓 `new.json` 前 100 条（发布后数小时内掉出列表即永久错过），无关键词搜索回溯；评论默认不抓（`fetch_comments=False`），评论内品牌提及全部丢失
- **修复**：
  - `collector/reddit_fetcher.py`：新增 `search_posts()` + `_fetch_search_json()` + `_fetch_search_rss()`（search.json → search RSS fallback，URL 编码 q）
  - `collector/scheduler.py`：`run_once` 支持 `search_keywords`，每 subreddit 抓完 new 后按品牌词（geimori/mywirsh/wirsh/gu63/gu64/gu38/t38）搜索补抓，走同一 dedupe；`_run_search_backfill()` 单关键词失败不阻塞
  - `deploy/reddit-intel-collector.service`：`--comments --search-keywords geimori mywirsh wirsh gu63 gu64 gu38 t38`
  - `deploy/reddit-intel-collector.timer`：每天 2 次 → 每 4 小时（`*-*-* 00/4:00:00`）
  - `collector/comments.py`：评论 JSON 403（数据中心 IP 常见）时 fallback curl 拉取同一 URL
- **回归用例**：
  - `tests/test_collector.py::test_search_posts_rss_prefer`
  - `tests/test_collector.py::test_search_posts_json_fallback`
  - `tests/test_collector.py::test_search_backfill_inserts_and_dedupes`
  - `tests/test_collector.py::test_comments_curl_fallback`
- **版本**：v0.2.5

### RGI-011：搜索补抓线上失败 — search RSS 429/JSON 403 + 返回值类型 bug
- **现象**：v0.2.5 CI full verification 中 `--search-keywords` 全部失败：search RSS 429、search JSON 403；且 `run_once` 抛 `TypeError: can only concatenate list (not "int") to list`，采集器整体退出，评论抓取未执行
- **根因**：
  1. Reddit 对 `/search/` 端点限速严格（实测比 `/new/` 严）：7 个关键词连续请求全部命中 429；且 prefer_rss 分支 RSS 失败后 fallback JSON，JSON 已知 403，白白多打 7 个请求污染限速窗口
  2. `_run_search_backfill()` 返回 `(新帖数量 int, 找到数量 int)`，但 `run_once` 按 `(新帖列表, int)` 解包，`new_posts + search_new_posts` 变成 `list + int` 直接 TypeError
- **修复**：
  - `collector/scheduler.py`：`_run_search_backfill()` 把全部关键词合并为单个 OR 查询（`q=geimori OR mywirsh OR ...`），每 subreddit 只打 1 次搜索请求，返回 `(新帖原始 dict 列表, 搜索到的帖子总数)`；`run_once` 解包列表、`search_new = len(search_new_posts)`，评论抓取直接复用 `new_posts + search_new_posts`
  - `collector/reddit_fetcher.py`：prefer_rss 时 search RSS 失败直接抛错、不再 fallback JSON（避免 403 污染限速窗口）；`_fetch_search_rss()` 对 429 退避 20s 重试一次
  - **第二轮（v0.2.7）**：搜索 RSS 首次 429 → 退避重试成功拉到 98 条，但入库抛 `sqlite3.IntegrityError: NOT NULL constraint failed: reddit_posts.created_utc`——search RSS 部分条目只有 `<updated>` 没有 `<published>`，时间字段缺失。`parse_rss_feed()` 增加 `<updated>` fallback；`_run_search_backfill()` 过滤 created_utc 为 None 的帖子（无时间戳无法纳入窗口统计）
- **回归用例**：
  - `tests/test_collector.py::test_search_backfill_inserts_and_dedupes`（改：OR 合并 + 返回列表断言）
  - `tests/test_collector.py::test_search_backfill_failure_returns_empty_list`（新增：搜索失败不阻塞整体）
  - `tests/test_collector.py::test_search_posts_rss_fail_no_json_fallback`（新增：prefer_rss 失败不再 fallback JSON）
  - `tests/test_collector.py::test_search_rss_429_backoff_retry`（新增：429 退避重试）
  - `tests/test_collector.py::test_parse_rss_feed_updated_fallback`（新增：updated fallback 解析 created_utc）
- **版本**：v0.2.6 → v0.2.7
