Geimori Reddit 智能监控与 AI 回复系统

完整开发需求 V2.0

一、项目目标

基于现有 reddit-grinder-intel 项目，建立一套 Geimori 品牌社区评论自动监测、AI 智能回复、人工审核和授权后自动发布系统。

最终工作流程：

Reddit 自动监控 → 发现 Geimori 相关帖子或评论 → AI 分析相关性及回复必要性 → Geimori Brand Voice 生成英文草稿 → 飞书人工审核 → 审核通过后自动发布至 Reddit → 记录发布结果和后续互动。

系统应该同时支持：

1. 自动监测 Reddit 品牌提及。
2. 发现新帖子及历史帖子中的新增评论。
3. 自动识别客户投诉、购买咨询、产品对比和普通讨论。
4. 判断是否需要品牌官方介入。
5. 按照 Geimori 统一品牌规范生成自然、美式英语的回复。
6. 将草稿推送飞书供员工审核和修改。
7. 审批通过后，在具备合法 Reddit API 授权时自动回复。
8. 保存评论、草稿、审核、发布及回复结果。
9. 与 ChatGPT Business 现有 Geimori Brand Voice Plugin 共用一套回复逻辑和品牌知识。
10. 未来可以扩展到 YouTube、Instagram 等平台。

核心原则：

AI 负责发现、分析、起草；人负责审核；系统负责发布与追踪。

所有公开发布内容必须经过人工批准。

⸻

二、现有基础设施

现有 GitHub 仓库：

https://github.com/sunnysunsunsunny2023-dotcom/reddit-grinder-intel

现有系统：

组件	技术
Reddit 数据采集	Python Collector
后端 API	FastAPI
服务端	阿里云
现有 API 端口	8086
数据存储	SQLite
AI 分析	DeepSeek
自动化工作流	Coze
企业通知	飞书
部署	GitHub Actions / CI/CD
品牌回复规范	ChatGPT Business Geimori Brand Voice

已存在的 API：

* GET /api/reddit/analysis-batch
* POST /api/reddit/analysis-complete

原有项目已经具备帖子收集、品牌关键词识别、评论采集代码、内容分析、去重、数据库及日报周报能力。

开发前必须检查：

* README.md
* docs/COZE_DAILY_WEEKLY_SPEC.md
* collector/
* analyst/
* app/
* sql/
* deploy/
* .github/workflows/

不得重新建设已经存在的功能，不得影响原有 Daily Pulse 和 Weekly Intelligence。

注意：GitHub 中存在部署工作流，但仍需验证阿里云当前实际运行状态，不能仅根据代码存在就判定线上正常。

⸻

三、系统整体技术架构

采用统一后端、多个调用入口的架构。

数据采集层：

Reddit → 阿里云 Collector → SQLite

AI 分析层：

SQLite → Geimori Mention Classifier → Geimori Reply API

审批与发布层：

Reply API → Coze → 飞书审批 → 阿里云 Publisher → Reddit

ChatGPT 集成层：

ChatGPT Business Plugin → Remote MCP → 同一个 Geimori Reply API

3.1 阿里云职责

阿里云作为系统核心，负责：

* Reddit 数据采集
* 增量评论识别
* 数据去重
* SQLite 存储
* 品牌提及分类
* DeepSeek AI 调用
* 统一品牌回复生成
* 审批任务状态管理
* 飞书审核回调处理
* Reddit 授权发布
* 发布日志
* 数据统计

3.2 Coze 职责

Coze 主要负责：

* 调用阿里云 API
* 获取待审核任务
* 组织飞书审核通知
* 渲染飞书交互卡片
* 传递审核操作
* 显示审核与发布结果
* 日常摘要与统计报告

Coze 不应该：

* 自行维护另一份评论数据库
* 在临时工作流变量中保存唯一审批状态
* 重复实现品牌分析逻辑
* 直接保存 Reddit 发帖凭证
* 绕过人工审核发布评论

3.3 ChatGPT Plugin 集成原则

目前 Geimori Brand Voice 是在 ChatGPT Business 中配置的回复工具。

不能假设阿里云可以直接调用 ChatGPT 中安装的 Plugin。

