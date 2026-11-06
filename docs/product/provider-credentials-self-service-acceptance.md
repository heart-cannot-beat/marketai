# 图片供应商自助密钥配置验收

日期：2026-10-17。范围是 Workspace 设置中的 OpenAI、Google Gemini、Seedream（火山方舟）密钥管理。真实供应商调用等取得 Key 后另验，不发布广告。

## 已实现

Owner 在 **Settings → Provider credentials** 的三个独立区域添加、启用、替换和移除密钥。三个区域共用已有受保护的凭据命令，每次写操作仍要求 Owner 和对应的一次性 MFA step-up；绑定范围为 `generation`。没有修改数据库、加密机制、权限或生成费用规则。

- 创建使用独立的供应商身份与凭据谱系：OpenAI、Gemini、Seedream 不混用；已有自定义凭据优先展示实际绑定版本。替换沿用被选凭据的谱系，不另建重复 Key。
- 密钥输入标注供应商专属名称、`new-password` 及密码管理器忽略提示，减少复用登录密码的机会；这些提示不能保证所有浏览器/插件行为。
- 确定已保存后立即清空输入和表单，并清理命令 Hook 中的明文请求参数；页面只显示标签、版本、指纹和绑定状态。取消及切换工作区也不会带入未提交的密钥或 MFA 状态。
- 保存成功、启用失败时可对已保存版本补做启用，不再次发送密钥；保存之后若启用需要新 MFA，验证后继续绑定准确版本。
- 响应丢失或结果不明时清空输入、停止新的修改并要求重新加载状态，不自动重复创建/替换。重复提交表单和 MFA 表单不会重复发送同一正在执行的动作。
- “Key enabled for generation”只表示当前绑定准确；页面明确保存不会调用供应商或证明 Key 有效，生成仍需要对应模型最大报价、预算和原有费用确认。

## 分层验收

| 层次 | 已验行为 | 边界 |
| --- | --- | --- |
| 三家组件 | 各自创建/绑定、替换/绑定、启用已保存版本、移除、错误响应、MFA 过期/未开通、保存后 MFA 继续启用、丢响应阻断、双提交、取消、跨供应商与跨工作区状态隔离 | 使用无效测试字符串与受控命令/认证响应，没有真实 Key 或供应商请求 |
| 真实领域 HTTP/持久化 | 三家独立谱系、旋转后旧绑定失效、新版本显式绑定、移除后不可用；Member、跨工作区及供应商不匹配被拒；响应不含明文或其 Base64 | 身份采用隔离测试上下文；不冒充真实浏览器 MFA 全生命周期验收 |
| 现有保护契约 | Owner 页面准入、MFA grant/issuer、受保护动作集合、请求身份及命令清理、加密/AAD 与凭据输出保护 | 未放宽既有访问或加密边界 |
| 原生本地页面 | 真实登录/BFF/API 读取；三家添加入口、密码型字段、来源说明、空值禁用保存、键盘取消、刷新后无未提交表单 | 没有向原生页面输入或提交任何密钥；不将空表单检查称为真实 Key 的生命周期验收 |
| 七种布局 | 320×740、360×800、390×844、768×1024、1024×768、1440×900、844×390；三家表单及关键控件无整页/控件横向溢出 | 浏览器尺寸模拟，实机和其他浏览器内核仍未验收 |

最终定向前端 **9 组、157 项**通过（其中三家配置组件 57 项）；后端 **83 项及 11 个子场景**通过。类型、定向 lint 与生产构建通过。专项相互重叠，不累加为全仓测试，也没有重跑旧营销全闭环。

私有机器回执与截图在私有仓库，不在本仓库，不提交客户材料或配置。代码与生产部署身份见 [上线记录](ai-marketing-production-release.md)。

## 生产复验

Web `c2859db2` 已成功上线，API/Worker 未部署。正式登录后的三个供应商区正常读取；Gemini、Seedream 空表单在桌面和手机视口通过输入类型、来源、禁用空提交、取消、刷新及横向边界检查。没有输入/提交任何 Key。生产凭据与绑定元数据摘要前后一致，原 OpenAI 未变，两家仍未配置。正式工作区里真实客户产品的原导出与调用记录不变，仍无广告关联或回传批次。

