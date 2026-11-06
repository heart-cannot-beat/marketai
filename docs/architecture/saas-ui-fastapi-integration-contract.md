# SaaS UI 与 FastAPI 集成契约

状态：Accepted  
定案日期：2026-08-25  
适用范围：v9 功能对齐及邀请制自助 Beta

## 1. 目标

本契约规定 BoxyHQ/Next.js SaaS 控制面如何接入 MarketAI FastAPI 领域服务。目标是在不复制领域规则和持久化的前提下，为客户提供带 Workspace 隔离的六个 v9 功能区，并让身份、权限、异步任务、文件、错误和部署兼容性都具有可自动验证的生产边界。

本契约服从以下既有决策：

- SaaS 控制面拥有用户、Workspace 成员关系、邀请、订阅、管理后台和可撤销会话。
- FastAPI 与 Worker 独占 MarketAI 领域状态、规则、成本账本和外部副作用。
- PostgreSQL TenantContext 与 RLS 是托管环境的最终租户隔离边界。
- 客户真实 Meta 写入不属于 v9 功能对齐或 Beta 范围；受控 Mock 必须经过真实领域路径。

## 2. 拓扑与部署单元

代码放在同一仓库，但形成三个独立构建、部署和运行的 Implementation：

| 部署单元 | 职责 | 禁止事项 |
|---|---|---|
| Next.js / BoxyHQ Web | 登录、Workspace 路由、成员与管理界面、六区 SaaS UI、同源 BFF | 不连接领域数据库，不实现领域规则，不执行外部副作用 |
| FastAPI Domain API | 领域命令和查询、策略校验、Operation 投影、签名文件授权 | 不拥有登录会话、邀请、订阅或成员关系真值 |
| Worker | 持久化任务、租约恢复、成本结算、Provider/Meta Adapter 调用 | 不自行选择 Workspace 或伪装成人类用户 |

Web、FastAPI、Worker 使用独立进程、镜像、服务身份和数据库权限。控制面数据库与领域数据库分开迁移和授权；同一 PostgreSQL 集群部署时仍必须使用不同 schema owner 和运行角色。

## 3. 用户入口与 Workspace 路由

六个功能区由新的 Next.js SaaS UI 一次性重写：

| 路由 | 功能区 |
|---|---|
| `/w/{slug}/generate` | 新建生成 |
| `/w/{slug}/assets` | 素材 |
| `/w/{slug}/experiments` | 投放实验 |
| `/w/{slug}/localization` | 本地化 |
| `/w/{slug}/scale` | 放量 |
| `/w/{slug}/library` | 创意库 |

`slug` 只用于人读路由。每个页面请求和 BFF 调用都必须在服务端把它解析为控制面拥有的不可变 Workspace UUID，并重新验证当前会话的活动成员关系。浏览器提交的 slug、UUID、角色或用户 ID 都不是授权依据。

旧 `static/` 立即冻结，不再增加功能。新 UI 完成六区验收合同后原子切换；同一次发布移除旧运行入口。演示 HTML 仅保留为只读验收样本，不参与运行时和数据写入。

## 4. 唯一领域 Seam

FastAPI 的版本化 REST/OpenAPI 是 Web 与领域服务之间唯一的远程 Interface：

- 浏览器访问同源 BFF 前缀：`/api/domain/v1/*`。
- 多 Workspace 请求采用：`/api/domain/v1/w/{workspace_slug}/*`。
- BFF 调用私有 FastAPI 前缀：`/api/v1/*`；不转发 slug、用户提供的 Workspace UUID 或角色。
- FastAPI 只从已验证 JWT claims 建立请求 Actor 与 TenantContext。
- Server Actions 可以承载控制面自身操作，但不得成为第二套领域 Interface。
- GraphQL、浏览器直连 FastAPI、浏览器持有领域 JWT 和控制面直写领域表均不允许。

BFF Adapter 使用显式允许的 method/path 清单和固定 FastAPI origin。它只负责会话和成员关系校验、短期 JWT 签发、请求约束、`trace_id`/幂等头传递及响应透传；除了把私有 `Location` 机械映射为同源 BFF 地址外，不得改写领域响应。它不得聚合领域数据库、计算判定、修改成本或重新解释错误。

