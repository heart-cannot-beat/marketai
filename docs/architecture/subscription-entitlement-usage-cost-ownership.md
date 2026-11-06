# 订阅、权益、用量与成本责任契约

状态：Accepted  
定案日期：2026-08-25  
适用范围：邀请制自助 Beta、首个正式付费版本及其演进边界

## 1. 目标与不变量

本契约规定 SaaS 控制面与 FastAPI 领域服务如何划分订阅、套餐权益、BYOK 用量、受控 Mock、成本账本、Workspace 硬上限、Beta Access Grant 和 Billing Provider 事件的责任。

以下不变量优先于具体 Provider 或价格：

1. Subscription 和 Beta Access Grant 属于 Workspace，不属于 User。
2. SaaS 控制面拥有商业关系；FastAPI 独占领域授权执行、Usage 与 Provider 成本。
3. Billing、Entitlement、Usage 和 Provider Cost 是四种不同真值，不得共用一个余额或账本。
4. BYOK 费用由客户直接承担；MarketAI 首个收费模型不转售 AI 图片、Token 或平台代付额度。
5. 可能产生费用的命令必须在外部调用前原子预留，任何页面检查都不能替代服务端强制。
6. 受控 Mock 的真实应付金额恒为零，模拟估算不得进入真实支出、订阅账单或真实经营指标。
7. 订阅失效、权益投影过期或 Workspace 受限时 fail closed 于新资源和新外部副作用，但保留安全读取、导出、对账与必要结算。

## 2. 权威边界

| 真值 | 权威服务 | 主要记录 | 禁止事项 |
|---|---|---|---|
| Billing | SaaS 控制面 | BillingAccount、付款、退款、发票、税务、Billing Provider Mapping | FastAPI 不接收 Stripe Webhook，不从支付跳转推断付款 |
| Commercial Access | SaaS 控制面 | PlanVersion、Subscription、AccessGrant、Restriction、有效周期 | 不允许浏览器或领域服务自行提升权益 |
| Entitlement Enforcement | FastAPI | 带 lease 的 Entitlement Projection、容量预留 | 不直接查询 Stripe，不永久信任过期投影 |
| Usage | FastAPI | 追加式 UsageEvent、周期投影 | 不把页面统计作为额度真值，不因周期结束删除历史 |
| Provider Cost | FastAPI | Quote、Reservation、Reported Cost、Estimated Settlement、Release、Adjustment | 不与订阅付款互相冲销，不把估算冒充实际账单 |

控制面和领域服务可以为管理报表建立只读组合投影，但任何组合投影都不能反向写入上述权威记录。

## 3. 商业模式

### 3.1 邀请制 Beta

- Beta 免费、邀请制、不绑卡、不设置倒计时，不称为 Trial。
- 平台向 Workspace 签发 Beta Access Grant；默认无到期时间，但可以由平台明确撤销或叠加 Restriction。
- Beta Access Grant 保存 `period_anchor`，从生效时间起形成连续月度 Usage 与 Spend Guardrail 周期。
- Beta 不设置月度图片积分；记录真实 Usage，以数据支持后续定价。

### 3.2 首个正式付费版本

- 按 Workspace 固定套餐收费，套餐可以限制功能、成员、Provider 并发和存储。
- 客户使用自己的 BYOK 凭据并直接承担 Provider 费用。
- 不销售预付 AI credits，不按 Provider Token 或图片用量转售费用，不承诺 Provider 退款。
- 正式价格在至少 5 个活跃客户 Workspace、连续 30 天真实 Usage、资源与支持负担分布以及一轮付费意愿访谈完成后再定案。
- 价格金额和 Billing Provider Price ID 不写入领域业务逻辑。

## 4. 商业访问模型

### 4.1 PlanVersion

控制面允许编辑套餐 Draft；发布动作创建不可变 PlanVersion。已发布版本只能停用以阻止新绑定，不能改写已有 Workspace 的历史权益。

PlanVersion 使用稳定 entitlement key，例如：

- `module.generate.enabled`
- `module.assets.enabled`
- `module.experiments.enabled`
- `module.localization.enabled`
- `module.scale.enabled`
- `module.library.enabled`
- `members.max`
- `provider_calls.concurrent.max`
- `storage.bytes.max`

发布需要平台运营权限和审计（MFA 只在登录时验证，见 ADR 0015）。Stripe Price ID 只存在于 Billing Provider Mapping；它不是 PlanVersion ID。

### 4.2 Subscription、AccessGrant 与 Restriction

