# 先看广告，再定测试 · 第 2 步实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用户从广告想法页上留下的广告出发，回答三个问题、点一次“开始测试 · 最多 $X”，拿到广告包；投出去之后，把 Meta 的完整报表传回来，在“测试进行中”看进度、到期看最小结果，在“测试记录”看历史。

**Architecture:** 每次“拿去测试”新建一个测试项目（`MarketingProjectRecord`，`flow = "ads_test"`），用留下的广告和回答按模板生成整份方案并记为已确认，用现有的小轮代码冻结、挂入广告、做“一起看”的检查、记录这一轮的审核、生成广告包。报表、对上广告、评估、结束沿用小轮代码；只对测试项目的轮次换上新的数据规则（完整报表覆盖、按广告名预填、改开始日期、提前结束、补记偏差）。改版前的测试（`plan_first`）和广告想法项目（`ads_first`）的行为都不变。

**Tech Stack:** FastAPI、SQLAlchemy、Alembic、PostgreSQL RLS（本地 SQLite）、后台 Worker；Next.js pages router、SWR、next-i18next、Jest、Playwright。

**Spec:** [`docs/superpowers/specs/2026-11-21-ads-first-step-2-take-to-test-design.md`](../specs/2026-11-21-ads-first-step-2-take-to-test-design.md)（上位设计 [`2026-11-16-ads-first-opening-design.md`](../specs/2026-11-16-ads-first-opening-design.md) §5.4）

**这份计划怎么用：** 第 2 步拆成 14 块（T0–T13），一块对应一个 GitHub Issue、一个分支、一个 PR。本文给出全部分块、块与块之间的接口、里程碑，以及 T0 和 T1 的逐步任务。T2 起每一块开工时，按本文的分块说明再写一份自己的逐步计划，放在 `docs/superpowers/plans/`。

## Global Constraints

- 第 1 步计划（[`2026-11-16-ads-first-step-1.md`](2026-11-16-ads-first-step-1.md)）的 Global Constraints 全部照旧：一个 Issue 一个分支一个 PR；公开仓库只用 Acme Charge、Brightvolt、Northpeak、Kestrel Power 和 `*.example`；每张新表强制 RLS、`tenant_isolation`、三种角色授权、同步 `deploy/postgres/domain-tenant-roles.sql`、登记 `tests/test_postgres_rls.py`；新接口六步；两套文案；每个按工作区存取的数据都有“另一个工作区读不到、改不了”的负向测试。
- 迁移文件名 `zz` + 下一个字母 + Issue 编号 + `a1b2c3`，`down_revision` 接当前 head（写本文时是 `zzr233a1b2c3`）。
- 旧流程不变：`flow = "plan_first"` 的项目和轮次，状态机、接口、返回字段、上传和评估规则一个都不改；`ads_first` 的广告想法接口不变。
- 新的数据规则（设计 §5.7）只在轮次所属项目的 `flow == "ads_test"` 时生效。
- 评估规则不改：`EvaluationProtocol`（`app/marketing/workflow_contracts.py`）、成熟判断（`today > end_date + attribution_lag_days`）、`app/experiments/judge.judge`。
- 付费调用只由 API 受理（持久任务、幂等、预留、结算、结果不确定走现有核对）；第 2 步不让 Worker 受理任何付费工作，也不给 Worker 加新的写权限。
- 不在主流程里出现 Mock：测试项目的执行方式跟着广告想法项目的广告走，页面上没有 Mock 入口；报表的 `source_mode` 在测试项目里只接受 `customer_file`。
- 网页这一层（BFF）对合同策略里没声明的 `Location` 回应头返回 502：新接口不带 `Location`。
- 前端生成的校验器是严格的：现有接口的返回不加字段；新数据走新接口。
- 界面文案同时写进 `web/locales/en` 和 `web/locales/zh-CN`，键一一对应。主流程不出现内部编号和上位设计 §9 左列的词。新画面都在 `NEXT_PUBLIC_MARKETAI_ADS_FIRST` 后面。
- Meta 写入保持关闭；`META_WRITE_ACCESS_TOKEN` 保持为空。

## Review Focus

设计文档没有逐条写、但用户最可能撞上的五种情况。每条的测试写在所属分块里。

1. **用户在“拿去测试”画面打开期间，又在别处取消了一张留下的广告或改了文案。** 点开始时应得到 409“留下的广告有变化，请看一眼再开始”，不会用旧的组合开测试。测试在 T3（报价 hash 覆盖留下的广告和它们的内容）。
2. **两个人同时点“开始测试”，或同一个人连点。** 只应有一个测试、一份检查预留。测试在 T3（幂等键加“同一个产品只有一个进行中的测试”）。
3. **用户的 Meta 账户币种不是 USD。** 第一次传报表时应得到明确的说法（报表是 EUR，这个测试是按 USD 定的），不是静默算错。测试在 T5；T10 在“其他都按默认”里让币种可改。
4. **“一起看”的检查跑到一半页面被关掉，或结果不确定。** 回来时页面应接着显示检查进度或结果；不确定的不重发、不生成广告包，按现有核对处理。测试在 T4。
5. **用户在 Meta 里改了广告名，短键没了；或者一个广告名里带了两个短键。** 不应预填错；对不上的列出来让用户选。测试在 T6。

