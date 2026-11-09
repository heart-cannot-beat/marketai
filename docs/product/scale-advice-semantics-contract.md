# 放量证据、健康与建议语义契约

状态：Accepted
定案日期：2026-08-26
本次修订：2026-11-09（§20 与验收第 23 条按 ADR 0017 允许 Scale handler 在没有 Meta 写能力的共享 Worker 里运行）
适用范围：v9 放量功能区、Meta 只读指标消费和建议到生成草稿的跨区流程

## 1. 目标与不变量

放量功能回答“一个已验证 Winner 在准确投放上下文中是否仍健康，以及用户下一步值得检查什么”。它不是广告控制器，不拥有 Meta delivery，也不把相关性包装成因果结论。

以下不变量优先于页面便利性和建议数量：

1. Scale 只拥有监控关系、观察段、不可变证据、健康评估、建议和用户处理记录，永远不拥有启停、预算、竞价、受众、版位或广告修改命令。
2. Scale 的分析单位是准确 Winner 与一次具体投放上下文，不存在“素材全局疲劳”或“产品全局可放量”。
3. Winner 只是资格；用户必须显式建立 ScaleEnrollment，系统不能自动把所有 Winner 加入经营监控。
4. 每个 ScaleEpisode 只包含稳定的素材、Offer、市场、Landing Page、投放配置和指标契约；重大变化关闭旧段并重新积累证据。
5. 只有完整、对账通过、同一语义合同且达到成熟门的证据才能产生确定性 Advice；样本不足的 EvidenceMaturity 是 `insufficient`，不是 Learning 或建议。
6. frequency 和 reach 必须针对准确窗口取数，不能把每日非加和指标相加或平均。
7. 疲劳是频次、点击反应和经济结果的复合信号，不能由一个通用 frequency 阈值触发。
8. 所有评估由不可变 ScaleEvidenceSnapshot 驱动，核心 evaluator 是无 I/O、无时钟、可逐字节重放的纯函数。
9. 用户 acknowledge、dismiss 或 convert 只表示处理意图；效果必须由后续素材、实验、配置和指标证据证明。
10. real-read-only、sandbox 和 controlled-mock 从采集到 AdviceOutcome 全链隔离，任何只读连接都不授权写操作。

## 2. 领域对象与所有权

| 对象 | 含义 |
|---|---|
| ScaleEnrollment | 用户建立的 Winner 与具体 Meta delivery 上下文监控关系 |
| ScaleEpisode | Enrollment 内配置稳定的一段不可变观察区间 |
| MetaDeliveryConfigSnapshot | 一次远端 campaign/ad set/ad 配置与安全摘要快照 |
| ScaleMetricContractVersion | daily、window、breakdown 和 delivery 配置查询的语义合同版本 |
| ScalePolicyVersion | 平台发布的算法、证据底线、统计方法、阈值和安全限制 |
| ScaleTargetVersion | Owner 为 ProductVersion/OfferVersion 发布的目标 CPA、保本 ROAS 和更严格门槛 |
| ScaleEvidenceSnapshot | 一次评估使用的不可变指标、配置、数据质量、策略和数据水位 |
| ScaleEvaluation | 纯函数针对一个 EvidenceSnapshot 产生的成熟度、健康、信号和建议候选 |
| ScaleAdvice | 面向用户的一类可处理建议及其稳定 AdviceThread |
| AdviceOccurrence | AdviceThread 在某次证据评估中再次出现或加重的追加记录 |
| AdviceDisposition | 用户对 Advice 的 acknowledge、dismiss 或 convert 追加记录 |
| AdviceSnooze | 与 Disposition 分离的 snooze actor、until、reason 和取消/失效记录 |
| AdviceOutcome | 后续素材、实验、配置或指标对建议实际结果的证据关联 |
| ScaleTestPlanDraft | 不连接 Meta 写侧的人工扩量测试清单 |
| UnobservedFactor | API 未观测但可能影响解释的明确盲区和人工巡检记录 |

Scale 不拥有 AssetVersion、WinnerDecision、OfferVersion、Experiment、Meta 远端对象、GenerationRun 或广告预算。它只引用这些对象的稳定版本和证据。

## 3. Enrollment 资格与建立

### 3.1 资格门

建立 ScaleEnrollment 时必须同时满足：

- 来源是具体 AssetVersion，当前人工质量为 Approved；
- 存在用户已确认、未撤销且数据充分的 WinnerDecision；
- Winner 的市场、版位、Offer、Landing Page 和归因合同与待绑定 delivery 上下文一致；
- ProductVersion、OfferVersion、MarketAssetBinding、Reference 权利和其他上游资格仍有效；
- 本地化素材不存在 `stale_truth`，商业事实和目标市场资格未被阻断；
- Meta account、campaign、ad set 和 ad 属于当前 Workspace 可访问的只读连接；
- ad 到 AssetVersion 的绑定唯一可对账，不接受无法确定素材归属的动态创意或轮播混合；
- evidence mode 与所有上游证据一致。

