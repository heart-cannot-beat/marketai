# 本地化真值包与素材谱系契约

状态：Accepted  
定案日期：2026-08-25  
适用范围：v9 本地化功能区与跨区黄金路径

## 1. 目标与不变量

本契约把演示版“一张 Winner 卖九国”的交互语义落实为持久化、可恢复、可核算和可审计的领域行为。它不授权系统推断客户的价格、折扣、组件、配送或市场表现。

以下不变量优先于 Provider、Prompt 和页面便利性：

1. 本地化输入是具体、人工 Approved 且具有未撤销已确认 Winner 结论的 AssetVersion，不是“当前最好看的图片”。
2. 一批只处理一个 ProductVersion 和与 Winner 上下文一致的 OfferVersion；不同商业来源不得因文件相同而静默合并。
3. 每批只解析并冻结一个不可变 TruthPackage；Worker 不在任务中重新查询实时商业事实。
4. 价格、币种、折扣、组件金额、配送、时效和认证编号只能来自可追溯事实，不能由模型、汇率或近似公式推断。
5. LLM 只翻译语言片段；锁定事实由确定性规则注入并验证。
6. 成功本地化产物是源 AssetVersion 的子版本，但不继承人工 Approved 或 Winner。
7. 相同语义意图复用成功产物；失败后的重新付费必须是用户明确的新意图。
8. 受控 Mock 只替换外部 Adapter，TruthPackage、状态机、成本、幂等、QC 证据和版本谱系仍走真实路径。

## 2. 领域对象与权威关系

| 对象 | 含义与所有权 |
|---|---|
| SourceMarketSnapshot | 源素材所处的 locale、国家、币种及 Winner 商业上下文快照 |
| OfferVersion | ProductVersion 在特定商品、Variant、数量、套装和 Landing Page 下的一次不可变销售方案 |
| MarketPolicyVersion | 一个目标市场的 locale、国家、币种、取价、格式、语气和声明规则版本 |
| DataSourceVersion | 创意库拥有的 Adapter、店铺、API、能力和其他非敏感来源配置版本 |
| MarketSourceMappingVersion | OfferVersion 到具体 DataSource 外部商品、Variant、Price List 或配送对象的不可变映射 |
| ManualFactSetVersion | Owner 发布的类型化人工商业事实版本 |
| TruthResolutionPolicyVersion | 按事实类型规定可信来源顺序、新鲜度和组合规则的版本 |
| TruthPackage | 一批使用的不可变商业事实、派生事实、来源和逐市场 readiness |
| SourceClaimInventory | 对源 AssetVersion 可见文字和声明的版本化观察与人工确认结果 |
| LocalizedCopyVersion | 一个源文案在目标市场下经锁定事实校验并由用户确认的不可变文本版本 |
| LocalizationBatch | 一次冻结的本地化用户意图，包含多个 source × market Item |
| LocalizationItem | 一个源 AssetVersion 与一个目标市场的独立执行单元 |
| LocalizationAttempt | 一次具体的翻译、分析、编辑或 QC 外部调用证据 |
| MarketAssetBinding | 一个市场可使用哪个 AssetVersion 以及是否具备进入实验资格的持久化关系 |

这些对象全部属于请求级 Workspace，并受 TenantContext、PostgreSQL RLS、双 Actor 审计、Entitlement lease 和 Spend Guardrail 约束。

## 3. 客户工作流