---

## 分块总表

| 块 | 内容 | 依赖 | 里程碑 |
| --- | --- | --- | --- |
| T0 | 合同、ADR、设计和计划入库 | 无 | A |
| T1 | 后端：测试项目和读接口 | T0 | A |
| T2 | 后端：按模板生成方案，“够不够”报价 | T1 | A |
| T3 | 后端：开始测试（建项目、确认方案、冻结、挂入广告、受理“一起看”的检查） | T2 | A |
| T4 | 后端：检查之后（记录这一轮的审核、生成广告包、接受发现、取消测试） | T3 | A |
| T5 | 后端：测试轮次的报表（完整报表覆盖、其他行只存不算） | T1 | B |
| T6 | 后端：按广告名预填对应、一次确认对应 | T5 | B |
| T7 | 后端：这一轮的安排（改开始日期、提前结束、看结果） | T1 | B |
| T8 | 后端：目前的数字、还差多少单、补记偏差、改了广告想法的提示 | T5、T7 | B |
| T9 | 后端：测试记录（按产品） | T1 | C |
| T10 | 前端：“拿去测试”画面 | T2、T3、T4 | A |
| T11 | 前端：“测试进行中”画面 | T6、T8、T10 | B |
| T12 | 前端：“测试记录”画面、产品内“测试”视图、总览状态 | T9、T11 | C |
| T13 | 收尾：端到端、教程、验收记录 | 全部 | C |

**Issue：** 依赖图是 heart-cannot-beat/marketai#245。T0 到 T13 依次是 #246、#247、#248、#249、#250、#251、#252、#253、#254、#255、#256、#257、#258、#259。迁移的 revision 用所属块的 Issue 编号（T1 是 `zzt247a1b2c3`：`s` 已被 #241 的 `zzs241a1b2c3` 用掉，T1 接在它后面）。

**里程碑 A：留下的广告到广告包。** 做完 T0–T4、T10：在开关打开的环境里，从留下的广告回答三个问题、点开始，检查通过后下载广告包。

**里程碑 B：数据回来。** 做完 T5–T8、T11：传完整报表、按广告名确认对应、看目前的数字、改开始日期、补记偏差、提前结束、到期看最小结果。

**里程碑 C：第 2 步完成。** 做完 T9、T12、T13：测试记录，设计 §7 的 12 条验收全部满足。

**可以并行的：** T1 之后，T2→T3→T4 这条主链和 T5→T6、T7、T9 可以同时做（文件见各块，T5 和 T7 都碰 `app/marketing/workflow.py`，同时做时 T7 开 PR 前合一次 `main`）。T10 在 T4 合并后开工；T11 在 T6、T8、T10 之后。

## 跨块的共同决定

