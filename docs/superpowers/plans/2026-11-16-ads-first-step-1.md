# 先看广告，再定测试 · 第 1 步实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新用户贴一个产品链接和自己的 API Key，点一次“开始”，几分钟内看到自己产品的 3 个主张、6 张广告草稿；核对事实和照片之后可以下载。

**Architecture:** 广告想法用现有的 `MarketingProjectRecord` 承载，新增 `flow = "ads_first"` 和 `ideas_json`，旧项目（`plan_first`）的状态机和接口不动。新流程走一组新接口（`/api/v1/marketing/ideas/*`、`/api/v1/marketing/first-batch/*`），旧接口看不到新流程的项目。一个持久的“首批”记录把读产品、起草理解、起草主张、出图、检查串起来，每一步仍是现有的任务、预留和结算。

**Tech Stack:** FastAPI、SQLAlchemy、Alembic、PostgreSQL RLS（本地 SQLite）、后台 Worker（`app/worker_runtime.py`）；Next.js pages router、SWR、next-i18next、Jest、Playwright。

**Spec:** [`docs/superpowers/specs/2026-11-16-ads-first-opening-design.md`](../specs/2026-11-16-ads-first-opening-design.md)

**这份计划怎么用：** 第 1 步拆成 17 块（S0–S16），一块对应一个 GitHub Issue、一个分支、一个 PR。本文给出全部分块、块与块之间的接口、里程碑，以及 S0 和 S1 的逐步任务。S2 起每一块开工时，按本文的分块说明再写一份自己的逐步计划，放在 `docs/superpowers/plans/`。

## Global Constraints

- 一个 Issue 一个分支 `claude/issue-<编号>-<slug>`，从当前 `main` 切出，一个 PR。合并前跑 `make verify`；动了 `web/` 还要跑 `make web-verify`。
- 仓库公开：代码、测试、文档、提交说明只用虚构品牌 Acme Charge（竞品 Brightvolt、Northpeak、Kestrel Power），域名一律 `*.example`。不写真实客户、真实账户或生产截图。
- 合同先于代码。S0 合并之前，不合并任何改变“方案确认后才能出图”这条规则的代码。
- 旧流程不变：`flow = "plan_first"` 的项目，状态机、接口、返回字段一个都不改。旧接口不返回 `ads_first` 项目（旧的 Web 对返回值做严格校验）。
- 每个按工作区存取的数据和命令，都要有“另一个工作区读不到、改不了”的负向测试。
- 每张新表：迁移里启用并强制 RLS、`tenant_isolation` 策略、给 `marketai_domain_api` / worker / readonly 角色授权；同步改 `deploy/postgres/domain-tenant-roles.sql`；在 `tests/test_postgres_rls.py` 的清单里登记。
- 迁移文件名 `<revision>_<描述>.py`，revision 取 `zz` + 下一个字母 + Issue 编号 + `a1b2c3`，`down_revision` 接当前 head（写本文时是 `zzl148a1b2c3`）。
- 每次付费调用：持久任务、幂等键、预留、结算，结果不确定时走现有的核对机制。不静默改用 Mock。主流程界面不出现 Mock。
- 新增一个后端接口的固定六步：
  1. FastAPI 路由带 `operation_id`，幂等命令加 `openapi_extra={"x-marketai-idempotent": True}`；跑 `python scripts/generate_openapi.py`。
  2. 在 `contracts/openapi/marketai-domain-v1-policy.json` 加条目（`bffPattern`、`maxRequestBytes`、`maxResponseBytes`、`forwardConditionalHeaders`）。
  3. 跑 `bash scripts/generate_all.sh`，提交生成的文件。
  4. 在 `web/lib/domain-client/gateway.ts` 的 `DomainGateway` 接口和 `DefaultDomainGateway` 类里加方法。
  5. 用到通用 hook 时，把接口加进 `ReadOperationId` / `CommandOperationId` 和对应的 switch。
  6. 改 `web/__tests__/domain-client/gateway-operations.spec.ts` 里的数量（现在是 242）。
- 新任务类型要登记到 `app/platform/job_policy.py` 的 `JOB_POLICIES`，并加进 `WorkerRuntime.run_once` 的分发；`tests/test_job_policy.py` 会检查两边一致。
- 界面文案同时写进 `web/locales/en` 和 `web/locales/zh-CN`，键必须一一对应。主流程不出现内部编号，也不出现设计文档 §9 左列的词。
- 首批规模：3 个主张，每个 2 种讲法，每种 1 张，共 6 张；主张不足 2 个时不出图。
- 只有 Owner 能添加 Key。Meta 写入保持关闭。

## Review Focus

设计文档没有逐条写、但用户最可能撞上的五种情况。每条的测试写在所属分块里。

1. **开始按钮连点两次，或同一个链接重复提交。** 只应有一个首批、一份预留。测试在 S4。
2. **首批跑到一半 Worker 重启。** 应从断点续跑，不重复出图，不重复计费。测试在 S4。
3. **核对时改了一条事实。** 引用它的广告应变成“需要重出”；只改没被引用的事实，其他广告不受影响。测试在 S10。
4. **旧页面或旧版 Web 读到新流程的项目。** 旧接口不应返回它们，直接访问应是 404。测试在 S1（本文 Task 1.3）。
5. **工作区只有 Gemini 的 Key。** 点开始之前就应得到明确提示，而不是读到一半失败。测试在 S5。

---

## 分块总表

| 块 | 内容 | 依赖 | 里程碑 |
| --- | --- | --- | --- |
| S0 | 合同和 ADR | 无 | A |
| S1 | 后端：广告想法的容器和读接口 | S0 | A |
| S2 | 后端：从产品理解起草主张 | S1 | A |
| S3 | 后端：按主张出图，不要求方案确认 | S2 | A |
| S4 | 后端：首批流水和一次报价 | S3、S5 | A |
| S5 | 后端：一个 OpenAI Key 跑完全程，保存 Key 时校验 | 无 | A |
| S6 | 前端：新左边栏、产品内视图框架、总览、手机菜单 | S1 | A |
| S7 | 前端：开始页、读产品页 | S4、S5、S6 | A |
| S8 | 前端：广告想法页（看图、留下） | S3、S6 | A |
| S9 | 后端：草稿的自动检查 | S3 | B |
| S10 | 后端：核对、草稿状态、下载 | S3、S9 | B |
| S11 | 后端：未核对的照片出草稿 | S3、S10 | B |
| S12 | 后端：草稿的修改和追加 | S3 | B |
| S13 | 前端：下载前核对、修改这张广告 | S8、S10、S12 | B |
| S14 | 前端：产品资料 | S6、S10 | B |
| S15 | 前端：竞品独立页面，素材库和设置接入 | S6 | B |
| S16 | 收尾：出错状态、教程、端到端验收、打开开关 | 全部 | B |

