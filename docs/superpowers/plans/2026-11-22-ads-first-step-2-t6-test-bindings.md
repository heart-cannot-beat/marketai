# 第 2 步 T6：按广告名预填对应、一次确认对应 · 逐步计划

> 上位计划：[`2026-11-21-ads-first-step-2.md`](2026-11-21-ads-first-step-2.md) 的“T6 后端：按广告名预填对应、一次确认对应”、E1–E10 和 Review Focus 5；设计 [§5.7](../specs/2026-11-21-ads-first-step-2-take-to-test-design.md) 的第三、四条；Issue heart-cannot-beat/marketai#252。

**Goal:** 读用户传上来的报表里的广告名，按短键预填“这条 Meta 广告是哪张图”；用户确认一次，这一次的全部对应一起生效。第一次确认报表之前可以改或删对应，之后只能新增。

## 做法（实现时定下的）

- **要加一个迁移（上位计划没预计到）。** `marketing_ad_bindings` 原来对（工作区、轮次、广告账户、Ad ID）唯一，API 角色对它只有读和插入。要“改的时候追加新记录、旧的留作历史”，表上加 `replaces_id`（替换的是哪条，同工作区组合外键指向本表）、`removed`（这条记录把对应拿掉）、`actor_id`（谁确认的）；原来的唯一约束换成只管第一条记录的部分唯一索引（`replaces_id IS NULL`），再加（工作区、`replaces_id`）唯一：一条 Meta 广告在一轮里的记录是一条线，每条最多被替换一次。迁移 `zzv252a1b2c3`，接在 `zzu253a1b2c3` 后面；不加表、不改授权。
- **“现在的对应”只有一个读法。** `workflow.round_bindings(session, ctx, round)`：一轮里没有被替换、也不是“拿掉”的记录。原来直接查绑定表的地方都改用它（评估的输入、评估是否最新、读轮次、看板、报表计入、证据交接、改版前的上传预检、Scale 按 Meta 广告找图）。改版前的轮次从来没有替换记录，结果和原来一样。
- **读广告名不落库。** `ad_test_reports.suggest_bindings` 读这一轮一份已上传报表的原始 CSV（“Ad name”列），每条 Meta 广告给出状态：`bound`（这一轮已对上）、`suggested`（只有一个短键、这一轮只有一张广告带它）、`no_match`（没有短键，或者短键不是这一轮的）、`ambiguous`（两个短键，或者一个码对上了好几张）、`bound_elsewhere`（已经对到别的轮次）。名字里有 `MA-` 键时只看 `MA-`，没有才看 `MK-` 码（`app/learnings/codes.codes_in_text`）。只在这一轮的广告包条目里找。
- **一次确认，要么全写要么全不写。** `ad_test_reports.confirm_bindings` 先把每一条都检查完（条目属于这一轮、账户一致、别的轮次在重叠的日子里没有对上同一条 Meta 广告、确认过报表之后不改不删），再一起写；和现有的一样，整条命令在一个事务里。已经是这样的选择不写新记录。
- **改和删都是新记录。** 改：一条新记录替换现在的那条；删：一条 `removed` 的新记录替换它；删掉之后再对上：再一条新记录。旧记录不动。
- **确认过报表之后只能新增。** 用 T7 的 `report_confirmed`：这一轮有确认过的报表时，改或删已有的对应返回 409 `test_bindings_locked`；同一条命令里有一条被拒，其他的也不写。
- **日期按有效日期。** 新记录记下这一轮当时的有效开始、结束（T7 的 `effective_dates`）；和别的轮次是否重叠也按两边的有效日期判断。
- **两条路不串用。** 改版前的 `bind_marketing_delivery` 遇到测试项目的轮次返回 409 `test_bindings_required`；新接口先按测试找，轮次不属于这个测试就 404。
- **偏差不在这里记。** 测试轮次的对应记录的条件一律是“按方案投放、没有偏差”，偏差由 T8 的补记偏差另外追加。

## 接口

| operation_id | 路径 | 说明 |
| --- | --- | --- |
| `suggest_marketing_test_bindings` | `POST /marketing/tests/{test_id}/rounds/{round_id}/bindings/suggest` | 200；只读，不落库，不要幂等键和 If-Match；请求是 `{batch_id}` |
| `confirm_marketing_test_bindings` | `POST /marketing/tests/{test_id}/rounds/{round_id}/bindings` | 200；If-Match 轮次修订号；幂等；一次写全部 |

`MarketingTestBindingSuggestions`：报表的广告账户、广告名那一列（没有时为空）、是否只能新增（`locked`）、这一轮广告包的广告（带 `MK-` 码），以及报表里每条 Meta 广告的状态、预填的广告、按哪种短键判断、可选的广告、已经对到的别的轮次。`MarketingTestBindings`：现在的对应和全部记录（从早到晚，带替换了哪条、谁、什么时候）。接口数 274 → 276。

## 步骤

- [x] 写失败的测试 `tests/test_marketing_test_bindings.py`（两种短键各一条能预填、读名字不落库；改了名、两个短键、别的测试的键不预填；已对到别的轮次的列出来；没有广告名一列；一次确认写全部；中途一条出错全不写；同一条 Meta 广告选两次；一个账户；报表确认前改和删、旧记录保留；确认后只能新增；幂等和修订号；要先有广告包；结束的轮次补对应；另一个工作区 404；别的测试的轮次和报表；停用的工作区；改版前轮次的规则；改版前接口拒绝测试轮次；删掉后改版前轮次能用；数据库的一条线约束；迁移降级；Scale 只看现在的对应）。
- [x] 迁移 `zzv252a1b2c3`、模型；固定 head 的测试改成新 head。
- [x] `workflow.current_bindings`、`round_bindings` 和它们的使用处；`workflow.bind` 拒绝测试轮次。
- [x] `ad_test_reports.suggest_bindings`、`confirm_bindings`、`bindings_view`；契约；两个接口。
- [x] 新码的英文、简体中文文案；OpenAPI、策略、生成的客户端、网关方法、接口数。
- [x] `make verify`、`make web-verify`。