- **E1 测试项目。** `MarketingProjectRecord.flow = "ads_test"`（常量 `app/marketing/ad_tests.py::FLOW = "ads_test"`；模块名用 `ad_tests`，不和 pytest 的 `test_*.py` 规则、顶层 `tests` 包混淆）。新列 `source_project_id`（可空，`(workspace_id, source_project_id)` 组合外键指向 `marketing_projects`），测试项目必填、指向它来自的广告想法项目。测试项目的 `state` 只有 `plan_confirmed`；`revision` 照常递增。`understanding_json` 是 `app/marketing/contracts.py::Understanding` 的形状（从 `IdeasUnderstanding` 去掉 `unsupported_claims`）。
- **E2 按小轮查项目。** 新函数 `service.get_round_project(session, ctx, project_id) -> MarketingProjectRecord`，接受 `flow in service.ROUND_FLOWS`（`("plan_first", "ads_test")`）。按小轮编号操作的接口（冻结之后的：读轮次、复核、AI 检查、交接包、对上广告、报表、评估、结束）用 `workflow.get_round` 按编号在本工作区里找轮次，本来就不看项目的 `flow`，测试项目的轮次照样找得到；这些路径里要读项目的地方改用 `get_round_project`（T1 时是读轮次的 `include_reuse_lineage` 和 `include_board` 两处，见 #247 的说明）。看板上的“可以出图”对测试项目恒为否。改方案、重新起草、改产品理解、按方案出图、冻结、读 `workflow`、`list_marketing_projects` 仍用 `get_project(flow="plan_first")`，对测试项目 404；按小轮编号的出图（`prepare_marketing_feedback`、`create_marketing_image_edit`）和结果解释（`start_marketing_explanation`，第 3 步）也一样。
- **E3 进行中的测试。** 一个测试项目“进行中”＝它有一轮还没结束（`marketing_rounds.state != "closed"`）。`ad_tests.in_progress(session, ctx, product_id) -> MarketingProjectRecord | None`；开始测试时有进行中的就 409 `test_in_progress`。
- **E4 留下的广告挂进小轮。** 复用 `MarketingCreativeReuseRecord`，`source_json = {"kind": "ideas", "ideas_project_id", "asset_version_id", "intent_hash", "verification_record_id"}`，`combination_id` 是这张广告出图意图里的组合编号。`reuse.creatives` 认这种记录；`workflow.ready` 对 `kind == "ideas"` 的图：照片是否最新按 `ideas_export` 的照片状态，不按 `product_references.lineage_current`；其余条件不变。广告包生成之后，这种图的“是否可用”按生成广告包那一刻的判断，不再随广告想法之后的修改变化（设计 §5.8）：评估不因之后的修改改变，广告包照样能下载；改了什么由 T8 的提示告诉用户。不加表、不加权限。
- **E5 审核记录。** 只写这一轮的 `MarketingQCRecord`（`marketing_qc_reviews`），不写 `QualityReviewRecord`。开始测试前，若某张广告在共享审核链上当前是不通过，409 `ad_review_rejected`。每一项记为通过的检查在记录里带 `evidence`：`{"source": "ideas_check" | "paired_check" | "verification", "id": ..., "finding_id" | "fact_id" | "photo_id": ...}`；找不到依据的不记为通过。
- **E6 “一起看”的检查。** 用现有的小轮 AI 检查（`ai_qc.quote_and_inputs(..., kind="paired")`、`ai_qc.admit_call(..., kind="paired", round_id=...)`），只做小轮要求、单图检查没做的那几项（`workflow.PAIRED_CHECKS`）。按钮上的最高费用就是它的报价。
- **E7 检查之后谁接着做。** 不让 Worker 接着写。`complete_marketing_test_start`（T4）是幂等命令：页面在检查全部有结果后自动调用；页面关掉了，下次打开测试页时自动调用。检查有不通过的，页面先展开问题，用户处理后再调用。
- **E8 报价 hash。** `test_quote_hash = digest([QUOTE_VERSION, ideas_revision, sorted((asset_version_id, intent_hash) for kept), answers, overrides, paired_maximum, protocol])`。开始测试时重算，不同就 409 `test_quote_changed`。
- **E9 日期。** 开始日期默认是广告账户时区的明天；`end_date = start_date + duration_days - 1`；预计出结论的日期 `= end_date + attribution_lag_days + 1`（7 天测试从开始那天算是第 16 天）。改开始日期、提前结束都追加记录，不改冻结的快照；评估时用“有效开始、有效结束”。
- **E10 新接口一览。** 第 2 步全部做完后接口数从 266 变成 281（T1 之后是 267）。各块可以改名，改了更新本表和总数。

| 块 | operation_id | 路径 |
| --- | --- | --- |
| T1 | `get_marketing_test` | `GET /marketing/tests/{test_id}`（测试项目、来源、方案摘要、各轮、当前轮） |
| T2 | `get_marketing_test_quote` | `GET /marketing/ideas/{project_id}/test-quote`（查询参数：三个回答、落地页、改过的默认项） |
| T3 | `start_marketing_test` | `POST /marketing/ideas/{project_id}/tests`（幂等，If-Match 广告想法的修订号） |
| T4 | `complete_marketing_test_start` | `POST /marketing/tests/{test_id}/package`（幂等；带用户接受的发现） |
| T4 | `cancel_marketing_test` | `POST /marketing/tests/{test_id}/cancel`（广告包生成之前；这一轮按原因结束） |
| T5 | `preview_marketing_test_report` | `POST /marketing/tests/{test_id}/rounds/{round_id}/report/preview` |
| T5 | `confirm_marketing_test_report` | `POST /marketing/tests/{test_id}/rounds/{round_id}/report/confirm` |
| T6 | `suggest_marketing_test_bindings` | `POST /marketing/tests/{test_id}/rounds/{round_id}/bindings/suggest`（只读，不落库） |
| T6 | `confirm_marketing_test_bindings` | `POST /marketing/tests/{test_id}/rounds/{round_id}/bindings`（幂等，一次写全部） |
| T7 | `change_marketing_test_start` | `POST /marketing/tests/{test_id}/rounds/{round_id}/start-date` |
| T7 | `end_marketing_test_round_early` | `POST /marketing/tests/{test_id}/rounds/{round_id}/end` |
| T7 | `get_marketing_test_result` | `GET /marketing/tests/{test_id}/rounds/{round_id}/result`（最小结果，带还差多少单） |
| T8 | `get_marketing_test_progress` | `GET /marketing/tests/{test_id}/rounds/{round_id}/progress`（目前的数字、还差多少单、提示） |
| T8 | `record_marketing_test_deviation` | `POST /marketing/tests/{test_id}/rounds/{round_id}/deviations` |
| T9 | `list_marketing_tests` | `GET /marketing/tests?product_id=`（这个产品的测试项目和改版前的测试） |

