# 创意库所有权、版本与复用契约

状态：Accepted  
定案日期：2026-08-26  
适用范围：v9 创意库、新建生成与依赖创意库版本的跨区流程

## 1. 目标与不变量

创意库是 Workspace 内可复用创意输入的权威来源，不是一个文件夹，也不是已生成素材的另一个入口。它必须让低频配置可以安全复用，同时保证任何后续编辑都不能改变已提交任务、历史素材、冻结实验或本地化批次的含义。

以下不变量优先于页面便利性：

1. 创意库条目有稳定身份；可编辑内容只存在于 Draft，发布后形成不可变版本。
2. GenerationRun、本地化、实验和 Launch 只引用已发布的准确版本，绝不读取可变的“当前表单”。
3. 预览可以解析“最新版”，提交必须校验预览 hash 并原子冻结所有实际依赖。
4. Worker 使用冻结快照中的内容；运行时资格检查只能阻断执行，不能静默替换版本。
5. Product、Offer、数据源配置、外部映射和凭据是不同对象，不能共用一条可变记录。
6. 相同文件不等于相同业务来源；内容存储复用和业务 Binding 必须分离。
7. 客户数据不跨 Workspace 活引用，平台目录只能通过显式 Fork/Apply 进入客户边界。
8. 普通新版本不改历史；紧急 Withdraw 必须沿依赖阻止新的外部动作。
9. Mock 与真实版本、指纹、证据和下游资格完全隔离，真实失败不回退 Mock。

## 2. 五类入口与领域所有权

创意库 UI 保留五个可见分类，但分类不是数据库单表：

| 分类 | 拥有的稳定对象与版本 |
|---|---|
| 产品 | ProductProfile、ProductVersion、OfferVersion、ReferenceGroupVersion、MarketSourceMappingVersion |
| 人物 | PersonProfile、PersonVersion、人物 ReferenceBinding 与权利证据 |
| 维度模板 | DimensionTemplate、DimensionTemplateVersion、DimensionConceptKey |
| Prompt 规则 | WorkspacePromptPolicy、WorkspacePromptPolicyVersion；平台安全基线独立拥有 |
| 数据源 | DataSourceDefinition、DataSourceVersion、CredentialBinding、ManualFactSetVersion、TruthResolutionPolicyVersion |

生成素材、AssetVersion、人工素材质量、实验结论、TruthPackage 和 ScaleAdvice 不归创意库所有。它们只能保存对创意库版本的引用和冻结快照。

OfferVersion 是产品分类下的销售方案，不新增第六个导航分类。ReferenceAssetVersion 是创意库共享的内部能力，通过 Binding 出现在产品或人物中，不作为“素材”功能区的生成产物。

## 3. 身份、草稿与发布版本

### 3.1 三层模型

每类可编辑对象采用：

- LibraryEntry：Workspace 内稳定身份、类型、展示名和生命周期；
- LibraryDraft：可修改的下一版本，保存 revision/ETag、校验状态和基准版本；
- LibraryVersion：一次原子发布得到的不可变内容、内容 hash、版本号、作者和发布时间。

自动保存只更新 Draft。任何下游执行都不能引用 Draft。发布新版本后，旧版本内容不变；`superseded` 是追加状态及当前投影，不是对旧内容的覆盖。

### 3.2 状态维度

不得使用一个 `status` 同时表达编辑、发布、连接和使用资格：

- LibraryEntryLifecycle：`active`、`archived`；
- DraftState：`editing`、`validation_failed`、`ready_to_publish`；
- PublishedVersionState：`effective`、`superseded`、`withdrawn`；
- ConnectionHealth：`unknown`、`healthy`、`degraded`、`failed`、`revoked`；
- UsageEligibility：`eligible`、`warning`、`blocked`。

版本状态变化通过追加式事件记录，并投影当前状态。连接临时失败不能把不可变内容版本改成 failed。

### 3.3 并发编辑

Draft 的每次写入必须携带 revision/ETag。版本不匹配返回冲突和字段级差异，由用户重新加载或合并；禁止最后写入者静默覆盖。v9 不提供多人实时共同编辑，用户若要保留两个方向应显式 Fork。

### 3.4 发布事务