1. 用户进入本地化页，系统列出当前 Workspace 中符合资格的 Winner AssetVersion。
2. 用户选择一个 ProductVersion 下的一至多个来源素材，并确认 SourceMarketSnapshot 和 OfferVersion。
3. 用户选择目标市场；系统创建或复用 SourceClaimInventory，低置信度观察等待确认。
4. 系统按冻结的 TruthResolutionPolicyVersion 解析一次 TruthPackage，并展示 full/degraded/failed、逐市场 readiness、来源和有效期。
5. 用户为已发现声明选择 replace、remove 或 preserve，并选择自动或自定义 headline。
6. 系统为每个 source × market 创建 LocalizedCopyVersion 草稿，注入锁定事实并运行确定性文本校验。
7. Owner 或 Member 预览并确认文案；真实图片编辑前展示最大成本报价和可复用项。
8. 用户最终确认后冻结 LocalizationBatch，原子创建 Operation、Item、成本 Reservation 和 Job。
9. Worker 分阶段执行分析复用、图片编辑、确定性校验、OCR/布局检查和视觉 QC。
10. 成功 Item 创建待人工审核的 localized AssetVersion；无需改图的 Item 创建复用源版本的 MarketAssetBinding。
11. 用户在素材区进行 AssetVersion 级人工审核；Approved 后目标市场绑定进入 `ready_for_experiment`。
12. 目标市场必须重新运行实验，才能产生该上下文的 Winner。

## 4. 来源资格与去重

### 4.1 资格门

来源 AssetVersion 必须同时满足：

- 当前人工素材质量为 Approved；
- 存在已由用户确认且未撤销的 Winner 结论；
- Winner 结论引用的市场、版位、Offer 和 Landing Page 可解析；
- SourceMarketSnapshot 与 Winner 上下文一致；
- ProductVersion、OfferVersion 和内容对象仍存在且属于当前 Workspace；
- 不是受控 Mock Winner 冒充的真实 Winner；Mock 验收只能进入明确标记的 Mock 批次。

v9 首版只接受 `en-US`、国家 `US`、币种 `USD` 的来源，但字段必须显式保存，不允许从像素或默认 Workspace 猜测。

系统展示合格来源，由用户明确选择。相同 AssetVersion 在一批只出现一次；相同内容哈希但 Winner、Offer、Landing Page 或业务来源不同的条目默认保持独立，只有用户在预览中确认语义等价后才能合并。

### 4.2 运行时资格变化

Worker 在每个新的外部或付费步骤前重新检查来源当前质量、Winner 撤销状态、Workspace 生命周期和 Entitlement lease：

- 尚未调用 Provider：Item 进入 paused，并记录具体恢复条件；
- 已发出的不可分割调用：允许返回，保存结果和成本证据；
- 已完成产物：保留历史谱系，但撤销 `ready_for_experiment` 并向用户告警；
- 来源资格恢复后不自动续跑，用户必须确认恢复。

## 5. 市场目录

`en-US` 是首版源市场，不在目标列表。首个 MarketPolicyVersion 目录为：

| 市场 | locale | 国家 | 币种 | Shopify 取价上下文 |
|---|---|---|---|---|
| English / United Kingdom | `en-GB` | `GB` | `GBP` | `GB` |
| English / Australia | `en-AU` | `AU` | `AUD` | `AU` |
| English / Canada | `en-CA` | `CA` | `CAD` | `CA` |
| German / Germany | `de-DE` | `DE` | `EUR` | `DE` |
| French / France | `fr-FR` | `FR` | `EUR` | `FR` |
| Spanish / Spain | `es-ES` | `ES` | `EUR` | `ES` |
| Italian / Italy | `it-IT` | `IT` | `EUR` | `IT` |
| Japanese / Japan | `ja-JP` | `JP` | `JPY` | `JP` |
| Korean / South Korea | `ko-KR` | `KR` | `KRW` | `KR` |

稳定 `market_id` 与 locale、国家、币种分字段保存；演示的 `de-EUR` 一类 output code 只能作为人读标签。币种相同不表示价格相同。只有 DataSource 证据明确表明多个国家共享同一 Price List 时才能复用价格，并仍为每个市场保存各自来源上下文。

MarketPolicyVersion 还冻结：数字与货币格式、基础语气、保留词、受限声明类型、是否需要本地 copy、配送地址粒度、事实新鲜度上限和 QC 规则。发布新版本不能改写历史批次。

## 6. Offer 与数据源

### 6.1 OfferVersion

OfferVersion 至少保存 ProductVersion、准确 Variant/组合的业务含义、数量、套装组件、Landing Page 和适用市场；具体 DataSource 的商品、Variant、Price List 和配送对象由 MarketSourceMappingVersion 绑定。系统不得查询“第一个 Variant”或只按商品标题猜映射。