“看结果”这一下调用现有的 `evaluate_marketing_round`（E2 之后它接受测试项目的轮次），再读 `get_marketing_test_result`。

---

## T0：合同、ADR、设计和计划入库

**Files:**
- Create: `docs/adr/0019-take-to-test-opens-a-test-project.md`
- Create: `docs/superpowers/specs/2026-11-21-ads-first-step-2-take-to-test-design.md`（已写好）
- Create: `docs/superpowers/plans/2026-11-21-ads-first-step-2.md`（本文）
- Modify: `docs/adr/0018-ads-before-test-setup.md`（“落地”一节加一句指向 ADR 0019）
- Modify: `docs/superpowers/specs/2026-11-16-ads-first-opening-design.md`（§5.4“第 15 天”改为第 16 天并说明按成熟规则算）
- Modify: `docs/product/ai-marketing-testing-spec.md`（§6 加第 2 步的注；§8 加“拿去测试”）
- Modify: `docs/product/ads-first-step-1-acceptance.md`（“和旧流程的衔接”一节写明 #46–#48 归第 3 步，维护者 2026-11-21 决定）
- Modify: `CONTEXT.md`（新增“测试项目”“一起看的检查”“完整报表”“有效开始、有效结束”）
- Modify: `CLAUDE.md`（合同路由：新开头一行补上第 2 步的设计、计划和依赖图）

### Task 0.1：ADR 0019

- [ ] **Step 1: 写 `docs/adr/0019-take-to-test-opens-a-test-project.md`**

  标题“拿去测试新开一个测试项目”。状态 Accepted，日期 2026-11-21。写明：背景（设计 §1）；决定（E1–E7 的要点：每次拿去测试一个 `ads_test` 项目；方案按模板生成并记为已确认；留下的广告按复用记录挂进小轮；“一起看”的检查付费、写在按钮上；只写这一轮的审核记录；新数据规则只用在测试项目的轮次）；考虑过的做法（设计 §4 的表）；后果（改版前测试不变；第 3 步从小轮拿证据）。

- [ ] **Step 2: 同步上面列出的文档**

  每处一到三句，指向 ADR 0019 或设计文档，不重复设计正文。

- [ ] **Step 3: 跑文档相关的检查**

  Run: `make verify PYTHON=… ALEMBIC=…`
  Expected: 退出码 0（客户名单扫描、文档链接检查都过）。

- [ ] **Step 4: 提交**

  ```bash
  git add docs CONTEXT.md CLAUDE.md
  git commit -m "第 2 步：合同、ADR 0019、设计和计划入库"
  ```

## T1：后端：测试项目和读接口

**Files:**
- Create: `migrations/versions/zzt247a1b2c3_marketing_test_projects.py`
- Create: `app/marketing/ad_tests.py`
- Create: `app/marketing/ad_tests_api.py`
- Create: `app/marketing/ad_tests_contracts.py`
- Modify: `app/marketing/models.py`（`MarketingProjectRecord.source_project_id`）
- Modify: `app/marketing/service.py`（`get_round_project`）
- Modify: `app/marketing/workflow.py`（读轮次时查项目的两处改用 `get_round_project`；`api.py` 里按小轮编号的接口不查项目，不用改，见 E2）
- Modify: `app/main.py` 或路由注册处（挂 `ad_tests_api.router`）
- Test: `tests/test_marketing_tests.py`、`tests/test_postgres_rls.py`

**Interfaces:**
- Produces:
  - `ad_tests.FLOW: str = "ads_test"`
  - `ad_tests.get_test(session, ctx, test_id: str) -> MarketingProjectRecord`（不存在或别的工作区 404 `marketing_test_not_found`）
  - `ad_tests.in_progress(session, ctx, product_id: str) -> MarketingProjectRecord | None`
  - `ad_tests.detail(session, ctx, row) -> dict`（`MarketingTestDetail`）
  - `service.get_round_project(session, ctx, project_id: str) -> MarketingProjectRecord`
  - `ad_tests_contracts.MarketingTestDetail`：`id`、`revision`、`product_id`、`source_project_id`、`comparison`（`"claims" | "expressions" | "single"`）、`plan_summary: MarketingTestPlanSummary`（`target_cost` 目标成本即方案的 CPA 门槛、`round_budget` 本轮预算、`duration_days`、`currency`、`landing_page`）、`rounds: list[MarketingTestRoundSummary]`（`id`、`ordinal`、`state`、`start_date`、`end_date`、`effective_start_date`、`effective_end_date`；T7 之前有效日期就是冻结的日期）、`current_round_id`、`created_at`
  - `ad_tests.comparison(plan: dict) -> str`：`business_retest` 是 `single`；各组的组合跨两个以上主张是 `claims`，否则 `expressions`。T2 生成的方案要和 `comparison_of` 的结果一致。

