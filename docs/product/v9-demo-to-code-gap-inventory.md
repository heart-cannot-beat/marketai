# v9 演示版到当前代码的能力差距盘点

- 状态：Baseline inventory
- 盘点日期：2026-08-25
- 代码基线：`91212fe`
- 对应任务：[GitHub #11](https://github.com/Raybbish/marketai/issues/11)
- 验收基准：[v9 六区功能对齐验收合同](v9-functional-alignment-acceptance.md)

## 1. 结论

当前仓库不是“只有 Demo”，但也还不是 v9 SaaS。它已经拥有一套有价值的生产底座：SQLAlchemy/Alembic、持久化 Worker、生成防重、成本账本、AssetVersion、Meta 只读采集、不可变实验和 PAUSED Launch Saga 均存在真实代码与自动化测试。主要缺口集中在两端：浏览器仍是旧的前期生图工作台，SaaS 身份与租户执行边界尚未进入运行时代码；六区中的本地化和放量没有后端实现，创意库只有产品与维度这一小部分。

六区总体状态如下：

| 功能区 | 总体判定 | 已有真实能力 | 阻断功能对齐的主要缺口 |
|---|---|---|---|
| 新建生成 | Partial | 产品、参考图、维度、全组合/覆盖采样、Provider、持久化 Run/Asset、Worker、防重和成本 | 完整配方快照、Seed/复现、矛盾组合、QC 循环、单项恢复入口、跳过 Loser、Mock 证据和 v9 UI |
| 素材 | Partial | 跨 Run 画廊、标签/Provider/双状态筛选、追加式人工审核、Lightbox、基础 ZIP 导出 | 全屏键盘审核、按 AssetVersion 审核、再生成/版本链、质量回流、完整 Manifest 与哈希去重 |
| 投放实验 | Partial | 不可变冻结、纯 judge、Meta daily、追加 Judgement、Preview/确认/PAUSED Saga | v9 UI、Approved 门禁、概念去重、人工确认终判、inconclusive 投影隔离、整轮 Launch 与归因/Pattern 视图 |
| 本地化 | Absent | AssetVersion 预留了 `localized` 和 `truth_package_id` 字段 | Truth Packet、数据源、批次/矩阵、Worker、幂等、QC、市场规则、API 和 UI 均不存在 |
| 放量 | Absent | Meta daily 已保存 spend/ROAS/CPA 所需原始量、CTR、frequency 和 CPM | ScalePolicy、ScaleAdvice、证据快照、状态机、草稿转换、API 和 UI 均不存在 |
| 创意库 | Partial | 产品 CRUD、产品参考图、版本化维度 schema | must-match/参考分组、人物、模板、Prompt 规则、数据源、统一版本链、凭据边界和创意库 UI |

跨区黄金路径当前只能由内部 Python smoke 脚本走到生成、审核、实验、inconclusive、关闭实验和 Fake PAUSED Launch；它不是登录后的浏览器流程，也没有本地化与放量，因此不能满足功能对齐合同。

## 2. 盘点方法与判定标准

### 2.1 来源

- 演示行为：`_prototype/MarketAI-v9-完整演示.html`
- 操作语义：`_prototype/MarketAI使用手册-v9.html`
- 定案标准：`docs/product/v9-functional-alignment-acceptance.md`
- 运行时代码：`app/`、`worker/`、`static/`、`migrations/`
- 证明材料：`tests/test_foundation.py`、`scripts/smoke_business_flow.py`

演示文件通过 `window.__mockApi` 和页面内 `state` 实现所有交互（`_prototype/MarketAI-v9-完整演示.html:1444`、`:1911`、`:1932`），所以“演示里按钮可点”只用于抽取能力，不计为当前实现。

两份演示原型已于 2026-11-05 移出本仓库，只保留在私有归档仓库；本文的文件名和行号指归档版本。

### 2.2 状态定义

- **Real**：存在持久化领域实现和可调用路径，并有自动化测试或可重复验证证据。
- **Partial**：已有可复用实现，但缺少合同要求的关键状态、边界、UI、恢复或测试。
- **Absent**：运行时代码中没有对应实体、路由或 Worker；只有 Demo 内存逻辑、注释或预留字段不算实现。
- **Demo-only**：仅存在于 `_prototype` 的 `window.__mockApi` 或固定 seed 数据中。

本次运行 `make test`，11 个隔离测试全部通过；测试使用临时 SQLite，不修改客户或开发数据。当前没有浏览器 E2E、PostgreSQL/RLS 或 SaaS 身份测试。

## 3. 跨区与 SaaS 基础

| 能力 | 状态 | 当前证据 | 与合同的差距 |
|---|---|---|---|
| SQL 持久化与迁移 | Real/Partial | SQLAlchemy records 覆盖 Workspace 至 Experiment；8 个 Alembic migration；11 个测试会从空库升级到 head | 默认和测试环境仍是 SQLite；未证明 PostgreSQL 建库、升级、并发或 RLS |
| Workspace 数据列 | Partial | 主要记录均有 `workspace_id`，见 `app/adapters/persistence/models.py` | repository、实验、Meta 和 Launch 全部读取 `config.DEFAULT_WORKSPACE_ID`，见 `app/store.py:95-939`；这是部署级单租户开关，不是请求级租户隔离 |
| 登录、成员关系和角色 | Absent | 运行时代码没有 auth、JWT、JWKS、membership 或 invite 路由 | 无 SaaS 控制面/BFF、Owner/Member 策略、会话撤销、授权租约和双 actor 身份 |
| PostgreSQL RLS | Absent | `app/adapters/persistence/database.py` 只按 URL 建 engine | 没有 TenantContext、RLS policy、API/dispatcher/handler 数据库角色和跨租户测试 |
| 持久化任务与租约 | Real | `app/platform/jobs.py` 实现防重、租约、心跳、retry_wait 和 uncertain；`app/worker_runtime.py:46-61` 分派任务 | Worker 只认识 generate_asset、meta_read_ingestion、launch_canary；本地化、导出和其他异步工作没有任务类型 |
| 崩溃恢复 | Real/Partial | Mock 生成 kill/restart 和 Launch 逐步恢复有测试；非安全付费调用进入 uncertain | 没有用户可见的 resume/reconcile 入口；真实生成 Provider 的 uncertain 尚不能完成业务对账 |
| 成本账本与硬上限 | Real/Partial | `app/store.py:428-520` 原子创建 Run、任务和 reserved 记录，`:679-714` 释放并记 actual；超限测试通过 | 只覆盖图片生成；上限是全局环境变量，不是 Owner 配置；没有 Workspace quota、BYOK 归属、Provider usage 对账或本地化成本 |
| BYOK 与凭据边界 | Absent | AI 与 Meta 凭据来自进程级 `.env`，见 `app/config.py` | 没有 Workspace 凭据版本、KMS envelope encryption、轮换、禁用和 Member 权限隔离 |
| 受控 Mock | Partial | MockProvider 免费且生成产物记录 provider=`mock`；Fake Meta Adapter 仅能作为测试 adapter 注入 | 没有统一 execution_mode、scenario、规则/模型版本和页面级显著标记；导出 Manifest 不含完整 Mock 证据 |
| 审计与可观测性 | Partial | Launch 有 append-only audit；任务和 Meta 保存错误码及关联 ID | 产品、生成、审核、实验创建、导出等没有统一双 actor 审计；没有全链路结构化日志合同 |
| 浏览器黄金路径 | Absent | `scripts/smoke_business_flow.py` 能走内部领域路径 | 没有登录、Workspace UI、六区浏览器流程、刷新/重启 E2E 或 PostgreSQL 证据 |

最高风险不是“表里没有 workspace_id”，而是“表里有、运行时却全部硬编码默认 Workspace”。在接入 BFF 之前继续扩展六区，会把单租户假设复制到更多 service 和 migration 中。

## 4. 新建生成

| 能力 | 状态 | 当前证据 | 差距 |
|---|---|---|---|
| 产品创建与编辑 | Partial | `/api/products` GET/POST/PUT，`ProductRecord` 持久化产品经济参数 | 没有 must-match、参考图分组、外部商品 ID、不可变产品版本；更新覆盖同一行 |
| 产品参考图 | Partial | `/api/products/{pid}/refs` 上传/删除；生成指纹包含参考图 SHA-256 | 文件存在本地磁盘且以名称挂在产品上；没有 AssetVersion、对象存储、分组、引用版本和 Workspace 安全下载 |
| AI/手工维度 | Real/Partial | `/angles/generate` 和 GET/PUT；`DimensionSchemaRecord` 保存版本与 snapshot hash；钩子维度存在 | 无创意库模板来源版本、锁定规则、人物实时引用解析和结构化富 JSON |
| 全组合与覆盖采样 | Real | `app/pipeline.py:17-67` 实现笛卡尔积和单值/两两覆盖贪心采样；Plan/PlanEntry 持久化 | 没有矛盾组合约束模型或排除报告 |
| Provider/模型/画幅/分辨率/变体/参考图 | Partial | `GenRequest`、Provider registry 和旧 UI 均支持这些基础参数 | 没有生成意图、Seed、负面 Prompt、引用组选取、skip_losers；最大变体数在后端静默限制为 4 |
| Prompt 组装 | Partial | `app/angle_engine.py:111-145` 确定性组装产品、维度片段、风格、品牌、画幅和保真句 | 未实现定案的版本化 Prompt 规则、must-match、结构化创意数据、QC 反馈和锁定短语；规则来源不可审计 |
| GenerationRun 快照 | Partial | Product/Dimension/Plan 分别持久化，Asset 保存最终 Prompt，Run 保存 Provider 参数 | Run 没有冻结产品版本、维度 schema ID、引用 AssetVersion、规则版本、组合排除、执行模式和完整 request JSON |
| 独立生成项 | Real/Partial | 每个组合变体预建一个 Asset 和一个 generate_asset Job，可独立成功/失败 | Asset 兼任任务项和业务素材，缺少明确 RunItem 尝试历史、QC 状态和每次外部请求证据 |
| 请求防重与素材复用 | Real | 指纹覆盖产品、组合值、Prompt、Provider、模型、画幅、尺寸、参考哈希和变体；成功文件硬链接或复制复用 | 没有 HTTP idempotency key 合同；`force` 可以绕过语义复用但 UI 不解释新付费意图 |
| Worker 与成本 | Real/Partial | Run、Job、reserved 在同一事务；Mock kill/restart 不重复产物；Decimal 成本硬上限 | Provider 内部重试没有独立 Attempt 记录；真实付费不确定结果只停在 uncertain，无用户恢复闭环 |
| Seed 与配方复现 | Absent | Asset/AssetVersion/Provider 接口没有 seed 字段，Mock 仅从 Prompt 内部派生颜色 | 无 same-recipe、新 Seed 或编辑 Prompt 再生成 API，无法满足版本链与复现披露 |
| 自动 QC 与反馈循环 | Absent | Worker 成功后直接 complete_asset | 无图像 QC、失败原因结构、反馈入 Prompt、三次上限或自动 Rejected |
| 质量/投放回流生成 | Absent | Demo 有 `/api/runs/retest` 和 `skip_losers`；运行时路由无对应项 | 无 Needs Changes ×2、Approved ×3、Learning 下一轮或 Loser 标签跳过 |
| v9 新建生成 UI | Partial | `static/index.html:22-120` 是产品→维度→组合→生成四段表单 | 不在六区 SaaS 壳中；没有用户/Workspace、Mock 证据、失败恢复、跳过报告或完整参数 |

## 5. 素材

| 能力 | 状态 | 当前证据 | 差距 |
|---|---|---|---|
| 批次轨 | Partial | `static/app.js:325-366` 轮询持久化 Run 和 Asset；可以取消 Run | 只展示粗粒度 pending/ok/failed；没有 Job 尝试、uncertain、QC、逐项 retry/resume 和部分成功行动 |
| 跨轮画廊与筛选 | Real/Partial | `/api/products/{pid}/assets` 聚合所有 Run；UI 按实际 tags、Provider、quality、verdict 筛选 | 绑定单产品旧页面；没有 AssetVersion/语言/实验上下文选择和 Workspace 级素材视图 |
| 人工审核 | Partial | QualityReviewRecord 追加写，当前投影从最新记录计算；网格和 Lightbox 可点三态 | Review 绑定 `asset_id` 而不是 `asset_version_id`（`models.py:217-227`）；无法正确处理再生成版本 |
| 全屏键盘审核 | Absent | 旧 UI 只监听 Escape（`static/app.js:493-495`） | 缺 A/C/X/空格/回退、自动下一张、完成总结与三出口 |
| 双状态显示 | Partial | 人工 quality 和只读 experiment verdict 分开返回；直接 verdict HTTP 端点已禁用 | `get_verdicts` 会把最新 ExperimentDecision 投影到 Asset；inconclusive 也可能成为素材 verdict，且上下文没有市场/版位/LP/Offer 全量快照 |
| Lightbox | Partial | 展示标签、模型、Prompt、quality、verdict，可复制 Prompt | 不展示 Seed、AssetVersion、版本历史、引用组、QC、成本、Mock 场景和完整实验上下文 |
| 单张再生成 | Absent | Demo 定义 `/api/assets/{id}/regenerate`；运行时无路由 | 无配方复现、新 Seed、编辑 Prompt、新 AssetVersion、父版本关联和新版本待审核 |
| 质量回流 | Absent | Demo/手册定义 Needs Changes ×2、Approved ×3 | 无 Run 创建、成本预览、防重、跳过 Loser 或状态反馈 |
| ZIP + Manifest | Partial | `/api/products/{pid}/export` 支持 approved/winner/all，包含标签、Provider、模型、双状态、上下文和 Prompt | 未按文件哈希去重；缺 Seed、AssetVersion、QC、引用素材、文件哈希、生成配方/Run 和 Mock 证据；下载未做请求级 Workspace 授权 |

## 6. 投放实验

| 能力 | 状态 | 当前证据 | 差距 |
|---|---|---|---|
| 实验 API 与持久化 | Real | create/get/freeze/bind/judge/close 路由；Experiment、Entry、StateEvent、Judgement、Decision 表 | 没有列表、取消、终判确认、建议和面向 UI 的查询模型 |
| Approved 候选门禁 | Absent | `create_experiment` 只验证 AssetVersion 存在且属于产品（`app/experiments/service.py:67-80`） | 未检查对应版本的人工质量为 Approved |
| 概念相似去重 | Absent | 无 concept/entity 模型或服务 | 无相似警告、用户确认和每概念一个候选 |
| 不可变冻结 | Real/Partial | freeze 保存版本内容哈希、组合标签、营销变量、统计 config 和 metric contract hash；revision 乐观锁 | 未把预算、周期、受众、版位、Landing Page/Offer 的规范字段和归因合同正文全部冻结；marketing_vars 是无 schema dict |
| 归因窗口 | Real | `app/meta/contracts.py:31` 固定 `7d_click + 1d_view` 并进入 contract hash；测试覆盖旧新 hash 不同 | UI 无法看到实验使用的具体 contract；尚无 contract/canary 升级 UX |
| Meta 只读采集 | Real | 原始分页 gzip、游标续拉、daily/status 投影、对账、调度和新口径重启观察时钟 | 仍是进程级单一 Meta 账户配置；无 Workspace Meta connection 或 SaaS UI |
| 纯函数 judge | Real | CTR Wilson，CPA/ROAS 确定性 bootstrap，阈值、样本门槛和 inconclusive；确定性测试通过 | 产品盈亏线没有从 ProductEconomics 自动形成冻结规则；没有判定预览与人工确认接口 |
| 判定写回 | Partial/不符合合同 | Judgement 和 ExperimentDecision 追加保存 | `run_judgement` 立即写 Decision，`get_verdicts` 立即投影；没有“建议→人工确认→素材结论”边界，且 inconclusive 会被投影为素材 verdict |
| Launch Preview/确认/Saga | Real/Partial | 单 Entry Preview hash、短时 token、五步 Saga、marker 恢复、三层 PAUSED 回读和 Fake 故障注入均存在 | 仅最小 canary，不是整轮 N 个候选；无 ABO 等额预算编排、整轮成本展示或浏览器 UI；客户 Sandbox 配置未产品化 |
| 归因卡/漏斗/下一轮配方 | Absent | daily 表已保存必要基础指标 | 无聚合查询、Pattern 实体、归因卡、逐日漏斗、CPM 比较和配方生成服务/UI |
| v9 投放实验 UI | Absent | 旧 `static/` 没有实验页面 | Prototype 的完整页面仍由内存数据和 `window.__mockApi` 驱动 |

## 7. 本地化

当前状态为 **Absent**。`AssetVersion.kind` 接受 `localized` 且有 `truth_package_id` 字段（`app/schemas.py:176-188`、`app/adapters/persistence/models.py:197-214`），但它们只是前置形状，没有对应外键实体、服务或任务。

| 能力 | 状态 | 当前证据 | 差距 |
|---|---|---|---|
| Winner 输入与去重 | Absent | Prototype 由内存 verdict 过滤 | 无查询、资格门禁和来源去重 |
| 数据源 | Absent | Prototype 有 `/api/datasource` 和 test Mock | 无人工表、Shopify Adapter、凭据版本、连接测试、市场取价上下文和失效事件 |
| Truth Packet | Absent | Prototype 有 `/api/localize/truth` | 无表、版本、full/degraded/failed、事实来源、币种纪律或价格变更失效 |
| 批次与任务矩阵 | Absent | Prototype 有 `/api/localize/start/state` | 无 Batch、source×market Item、并发 3、幂等指纹和状态机 |
| 处理与 QC | Absent | Prototype 使用 canvas 与确定性假 QC | 无图像分析、编辑 Adapter、视觉 QC、反馈重试、失败原因和成本 |
| 市场/声明规则 | Absent | 规则只存在 Prototype/手册 | 无九市场配置、en-US 排除、四声明开关、英语跳过和只替换不新增约束 |
| UI | Absent | 旧 `static/` 没有本地化导航或页面 | 全部交互仍是 Demo-only |

## 8. 放量

当前状态为 **Absent**。Meta daily 表已经保存 spend、impressions、clicks、purchase value、purchase ROAS、frequency 和 CPM，可作为后续证据来源；除此之外没有放量领域代码。

| 能力 | 状态 | 当前证据 | 差距 |
|---|---|---|---|
| 指标来源 | Partial | `AdDailyMetricRecord` 和 Meta 投影保存趋势计算所需基础量 | 无观察窗口聚合、版位/受众/地区钱流模型和数据质量门禁 |
| 规则版本 | Absent | Prototype 使用固定疲劳样本规则 | 无 ScalePolicyVersion、阈值配置、重放和解释 |
| ScaleAdvice | Absent | 无 record、service 或 route | 无证据快照、原因、proposed/acknowledged/dismissed/converted 状态 |
| 转换生成草稿 | Absent | 无 Draft GenerationRun 概念 | 无预填配方、人工确认和不付费边界 |
| 安全边界 | Partial | Launch 默认禁用真实写入 | 放量本身没有显式“只建议、绝不改广告/预算”的 command boundary 与测试 |
| UI | Absent | 旧 `static/` 无放量页；Prototype 明确标注“占位” | 固定建议卡不能复用，必须从持久化指标和 ScaleAdvice 构建 |

## 9. 创意库

| 能力 | 状态 | 当前证据 | 差距 |
|---|---|---|---|
| 产品档案 | Partial | Product CRUD、经济参数和参考图真实持久化 | 无 must-match、参考分组、商品 ID、不可变 ProductVersion 和引用影响分析 |
| 人物库 | Absent | Prototype 有 `/api/persons` 与人物 refs | 无 Person/Profile/Reference/Version 实体、路由和任务解析 |
| 维度模板 | Absent/可复用基础 | 现有 DimensionSchema 已版本化；Prototype 有 `/api/dim_templates` | 无跨产品 Template 实体、套用深拷贝命令、模板版本和引用追踪 |
| Prompt 规则 | Absent | 规则硬编码在 `app/angle_engine.py`；Prototype 有 GET/PUT `/api/prompt_rules` | 无 PromptRuleVersion、锁定短语校验/自动恢复、审计和 Run 快照引用 |
| 数据源 | Absent | 仅 Prototype 内存接口 | 无人工/Shopify Adapter、版本、凭据边界、测试和 Truth Packet 失效 |
| 创意库 UI | Absent | 旧 `static/` 只有产品与维度编辑 | 无五类对象的三栏管理、版本历史、引用关系和权限状态 |

## 10. 当前测试证据与空白

### 已证明

- Alembic 可从临时空 SQLite 升到 head；
- Mock 生成可持久化并创建 AssetVersion；
- 人工 quality 与兼容 verdict 历史为追加式；
- 非安全付费任务租约过期进入 uncertain；
- 生成成本硬上限在入队前拒绝；
- JSON 导入幂等；
- judge 确定且不会把不足数据提升为 Winner；
- 实验冻结、重放判定、关闭和追加记录；
- Mock Worker 被 kill 后安全恢复；
- Launch 确认、防漂移、五步 PAUSED Saga 和逐步恢复；
- Meta `7d_click + 1d_view` contract、分页断点、原始页、daily 和状态投影。

### 未证明

- PostgreSQL migration、并发和 RLS；
- SaaS 登录、邀请、Owner/Member、撤销和 Workspace 切换；
- 浏览器 E2E、无障碍和刷新/重启恢复；
- 真实 Provider 的 usage、Seed、QC 和 uncertain 对账；
- AssetVersion 级审核、再生成、完整导出；
- Approved 实验门禁、概念去重和人工确认终判；
- 本地化、放量和完整创意库；
- 受控 Mock 与真实指标/结论的数据库级不可混用；
- Workspace BYOK、成本上限和凭据轮换。

## 11. 必须先修正的语义风险

### P0 — 默认 Workspace 不是租户隔离

`workspace_id` 已存在于表中，但所有 service 通过 `config.DEFAULT_WORKSPACE_ID` 选择租户。若直接在现有模式上增加 SaaS 页面，会产生横向越权风险，也会迫使后续逐个重写 service。接入 BFF 前必须建立请求级 TenantContext 和 PostgreSQL RLS。

### P0 — 当前浏览器不是 v9 产品壳

`static/index.html` 只有产品、角度、组合、批量生成四段侧栏与 Run/Gallery 两个 tab（`:22-140`）。实验、Meta、Launch 的真实后端能力完全没有客户 UI；本地化、放量和创意库也没有运行时页面。

### P1 — 双状态已分开，但版本和确认边界仍错误

人工审核写在 Asset 而不是 AssetVersion；实验创建不验证 Approved；judge 运行后立即写 ExperimentDecision 并被素材投影读取，inconclusive 也可能显示成 verdict。应先把“统计判定”“人工确认”“上下文化素材结论”建成三个明确阶段，再接 v9 实验 UI。

### P1 — 生成无法复现

Provider 接口、Asset 和 AssetVersion 都没有 Seed；Run 也没有完整配方快照或规则版本。当前指纹适合防重，但不能承担用户可理解的配方复现和版本链。

### P1 — 全局凭据和全局成本配置不能服务多客户

AI/Meta token、Workspace ID 和成本上限来自进程环境变量。它们适合单部署内部验证，不满足多 Workspace BYOK、Owner 上限、轮换、撤销和审计。

### P2 — Schema 占位容易被误认为功能

`AssetVersion.kind=localized` 和 `truth_package_id` 并不代表本地化已经存在；Meta daily 有 frequency 也不代表放量已经存在。验收必须以实体、命令、Worker、UI 和测试形成闭环为准。

## 12. 建议施工切片

以下顺序减少返工，并保持每个切片都能产生可验证的用户价值：

1. **SaaS 执行边界**：确定前端壳/BFF/FastAPI 合同，接入请求级 TenantContext、PostgreSQL 和最小 RLS；先迁移现有产品/生成查询，不新增六区业务。
2. **创意库上游真值**：实现 ProductVersion、must-match、人物、维度模板、PromptRuleVersion 和引用快照；数据源凭据接口先定义但不立即接 Shopify。
3. **生成→素材纵向切片**：补 GenerationRecipeSnapshot、RunItem/Attempt、Seed、统一 Mock provenance、AssetVersion 级审核、再生成、QC、质量回流和完整 Manifest，并在新 SaaS UI 走通。
4. **实验语义与 UI**：补 Approved 门禁、概念去重、规范化冻结字段、判定预览/人工确认/素材结论三阶段，再把现有 Meta/Launch 能力接入整轮实验页。
5. **本地化纵向切片**：先人工数据源和 Truth Packet，再任务矩阵、Mock 编辑/QC、失败恢复和九市场 UI；Shopify 作为可替换 Adapter 后接。
6. **放量纵向切片**：实现 ScalePolicyVersion、指标证据快照、ScaleAdvice 和生成草稿转换；保持纯建议边界。
7. **黄金路径硬化**：邀请登录、六区浏览器 E2E、Worker 故障注入、跨租户/RLS、PostgreSQL migration、成本和 Mock/真实不可混用全部通过。

现阶段不建议直接把 277KB 演示 HTML 接到零散 API 上。正确复用对象是其信息架构和交互语义；运行时应由新的 SaaS 壳调用上述纵向切片，而不是继续扩展 `window.__mockApi` 或让浏览器保存业务真值。