- Workspace 至多有一个当前基础商业访问来源：有效 Subscription 或基础 AccessGrant。
- 临时促销或支持赠送使用附加 AccessGrant，必须包含来源、原因、开始时间、可选结束时间和签发主体。
- Restriction 用于安全、欠费、滥用或合规限制，只能禁止功能或降低上限，不能提升权益。
- 最终权益按“平台绝对上限 → 基础 PlanVersion → 附加 AccessGrant → Restriction”的顺序解析；Restriction 始终优先。
- 禁止直接编辑最终 Entitlement Snapshot 或通过客服脚本修改数据库字段。

### 4.3 周期

- Beta 周期以 Access Grant 的 `period_anchor` 为起点按月重复。
- 付费周期以 Billing Provider 已确认的 Subscription period 为准。
- Beta 转付费时从已确认付费周期开启新投影，不追溯重算 Beta Usage 或 Provider Cost。
- 周期结束只关闭投影并创建下一投影；UsageEvent、成本账本和审计历史永不因“重置”而删除。

## 5. Subscription 生命周期

| 状态或事件 | 访问行为 |
|---|---|
| Beta Access Grant active | 按绑定 PlanVersion 正常访问 |
| Subscription active | 按已确认 PlanVersion 正常访问 |
| 支付失败宽限期 | 7 天内保留原权益并持续提示 Owner |
| 宽限期结束 | 进入只读；停止新任务、上传、邀请和外部调用 |
| 主动取消 | 当前已付周期结束时进入只读 |
| 已付款升级 | 已验证 Billing 事件完成投影后立即生效 |
| 降级 | 当前周期结束时生效；提前保存下一 PlanVersion |
| Restriction | 可立即降低或撤销相应权益 |
| Workspace suspended/deletion_pending | Workspace 生命周期限制优先于商业权益 |

Checkout success 页面只显示付款确认中，并轮询控制面状态；浏览器跳转、query 参数或 Checkout ID 均不构成开通证据。

进入只读不会删除客户数据。查看、下载和导出保持可用；数据删除仍遵循 Workspace 的 30 天恢复期。若降级后成员或存储已超限，不自动踢人或删除对象，只禁止新增，直到 Owner 恢复到允许范围。

## 6. Billing Provider Seam

控制面定义 Billing Provider Interface，Stripe 是首个 Adapter。内部 Subscription、PlanVersion、BillingAccount 和 Workspace ID 不使用 Stripe ID 作为主键。

### 6.1 Webhook Inbox

1. 在原始请求体上验证 Stripe 签名、时间容差和 Endpoint Secret。
2. 以 Provider account 和 Event ID 建立唯一键，脱敏并持久化 Inbox 后才返回成功。
3. 异步处理事件；重复投递返回成功但不重复改变状态。
4. 不按到达顺序覆盖 Subscription；根据 Provider 对象版本、时间和必要的 API 读取重建最新投影。
5. 单个事件处理超过 5 分钟告警；失败进入可观察的重试状态和死信处置流程。

### 6.2 主动对账

- 每 6 小时对账 active、宽限期、欠费和近期发生变化的 Subscription。
- 每日对全部 Subscription 做一致性抽查，并核对长期未处理 Inbox。
- Webhook 和主动对账必须汇聚到同一个幂等 Subscription 状态迁移 Interface。
- Provider outage 不得把未知状态推断成已付款；已持久化的有效权益按 lease 规则继续。

Billing Provider 的版本升级、事件类型增加和 Price Mapping 变化需要受审查配置，不允许在运行时静默跟随 Provider 最新值。

## 7. Entitlement Snapshot 传播

控制面在同一事务中更新 Commercial Access Projection 并写 Transactional Outbox。Dispatcher 使用独立控制面服务身份调用 FastAPI 私有幂等接口，领域服务将消息先写 Inbox 再更新 Entitlement Projection。

每份 Snapshot 至少包含：

- `workspace_id`
- 权益变化时递增的 `entitlement_version`
- 每次发布或续租都递增的 `lease_sequence`
- `plan_version_id`
- 基础与附加商业访问来源的不可变 `source_refs`
- `period_start`、`period_end`、`period_anchor`
- 解析后的稳定 entitlement key/value
- Restriction 来源摘要
- `issued_at`、`valid_until`
- 控制面审计事件 ID

应用规则：