### Task 1.1：表结构

- [ ] **Step 1: 写迁移测试**

  在 `tests/test_marketing_tests.py`：
  ```python
  def test_a_test_project_names_the_ideas_it_came_from():
      # 建一个 ads_first 项目和一个 ads_test 项目，后者 source_project_id 指向前者
      # 断言：读回 source_project_id 等于广告想法项目的 id；另一个工作区的编号写不进去（外键）
  ```
- [ ] **Step 2: 跑测试，确认失败**（`source_project_id` 不存在）
- [ ] **Step 3: 迁移和模型**：加列 `source_project_id String(64) NULL`，组合外键 `fk_marketing_project_source`（`ondelete="RESTRICT"`），索引 `ix_marketing_projects_source`（`workspace_id, source_project_id`）。表已有强制 RLS 和三种角色的授权，不加权限。降级：有任何 `flow = "ads_test"` 的项目就拒绝，否则删列。
- [ ] **Step 4: 跑测试，确认通过；跑 `alembic check` 无漂移**
- [ ] **Step 5: 提交**

### Task 1.2：按小轮查项目，旧接口看不到测试项目

- [ ] **Step 1: 写失败的测试**
  ```python
  def test_round_paths_accept_a_test_project_and_plan_paths_do_not():
      # 直接造一个 ads_test 项目（plan_confirmed、合法的 plan_json）和它的一轮
      # GET /marketing/rounds/{round_id} → 200
      # GET /marketing/projects/{test_id}/workflow、PATCH /marketing/projects/{test_id} → 404
      # GET /marketing/projects → 不含它；GET /marketing/ideas → 不含它
  def test_another_workspace_cannot_read_a_test_or_its_rounds():
      # 404，且不泄露存在
  ```
- [ ] **Step 2: 跑测试，确认失败**
- [ ] **Step 3: 实现 `service.get_round_project`，把 `api.py` 里按轮次编号的接口改用它**（列表见 E2；改方案、重新起草、改理解、按方案出图不改）
- [ ] **Step 4: 跑这两条和 `tests/test_marketing_workflow.py`、`tests/test_marketing_planning.py`，全部通过**
- [ ] **Step 5: 提交**

### Task 1.3：`get_marketing_test` 和“进行中”

- [ ] **Step 1: 写失败的测试**
  ```python
  def test_a_test_reads_its_source_plan_summary_and_rounds():
      # 断言 MarketingTestDetail 的字段；current_round_id 是没结束的那一轮
  def test_a_product_has_a_test_in_progress_until_its_round_closes():
      # in_progress(product_id) 有一轮 reviewing → 返回该项目；close 之后 → None
  ```
- [ ] **Step 2: 跑测试，确认失败**
- [ ] **Step 3: 实现 `ad_tests.get_test`、`ad_tests.in_progress`、`ad_tests.detail` 和 `GET /marketing/tests/{test_id}`**（接口六步；Owner、Member 都能读）
- [ ] **Step 4: 跑测试和 `make verify`、`make web-verify`，全部通过；接口数 266 → 267**
- [ ] **Step 5: 提交**

## T2–T13 分块说明

### T2 后端：按模板生成方案，“够不够”报价

- **目的：** 设计 §5.2、§5.6。
- **改哪里：** `app/marketing/ad_test_plans.py`（新）：`comparison_of(kept_ads) -> Literal["claims", "expressions", "single"]`、`build_plan(ideas_row, kept_ads, answers, overrides, language) -> TestPlan`、模板（中英两套）；`app/marketing/ad_tests.py`：`quote(session, ctx, ideas_row, answers, overrides) -> dict` 和 `GET /marketing/ideas/{project_id}/test-quote`。
- **规则：** 留下的广告必须全部可导出，否则报价写 `unavailable_reason = "ads_not_exportable"` 并列出哪几张。方案过 `TestPlan` 校验和 `service.validate_plan(confirming=True)`；过不了时报价写原因，不放宽校验。三档和建议按设计 §5.6；`min_purchases`、`attribution_lag_days` 取 `EvaluationProtocol()` 默认值。没有产品链接且没给落地页时 `unavailable_reason = "landing_page_required"`。报价带“一起看”的检查的最高费用（E6）和 `quote_hash`（E8）。
- **测试要点：** 跨两个主张是 `claims`、同一主张两种讲法是 `expressions`、一张是 `single`；三种都生成能通过校验的方案；20 单、10 单的边界（够下结论 / 能看出方向 / 只能看个大概）；7 天测试的预计日期是开始后第 16 天；改了一张留下的广告报价 hash 就变；另一个工作区 404。
- **开工前先确认：** 只有一张时用哪个 `stage`；`screen_angles` 每个主张只有一种讲法时 `limited_expression_coverage` 必须为真。
- **已确认（维护者，2026-11-21，#248）：** 只有一张（或同一组合的几个版本）用 `confirm_combination`；比主张时只要有一个主张只留了一种讲法，`limited_expression_coverage` 为真，并在“局限”里逐个写明“这个主张只试了一种讲法，没达标也不能说明主张不行”；每个主张都留了两种讲法时为假。
- **做成的样子（#248）：** 报价的查询参数是 `MarketingTestAnswers`（三个回答、落地页、改过的默认项），T3 的开始命令带同一份回答和报价 hash。报不了时 `unavailable_reason` 依次是 `ideas_not_ready`、`no_kept_ads`、`ads_not_exportable`（`not_exportable` 列出哪几张）、`ads_execution_mixed`（示例广告和真实广告混在一起）、`start_date_past`、`landing_page_required`、`plan_invalid`（`plan_error` 写校验码）、`checks_unavailable`。“一起看”的检查按每张留下的广告一次计费。

