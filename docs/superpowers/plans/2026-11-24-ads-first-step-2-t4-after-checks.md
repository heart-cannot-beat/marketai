# 第 2 步 T4：检查之后（审核记录、广告包、接受发现、取消） · 逐步计划

> 上位计划：[`2026-11-21-ads-first-step-2.md`](2026-11-21-ads-first-step-2.md) 的“T4 后端：检查之后”、E1–E10（特别是 E4、E5、E6、E7）和 Review Focus 4；设计 [§5.3](../specs/2026-11-21-ads-first-step-2-take-to-test-design.md) 的后半和 §5.5；Issue heart-cannot-beat/marketai#250。

**Goal:** “一起看”的检查全部有结果后，为每张留下的广告写这一轮自己的审核记录（每项通过都指得出依据），再生成广告包；有没通过的，用户写明理由接受后再生成；广告包生成之前可以取消测试，取消后可以重新开始。

## 做法（实现时定下的）

- **要加一个迁移（上位计划没预计到）。** `marketing_qc_reviews.quality_review_id` 原来是必填的组合外键：改版前每条这一轮的审核记录都和共享审核链上的一条审核一起写。E5 规定测试项目只写这一轮的记录、不写共享审核链，所以这一列改成可空（迁移 `zzw250a1b2c3`，接在 `zzv252a1b2c3` 后面，Alembic 仍只有一个 head）。外键保留，空值不指向任何记录；不加表、不改授权。降级时如果有不指向共享审核的记录就拒绝。
- **对应表。** `ad_tests.CHECK_SOURCES`：小轮对一张图要求的每一项从哪里找依据。只看这张图的几项（产品外观、事实、主张、讲法、文案一致、语言、品牌风格、文字清楚）由广告想法页上这张广告自己的单图检查回答，检查项编号相同；对照的三项（`workflow.PAIRED_CHECKS`）由这一轮“一起看”的检查回答。组合自带的 `intent_<n>` 按文字相同对上单图检查的 `intent_<n>`。
- **每项通过都带依据（E5）。** `evidence` 按检查项列出依据，每条是 `{"source", "id", ...}`：
  - `paired_check`：这一轮“一起看”的检查里通过的发现，或者这次命令里用户接受的发现（`finding_id`）；
  - `ideas_check`：单图检查里通过的发现（`finding_id`）；
  - `verification`：下载前核对里用户接受过的单图检查发现（`finding_id`）、核对过的事实（`fact_id`，事实的文字和核对时一样）、确认可用的照片（`photo_id`），以及没用产品照片的广告用户确认外观没问题（`asset_version_id`，E5 原来只列了前三种键）。
  第一条是这项为什么算通过；事实和照片是补充。找不到第一条的，不记为通过。