若 Winner 使用套装、限时 Offer 或特定 Landing Page，本地化必须绑定同一商业含义。更换商品、Variant、数量、套装或页面属于新 OfferVersion，也会使依赖的 TruthPackage 对未来批次不可复用。

### 6.2 DataSourceVersion

DataSourceVersion 保存 Adapter、店铺、API、市场能力和非敏感配置；CredentialVersion 在独立 KMS 边界保存秘密，轮换凭据不改变 DataSourceVersion 或历史 TruthPackage。首版支持以下深度明确的 Adapter：

- Manual Commerce Facts：非 Shopify 客户发布的类型化 ManualFactSetVersion，不是临时假数据；
- Shopify Storefront：按目标国家上下文获取商品/Variant 的当地价、划线价、币种和可售性；
- Product Claim Source：来自版本化产品档案和合规证据；
- Shipping Policy Source：人工发布规则、Shopify metaobject 或可复现的配送查询。

Member 可以编辑 Draft；只有 Owner 能在二次确认和审计下发布或撤销 DataSourceVersion、OfferVersion 和人工商业事实。外部凭据使用 Workspace 凭据版本和 KMS 加密边界，不能进入 TruthPackage、浏览器、日志或导出。

### 6.3 TruthResolutionPolicyVersion

策略按事实类型规定允许来源、优先级、最大年龄和失败行为。不同来源可以组成一个 TruthPackage，但每条 Fact 必须保留 source kind、source version、external object reference、requested context、observed_at、valid_until、raw evidence hash 和解析规则版本。

`rules[].source_priority` 按优先顺序列出数据源**条目** id（DataSourceDefinition 的稳定身份），不是 Adapter kind，也不是某个 DataSourceVersion（#70）。

- 发布策略时，每一项解析到该数据源的当前版本，依赖边仍是版本到版本；撤回这个版本时，影响预览会列出该策略版本。数据源未发布、已撤回、已归档或属于其他 Workspace 时，发布被拒。
- 解析 Truth 时，按顺序读取每个条目**当前 effective** 的 DataSourceVersion，由它的 Adapter kind 选择 Adapter，并把实际用到的版本记入 `truth_source_attempts`。数据源重新发布后，策略不用重发。数据源已撤回或归档时，解析返回 422。
- 开始准备的请求只指定策略版本，以及 manual 来源要读的 ManualFactSetVersion；调用方不指定数据源版本。
- 旧写法：写 Adapter kind（如 `mock`）的，发布时报 `trp_source_priority_adapter_kind`；#70 之前发布、写数据源版本 id 的策略继续有效，该 id 映射回所属条目，同样读取当前 effective 版本。

Adapter 失败不能临时选择未配置来源。已配置备用来源只在它自身有效、满足同一 Offer/市场语义且页面明确展示 provenance 时使用；不得用汇率、默认市场或模型补齐缺失价格。

## 7. TruthPackage

### 7.1 事实形状

TruthPackage 区分原始事实和派生事实。

原始事实至少包括：

- 当地售价、划线价、币种和可售状态；
- 优惠值、适用范围、资格、组合/互斥证据和有效期；
- 每个组件自己的 OfferVersion 与当地价格；
- 配送可用性、适用地区、免邮门槛、时效范围、条件和有效期；
- 产品性能、重量、认证编号及其证据来源；
- 各市场请求上下文和 Provider 返回证据哈希。

派生事实至少保存输入 Fact ID、规则版本和结果，例如格式化金额、经证据确认的叠加折扣和最高可宣称折扣。不能只保存最终 Prompt 或展示字符串。

### 7.2 商业纪律