**Issue：** 依赖图是 heart-cannot-beat/marketai#187。S0 到 S16 依次是 #188、#189、#190、#191、#193、#192、#194、#195、#196、#197、#198、#199、#200、#201、#202、#203、#204（S4 是 #193，S5 是 #192，因为 S4 依赖 S5，先建了 S5）。

**里程碑 A：贴链接到看见草稿。** 做完 S0–S8，在开关打开的环境里，贴链接和 Key、点开始，能看到事实、主张、6 张草稿，能留下或取消。这时草稿还不能下载。

**里程碑 B：核对后能下载，第 1 步完成。** 做完 S9–S16，设计文档 §10 第 1 步的 13 条验收全部满足。

**可以并行的：** S5、S6、S15 不依赖后端主链（S1→S2→S3→S4），可以和它同时做。S9、S12 在 S3 之后可以并行。

**上线开关：** 仓库没有通用的功能开关。S6 在 `web/lib/env.ts` 加 `adsFirstEnabled`（读 `NEXT_PUBLIC_MARKETAI_ADS_FIRST === 'true'`）。关着的时候，“新产品”仍去现在的 `/w/{slug}/products/new`，工作区首页仍跳到 `/experiments`。后端新接口不受开关控制。S16 在生产环境打开它。

## 跨块的共同决定

- **D1 容器。** 一个产品的广告想法是一条 `MarketingProjectRecord`，`flow = "ads_first"`，`plan_json` 在第 1 步始终为空。状态只有三个：`input`、`understanding_draft`、`ideas_ready`。
- **D2 主张文档。** `ideas_json` 存 `Ideas`，是 `TestPlan` 的广告内容那一半：`market`、`ad_language`、`strategy_language`、`angles`、`combinations`、`style_notes`、`brand_kit`、`competitor_snapshot_id`、`guiding_questions`。`Angle`、`Expression`、`CreativeCombination` 复用 `app/marketing/contracts.py` 里现有的类。
- **D3 产品锚点。** `ads_first` 项目创建时必须已有 `product_id`。S4 在读完产品后把录入结果存成创意库里的产品草稿（不发布），拿到桥接的 `ProductRecord`，再建项目。发布推迟到 S10 的核对。
- **D4 原始资料。** 项目的 `source_text` 是产品页的文字摘要（`evidence_json["fetch"]["digest_text"]`）加用户贴的文字，这样事实的引文能在原始资料里找到。资料只有图片或截图时，把模型从图里读出的文字放进 `source_text`，事实的出处标为“来自你上传的图片”。
- **D5 出图意图。** 草稿的 `selectors.marketing_intent` 带 `schema_version: 2` 和 `flow: "ads_first"`，并带出图时的产品理解快照和这张广告引用的事实编号。S10 靠它判断草稿状态。
- **D6 广告清单。** 一个项目的广告通过出图配方里的 `marketing_intent.project_id` 找，不依赖小轮。查询写在 `app/marketing/ideas.py::project_ads`，S1 提供，后面各块共用。
- **D7 费用。** 首批总价是各步最高费用之和，确认一次。每一步仍然单独预留、单独结算。预留总额不超过确认的数字，超过就停下来。
- **D8 供应商。** 第 1 步保证只有 OpenAI Key 时全程能跑。只有 Gemini Key 时，在开始之前提示“目前需要 OpenAI 的 Key”。
- **D9 新接口一览。** 全部做完后接口数从 242 变成 256。

| 块 | operation_id | 路径 |
| --- | --- | --- |
| S1 | `create_marketing_ideas` | `POST /marketing/ideas` |
| S1 | `get_marketing_ideas` | `GET /marketing/ideas/{project_id}` |
| S1 | `list_marketing_ideas` | `GET /marketing/ideas` |
| S1 | `set_marketing_ad_kept` | `PUT /marketing/ideas/{project_id}/ads/{asset_version_id}/kept` |
| S2 | `start_marketing_ideas_planning` | `POST /marketing/ideas/{project_id}/planning` |
| S3 | `prepare_marketing_ideas_generation` | `POST /marketing/ideas/{project_id}/generation-drafts` |
| S4 | `get_marketing_first_batch_quote` | `GET /marketing/first-batch/quote` |
| S4 | `start_marketing_first_batch` | `POST /marketing/first-batch` |
| S4 | `get_marketing_first_batch` | `GET /marketing/first-batch/{first_batch_id}` |
| S4 | `resume_marketing_first_batch` | `POST /marketing/first-batch/{first_batch_id}/resume` |
| S9 | `start_marketing_ideas_checks` | `POST /marketing/ideas/{project_id}/checks` |
| S10 | `edit_marketing_ideas_understanding` | `PATCH /marketing/ideas/{project_id}/understanding` |
| S10 | `verify_marketing_ideas` | `POST /marketing/ideas/{project_id}/verification` |
| S10 | `download_marketing_ideas` | `POST /marketing/ideas/{project_id}/download-intent` |

S12 再加两个（按意见重出、直接处理图片），接口名在 S12 自己的计划里定，届时更新这张表和总数。

---

## S0：合同和 ADR

纯文档，没有代码。它先合并，后面的代码才有合同可依。

### Task 0.1：新 ADR

**Files:**
- Create: `docs/adr/0018-ads-before-test-setup.md`

- [ ] **Step 1：写 ADR。** 标题“广告内容先于测试设置”。正文要定下这五点，每点一段：
  1. 新产品走 `ads_first`：读完产品就起草主张并出图，不要求先确认产品理解或测试方案。
  2. 没核对的事实和没核对的产品照片可以用来出草稿。草稿不能导出，不能进入任何小轮，不构成效果证据。
  3. 导出的条件：广告引用的事实已由用户核对且与出图时一致；用到的产品照片已核对；检查没有未处理的问题。
  4. 测试设置（预算、目标、周期、判断规则）在用户要测试时才确认；小轮冻结时的快照仍是完整的 `TestPlan`。
  5. 没有 Key 时不出图，也不提供受控 Mock 体验；Mock 保留为测试手段。
  写明它接替 ADR 0010 的默认入口，不改 ADR 0005、0009、0011、0014。