- **拒绝的顺序。** 轮次已结束 `round_closed`、修订号不对 `stale_round_revision`；广告已不可导出 `ads_not_exportable`；共享审核链上当前不通过 `ad_review_rejected`；检查还在跑或结果不确定 `checks_running`（`params.running`、`params.uncertain`；不确定的按现有核对处理，不重发）；接受的发现不是最新那次检查的 `check_changed`，那次检查里没有这条 `finding_not_found`；有检查项找不到依据 `check_evidence_missing`（`params.ads` 每张列出检查项和原因：`check_missing`、`check_failed`、`check_outdated`、`no_evidence`）；有没通过、也没被接受的 `check_findings_open`（`params.findings` 和请求体里接受发现的形状一样，页面可以原样带回）。被拒时什么都不写。
- **写什么。** 每张广告一条 `MarketingQCRecord`：`quality_review_id` 为空，`findings_json` 的 `source` 是 `test_checks`，带检查结果、要求的检查项、两次检查的编号和输入 hash、接受的发现（`overrides` 记 AI 的结果和“人工判为通过”，`override_note` 是理由）、`evidence`；每张写审计事件 `marketing.creative_reviewed`。然后照现有方式请求广告包（`workflow.create_handoff`，审计 `marketing.handoff_requested`），测试项目上再写 `marketing.test_package_requested`。打包由 Worker 照现有方式做；审核记录和广告包请求只由 API 写（E7）。
- **幂等（E7、Review Focus 4）。** 这一轮有了广告包之后，再调用（同一个幂等键或新的、带不带接受的发现、修订号新旧）都返回同一个结果，不会出第二个包。同一个幂等键原样重放也返回同一个结果。
- **`ready` 对留下的广告。** 这一轮的审核通过、没有阻断的文件检查、照片按 E4 是最新的；不要求共享审核链上有通过的审核，只要那里没有不通过的。改版前的图仍按原规则，共享审核链不变，所以同一张图在改版前测试里的 `ready` 不变。
- **旧的小轮复核接口不能复核留下的广告。** `workflow.review`（以及调用它的 AI 检查批量确认）遇到测试轮次里留下的广告返回 409 `test_review_by_checks`：它会写共享审核链。
- **读轮次的接口不加字段。** `get_marketing_round` 的校验是严格的。测试的审核记录在那里显示成它本来的样子（`ai_assisted_human_review`，去掉 `evidence` 和接受的发现）；广告包条目里留下的广告的来源（`kind: "ideas"`）显示成 `existing_creative`，带上 `include_reuse_lineage` 时 `source_project_id` 是来源的广告想法项目。依据走新接口。
- **取消。** `ad_tests.cancel`：广告包生成之前，这一轮按 `user_stopped` 结束（不删除），`note` 是用户写的原因，没有就写一句默认的；测试项目上写 `marketing.test_cancelled`。之后产品没有进行中的测试，可以用同样的广告重新开始。还没发出的检查 Worker 不再发（轮次已不在检查阶段），预留释放；已经发出或不确定的照常结算或核对。广告包生成之后取消返回 409 `test_package_exists`（要停下来用 T7 的提前结束）。

## 接口

| operation_id | 路径 | 说明 |
| --- | --- | --- |
| `complete_marketing_test_start` | `POST /marketing/tests/{test_id}/package` | 200；If-Match 测试当前轮次的修订号；幂等；请求 `{accepted_findings: [{asset_version_id, check_id, finding_ids, note}]}`；回应 `MarketingTestPackage`（广告包编号和状态、每张广告的审核记录和依据），ETag 是轮次修订号 |
| `cancel_marketing_test` | `POST /marketing/tests/{test_id}/cancel` | 200；If-Match 轮次修订号；幂等；请求 `{note?}`；回应 `MarketingTestDetail`，ETag 是测试的修订号 |

接口数 279 → 281。新错误码：`checks_running`、`check_findings_open`、`check_evidence_missing`、`test_package_exists`、`test_review_by_checks`（两种语言都有文案）。

## 步骤

- [x] 写测试 `tests/test_marketing_test_package.py`：全部通过 → 每张一条审核记录、每项都有依据、不写共享审核链、广告包生成并能下载、读轮次的接口仍然有效；单图检查通过的作为依据；Controlled Mock 的发现全部列出、接受后生成；一项没通过 → 列出，接受时检查已变化、没有这条发现、没写理由都被拒，接受后生成；“一起看”的检查失败 → 找不到依据、不生成；检查还在跑或不确定 → 等，不重发；共享审核链上新判了不通过、广告已不可导出 → 拒绝；重放同一命令、新键旧修订号、带无关的接受都返回同一个包；修订号不对、缺 If-Match；取消后没有进行中的测试、检查不再发、可以重新开始；默认的取消原因；有广告包后不能取消；另一个工作区 404；工作区停用 403；Owner 和 Member 都可以；旧复核接口不复核留下的广告；对应表；同一张图在改版前测试里的审核和 `ready` 不变。
- [x] 迁移 `zzw250a1b2c3`，模型 `quality_review_id` 可空；固定 head 的四个测试改成新 head。
- [x] `app/marketing/ad_tests.py`：`complete_start`、`cancel`、`package`、`CHECK_SOURCES`。
- [x] `app/marketing/workflow.py`：`ready` 对留下的广告的规则、`review` 拒绝留下的广告、读轮次时的两个投影。
- [x] 两个接口、合同、BFF 策略、网关方法、生成的客户端、接口数、两种语言的错误文案。
- [x] `make verify`。