发布先产生 ValidationReport，再在一个事务中创建 LibraryVersion、来源谱系、审计和当前版本投影。任一阻断项失败时不产生部分版本；非阻断警告必须由用户确认并进入审计。

通用校验至少覆盖 Workspace 归属、引用资格、循环引用、Reference 权利、Schema、Prompt Token、维度约束、外部映射和 execution mode。Published Version 不提供 PUT/PATCH。

## 4. 角色与权限

- Owner 和 Member 都可以创建、编辑、校验 Draft，并查看版本、引用和影响分析；
- Member 可以发布 ProductVersion、PersonVersion 和 DimensionTemplateVersion；
- Owner 才能发布或 Withdraw OfferVersion、DataSourceVersion、ManualFactSetVersion、TruthResolutionPolicyVersion 和 WorkspacePromptPolicyVersion；
- 凭据创建、轮换、撤销以及高风险发布要求 Owner、二次确认和双 Actor 审计；MFA 只在登录时验证，这些操作不再要求 MFA step-up（ADR 0015）；
- 平台安全与肖像基线不属于 Workspace 内容，Owner 也不能关闭或降低；
- 任何角色都不能把 Mock 版本改成真实版本，也不能跨 Workspace 建立 Binding。

## 5. Product 与 Offer

### 5.1 ProductProfile 与 ProductVersion

ProductProfile 是稳定产品身份。ProductVersion 至少保存：

- 名称、描述、卖点、受众和品牌约束；
- 结构化 MustMatchRequirement；
- 产品 ReferenceGroupVersion 引用和默认 ReferenceSelectionRuleVersion；
- 品牌 SKU 等不绑定某个 Adapter 的产品身份；
- 默认 DimensionSchemaVersion 与适用的 WorkspacePromptPolicyVersion 引用。

ProductVersion 不保存当前价格、默认 Variant、Shopify Token 或可变商店映射。修改描述、受众、must-match、参考组或其他生成语义必须创建新 ProductVersion。

### 5.2 OfferVersion

OfferVersion 保存一次不可变销售语义：ProductVersion、商品/Variant/组合、数量、套装组件、Landing Page、市场范围、声明约束和业务有效期。它不得通过标题或“第一个 Variant”推断外部对象。

MarketSourceMappingVersion 把 OfferVersion 映射到一个 DataSourceVersion 可识别的外部商品、Variant、Price List、配送规则或其他对象。更换店铺、Adapter 或外部对象创建新 Mapping；ProductVersion 不被污染。

Clone Product 只复制描述、must-match、维度和参考 Binding。Offer、价格、DataSource Mapping 和 Landing Page 不复制，用户也不能选择复制；新产品需要时由用户另行创建（[ADR 0016](../adr/0016-clone-product-never-copies-offers-prices-mappings-or-landing-pages.md)）。

## 6. Must-match 与产品保真

MustMatchRequirement 不是字符串数组。每条要求至少保存稳定 ID、类型、描述、严重级别、适用视角、证据 ReferenceAssetVersion 和 QC 方法。

AI 可以提出 Draft 建议，但模型观察不是已确认要求；用户确认后才能发布 ProductVersion。阻断级 must-match 进入 Prompt 锁定输入和 QC 清单，失败产物进入 QC failed，不能直接获得人工 Approved。

must-match 变化不会修改历史素材。普通 Supersede 只影响新任务；若旧描述涉及安全、商标或事实错误，Owner 使用 Withdraw 触发下游受限传播。

## 7. ReferenceAsset、Binding 与参考组

### 7.1 文件身份与业务含义

ReferenceAssetVersion 保存不可变文件 hash、MIME、像素尺寸、来源、原件对象、受控派生对象、安全检查、权利状态和审计。ReferenceBinding 把它绑定到 ProductVersion、PersonVersion 或 ReferenceGroupVersion，并保存用途、视角、优先级、说明和适用条件。

同一 Workspace 内相同文件可以复用对象存储，但不同 Binding 不因 hash 相同而合并。不同 Workspace 之间不得建立可观察的内容 hash 去重或活引用。

### 7.2 上传门禁

上传先进入 quarantine。发布 ReferenceAssetVersion 前至少完成：