- [ ] **Step 2：核对。** 通读一遍，确认只出现虚构品牌和 `*.example` 域名。名单只在 CI 的密钥里，PR 上的 `customer-denylist` 检查通过才算完成。

### Task 0.2：同步现有合同

**Files:**
- Modify: `docs/product/ai-marketing-testing-spec.md`（§5.1、§6、§8、§8.2）
- Modify: `docs/product/front-flow-two-config-contract.md`（§3.1、§6）
- Modify: `CONTEXT.md`
- Modify: `CLAUDE.md`（Contract routing 一节）

- [ ] **Step 1：`ai-marketing-testing-spec.md`。** 在 §6 开头加一段说明：新产品走 ADR 0018 的顺序，本节“先确认方案再生成”只适用于改版前建的测试。§5.1 加一句：未核对的照片可以用来出草稿，不能用于导出。§8 和 §8.2 开头各加一句，指向设计文档 §5。原文保留为历史，做法和这份文件里已有的“已冻结，本节已被取代”一致。
- [ ] **Step 2：`front-flow-two-config-contract.md`。** §3.1 加说明：新流程里产品卡不再整张确认，见 ADR 0018。§6“AI 提议不等于确认”一条补一句：AI 的提议可以用来出草稿，草稿不能导出。
- [ ] **Step 3：`CONTEXT.md`。** 新增四个词条：广告想法（Ideas）、留下（Kept）、草稿 / 可导出 / 需要重出、首批（First batch）。每条一句定义，写明和现有“创意组合”“执行”“生成草稿”的关系。
- [ ] **Step 4：`CLAUDE.md`。** Contract routing 加一行：新开头（贴链接、广告想法、首批、核对）读设计文档和 ADR 0018。
- [ ] **Step 5：提交设计文档和本计划。**

```bash
git add docs/adr/0018-ads-before-test-setup.md docs/product/ai-marketing-testing-spec.md docs/product/front-flow-two-config-contract.md CONTEXT.md CLAUDE.md docs/superpowers/specs/2026-11-16-ads-first-opening-design.md docs/superpowers/plans/2026-11-16-ads-first-step-1.md
git commit -m "新开头的合同：广告内容先于测试设置（ADR 0018），设计文档和第 1 步计划入库"
```

---

## S1：后端：广告想法的容器和读接口

做完之后：可以通过接口为一个已有产品建一个 `ads_first` 项目、读它、列出它们、标记一张广告留下或不留；旧接口看不到这些项目。还没有起草和出图。

### Task 1.1：表结构

**Files:**
- Create: `migrations/versions/<revision>_marketing_ads_first.py`
- Modify: `app/marketing/models.py`
- Modify: `deploy/postgres/domain-tenant-roles.sql`
- Modify: `tests/test_postgres_rls.py`
- Test: `tests/test_marketing_ideas.py`

**Interfaces:**
- Produces: `MarketingProjectRecord.flow: str`（`"plan_first"` 或 `"ads_first"`）、`.market: str | None`、`.ad_language: str | None`、`.ideas_json: dict | None`。
- Produces: `MarketingAdKeepRecord`，表 `marketing_ad_keeps`：`id` String(64) 主键、`workspace_id`、`project_id` String(64) 非空、`asset_version_id` String(64) 非空、`kept` Boolean 非空、`actor_id` String(128) 可空、`updated_at` 带时区。唯一约束 `uq_marketing_ad_keep(workspace_id, project_id, asset_version_id)`，复合外键到 `marketing_projects(workspace_id, id)`，`ondelete="RESTRICT"`。

- [ ] **Step 1：写失败的测试。**

```python
class MarketingIdeasSchemaTests(MarketingPlanningFixtures):
    def test_existing_projects_are_plan_first(self):
        old = self.create()
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, old["id"])
            self.assertEqual(row.flow, "plan_first")
            self.assertIsNone(row.ideas_json)
            self.assertIsNone(row.market)
```

- [ ] **Step 2：跑测试确认失败。** `.venv/bin/python -m pytest tests/test_marketing_ideas.py -q`，期望因为没有 `flow` 属性失败。
- [ ] **Step 3：写迁移和模型。** `marketing_projects` 加四列：`flow` String(20) 非空、`server_default="plan_first"`；`market` String(80) 可空；`ad_language` String(35) 可空；`ideas_json` JSON 可空。建 `marketing_ad_keeps`，照 `zn9a0b1c2d3e_marketing_planning.py` 的写法启用并强制 RLS、建 `tenant_isolation` 策略；给 `marketai_domain_api` SELECT、INSERT、UPDATE，给 worker 和 readonly SELECT。迁移里导出 `NEW_WORKSPACE_OWNED = ("marketing_ad_keeps",)`。
- [ ] **Step 4：同步角色文件和 RLS 清单。** `deploy/postgres/domain-tenant-roles.sql` 在营销表那一段加同样的授权。`tests/test_postgres_rls.py` 的 `CURRENT_AGGREGATE_TABLES["marketing_testing"]` 加 `marketing_ad_keeps`，并导入新迁移的 `NEW_WORKSPACE_OWNED`。
- [ ] **Step 5：跑测试和迁移检查。** `.venv/bin/python -m pytest tests/test_marketing_ideas.py -q` 通过；`make migration-check` 通过。
- [ ] **Step 6：提交。** `git commit -m "营销项目加 flow、market、ad_language、ideas_json，新增 marketing_ad_keeps"`

### Task 1.2：`Ideas` 文档和返回模型

**Files:**
- Create: `app/marketing/ideas_contracts.py`
- Test: `tests/test_marketing_ideas.py`

**Interfaces:**
- Consumes: `Document`、`Text`、`Identifier`、`Angle`、`CreativeCombination`、`BrandKitRef`、`GuidingQuestion`、`Understanding`、`PlanningCallSummary`（都在 `app/marketing/contracts.py`）。
- Produces:

```python
class Ideas(Document):
    market: Text
    ad_language: str            # 和 TestPlan.ad_language 同一个 pattern
    strategy_language: Literal["zh-CN", "en-US"] = "en-US"
    angles: list[Angle] = Field(min_length=2, max_length=8)
    combinations: list[CreativeCombination] = Field(min_length=2, max_length=30)
    style_notes: str = Field(default="", max_length=500)
    brand_kit: BrandKitRef | None = None
    competitor_snapshot_id: Identifier | None = None
    guiding_questions: list[GuidingQuestion] = Field(default_factory=list, max_length=3)

class AdSummary(Document):
    asset_version_id: str
    combination_id: str
    angle_id: str
    expression_id: str
    status: Literal["draft", "exportable", "needs_redo"]
    kept: bool
    created_at: str

class PendingAd(Document):
    combination_id: str
    state: Literal["queued", "running", "failed"]

class IdeasProjectDetail(Document):
    id: str
    revision: int
    state: Literal["input", "understanding_draft", "ideas_ready"]
    product_id: str
    market: str
    ad_language: str
    understanding: Understanding | None
    ideas: Ideas | None
    latest_call: PlanningCallSummary | None
    ads: list[AdSummary]
    pending: list[PendingAd]

class IdeasProjectSummary(Document):
    id: str
    revision: int
    state: str
    product_id: str
    product_name: str
    ad_count: int
    kept_count: int
    updated_at: str

class IdeasProjectPage(Document):
    items: list[IdeasProjectSummary]
    next_cursor: str | None = None

class CreateIdeas(Document):
    product_id: str = Field(min_length=1, max_length=64)
    source_text: str = Field(min_length=1, max_length=16000)
    market: str = Field(min_length=1, max_length=80)
    ad_language: str            # 同上 pattern

class SetKept(Document):
    kept: bool
```

`Ideas` 带一个 `model_validator`：每个 `combination.angle_id` 必须是某个主张的 id，`combination.expression_id` 必须是那个主张下某个讲法的 id；每个讲法至少被一个组合引用。第 1 步里 `AdSummary.status` 只会是 `"draft"`，另外两个值留给 S10。

- [ ] **Step 1：写失败的测试。**

```python
class IdeasDocumentTests(unittest.TestCase):
    def test_combination_must_point_at_an_expression_of_its_angle(self):
        doc = ideas_example()                      # 测试文件里的辅助函数：2 个主张，各 2 个讲法，4 个组合
        doc["combinations"][0]["expression_id"] = "missing"
        with self.assertRaises(ValidationError):
            Ideas.model_validate(doc)

    def test_one_angle_is_not_enough(self):
        doc = ideas_example()
        doc["angles"] = doc["angles"][:1]
        doc["combinations"] = [c for c in doc["combinations"] if c["angle_id"] == doc["angles"][0]["id"]]
        with self.assertRaises(ValidationError):
            Ideas.model_validate(doc)
```

- [ ] **Step 2：跑测试确认失败。** 期望 `ImportError`。
- [ ] **Step 3：实现 `app/marketing/ideas_contracts.py`。**
- [ ] **Step 4：跑测试确认通过。**
- [ ] **Step 5：提交。** `git commit -m "广告想法的文档模型 Ideas 和接口返回模型"`

### Task 1.3：服务函数，以及旧接口看不到新项目

**Files:**
- Create: `app/marketing/ideas.py`
- Modify: `app/marketing/service.py`（`get_project`）
- Modify: `app/marketing/api.py`（`list_projects` 的查询）
- Test: `tests/test_marketing_ideas.py`

**Interfaces:**
- Consumes: `service.ensure_active`、`service.checkpoint`、`service.product_card`、`service.MarketingError`、`service.latest_call`、`service.call_dict`、`new_id`、`append_event`。
- Produces（都在 `app/marketing/ideas.py`）：

```python
def create_ideas_project(session, ctx, *, product_id: str, source_text: str, market: str, ad_language: str) -> MarketingProjectRecord
def get_ideas_project(session, ctx, project_id: str) -> MarketingProjectRecord          # 不是 ads_first 就 404 marketing_project_not_found
def project_ads(session, ctx, row) -> list[tuple[AssetVersionRecord, GenerationRecipeSnapshotRecord]]
def set_kept(session, ctx, row, asset_version_id: str, kept: bool) -> None              # 广告不属于这个项目就 404 ad_not_found
def ideas_dict(session, ctx, row) -> dict                                               # IdeasProjectDetail 的内容
def ideas_summary(session, ctx, row) -> dict                                            # IdeasProjectSummary 的内容
```

- Produces（改动）：`service.get_project(session, ctx, project_id, *, flow: str = "plan_first")`，项目的 `flow` 不等于参数时按找不到处理。

`project_ads` 的查询照 `workflow.creative_assets` 的四表连接写，条件是 `canonical_json['selection']['marketing_intent']['project_id'] == row.id`，再在 Python 里过滤 `intent.get("flow") == "ads_first"`。`create_ideas_project` 用 `product_card` 确认产品在这个工作区里，找不到时抛 `MarketingError("product_not_found", "找不到这个产品。", 404)`。`set_kept` 写一条审计事件 `marketing.ad_kept`，`details` 里有 `asset_version_id` 和 `kept`。

- [ ] **Step 1：写失败的测试。** 在 `tests/test_marketing_ideas.py` 里加 `MarketingIdeasFixtures(MarketingPlanningFixtures)`，带一个 `product()` 辅助方法（建一个产品并返回它的 id，做法照 `tests/test_marketing_product_card_entry.py`）。

