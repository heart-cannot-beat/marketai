# 第 2 步 T5：测试轮次的报表 · 逐步计划

> 上位计划：[`2026-11-21-ads-first-step-2.md`](2026-11-21-ads-first-step-2.md) 的“T5 后端：测试轮次的报表”和 E1–E10；设计 [§5.7](../specs/2026-11-21-ads-first-step-2-take-to-test-design.md) 的前两条；Issue heart-cannot-beat/marketai#251。

**Goal:** 测试项目的轮次接受“从开始到今天”的完整报表：后传的覆盖重叠的日子，两份都留在记录里；不属于这一轮的行只存不算，并在返回里列出来。

## 做法（实现时定下的）

- **每份上传是一条普通的 `MarketingMetricBatchRecord`。** 不加表、不加迁移。`metadata_json` 的键和改版前完全一样（读轮次的接口返回它，前端的校验器是严格的）；`replaces_id` 恒为空。
- **计入什么在读的时候算，不存。** `ad_test_reports.counted(session, ctx, round)`：按上传先后（`created_at`、`id`）把已确认的报表叠起来，每个（哪天、哪条 Meta 广告）留最后一份；再只留这一轮对上的广告、在计入日期里的行。这样 T6 之后补对的广告、T7 改的开始日期，不用改任何一份上传就能生效。
- **计入日期集中在一处。** `ad_test_reports.counting_dates(session, ctx, round)` 现在返回冻结的日期；T7 有了“有效开始、有效结束”之后改这一个函数。
- **所有读报表行的地方都走 `workflow.counted_rows`。** 花费（`project_spend`）、评估的输入、评估是否最新、看板上哪些广告有数据、证据交接的核对。改版前的轮次返回的行和原来逐字相同。
- **两条规则互不串用。** 改版前的 `preview_marketing_metrics`、`confirm_marketing_metrics` 遇到测试项目的轮次返回 409 `test_report_required`；新接口先按测试找，再要求轮次属于这个测试，改版前的轮次走不到。
- **只接受 `customer_file`。** 请求模型里 `source_mode` 只有这一个值，别的值 422。
- **设置不一致逐项说明。** 时区、币种（自报的和金额列名里写的，比如 `Amount spent (EUR)`）、归因窗、广告账户，各自一个问题码 `report_*_mismatch`，带 `expected`（测试按什么定的）和 `found`（报表是什么）。改版前那条笼统的 `reporting_context_mismatch` 在测试报表里不出现。
- **其他行不算，但有问题照样拦。** 不属于这一轮的广告（`other_ad`）、开始之前（`before_start`）、结束之后（`after_end`）的行不报问题；格式、重复、数值这些问题照旧用现有问题码，有就不能确认。
- **旧报表不能后确认。** 已经确认了一份更晚上传的报表时，再确认更早上传的那份会把新数据换回旧的，返回 409 `test_report_outdated`。
- **要先有广告包。** 广告包没生成时上传返回 409 `handoff_not_ready`。

## 接口

| operation_id | 路径 | 说明 |
| --- | --- | --- |
| `preview_marketing_test_report` | `POST /marketing/tests/{test_id}/rounds/{round_id}/report/preview` | 201；If-Match 轮次修订号；幂等；同一份内容再传返回同一份 |
| `confirm_marketing_test_report` | `POST /marketing/tests/{test_id}/rounds/{round_id}/report/confirm` | 200；If-Match；幂等；已确认的再确认返回它现在的样子 |

两个都返回 `MarketingTestReport`：这份报表的设置、日期范围、问题，以及 `counted_rows`（现在计入几行）、`replaces_rows`（覆盖了之前几行）、`replaced_rows`（被之后的报表覆盖了几行）、`excluded`（按广告和原因分组：日期范围、行数、花费）。接口数 267 → 269。

## 步骤

- [x] 写失败的测试 `tests/test_marketing_test_reports.py`（先 1–3 日再 1–5 日；多一条别的广告；EUR 对 USD；时区、归因窗、账户；坏行；只收 `customer_file`；旧报表；幂等；没有广告包；修订号；另一个工作区；别的测试的轮次；停用的工作区；改版前轮次的规则；改版前接口拒绝测试轮次）。
- [x] `metrics.read_rows` 从 `metrics.preview` 拆出；新问题码进 `ISSUE_PARAMS`。
- [x] `app/marketing/ad_test_reports.py`、`ad_test_reports_contracts.py`；`workflow.counted_rows` 和它的使用处；两个接口。
- [x] 新码的英文、简体中文文案；OpenAPI、策略、生成的客户端、网关方法、接口数。
- [x] `make verify`、`make web-verify`。