- 相同 `entitlement_version` 与 `lease_sequence` 的重复消息幂等成功。
- 较旧的权益版本或租约序号忽略并记录审计。
- 权益内容不变的续租保持 `entitlement_version`，只提高 `lease_sequence` 和 `valid_until`；它只能延长相同权益，不能借续租改变 key/value。
- `entitlement_version` 出现跳跃时暂停提升权益，请求完整 Snapshot 后修复。
- lease 为 24 小时，控制面至少每 6 小时续租，即使权益没有变化也要刷新有效期。
- lease 过期后允许读和导出，但暂停新领域资源、上传、外部调用和费用预留。
- 周期性全量修复保证 Outbox 长期故障后仍能收敛；Beta 不为此引入 Kafka 或共享数据库。

Worker 在每个新的外部或付费步骤前重新检查 Workspace 生命周期和 Entitlement lease。尚未调用 Provider 的任务暂停或取消；已经发出的不可分割调用允许返回，并必须保存结果、失败或 `uncertain`，完成成本结算或对账。

## 8. Beta 默认权益

Beta PlanVersion 初始值：

| 权益 | 默认值 |
|---|---:|
| 六个 v9 功能区 | 全部启用 |
| Owner + Member 总数 | 5 |
| 同时真实 Provider 调用 | 2 |
| 私有素材存储 | 10 GiB |
| 月度生成数量 | 只计量，不设硬积分 |
| 受控 Mock | 启用，且必须显式标识 |

有效待接受邀请预留一个席位；邀请过期或撤销后释放。降级造成成员超限时不自动移除现有成员，但禁止新邀请、接受邀请和角色提升。API 请求速率、异常流量和平台总负载保护属于安全策略，不包装为套餐卖点。

## 9. Usage

UsageEvent 是追加式事实，至少包含 Workspace、稳定 metric key、数量/单位、发生时间、来源资源、execution mode 和幂等 key。首批 metric key 覆盖：

- Provider 调用次数
- 成功与失败的 AssetVersion 数
- Mock Operation 数
- 私有存储字节数
- 活跃成员和预留邀请数
- Provider 调用并发峰值
- Operation 数量与处理时长

成员和邀请由控制面事务强制；Provider 并发、存储和成本由领域事务或容量预留强制。Dashboard 使用可重建周期投影，最多允许延迟 5 分钟，但任何最终一致投影不得作为放行超限命令的依据。

Usage 不等于 Provider Cost，也不自动形成 Stripe 用量账单。引入任何硬月度生成额度或用量计费前必须重新定案，而不能复用当前统计字段静默收费。

## 10. Provider Cost Ledger

### 10.1 证据类型

| 类型 | 含义 |
|---|---|
| Quote | 提交前基于冻结配方、模型和版本化价格表形成的成本报价 |
| Reservation | 外部调用前原子占用的最大成本暴露 |
| Release | 任务结束、取消或缩减后释放未使用的预留 |
| Reported Cost | Provider 返回 usage 证据后，按冻结价格规则计算的金额 |
| Estimated Settlement | Provider 没有 usage 证据时，依据冻结报价形成的估算结算 |
| Adjustment | 后续对账对既有结算追加的正向或负向修正 |

现有代码在 Provider 未返回 usage 时写入的 `actual` 语义必须在实现阶段迁移为 Estimated Settlement；历史不得原地覆盖。最终发票金额只有在获得相应 Provider 对账证据后才能作为更高等级证据引用。

账本金额保持非负 Decimal；Release 和 Adjustment 使用显式 `direction` 表示增加或减少暴露，不用负数金额绕过数据库约束。每条 Adjustment 必须引用被修正记录、对账证据和审计主体。

### 10.2 多币种

- 保存 Provider 原币金额和币种。
- 同时保存 Workspace 预算币种下的折算金额、汇率、来源、观察时间和价格规则版本。
- Reservation 使用保守汇率与缓冲，Settlement 释放差额或追加调整。
- 历史金额永不按新汇率重新计算。
- 修改 Workspace 预算币种只限 Owner，且存在未结算 Reservation 时不得生效。

### 10.3 Mock

- Mock 可以被活动 Workspace 显式选择；没有 BYOK 时 UI 可以推荐，但提交前必须明确展示。
- Provider 失败不得静默回落 Mock。
- Mock 真实应付金额为零，不占用 Spend Guardrail。
- 模拟报价可以单独记录并展示，但必须带 execution mode 和场景，不能聚合到真实支出。

## 11. Spend Guardrail

Beta 默认 Workspace Guardrail，以 Workspace 预算币种计：

| 窗口 | 默认上限 |
|---|---:|
| 单次 Operation | 5 |
| 滚动 24 小时 | 20 |
| 当前 Access Grant/Subscription 周期 | 100 |
| 同时未结算暴露额 | 10 |

