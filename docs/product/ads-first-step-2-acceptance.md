# 新开头第 2 步验收记录

对应 #259（新开头第 2 步 T13，依赖图 #245）。合同是 [ADR 0019](../adr/0019-take-to-test-opens-a-test-project.md) 和[第 2 步设计](../superpowers/specs/2026-11-21-ads-first-step-2-take-to-test-design.md) §7 的 12 条验收。逐条写明怎么验的、结果、证据在哪。

## 在哪里验的，没验什么

- **本地栈。** 本机 Docker Compose（`docker-compose.yml` 加 `docker-compose.ads-first.yml`，网页带开关构建），2026-11-25，#259 的分支（从 main `e198dba` 切出，T0–T12 都已合并）。本机的 5432、8787 端口被别的项目占着，所以多叠了一个只在本机用的覆盖文件，把数据库和 API 映射到别的端口，其余和 `make ads-first-e2e` 一样。全程 Controlled Mock，没有调用任何真实的 AI 服务，没有用真实的 Key，没有任何 Meta 写入。只用虚构内容：贴的是 Acme Charge 的文字；模拟读产品给的产品名是原教程用的虚构产品名；报表是端到端测试里现生成的 CSV，广告账户和 Ad ID 都是编出来的。
- **端到端。** 新加的 [`web/e2e/ads-first/take-to-test.spec.ts`](../../web/e2e/ads-first/take-to-test.spec.ts)，在 `make ads-first-e2e` 下运行，第 1 步的 `empty-to-download.spec.ts` 同时跑。两份共用的注册、Controlled Mock 和截图挪进了 `web/e2e/ads-first/journey.ts`。新端到端走的是：第 1 步做出广告想法、留下两张 → 没核对时“拿去测试”被挡、去核对 → 三个问题 → 预算少时“只能看个大概”和需要多少钱 → 预算够了 → 点一次开始 → “一起看”的检查有发现、广告包等着 → 写理由接受 → 广告包 → 下载广告包 → 传第一份报表、按短键预填、确认一次、开始日期改到实际开始那天 → 传第二份更长的报表、覆盖重叠的日子 → 目前的数字 → 测试记录 → 总览 → 看结果 → 提前结束、再看结果。每个画面都断言主区域不出现内部编号；“拿去测试”、测试记录和“测试进行中”还在 375 像素宽下量有没有横向滚动。浏览器时区固定为 UTC，计划的日子、报表的日子和断言用同一本日历。
- **自动测试。** 后端 `make verify`，前端 `make web-verify`，旧黄金路径（开关关着的构建）。命令和结果见 #259 的 PR。下文引用的后端测试写成 `文件::类::测试`，前端测试写成 `文件` 加测试标题。
- **本地浏览器走查。** 端到端跑完后，用它留下的登录状态把“拿去测试”“测试进行中”“测试记录”和总览在简体中文、手机宽度、暗色下各看一遍。截图在本机，不进仓库。
- **没有验的。** 下面这些只有维护者能做，列在本文最后的“上线前（维护者）”：
  - 用真实的 Key 跑“一起看”的检查：按钮上的最高费用大于 $0 时的写法、真实检查的结果和花费；
  - 真实 Meta 导出的报表（列名、币种、时区、归因窗口）；
  - 在生产上打开开关、在生产环境走一遍。
  生产证据不进这个仓库。
- **结果的写法。** “通过”是在上面的环境里看到了要求的行为；“部分”写明缺哪一块；“自动测试”是只靠自动测试证明、本地栈上没有走到。

## 12 条