### T3 后端：开始测试

- **目的：** 设计 §5.1、§5.3 的前半、§5.4、§5.5 的前提。
- **改哪里：** `app/marketing/ad_tests.py`：`start(session, ctx, ideas_row, revision, body) -> MarketingProjectRecord`；`POST /marketing/ideas/{project_id}/tests`。同一事务：建测试项目、`service.checkpoint(..., "confirm_plan")`、`workflow.freeze`（E9 的日期）、按 E4 写复用记录、按 E6 受理“一起看”的检查。`app/marketing/reuse.py::creatives`、`app/marketing/workflow.py::ready` 按 E4 认新来源。
- **规则：** If-Match 广告想法修订号；`quote_hash` 不同 409 `test_quote_changed`；有进行中的测试 409 `test_in_progress`；共享审核链不通过 409 `ad_review_rejected`；不可导出 409 `ads_not_exportable`；没有 Key 时 409 `checks_unavailable`。每一件写自己的审计事件。
- **测试要点：** 一次开始得到一个测试项目、一轮、每张广告一条复用记录、一组“一起看”的检查和它的预留；同一幂等键重放得到同一个测试；两个请求并发只有一个成功（Review Focus 2）；开始前取消一张留下的广告 → 409（Review Focus 1）；广告想法项目的状态和广告状态不变；改版前测试的冻结和复用测试全部通过。
- **做成的样子（#249）：** 请求体是 `StartMarketingTest`：`answers`（就是报价用的 `MarketingTestAnswers`）、`quote_hash` 和页面上看到的留下的广告 `asset_version_ids`；回应是新测试的 `MarketingTestDetail`（201，ETag 是测试的修订号 1）。开始时和报价跑同一段计算（`ad_tests._quoted`），所以 hash 对上了，开始的就是报价里的那份方案和那一轮。拒绝的顺序：修订号不对 `stale_marketing_revision`；有进行中的测试 `test_in_progress`（`params.test_id`）；hash 或留下的广告不对 `test_quote_changed`；报价报不了时用报价自己的原因（`ads_not_exportable` 列出每张，`checks_unavailable` 带 `params.reason`，其余是 `no_kept_ads`、`ads_execution_mixed`、`start_date_past`、`landing_page_required`、`plan_invalid`、`ideas_not_ready`）；共享审核链上当前是 `rejected` 或 `needs_changes` 的 `ad_review_rejected`。复用记录的 `source_json` 除 E4 列的五项外，还带 `asset_id`、`product_id`、`execution_mode`、`content_sha256`、`plan_hash`、`combination`，`reuse.creatives` 照原来的条件认它。留下的广告是这次测试自己的执行，不是“拿来对照新图的旧图”：只有一张时（`business_retest`）“一起看”的检查没有要等的对照，只看这张图是不是它该有的主张和讲法。照片是否最新（E4）在 `workflow.ready` 和 `execution_facts` 里都按 `ideas_export.photos_current`；这一轮有了广告包之后，按生成广告包那一刻的判断。

### T4 后端：检查之后