- 文件大小、声明 MIME、实际解码格式和像素限制；
- 恶意文件与畸形图片检查；
- 内容 hash、重复提示和对象完整性；
- EXIF、GPS 和其他隐私元数据检查；
- 内容安全检查；
- 权利状态、授权范围、有效期和操作者声明。

原件保持私有，仅在授权审计场景访问；Provider 使用去 EXIF、符合能力限制的受控派生版本。检查失败、权利缺失、撤销或过期都阻止新的外部调用。

### 7.3 ReferenceGroupVersion 与选择规则

ReferenceGroupVersion 保存明确成员和业务角色，例如整机、接口特写或白底棚拍。ReferenceSelectionRuleVersion 匹配稳定 Dimension/Value ID、画幅、生成意图和 Provider 能力，不匹配中文标签或文件名。

ResolutionPreview 必须展示每个 RunItem 将使用的组和具体 ReferenceAssetVersion。超过 Provider 数量或尺寸限制时不得静默截断；系统按冻结的优先级给出裁剪方案并要求确认，或阻止提交。

## 8. Person 与人物引用

PersonProfile 是稳定人物身份；PersonVersion 保存名称、年龄表达、人物类型、穿着、气质、可用 prompt 片段、参考 Binding、权利范围、地域/渠道限制和有效期。

“加为取值”创建带 PersonBinding 的产品 DimensionValue Draft，不把人物档案降格成无来源字符串。Binding 支持：

- `latest_published_at_preview`：预览时解析当前有效 PersonVersion；
- `pinned`：始终解析用户指定版本。

默认使用 latest，但人物版本或参考图在预览后变化会使 preview hash 失效，提交必须重新预览和确认。GenerationRecipeSnapshot 冻结实际 PersonVersion 和 ReferenceAssetVersion，历史任务不随人物库更新。

人物必须是虚构形象或具有明确授权的品牌人才。授权 Withdraw 或过期会停止尚未发出的外部调用并阻止新的生成、实验、本地化和 Launch；历史版本和证据保留，不自动删除已投广告。

## 9. 维度模板、产品维度与概念身份

### 9.1 DimensionTemplateVersion

一个模板版本可包含一至多个维度、取值、正向/负向片段、CreativeBrief、组合约束和显式 DimensionConceptKey。CreativeBrief 必须通过版本化 JSON Schema 校验，不能把任意 JSON 原样注入 Prompt。

Apply 将模板深拷贝进产品 DimensionSchema Draft，为 Dimension/Value 创建产品本地稳定 ID，并保存 `derived_from_template_version_id` 作为谱系。模板后续修改不影响已 Apply 的产品或历史任务。

### 9.2 模板升级

已有产品采用模板新版本时执行“比较并重新套用”：展示新增、修改、删除和冲突，允许逐项选择，结果创建新的 DimensionSchema Draft。不得把模板新版本自动推送到产品，也不得覆盖 Published Schema。

### 9.3 组合约束

组合约束引用稳定 Dimension/Value ID，并至少支持 `requires`、`excludes` 和 `max_occurrence`。计划阶段先验证循环与不可满足约束；所有无效组合必须在成本预留和 Provider 调用前排除，并保留 SkipReason。

### 9.4 跨产品概念

名称和展示标签不是学习身份。产品本地 Dimension/Value 可以显式映射到 Workspace 或平台治理的 DimensionConceptKey；只有 ConceptKey 相同且实验上下文可比时，后续系统才能形成跨产品 Pattern。

实验学习不能自动修改模板、ProductVersion 或 DimensionSchema。任何把 Pattern 转成模板或生成草稿的动作都必须由用户确认并产生新 Draft。

## 10. PromptPolicy 与确定性配方

Prompt 由三层按版本化顺序组装：

1. PlatformPromptBaselineVersion：不可关闭的安全、肖像、产品保真和 Provider 限制；
2. WorkspacePromptPolicyVersion：Owner 发布的品牌语气、允许模板、可选条款和锁定短语；
3. Run 输入：已发布产品、must-match、人物、维度、意图、画幅、负面约束和 QC feedback。