| # | 验收（设计 §7） | 怎么验的 | 结果 | 证据 |
| --- | --- | --- | --- | --- |
| 1 | 从留下的广告到下载广告包，用户最多回答三个问题（没有产品链接时加落地页），加一次下载前核对；唯一的费用确认是按钮上的最高费用 | 端到端：页面只问一单多少钱以内算成功、这一轮花多少、投多久（默认 7 天），其他折叠在默认项里；贴文字建的产品没有链接，报价要求落地页后才多问这一项；没核对时被挡，去做第 1 步的下载前核对；之后点一次“开始测试”，到广告包之间唯一要做的是给检查发现写理由（见第 6 条）。前端测试：三个回答后按钮上写报价的最高费用（$0.24）；有链接时不问落地页；默认项折叠、可改。后端测试：没有链接时报价要求落地页；最高费用是每张广告一次“一起看”的检查 | 通过（本地栈加自动测试）。Controlled Mock 的检查不花钱，本地按钮写“开始测试”，不带金额；带金额的写法由前端测试和后端测试证明，真实金额留给维护者 | 端到端；`web/__tests__/components/tests/take-to-test.spec.tsx`“asks three questions; the button then carries the quote’s maximum”“asks for the landing page only when the product has no link”“keeps the defaults folded, each changeable, the currency among them”；`tests/test_marketing_test_quote.py::TestQuoteTests::test_without_a_product_link_the_landing_page_is_asked_for`、`::PaidCheckQuoteTests::test_the_button_maximum_is_one_look_together_check_per_ad`；新开头教程图 10–13 |
| 2 | 钱不够下结论时，下载前页面写明“只能看个大概”和够下结论需要多少，并给出建议 | 端到端：目标 $30、预算 $240 时页面写“只能看个大概”，“要都能下结论，这一轮需要 $1,200”，等于比较对象数 × 20 单 × $30，和报价接口给的一样；预算改成 $1,200 后写“够下结论”；预计出结论的日期是开始后第 16 天。前端测试另外覆盖“少比几个”的建议。后端测试覆盖三档的边界 | 通过（本地栈加自动测试）。两个比较对象、$240 时只比一个也不够，所以本地栈上没有出现“少比几个”的建议，由前端和后端测试证明 | 端到端；`take-to-test.spec.tsx`“short of money: only a rough read, what a verdict needs and a suggestion”；`tests/test_marketing_test_quote.py::EstimateTests::test_under_ten_orders_gives_a_rough_read_only`、`::EstimateTests::test_the_suggestion_is_how_many_to_compare_for_enough`、`::TestQuoteTests::test_the_three_tiers_and_the_day_of_the_result`；教程图 10 |
| 3 | 冻结的小轮快照和现有形状兼容；导出、回传、评估的现有验收场景照常通过；改版前的测试行为不变 | 后端测试：一次开始得到一个测试、一轮、每张广告一条复用记录，快照是改版前的形状；模板生成的方案过不了现有校验时拒绝、不放宽；按小轮编号的接口接受测试项目，改方案等接口对它 404。改版前的轮次：上传、对应、评估、审核、素材库规则都有“不变”的测试。现有的导出、回传、评估测试都在 `make verify` 里照常通过，旧黄金路径 e2e 照常通过 | 通过（自动测试加旧黄金路径） | `tests/test_marketing_test_start.py::StartTests::test_one_start_opens_one_test_with_one_round_a_reuse_per_ad_and_its_checks`；`tests/test_marketing_test_quote.py::PlanTemplateTests::test_a_plan_the_checks_refuse_is_refused_not_loosened`；`tests/test_marketing_tests.py::TestProjectRoundPathTests::test_round_paths_accept_a_test_project_and_plan_paths_do_not`；改版前的轮次见下面“和旧流程的衔接” |
| 4 | 上传的报表按广告名里的短键预填对应关系，用户确认后才生效 | 端到端：报表里两条广告用广告包里的广告名（带 `MA-` 短键），页面预填这两条，写明“从广告包短键填好”；第三条广告名里没有短键，列为“对不上”，默认跳过；按钮写“确认这 2 条对应”，点一次才写入。后端测试：`MA-` 和 `MK-` 各一条能预填、预填不落库；一次确认写全部，一条被拒就一条都不写 | 通过（本地栈加自动测试）。`MK-` 码的预填本地栈上没有走，由后端测试证明 | 端到端；`tests/test_marketing_test_bindings.py::SuggestionTests::test_each_short_key_prefills_the_ad_it_names_and_nothing_is_stored`、`::ConfirmationTests::test_one_confirmation_writes_every_match_and_the_rows_count_from_then_on`、`::ConfirmationTests::test_when_one_match_is_refused_none_is_written`；`test-in-progress.spec.tsx`“uploads the full report, prefills the matches, confirms them once, then counts the report”；教程图 15 |
| 5 | 同一轮连续上传两份“从开始到今天”的报表，后一份覆盖重叠的日子，结果和只传后一份一样，两份都留在记录里；报表里不属于这次测试的行不算进数字，并列出来 | 端到端：第一份 5 天、第二份 7 天，数字不同；第二份确认后写“替换了前面报表里的 15 行”（三条 Meta 广告重叠的 5 天，不属于测试的那条也一样被覆盖，只是不算），“目前的数字”等于只算第二份的花费和订单（不是两份相加）；测试记录列出两份上传；不属于测试的那条广告列在“存下了但不算进数字的行”。后端测试：结果和只传后一份完全一样，两份都确认在记录里；别的广告、开始之前、结束之后的行都列出 | 通过（本地栈加自动测试） | 端到端；`tests/test_marketing_test_reports.py::FullReportTests::test_a_later_full_report_replaces_the_overlapping_days_and_both_stay_on_record`、`::FullReportTests::test_rows_of_other_ads_and_other_days_are_kept_but_not_counted`；`tests/test_marketing_test_history.py::ProductTestHistoryTests::test_a_test_rounds_full_reports_are_listed_as_uploaded_through_the_test_report_commands`；教程图 16、17 |
| 6 | 留下的广告里有不可导出的，不能开始测试；“一起看”的检查有不通过的，广告包不会生成，直到用户处理；质量审核里每一项通过都能指出依据；改版前测试的审核结果不变 | 端到端：没核对的两张留下的广告，页面写“有 2 张留下的广告还不能放进广告包”，开始按钮不可用，“去核对”打开下载前核对。Controlled Mock 的“一起看”的检查看不出结果，每张都展开发现，这时没有广告包；写理由点“这样可以，继续”后才生成。后端测试：不可导出的拒绝并列出；没通过的拒绝到用户接受；找不到依据的拒绝、不生成；全部通过时每张一条审核记录、每项都有依据；改版前测试的审核和 `ready` 不变 | 通过（本地栈加自动测试）。“全部通过、自动生成广告包”的路径 Controlled Mock 走不到，由后端和前端测试证明 | 端到端；`tests/test_marketing_test_start.py::StartRefusalTests::test_ads_that_may_not_be_exported_are_refused_and_named`；`tests/test_marketing_test_package.py::OpenFindingTests::test_a_check_that_did_not_pass_is_refused_until_the_user_accepts_it`、`::OpenFindingTests::test_a_check_with_nothing_behind_it_is_refused_and_no_package_is_made`、`::PackageTests::test_all_checks_passed_gives_one_review_per_ad_with_evidence_for_every_check_and_the_package`、`::EarlierTestTests::test_the_earlier_tests_review_and_ready_do_not_change`；`take-to-test.spec.tsx`“sends a kept ad that cannot be exported yet to the check before download”“checks did not pass: only those ads open, and a reason for each makes the package”；教程图 12、13 |
| 7 | 晚几天开始投、第一次确认报表之前改了开始日期的一轮，能正常出结论 | 端到端：计划明天开始，报表从 30 天前开始；第一次传报表时页面问“看起来你是那天开始投的，要把开始日期改成那天吗？”，改了以后计入，最后一天跟着顺延；之后“看结果”正常出结果。后端测试：晚三天开始、改了开始日期的一轮出结论；确认报表之后不能再改 | 通过（本地栈加自动测试） | 端到端；`tests/test_marketing_test_schedule.py::MovedStartTests::test_a_round_that_went_live_three_days_late_reaches_a_conclusion`、`::MovedStartTests::test_the_start_moves_before_the_first_report_only_within_the_rules`；`tests/test_marketing_test_reports.py::FullReportTests::test_a_moved_start_moves_the_days_that_count`；`test-in-progress.spec.tsx`“on the first report, asks to move the start to the day the ads went live” |
| 8 | 提前结束的一轮，等归因期过去能看到最小结果（多半是还看不出来，写明还差多少单） | 端到端：看过一次结果后提前结束在第 4 天，页面先说明后果再确认；再看结果写“这一轮提前结束，最后一天是那天”，每个比较对象“还看不出来”，还差的单数按前 4 天的订单算。后端测试：提前结束的一轮在最后一天加归因期之后评估，`not_clear` 带还差多少单 | 通过（本地栈加自动测试） | 端到端；`tests/test_marketing_test_schedule.py::EarlyEndTests::test_an_early_ended_round_is_evaluated_after_its_last_day_and_the_attribution_lag`、`::EarlyEndTests::test_the_last_day_lies_between_the_start_today_and_the_planned_end`；`test-in-progress.spec.tsx`“ends the round early after saying what it means, and shows a refusal” |
| 9 | 测试开始后改了广告引用的事实，测试的评估不变，测试页出现提示 | 后端测试：改被引用的事实、否定用到的照片、改文案，三种都出现提示，评估和广告包都不变。前端测试：测试页列出改过的广告，广告包照样能下载 | 通过（自动测试）。本地栈上没有改事实：端到端没有走产品资料，走查时也没做 | `tests/test_marketing_test_progress.py::ChangedAdTests::test_a_changed_fact_shows_on_the_test_and_changes_neither_its_evaluation_nor_its_package`、`::ChangedAdTests::test_a_rejected_photo_shows_on_the_test_and_changes_neither_its_evaluation_nor_its_package`、`::ChangedAdTests::test_changed_copy_shows_on_the_test_and_changes_neither_its_evaluation_nor_its_package`；`test-in-progress.spec.tsx`“shows the ads changed after the start and still downloads the package” |
| 10 | 同一个产品不能同时有两个进行中的测试 | 端到端：测试开始后再打开这个产品的“拿去测试”，页面直接转到进行中的那个测试。后端测试：有进行中的测试时开始被拒；两个开始同时到只开一个；取消后可以重新开始 | 通过（本地栈加自动测试） | 端到端；`tests/test_marketing_test_start.py::StartTests::test_a_product_with_a_test_in_progress_starts_no_other`、`::StartTests::test_two_starts_at_once_open_one_test`；`tests/test_marketing_tests.py::TestProjectReadTests::test_a_product_has_a_test_in_progress_until_its_round_closes`；`tests/test_marketing_test_package.py::CancelTests::test_a_cancelled_test_leaves_nothing_in_progress_and_the_ads_can_be_tested_again`；`take-to-test.spec.tsx`“a second test of the product is refused: carries on with the one in progress” |
| 11 | 另一个工作区的成员和非成员看不到也操作不了这些测试、轮次、报表和对应 | 后端负向测试：每个新接口在另一个工作区返回 404 或什么都读不到，轮次只能从它自己的测试找到；没有当前授权的工作区什么都做不了；测试记录没有经过验证的身份时 401。PostgreSQL：两张新表的行只在自己的工作区，只追加不改写；测试项目只能指向本工作区的广告想法 | 通过（自动测试）。本地栈上没有用第二个工作区的账号点一遍。PostgreSQL 的测试在 CI 的 `domain-rls` 里跑 | `tests/test_marketing_tests.py::TestProjectRoundPathTests::test_another_workspace_cannot_read_a_test_or_its_rounds`；`tests/test_marketing_test_quote.py::TestQuoteAccessTests::test_another_workspace_and_other_flows_are_not_found`；`tests/test_marketing_test_start.py::StartAccessTests::test_another_workspace_cannot_start_a_test_of_these_ideas`；`tests/test_marketing_test_package.py::PackageAccessTests::test_another_workspace_can_neither_complete_nor_cancel`；`tests/test_marketing_test_reports.py::ReportScopeTests::test_another_workspace_cannot_preview_or_confirm`；`tests/test_marketing_test_bindings.py::BindingScopeTests::test_another_workspace_can_neither_read_names_nor_confirm`；`tests/test_marketing_test_schedule.py::ScheduleAccessTests::test_another_workspace_can_neither_read_nor_change_a_round_schedule`；`tests/test_marketing_test_progress.py::ProgressAccessTests::test_another_workspace_can_neither_read_nor_record`；`tests/test_marketing_test_history.py::ProductTestHistoryTests::test_another_workspace_gets_not_found_and_reads_nothing`、`::ProductTestHistoryTests::test_a_request_without_a_verified_identity_is_refused`；`tests/test_postgres_rls.py::RowLevelSecurityTests::test_a_round_schedule_change_stays_in_its_workspace_and_is_never_rewritten`、`::RowLevelSecurityTests::test_a_round_deviation_stays_in_its_workspace_and_is_never_rewritten`、`::RowLevelSecurityTests::test_a_test_project_names_ideas_of_its_own_workspace` |
| 12 | 手机宽度可用，英文和简体中文都完整 | 端到端：“拿去测试”、测试记录、“测试进行中”在 375 像素宽时没有横向滚动。本地浏览器走查：同样几个画面和总览在简体中文、手机宽度、暗色下看一遍。前端测试：两套文案的键一一对应、占位符一致、中文不是照抄英文，每个页面加载了它的组件用到的文案 | 通过（本地栈加自动测试）。走查：“拿去测试”（用第 1 步端到端留下的工作区，它没有进行中的测试）、“测试进行中”、测试记录和总览，在简体中文和英文、亮色和暗色、375 和 1280 像素宽下各看一遍，32 种组合都没有横向滚动，主区域都没有内部编号。“这一轮想弄清”下面那句问题和讲法的名字是英文：它们是测试自己的内容，按广告想法的策略语言写（这次第 1 步是在英文界面下开始的），不是界面文案 | `web/__tests__/locales/key-parity.spec.ts`、`en-no-cjk.spec.ts`、`page-namespaces.spec.ts`；端到端 |

