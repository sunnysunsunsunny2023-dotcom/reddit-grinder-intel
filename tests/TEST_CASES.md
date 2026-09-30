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