规则只允许注册 Token，执行单遍替换；未知 Token、缺少必填 Token、递归展开或无法通过 Schema 的内容阻止发布。用户删除受保护短语时，系统恢复完整受保护条款、明确告警，并要求重新确认后才能发布新 WorkspacePromptPolicyVersion。

PlatformPromptBaselineVersion 若因紧急安全问题被 Withdraw，使用旧基线且尚未发出 Provider 调用的任务必须暂停并重新预览；已完成任务保留当时的基线和最终 Prompt 证据。

最终 Prompt、各层输入 hash、规则顺序和版本全部进入 GenerationRecipeSnapshot。日志不得记录包含客户敏感内容的完整 Prompt；授权页面和 Manifest 可以按既定脱敏规则展示。

## 11. DataSource、凭据与人工事实

### 11.1 DataSourceVersion

DataSourceDefinition 是稳定连接身份。DataSourceVersion 保存 Adapter kind、店铺域名、API 版本、支持能力、市场上下文、非敏感配置和 execution mode，不保存 Token。

Workspace 可以拥有多个数据源，不存在隐式“当前默认源”。TruthResolutionPolicyVersion 按事实类型定义来源优先级、允许备用、最大新鲜度和失败行为；Product/Offer 显式绑定适用策略。

### 11.2 CredentialVersion

CredentialVersion 由 KMS 加密边界拥有；领域记录只保存不透明 ID、类型、创建者、创建/轮换/撤销时间和状态。CredentialBinding 指定 DataSource 当前使用的有效版本。

凭据轮换不创建新 DataSourceVersion，也不使历史 TruthPackage 失效。每次外部调用记录实际使用的 CredentialVersion ID；秘密值永不返回浏览器、日志、审计正文或导出。

### 11.3 连接测试

连接测试由服务端 Worker 执行，使用域名规范化、HTTPS、DNS/IP 重绑定防护、出站允许列表、最小权限、超时和响应大小上限。测试结果是追加式 ConnectionCheck，更新 ConnectionHealth 投影，不修改 Published Version。

连接失败阻止新取数，但不改写历史事实。受控 Mock ConnectionCheck 与真实检查使用不同 Adapter、版本身份和证据。

### 11.4 ManualFactSetVersion

CSV、表格或 JSON 上传只能创建 Import Draft。解析结果必须成为类型化 ManualFactSetVersion，至少包含 Offer、市场、Decimal 金额、ISO 币种、事实类型、有效期和证据来源；缺币种、范围不明或无法映射的行是阻断错误。

Owner 查看逐行 ValidationReport 后发布。任意 JSON、展示字符串、汇率推断和无有效期的临时价格不能成为商业真值。

## 12. Bind、Fork、Apply 与 Clone

复用命令必须显式区分：

- Bind：在同一 Workspace 内继续引用一个稳定人物或 Reference 身份；
- Fork：复制某个 Published Version 为新的 LibraryEntry Draft，之后独立演进；
- Apply：把 DimensionTemplateVersion 深拷贝进产品 DimensionSchema Draft；
- Clone Product：创建新 ProductProfile Draft，只复制低风险内容；永不复制 Offer、价格、DataSource Mapping 和 Landing Page（§5.2、ADR 0016）。

每次操作保存 actor、source version、目标 Entry、时间和选择项。不能用“duplicate”一个动作隐式覆盖上述四种不同语义。

平台官方模板采用 Fork/Apply；客户 Workspace 之间不提供 Bind。对象存储层即使实现物理去重，也不得泄露另一 Workspace 是否拥有相同内容。

## 13. ResolutionPreview 与提交冻结

Generation Draft 可以保存 latest 或 pinned 选择器，但它不是执行依据。预览时领域服务解析并返回有有效期的 ResolutionPreview，至少包含：

- ProductVersion、OfferVersion 和 DimensionSchemaVersion；
- PersonVersion、ReferenceGroup/Rule 与实际 ReferenceAssetVersion；
- Platform/Workspace Prompt Policy、最终 Prompt 和锁定输入；
- 组合约束结果、SkipReason 和 Provider 能力裁剪；
- execution mode、Provider/模型/参数、最大成本 Quote；
- 所有内容 hash、eligibility 和整体 preview hash。