## 计划里的五种情况（Review Focus）

| # | 情况 | 结果 | 证据 |
| --- | --- | --- | --- |
| 1 | “拿去测试”画面开着时，在别处取消了一张留下的广告或改了文案 | 通过（自动测试）：开始时 409，页面说“留下的广告有变化”，点刷新再看 | `tests/test_marketing_test_start.py::StartRefusalTests::test_an_ad_no_longer_kept_since_the_quote_is_refused`；`tests/test_marketing_test_quote.py::QuoteHashTests::test_the_hash_covers_every_kept_ad_and_what_it_was_made_from`；`take-to-test.spec.tsx`“kept ads changed since the quote: says so and refreshes on request” |
| 2 | 两个人同时点“开始测试”，或连点 | 通过（自动测试）：只开一个测试、一份检查预留 | `tests/test_marketing_test_start.py::StartTests::test_two_starts_at_once_open_one_test`、`::StartTests::test_the_same_key_replays_the_same_test` |
| 3 | Meta 账户币种不是 USD | 通过（自动测试）：报表按名字说清楚哪里不一样，不计入；币种在默认项里可改 | `tests/test_marketing_test_reports.py::FullReportTests::test_a_report_in_another_currency_is_refused_by_name`、`::FullReportTests::test_timezone_attribution_and_account_differences_are_named`；`test-in-progress.spec.tsx`“says plainly when the report’s currency or attribution is not the test’s, and stops there” |
| 4 | “一起看”的检查跑到一半页面被关掉，或结果不确定 | 通过（自动测试）：回来接着显示进度；不确定的不重发、不生成广告包；同一个命令再发返回同一个包 | `tests/test_marketing_test_package.py::OpenFindingTests::test_a_check_still_running_or_uncertain_is_waited_for_and_never_sent_again`、`::ReplayTests::test_the_same_command_again_returns_the_same_package`；`take-to-test.spec.tsx`“every check passed: makes the package by itself when the page opens again, and downloads it” |
| 5 | 在 Meta 里改了广告名，短键没了；或一个广告名里带两个短键 | 通过（自动测试）：都不预填，列出来让用户选 | `tests/test_marketing_test_bindings.py::SuggestionTests::test_a_renamed_ad_or_a_name_with_two_keys_is_not_prefilled`、`::SuggestionTests::test_a_meta_ad_already_matched_elsewhere_is_listed_for_the_user_to_choose` |

