# v9 六区功能对齐验收合同

- 状态：历史验收合同；2026-10-16 起测试组织与主流程部分由新规格覆盖
- 定案日期：2026-08-25
- 决策任务：[GitHub #4](https://github.com/Raybbish/marketai/issues/4)
- 适用里程碑：邀请制自助 SaaS Beta 的 v9 功能对齐
- 性质：产品与工程验收合同，不代表功能已经实现，也不代表 Gate 3 通过
- 2026-10-10 补充：前台主流程由 [前台两项配置合同](front-flow-two-config-contract.md) 覆盖本合同 §3.1 的用户行为（由系统代做、用户确认）；六区降为「高级工作台」，§2 不变量与其余条款不变（ADR 0011）。

> 2026-10-16 修订边界：[AI 营销测试改版规格](ai-marketing-testing-spec.md) 与 [ADR 0010](../adr/0010-organize-marketing-tests-around-questions-and-scoped-evidence.md) 优先于本文中强制六区主流程、默认全组合/覆盖采样、按 Loser 标签跳过、每概念仅一候选、一候选一素材/一 AdSet/一 Ad，以及以单一 verdict 解释全部结果的约定。当前客户投放采用 Meta 人工交接和回传，不以 Launch 为前提；共享持久化、租户隔离、版本、成本、恢复及来源隔离要求继续有效。下文保留历史内容，不应据此新建冲突任务或声称新版已实现。

## 1. 目的与完成定义

本合同定义新建生成、素材、投放实验、本地化、放量和创意库六个 v9 功能区在什么条件下才算完成。页面存在、按钮可点击、API 单独返回成功或预置演示数据能够播放，都不能单独证明功能对齐。

单个功能区只有同时满足以下五类要求才算通过：

1. 用户可观察行为与 v9 手册的主要业务动作一致；
2. 业务状态、进度、成本和产物来自持久化领域真值；
3. 刷新、重试、API 重启和 Worker 中断后具有明确恢复行为；
4. 与其他功能区通过稳定 ID、版本和快照形成真实联动；
5. 外部服务使用受控 Mock 时，页面、运行记录和产物都有不可混淆的来源证据。

整个里程碑只有在六区分别通过，且一个已登录用户从空 Workspace 通过 SaaS UI 在 PostgreSQL 上完成跨区黄金路径后才算通过。演示版的信息架构、主要交互和恢复动作具有约束力；教学文案、演示故障道具和像素级布局不具有约束力。本合同中的明确决策优先于旧手册；其中最重要的覆盖项是“放量”必须成为真实建议功能，不能继续保留为二期占位。

## 2. 六区共享不变量

### 2.1 持久化状态

- 浏览器状态只能用于缓存和展示，不能决定业务真值。
- 每个适用功能必须表现空、加载、排队、运行、部分成功、成功、失败、可重试、权限不足、额度或成本受限状态。
- 页面刷新后必须从 API 重建状态；API 或 Worker 重启后不得把运行中任务错误显示为成功或永久丢失。
- 导航计数、筛选项和摘要必须从当前 Workspace 的领域数据计算，不能写死或从演示种子读取。

### 2.2 受控 Mock

- 只有外部网络或付费 Adapter 可以被替换：AI Provider、图片编辑或视觉 QC、Meta、Shopify 等。
- 领域实体、状态机、任务队列、幂等、防重、成本、审计、权限和持久化必须执行真实路径。
- 页面、运行记录、任务项、产物和导出 Manifest 必须记录运行模式、Provider、模型或 API 版本、模拟场景和关联 ID。
- Mock 指标和真实指标必须分区保存；Mock 结果不得进入真实 Winner、Pattern、放量证据或真实成本汇总。
- 受控 Mock 的真实应付金额为 0；可以保存独立的模拟成本估算，但不能把它记为真实支出。

### 2.3 Workspace、角色与审计

- 所有查询、命令、任务、对象引用和导出都必须绑定服务端验证后的 Workspace 身份。
- Owner 和 Member 均可操作六个功能区；Member 只能在 Owner 设置的硬上限内产生 BYOK 成本，不能管理成员、订阅、凭据、Meta 连接、预算上限或真实写入能力。
- UI 隐藏不是授权控制。跨 Workspace ID、父子对象拼接和导出请求必须在 API 和 PostgreSQL RLS 层被拒绝。
- 后台动作同时保留系统审计主体和原始发起人；平台运营人员默认无权读取客户领域数据或凭据。
- 成员撤销、Workspace 停用或授权租约过期后，不得开始新的外部或付费副作用。

### 2.4 成本、币种与重放

- 所有金额使用 Decimal 和明确币种。
- 外部付费调用前必须原子预留成本；完成后结算，未调用或明确失败后释放。
- 相同幂等键或请求指纹不得重复预留、调用或结算。
- 处于 uncertain 的外部调用必须先对账，不能盲目重试。
- 页面必须在用户确认付费动作前显示估算依据、币种和硬上限结果。

### 2.5 素材双状态

- 人工素材质量属于具体 AssetVersion，只能是 Approved、Needs Changes 或 Rejected。
- Winner、Learning 和 Loser 属于实验上下文中的追加式数据结论，用户不能直接编辑。
- inconclusive 是一次判定的数据不足结果，不是 Learning，也不写素材结论。
- 页面可以展示当前主要结论，但必须能追溯实验、市场、版位、落地页、Offer、指标快照和规则版本。

## 3. 功能区验收合同

### 3.1 新建生成

#### 用户行为

- 用户可选择或编辑产品，选择维度取值，并在全组合与带上限的覆盖采样之间切换；无效或矛盾组合在付费调用前被排除。
- 用户可选择 Provider、模型、生成意图、画幅、分辨率、变体数、引用素材和“跳过已判 Loser 组合”等参数，并在提交前看到组合数、调用数和成本估算。
- Prompt 必须由版本化规则、产品及 must-match、受众、意图、维度英文片段、风格、负面约束、结构化创意数据、画幅或质量约束、引用素材和适用的 QC 反馈组装；最终 Prompt 必须可审计。

#### 持久化与联动

- 提交时创建不可变 GenerationRun 和生成配方快照，保存产品、维度、组合策略、Prompt、Provider、模型、参数、引用素材、成本估算及规则版本。
- 每个“组合 × 变体”对应独立 RunItem，可独立排队、运行、重试、复用、失败或成功。
- 组合跨轮识别使用稳定维度值与标签签名，不依赖当轮可变的 C01、C02 等展示序号。
- 成功 RunItem 创建或复用 AssetVersion；素材必须能反查生成配方、成本记录和引用来源。

#### 恢复与防重

- 相同请求重放时复用已成功的 RunItem 和素材，不重复调用或成本。
- 只有用户明确选择新 Seed、编辑 Prompt 或其他再生成动作时，才创建新的付费意图和 AssetVersion。
- 超时和明确的暂时性错误最多自动重试三次；Worker 中断后从未完成项恢复，已成功项不得重做。
- QC 反馈可以进入下一次配方；连续三次仍未通过时保留产物和失败证据，并将人工素材质量投影为 Rejected，不能伪装成任务丢失。
- 配方复现固定 Prompt、Seed、模型版本和参数。只有 Provider 明确保证确定性时才要求文件哈希一致，否则 UI 必须说明“配方一致，不保证逐字节一致”。

### 3.2 素材

#### 用户行为

- 同一页面展示批次轨和聚合所有轮次的素材画廊；批次轨显示排队、重试、QC、失败、续跑和复用，画廊筛选项从素材实际标签、Provider、人工质量和实验结论计算。
- 全屏审核模式必须支持 `A`=Approved、`C`=Needs Changes、`X`=Rejected、空格=跳过、左箭头=返回修改，并在完成后提供“重生成 Needs Changes”“增补 Approved”“去投放实验”三个出口。
- Lightbox 必须展示组合标签、Provider、模型、Seed、引用素材、QC、人工素材质量、上下文化实验结论、版本历史和完整 Prompt。
- 单张操作必须支持配方复现、新 Seed 再生成和编辑 Prompt 再生成；每次再生成都创建新的 AssetVersion，新版本回到待审核，旧版本和意见不被覆盖。
- 质量回流支持每个 Needs Changes 组合按原配方增加 2 张、每个 Approved 组合按原配方增加 3 张；下一轮默认跳过上下文化结论为 Loser 的标签签名，并清楚显示跳过数量和节省估算。

#### 导出

- 用户可导出仅 Winner、仅 Approved 或全部成功素材的 ZIP。
- 相同文件按内容哈希去重，但 Manifest 保留所有业务引用。
- Manifest 至少包含组合展示号与稳定签名、全部维度标签、Provider、模型、Seed、AssetVersion、人工质量、实验结论及上下文、QC、引用素材、完整 Prompt、生成运行、文件哈希和 Mock 证据。

### 3.3 投放实验

#### 创建与冻结

- 候选只来自当前 Workspace 中 Approved 的具体 AssetVersion；概念相似素材必须被标识，用户确认后每个概念只保留一个实验候选。
- 实验结构为一组合对应一 ad set 和一条 ad，使用 ABO 等额预算及 broad 定向；Persona、Angle、Hook、Offer 和 Landing Page 与广告映射一起持久化。
- 冻结快照必须包含候选版本、组合、预算、周期、受众、版位、营销变量、`7d_click + 1d_view` 归因窗口和判定规则。冻结后不可修改，只能取消或创建新实验。
- UI 在冻结或 Launch 前显示 `候选数 × 每日预算 × 天数` 的成本边界。

#### Launch 与同步

- Launch 必须完整执行 Preview、短时二次确认和可恢复 Saga；Preview 变化后旧确认立即失效。
- Meta Sandbox 可以替换外部 Adapter，但必须执行同一套领域状态机、幂等映射、补偿、marker 对账和故障恢复。
- Campaign、AdSet、Ad 三层创建时都必须为 PAUSED，并回读验证；MarketAI 永远不得自动启用。客户真实 Meta 写入不属于本里程碑。
- 每日同步保存原始分页、游标、daily 指标、状态历史和 ad_id 到实验候选及组合的映射，且使用冻结契约哈希隔离不同采集口径。

#### 判定与回流

- CTR 使用 Wilson 区间；CPA 和 ROAS 使用业务阈值及确定性 bootstrap；盈亏线、最低购买数和统计计划属于冻结规则。
- 数据不足追加 inconclusive 记录；数据充分但未形成明确输赢时可以判 Learning；不得为了展示强制选择 Winner。
- 每次判定记录指标快照、规则版本、理由和结果。用户确认终判后，才为对应 AssetVersion 追加 Winner、Learning 或 Loser。
- Asset Winner 必须保留市场、版位、Landing Page 和 Offer 上下文；同一变量组合跨素材、跨轮重复胜出后才可升级为 Pattern。
- 归因卡、逐日数据、漏斗和下一轮配方必须从持久化指标与判定记录生成。

### 3.4 本地化

#### 输入与真值

- 一个批次固定一个 ProductVersion 和与 Winner 上下文一致的 OfferVersion；输入只允许使用人工 Approved 且具有未撤销、已确认 Winner 结论的具体 AssetVersion。
- 用户可从 GB、AU、CA、DE、FR、ES、IT、JP、KR 九个版本化目标市场中选择；首版源市场显式冻结为 `en-US/US/USD`，不在目标列表。
- SourceClaimInventory 保存源图声明、区域、置信度和人工确认；用户只能对已发现声明选择 replace、remove 或市场中性的 preserve，不得新增声明。
- 每个批次只解析并冻结一个版本化 TruthPackage，记录 Offer、当地价、划线价、优惠、组件、配送、币种、取价上下文、来源、有效期及派生规则。
- full 表示所有选定市场可安全运行；degraded 表示至少一个市场可运行但其他市场或声明被明确阻塞、移除或复用；failed 表示包无法安全冻结或没有市场可运行并阻止整批。市场敏感事实缺失时不得保留源市场值。
- 不得用汇率、默认/第一 Variant、主商品价格比例、无证据折扣叠加或模型推断价格、组件、配送和商业声明；每个国家独立取价，除非来源证明共享同一 Price List。
- 数据源变化创建新版本并使依赖包对未来批次不可复用；历史产物继续关联原包，但 MarketAssetBinding 可被标记 `stale_truth` 并阻止新实验或 Launch。

#### 执行与恢复

- 翻译先创建 LocalizedCopyVersion；锁定商业事实由确定性规则注入，Owner 或 Member 确认后才进入真实图片编辑。
- 每个“源 AssetVersion × 目标市场”创建独立 Item，具有 queued、running、retry_wait、paused、succeeded、failed、skipped、cancelled 和 uncertain；批次结果独立表达 succeeded、partially_succeeded 或 failed。
- 同来源、Winner、Offer、市场、TruthPackage、ClaimInventory、CopyVersion、MarketPolicy、处理配方和 execution mode 已有成功产物时必须复用，不覆盖、不调用、不扣费。
- 任何市场没有需本地化内容时记录 `skipped_no_change_required` 并让 MarketAssetBinding 引用源版本；英语市场不能仅因语言相同自动跳过。
- 处理管道包含源声明分析、图片编辑、确定性事实校验、OCR/布局检查和视觉对照 QC；最多 3 个编辑 Attempt（含首次），仍不通过时保留结构化失败证据，人工 RetryItem 属于新的付费意图。
- 本地化调度上限为 3，实际 Provider 并发取该值、Plan Entitlement、Provider 限制和 Workspace 容量的最小值；Beta 真实调用上限为 2。
- 成功产物创建源 AssetVersion 的子版本，人工质量回到 pending 且不继承 Winner；Approved 后只获得目标市场 `ready_for_experiment` 资格。
- Shopify、翻译、图片编辑和视觉 QC 可以使用受控 Mock；TruthPackage、状态矩阵、幂等、重试、成本、QC 和产物版本必须真实。

详细领域行为、市场目录和验收门见 [本地化真值包与素材谱系契约](localization-truth-package-contract.md)。

### 3.5 放量

- 用户必须显式建立 ScaleEnrollment，把准确 WinnerDecision、AssetVersion、Offer、市场、LP 与具体 Meta campaign/ad set/ad 绑定；相同素材的不同 delivery 上下文不能混合。
- ScaleEpisode 只覆盖配置稳定区间；素材、Offer、受众、版位、归因、竞价或累计 20% 预算变化关闭旧段并重新积累成熟度，暂停和零 delivery 不冒充绩效下降。
- ScaleEvidenceSnapshot 冻结 daily、准确 window reach/frequency、独立 breakdown、DeliveryConfig、数据水位、指标合同、Policy、Target 和质量问题；frequency/reach 不得从 daily 求和或平均。
- Beta 默认成熟门为 14 个完整账户本地日、至少 10 个 delivery 日，以及两个 7 日窗口各 1,000 impressions、20 link clicks；经济建议还要求各 5 purchases 和适用 spend 门。
- 数据必须通过分页完成、36 小时 freshness、contract、时区、币种、binding、gap、reconcile 和 mode 门禁；失败只产生 DataQualityIssue，不产生确定性 Advice。
- `evaluate_scale(snapshot)` 必须是确定性纯函数；疲劳需要 frequency≥3.5 且增长≥20%、CTR 下降≥25%并有区间支持、CPA 上升或 ROAS 下降≥20%，连续两次评估成立。
- ScaleHealth 区分 healthy、fatigue_watch、creative_fatigue、economics_decline、delivery_anomaly 和 not_delivering；Offer/LP、拍卖与素材问题不能混成一个“换弹”原因。
- ScalePolicyVersion 由平台发布最低证据和算法；Owner 发布 Product/Offer 的 ScaleTargetVersion，只能更严格。consider scale 必须通过成熟度、连续健康、CPA/ROAS 区间、配置和 learning diagnostic 门禁。
- Advice 只支持 refresh creative、review offer/LP、review delivery mix、investigate delivery anomaly 和 consider scale test；重复信号进入 AdviceThread/Occurrence，不每天制造新卡。
- refresh creative 只创建未付费 GenerationDraft，重新经过 ResolutionPreview、成本确认、QC 和实验；consider scale 只创建不连接 Meta 的 ScaleTestPlanDraft。
- placement/country 只有通过占比、样本、经济区间、多重比较和连续命中才产生 review 建议；age/gender 仅描述，不产生 targeting 建议。
- UserDisposition 不等于 AdviceOutcome；效果只能由后续素材、实验、远端配置和 Evidence 证明。UnobservedFactor 与未知 DeliveryDiagnostic 不得估算。
- real-read-only、Sandbox 和 Mock 从 Enrollment 到 Outcome 全链隔离；Scale API/Worker 永远不读取写 Token，也不启停、删除或修改真实广告、预算、竞价或受众。

详细状态、阈值、调度与验收门见 [放量证据、健康与建议语义契约](scale-advice-semantics-contract.md)。

### 3.6 创意库

- 创意库保留产品、人物、维度模板、Prompt 规则和数据源五类入口；每个对象使用稳定 LibraryEntry、带 ETag 的 Draft 和不可变 Published Version，Published Version 不允许直接修改。
- ProductVersion、OfferVersion、MarketSourceMappingVersion、DataSourceVersion 和 CredentialVersion 分离；Clone Product 默认不复制 Offer、价格、数据源映射或 Landing Page。
- ReferenceAssetVersion 经过 quarantine、文件/隐私/内容/权利门禁后，通过 ReferenceBinding 和版本化选择规则关联产品、人物或参考组；相同 hash 不合并业务来源，也不跨 Workspace 暴露命中。
- 人物取值保存 latest 或 pinned PersonBinding；预览解析实际版本，提交后冻结，人物或权利变化不得改写历史任务。
- DimensionTemplateVersion 可包含多维度、结构化 CreativeBrief 和稳定 ID 约束；Apply 是带谱系的深拷贝，模板升级只能经显式 diff/merge 产生新 Draft。
- must-match 采用结构化 Requirement；跨产品学习只认显式 DimensionConceptKey 和可比实验上下文，不能按名称聚合或自动回写模板。
- Prompt 由平台不可关闭安全基线、Owner 发布的 Workspace Policy 和运行输入确定性组装；未知 Token 阻止发布，删除锁定短语会恢复受保护条款并要求重新确认。
- DataSource 非敏感配置、KMS 凭据和连接健康分离；多源优先级由 TruthResolutionPolicyVersion 决定，人工事实导入只创建类型化 Draft。
- Bind、Fork、Apply、Clone、Supersede、Withdraw 和 Archive 是不同命令；已引用版本不物理删除，紧急 Withdraw 沿 DependencyIndex 阻止新的外部步骤和下游资格。
- 提交前 ResolutionPreview 展示所有解析版本、实际参考图、最终 Prompt、Provider 能力和最大成本；依赖漂移使 hash 失效，成功提交原子创建 GenerationRecipeSnapshot，Worker 不解析 current version。
- Mock 和真实 DataSource、Reference 检查、预览与快照使用不同版本身份和指纹，不能原地转换或静默回退。

详细对象、复用、发布和验收门见 [创意库所有权、版本与复用契约](creative-library-ownership-and-reuse-contract.md)。

## 4. 跨区黄金路径

验收从一个已激活但没有任何产品、素材、实验或建议数据的 Workspace 开始。预置演示对象或直接向业务表插入数据不能替代以下步骤：

1. Owner 完成 Workspace 基础配置；Member 登录并只能看到该 Workspace。
2. 用户在创意库创建产品、must-match、人物或参考素材、维度模板、Prompt 规则和人工数据源。
3. 用户从新建生成页应用模板、选择组合与受控 Mock Provider，确认成本后提交 GenerationRun。
4. 测试在运行中终止 Worker；恢复后成功项不重复，未完成项继续，成本预留与结算一致。
5. 用户刷新页面，在素材页看到真实批次和素材，完成键盘审核、一次再生成和一次 ZIP + Manifest 导出。
6. 用户用 Approved AssetVersion 创建并冻结实验，通过 Meta Sandbox 完成 Preview、二次确认和全 PAUSED Saga。
7. 受控 Meta 指标经真实同步、投影和 judge 路径产生至少一次 inconclusive，并在数据充分后形成可确认结论。测试时钟和确定性指标只能存在于测试 Adapter，不得成为产品演示状态。
8. Winner AssetVersion 进入本地化，冻结 Truth Packet，完成至少一个成功、一个 skipped 或 degraded，以及一个失败后可重试的任务项。
9. 持久化指标触发一条带证据的 ScaleAdvice，用户将其转换成生成草稿，但不提交费用、不修改 Meta 状态。
10. API 和 Worker 再次重启后，六区状态、版本链、成本、审计、Mock 标识及跨区链接保持一致。

## 5. 必须覆盖的故障与安全验收

| 场景 | 必须结果 |
|---|---|
| 重复点击提交或客户端重试 | 返回同一业务结果或安全重放，不重复外部调用、对象或成本 |
| Worker 在生成或本地化项之间退出 | 租约到期后从未完成项恢复，成功项不重做 |
| Worker 在付费调用期间失联 | 进入 uncertain 并先对账，不盲重试 |
| Provider 超时或暂时失败 | 按策略最多重试三次，记录每次尝试和最终原因 |
| Meta Saga 任一步失败 | 保留可恢复步骤、远端 marker 和补偿证据，已有对象仍为 PAUSED |
| Shopify 主源失败 | 使用明确的 degraded Truth Packet；备用也失败则整批停止 |
| QC 连续失败 | 保留产物、反馈和失败状态，允许人工处理，不伪造成功 |
| 成本硬上限被并发请求争抢 | 只有原子预留成功的任务可调用外部服务 |
| Member 被撤销或 Workspace 停用 | 不再开始新的外部或付费副作用；已完成原子调用只允许收口与审计 |
| 跨 Workspace 猜测 ID 或拼接父子对象 | UI 不泄露，API 拒绝，RLS 阻断，审计不包含敏感内容 |

## 6. 验收证据

里程碑关闭前必须同时具备：

- 六区跨区黄金路径浏览器 E2E；
- API 合同测试和领域规则单元测试；
- Worker kill/restart、重复提交、Adapter 超时、uncertain 对账和 Meta Saga 故障注入；
- Owner、Member、撤销成员和跨 Workspace 的授权及 RLS 测试；
- Alembic 空 PostgreSQL 建库、从当前支持版本升级和迁移漂移测试；
- 成本预留、结算、释放与多币种精度测试；
- 受控 Mock 与真实数据不可混用测试；
- 可关联用户请求、领域任务、外部调用、成本和审计事件的结构化日志；
- 一份人工 UX 检查表，确认空状态、进行中、部分成功、失败原因、恢复动作、权限和成本提示可理解。

## 7. 明确排除项

以下内容不属于 v9 六区功能对齐，不得用来阻塞本里程碑：

- 像素级复刻独立 v9 演示 HTML；
- 公开注册、正式定价、试用政策和完整商业化计费；
- 客户真实 Meta 写入、自动启停广告、删除广告或自动修改预算；
- 完整 KOL CRM、达人撮合或结算；创意库中的人物档案和参考素材仍属于本合同；
- 正式生产部署拓扑、灾备演练和规模压测；
- Gate 3 真实广告闭环；
- 演示版预置样本、固定建议卡、确定性故障选择器和其他教学道具。

## 8. 后续分解边界

本合同是后续专项设计和实现的共同验收上界，不替代它们：

- Creative Library 的实体、引用和复用细节见 [已定案专项契约](creative-library-ownership-and-reuse-contract.md)；
- Localization 的 Truth Packet、市场规则和 Adapter 合同由对应专项任务细化；
- Scale 的指标窗口、疲劳阈值和建议规则见 [已定案专项契约](scale-advice-semantics-contract.md)；
- 订阅、额度和成本归属只可在本合同的成本不变量内细化；
- 前端壳、BFF 和 FastAPI 合同只可在本合同的 UI 黄金路径与身份边界内细化。

任何后续设计如果降低本合同中的持久化、恢复、租户隔离、成本、防重或 Mock 证据要求，都必须重新打开决策，而不能在实现中静默弱化。