正确方案是：

1. 在阿里云建立统一 Geimori Reply API。
2. 将品牌规范和产品知识库作为统一配置。
3. Coze 通过 HTTPS API 请求生成回复。
4. ChatGPT Business 通过可接入的远程 MCP 工具调用同一套 API。
5. 两个入口使用相同的回复规则、知识库版本和审核规范。

首先检查已有 ChatGPT Plugin 是否包含可复用的 MCP 服务接口。

如果只是基于 ChatGPT 指令配置，则将回复规范提取为后端统一配置，并在 ChatGPT 端连接新工具。

不得使用模拟登录 ChatGPT 网页的方式调用插件。

⸻

四、模块一：Reddit Comment Watcher

4.1 监测范围

初期覆盖：

* r/espresso
* r/pourover
* r/Geimori

后续支持增加其他相关社区。

品牌关键词：

* Geimori
* Geımori
* MyWirsh
* Wirsh

产品关键词：

* GU63
* GU64 / GU64 Gen2
* GU38
* T38
* T38 Plus
* T38 Battery
* T38 Gen2
* GU78

必须结合上下文分析，避免普通英文、型号缩写或无关内容导致误匹配。

4.2 监测帖子和评论

系统必须同时识别：

* 新发布的相关帖子
* 新帖子中的相关评论
* 旧帖子中新出现的评论
* 品牌账号收到的新回复
* 用户针对 Geimori 产品的投诉
* 产品评测帖子中的新问题
* 品牌产品比较讨论

重点解决现有 Collector 的一个问题：

现有代码主要对首次发现的新帖子抓取评论，历史帖子后续增加的新评论可能被漏掉。

因此必须建立独立的 Comment Incremental Watcher。

建议：

* 按小时进行增量检查，实际频率遵守授权和平台限制
* 对活跃的品牌相关讨论持续跟踪
* 对历史相关帖子设置合理观察周期
* 使用 Comment ID 去重
* 保留父评论和帖子上下文
* 已处理评论不得重复创建任务
* 已删除或不可回复内容自动标记

需要使用符合 Reddit 当前政策、权限及数据使用条件的访问方式。

此前 Reddit Data API 曾申请未获批，必须在开发前确认现行许可。不得通过技术方式绕过授权要求或反爬限制。

⸻

五、模块二：AI 智能分类

不是所有品牌提及都值得回复。

系统必须先判断是否需要 Geimori 介入。

分类	优先级	处理方式
产品故障、售后投诉	P1	生成回复并优先送审
产品规格、技术问题	P2	生成技术性回复
购买咨询、竞品比较	P2	根据社区规则决定是否回复
正面评价、晒单	P3	选择性回复
普通品牌提及	P3	记录，可不回复
无关、重复或禁止品牌参与	Skip	不生成回复

AI 输出需要包括：

* 平台
* Subreddit
* Post ID
* Comment ID
* 产品型号
* 情感分析
* 用户关注问题
* 紧急程度
* 回复必要性
* 推荐回复理由
* 是否需要事实核查
* 是否允许进入审核队列

必须允许 AI 判断：

reply_needed = false

禁止为提高品牌曝光而自动回复所有提及。

⸻

六、模块三：Geimori Brand Voice 统一回复引擎

这是整个系统最重要的部分。

6.1 建立统一 Geimori Reply API

建议在现有 FastAPI 服务中增加：

POST /api/geimori/draft-reply

请求示例：

{
  "platform": "reddit",
  "subreddit": "espresso",
  "product": "GU63",
  "comment_id": "t1_example",
  "comment": "My GU63 is retaining over 2g of coffee.",
  "context": "Customer discussing inconsistent retention."
}

预期输出：

{
  "reply_needed": true,
  "priority": "P1",
  "category": "product_complaint",
  "product": "GU63",
  "draft_reply": "Hey, that's definitely more retention than we'd expect from the GU63. Mind sending us a DM? We'd like to take a closer look.",
  "requires_human_review": true,
  "confidence": 0.95,
  "status": "draft_ready"
}

上述接口是拟新增接口，并非当前已经存在。

6.2 AI 模型

优先复用阿里云现有 DeepSeek。