相同 AssetVersion 可以在不同市场、Offer、Landing Page、受众或 delivery 对象下建立多个 Enrollment。相同 Workspace/account/ad 同一时段只能属于一个 active 或 paused Enrollment，防止重复计算和冲突建议；paused 继续保留绑定，closed 才释放。

### 3.2 显式用户意图

Owner 或 Member 从合格 Winner 列表选择远端绑定并创建 Draft Enrollment，预览完整上下文和数据可用性后确认激活。系统发现可绑定广告时只能提示候选，不自动激活。

真实客户写侧在 v9 仍未授权；v9 的 real 模式仅用于内部验证账户且只需要读权限。客户自助黄金路径使用 Sandbox 或 Mock，不得形成真实经营建议。

## 4. ScaleEpisode 与投放配置

### 4.1 Episode 身份

ScaleEpisode 冻结：

- Enrollment、WinnerDecision、AssetVersion；
- ProductVersion、OfferVersion、市场和 Landing Page；
- account/campaign/ad set/ad 标识；
- MetaDeliveryConfigSnapshot；
- ScaleMetricContractVersion 与 delivery comparability contract；
- evidence mode、账户时区和币种；
- started_at、配置基线和来源证据 hash。

Episode 不等于广告一生。系统按远端配置快照把一段不可比较的 delivery 拆成新的 Episode，避免把调整前后的数据混为一体。
ScalePolicyVersion 与 ScaleTargetVersion 不冻结在 Episode；每次 Evaluation
在 EvidenceSnapshot 中冻结其准确版本，因此语义重评不改写 Episode。
Episode 冻结 Scale metric 的语义合同，但不永久冻结一个底层 #23 observation-contract generation。每个 EvidenceSnapshot 冻结实际使用的 generation；只有声明语义兼容的 generation 才能留在同一 Episode，并推进 data continuity、按 Policy 重置相关 streak。metric 含义、归因、时区、币种、account 或 evidence mode 不兼容时必须关闭 Episode。

### 4.2 MetaDeliveryConfigSnapshot

快照至少覆盖 ad creative ID、campaign/ad set/ad 身份与状态、预算来源和金额、optimization goal、bid strategy、attribution setting、placement/targeting 安全 hash、Offer/LP 映射、observed_at、读取合同和响应证据 hash。它分别保存覆盖完整合同观察的 `evidence_hash` 与只覆盖必然破坏 Episode 可比性字段的 `comparability_hash`；预算相对基线按数值阈值独立判断，不能因为任意小额预算变化改变完整 evidence hash 就提前重开 Episode。

完整 targeting 只保存在受控原始证据边界；结构化日志、普通 UI 和导出仅显示必要摘要与 hash。名称或 updated_time 变化本身不能悄悄解释为业务变化；无法安全识别差异时使用 `unexplained_delivery_config_change` 并保守重开 Episode。

### 4.3 关闭与重开

以下可比性变化关闭旧 Episode，并在 Enrollment 仍合格且启用时用新基线建立新段：

- AssetVersion、ad creative ID、OfferVersion 或 Landing Page；
- 市场、受众、placement；
- optimization goal、bid strategy、attribution setting；
- 预算相对 Episode 基线累计变化达到或超过 20%；
- 无法解释的 delivery 配置 hash 变化；

以下条件不自动建立新 Episode：

- 上游 Approved、Winner、Truth 或权利资格被撤销时，旧 Episode 与 Enrollment 一起终止，释放远端 binding reservation；恢复资格后必须从新的合格候选显式建立 Enrollment；
- 用户主动关闭 Enrollment 时，旧 Episode 与 Enrollment 一起终止并释放 binding reservation；
- 连续 7 个完整日无 delivery 时，旧 Episode 关闭，Enrollment 由系统转为 `paused` 并继续保留 binding；用户显式 `resume` 且通过新鲜配置和全部资格门后创建新 Episode。

远端暂停和零 delivery 日不计入活跃成熟度，也不被当成 CTR/CPA 下降。相同配置在 7 日内恢复可以延续 Episode，但窗口必须重新满足样本门；连续 7 个完整日无 delivery 时关闭 Episode、把 Enrollment 转为 `paused` 并保留 binding，后续显式恢复从新 Episode 重新积累证据。

## 5. 状态维度

不得使用一个 status 同时表达监控、数据和建议：