平台另有不可被客户突破的紧急安全上限。Owner 可以调低；提高需要服务端 Preview、二次确认和审计，且不能超过平台上限（ADR 0015：不再要求 MFA step-up）。降低立即影响新 Reservation，但不伪装成对已发出 Provider 调用的撤销；若当前暴露额已经超过新上限，所有新 Reservation 都被拒绝，Owner 可以另行取消尚未开始外部调用的任务。

一次副作用命令的固定顺序：

1. 校验 Workspace 生命周期、Actor、角色和 Entitlement lease。
2. 解析幂等 key；重放相同意图时复用原 Operation。
3. 强制功能、并发、存储和其他 Plan Entitlement。
4. 冻结 Provider、模型、价格、汇率和 Quote。
5. 在同一领域事务中检查全部 Guardrail、创建 Reservation、容量占用、Operation 和 Job。
6. Worker 在每个外部调用边界重新校验 lease 与 Reservation 后执行。
7. 以 Reported Cost 或 Estimated Settlement 结算并释放余额；不确定结果进入 `uncertain` 对账。

并发命令必须争抢同一数据库保护的额度，不能分别查询后各自通过。页面展示的剩余额度只是投影，服务端拒绝结果才是最终裁决。

## 12. 客户可见性与通知

- Member 可以查看当前功能权益、Usage、剩余资源、Spend Guardrail 空间及成本证据类型，但不能修改设置或查看付款方式、发票和凭据。
- Owner 额外管理套餐、账单、付款、成员、BYOK 和 Spend Guardrail。
- 页面必须区分 Mock、Reported Cost、Estimated Settlement 和尚未结算 Reservation。
- 任何适用上限达到 80% 时显示站内预警并通知 Owner；达到 100% 时原子拒绝新预留。
- 系统不得自动提额、自动升级套餐或为了完成任务绕过 Restriction。

## 13. 删除与法定保留

Workspace 进入 deletion_pending 后停止新任务和外部调用；30 天恢复期结束后删除凭据、素材、Prompt、领域内容和可识别用户信息。法律、税务或争议处理所需的 Billing 记录与必要成本金额可以按部署地区政策独立保留，但必须去标识化，只引用不可逆 Workspace 伪名，且不能用于恢复客户内容或访问领域数据。

数据保留期限必须是部署配置和合规决策，不能由 Stripe 默认值或应用日志保留周期隐式决定。

## 14. 实现验收门

本契约只有在以下自动化证据存在时才算实现：

1. 两个 Workspace 的 Subscription、Access Grant、Restriction 和 Entitlement Projection 不可串租户。
2. 重复、延迟、乱序和伪造 Billing Webhook 不会重复开通或回退到旧状态。
3. Checkout success 跳转不能提前提升权益。
4. Outbox 重投、版本跳跃、控制面宕机和 lease 过期均按契约收敛或进入只读。
5. 宽限期、取消、升级、降级、成员超限、存储超限和 Restriction 有正负向测试。
6. 并发请求争抢同一 Guardrail 时只有原子预留成功的命令可以入队。
7. Worker 在权益撤销、崩溃和外部调用不确定时不会重复付费。
8. Reported、Estimated、Adjustment、Release、多币种和汇率历史可以重放对账。
9. Mock 的真实成本为零，且不能进入真实成本、Billing、指标或实验结论。
10. Owner/Member 的页面可见性和服务端权限均有 Playwright 及 API 负向测试。
11. Workspace 删除后领域内容不可恢复，保留的财务证据无法反查个人或素材。

## 15. 参考资料

- [Stripe：使用 Webhook 管理订阅](https://docs.stripe.com/billing/subscriptions/webhooks)
- [Stripe：Entitlements 与内部持久化建议](https://docs.stripe.com/billing/entitlements?dashboard-or-api=api)
- [Stripe：Webhook 签名验证](https://docs.stripe.com/webhooks/signature)

## 16. 明确不做

- Beta 不启用 Checkout、绑卡 Trial、正式价格或用量计费。
- 不把 BYOK Provider 支出作为 MarketAI 订阅收入。
- 不共享控制面和领域数据库，不让 FastAPI 直接消费 Stripe Webhook。
- 不把 Stripe Entitlements 当作唯一运行时授权源；控制面仍解析 MarketAI PlanVersion 并向领域传播。
- 不为 Beta 引入 Kafka、复杂 credits 钱包、平台代付余额、自动超额计费或自动提额。
- 不用全局环境变量继续充当多 Workspace 的 Owner Guardrail；迁移完成前它们只可作为平台级紧急上限。