- Money 使用 Decimal、ISO 4217 币种和来源精度；显示按 locale/MarketPolicy 确定性格式化。
- 不自动把 JPY 取百或 KRW 取千；营销取整必须是 Owner 发布的规则并保存派生证据。
- 不按主商品价格比例估算组件；每个组件必须绑定自己的 OfferVersion 并获取当地事实。
- 只有来源明确证明优惠可叠加时才计算叠加折扣；否则不生成“最高可省”声明。
- 不根据商品价格猜免邮门槛，也不把仅对部分邮编有效的配送结果写成全国承诺。
- Shopify 按每个国家独立取价；FR、ES、IT 不默认复用 DE。

### 7.3 状态与逐市场 readiness

- `full`：所有选定市场与已选择声明都有安全、未过期事实；
- `degraded`：至少一个市场可安全运行，但部分市场或声明被明确阻塞、移除或复用；
- `failed`：核心 Product/Offer 身份错误、来源整体不可用、包无法冻结，或没有任何选定市场可安全运行。

`failed` 阻止整批；`degraded` 允许 ready 市场继续，对应不可用 Item 记录 failed 或 skipped，并使 BatchOutcome 可以是 partially_succeeded。不得把部分结果展示成全成功。

缺失市场敏感真值时禁止保留源市场的价格、币种、折扣、组件金额、配送或时效。只有 MarketPolicy 确认为市场中性的内容可以 preserve；如果编辑策略能可靠遮盖声明，用户可在预览后选择 remove，否则该市场不可执行。

`available=false` 是有效事实，对应 Item 记录 `skipped_market_unavailable`；关闭运费展示不能绕过商品不可销售门禁。

TruthResolutionPolicyVersion 为每个事实类型冻结一条 `on_failure`。已配置来源用尽（或不允许 fallback）后，仍未解析的事实按下表决定该市场的 readiness：

| `on_failure` | 非价格事实缺失 | 价格缺失 |
|---|---|---|
| `block` | 该市场 `failed`（`fact_missing_blocked`），条目不执行 | 同左，不适用价格例外 |
| `degrade` | 该市场 `degraded`（`fact_missing_permitted`），依赖它的声明只能删除 | 发布和解析时都拒绝 |
| `skip` | 同 `degrade` | 该市场 `failed`（`price_unresolved`）；只有 MarketPolicy 不要求本地价、并且全部价格声明都删除时，条目才执行 |

- 先后顺序：`available=false` 优先于缺失规则，其次是 `block`，再次是价格。缺失的事实里只要有一个是 `block`，该市场就是 `failed`；`readiness_detail` 的 `blocked` 列出这些 `block` 事实，`missing` 列出全部缺失事实。
- `block` 不看来源图里是否有声明用到这个事实。想要「用到才阻断」，用 `skip` 加声明审核。
- 非价格事实的 `degrade` 和 `skip` 效果相同，二者只在价格上不同。
- readiness 在解析时按冻结的策略算一次，随 TruthPackage 冻结；规则改了不回溯。已冻结的包，以及已绑定它的批次，仍按冻结的 readiness 处置。解析规则版本（当前为 `truth-rule-v2`）记在每条来源尝试上。

### 7.4 新鲜度与失效

TruthPackage 的 `valid_until` 取全部依赖事实的最短有效期。Beta 默认策略：

- Shopify 价格、折扣、可售性：6 小时；
- 动态配送事实：6 小时；
- 人工商业事实：发布时必须填写有效期，默认最长 30 天；
- 稳定产品声明：持续到来源版本被替换。

DataSource 配置、MarketSourceMapping、Offer 或事实变化创建新版本，并只将真实依赖包标为 superseded；CredentialVersion 单纯轮换不使历史事实失效。历史包和 AssetVersion 不修改，旧包不能创建新批次。Worker 在每个图片编辑调用前检查 freshness：调用前过期则暂停；调用已经发出则保存结果并标记 stale，重新确认前不得发布。

FactDependencyIndex 将数据变化传播到依赖的 MarketAssetBinding，标记 `stale_truth` 并阻止新实验和新 Launch。已冻结实验保留历史事实并告警；MarketAI 不自动暂停真实广告或改图。

## 8. SourceClaimInventory