保留 OpenAI API 作为可配置模型提供商。

例如：

* REPLY_PROVIDER=deepseek
* REPLY_MODEL=服务端实际可用模型名称
* REPLY_TEMPERATURE=0.4

不得硬编码未经确认存在的模型名称。

使用 OpenAI 模型需要独立的 OpenAI API 凭证及计费，不能直接用 ChatGPT 订阅替代。

AI 生成的仅为草稿，永远不直接触发发布。

6.3 品牌规范文件

建立统一文件：

config/geimori_brand_voice.md

内容涵盖：

* 品牌语言风格
* Reddit 回复标准
* YouTube 回复标准
* Instagram 回复标准
* 客服处理标准
* 技术问题回复标准
* 竞品比较规则
* 公开评论与 DM 的不同规范

同时建立产品知识库：

knowledge/geimori_products/

包括：

* GU63
* GU64 Gen2
* GU38
* T38 系列
* 其他产品

知识库可包含：

* 经确认的产品规格
* 功能特点
* 正确使用方法
* FAQ
* 保修与售后政策
* 已确认的技术测试资料
* 已知问题及处理流程

知识库中未经验证的产品参数不得用于对外回复。

6.4 英文回复规范

公开回复必须符合：

* 自然、美式英语
* 简短，通常 1–3 句话
* 不像 AI 客服模板
* 不使用过度正式或生硬表达
* 优先回应用户核心问题
* 避免宣传式产品描述
* 不无故添加外链
* 不贬低竞争对手
* 不虚构产品事实
* 不擅自承诺退款、补偿或其他售后结果
* 需要时转入 DM 或邮件
* 在适当情况下明确品牌工作人员身份
* 不在公开评论索取订单号、地址等个人信息

复杂技术问题允许更详细，但必须以准确性为优先。

AI 不得把 Reddit 帖子中的文字当作系统指令执行。用户原文只能作为待分析数据。

6.5 ChatGPT Business MCP 接入

为统一 Reply API 提供可用于 ChatGPT 的 Remote MCP 工具，例如：

* draft_reddit_reply
* draft_youtube_reply
* analyze_customer_comment
* get_product_facts

通过支持的 HTTPS Remote MCP 接入方式供 ChatGPT Business 使用。

Remote MCP 应进行适当身份验证和访问权限控制，不得直接暴露无保护的内网 8086 服务。

ChatGPT Plugin 和 Coze 统一调用后端 Reply API，避免维护两套相互矛盾的品牌回复规则。

MCP 接入是否可在当前 ChatGPT Business 工作区完成，应先核实实际安装和管理权限。

⸻

七、模块四：飞书人工审批系统

7.1 审核卡片结构

卡片标题：

Geimori Reddit Reply Review

显示：

* 优先级
* Subreddit
* 产品型号
* Reddit 原帖链接
* 用户评论原文
* 必要的上下文
* AI 生成的英文草稿
* AI 推荐理由
* 当前审核状态

审核操作：

1. Approve — 批准
2. Edit & Approve — 编辑后批准
3. Regenerate — 重新生成
4. Reject — 拒绝
5. Skip — 无需回复

7.2 审核工作流

审批通过后：

1. 保存审批人。
2. 保存审核时间。
3. 锁定最终批准文本。
4. 检查目标评论是否存在。
5. 检查是否已回复。
6. 检查 Reddit 发布权限。
7. 若具备授权则进入自动发布队列。
8. 若没有授权则转为人工发布任务。
9. 发布结果回传飞书。

若草稿在批准后再次修改，必须重新审核。

不同员工同时操作同一任务时，只能有一个有效审批结果。

飞书回调必须进行签名/令牌校验，并验证审核人的实际权限。

仅拥有卡片链接不得自动获得审批或发帖权限。

⸻

八、模块五：Reddit Publisher

8.1 设计目标

在具备 Reddit 授权的情况下，通过 Geimori 官方账号发布审核通过的评论。

发布前必须校验：

* 账号授权有效
* Reddit 允许当前用途
* 社区规则允许品牌参与
* 目标评论仍然存在
* 草稿已由人工批准
* 待发布文本与批准版本完全一致
* 当前任务未发布过