- **目的：** 设计 §5.3 的后半、§5.5。
- **改哪里：** `app/marketing/ad_tests.py`：`complete_start(session, ctx, test_row, revision, body) -> dict`（为每张广告写这一轮的 `MarketingQCRecord`，带 E5 的 `evidence`，再调 `workflow.create_handoff`）、`cancel(session, ctx, test_row, revision, body)`（广告包生成之前，`workflow.close(..., reason="user_stopped")`）；单图检查各项到小轮检查项的对应表。
- **规则：** 检查没全部有结果 409 `checks_running`；有不通过且没被接受的 409 `check_findings_open`（列出）；结果不确定的不重发，按现有核对；不写 `QualityReviewRecord`；交接包生成后再调用返回同一个结果。
- **测试要点：** 全部通过 → 一张一条审核记录、每项都有 `evidence`、广告包生成；有一项找不到依据 → 拒绝、不生成；接受一条发现后可以生成；取消后产品没有进行中的测试，可以重新开始；页面关掉再打开重放同一命令不出第二个包（Review Focus 4）；改版前测试的审核结果不变（同一张图在改版前的测试里 `ready` 不变）。
- **做成的样子（#250）：** 要加一个迁移（`zzw250a1b2c3`）：`marketing_qc_reviews.quality_review_id` 改成可空，测试的审核记录不指向共享审核链。两个接口的 If-Match 是测试当前轮次的修订号。`complete_marketing_test_start` 的请求体是 `accepted_findings`（每张广告：看过的那次“一起看”的检查 `check_id`、接受的发现、理由 `note`），回应 `MarketingTestPackage`（广告包和每张广告的审核记录、依据）；有了广告包后无论再发什么都返回同一个结果。依据除 E5 列的三种键外，没用产品照片、用户确认过外观的广告用 `asset_version_id`。检查失败或过期、或者找不到依据的是 409 `check_evidence_missing`；结果不确定的算在 `checks_running` 里。留下的广告的 `ready` 不要求共享审核链上有通过的审核，只要那里没有不通过的；旧的小轮复核接口遇到留下的广告返回 409 `test_review_by_checks`。`get_marketing_round` 不加字段：测试的审核记录显示为 `ai_assisted_human_review`、去掉依据，广告包条目里留下的广告的来源显示为 `existing_creative`。`cancel_marketing_test` 回应测试详情，广告包生成之后返回 409 `test_package_exists`。细节见 [`2026-11-24-ads-first-step-2-t4-after-checks.md`](2026-11-24-ads-first-step-2-t4-after-checks.md)。

### T5 后端：测试轮次的报表

- **目的：** 设计 §5.7 的前两条。
- **改哪里：** `app/marketing/ad_test_reports.py`（新）：在 `app/marketing/metrics.py` 的解析上包一层；`preview`、`confirm` 和 T5 的两个接口。当前计入的数据：每个（哪天、哪条 Meta 广告）取最新确认的那一份。
- **规则：** 只接受 `customer_file`；时区、币种、归因窗和冻结的不一致时说清楚（Review Focus 3）；不属于这一轮的广告、有效日期之外的日子，存下来、不算、在返回里列出（`excluded`）；有问题的行照现有的问题码；改版前轮次的 `already_imported_ad_day`、`replacement_scope_mismatch` 不动。
- **测试要点：** 先传 1–3 日、再传 1–5 日：确认后计入 1–5 日、1–3 日以后一份为准、两份都在记录里（设计验收 5）；报表里多一条别的广告 → 列为不算；EUR 报表对 USD 测试 → 明确拒绝；另一个工作区 404。

### T6 后端：按广告名预填对应、一次确认对应

- **目的：** 设计 §5.7 的第三、四条。
- **改哪里：** `app/marketing/ad_test_reports.py`：`suggest_bindings(...) -> list[dict]`（读“Ad name”列，`MA-` 先于 `MK-`，只在这一轮的广告包条目里找）、`confirm_bindings(...)`（一次写全部，要么全写要么全不写）；`app/learnings/codes.py` 的 `codes_in_text` 复用来认 `MK-`。
- **规则：** 读广告名不落库；对不上、对上多张、已对到别的轮次的列出来让用户选或跳过；第一次确认报表之前可以改或删对应（追加新记录，旧的留作历史），之后只能新增。正式对应仍按 Ad ID。
- **测试要点：** 两种短键各一条能预填；改了名、带两个短键的不预填（Review Focus 5）；一次确认写全部、中途出错一条都不写；确认报表后改对应被拒、新增被接受。

### T7 后端：这一轮的安排

- **目的：** 设计 §5.7 的改开始日期、提前结束，§5.9 的最小结果。
- **改哪里：** 新表 `marketing_round_schedule_changes`（`round_id`、`kind` = `start_moved` | `ended_early`、`date`、`reason`、`actor_id`、`created_at`；只追加；三种角色授权按“API 读写、Worker 读、只读角色读”）；`app/marketing/ad_test_schedule.py`（新）：`effective_dates(session, ctx, round_row) -> tuple[date, date]`；评估时测试项目的轮次用有效日期（`workflow.evaluate` 读有效日期，改版前的轮次不变）；`get_marketing_test_result`：每个比较对象的状态和还差多少单。
- **规则：** 改开始日期只在第一次确认报表之前，结束日期跟着顺延；提前结束的那天不晚于今天、不早于有效开始；冻结的快照不改写。
- **测试要点：** 晚三天开始、改了开始日期的一轮能出结论（设计验收 7）；提前结束的一轮在结束日加归因期之后能评估，结果写明提前结束、还差多少单（验收 8）；确认报表之后改开始日期被拒；改版前的轮次评估结果不变。