## 仍待真实验收

Gemini/Seedream 的真实 Key、供应商鉴权、模型可用性、最大成本配置及真实附图输出继续待验。没有为生产工作区填写假 Key，没有修改已有正式 OpenAI 密钥，没有新增付费模型调用，也没有 Meta 写操作。先前 [供应商/设备/故障记录](provider-device-fault-acceptance.md) 中“自助入口待补”的缺口由本次补齐；真实调用暂停这一边界保留。

## 竞品扫描数据供应商（ScrapeCreators，2026-10-28，#267）

范围是竞品扫描（Epic #265 的 C-11）所需的数据供应商密钥：`binding_scope=market_data`、provider `scrapecreators`，以及每次真实解析与扫描报价所用的价格表。没有真实 ScrapeCreators Key、没有真实供应商请求；真实调用留给 C-50 由用户确认费用后进行。

### 已实现

- **独立入口：** Settings → Provider credentials 新增第四个区域“ScrapeCreators（竞品广告数据）”，与 OpenAI、Gemini、Seedream 并列，同样支持添加、启用、替换、移除，复用同一套受保护凭据命令、Owner 与一次性 MFA step-up、加密与隔离规则。凭据谱系为 `scrapecreators_api_key`。组件不再只有一个 `generation` 作用域：每个供应商各自声明作用域，ScrapeCreators 绑定 `market_data`。
- **保存即检查：** 创建与替换 ScrapeCreators Key 时，领域 API 先用本次请求里的明文调用免费的 `GET /v1/account/credit-balance`（请求头 `x-api-key`，5 秒超时，不跟随重定向、不读代理环境）。该接口对无效 Key 也返回 200，所以只以 `creditCount > 0` 判定可用：
  - `creditCount > 0`：保存，并把检查结果（`outcome`、`credit_count`、检查时间）作为追加审计事件 `market_data.credential_checked` 记在该版本上；
  - `creditCount = 0`、401/403 或 Key 含请求头不能承载的字符：返回 422 `market_data_credential_invalid`（“密钥无效或没有余额，未保存”），什么都不存；
  - 超时、网络错误、429、5xx 或读不出 `creditCount`：返回 503 `market_data_credential_check_unavailable`，什么都不存；
  - 被拒的检查也追加一条审计事件，资源是凭据谱系名（没有版本可指）。审计细节里没有 Key。
  - 选择在保存时检查，是因为 API 角色读不回已存密文（`deploy/postgres/domain-tenant-roles.sql`），保存是不经 Worker 就能检查的唯一时刻；替换失败时原版本保持生效与绑定。