发布后：

* 保存 Reddit Reply ID
* 保存评论链接
* 保存发布时间
* 更新任务状态
* 通知飞书审核人

8.2 Reddit 授权限制

系统必须设置：

REDDIT_PUBLISH_ENABLED=false

默认关闭。

只有获得适用于本项目商业用途的 Reddit API 权限、通过测试及明确授权后才能开启。

未经授权前：

* 可以监控获准访问的数据
* 可以分析
* 可以生成草稿
* 可以在飞书审核
* 可以将已批准文本交给员工手动发布

不得绕过 Reddit 权限限制，也不得通过模拟网页操作规避平台规则。

8.3 失败处理

* 禁止重复发布
* 遇到限速应遵守平台规定
* 网络失败不能直接假设发布未成功
* 发布状态未知时先核验再决定是否重试
* 失败原因写入数据库
* 异常任务返回人工处理队列

⸻

九、数据库设计

复用已有 SQLite。

建议新增四张表。

reply_tasks

字段：

* task_id
* platform
* subreddit
* post_id
* comment_id
* original_text
* context
* permalink
* product
* category
* priority
* reply_needed
* status
* created_at
* updated_at

reply_drafts

字段：

* draft_id
* task_id
* draft_text
* model_provider
* model_name
* brand_voice_version
* knowledge_version
* draft_version
* created_at

reply_approvals

字段：

* approval_id
* task_id
* draft_version
* reviewer_id
* action
* approved_text
* approved_at
* status

reply_publications

字段：

* publication_id
* task_id
* approved_draft_version
* reddit_account
* target_comment_id
* reddit_reply_id
* published_text
* published_url
* status
* error_message
* published_at

需要合理设计唯一约束、状态机和并发事务，防止重复审核和重复发帖。

⸻

十、API 扩展

在现有 FastAPI 服务上新增：

API	功能
POST /api/geimori/draft-reply	AI 起草回复
GET /api/reddit/reply-queue	获取待处理任务
POST /api/reddit/reply-regenerate	重新生成
POST /api/reddit/reply-approve	审批
POST /api/reddit/reply-reject	拒绝
POST /api/reddit/reply-publish	授权后发布
GET /api/reddit/reply-stats	查询统计
POST /api/feishu/reply-callback	飞书审核回调

这些接口均为新增设计，需要根据实际项目代码进一步确定路径与参数。

所有写接口必须增加：

* 身份认证
* 角色权限校验
* 输入验证
* 幂等控制
* 审计日志
* 错误处理

外部 HTTPS 请求应通过受保护的服务网关或反向代理进入，不应直接公开现有内部服务端口。

⸻

十一、通知与数据统计

建立 Geimori Reddit Reply Dashboard。

至少包含：

* 今日品牌提及数
* 新增品牌相关评论数
* AI 建议回复数
* P1 客户投诉数
* 待审核数
* 已审核数
* 已发布数
* 跳过数
* 发布失败数
* 平均审核时间
* 不同产品的投诉分布
* 常见产品问题和趋势

可以整合进已有 Reddit Daily Pulse。

保持原有行业情报统计与品牌回复运营统计独立，避免混淆两种数据。

⸻

十二、开发阶段

Phase 0 — 系统审查

首先完成：

* 检查 GitHub 实际代码
* 检查部署配置
* 检查阿里云服务运行状态
* 检查 Reddit 数据获取权限
* 检查 SQLite 现有结构
* 检查 DeepSeek 调用
* 检查 Coze 和飞书现有工作流
* 检查 ChatGPT Brand Voice 是否有可调用接口

输出现有功能清单和缺失功能清单。

不得未经检查就重建项目。

Phase 1 — 评论监控及 AI 草稿

完成：

* 历史帖子评论增量识别
* 品牌提及筛选
* 分类和优先级
* 回复必要性判断
* 统一 Geimori Brand Voice
* Reply API
* 草稿存储
* 自动去重

进行 Dry Run。

Phase 2 — 飞书审批

完成：

* 审核任务队列
* 飞书交互卡片
* 审批
* 编辑后批准
* 拒绝和跳过
* 审核日志
* 人工发布交接