## 看到、这次没改的

- “一起看”的检查本身没跑成（不是有发现，是检查失败了）时，只能取消测试、重新开始，所有检查都要再做一次；没有接上“只重做这一项检查”（T10 记下的）。
- 折叠的默认项里，受众、优惠、版位显示的是广告想法的受众和模板的默认说法，作为输入框的占位文字；报价接口不返回这几项的值（T10）。
- 第一次传报表时问开始日期，用的是整份报表最早的一天，包括不属于这次测试的广告。同一个账户里有更早投放的别的广告时，问的日子会早于这次测试的广告真正开始的那天；用户可以选“不用改，按原定日期”，或在“这一轮的安排”里改成对的日子（T11）。
- 传了报表、还没点“计入这份报表”就刷新页面，这份预览不会接着显示，要重新上传；同一个文件再传得到同一份（T11）。
- “替换了前面报表里的 N 行”和测试记录里每份报表的行数，都把不属于这次测试的广告的行算在内（本地栈：替换 15 行，其中这次测试的广告 10 行；测试记录写 15 行、21 行）。说法没有错，但可能让人以为数字里算了这些行。
- 端到端第一次跑时，第一张广告刚变成“已留下”，马上点第二张的“留下这张”，第二张没有留下（底部仍写留下 1 张），页面没有报错。没有再试着复现，也没有查原因；端到端改成每点一张、等它变成“已留下”再点下一张。这是第 1 步的广告想法页。后来在 #285 查明、由 #286 修好：服务端两次“留下”都会成功，错在页面的显示。连点时，页面会把第一张还没回答的“已留下”抹掉，或者停在一个少了一张的回答上；第二个被拒时，还会连第一张一起退回。现在同一个项目的“留下”按点击顺序一次发一个。端到端里“等它变成‘已留下’”的写法可以保留：那个“已留下”是页面立刻显示的，本来也没在等服务端。
- Controlled Mock 下“一起看”的检查一律“看不出”，所以每次都要写理由；“全部通过、自动生成广告包”要用真实的 Key 才能在栈上看到（第 6 条）。
- 结果里的“还看不出来”：订单不到 20 单时一定是它；够 20 单、每单成本正好等于目标时，评估的区间跨过目标，也是“还看不出来”（T11 在本地看到过）。这是现有评估规则（`judge`）的行为，第 2 步不改。