```python
class IdeasServiceTests(MarketingIdeasFixtures):
    def make(self, ctx=None):
        ctx = ctx or self.context
        with tenant_session(ctx) as session:
            row = ideas.create_ideas_project(session, ctx, product_id=self.product(), source_text="Schedule charging from the Acme Charge app.",
                                             market="United States", ad_language="en-US")
            return row.id

    def test_create_starts_in_input(self):
        project_id = self.make()
        with tenant_session(self.context) as session:
            row = ideas.get_ideas_project(session, self.context, project_id)
            self.assertEqual((row.flow, row.state, row.market, row.ad_language), ("ads_first", "input", "United States", "en-US"))
            self.assertEqual(ideas.ideas_dict(session, self.context, row)["ads"], [])

    def test_old_lookup_does_not_find_an_ads_first_project(self):
        project_id = self.make()
        with tenant_session(self.context) as session:
            with self.assertRaises(service.MarketingError) as caught:
                service.get_project(session, self.context, project_id)
            self.assertEqual(caught.exception.status, 404)

    def test_ideas_lookup_does_not_find_a_plan_first_project(self):
        old = self.create()
        with tenant_session(self.context) as session:
            with self.assertRaises(service.MarketingError):
                ideas.get_ideas_project(session, self.context, old["id"])

    def test_another_workspace_cannot_find_it(self):
        project_id = self.make()
        other = self._make_context()
        with tenant_session(other) as session:
            with self.assertRaises(service.MarketingError) as caught:
                ideas.get_ideas_project(session, other, project_id)
            self.assertEqual(caught.exception.status, 404)

    def test_keeping_an_ad_that_is_not_in_the_project_is_refused(self):
        project_id = self.make()
        with tenant_session(self.context) as session:
            row = ideas.get_ideas_project(session, self.context, project_id)
            with self.assertRaises(service.MarketingError) as caught:
                ideas.set_kept(session, self.context, row, "av_missing", True)
            self.assertEqual(caught.exception.code, "ad_not_found")

    def test_unknown_product_is_refused(self):
        with tenant_session(self.context) as session:
            with self.assertRaises(service.MarketingError) as caught:
                ideas.create_ideas_project(session, self.context, product_id="prod_missing", source_text="x", market="United States", ad_language="en-US")
            self.assertEqual(caught.exception.code, "product_not_found")
```

- [ ] **Step 2：跑测试确认失败。**
- [ ] **Step 3：实现 `app/marketing/ideas.py`，改 `service.get_project`。**
- [ ] **Step 4：改 `list_projects` 的查询。** 在 `app/marketing/api.py` 的 `query = select(cls).where(cls.workspace_id == ctx.workspace_id)` 后面加 `.where(cls.flow == "plan_first")`。
- [ ] **Step 5：跑全部营销测试。** `.venv/bin/python -m pytest tests/ -q -k marketing`，期望全部通过，旧测试没有一条因此变红。
- [ ] **Step 6：提交。** `git commit -m "广告想法的服务函数；旧接口不再看到 ads_first 项目"`

### Task 1.4：四个接口

**Files:**
- Create: `app/marketing/ideas_api.py`
- Modify: `app/main.py`（在 `marketing` 路由旁边挂上新路由）
- Test: `tests/test_marketing_ideas.py`

**Interfaces:**
- Consumes: Task 1.3 的六个函数；`request_tenant_context`、`tenant_session`、`_idempotency_key`、`_resource_command`、`set_etag`、`encode_cursor`、`decode_cursor`。
- Produces: `router = APIRouter(prefix="/marketing/ideas")`，四个 `operation_id` 见 D9。`create_marketing_ideas` 返回 201，命令名 `"marketing.ideas.create"`；`set_marketing_ad_kept` 命令名 `"marketing.ideas.keep"`。四个接口里，新建、读取、留下都返回 `IdeasProjectDetail`，列表返回 `IdeasProjectPage`。两个命令都带 `openapi_extra={"x-marketai-idempotent": True}`。Owner 和 Member 都能调用。

- [ ] **Step 1：写失败的测试。**

```python
class IdeasApiTests(MarketingIdeasFixtures):
    def ideas_create(self, **overrides):
        body = {"product_id": self.product(), "source_text": "Schedule charging from the Acme Charge app.",
                "market": "United States", "ad_language": "en-US", **overrides}
        return self.client.post("/api/v1/marketing/ideas", json=body, headers={"Idempotency-Key": uuid4().hex})

    def test_create_then_read(self):
        created = self.ideas_create()
        self.assertEqual(created.status_code, 201)
        body = created.json()
        self.assertEqual((body["state"], body["ads"], body["pending"], body["ideas"]), ("input", [], [], None))
        read = self.client.get(f"/api/v1/marketing/ideas/{body['id']}")
        self.assertEqual(read.json()["id"], body["id"])
        self.assertIn("ETag", read.headers)

    def test_same_idempotency_key_returns_the_same_project(self):
        key = uuid4().hex
        body = {"product_id": self.product(), "source_text": "x", "market": "United States", "ad_language": "en-US"}
        first = self.client.post("/api/v1/marketing/ideas", json=body, headers={"Idempotency-Key": key}).json()
        second = self.client.post("/api/v1/marketing/ideas", json=body, headers={"Idempotency-Key": key}).json()
        self.assertEqual(first["id"], second["id"])

    def test_old_endpoints_do_not_show_it(self):
        body = self.ideas_create().json()
        listed = self.client.get("/api/v1/marketing/projects").json()["items"]
        self.assertNotIn(body["id"], [item["id"] for item in listed])
        self.assertEqual(self.client.get(f"/api/v1/marketing/projects/{body['id']}").status_code, 404)

    def test_list_shows_only_ads_first_projects(self):
        old = self.create()
        new = self.ideas_create().json()
        ids = [item["id"] for item in self.client.get("/api/v1/marketing/ideas").json()["items"]]
        self.assertIn(new["id"], ids)
        self.assertNotIn(old["id"], ids)

    def test_keep_on_unknown_ad_is_404(self):
        body = self.ideas_create().json()
        response = self.client.put(f"/api/v1/marketing/ideas/{body['id']}/ads/av_missing/kept", json={"kept": True},
                                   headers={"Idempotency-Key": uuid4().hex})
        self.assertEqual((response.status_code, response.json()["code"]), (404, "ad_not_found"))
```

- [ ] **Step 2：跑测试确认失败。** 期望 404，因为路由还不存在。
- [ ] **Step 3：实现 `app/marketing/ideas_api.py`，在 `app/main.py` 挂路由。**
- [ ] **Step 4：跑测试确认通过。**
- [ ] **Step 5：提交。** `git commit -m "广告想法的四个接口：新建、读取、列表、留下"`

### Task 1.5：合同和前端客户端

**Files:**
- Modify: `contracts/openapi/marketai-domain-v1.json`（生成）
- Modify: `contracts/openapi/marketai-domain-v1-policy.json`
- Modify: `web/lib/domain-client/generated/*`（生成）
- Modify: `web/lib/domain-client/gateway.ts`
- Modify: `web/__tests__/domain-client/gateway-operations.spec.ts`
- Test: `web/__tests__/domain-client/marketing-ideas-contracts.spec.ts`