此阶段完成后，即使没有 Reddit 自动发帖授权，也应该能够实际投入运营。

Phase 3 — ChatGPT Plugin 统一集成

完成：

* Remote MCP 服务
* ChatGPT Business 连接配置
* 统一产品知识库
* 统一 Brand Voice 规则
* ChatGPT 与 Coze 回复一致性测试

现有 ChatGPT Plugin 不能被外部服务调用时，采用共同调用阿里云 API 的方式解决。

Phase 4 — Reddit 自动发布

前提：获得符合要求的 Reddit 商业 API/发帖授权。

完成：

* OAuth 授权
* 自动回复
* 防重复发送
* 失败处理
* 回复链接追踪
* 发布审计日志

未经授权，保持禁用。

⸻

十三、测试与验收

至少需要以下测试：

1. 新帖子中出现 Geimori，成功识别。
2. 三天前的帖子出现新 GU63 评论，成功识别。
3. 非品牌相关的 GU63 字符串不误判。
4. 同一评论重复被采集，不重复建任务。
5. AI 能判断哪些评论无需回复。
6. 客户投诉自动设为 P1。
7. AI 使用正确的产品知识。
8. 草稿符合 Geimori 自然美式英语规范。
9. 飞书可以批准、拒绝、编辑或重新生成。
10. 未审批评论不能发布。
11. 审批后修改文字必须重新批准。
12. 多名审核人同时操作不会重复发布。
13. Reddit 未授权时不能执行真实发布。
14. 发布失败后不会产生重复评论。
15. 任务状态和发布结果可追踪。
16. 已有 Reddit Daily/Weekly Intelligence 不受影响。
17. ChatGPT 与 Coze 使用相同品牌规则时，输出符合一致的品牌标准。

所有测试首先使用模拟数据和 Dry Run。

⸻

十四、开发和部署纪律

沿用现有规则：

* 所有代码提交 GitHub
* GitHub 为代码唯一可信来源
* 使用 CI/CD 部署阿里云
* 不直接在生产服务器裸改代码
* 保持现有 8086 API 兼容
* 使用现有 SQLite
* 优先复用 DeepSeek
* 不泄露 API Key、Token、OAuth 凭证
* 密钥放在安全配置环境中
* 测试不得自动发布真实 Reddit 评论
* 测试不得向真实员工大量推送飞书卡片
* 保持原有日报与周报正常运行

⸻

十五、要求 Coze 的交付内容

请依次完成以下工作：

第一步：现状审计

实际读取现有 GitHub 仓库，明确已完成、部分完成和未完成的功能。

第二步：技术方案

给出结合现有阿里云、DeepSeek、Coze、飞书和 ChatGPT Plugin 的架构方案。

第三步：实现 P0 核心闭环

优先实现评论增量监控、智能分类、AI 草稿、飞书人工审批、数据库任务状态。

第四步：统一回复接口

建立 Geimori Reply API，并规划 ChatGPT Remote MCP 接入，使 ChatGPT 和 Coze 使用同一个后端回复引擎。

第五步：测试

完成模拟测试，验证不会漏检新增评论、不会重复处理、不会未经批准发布。

第六步：交付与部署

提供：

* GitHub 修改记录
* API 文档
* 数据库结构
* 飞书工作流配置
* 品牌规范配置
* 测试结果
* 部署说明
* 当前可用功能
* 未完成及需要授权的功能

如果 Coze 无法直接修改 GitHub 或阿里云服务，请明确列出需要 Codex 实现的具体文件、接口、代码修改和测试，而不是假设这些功能已经部署。

最终要求

不要只输出一份抽象的设计方案，也不要重新建立一套与现有 Reddit Intelligence 重复的系统。

请先分析现有代码，复用已有能力，逐步实现可运行的生产系统。

最终实现：

自动监控 → AI 分析 → Geimori 统一回复引擎 → 飞书人工审核 → 授权后自动发布 → 数据追踪。

第一阶段优先确保 AI 草稿和飞书审核能实际投入使用。Reddit 自动发布在取得授权前保持关闭。

整个系统应该可以长期稳定运行，未来也能扩展到 YouTube 和其他社区平台。