## 和旧流程的衔接

- **改版前的测试（`plan_first`）不变。** 状态机、接口、返回字段、上传和对应的规则、评估、审核都有“不变”的测试：
  - `tests/test_marketing_test_reports.py::PlanFirstRoundTests::test_plan_first_rounds_keep_their_upload_rules`（`already_imported_ad_day`、`replacement_scope_mismatch` 照旧）、`::PlanFirstRoundTests::test_a_test_round_takes_reports_only_on_the_test_paths`；
  - `tests/test_marketing_test_bindings.py::PlanFirstBindingTests::test_plan_first_rounds_keep_their_binding_rules`；
  - `tests/test_marketing_test_schedule.py::EarlierTestsTests::test_a_round_of_an_earlier_test_is_evaluated_exactly_as_before`；
  - `tests/test_marketing_test_progress.py::EarlierTestTests::test_a_round_of_an_earlier_test_has_neither_and_is_evaluated_exactly_as_before`、`::ChangedAdTests::test_an_earlier_tests_package_is_still_refused_once_an_ad_in_it_changed`；
  - `tests/test_marketing_test_package.py::EarlierTestTests::test_the_earlier_tests_review_and_ready_do_not_change`；
  - `tests/test_marketing_test_start.py::PlanFirstRoundTests::test_a_plan_first_rounds_images_keep_the_creative_library_rule`。