源图分析生成版本化 Inventory，每条观察至少保存 claim type、OCR 原文、区域坐标、置信度、分析 Provider/模型/规则、证据图和确认状态。模型观察不是商业真值；低置信度、冲突或高风险声明必须由用户确认。

用户对每条已发现声明选择：

- `replace`：必须绑定目标市场 Truth Fact；
- `remove`：必须预览遮盖策略并由 QC 验证区域确实移除；
- `preserve`：只允许 MarketPolicy 认定的市场中性内容。

页面可以保留价格、折扣、组件和运费四类快捷开关，但命令必须展开为逐 Claim 决策。关闭是 remove/preserve 决策，不是忘记该 Claim；打开不得新增源图不存在的商业声明。

当分析配方或用户修正变化时创建新 SourceClaimInventory 版本。已经冻结的 Batch 仍引用原版本，除非资格或安全 Restriction 要求暂停。

## 9. 文案与翻译

平台 MarketPolicyVersion 与 Workspace BrandLocalizationPolicyVersion 解析成 Batch 使用的 CopyPolicySnapshot。平台负责 locale、货币格式、基础语气和禁用模式；Workspace 负责产品名、保留英文词、禁译词、称谓和品牌禁用短语。

LocalizedCopyVersion 的流程为：

- `draft`：自动或自定义 source headline 与 translatable segments 已确定；
- `needs_review`：翻译完成，锁定事实已注入并通过机器校验，等待用户确认；
- `approved`：Owner 或 Member 已确认，可进入真实图片编辑；
- `superseded`：用户编辑或上游 CopyPolicy/Truth 变化后被新版本替代。

LLM 只接收可翻译片段和不可改写占位符。金额、折扣、时效、认证编号和其他锁定值由确定性 Formatter 从 Truth Fact 注入。校验必须证明占位符一一对应、数值和单位未变化、没有新增商业声明，且受保护短语完整。

真实图片编辑前必须展示每个市场的文案、事实来源、警告、Mock/真实模式和最大成本报价，并由用户确认；可以批量确认全部已通过事实校验的版本。修改文本创建新 LocalizedCopyVersion，不覆盖已确认记录。

## 10. Batch、Item 与 Attempt 状态

一个字段不能同时承载流程、结果和真值新鲜度：

- BatchPhase：`draft`、`preparing`、`awaiting_copy_approval`、`ready`、`running`、`closed`、`cancelled`；
- BatchOutcome：`none`、`succeeded`、`partially_succeeded`、`failed`；
- TruthFreshness：`current`、`superseded`、`expired`。

LocalizationItem 状态为：`queued`、`running`、`retry_wait`、`paused`、`succeeded`、`failed`、`skipped`、`cancelled`、`uncertain`。每个非成功状态必须保存稳定 reason code、用户可读说明、是否可恢复和下一动作；`uncertain` 只能通过对账退出。

Item 最多执行 3 次图片编辑 Attempt，包括首次。每个 Attempt 保存 Provider、模型、Prompt/参数 hash、输入版本、QC feedback、调用引用、开始/结束时间、成本证据、输出对象和结果。三次未通过进入 `failed_qc_exhausted`；再次尝试需要用户发出新的 RetryItem 付费意图和新 Reservation。

## 11. 冻结与幂等

Batch 进入 ready 前可以准备 Truth、Inventory 和 Copy。最终确认冻结：

- ProductVersion、OfferVersion 和 SourceMarketSnapshot；
- 来源 AssetVersion 与 Winner 结论；
- 目标 MarketPolicyVersion；
- TruthPackage 与逐市场 readiness；
- SourceClaimInventory 与逐 Claim 决策；
- LocalizedCopyVersion 与 CopyPolicySnapshot；
- 编辑/QC Provider、模型、参数和规则版本；
- execution mode、最大 Attempt、成本 Quote 和 Guardrail 结果。

冻结后不可修改，只能取消或新建 Batch。Item 防重指纹至少覆盖：

`source AssetVersion + WinnerDecision + OfferVersion + target market + TruthPackage + SourceClaimInventory + LocalizedCopyVersion + MarketPolicyVersion + edit/QC recipe + execution mode`