BFF 每次转发都附带 `X-UI-Language`（`en` 或 `zh-CN`，#255）。取值由 BFF 在服务端按用户保存的界面语言、`NEXT_LOCALE` Cookie、`Accept-Language` 的顺序决定，不转发浏览器自带的同名头。FastAPI 把它放进 `TenantContext.ui_language`，只用来选择本次请求写出的文字语言，例如导出包说明；缺失或未知值按 `en` 处理。它不进入 JWT claims，也不参与 Workspace、Actor、角色、RLS 或幂等判断。之后由 Worker 生成的文字，在命令受理时冻结语言。

## 5. 请求身份协议

一次领域请求的固定顺序是：

1. 浏览器携带 HttpOnly、Secure、SameSite 会话 Cookie 调用同源 BFF。
2. BFF 校验 Host、Origin 和 CSRF 防护，验证可撤销会话。
3. BFF 以路由 slug 查询 Workspace UUID，并验证活动成员关系及 Owner/Member 角色。
4. BFF 为这一次请求签发有效期不超过 60 秒的 ES256 JWT。
5. FastAPI 通过受信 JWKS 验证签名、算法、issuer、audience、时间和必要 claims。
6. FastAPI 从 claims 创建 Actor 与 TenantContext，随后执行服务端策略和 RLS。

委托用户 JWT 必须包含：

| Claim | 约束 |
|---|---|
| `iss` | 固定控制面 issuer |
| `aud` | 固定 MarketAI Domain API audience |
| `iat`, `nbf`, `exp` | `exp - iat <= 60s`，仅允许受控时钟偏差 |
| `jti` | 每次签发唯一，用于审计和重放观察 |
| `sub` | 不可变用户 ID |
| `workspace_id` | 服务端解析出的不可变 Workspace UUID |
| `membership_id` | 当前活动成员关系 ID |
| `role` | `owner` 或 `member` |
| `session_id` | 可撤销控制面会话 ID |
| `entitlement_version` | 控制面签发请求时看到的 Workspace 权益版本；不携带或替代具体权益 |

算法降级、缺失 claim、未知 key、错误 audience、过期、尚未生效或无活动成员关系均须 fail closed。领域 JWT 不进入浏览器存储、URL、分析事件或应用日志。控制面服务、委托用户和 Worker 使用不同 issuer/audience 或 token type，FastAPI 按调用类型应用不同策略。

FastAPI 仍以本地 Entitlement Projection 强制策略，JWT 中的 `entitlement_version` 只用于检测同步时序：claim 落后时采用本地较新投影；claim 超前时，读取可以继续，但新资源和副作用返回 `entitlement_sync_pending` 并触发完整 Snapshot 修复。JWT 不能携带功能布尔值或容量值来绕过本地 lease。

成员权限：

- Owner 管理成员、订阅、BYOK 凭据、成本硬上限以及未来获准的高风险真实写入。
- Member 可在 Owner 设定的硬上限内使用六个功能区，包括受控 Mock 和 BYOK 生成、审核、实验、本地化及放量建议。
- BFF 可以为体验预判权限，但 FastAPI 必须再次强制领域策略；隐藏按钮不是安全边界。

## 6. OpenAPI 与生成客户端

FastAPI OpenAPI 是领域 HTTP 契约的唯一真相源。

- 规范化后的 v1 文档提交到 `contracts/openapi/marketai-domain-v1.json`。
- Web 仅使用该文档生成的 TypeScript 类型和服务端客户端；业务组件不得散落手写 `fetch`。
- 生成物通过一个深的 `DomainGateway` Module 暴露小型、按用例命名的 Interface，隐藏鉴权头、超时、解析和传输细节。
- CI 从运行代码重新生成并比较 OpenAPI；存在未提交漂移时失败。
- CI 执行 breaking-change 检查；v1 允许新增可选字段和端点，不允许删除、改义、收窄枚举或把可选字段改为必填。
- 私有 FastAPI 先发布向后兼容变更，Web 后发布；至少兼容当前和上一个已部署 Web 版本，不要求锁步上线。

## 7. HTTP 语义

### 7.1 查询与命令

- 无副作用读取使用 `GET`，支持游标分页、稳定排序和过滤参数。
- 新建或触发命令使用 `POST`；局部状态迁移不通过通用 CRUD 绕过领域动作。
- 删除语义必须显式区分取消、归档和请求删除，不提供无条件通用 `DELETE`。
- BFF 与 FastAPI 都设置请求体、文件元数据、分页大小和响应大小上限。