- **测试项目不进旧页面。** 改版前测试的列表不列测试项目，改方案、重新起草、改产品理解、按方案出图、冻结这些接口对测试项目 404（`tests/test_marketing_tests.py::TestProjectRoundPathTests::test_round_paths_accept_a_test_project_and_plan_paths_do_not`）。
- **广告想法（`ads_first`）不变。** 拿去测试不改广告想法项目，它仍是 `ideas_ready`，广告的状态照第 1 步的规则推出来；广告想法的接口没有改。测试开始后广告想法照样能改、能追加，改了的由测试页提示（第 9 条）。
- **测试记录里的改版前测试。** 这个产品的改版前测试列在测试记录的“改版前的测试”，点“在改版前的测试页打开”进旧页面（`web/__tests__/components/tests/test-history.spec.tsx`）。
- **开关关着时什么都不变。** “拿去测试”“测试进行中”“测试记录”都只在开关打开时有（页面带 `adsFirstOnly`）。旧黄金路径用开关关着的构建跑，照常通过。
- **结论还在旧页面。** 产品里的“结论”视图仍链到改版前的测试页，第 3 步换成结论页。第 1 步验收记录 #46–#48（胜出的新开头广告去本地化、放量、新版实验）随结论页在第 3 步定；第 2 步建出的测试按 ADR 0019 走自己的小轮，不经过那几条路径。