- 同指纹已有成功 Item：复用原产物和证据，不调用、不扣费；
- 同指纹正在执行：返回原 Operation；
- 同指纹失败：返回原失败，只有 RetryItem 才创建新的付费 Attempt；
- 真值、文案、市场或配方变化：创建新 Batch/Item 指纹；
- 输出文件名和对象是否存在都不是领域身份。

源图分析和翻译可以分别按自身完整指纹复用，但不得跨 execution mode 或版本证据混用。

## 12. 编辑与 QC

处理管道按阶段执行：

1. 复用或创建 SourceClaimInventory；
2. 复用已 approved LocalizedCopyVersion；
3. 调用图片编辑 Adapter，只修改已授权区域；
4. 运行锁定事实、数字、币种、Claim 决策等确定性检查；
5. 运行 OCR、文字残留、截断、遮挡和越界检查；
6. 运行视觉对照 QC，确认产品、Logo、人物和非目标区域与源版本保持一致；
7. 失败时把结构化反馈加入下一 Attempt，成功时完成 AssetVersion。

QCRecord 保存每条 check、expected、observed、证据位置、severity、规则版本和结果。阻断项包括锁定事实变化、未知新增声明、错误币种/数字、replace/remove 未落实、严重文字截断和源产品保真失败；非阻断警告仍需在人工审核中可见。

本地化自身调度目标上限为 3，但实际 Provider 调用并发取“3、Plan Entitlement、Provider 限制、Workspace 当前容量”的最小值。Beta PlanVersion 的真实 Provider 上限为 2，因此首版实际最多并发 2；排队 UI 必须解释限制来源。

批次 Quote 分别计算可复用分析、翻译、每次编辑和 QC 的最大暴露，不假设每个 Item 固定三次调用。Reservation、Reported/Estimated Settlement、Release、Adjustment 和 `uncertain` 遵守 Provider Cost Ledger 契约。

## 13. 跳过、取消与恢复

### 13.1 无需改图

任何目标市场在 SourceClaimInventory 证明没有需要本地化的文字或市场敏感声明，且 MarketPolicy 不要求语言改写时，Item 记录 `skipped_no_change_required`，MarketAssetBinding 直接引用源 AssetVersion。不复制文件、不调用 Provider、不产生真实成本。

英语市场不是自动豁免；只要源图存在需改写的 copy、价格、币种或声明就必须处理。

### 13.2 取消

取消 Batch 后停止未开始的 Job 并释放 Reservation。已经发出的原子调用可以返回，结果保存为隔离的 AttemptOutput，用于审计、成本和对账，不自动发布为 AssetVersion。若它通过全部 QC，用户以后可以显式采纳；采纳命令必须重新检查当前资格、Truth freshness 和审计归因。

### 13.3 真值过期

Truth 过期或 superseded 后解析新 TruthPackage 并创建新 Batch，不能延长或改写旧包。新 Batch 可以复用不依赖变化事实且指纹完全一致的分析、翻译或产物；涉及变化事实的图片必须重新编辑和 QC。

## 14. 产物谱系与发布

成功 Item 创建 `kind=localized` 的 AssetVersion，`parent_version_id` 指向源 AssetVersion，并保存 Batch、Item、Winner、ProductVersion、OfferVersion、SourceMarketSnapshot、目标 MarketPolicyVersion、TruthPackage、Inventory、CopyVersion、Attempt/QC、execution mode、对象 hash 和成本证据引用。

新版本人工质量回到 pending，不继承源版本 Approved，也不继承 Winner。人工 Approved 后 MarketAssetBinding 才进入 `ready_for_experiment`；目标市场必须运行新的冻结实验并确认结论，才能成为该市场 Winner。

来源质量或 Winner 撤销、Truth 过期、Offer/DataSource 变化会让绑定失去资格或进入 `stale_truth`，但不删除历史 AssetVersion。已冻结实验保留所用版本证据；新实验和新 Launch 被阻止。

下载文件名可以包含组合展示码、locale、国家和币种，但必须附唯一 AssetVersion 标识以避免碰撞。导出 Manifest 至少包含：