- EnrollmentLifecycle：`draft`、`active`、`paused`、`closed`；
- EpisodePhase：`collecting`、`evaluating`、`closed`；
- EvidenceMaturity：`warming`、`monitorable`、`economics_ready`、`insufficient`；
- DataQualityState：`pass`、`warning`、`blocked`；
- ScaleHealth：`healthy`、`fatigue_watch`、`creative_fatigue`、`economics_decline`、`delivery_anomaly`、`not_delivering`。

这些状态来自追加式事件和当前投影。DataQuality blocked 时不能同时展示确定性 healthy 或 fatigue。

## 6. Evidence mode 与指标合同

### 6.1 三种模式

- `internal_validation`：v9 仅来自平台验证 Workspace 内部账户的真实只读数据，可以形成该隔离 Workspace 内的真实建议；未来客户真实只读必须通过独立准入，不因连接存在而自动开放；
- `customer_sandbox`：客户 Meta Sandbox 固定测试路径，只能形成 Sandbox Advice；
- `controlled_mock`：确定性 Adapter 场景，只能形成 Mock Advice。

mode 进入 Enrollment、Episode、Metric Contract、EvidenceSnapshot、Evaluation、Advice、Draft 和所有防重指纹。跨 mode 复用或转换被禁止；创建真实版本必须重新绑定、取数和评估。

### 6.2 ScaleMetricContractVersion

每份合同冻结 API 版本、level、fields、time_increment、action report time、7d_click + 1d_view 归因窗口、purchase action type、账户时区、币种、查询类型、breakdown、分页、解析规则和 contract hash。

Scale 使用四类独立合同：

1. ad-level daily contract；
2. exact-window reach/frequency contract；
3. 每种 breakdown 的独立 contract；
4. delivery configuration contract。

API 不保证所有 field/breakdown 组合可用。新合同必须通过目标 API 版本的 contract test 和只读 canary 后发布，不能在运行时删字段后静默继续。

### 6.3 非加和指标

spend、impressions、link clicks、purchases 和 purchase value 可从同语义 daily 行聚合。reach、frequency 和其他去重指标必须为准确 7 日/14 日窗口单独查询；禁止求和或平均 daily frequency/reach。

## 7. ScaleEvidenceSnapshot

一次快照至少保存：

- Episode 和所有冻结上游版本；
- current 7d、baseline 7d 和 14d 窗口边界；
- daily 可加和指标与 exact-window reach/frequency；
- placement、country 及描述性 age/gender breakdown 证据；
- spend、impressions、clicks、CTR、CVR、CPC、CPM、purchases、purchase value、CPA、ROAS 的 Decimal 结果；CVR 由 purchases/link clicks 确定性派生，分母为零时保持 unavailable；
- MetaDeliveryConfigSnapshot 与状态历史；
- ScaleMetricContractVersion、ScalePolicyVersion、ScaleTargetVersion；
- ingestion/page/reconcile 证据、数据水位、as_of、raw hashes；
- 由 durable evaluation trigger 冻结且带时区的 freshness reference time；retry 不得改用新的 ambient clock；
- Evidence mode、币种、时区、归因和全部 DataQualityIssue；
- 封存前选定的 prior Evaluation/StreakState，或没有合格 predecessor 的 typed absence reason；
- canonical snapshot hash。

Money 使用 Decimal 和账户 ISO 4217 币种。跨币种 Episode 不能聚合；Scale 不在建议中进行汇率换算。

## 8. 数据质量门禁

Scale 评估前必须证明：

- 相关 ingestion 的全部分页完成且原始证据可定位；
- 最近成功同步距评估不超过 36 小时；
- daily、window、breakdown 和 delivery contract 与 Episode 完全匹配；
- account、ad binding、attribution setting、时区和币种唯一一致；
- reconcile 无未解释的远端/本地缺失；
- 窗口没有重复行或无法由状态历史解释的缺口；
- 数据值非负，clicks 不超过 impressions，分母为零时不伪造比率；
- `internal_validation`、`customer_sandbox`、`controlled_mock` 不混用；
- Episode 上游资格仍有效。

阻断问题只产生所在证据阶段的 typed quality issue 和 EvidenceMaturity `insufficient`，不产生 ScaleAdvice。发生在可评估 source manifest 之前的分页、传输或未决 reconcile 故障，必须形成不可变 blocked source manifest 和当前 evidence-readiness 投影；它不伪造 EvidenceSnapshot 或 Evaluation，但必须显示相同的 `insufficient`、阻断原因和恢复动作。warning 可以展示描述性数据，但是否允许某类评估由 ScalePolicyVersion 明确决定。

## 9. 成熟度与窗口