## 上线前（维护者）

第 2 步的代码都在开关后面，合并后生产上看不到。打开开关、在生产上验，按下面的顺序，都由维护者做：

1. 确认生产的 API 已经跑到包含 T1–T9 迁移的版本（`alembic current` 是当前 head），两张新表 `marketing_round_schedule_changes`、`marketing_round_deviations` 在生产库上有 RLS 和三种角色的授权（照 `deploy/postgres/domain-tenant-roles.sql`）。
2. 确认“一起看”的检查用的视觉模型和价格表在生产上配好：拿一个有留下广告的产品打开“拿去测试”，按钮上应写“开始测试 · 最多 $X”，X 大于 0；写的是“这个站点还没把设置配好”时先配置。
3. 在网页服务上打开 `NEXT_PUBLIC_MARKETAI_ADS_FIRST`（第 1 步已开的话不用再动）。
4. 用维护者自己的工作区和自己的 Key，走一遍：留下两张核对过的广告 → 拿去测试 → 检查 → 下载广告包，看实际花费不超过按钮上的金额。
5. 在 Meta 用广告包里的广告名建广告（Meta 写入在 MarketAI 里保持关闭，广告由人在 Meta 后台建），投出去以后导出真实报表传回：看列名、币种、时区、归因窗口能不能认出来，短键能不能预填。
6. 到期后看一次结果；测试记录里列出这次测试和每份上传。
7. 生产上的证据、截图和真实报表放私有仓库 `marketai-ops`，不进这个仓库。