提交必须携带 preview ID/hash。服务端在同一事务内重新验证 ETag、版本状态、权利、Entitlement、Spend Guardrail 和所有依赖；任何变化都拒绝提交并要求重新预览，不能静默解析新版本。

成功提交原子创建 GenerationRun、GenerationRecipeSnapshot、RunItems、成本 Reservation 和 Jobs。Worker 的业务内容只来自快照；每个新的外部步骤前只检查 Withdraw、Workspace/成员、Entitlement、凭据和其他资格，不得用当前版本替换快照。

## 14. 影响分析、Supersede 与 Withdraw

DependencyIndex 记录 LibraryVersion 到 Generation Draft/Run、AssetVersion、Experiment、LocalizationBatch、TruthPackage、MarketAssetBinding 和 Launch 的引用。

- 发布新版本：旧版成为 superseded，只影响以后默认选择；冻结对象不变；
- Archive Entry：阻止新选择，不删除版本；
- Withdraw Version：阻止新的外部步骤，并将依赖对象投影为 warning 或 blocked；
- 权利、法务、错误事实或安全原因：必须使用 Withdraw，不得伪装成普通编辑；
- 连接临时失败：只影响新取数和 ConnectionHealth；
- Credential 轮换：不触发内容失效；
- Offer、Mapping 或商业事实变化：只 supersede 真正依赖的 TruthPackage，并按本地化契约传播 `stale_truth`。

按条目引用的依赖按条目判断影响。这类内容写的是条目 ID，发布时解析到该条目的当前版本，例如 TruthResolutionPolicy 的 `source_priority`、ManualFactSet 的 `data_source_entry_id` 和事实行的 `offer_entry_id`、DimensionSchema 的 `product_entry_id`、WorkspacePromptPolicy 的 `allowed_template_entry_ids`。它们的依赖边仍指向发布时的版本，不迁移，也不改写。Withdraw 和 Archive 的影响分析遇到条目的当前 effective 版本时，把它归到条目，列出引用该条目任一版本的依赖对象，因为这些依赖对象都解析到它。遇到已被取代的旧版本时，只列出依赖边指向该版本的依赖对象：撤回旧版本不改变条目解析到哪个版本，不要求引用条目的依赖对象重新发布。Withdraw 按同一结果把依赖对象投影为 blocked。其余依赖仍按所引用的准确版本判断。

发布、撤销和归档前 UI 展示影响分析。已发出的不可分割 Provider 调用允许返回并保存证据，结果是否可采用取决于当前资格；系统不自动删除素材、不改历史实验，也不自动暂停或修改真实广告。

## 15. 删除、保留与 Workspace 生命周期

只有从未发布、从未引用的 Draft 可以物理删除。已发布或被引用的 Entry 只能 Archive，版本和必要对象按 Workspace 保留策略继续可审计。

Reference 原件、派生文件和凭据遵守各自保留与撤销边界。Workspace 进入 deletion_pending 后停止新调用；恢复期结束后按订阅与数据生命周期契约删除客户内容，不能因全局去重保留可恢复的跨租户副本。

## 16. 导入、导出与审计

导出包包含不可变内容、版本号、内容 hash、来源谱系、结构化配置、引用清单、权利/资格摘要和 Manifest。它不得包含凭据、签名 URL、原始敏感 Provider 响应或另一 Workspace 的标识。

导入永远创建 Draft，执行 ID 重映射、Schema/引用/权限校验并生成报告。同名对象不覆盖，外部映射和商业事实不自动发布，凭据不能随包迁移。

所有创建 Draft、校验、发布、Withdraw、Archive、Bind、Fork、Apply、Clone、导入/导出和凭据操作保存双 Actor 审计。结构化日志使用 trace ID 和不透明对象 ID，不记录秘密或完整敏感 Prompt。

## 17. UI 与 API 合同

五类三栏布局可以延续演示的信息架构，但必须展示真实状态：列表、当前生效版、Draft、历史版本、来源谱系、引用者、影响分析、权限、ValidationReport、审计和 Mock/真实标识。

Published Version 只读。API 使用明确命令而非通用修改：create draft、validate、publish、withdraw、archive、fork、apply、bind、clone、resolve preview、submit。所有命令要求 Workspace Context、授权、Idempotency-Key、ETag/expected revision 和审计信息。