Beta 默认 Platform ScalePolicyVersion 使用广告账户本地完整日，排除当前未完成日，并比较最近 7 个完整日与此前 7 个完整日。

### 9.1 Monitorable

Episode 至少需要：

- 14 个完整账户本地日；
- 其中至少 10 日有实际 delivery；
- current 与 baseline 窗口各至少 1,000 impressions；
- 各至少 20 link clicks；
- DataQualityState 不是 blocked。

不足时为 warming 或 insufficient，不产生确定性经营 Advice。

### 9.2 Economics ready

涉及 CPA、ROAS、经济恶化或扩量资格时，每个 7 日窗口还必须：

- 至少 5 次 purchase；
- 若有 target CPA，spend 至少为其 3 倍；
- 若只有 ROAS 目标，ScaleTargetVersion 必须提供不低于平台底线的 minimum economic spend；
- purchase action type、归因合同和 Offer 与 Episode 一致。

平台门槛是最低值；Owner 只能通过 ScaleTargetVersion 提高，不能降低。

## 10. 策略与经济目标

### 10.1 ScalePolicyVersion

平台发布不可变版本，冻结算法版本、证据底线、窗口、相对变化阈值、统计方法、连续命中次数、breakdown 门禁、多重比较方法、通知冷却和安全限制。修改必须创建新版本，旧 Evidence/Evaluation 不重新解释。

### 10.2 ScaleTargetVersion

Owner 为一个 ProductVersion/OfferVersion/市场上下文发布不可变目标，至少可以包含 target CPA、break-even ROAS、minimum economic spend 和比平台更严格的信号门槛。发布和撤销只限 Owner，要求二次确认、影响分析和审计；MFA 只在登录时验证（ADR 0015）。

没有 Owner 发布的适用经济目标时可以评估 delivery/CTR 健康，但不能产生 `consider_scale_test` 或明确经济优劣 Advice。

## 11. 纯函数评估

唯一计算接口为：

`evaluate_scale(ScaleEvidenceSnapshot) -> ScaleEvaluation`

Evaluator 不读取数据库、网络、环境变量或当前时间。相同 canonical snapshot 必须产生逐字节一致的 JSON-safe 结果，至少包含 algorithm version、成熟度、数据质量、ScaleHealth、每个 Signal 的 expected/observed/interval/threshold/reason code、Advice candidate 和 result hash。

新 EvaluationRun 只能选择部署包的 `CURRENT_ALGORITHM_VERSION`；已封存 Snapshot 和未终止 Run 通过只读 evaluator registry 使用其 Policy 冻结的旧实现重放。只要仍有可重放记录引用某版本就不得移除该实现，也不得在 retry 时静默改用新算法。

CTR 比较使用版本化两比例方法；Beta 默认先分别计算两个 95% Wilson score 区间，再使用 Newcombe method 10 得到差值区间，并要求差值下界严格大于零且相对下降达到阈值。不得以两个 Wilson 区间是否重叠替代差值区间判定。CPA/ROAS 使用与冻结输入绑定的确定性 bootstrap。所有随机种子从 snapshot hash 和 signal identity 派生。

持久化、AdviceThread 更新、通知和草稿创建在纯函数外执行，并使用结果 hash 防重。

## 12. ScaleHealth 与疲劳

### 12.1 Beta 复合疲劳规则

`creative_fatigue` 必须同时满足：

- current 7d exact-window frequency 不低于 3.5；
- frequency 相比 baseline 7d 增长至少 20%；
- link CTR 相对下降至少 25%，且 95% 区间支持下降；
- CPA 相对上升至少 20%或 ROAS 相对下降至少 20%，并通过 economics-ready 与 bootstrap 门；
- 连续两次账户完整日评估命中。

第一次命中进入 `fatigue_watch`。frequency + CTR 命中但经济样本不足时也只能 watch；单独 frequency 高不判疲劳。数据中断、Episode 变化或信号恢复会重置连续计数。

### 12.2 原因分流

- frequency↑ + CTR↓ + CPA/ROAS 恶化：`creative_fatigue`；
- CTR 稳定但 CVR、CPA 或 ROAS 恶化：`economics_decline`，检查 Offer、LP、结账或归因；
- CPM 上升或 delivery 下降但 CTR/CVR 稳定：`delivery_anomaly`；
- 指标方向冲突：继续收集，不创建确定性 Advice；
- 数据质量失败：insufficient，不评健康。

系统只能说明“证据符合规则”，不能声称素材、受众或分段导致绩效变化。

## 13. Breakdown 与钱流向

placement、country、age+gender 分别使用独立查询合同，不能假设它们能任意组合。age/gender 只做隐私安全的描述性展示，不生成排除或加码建议。