**Interfaces:**
- Produces: `DomainGateway.createMarketingIdeas`、`getMarketingIdeas`、`listMarketingIdeas`、`setMarketingAdKept`，参数形状照 `createMarketingProject`、`getMarketingProject` 的写法。

- [ ] **Step 1：按 Global Constraints 的六步走一遍。** 策略条目照 `create_marketing_project`、`get_marketing_project`、`list_marketing_projects` 三条的取值；`set_marketing_ad_kept` 的 `maxRequestBytes` 取 1024。数量从 242 改成 246。
- [ ] **Step 2：写合同测试。** `marketing-ideas-contracts.spec.ts`，照 `marketing-contracts.spec.ts` 的写法，用一个 `IdeasProjectDetail` 的样例（`state: "input"`、`ads: []`）过生成的校验器，期望通过；把 `state` 改成 `"plan_confirmed"`，期望不通过。
- [ ] **Step 3：跑检查。** `make verify` 和 `make web-verify` 都通过。
- [ ] **Step 4：提交并开 PR。** PR 说明写：迁移名、测试结果、对运行的影响（多四列一张表，旧接口行为不变）、遗留风险（暂无起草和出图）。

---

## S2–S16 分块说明

每块开工时照这里写自己的逐步计划。“开工前先确认”是这次没来得及读透、可能改变做法的地方。

### S2 后端：从产品理解起草主张

- **目的：** `ads_first` 项目不用确认事实就能起草主张。
- **改哪里：** `app/marketing/planner.py`（新 kind `"ideas"`：`system_prompt` 末尾换成 `planner_ideas_schema()`，并要求 3 个主张、每个 2 个讲法、每个讲法 1 个组合；`execute_planning` 加一支，用 `Ideas` 校验，写 `state="ideas_ready"` 和 `ideas_json`）、`app/marketing/ideas.py`（`start_ideas_planning`）、`app/marketing/service.py`（`latest_call` 的 kind 过滤加 `"ideas"`）、`app/marketing/ideas_api.py`。
- **规则：** kind `understanding` 沿用现有校验（引文必须在 `source_text` 里；认证、参数、价格要有原文依据），成功后停在 `understanding_draft`。kind `ideas` 要求状态是 `understanding_draft` 或 `ideas_ready`。讲法重复、缺三要素时本次调用记为 `planner_output_invalid`，不写入项目；现有的“带上被拒草稿重试”机制照用。
- **测试要点：** 事实都还是未核对时能起草成功；同一主张下画面机制相同的草稿被拒；`plan_first` 项目调这个接口是 404；`ads_first` 项目调旧的 `/projects/{id}/planning` 是 404；另一个工作区 404。
- **开工前先确认：** `execute_planning` 里怎么取项目行，`get_project` 加了 `flow` 参数后要不要传。

### S3 后端：按主张出图，不要求方案确认

- **目的：** `ideas_ready` 的项目可以为每个组合准备出图草稿、预览、提交，出来的图挂在项目下。
- **改哪里：** `app/marketing/ideas.py`（`generation_drafts`、`ideas_intent`）、`app/generation/selection.py`（`resolve_selection` 里对营销链接的校验，认 `flow == "ads_first"`：项目存在、是 `ads_first`、组合内容和 `ideas_json` 里的一致）、`app/generation/selected_submission.py`（`check_frozen_selection` 同样处理）、`app/marketing/ideas_api.py`。
- **规则：** 意图带 `schema_version: 2`、`flow`、`project_id`、`plan_revision`、`ideas_hash`、`combination`、`proposition`、`expression`、`understanding`（含每条事实的核对状态）、`cited_fact_ids`。预览和提交仍用现有的 `create_generation_preview`、`submit_generation_run`。
- **测试要点：** 没有任何确认也能准备、预览、提交；出来的图在 `get_marketing_ideas` 的 `ads` 里，状态是 `draft`；出图中的在 `pending` 里；`set_marketing_ad_kept` 对真实广告生效；`plan_first` 项目没确认方案仍然不能出图（回归）。
- **开工前先确认：** 产品只有创意库草稿、没有已发布版本时，`create_draft` 和预览能不能走通。

### S4 后端：首批流水和一次报价

- **目的：** 一次调用把读产品、起草理解、起草主张、出图串起来，确认一次总价。
- **改哪里：** 新表 `marketing_first_batches`（`id`、`workspace_id`、`intake_id`、`project_id` 可空、`product_id` 可空、`market`、`ad_language`、`state`、`attention_json`、`confirmed_max_cost`、`reserved_total`、`quote_hash`、`operation_id`、`initiator_id`、时间戳；唯一约束 `(workspace_id, intake_id)`）；新文件 `app/marketing/first_batch.py`、`app/marketing/first_batch_api.py`；新任务类型 `marketing.first_batch`；在 `run_product_intake`、`execute_planning`、`handle_run_item` 的成功和失败出口调用 `first_batch.notify(session, ctx, ...)`。
- **规则：** `state` 取 `reading`、`understanding`、`ideas`、`generating`、`done`、`needs_attention`、`failed`。`marketing.first_batch` 是一次“推进”：看当前这一步完成没有，完成了就受理下一步并排队，没完成就什么都不做，可以重复执行。每受理一步付费调用前，检查 `reserved_total + 这一步最高费用 <= confirmed_max_cost`，否则进 `needs_attention`。停下来的三种情况：多个型号、读不到页面、主张不足 2 个。读完产品后把录入结果存成创意库产品草稿（不发布）并建项目（D3、D4）。
- **测试要点：** 用假的抓取器、假的模型和 Mock 出图，反复推进直到 `done`，得到 6 张草稿；同一个推进任务跑两遍，不多一次调用、不多一份预留（Review Focus 1、2）；重复提交同一个链接返回同一个首批；超出确认的总价时停下；三种停下的情况各一条；另一个工作区 404。
- **开工前先确认：** 录入的 `create_intake` 能不能在同一个事务里被复用；创意库产品草稿不发布时桥接的 `ProductRecord` 是否已经存在；出图完成的通知挂在哪个函数最稳。S9 做完后，在这里加上“检查”这一步。

### S5 后端：一个 OpenAI Key 跑完全程，保存 Key 时校验

