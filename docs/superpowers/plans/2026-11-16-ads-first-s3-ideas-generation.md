# 新开头 S3：按主张出图，不要求方案确认 · 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `ideas_ready` 的项目不用确认事实、不用确认方案，就能为每个组合准备出图草稿、预览、提交；出来的图挂在项目下，出图中的在 `pending` 里。

**Architecture:** 新接口 `prepare_marketing_ideas_generation`（`POST /marketing/ideas/{project_id}/generation-drafts`）只认 `ads_first` 项目，为每个组合建一个出图草稿，并沿用 `marketing_generation_links` 把草稿连到项目和组合。预览和提交仍走现有的 `create_generation_preview`、`submit_generation_run`。出图链路上三处按项目查找的地方按 `flow` 分开处理：`plan_first` 的规则一字不改，`ads_first` 只核对“项目存在、是 `ads_first`、这张广告的内容和 `ideas_json` 一致”。

**Tech Stack:** FastAPI、SQLAlchemy、Pydantic、pytest；OpenAPI 生成脚本和 web 生成的客户端。

**Spec:** 设计文档 [`2026-11-16-ads-first-opening-design.md`](../specs/2026-11-16-ads-first-opening-design.md) §5.3、§6；第 1 步计划 [`2026-11-16-ads-first-step-1.md`](2026-11-16-ads-first-step-1.md) 的 S3 一节和 D1–D9；ADR 0018；Issue heart-cannot-beat/marketai#191（含 #206 留下的更正评论）。

## 开工前先确认（已核实，2026-11-16）

**1. 产品只有创意库草稿、没有已发布版本时，`create_draft` 和预览能不能走通？能。**

- 新建创意库产品条目时（`library.service._bridge_product_identity`），同一事务里就建好桥接的 `products` 行（`prod_` 编号，`product_profile_entry_id` 指向条目）。S1 的 `ads_first` 项目锚在这一行上。
- `create_draft` 用 `prod_` 编号或条目编号都能找到这一行。
- 预览时 `selection.product_input` 先找“编号等于 `product_id` 的创意库条目”。桥接行的编号是 `prod_…`，不是条目编号，所以找不到条目，转而冻结 `products` 行的五列（名称、描述、卖点、受众、品牌说明），`version_id` 为空。不需要已发布版本；执行时 `version_id` 为空就跳过版本检查。
- 测试 `DraftProductTests` 把这条路走到出图完成。
- 由此带来的两点，留给后面的块：
  - 这种产品冻结进配方的“产品事实”只有名称（其余列是空的），出图模型读到的事实很少；这张广告依据的事实在意图的理解快照里，不在提示词的事实段落里。要不要把引用的事实写进出图提示词，留给 S4 或 S11 决定。
  - 草稿以后发布了（S10），出图仍冻结 `products` 行，而不是创意库的发布版本，因为两者编号不同。旧流程里桥接产品的出图今天也是这样（`service.product_card` 只在起草时读发布版本）。S10 做发布时要决定是否改为读发布版本。

**2. #206 留下的更正（Issue 评论）。** `check_frozen_selection` 定义在 `app/generation/selection.py`；出图链路上按意图里的 `project_id` 找项目的三处（`selection.py` 预览和提交各一处、`paid_execution.py` 执行一处）原来只找 `plan_first` 项目。三处都要能处理 `ads_first`。

## 规则

- 准备：项目状态必须是 `ideas_ready` 且有主张，否则 409 `ideas_not_ready`；有起草调用在跑时 409 `planning_in_progress`；`If-Match` 必须是当前修订号，否则 409 `stale_marketing_revision`。不要求任何事实或方案已确认。
- 同一修订号下再准备一次：设置相同就返回同样的草稿；用户选的设置（供应商、型号、画幅、清晰度、张数）不同是 409 `ideas_drafts_already_prepared`。参考照片或品牌标志后来变了不算冲突，返回原来的草稿，预览时照常核对它们是否仍可用。
- 出图设置：供应商、型号、画幅、清晰度、每种讲法出几张（默认 1）。参考照片用产品已核对实物的照片（和旧流程自动选的一样）；用未核对的照片出草稿是 S11。
- 意图（`selectors.marketing_intent`，预览时冻结进配方）：`schema_version: 2`、`flow: "ads_first"`、`project_id`、`plan_revision`（当时的项目修订号）、`ideas_hash`、`combination`、`proposition`、`expression`、`understanding`（每条事实都带 `confirmation`）、`cited_fact_ids`；另带 `ad_language`、`strategy_language`、`brand_kit`，有风格说明时带 `style_variation: "held"` 和 `style`，出图说明照旧由 `service.image_brief` 生成。
- 校验按 `flow` 分开：
  - 预览（`resolve_selection`）：草稿的链接指向哪个项目，就按那个项目自己的 `flow` 走。`plan_first` 不变；`ads_first` 比较草稿里记的这张广告的内容（组合、主张、讲法、广告语言、风格、品牌）和当前主张里的是否一致，一致才用当前的理解重新算一份意图冻结进配方。
  - 提交（`check_frozen_selection`）和执行（`paid_execution`）：意图写着 `flow == "ads_first"` 的，只找 `ads_first` 项目（找不到是 `ideas_unavailable`），再比较内容；其余照旧只找 `plan_first` 项目。
  - 内容不一致是 `ideas_changed`。只是修订号变了不算。