placement 或 country 只有同时满足以下 Beta 门槛时才能形成 `review_delivery_mix`：

- segment 至少占窗口 spend 的 15%；
- 至少 1,000 impressions、20 link clicks 和 5 purchases；
- CPA/ROAS 区间明确越过适用 ScaleTargetVersion；
- 通过同批 segment 的多重比较校正，Beta 使用 Benjamini-Hochberg FDR 10%；
- 连续两次评估命中。

不满足门槛的单元只展示允许的钱流占比并附 insufficient reason。API 隐私抑制单元使用带 typed reason 的占位记录，禁止保存、哈希、返回或导出被抑制的指标值；它不能被静默省略成“零数据”。建议文案只能说“检查该分段”，不得声称因果、要求排除或生成 targeting 命令。

## 14. 扩量资格

`consider_scale_test` 只在以下条件全部满足时产生：

- Episode 为 economics_ready；
- 连续两次完整日评估为 healthy；
- 若配置 target CPA，其 CPA 95% 上界不高于目标；
- 若配置 break-even ROAS，其 ROAS 95% 下界不低于目标；
- 两个目标同时存在时全部通过；
- 无 fatigue/watch、delivery anomaly、DataQualityIssue 或配置漂移；
- 至少存在一个 Owner 发布的经济目标；
- Meta 明示的 DeliveryDiagnostic 不处于 learning limited 或其他阻断状态。

Advice 只说明“符合人工受控扩量测试资格”，展示稳定性、经济区间和盲区；不提供通用预算倍数，不写 Meta。

## 15. Advice 类型、线程与状态

### 15.1 五类 Advice

v9 只支持：

- `refresh_creative`；
- `review_offer_or_landing_page`；
- `review_delivery_mix`；
- `investigate_delivery_anomaly`；
- `consider_scale_test`。

healthy、warming、not_delivering 和 insufficient 属于 ScaleHealth/成熟度，不创建“继续观察”Advice 卡。

### 15.2 AdviceThread

相同 Episode、Advice type 和稳定 reason signature 在 active 期间复用一个 AdviceThread。每次再次命中保存不可变 AdviceOccurrence，引用 Evidence/Evaluation，不覆盖原始证据。resolved 或 expired 后再次发生会创建带 predecessor link 的新 thread generation，不把终态 Thread 重新激活；superseded Thread 对旧语义永久终止。

状态维度分离：

- AdviceLifecycle：`active`、`resolved`、`superseded`、`expired`；
- UserDisposition：`unreviewed`、`acknowledged`、`dismissed`、`converted_to_draft`；
- NotificationDecision：`send` 或带原因和截止时间的 `suppressed`；
- NotificationDelivery：只为 `send` decision 建立 transactional outbox，并在 `pending`、`delivering`、`delivered`、`failed`、`dead_letter` 间推进；
- Snooze：独立保存 actor、until 和 reason。

只在确认 Advice 的首次出现或严重程度提升时通知。`fatigue_watch` 只是 Health，不创建 AdviceCandidate 或通知；后来确认的 fatigue Advice 是新 Thread 的首次 occurrence。Dismiss 默认冷却 7 日，除非证据显著恶化；冷却抑制通知但不删除 Occurrence。

连续两次合格、顺序且不同账户本地日的实时评估不再命中时 lifecycle 进入 resolved；warming、blocked、historical backfill 与 intermediate replay 不计 recovery miss。Episode 关闭，或 Policy、Target、algorithm、metric-contract identity 改变时，旧 Advice superseded；证据超过 freshness SLO 时 expired。用户不能手工把系统问题标成 resolved。

## 16. 建议出口

### 16.1 Refresh creative

`refresh_creative` 可以幂等转换为 GenerationDraft：

- 固定原 Winner 的 ProductVersion、OfferVersion、市场、Hook/Angle、must-match 和安全规则来源；
- 默认保留获胜 DimensionConceptKey；
- 用户显式选择新 Seed、构图、人物或其他 refresh axis；
- 默认建议 3 张，最终数量受 Plan、Provider、Workspace 容量和成本 Quote 约束；
- 保存 Advice、Occurrence、Evidence、Winner 和原 GenerationRecipeSnapshot 谱系。

转换不创建 Reservation、不调用 Provider。用户必须重新 ResolutionPreview、确认成本并提交；新 AssetVersion 回到 QC pending，Approved 后还要完成新实验，不能自动替换真实广告或继承 Winner。

### 16.2 Consider scale test

`consider_scale_test` 可以创建不可执行的 ScaleTestPlanDraft，包含当前基线、成功/停止指标、风险、人工 Ads Manager 清单、观察期和用户自行填写的计划变化。生成的基线、标准与谱系不可修改，只有 planned changes 和 observation notes 使用 CAS revision 编辑。它不是 Meta 命令，不自动生成预算数值。