列表和搜索使用稳定 ID、类型、当前投影、标签和归档状态；名称不是唯一业务身份。跨 Workspace 猜测 ID 时 API、对象下载和 RLS 都必须拒绝且不泄露存在性。

## 18. 受控 Mock

Mock 只替换外部连接检查、外部数据读取、内容安全或 Provider Adapter；LibraryEntry、Draft、Published Version、ValidationReport、Reference 派生、依赖、预览、冻结、成本、审计和恢复路径仍真实执行。

execution mode 进入 DataSourceVersion、Reference 检查证据、ResolutionPreview、GenerationRecipeSnapshot、TruthPackage 和防重指纹。Mock 版本不能原地切换真实，必须创建并验证新的真实版本；真实 Adapter 失败永不自动回退 Mock。

## 19. 实现验收门

本功能只有在以下自动化证据存在时才算实现：

1. 五类条目均具备 Workspace 隔离的 Draft、校验、原子发布、版本历史和明确命令 API。
2. Published Version 无法修改；新发布不会改变历史 GenerationRun、AssetVersion、Experiment 或 LocalizationBatch。
3. Member 与 Owner 发布边界、Owner 专属高风险动作、撤销成员和跨 Workspace 负向测试通过。
4. ETag 冲突不会覆盖另一用户 Draft，并返回可理解的字段差异。
5. Product、Offer、MarketSourceMapping、DataSource 和 Credential 的身份及失效范围不混用。
6. Reference 上传经过 quarantine、安全、EXIF、内容和权利检查；Provider 只能读取受控派生文件。
7. 相同文件的不同 Binding 保持独立；客户之间不能查询 hash 命中或建立引用。
8. 人物 latest/pinned 两种解析可重放；预览后人物变化会使提交失败并要求重新预览。
9. 模板 Apply 是深拷贝；模板升级只通过显式 diff/merge 创建新 Draft。
10. CreativeBrief、MustMatchRequirement、Token 和组合约束阻断无效发布及付费调用。
11. Prompt 三层组装顺序、锁定短语恢复、未知 Token 拒绝和最终快照可确定性重放。
12. 多数据源按 TruthResolutionPolicy 解析；凭据轮换不使 Truth 失效，Offer/事实变化只影响真实依赖。
13. Manual Fact 导入逐行校验 Decimal、币种、市场、Offer、有效期和证据，且只能先进入 Draft。
14. ResolutionPreview hash 覆盖所有依赖；任何版本、资格、能力或 Quote 漂移都会阻止旧预览提交。
15. Worker 崩溃恢复后继续使用同一 GenerationRecipeSnapshot，不重新解析 current version 或重复成本。
16. Supersede、Archive、Withdraw、连接失败和凭据撤销产生各自正确的影响，不用一个状态覆盖。
17. DimensionConceptKey 只在显式映射和可比上下文下聚合，标签相同不会自动跨产品学习。
18. Bind、Fork、Apply、Clone 保存不同谱系；Clone 永不复制 Offer、Mapping、价格或 Landing Page，请求带上它们时被拒绝（ADR 0016）。
19. 导入不覆盖、不自动发布；导出与 Manifest 不含凭据、签名 URL 和跨租户标识。
20. Mock 和真实路径不能混用、转换或静默回退；页面与产物均显示 provenance。
21. Playwright 从空 Workspace 完成产品、人物、参考图、模板、Prompt Policy、人工数据源、发布、预览与 Mock GenerationRun，并验证刷新和重启后状态一致。

## 20. 明确不做

- 不把生成素材或 Winner 移入创意库所有权。
- 不允许 Published Version 直接编辑或由“最新记录”改写历史任务。
- 不按名称、中文标签、文件名或内容 hash 推断业务身份。
- 不自动把模板、人物或跨产品学习更新推送进产品。
- 不跨客户共享或活引用产品、人物、参考图、规则、数据源和商业事实。
- 不允许任意 JSON、未知 Prompt Token、第一 Variant、默认数据源或明文凭据进入执行路径。
- 不在 v9 实现多人实时编辑、公开模板市场、KOL CRM、达人授权采购或跨客户学习。