- **目的：** 工作区只有 OpenAI Key 时，读产品、策划、出图都能跑；Key 不对在保存时就知道。
- **改哪里：** `app/marketing/service.py::planner_capability`（部署配置的供应商没有 Key、而 OpenAI 有时，改用 OpenAI；模型和上限从新配置 `MARKETAI_PLANNING_MODELS_JSON` 读，旧的三个环境变量继续有效）；`app/marketing/ai_qc.py::capability` 同样处理（`MARKETAI_MARKETING_QC_MODELS_JSON`）；`app/entitlements/api.py::create_credential` 和 `rotate_credential` 的请求体加 `verify: bool = False`，为真且供应商是 OpenAI 时向供应商发一次不收费的请求验证 Key，被拒返回 422 `provider_key_rejected`。
- **测试要点：** 只有 OpenAI Key、部署配的是 Gemini 时，`get_marketing_capabilities` 报可用且供应商是 OpenAI；只有 Gemini Key 时首批报价返回 `unavailable_reason: "openai_key_required"`（Review Focus 5，报价接口在 S4，这条测试在 S4 合并后补）；`verify` 为真且 Key 被拒时不保存；`verify` 不传时行为和现在完全一样；Member 仍然不能添加 Key。校验请求用 `httpx.MockTransport`。
- **开工前先确认：** OpenAI 用哪个不收费的请求验证 Key，以及“有效但没有出图权限”怎么区分。

### S6 前端：新左边栏、产品内视图框架、总览、手机菜单

- **目的：** 换成设计稿的导航。
- **改哪里：** `web/components/workspace/WorkspaceNavigation.tsx`（新产品、总览、产品列表、素材库、竞品、更多功能、设置）；`web/components/layouts/WorkspaceLayout.tsx`（手机顶栏加菜单按钮和抽屉，现在手机上没有导航）；新增 `web/components/workspace/ProductFrame.tsx`（产品名和四个视图的标签）；新增页面 `web/pages/w/[slug]/overview.tsx`、`web/pages/w/[slug]/p/[productId]/ideas.tsx`、`tests.tsx`、`results.tsx`、`profile.tsx`（后三个先放占位，分别链到现在的测试页和产品页）；`web/pages/w/[slug]/index.tsx` 和 `web/lib/workspace-navigator.ts`（开关打开时落到总览）；`web/lib/env.ts`（`adsFirstEnabled`）；两套 `common.json`。
- **规则：** 产品列表合并 `list_products` 和 `list_marketing_ideas`。总览里每个产品“现在最该做的事”按这个顺序取第一条成立的：有出错待处理的，测试中缺报表的，有结论还没看的，留下了广告还没测试的，有草稿还没处理的。更多功能沿用现有的折叠组和它的本地记忆，加“改版前的测试”（`/experiments`）和“旧版测试”（`/tests`）。保留现有的 `data-testid`（`workspace-nav-advanced-toggle`、`workspace-settings-link`），黄金路径 e2e 靠它们。
- **测试要点：** `navigation.spec.tsx` 更新；总览每个产品一张卡、一个主要按钮；开关关着时首页仍跳 `/experiments`、“新产品”仍去 `/products/new`；手机宽度能打开菜单；`page-namespaces`、`key-parity` 通过。
- **开工前先确认：** 成员入口放设置页里还是留在导航里（现有 e2e 是否依赖）。

### S7 前端：开始页、读产品页

- **目的：** 设计稿第 1、2 屏。
- **改哪里：** 新增 `web/components/ideas/StartPage.tsx`、`ReadingPage.tsx`、`web/hooks/ideas/useFirstBatch.ts`；页面 `web/pages/w/[slug]/start.tsx`；复用 `PasteBox`、`useUpload('product_intake_image')`；Key 一行复用 `create_credential`（带 `verify: true`）和 `bind_credential`。
- **规则：** Key 的三种情况照设计文档 §5.1。按钮文字带报价接口返回的最高费用。读产品页轮询 `get_marketing_first_batch`，`done` 后跳到广告想法页；`needs_attention` 时显示对应的处理（选型号、改贴介绍、补资料）。示例图是仓库里的静态图片。
- **测试要点：** 有 Key 时不出现 Key 行；没有 Key 且是 Owner 时出现，保存成功后直接开始；不是 Owner 时按钮不可用并有提示；Key 被拒时显示原因且不开始；三种停下的情况各有画面；文案两套齐全。

### S8 前端：广告想法页（看图、留下）

- **目的：** 设计稿第 3 屏的只读部分。
- **改哪里：** 新增 `web/components/ideas/IdeasPage.tsx`、`AdCard.tsx`、`ProductPanel.tsx`；图片用现有的 `useMarketingImageSource`。
- **规则：** 按主张分组；每张广告显示讲法、“草稿”标记、留下按钮；没用产品照片的广告标“没有用产品照片”；做过竞品扫描时，主张卡片上显示一行“和竞品重合”或“还没人说”；右栏显示事实和“还没核对”。“只下载图片”“修改”“再来一种讲法”等按钮在里程碑 A 先不显示，S13 加上。
- **测试要点：** 留下后计数变化并调用 `setMarketingAdKept`；出图中的显示占位；出图失败的显示重试入口占位；一张都没留下时底栏按钮不可用。

### S9 后端：草稿的自动检查

- **目的：** 出图后对每张草稿做单图检查，不依赖小轮。
- **改哪里：** `app/marketing/ai_qc_models.py`（`round_id` 改为可空，加 `project_id`；或新表，开工时定）、`app/marketing/ai_qc.py`（新的输入构造，用 `ideas_json` 的组合和出图意图里的理解快照代替小轮快照）、`app/marketing/ai_qc_worker.py`（不要求小轮在审核状态）、接口 `start_marketing_ideas_checks`。
- **规则：** 只做单图检查；对照检查留到第 2 步冻结小轮之后。检查结果是建议，不产生质量通过记录。检查对照的是出图时的事实；事实核对后变了，检查要重做（S10 判断）。
- **测试要点：** 图里的字和文案不一致时给出发现；引用了没有依据的说法时给出发现；没有 Key 时不受理；结算记录正确；小轮上的检查行为不变（回归）。
- **开工前先确认：** 改现有表还是新建表，哪个对已有的 RLS、授权和查询影响小。

### S10 后端：核对、草稿状态、下载