用户在 Ads Manager 完成任何显著变化后，新的 MetaDeliveryConfigSnapshot 会关闭旧 Episode 并建立新 Episode。Scale 不把人工声称的“已修改”当远端事实。

其他 Advice 可以深链到 Offer/LP、实验或 delivery 证据页面，但不自动修改 LibraryVersion、实验、Meta 配置或商业事实。

## 17. AdviceOutcome

UserDisposition 只表示意图，不是效果。AdviceOutcome 必须引用后续可验证证据，例如：

- GenerationDraft → GenerationRun → 新 AssetVersion → 人工 Approved → 新 ExperimentDecision；
- 远端人工修改 → 新 MetaDeliveryConfigSnapshot → 新 ScaleEpisode；
- 后续 ScaleEvidenceSnapshot 显示健康恢复或经济改善；
- 用户没有执行且信号自行恢复的证据。

Outcome 保存关系和证据，不把相关性升级成因果结论。acknowledged、dismissed 或备注不能直接创建 positive/negative Outcome。

## 18. 调度、回补与防重

每个请求的 source revision 都形成不可变 terminal source manifest：`complete` 表示合同、分页和 reconcile 完整，可用于封存；`blocked` 只保存失败谱系、DataQualityIssue 和恢复动作，不得创建 Snapshot/Evaluation。实时 Scale Evaluation 只在广告账户本地日结束、daily、exact-window、全部声明的 breakdown 与 config 所需 Meta ingestion 形成 `complete` source manifest 后调度。数据水位必须指向最后一个完整日。Policy/Target 语义变更、Meta backfill/correction 和 deterministic forward replay 使用独立事件触发，不冒充实时完整日 ingestion。

Evaluation 指纹至少覆盖 Workspace、evidence mode、Episode、EvidenceSnapshot canonical hash、Metric Contract set、PolicyVersion、TargetVersion 或显式 no-target marker，以及 algorithm version；nullable Target 不能留下数据库唯一性漏洞：

- 相同指纹返回原 Evaluation；
- Meta 回补或修订创建新 ScaleEvidenceSnapshot 和 Evaluation；
- historical backfill 不能直接倒退当前 projection；需要影响当前 streak 时，按账户本地日创建新 lineage generation 并顺序 forward replay；中间 replay 不更新 Advice、不发送历史通知，只有最终 head 可以通过 CAS 更新当前 projection；
- Policy/Target 新版本产生新评估并 supersede 不再适用的 Advice，不改写旧结果；
- Worker kill/restart 通过持久化 Job/租约恢复，不重复 Snapshot、Evaluation 或通知。

一次 durable ScaleEvaluationRun 在封存前固定 source-manifest、Policy、Target/no-target、algorithm 与 trigger kind，并保存选中的 snapshot_id 作为恢复 checkpoint。编排分为短事务封存、无开放事务的纯函数计算、短事务持久化与投影三段；不能用一个长事务同时声称支持“snapshot 已提交但 Evaluation 尚未提交”的恢复语义。

## 19. 盲区与 DeliveryDiagnostic

拍卖重叠、受众饱和、首次展示比率和其他只能在 Meta Inspect 或 UI 获取的因素保存为 UnobservedFactor。页面提供带时间戳的人工巡检清单和可选备注/截图引用，但不估算、不伪造，也不进入自动 evaluator。

learning stage 等 DeliveryDiagnostic 只有 Meta 读侧明确返回时才保存，并记录字段合同和 observed_at。已知 `learning_limited` 阻止 `consider_scale_test`，但不自动判 creative fatigue；字段不可用时为 unknown，不按经验猜测。

## 20. 上游撤销与安全边界

Approved、Winner、Offer、Truth、MarketAssetBinding、Reference 权利或 Workspace 资格被 Withdraw 后：

- 停止新的 Scale 评估；
- 关闭当前 Episode；
- 把 Enrollment 终止为 `closed` 并释放远端 binding reservation，恢复资格后也不自动重开；
- active Advice lifecycle 进入 `superseded`，关联动作的 ActionEligibility 进入 `blocked`；
- 阻止从 Advice 创建新 GenerationDraft、ScaleTestPlanDraft、实验或 Launch；
- 保留历史 Evidence、Evaluation、Advice 和审计；
- 向用户发出高优先级人工处理告警。

上游拥有模块必须在 Withdraw 同一事务写入带 semantic event ID 的 durable event/outbox；Scale 通过幂等 inbox 或同事务 in-process handler 消费。仅做 best-effort callback 或依赖轮询都不满足撤销安全语义。每次 Evaluation 和 Advice conversion 仍在事务内重查资格，以封住事件消费竞态。