- **状态：** `list_credentials` 每项新增 `bindings`（指向该版本的作用域与供应商）与 `last_check`（市场数据 Key 的最近一次检查，其他供应商为 `null`）。页面只在“已绑定 `market_data` 且最近检查有余额”时显示“已连接 · 上次检查剩余 N 额度”；已绑定但没有成功检查显示“已保存 · 未验证”并提示更换密钥重新检查；未绑定显示“已保存，未启用”。无效 Key 只显示“密钥无效或没有余额，未保存”，不会出现“已连接”。
- **作用域规则：** `market_data` 只接受市场数据供应商，ScrapeCreators 的 Key 也只能绑定 `market_data`；混用返回 409 `credential_scope_provider_mismatch`。绑定一个本 Workspace 没有的版本（不存在或属于其他 Workspace）现在返回 404 `credential_not_found`，与替换、移除一致；原先是 409 `credential_not_bindable`，此码仍用于已替换、已撤销或供应商不符的版本。
- **价格表：** `MARKETAI_MARKET_DATA_MAX_COSTS_JSON`，供应商 → 操作 → `{"credits": 1, "credit_usd": "0.00188"}`，操作为 `page_search`、`probe`、`page`、`keyword_page`、`ad_refresh`。`app.market_data.pricing.quote_market_data(provider, calls)` 返回额度、USD 与 `MARKETAI_PRICE_REGISTRY_VERSION`；价格表缺失、缺少任一操作或格式有误时，拒绝所有真实报价，错误码分别为 `market_data_prices_unconfigured`、`market_data_price_operation_missing`（`params.operations` 列出缺少的操作）、`market_data_prices_invalid`。受控 Mock 不需要价格表。解析（C-13）与扫描（C-21）的报价接口调用这一函数并原样返回这些错误码。
- **首次配置说明：** 尚未配置 Key 时，区域内说明扫描使用用户自己的 ScrapeCreators 账号与密钥、使用关系受用户与 ScrapeCreators 之间的协议约束、MarketAI 不转售也不代付额度（规格 §10）。
- **文案：** 入口文案在 `settings` 命名空间（en 与 zh-CN），按钮、状态与提示与生成那套分开（“用于竞品扫描”“检查并保存密钥”等）；新增错误码的 `error-<code>` 文案按现有机制在 `common` 命名空间，en 与 zh-CN 同时写。`detail` 只作英文日志诊断。
- **没有迁移：** provider 与 binding_scope 都是无约束的字符串列，检查证据写入已有的 `audit_events`；OpenAPI 文档也没有变化（`list_credentials` 的响应体本来就不定型）。
- **“移除”修复：** BFF 策略里 `revoke_credential` 的请求体上限原为 0 字节，而领域端要求 `{"reason": ...}`，所以真实页面上任何供应商的“移除”都在 BFF 被 413 `request_body_too_large` 拒绝（2026-10-17 的验收只在组件与领域 HTTP 层验过移除）。现改为 8192 字节，与另外三个凭据命令一致，并有测试防回归。

### 分层验收

| 层次 | 已验行为 | 边界 |
| --- | --- | --- |
| 价格表 | 完整表的额度与 USD、价格版本、多额度操作；缺表、缺供应商、空表；缺任一操作时连已定价操作也拒绝；JSON、额度、单价格式错误；未知操作与负数调用次数是调用方错误 | 只验证部署配置的解析，不代表供应商现价 |
| 余额检查 | 请求地址与 `x-api-key` 头、Key 不进 URL 与返回值；`creditCount` 为正 / 为 0；401/403；429、5xx、非 JSON、缺少或非数字 `creditCount`、超时、网络错误；请求头不能承载的 Key 不发请求 | 全部走 `httpx.MockTransport`，没有网络请求 |
| 领域 HTTP / 持久化 | 有余额的 Key 保存并绑定 `market_data`、可被 `resolve_market_data_credential` 解析、不出现在生成能力里；无效与检查失败不存任何版本；替换为无效 Key 时原版本保持生效与绑定，替换成功后需重新启用；移除；重复创建不请求供应商；图片供应商的 Key 从不发往 ScrapeCreators；作用域与供应商混用被拒；Member 不能列出、创建、替换、启用、移除且不触发供应商请求；其他 Workspace 读不到列表与绑定，启用、替换、移除返回 404；响应与审计中没有 Key 或其 Base64 | 身份采用隔离测试上下文，step-up 在测试配置下关闭 |
| 组件 | ScrapeCreators 区与三家并列；独立提示、首次配置说明、检查并保存、绑定 `market_data`、无效 Key 与检查失败的文案、已连接 / 未验证 / 未启用三种状态、替换后重新绑定、状态不依赖生成能力读取；三家生成入口的提示、按钮、状态文案与 `generation` 作用域不变 | 受控命令与认证响应，没有真实 Key |
| 本机真实栈（compose） | 新 Owner 注册并开启 MFA 后打开 Settings：四个入口并列、首次配置说明可见；余额为 0 的夹具 Key 显示“The key is invalid or has no credits left, so it was not saved.”且无“Connected”；有余额的夹具 Key 经真实 BFF / API 保存、启用后显示“Connected · 2,500 credits at last check”，刷新后保持；390×844 与 320×740 无横向溢出；“移除”成功并回到首次配置状态 | API 的余额检查地址指向本机受控桩（按 Key 后缀答 0 或 2500），没有连 ScrapeCreators；截图存本机忽略目录 `data/provider-credentials-acceptance-2026-10-28/` |