- **目的：** 用户核对事实之后，草稿变成可导出，可以下载。
- **改哪里：** `app/marketing/ideas.py`（`ad_status`、`edit_understanding`、`verify`、`download_intent`）、`app/marketing/ideas_api.py`、创意库发布（核对时把已核对的内容发布成产品版本）。
- **规则：** `ad_status` 按设计文档 §5.3 的三条算：引用的事实都已核对且和出图时一致；用到的照片已核对（没用照片的要用户明确确认外观）；检查没有未处理的问题且对照的是当前事实。不满足第一条且事实已被改或删的是 `needs_redo`，其余不满足的是 `draft`。下载只接受全部 `exportable` 的广告，否则 409 `ads_not_exportable`，并列出每张的原因。
- **测试要点：** 草稿下载被拒；核对后可下载，下载的只有请求的那几张；改一条被引用的事实后变 `needs_redo`；改一条没被引用的事实不影响（Review Focus 3）；只核对了一部分事实时，只引用这部分的广告可导出；另一个工作区 404。
- **开工前先确认：** 只含已核对内容的产品版本能不能通过创意库的发布校验；下载用现有的导出包还是逐张的下载意图。

### S11 后端：未核对的照片出草稿

- **目的：** 首批就能用产品页上的照片保证产品外观。
- **改哪里：** `app/marketing/product_references.py`（新的解析路径：录入里已通过图片检查的候选图，`attestation="unverified_product_photo"`，策略名 `ads-first-unverified-photos-v1`）、`app/marketing/ideas.py`（首批选主图加至多两张细节图）、S10 的 `verify`（核对照片时把它提升为创意库参考图并标为已对照实物）、`lineage_current`（同一内容哈希的照片已核对时视为当前）。
- **测试要点：** 没核对的照片进了出图请求，图的状态是 `draft`；核对该照片后同一张图变 `exportable`，不用重出；用户否定该照片后变 `needs_redo`；`plan_first` 项目仍然只用已核对的照片（回归）。

### S12 后端：草稿的修改和追加

- **目的：** “修改这张广告”“再来一种讲法”“再想一个主张”“上传我自己的图”在没有小轮时可用。
- **改哪里：** 现有的按意见重出（`prepare_marketing_feedback`）和图片编辑（`create_marketing_image_edit`）都挂在小轮下，要各加一个挂在项目下的入口；`start_marketing_ideas_planning` 加追加模式（加一个主张，或给某个主张加一个讲法，可带上用户对引导问题的回答）；外部素材归到项目的某个组合下。
- **测试要点：** 重出得到新版本，旧版本保留；新版本状态是 `draft`，需要重新检查；追加的讲法和同主张下已有的不能是同一种画面机制；裁剪旋转不产生费用。
- **开工前先确认：** 这两个现有功能对小轮的依赖有多深；接口名定下后更新 D9 的表和总数。

### S13 前端：下载前核对、修改这张广告

- **目的：** 设计稿的“3a”“3b”两屏，以及广告想法页上被 S8 暂时隐藏的按钮、右栏的出图设置和可选的引导问题。
- **改哪里：** `web/components/ideas/CheckSheet.tsx`、`EditAd.tsx`；复用 `MarketingImageEdit`、`MarketingImageTools`、`MarketingVersionHistory`（它们现在从小轮取数，要改成也能从项目取数）。
- **测试要点：** 只列出留下的广告引用的事实；全部勾选后才能确认；确认后调用 `verify_marketing_ideas` 再调用下载；有检查发现时显示三个处理按钮；改文案后预览同步变化。

### S14 前端：产品资料

- **目的：** 设计稿的“产品资料”一屏。
- **改哪里：** `web/components/ideas/ProductProfile.tsx`，页面 `p/[productId]/profile.tsx`；照片和品牌两块复用 `ProductReferenceSettings`、`ProductBrandKitSettings`。
- **规则：** 新流程的产品用这个视图；改版前的产品仍用现在的产品卡编辑器。
- **测试要点：** 核对、修改、删除事实各一条；补充出处后“没有出处的说法”变成事实（未核对）；改被引用的事实前提示“用到它的广告需要重出”。

### S15 前端：竞品独立页面，素材库和设置接入

- **目的：** 竞品从营销测试页里拆出来；素材库和设置从新左边栏进入。
- **改哪里：** 新页面 `web/pages/w/[slug]/competitors.tsx`，渲染现有的 `CompetitorsArea`，上面加产品选择；`CompetitorsArea` 的 `planMarket` 改从产品的 `market` 取；`SnapshotSummary.tsx` 的 `useSnapshotStale` 在没有 `router.query.test` 时不报错。旧的营销测试页里的竞品区保留（改版前的测试还在用）。
- **测试要点：** 不经过营销测试页也能加竞品、扫描、看汇总；切换产品后内容跟着变；`competitors-area.spec.tsx` 仍通过。

### S16 收尾

- **目的：** 第 1 步的验收和上线。
- **内容：** “出错和待处理”里属于第 1 步的情况逐个补齐并各有一条测试；新流程的 Playwright 端到端（空工作区到下载图片）；确认旧的黄金路径 e2e 仍通过；更新 `docs/user-guide.md`、`docs/tutorials/`、`docs/product/provider-credentials-self-service-acceptance.md`；按设计文档 §10 第 1 步的 13 条逐条验收并记录；在生产环境打开 `NEXT_PUBLIC_MARKETAI_ADS_FIRST`。

---

## 设计文档和分块的对应

| 设计文档 | 分块 |
| --- | --- |
| §5.1 开始 | S4、S5、S7 |
| §5.2 读产品 | S2、S4、S7 |
| §5.3 广告想法 | S3、S8、S9、S10、S12、S13 |
| §5.6 导航、总览、竞品独立 | S6、S15 |
| §5.7 现有功能对照（第 1 步范围内的） | S12、S14、S15、S16 |
| §5.8 手机上的排法 | S6 起每块自带 |
| §6 关卡挪到哪里 | S0（合同）、S2、S3、S9、S10、S11 |
| §7 方案拆成两段 | S1、S2（测试设置那一半在第 2 步） |
| §8 费用和 Key | S4、S5、S7 |
| §9 用语表 | S6 起每块的文案 |
| §10 第 1 步验收 1、2、5、6、7、8、9、10、12 | 里程碑 A |
| §10 第 1 步验收 3、4、11、13 | 里程碑 B |
| §11 要同步改的文档 | S0、S16 |

不在第 1 步里的：设计文档 §5.4（拿去测试）、§5.5（结论）、对比表、素材库的连续看图、竞品汇总页和素材库的重画。