Scale 服务和 API 不加载 Meta 写 Token，不暴露 remote mutation route；Scale 代码不复用、也到不了 Launch 写侧客户端。Scale 永远不自动暂停、启用、删除或修改真实广告/预算。

Scale handler 可以运行在共享 Worker 里（[ADR 0017](../adr/0017-scale-handlers-may-share-a-worker-without-meta-write-capability.md)），前提是这个进程同时满足：

- 没有 Meta 写端口和写侧客户端：composition root 不注入写端口，按进程配置也构造不出写侧客户端；
- 没有 Launch dispatch：进程里没有能到达真实 Meta 写客户端的 Launch Job 路径；
- 没有 `META_WRITE_ACCESS_TOKEN`：进程环境和配置里的写 Token 都为空。

共享 Worker 启动时检查这三条，任一条不满足就拒绝启动；守护测试在 CI 里证明这项检查有效。任一条不再成立时，必须先拆出独立的 Scale Worker（不含写环境变量、Launch dispatch 和写侧数据库权限的独立 composition root），再改动共享 Worker。

## 21. 权限、API 与审计

Owner 和 Member 都能创建/激活/暂停/恢复/关闭 Enrollment、查看证据、acknowledge/dismiss/snooze Advice、转换 GenerationDraft 和创建 ScaleTestPlanDraft。只有 Owner 能发布或 Withdraw ScaleTargetVersion。

所有命令要求 TenantContext、RLS、expected revision、Idempotency-Key 和双 Actor 审计。自动评估记录系统审计主体、Enrollment 创建者和触发 SyncRun；它不能冒充人类操作者。

API 至少提供 Enrollment/Episode/Health/Evidence/Advice 查询，以及显式 create/activate/pause/resume/close/acknowledge/dismiss/snooze/convert 命令。任何请求体都不能携带可执行 Meta budget、status、targeting 或 bid mutation；ScaleTestPlan 中描述人工意图的非执行文本不得映射为 adapter 请求。

## 22. UI 与导出

每张 Enrollment 卡必须来自持久化数据并展示：

- 精确 AssetVersion、Winner、Offer、市场和远端 binding；
- evidence mode、Episode、成熟度、数据水位和账户时区；
- current 7d 与 baseline 7d 的绝对指标、变化、样本和置信区间；
- exact-window frequency/reach；
- DataQualityIssue、UnobservedFactor 和缺失原因；
- ScaleHealth、Advice reason code、Policy/Target/Contract 版本；
- Advice 时间线、用户处理、关联 Draft 和 Outcome；
- 固定文案：“MarketAI 不会修改广告或预算”。

页面必须覆盖 empty、warming、collecting、healthy、watch、fatigue、economics decline、delivery anomaly、not delivering、stale/blocked、Advice dismissed/snoozed/resolved 等状态。不得展示演示 seed、固定 sparkline 或“二期占位”。

breakdown 页展示 placement、country 和经抑制的 age/gender 钱流，但不得渲染因果或自动排除按钮。导出只包含聚合证据、规则/目标版本和审计引用，不包含 Token、完整 targeting、用户级数据、签名 URL 或敏感 Meta 原始响应。

## 23. 受控 Mock 与客户 Sandbox

`controlled_mock`/`customer_sandbox` 只替换 Meta 外部数据与配置 Adapter；Enrollment、Episode、EvidenceSnapshot、数据质量、纯函数评估、AdviceThread、Disposition、Draft 转换、调度、防重和审计走真实持久化路径。

Controlled-Mock scenario、seed、mode 和 contract 进入证据及指纹。`controlled_mock`/`customer_sandbox` 的真实经营 Advice 资格永远为 false；它们只能形成同 evidence mode 的非生产 Advice 和测试 Outcome，用于验证完整持久化链路。两种模式的指标不得进入或晋升为 `internal_validation`/真实经营的 Winner、Pattern、Scale 汇总、Advice 或 AdviceOutcome，也不得与其他 mode 的后续证据建立 Outcome。

## 24. 实现验收门

本功能只有在以下自动化证据存在时才算实现：

