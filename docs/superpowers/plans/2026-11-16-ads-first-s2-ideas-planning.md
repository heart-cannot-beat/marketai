# 新开头 S2：从产品理解起草主张 · 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `ads_first` 项目在事实还没核对时就能起草产品理解和主张：先起草理解（停在 `understanding_draft`），再起草 3 个主张、每个 2 种讲法、每种讲法 1 个组合（停在 `ideas_ready`）。

**Architecture:** 新接口 `start_marketing_ideas_planning`（`POST /marketing/ideas/{project_id}/planning`）只认 `ads_first` 项目，body 的 `kind` 是 `understanding` 或 `ideas`。两种都走现有的策划调用：同一个 `PlanningCallRecord`、同一个任务类型 `marketing.plan`、同一套预留和结算、同一个 Worker 入口 `execute_planning`。`kind=understanding` 复用 `service.start_planning`；`kind=ideas` 由 `ideas.start_ideas_planning` 组输入，提示词末尾是 `planner_ideas_schema()`，Worker 用 `Ideas` 校验并在写入前做 `ideas.validate_ideas` 的确定性检查。

**Tech Stack:** FastAPI、SQLAlchemy、Pydantic、unittest；OpenAPI 生成脚本和 web 生成的客户端。

**Spec:** 设计文档 [`2026-11-16-ads-first-opening-design.md`](../specs/2026-11-16-ads-first-opening-design.md) §5.2、§6、§7；第 1 步计划 [`2026-11-16-ads-first-step-1.md`](2026-11-16-ads-first-step-1.md) 的 S2 一节和 D1–D9；ADR 0018；Issue heart-cannot-beat/marketai#190。

## 开工前先确认（已核实，2026-11-16）

**1. `execute_planning` 怎么取项目行。** Worker 写回时原来调 `service.get_project(session, ctx, project_id)`，#206 之后它只找 `plan_first` 项目。调用记录不存 `flow`，而且 `input_json` 原样发给模型，不适合加字段。项目的 `flow` 建好以后不会变，所以新增 `service.call_project`：按调用记录里的 `project_id` 取项目，不过滤 `flow`。受理时已经按 `flow` 查过一次，写回时只是回到同一个项目。

**2. `PlanningCallSummary.kind` 只有 `understanding | plan`。** `IdeasProjectDetail.latest_call` 用的是它，`ideas` 调用会让读接口校验失败。旧方案流程的返回类型不改，新增 `IdeasCallSummary`（`kind` 为 `understanding | ideas`），给 `IdeasProjectDetail.latest_call` 和新接口的返回用。

**3. 竞品扫描读未核对的理解。** 维护者决定（#190 评论，2026-11-16）：可以读。`app/competitors/relevance.py::product_context` 不按 `flow` 过滤，加一条测试固定，竞品扫描规格 §7.1 补一句。

## 规则

- `kind=understanding`：沿用现有校验（引文必须在 `source_text` 里；认证、参数、价格要有原文依据；AI 不能把事实标为已核对）。`ads_first` 项目成功后停在 `understanding_draft`，同时清空 `ideas_json`（事实编号可能变了）。`plan_first` 写入的字段不变。
- `kind=ideas`：受理时项目状态必须是 `understanding_draft` 或 `ideas_ready`，否则 `understanding_required`。不要求事实已核对。
- Worker 写入前的检查，任何一条不过都记为 `planner_output_invalid`、项目不变：
  - `Ideas` 文档校验（含三要素：`how_to_tell`、`visual_mechanism`、`evidence_basis`）；
  - 广告语言、策略语言与受理时一致（`planning_language_mismatch`）；
  - 引用的事实、推测都在当前理解里（`plan_evidence_invalid`）；
  - 同一主张下两个讲法的画面机制相同（`expressions_not_distinct`）；
  - 标题、正文用广告语言；每个组合都有英文出图说明，风格说明是英文（`copy_language_mismatch`、`image_brief_language_mismatch`）；
  - 首批规模：最多 3 个主张、每个最多 2 种讲法、每种讲法正好 1 个组合（`ideas_first_batch_too_large`）。至少 2 个主张由 `Ideas` 保证。
- 市场以项目为准（开始页选的“广告投到”），品牌底线引用由应用填（产品当前的品牌底线），引导问题首批不问（设计文档 §6），所以三者都不进模型的 schema。
- “带上被拒草稿重试”：上一次同 kind 的调用 `planner_output_invalid` 且理解没变时，下一次起草带上错误和被拒草稿。主张用自己的策略名 `marketing-explicit-ideas-repair-v1` 和自己的修正提示，方案的修正提示一个字不变。
- 本块不读竞品快照，`competitor_snapshot_id` 保持为空。

## Task 1：测试先行

**Files:** `tests/test_marketing_ideas_planning.py`（新）

- [ ] 事实全部未核对时，`understanding` 再 `ideas` 两步都成功，项目到 `ideas_ready`，`ideas_json` 是 3×2×1；修订和审计里有这两步。
- [ ] Worker 写回 `ads_first` 项目（S1 的发现 1）：付费 `understanding` 调用的结果写回，项目到 `understanding_draft`。
- [ ] 同一主张下两个讲法画面机制相同 → `planner_output_invalid`，项目不变；再起草带上被拒草稿和错误。
- [ ] 讲法缺三要素之一 → `planner_output_invalid`，项目不变。
- [ ] 首批超过 3×2×1、引用不存在的事实、语言不一致 → `planner_output_invalid`。
- [ ] 状态是 `input` 时起草主张 → 422 `understanding_required`，不建调用、不预留。
- [ ] `plan_first` 项目调新接口 → 404；`ads_first` 项目调旧的 `/projects/{id}/planning` → 404。
- [ ] 另一个工作区调新接口 → 404，不建调用；另一个工作区的 Worker 上下文执行不了这个调用。
- [ ] 重新起草理解会清空主张，回到 `understanding_draft`。
- [ ] `product_context` 读到 `ads_first` 项目未核对的理解。
- [ ] `plan_first` 的理解和方案起草：现有测试全部通过；理解写入的字段和修订内容不变。

## Task 2：后端实现

**Files:** `app/marketing/planner.py`、`app/marketing/ideas.py`、`app/marketing/ideas_api.py`、`app/marketing/ideas_contracts.py`、`app/marketing/service.py`、`app/competitors/relevance.py`（只改说明）。

## Task 3：合同和前端客户端

按第 1 步计划 Global Constraints 的新增接口六步：`python scripts/generate_openapi.py`；策略条目照 `start_marketing_planning`（`maxRequestBytes` 8192、`forwardConditionalHeaders: true`）；`bash scripts/generate_all.sh`；`DomainGateway.startMarketingIdeasPlanning`；接口数 246 → 247；`marketing-ideas-contracts.spec.ts` 加 `IdeasCallSummary` 和 `ideas_ready` 详情的样例。新错误码 `ideas_first_batch_too_large` 的中英文提示。还没有页面用通用 hook，第 5 步不做。

## Task 4：文档和检查

- `docs/product/competitor-scan-spec.md` §7.1 补一句。
- `make verify`、`make web-verify`。