### T8 后端：目前的数字、还差多少单、补记偏差、改了广告想法的提示

- **目的：** 设计 §5.7 的“目前的数字”“还差多少单”“补记偏差”，§5.8。
- **改哪里：** `app/marketing/ad_test_progress.py`（新）：`progress(...)`（只读，不写评估记录）；新表 `marketing_round_deviations`（只追加，授权同 T7）；评估时把补记的偏差当作对应时记的偏差；提示：比较这一轮复用记录里的 `intent_hash` 和广告想法当前的事实、照片、文案。
- **规则：** 还差多少单 = `max(0, min_purchases - purchases)`；补记偏差前的提示由前端写，后端返回影响到哪些比较对象。
- **还要做：** 测试项目的广告包下载（`download_marketing_handoff`）不再按广告想法当前的状态拒绝（E4），改版前的轮次照旧。
- **测试要点：** 读目前的数字前后评估记录数不变；补记偏差后这个对象“还看不出来”；改一条被引用的事实或否定一张用到的照片，提示出现，评估不变，广告包照样能下载（验收 9）。

### T9 后端：测试记录

- **目的：** 设计 §5.10。
- **改哪里：** `app/marketing/ad_tests.py`：`history(session, ctx, product_id) -> dict` 和 `GET /marketing/tests?product_id=`。测试项目和它们的轮次、每份上传和更正（时间来自批次的 `created_at`，人来自审计事件）；改版前的测试按 `app/marketing/card_lineage.py` 找。
- **测试要点：** 一个产品有一个测试项目和一个改版前的测试时两个都列出；别的产品的不列；另一个工作区 404。

### T10 前端：“拿去测试”画面

- **目的：** 设计 §5.11 第一条。
- **改哪里：** `web/components/tests/TakeToTest.tsx`、`TestQuotePanel.tsx`、`TestStartProgress.tsx`；`web/hooks/tests/useTestQuote.ts`、`useTestStart.ts`；页面 `web/pages/w/[slug]/p/[productId]/take-to-test.tsx`；广告想法页的入口。
- **规则：** 三个问题（没有产品链接时加落地页）；默认项折叠，可改，币种在里面；右侧“够不够”和一句“比什么”全部来自报价接口；按钮“开始测试 · 最多 $X”；之后显示检查进度，有问题只展开问题，处理后自动调用 `complete_marketing_test_start`；广告包下载用现有的交接包下载。
- **测试要点：** 回答三个问题后按钮上是报价的最高费用；钱不够时写“只能看个大概”和需要多少；留下的广告变了 → 提示刷新；检查不通过 → 只展开问题；手机宽度；两套文案。

### T11 前端：“测试进行中”画面

- **目的：** 设计 §5.11 第二条。
- **改哪里：** `web/components/tests/TestInProgress.tsx`、`ReportUpload.tsx`、`BindingConfirm.tsx`、`TestSchedule.tsx`、`TestResult.tsx`；页面 `web/pages/w/[slug]/p/[productId]/tests/[testId].tsx`。复用 `MarketingExecution.tsx` 里能独立出来的部分，不改它在改版前测试里的行为。
- **测试要点：** 传报表 → 预填对应 → 确认一次；第一次传报表时开始日期对不上会问；目前的数字标明不是结论；补记偏差前有提示；到期后“看结果”；改了广告想法时的提示。

### T12 前端：测试记录、产品内“测试”视图、总览

- **目的：** 设计 §5.11 第三条；总览里“测试中缺报表”“有结果没看”的状态。
- **改哪里：** `web/components/tests/TestHistory.tsx`；`web/components/workspace/ProductViews.tsx` 的测试占位；`web/lib/products/workspace-products.ts`。

### T13 收尾

- **目的：** 设计 §7 的 12 条验收逐条记录；端到端；教程。
- **改哪里：** `web/e2e/ads-first/take-to-test.spec.ts`（沿用第 1 步端到端的做法：测试里把报价和开始请求改成 Controlled Mock，报表用测试里生成的 Acme Charge CSV）；`docs/user-guide.md`、`docs/tutorials/ads-first-tutorial.html`；`docs/product/ads-first-step-2-acceptance.md`。

## 设计文档和分块的对应

| 设计 | 块 |
| --- | --- |
| §5.1 测试项目 | T1、T3 |
| §5.2 方案生成 | T2 |
| §5.3 一个按钮 | T3、T4、T10 |
| §5.4 挂进小轮 | T3 |
| §5.5 检查和审核 | T3、T4 |
| §5.6 够不够、日期 | T2、T10 |
| §5.7 数据回来 | T5、T6、T7、T8、T11 |
| §5.8 改了广告想法 | T8、T11 |
| §5.9 最小结果 | T7、T11 |
| §5.10 测试记录 | T9、T12 |
| §5.11 画面 | T10、T11、T12 |
| §7 验收 | T13 |