1. 只有用户显式选择且通过 Approved、Winner、Offer、市场、绑定和 mode 门禁的对象能激活 Enrollment。
2. 相同素材不同市场/Offer/delivery 形成独立 Enrollment；一个 ad 不能同时重复归属 active 或 paused Enrollment。
3. DeliveryConfig 的素材、Offer、受众、版位、归因、竞价或累计 20% 预算变化会关闭旧 Episode 并重开。
4. 暂停和零 delivery 不被当成性能下降；7 日无 delivery 关闭 Episode、系统暂停 Enrollment、保留当前绑定，显式恢复时创建新 Episode。
5. daily、exact-window、breakdown 和 config 使用独立版本合同，字段不兼容 fail closed。
6. 7d/14d frequency 和 reach 来自准确窗口，不求和或平均 daily 值。
7. 分页、freshness、contract、时区、币种、binding、gap、reconcile 和 mode 任一阻断问题都只产生所在阶段的 typed quality issue/`insufficient` 投影，不产生 Advice；封存前故障只形成 blocked manifest，不形成 Snapshot/Evaluation。
8. 14 日/10 delivery day、impression、click、purchase 和 spend 成熟门按 PolicyVersion 可重放。
9. 相同 ScaleEvidenceSnapshot 由纯函数产生逐字节相同 Evaluation；数据库、时钟或环境变化不影响结果。
10. fatigue 只有在 frequency、CTR、经济指标和连续两次评估全部满足时成立；单指标不误报。
11. CTR 稳定而 CPA/ROAS 恶化不会错误生成 refresh creative；CPM/delivery 异常单独分流。
12. placement/country 建议满足占比、样本、经济区间、多重比较和连续命中；age/gender 不产生 targeting Advice。
13. consider scale 同时满足成熟、连续健康、目标置信区间、配置稳定、质量和 DeliveryDiagnostic 门禁。
14. 五类 Advice、Thread/Occurrence、Lifecycle/Disposition/Notification/Snooze 分离且重复评估不制造卡片。
15. dismissed 冷却、严重恶化唤醒、连续恢复 resolved、Episode/Policy 变化 superseded 和证据过期 expired 正确。
16. refresh creative 转换只创建未付费 GenerationDraft；重新预览、成本确认、QC 和实验不可跳过。
17. ScaleTestPlanDraft 不能调用 Meta 或自动填通用预算倍数；人工远端修改由新配置证据和 Episode 证明。
18. AdviceOutcome 只能引用后续可验证链路，acknowledge/dismiss 不能伪造结果。
19. terminal blocked source manifest 能显示 `insufficient`、阻断原因和恢复动作但不伪造 Evaluation；complete source manifest 后调度、补数、Policy/Target 重评、顺序 forward replay 和 Worker 重启均不倒退 projection，也不重复 Evaluation 或通知。
20. UnobservedFactor 与 unknown DeliveryDiagnostic 不被估算；已知 learning limited 阻止扩量但不冒充疲劳。
21. 上游资格撤销关闭 Episode 与 Enrollment、释放当前绑定、阻止新动作并保留历史；资格恢复后必须新建 Enrollment，且系统不自动修改真实广告。
22. Owner/Member、ScaleTarget、跨 Workspace、对象下载、导出脱敏和 RLS 负向测试通过。
23. Scale API 和运行 Scale handler 的 Worker 无 Meta mutation route，Scale 代码不可传递到 Launch job，也不读取或传递写 Token；共享 Worker 满足 §20 的三条条件，独立 Scale Worker 另外无写侧数据库授权；AST、transitive import closure、部署环境、启动断言与运行 spy 共同证明零远端写能力。
24. `customer_sandbox`/`controlled_mock` 完整跑通领域路径，只能形成同 mode 的非生产 Advice/测试 Outcome，不能进入或晋升为 `internal_validation`/真实经营的 Advice、Pattern、汇总或 Outcome。
25. Playwright 从已确认 Winner 显式建立 Enrollment，经历 warming、数据质量阻断、健康、疲劳 Advice、dismiss/snooze、GenerationDraft 转换和后续 Outcome 链，刷新及重启后状态一致。

## 25. 参考资料

- [Meta 官方 Facebook Marketing API Postman 集合：Insights ad-level、daily、frequency/reach 与 attribution 参数](https://www.postman.com/meta/facebook-marketing-api/request/5wdl62t/attributionsetting)

## 26. 明确不做

- 不自动启停、删除广告或修改预算、bid、targeting、placement 和 creative。
- 不把 Winner、AssetVersion 或广告名称当成全局 Scale 身份。
- 不平均 daily frequency/reach，不混用 metric contract、归因、币种或 evidence mode。
- 不把一个阈值、一天波动或样本不足包装成 fatigue 或 scale advice。
- 不把 breakdown 相关性写成因果，不生成人口属性排除建议。
- 不用 acknowledge、dismiss、备注或人工截图伪造 AdviceOutcome。
- 不估算 Meta Inspect 盲区或缺失的 DeliveryDiagnostic。
- 不在 v9 做自动预算优化、预测 LTV、MMM、跨客户经营学习、真实客户 Meta 写入或完整广告运维平台。