- `get_marketing_ideas` 的 `pending`：当前主张里还没出完的图（排队、进行中），以及最近一次尝试失败的广告；再试一次就替换掉失败的那条。已经出了图的尝试算广告，不算 `pending`（检查中、被检查拦下的也一样）。重新起草后组合编号会重复，内容已经不在当前主张里的尝试不列出。

## 裁定

- `cited_fact_ids` 除了主张和讲法直接引用的事实，还算上主张所引推测背后的事实。改了这些事实，主张本身也站不住。代价：下载前要核对的事实可能多一两条。
- `ads_first` 的草稿按内容判断是否还有效，不按修订号。核对事实、追加主张都会改修订号，不应让已经准备好的草稿作废。代价：如果以后有改动只改了理解而没改主张，旧草稿仍能出图，但配方会记下出图时的理解，S10 据此判断状态。
- 准备接口不接受手选参考照片。选照片的规则在 S11 定，现在先不开这个口子。
- 预览时草稿的产品、组合和链接不一致，一律按 `ideas_changed` 处理（重新准备即可），不复用旧流程那条提到“测试方案”的提示。
- 再准备时只有用户选的设置会冲突，参考照片和品牌标志变了不冲突（整体审查发现：否则核对一张照片后这个修订号就再也准备不了，而新流程没有读回草稿编号的接口）。代价：照片变了以后再准备，拿到的仍是用旧照片的草稿，要换照片得等主张重新起草；S11 定照片规则时可改。

## 整体审查后留下的小问题（未修）

- 同一张广告出多张变体时，`pending` 的“最近一次失败”按创建顺序取最后一条，可能和兄弟变体的结果不一致；多个排队项是同样的条目，没有编号区分。
- 用两个不同的幂等键同时准备，可能有一个撞上唯一约束返回 500（不会多出草稿）；旧流程同样如此。
- 预览时比较的是草稿里记的内容，客户端可以改草稿的提示词和意图（旧流程同样能改提示词）；配方里的意图始终由服务端重算。S10 判断草稿状态时要知道：配方意图不能证明提示词写了什么。
- 新流程没有按项目读回草稿编号的接口，只能再调一次准备接口（设置相同）拿到。

## Task 1：测试先行

**Files:** `tests/test_marketing_ideas_generation.py`（新）

- [ ] 事实全部未核对时，准备、预览、提交、执行都成功；图在 `ads` 里、状态 `draft`；项目仍是 `ideas_ready`，没有方案。
- [ ] 配方里的意图字段齐全；理解里每条事实带核对状态；`cited_fact_ids` 正确（含推测背后的事实）。
- [ ] 出图中的在 `pending`（`queued`），失败的显示 `failed`，再试一次后被替换。
- [ ] `set_marketing_ad_kept` 对真实出的图生效。
- [ ] 同修订号再准备返回同样的草稿；设置不同 409；同一个幂等键重放；没有主张 409；起草中 409；旧修订号 409；缺 `If-Match` 428；未知型号 422。
- [ ] 内容校验：只改修订号不影响；改了这个组合的内容，预览、提交、执行分别被拒；改别的组合不影响；重新起草理解后旧草稿被拒；冻结的意图只找自己 `flow` 的项目。
- [ ] `plan_first`：没确认方案仍是 409 `plan_not_confirmed`；两种项目互相调不到对方的准备接口；确认过方案的旧流程照常出图。
- [ ] 另一个工作区：准备、读、预览、留下都是 404；看不到这个项目的 `ads` 和 `pending`；冻结的意图在另一个工作区找不到项目。
- [ ] 创意库草稿产品（开工前确认第 1 条）走到出图完成。

## Task 2：后端实现

**Files:** `app/marketing/ideas.py`（`ideas_intent`、`generation_drafts`、`prepared_drafts`、`pending_ads`、`selection_intent`、`check_intent`）、`app/marketing/ideas_contracts.py`、`app/marketing/ideas_api.py`、`app/generation/selection.py`、`app/generation/paid_execution.py`。

## Task 3：合同和前端客户端

按第 1 步计划 Global Constraints 的新增接口六步：`python scripts/generate_openapi.py`；策略条目照 `prepare_marketing_generation`（`maxRequestBytes` 8192、`forwardConditionalHeaders: true`）；`bash scripts/generate_all.sh`；`DomainGateway.prepareMarketingIdeasGeneration`；接口数 247 → 248；`marketing-ideas-contracts.spec.ts` 加请求、返回和 `pending` 的样例。新错误码 `ideas_not_ready`、`ideas_changed`、`ideas_unavailable`、`ideas_drafts_already_prepared`、`ad_brief_too_long` 的中英文提示。还没有页面用通用 hook，第 5 步不做。

## Task 4：文档和检查

- 营销测试规格 §6 的 ADR 0018 修订说明下补一段出图规则。
- `make verify`、`make web-verify`。
