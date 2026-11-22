# 第 2 步 T8：目前的数字、还差多少单、补记偏差、改了广告想法的提示 · 逐步计划

> 上位计划：[`2026-11-21-ads-first-step-2.md`](2026-11-21-ads-first-step-2.md) 的“T8 后端：目前的数字……”和 E1–E10（特别是 E4、E9、E10）；设计 [§5.7](../specs/2026-11-21-ads-first-step-2-take-to-test-design.md) 的“目前的数字”“还差多少单”“补记和计划不一样的地方”和 §5.8；Issue heart-cannot-beat/marketai#254。

**Goal:** 测试进行中随时能看每个比较对象目前的花费、订单数、每单成本和还差多少单（不是结论，看的时候不评估）；任何时候都能补记和计划不一样的地方，记了的比较对象这一轮不下结论；测试开始后改了广告想法时提示是哪几张广告，评估和广告包不受影响。

## 做法（实现时定下的）

- **目前的数字在读的时候算，不存、不评估。** `ad_test_progress.progress`：计入的行就是评估会读的那些（`workflow.counted_report_rows`，T5 的“每个哪天、哪条 Meta 广告取最新确认的一份”，只在有效日期里）；每个比较对象（方案里的一组）把它的广告对上的 Meta 广告的行加起来。每单成本 = 花费 ÷ 订单数，还没有订单时为空。还差多少单 = `max(0, min_purchases - purchases)`，用 T7 的 `ad_test_schedule.remaining_purchases`，和最小结果里的同一个算法。读的时候不写评估记录、不写审计事件、轮次修订号不变。
- **偏差记在新表 `marketing_round_deviations`，只追加。** 一条记录写明这一轮广告包里的哪几张广告（`asset_version_ids`）、哪里不一样（`note`）、谁记的、什么时候。按广告记，不按比较对象记：实际投放里变的是某几条广告；返回里每条偏差带它影响到的比较对象（`comparison_ids`）。
- **评估时当作对应时记的偏差。** `workflow.evaluation_bindings` 是评估读对应关系的唯一入口（`evaluate`、`evaluation_current`、`evidence_handoff.current_evidence` 都用它）：测试项目的轮次把每条偏差的 `note` 追加进它点名的广告的每条对应的 `deviations`（按广告包条目找），评估输入里多一个 `deviation_ids`。照现有规则，这些对象得到 `delivery_conditions_changed`，判为“还看不出来”。对应记录本身不改；先补记、后对上 Meta 广告的也算。改版前的轮次、没有偏差的测试轮次，评估输入一字不变。
- **什么时候能补记。** 广告包生成之后（之前没有投放，409 `handoff_not_ready`），任何时候，轮次结束后也行（`allow_closed_evidence`）。广告要在这一轮的广告包里（422 `ad_not_in_package`），不能重复（422 `duplicate_asset`）。If-Match 轮次修订号、幂等，写审计事件 `marketing.round_deviation_recorded`。补记前的提示由前端写。
- **改了广告想法的提示。** 这一轮里来自广告想法的广告，是 `kind == "ideas"` 的复用记录（E4，T3 写入），记着开始时那张图的 `intent_hash`。按第 1 步的规则（`ideas_export.statuses`）拿开始时的出图意图和广告想法现在的样子比：引用的事实被改或删（`facts_changed`、`facts_removed`）→ `fact`，用到的照片被否定或不能再用（`photo_unavailable`）→ `photo`，这个组合下的内容（文案、主张、讲法）不再是图里的（`ideas_changed`）→ `copy`。广告想法读不出来、或者图不是开始时那张，按 `copy` 说。
- **广告包照样能下载。** `download_marketing_handoff` 对测试项目的轮次不再调 `assert_ads_exportable`（按广告想法当前的状态拒绝）；`handoff_quality_current` 照旧。改版前的轮次照旧拒绝。生成广告包和重试失败的广告包不变。
- **评估不变。** 改事实、否定照片、改文案之后，同一轮再评估得到同一条评估记录（输入 hash 相同），`get_marketing_test_result.current` 仍为真。main 上的 `workflow.ready` 不看广告想法；T3 若让 `ready` 按广告想法的照片状态判断 `kind == "ideas"` 的图，需按 E4 在广告包生成后固定下来，这里的测试会守住。

## 接口

| operation_id | 路径 | 说明 |
| --- | --- | --- |
| `get_marketing_test_progress` | `GET /marketing/tests/{test_id}/rounds/{round_id}/progress` | ETag 是轮次修订号；只读 |
| `record_marketing_test_deviation` | `POST /marketing/tests/{test_id}/rounds/{round_id}/deviations` | 201；If-Match；幂等；返回补记后的 `MarketingTestProgress` |

`MarketingTestProgress`：`provisional`（恒为真：不是结论）、币种、目标成本、`min_purchases`、有效日期、`result_date`、`data_through`（计入的报表到哪天）、`comparisons`（每组的广告、花费、订单数、每单成本、还差多少单、`conditions_changed`）、`deviations`（每条偏差和它影响的比较对象）、`changed_ads`（广告、改了哪几种、改了的事实）。接口数 274 → 276。

## 新表的授权（等维护者确认）

| 角色 | 权限 | 为什么 |
| --- | --- | --- |
| `marketai_domain_api` | `SELECT`、`INSERT` | 补记偏差；读目前的数字、评估、判断评估是否最新 |
| `marketai_domain_worker` | `SELECT` | Worker 在付费的结果解释前、Scale 的证据核对里判断评估是否最新，要读测试轮次的偏差 |
| `marketai_domain_readonly` | `SELECT` | 只读 |

没有角色能 `UPDATE`、`DELETE`。RLS 启用并强制，`tenant_isolation`；`(workspace_id, round_id)` 组合外键指向 `marketing_rounds`。同步到 `deploy/postgres/domain-tenant-roles.sql`，登记在 `tests/test_postgres_rls.py`。

## 步骤

- [x] 写测试 `tests/test_marketing_test_progress.py`：读目前的数字前后评估记录数、审计事件数、修订号不变；还差多少单不小于 0；补记偏差后这个对象“还看不出来”、别的不变；先补记后对上也算；补记的拒绝和幂等、轮次结束后也能补记；改事实、否定照片、改文案：提示出现、评估不变、广告包照样能下载；改版前轮次的下载仍被拒、评估不变；另一个工作区 404、别的测试的轮次 404、停用的工作区 403、成员和所有者都能记；降级有记录时拒绝。
- [x] 迁移 `zzv254a1b2c3`（`down_revision = "zzu253a1b2c3"`）、模型、角色授权、RLS 测试、各处固定的 head。
- [x] `app/marketing/ad_test_progress.py`、`ad_test_progress_contracts.py`；`workflow.evaluation_bindings`；下载的放宽；两个接口。
- [x] 新错误码的英文、简体中文文案；OpenAPI、策略、生成的客户端、网关方法、接口数。
- [x] `make verify`、`make web-verify`。