### 7.2 幂等

可能产生外部调用、费用、任务或不可变记录的命令必须携带 `Idempotency-Key`。BFF 为一次用户意图生成并在网络重试时保留同一 key，FastAPI 将 key 与 Workspace、Actor、命令类型及规范化请求指纹绑定。

- 同 key、同指纹：返回原 Operation 或既有结果。
- 同 key、不同指纹：返回 `409 idempotency_key_reused`。
- Worker 的内部重试继续依赖持久化 Job、租约、指纹和成本预留，不把 HTTP key 当作唯一防线。

### 7.3 并发控制

可变聚合在响应中暴露 revision/ETag；更新命令使用 `If-Match`。版本不一致返回 `409 revision_conflict` 并携带当前 revision。追加式记录和不可变快照不伪装成可覆盖资源；冻结后修改必须创建新资源或显式取消。

### 7.4 超时与重试

BFF 对 FastAPI 的请求必须在领域 JWT 到期前结束；任何可能超过同步请求预算的工作都先持久化 Operation 再返回 `202`。BFF 只可自动重试幂等读取，或携带原 `Idempotency-Key` 且明确没有收到可判定响应的命令；它不得在收到领域错误后自行重放。连接、首字节和总时限分别配置，超时返回稳定的可重试 Problem Details，不把上游堆栈暴露给浏览器。

## 8. Operation 与后台任务

生成、批量本地化、实验运行、同步、导出及任何外部或长耗时动作返回 `202 Accepted`：

```http
HTTP/1.1 202 Accepted
Location: /api/domain/v1/w/acme/operations/op_...
Content-Type: application/json

{"operation_id":"op_...","state":"queued"}
```

Operation 是面向用户的持久化状态，不等同于单个 Worker Job。一个 Operation 可以投影多个 Job/RunItem，但浏览器不需要理解 Worker 拆分。允许状态为：

- `admitted` — 付费命令的入口状态：PaidOperationAdmission 已声明语义意图、创建 Jobs 并预留成本，但尚无 Worker 领取。非付费命令仍以 `queued` 进入。
- `queued` — 非付费命令的入口状态，等待 Worker 领取。
- `running`
- `partially_succeeded`
- `succeeded`
- `failed`
- `cancelled`
- `uncertain`

合法状态迁移：

| 源状态 | 可达目标 |
|---|---|
| `admitted` | `running`、`cancelled`、`failed`、`uncertain` |
| `queued` | `running`、`cancelled` |
| `running` | `succeeded`、`failed`、`partially_succeeded`、`cancelled`、`uncertain` |
| `uncertain` | `succeeded`、`partially_succeeded`、`failed`、`cancelled`、`running` |
| `succeeded` | — |
| `failed` | — |
| `partially_succeeded` | — |
| `cancelled` | — |

`admitted` 不能直接到达 `succeeded` 或 `partially_succeeded`：成功意味着调度，聚合器会先插入显式 `admitted → running` 事件再应用终态。`admitted` 进入 `cancelled`/`failed`/`uncertain` 覆盖的场景：授权 epoch 被撤销、Worker 领取前取消、以及调度结果不确定。

状态迁移必须符合显式状态机并追加审计历史，不得静默倒退；`uncertain` 只能通过对账进入确定结果，不能按普通失败盲目重试。确定终态不会因刷新或重启消失。首版使用带退避的 HTTP 轮询，响应提供 `ETag` 和适用的 `Retry-After`。没有证据表明轮询不足前，不引入 WebSocket；未来若需要服务端推送，SSE 只能作为同一 Operation Interface 的附加 Adapter。

## 9. 错误契约

非成功响应使用 `application/problem+json`，遵循 Problem Details 并增加稳定扩展字段：

```json
{
  "type": "urn:marketai:error:revision-conflict",
  "title": "Resource changed",
  "status": 409,
  "detail": "Reload the latest version before retrying.",
  "instance": "/api/v1/experiments/exp_.../freeze",
  "code": "revision_conflict",
  "params": {},
  "trace_id": "tr_...",
  "retryable": false,
  "field_errors": []
}
```