- 源 AssetVersion、Winner 结论和上下文；
- ProductVersion、OfferVersion、源与目标市场；
- TruthPackage、Fact 摘要、来源与 freshness；
- Inventory 与每条 Claim 决策；
- LocalizedCopyVersion、CopyPolicySnapshot；
- 编辑/QC Attempt、Provider、模型、规则和结果；
- 人工质量、文件 hash、成本证据和 Mock provenance。

Manifest 不包含凭据、签名 URL、未脱敏 Provider 响应或可用于访问 Shopify/对象存储的标识。

## 15. 权限、审计与安全

- Owner 发布/撤销 DataSourceVersion、OfferVersion、人工商业事实和 BrandLocalizationPolicyVersion，并管理凭据；
- Member 可以准备 Draft、选择 Winner/市场、确认已通过事实校验的文案、启动/取消/重试 Batch 和审核产物；
- 平台运营人员不因管理 Beta Access Grant 而获得客户事实、凭据或素材权限；
- 所有命令保存 audit actor 与 initiator，外部调用由 Worker 系统身份执行；
- Worker 不接受 payload 中自报的 Workspace，所有读取受 Job Workspace 与 RLS 双重约束；
- 受控 Mock、真实 Provider 和不同 Mock scenario 的指纹及数据分区不可混用。

## 16. 实现验收门

本功能只有在以下自动化证据存在时才算实现：

1. 只有 Approved + 已确认未撤销 Winner 的具体 AssetVersion 能进入 Batch。
2. 一个 Batch 不能混合 ProductVersion、OfferVersion、Workspace 或 source market。
3. 九个 MarketPolicy 使用各自国家取价；同币种不会静默复用价格。
4. Manual 与 Shopify Adapter 通过同一契约测试，来源切换和部分故障保留逐 Fact provenance。
5. 缺失市场敏感事实不能保留源市场值、汇率推断、近似组件价或无证据叠加折扣。
6. full/degraded/failed、逐市场 readiness、不可配送和部分成功可被持久化并正确恢复。
7. 锁定事实无法被翻译、图片编辑或重试改变；新增声明和错误数字被阻断。
8. 最多 3 个编辑 Attempt，明确 RetryItem 才产生新成本；Worker 崩溃和 `uncertain` 不重复外部调用。
9. 相同完整指纹复用成功产物；改变 Truth、Copy、MarketPolicy 或 execution mode 不错误复用。
10. 取消、来源资格撤销、Truth 过期和 Entitlement lease 过期会停止新的外部步骤并正确处置在途结果。
11. localized AssetVersion 不继承 Approved/Winner；人工 Approved 后只进入目标市场实验资格。
12. Fact 变化使所有依赖 MarketAssetBinding 进入 `stale_truth`，历史版本不被覆盖。
13. Owner/Member、跨 Workspace、对象访问、Manifest 脱敏和 Mock/真实隔离负向测试通过。
14. Playwright 从已有 Winner 完成选市场、Truth 预览、文案确认、故障恢复、人工审核和目标市场实验入口。

## 17. 参考资料

- [Shopify Storefront：Contextual queries](https://shopify.dev/docs/storefronts/headless/building-with-the-storefront-api/in-context)
- [Shopify Storefront：按国家查询当地商品价格](https://shopify.dev/docs/api/storefront/latest/queries/product)
- [Shopify Storefront：Localization 与可用国家/语言](https://shopify.dev/docs/api/storefront/latest/objects/Localization)

## 18. 明确不做

- 不自动声明本地化素材已在目标市场获胜。
- 不从汇率、第一 Variant、主商品比例或 LLM 推断商业事实。
- 不把演示的 `de-EUR` output code、前端四个布尔值或文件存在性当作领域模型。
- 不因一个市场失败而隐藏其他市场结果，也不把部分成功显示为全成功。
- 不在 Batch 运行时查询实时价格并静默改变同批输出。
- 不自动暂停、启用或修改真实 Meta 广告。