前端只根据 HTTP status、稳定 `code`、`retryable` 和 `field_errors` 决定交互，不解析中英文 `detail`。`code` 必填，同一含义复用同一 code，新 code 命名 `<module>_<reason>`；可选的 `params`（以及 `field_errors[].params`）携带文案插值所需的值（如 `{"limit": 16000}`）。界面按界面语言渲染 `error-<code>` 文案（en 与 zh-CN 必须同时存在），`detail` 只用于日志与审计，界面不显示；Worker 与任务记录里的 `error_code`、CSV 预检与投放结果导入的逐行 issue（`code` + `params`，#255）使用同一张错误码表。认证失败、权限不足、资源不存在、租户不匹配、幂等冲突、成本受限、Provider 暂时失败和不确定外部结果必须拥有不同错误码；不得用 `404` 掩盖服务端审计中的跨租户拒绝事实。

## 10. 文件 Seam

业务元数据和文件授权仍通过 BFF/FastAPI；图片和导出包的二进制不经过 Next.js 内存中转。

1. 浏览器请求创建上传或下载会话。
2. FastAPI 验证 Actor、Workspace、文件用途、MIME、大小和成本策略。
3. `BlobStore` Interface 返回仅针对单个对象、短时有效的签名 URL。
4. 浏览器直传或直下私有对象存储。
5. 上传完成后浏览器提交 finalize 命令；FastAPI 校验对象 metadata、大小、checksum 和 Workspace 绑定后才创建领域引用。

生产使用 S3 兼容私有对象存储 Adapter；本地开发使用本地文件 Adapter。两种 Implementation 必须通过同一契约测试。对象 key 由服务端生成，不接受浏览器提供的任意路径；日志不记录签名 URL。

## 11. 前端状态与 Locality

- Next.js 服务端布局负责会话、Workspace 解析、导航和初始权限边界。
- 六区交互沿用控制面已安装的 SWR，并通过深的 domain hooks 隐藏缓存实现；缓存 key 必须含 Workspace 路由身份。
- 筛选、排序、分页和可分享视图进入 URL；领域状态不复制到全局 Store。
- FastAPI 持久化状态是刷新、重开标签页和进程重启后的唯一真值。
- 页面必须实现验收合同要求的空、加载、排队、运行、部分成功、成功、失败、可重试、权限不足和成本受限状态。

## 12. 可观测性与审计

BFF 接收或生成 `trace_id`，并在整个调用链保留：

`trace_id -> Workspace -> audit actor -> initiator -> Operation -> Job -> Provider call`

结构化日志不得记录会话 Cookie、领域 JWT、BYOK 明文、签名 URL、完整 Prompt 中的敏感字段或原始素材。浏览器只向用户展示可复制的 `trace_id`；审计记录保存双 Actor、策略结果、幂等 key 哈希、成本变化和外部调用引用。指标至少按服务、路由、状态码、Operation 状态、队列延迟和 Provider 分类，但不得把 Workspace ID 作为无限基数指标标签。

## 13. 验收门

此架构只有在以下证据全部存在时才算实现，而不是因为文档已定案：

1. 新 Web、FastAPI、Worker 和测试 PostgreSQL 可由一条开发命令启动。
2. Owner 与 Member 可登录并进入各自有权访问的 Workspace。
3. Playwright 在两个 Workspace 上覆盖六区跨区黄金路径；外部服务仅替换为可控 Adapter。
4. 负向测试证明跨 Workspace 读取、写入、对象下载和 Operation 查询均被拒绝，并在审计中可见。
5. 成员撤销、Workspace suspended 和 auth lease 过期会阻止新外部或付费副作用。
6. 重放相同命令不会重复生成、重复创建任务或重复成本结算；并发冲突可恢复。
7. API/Worker 重启后 Operation 继续，付费调用中断进入 `uncertain` 而非盲目重试。
8. OpenAPI 生成、漂移和 breaking-change 检查进入 CI。
9. 安全检查证明领域 JWT 不出现在浏览器、日志、错误跟踪和分析事件中。
10. 六区通过《v9 功能对齐验收合同》，随后旧 `static/` 运行入口才可删除。

## 14. 明确不做

- 不把 BoxyHQ ORM 扩展成第二套 MarketAI 业务后端。
- 不在 BFF 聚合或重算领域规则。
- 不用公开对象 URL 或 Next.js 二进制中转替代文件授权。
- 不用 WebSocket、GraphQL、微前端或共享数据库换取尚未证明需要的复杂度。
- 不把受控 Mock 当成前端假数据。
- 不因完成 SaaS UI 就宣称 Gate 3 或客户真实 Meta 写入完成。
