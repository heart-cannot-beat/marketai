# Authenticated Next.js Workspace Shell

状态：Accepted

定案日期：2026-08-31

适用范围：Issue #26；邀请制自助 Beta；六个 v9 功能区的 SaaS UI 壳

## 1. 目标

本规格定义登录用户在 `/w/{slug}` 下使用的 Next.js Workspace 壳，以及六个 v9
功能区共享的浏览器基础设施。它把现有 SaaS 控制面的会话和 Workspace 成员关系，
与 #17 已交付的同源 BFF、`DomainGateway`、Operation 和 signed-intent 文件 seam
组合成一个可恢复、可测试且不会绕过租户边界的前端运行环境。

Issue #26 交付：

- 六个 v9 路由、持久布局、导航和 Workspace 选择器；
- Owner/Member 账户与 Workspace 管理入口；
- 服务端页面准入和浏览器 WorkspaceContext；
- 只经过同源 BFF 的查询、命令和 Operation hooks；
- 空、加载、错误、权限不足、陈旧缓存和部分成功等共享操作状态；
- 通过 signed intent 的私有上传与下载；
- 路由级渲染错误恢复、键盘操作和焦点管理；
- 身份、Workspace 切换、导航、CSP 和键盘行为的 Playwright 证据。

## 2. 既有约束与不做事项

本规格服从 ADR 0001–0004、`CONTEXT.md`、
《SaaS UI 与 FastAPI 集成契约》和《v9 六区功能对齐验收合同》。以下约束不可由
前端实现弱化：

- SaaS 控制面拥有登录会话、Workspace、成员关系和 Owner/Member 角色；
- FastAPI 与 Worker 独占领域状态、规则、成本和外部副作用；
- Workspace slug 是人读别名，不是授权凭据；
- 浏览器只能调用 `/api/domain/v1/w/{slug}/...`，不能直连 FastAPI；
- BFF 为每次调用重新解析 slug、验证成员关系并签发短期委托身份；
- 持久化领域真值来自 FastAPI，浏览器状态只缓存和展示；
- 二进制直接经过私有对象存储，不经过 Next.js 或 FastAPI 内存中转。

本 Issue 明确不做：

- 不实现六区的真实业务查询、表单、命令和结果页面；它们属于 #27 和 #28；
- 不实现 PlanVersion、Entitlement 解析、BYOK、Usage 或 Provider Cost；它们属于 #18；
- 不迁移到 App Router，不引入 Server Actions 作为第二套领域 Interface；
- 不引入浏览器全局领域 Store、WebSocket、GraphQL 或通用开放代理；
- 不把 UI 隐藏、客户端 Workspace UUID 或缓存内容当成授权证据；
- 不新增远程 Operation 取消；当前 OpenAPI 没有 `cancel_operation`。

## 3. 框架决策

### 3.1 继续使用 Pages Router

控制面当前统一采用 Pages Router、`getLayout`、`getServerSideProps` 和 SWR。#26
继续沿用这些模式。迁移到 App Router 会同时改变会话加载、布局持久化、i18n、路由
错误和数据缓存，不产生本 Issue 所需的产品价值。

`WorkspaceLayout` 通过所有 Workspace 页相同的 `getLayout` 根元素在客户端导航间
保持挂载。页面内容变化不重新创建侧栏、Workspace 选择器或顶部账户区域。

### 3.2 使用 SWR

六区查询沿用仓库已经安装并使用的 SWR。hooks 隐藏 SWR 的实现细节，调用方只看到
领域结果和 `refresh()`，不直接依赖 `mutate`。这条决策取代集成契约中早期写入、但
从未落地的 TanStack Query 选择；集成契约同步更新为 SWR。

### 3.3 深模块与 seam

本设计把复杂度集中在以下模块接口后面：

| 模块 | 小型接口隐藏的行为 |
|---|---|
| `getWorkspacePageProps` | 会话、slug、成员关系、角色、状态码和 i18n |
| `WorkspaceNavigator` | 目标解析、角色回退、pending 状态和路由提交 |
| Domain hooks | Workspace cache key、typed result、CSRF、intent、轮询和陈旧缓存 |
| `useUpload` | 摘要、intent、对象存储传输、finalize 和 reconciliation |
| `useDownload` | intent、origin 校验、短期 capability 和浏览器导航 |
| Storage origin policy | 公共配置解析、浏览器 allowlist 与 CSP source list |

远端但由本项目拥有的 FastAPI/BFF 通过 `DomainGateway` seam 注入；对象存储传输、
文件摘要和浏览器导航也通过生产 adapter 与测试 adapter 在模块接口处验证。

## 4. 路由与布局

### 4.1 路由表

新增页面：

| Pages Router 文件 | 客户路由 | 行为 |
|---|---|---|
| `pages/w/[slug]/index.tsx` | `/w/{slug}` | 服务端重定向到 `products`（#162 起；此前为 `generate`） |
| `pages/w/[slug]/products/index.tsx` | `/w/{slug}/products` | 产品首页（前台两项配置合同 §3.1，#162） |
| `pages/w/[slug]/products/new.tsx` | `/w/{slug}/products/new` | 添加产品：粘贴 → 识别 → 产品卡确认（#162） |
| `pages/w/[slug]/tests/index.tsx` | `/w/{slug}/tests` | #337 起只读：全部测试方案（新的在前），顶部提示去「营销测试」，不再折叠「生成默认值」（只在 `settings`）；此前为：全部测试方案（新的在前）；折叠的「生成默认值」（Member 只读，#165） |
| `pages/w/[slug]/tests/new.tsx` | `/w/{slug}/tests/new` | #337 起：服务端重定向到 `experiments`（含 `?product=`，参数丢弃）；此前为：设置广告测试：三问带默认值、「按默认开始」；产品经一次性 sessionStorage 交接传入，不进 URL（#165） |
| `pages/w/[slug]/tests/[planId].tsx` | `/w/{slug}/tests/{planId}` | #337 起只读：方向卡片、样图与这一轮的结果，顶部提示去「营销测试」；此前为：方向卡片、先出样图 → 展开完整测试、按方向分组的结果（#165） |
| `pages/w/[slug]/generate.tsx` | `/w/{slug}/generate` | 新建生成壳 |
| `pages/w/[slug]/assets.tsx` | `/w/{slug}/assets` | 素材壳 |
| `pages/w/[slug]/experiments.tsx` | `/w/{slug}/experiments` | 投放实验壳 |
| `pages/w/[slug]/localization.tsx` | `/w/{slug}/localization` | 本地化壳 |
| `pages/w/[slug]/scale.tsx` | `/w/{slug}/scale` | 放量壳 |
| `pages/w/[slug]/library.tsx` | `/w/{slug}/library` | 创意库壳 |
| `pages/w/[slug]/settings.tsx` | `/w/{slug}/settings` | Owner-only Workspace 管理 |
| `pages/w/[slug]/members.tsx` | `/w/{slug}/members` | Owner-only 成员管理 |

旧测试流程 `/tests` 已冻结（#336 维护者定案，#337 实施，2026-11-01）：上表三行的「此前为」部分是历史，已被
「营销测试」（`experiments`）取代。`revise_test_plan` 与 `create_test_plan_run` 返回 410
`legacy_test_plans_frozen`，产品首页不再挂载「学到了什么」；`create_test_plan` 也已冻结（#343，2026-11-02），
同样返回 410 `legacy_test_plans_frozen`。
见[前台两项配置合同](../product/front-flow-two-config-contract.md) §3.2、§3.3。

六个功能页在 #26 中只展示真实的加载、空、错误或能力说明壳，不读取假数据，不预置
演示对象，也不实现 #27/#28 拥有的领域行为。

### 4.2 WorkspaceLayout

`web/components/layouts/WorkspaceLayout.tsx` 负责：

- `WorkspaceProvider`；
- 桌面侧栏和移动抽屉；
- 导航：产品 / 测试 / 素材在前；新建生成、创意库、投放实验、本地化、放量折叠在「高级工作台」组（ADR 0011，#165；组内区域激活时自动展开，显式展开状态按浏览器记住）；
- Workspace 选择器；
- 当前用户入口；
- Owner-only 的 settings/members 入口；
- skip-to-content；
- route focus；
- `WorkspaceRouteBoundary`；
- 主内容容器和每页唯一 `<h1>`。

Owner-only 导航隐藏只是体验优化。页面 loader 和现有控制面请求 handler 仍分别执行
服务端角色校验。

## 5. 服务端 Workspace 准入

### 5.1 WorkspaceBootstrap

页面只接收显示和路由所需的最小控制面投影：

```ts
type WorkspaceBootstrap = {
  workspaceId: string;
  slug: string;
  displayName: string;
  membershipId: string;
  role: 'OWNER' | 'MEMBER';
};

type WorkspaceSummary = {
  workspaceId: string;
  slug: string;
  displayName: string;
  role: 'OWNER' | 'MEMBER';
};
```

这些 UUID 可以用于 React key、显示投影和控制面请求，但 domain hooks 永远只把 URL
中的 slug 交给 BFF。FastAPI 身份中的 Workspace UUID 仍由 BFF 服务端解析。

`WorkspaceBootstrap` 不包含 Entitlement。#18 完成前，#26 不查询或猜测权益。

### 5.2 getWorkspacePageProps

`web/lib/workspace-bootstrap.ts` 导出：

```ts
getWorkspacePageProps({
  ownerOnly?: boolean,
  namespaces?: string[],
}): GetServerSideProps
```

一次页面请求按固定顺序执行：

1. 验证可撤销会话；
2. 校验路由 slug 的形状；
3. 以 slug 查询 Workspace，并在同一 Workspace 查询中筛选当前用户成员关系；
4. 区分 Workspace 不存在与成员关系不存在；
5. 若 `ownerOnly`，强制 `OWNER`；
6. 加载所需 i18n namespaces；
7. 返回最小 `WorkspaceBootstrap`。

结果语义：

- 无会话：带原始 callback URL 重定向到登录页；
- Workspace 不存在：HTTP 404；
- Workspace 存在但没有成员关系：HTTP 403；
- Member 访问 Owner-only 页面：HTTP 403；
- 数据库或内部失败：HTTP 500，不把内部信息放进页面 props；
- 成功：页面才允许 hydration。

SSR 状态页使用可信静态路由元数据决定是否能说明所需角色；对象级 403 不推测权限。

### 5.3 Workspace 列表和稳定排序

控制面 Workspace 列表响应增加当前用户在每个 Workspace 的 role，并采用
`createdAt ASC, id ASC` 的稳定排序。列表只返回当前用户拥有有效成员关系的
Workspace。

`dashboard.tsx` 改为服务端入口：

- 有 Workspace：重定向到稳定排序第一项的 `/w/{slug}/generate`；
- 无 Workspace：进入现有 All Workspaces/无可用 Workspace 状态；
- 不再使用 `useEffect + router.push`，因此没有登录后闪屏。

## 6. WorkspaceContext 与原子切换

### 6.1 单一真相源

URL 是当前 Workspace 的唯一真相源。Context 只是最近一次成功路由的投影，不维护
独立的“当前 Workspace”选择，不使用 Cookie 或 localStorage 覆盖 URL。

```ts
type WorkspaceContextValue = {
  current: WorkspaceBootstrap;
  available: WorkspaceSummary[];
  pendingSlug: string | null;
  switchWorkspace(slug: string): Promise<boolean>;
};
```

### 6.2 WorkspaceNavigator

`web/lib/workspace-navigator.ts` 隐藏目标路由计算：

- 从六区页切换时保留当前功能区；
- 从 settings/members 切换时，仅当目标 role 为 Owner 才保留管理页；
- 目标 role 为 Member 时回退到 `/generate`；
- 未知路径回退到 `/generate`；
- 目标必须出现在服务端返回的 `available` 列表中；这仍然只是 UX 预判，目标页面
  loader 会重新验证。

切换顺序：

1. 设置 `pendingSlug`，禁用会产生命令的交互并显示非阻塞切换状态；
2. 计算目标 URL；
3. 调用 `router.push`；
4. 路由成功且新页面 props 到达后，Context 投影更新；
5. 路由失败则清除 pending，继续显示原 Workspace；
6. 任一时刻都不把旧 Workspace 数据标成新 Workspace 数据。

所有 SWR/domain key 必须含 slug。旧请求迟到时只能写回旧 key；新页面不会订阅该 key。
切换或卸载时，Operation polling 清理 timer，并忽略旧 Workspace 的迟到响应。

## 7. BFF domain hooks

Hooks 位于 `web/hooks/domain/`，只能经过已注入的 `DomainGateway`，不得直接 `fetch`
BFF path。路径由生成的 `buildBffPath()` 和 allowlist 构造。

### 7.1 生成类型

`DomainOperationMap` 是参数、请求体和成功响应的类型源。生成步骤还应区分 read 与
command operation ID，使查询 hook 不能接收写操作，命令 hook 不能接收 GET。

Hooks 返回浏览器侧 `result-types.ts` 中的 `ProblemDetails`，不导入服务端
`problem-details.ts`。

### 7.2 useDomainQuery

```ts
useDomainQuery(operationId, params?, options?) => {
  data,
  problem,
  isLoading,
  isStale,
  lastSuccessfulAt,
  refresh,
}
```

不变量：

- cache key 至少包含 `domain`、slug、operation ID、规范化 path params 和 query params；
- `refresh()` 包装 bound SWR mutate，不向调用方暴露 SWR；
- 成功响应更新 `lastSuccessfulAt`；失败或 304 不伪造成功时间；
- `isStale` 只表示客户端缓存年龄，并由 timer 在阈值跨越时更新；
- UI 文案使用“last loaded”，不能声称领域证据本身陈旧；
- focus 和重新进入路由时按 SWR 策略 revalidate；
- 结构化 `problem` 与成功数据互斥，不把 `detail` 字符串解析成控制流。

### 7.3 useDomainCommand

```ts
useDomainCommand(operationId, options?) => {
  execute,
  retry,
  reset,
  result,
  problem,
  reconciliation,
}

execute({ body, pathParams?, queryParams?, actionInstanceId })
```

`actionInstanceId` 是调用方拥有、跨网络重试保持稳定的逻辑用户意图。Hook 不生成或
发送 `Idempotency-Key`、`X-Request-Id`、CSRF 或身份头：

- `DomainGateway`/`IntentTracker` 生成并持久化 `X-Intent-Id`；
- BFF 从 operation ID 与 intent ID 派生 `Idempotency-Key`；
- 浏览器 Gateway 发送 `X-Trace-Id`；
- BFF 生成内部 `X-Request-Id`；
- `DomainGateway` 获取 CSRF token，并只按既有合同重试一次 CSRF 刷新。

`retry()` 只用于未获得确定响应的网络提交，并复用同一个 action identity。一个已经
持久化、随后进入 terminal `failed` 的 Operation 不调用这里的 retry；它需要新的用户
意图或领域专用 retry command。

`reset()` 只清理已确定的本地展示状态。存在 reconciliation 时，必须经过显式用户
确认，不能借 reset 静默丢弃未确定意图。

不提供通用 `expectedRevision`。只有某个 OpenAPI operation 明确声明 request revision
或允许 `If-Match` 后，生成类型才暴露该字段。

### 7.4 useOperation

```ts
useOperation({ operationRecordId, purpose, actionInstanceId }) => {
  operation,
  problem,
  isTerminal,
  isPartialSuccess,
}
```

行为：

- 通过 `gateway.pollOperation()` 查询；
- 保存 ETag，并在下一次请求发送 `If-None-Match`；
- 304 保留现有数据和成功时间，不触发无意义的数据替换；
- 服务端 `Retry-After` 优先；缺失时才使用 1s → 5s → 15s 上限退避；
- terminal：`succeeded`、`failed`、`cancelled`、`partially_succeeded`；
- non-terminal：`queued`、`running`、`uncertain`；
- `uncertain` 继续轮询等待对账，不映射成普通失败；
- 当前合同没有 progress 字段，因此不生成百分比；
- 当前合同没有 cancel operation，因此不暴露远程 `cancel()`。

本地取消只表示：卸载、路由变化或 Workspace 切换时清理 timer，并忽略迟到响应。

Operation 恢复索引使用：

```text
sessionStorage[operation:{slug}:{actionInstanceId}] = operationRecordId
```

调用方在刷新后仍拥有稳定 action identity，因此能恢复 Operation ID。索引只用于定位，
Operation 状态仍从 FastAPI 重新读取。索引在用户显式 reset、会话结束或对应功能确认
不再需要时清理，不以浏览器值决定业务终态。

## 8. 共享操作 UX

共享模块放在 `web/components/workspace/`。它们接收规范化 view model，不直接解析
任意原始领域响应。

### 8.1 ModeProvenance

接收调用方映射后的 provenance view model，可展示：

- real、real-read-only、sandbox 或 controlled mock 等领域明确模式；
- Provider；
- 模型或外部 API 版本；
- Mock scenario；
- 可安全展示的 correlation ID。

#26 只实现展示合同和测试，不假设所有当前 OpenAPI response 已经含统一 `mode`。
#27/#28 只能从各自可信领域响应映射，不从文件名、页面选择或 Provider 名猜测模式。
影响解释的 Mock/Sandbox provenance 使用持久 badge，不用可消失 toast。

### 8.2 OperationStatus

```tsx
<OperationStatus
  operation={operation}
  pollingProblem={problem}
  outcomeSummary={summary}
  actions={actions}
/>
```

- `queued`/`running`：状态文字和 spinner；
- `succeeded`：明确成功，但公共模块不自动消失；
- `partially_succeeded`：warning，展示调用方从实际 result resource 提供的摘要；
- `failed`：展示实际结果资源提供的原因和恢复动作；
- `cancelled`：明确取消；
- `uncertain`：明确“结果未知，正在核对”，不冒充成功或失败；
- polling Problem Details 与 Operation 自身 `failed` 严格区分。

Operation response 本身只有 state、result reference 和 timestamps，因此公共模块不得
编造失败原因、部分成功明细或 retry 行为。恢复动作由实际领域结果提供。

### 8.3 CapabilityNotice

`CapabilityNotice({ decision })` 只展示调用方传入的 resolved decision，例如 allowed、
not available 或 limit reached。它不读取 WorkspaceBootstrap，不自行解析 Entitlement，
也不充当服务端门禁。#18 负责真实 Entitlement resolution 和后续 `EntitlementGate`。

### 8.4 StaleIndicator

接收 `isStale` 与 `lastSuccessfulAt`，显示“loaded {duration} ago”。它不把客户端缓存
年龄解释成领域证据水位。

### 8.5 ForbiddenState

SSR 已知 Owner-only 页面可以使用可信静态元数据说明需要 Owner。BFF 返回的对象级
403 只显示通用无权访问说明和可复制 `trace_id`；不解析自然语言、不猜测对象存在性，
也不泄漏权限模型细节。

### 8.6 WorkspaceRouteBoundary

React error boundary 只捕获子树 render/lifecycle 错误。通过 `router.asPath` 作为 reset
key 在成功路由变化后重置，不在内部订阅 router events。它不声称捕获 SSR、事件处理
器异常或 rejected promise；这些分别由 loader 和 hooks 处理。

### 8.7 FeatureEmptyState

扩展现有 `web/components/shared/EmptyState.tsx`，增加 action slot。每个功能区提供自己的
标题、说明和 CTA，公共模块只负责一致布局。名称不使用 `EmptyWorkspace`，避免与“用户
没有任何 Workspace”混淆。

## 9. 私有上传

### 9.1 接口

```ts
useUpload(purpose) => {
  startUpload,
  state,
  hashProgress,
}

startUpload({ file, ownerKind, ownerId, actionInstanceId })
  => Promise<UploadStartResult>
```

```ts
type UploadStartResult =
  | {
      kind: 'accepted';
      uploadIntentId: string;
      operationRecordId: string;
    }
  | { kind: 'problem'; problem: ProblemDetails }
  | ReconciliationRequired
  | { kind: 'transfer_error'; error: TransferError };
```

### 9.2 三阶段实现

调用方只发起一次上传。模块内部完成：

1. 使用 `FileDigest` adapter 分块计算 SHA-256；
2. 读取 `file.name`、`file.type` 和 `file.size`；
3. 调用 `createUploadIntent`，且
   `content_length_min === content_length_max === file.size`；
4. 把返回的 grant 与原始 File 交给
   `BrowserBlobTransferClient.upload(grant, file)`；
5. 调用 `finalizeUpload`；
6. 持久化 action → Operation 恢复索引；
7. 返回 `operationRecordId` 给 `useOperation` 跟踪异步验证。

`file.type` 是客户端声明的 MIME，不是最终事实。Worker finalize 会读取 magic bytes、
metadata、大小、checksum 和 Workspace 绑定后才建立领域引用。

create 与 finalize 使用不同但稳定的 phase action identity：

```text
upload:{actionInstanceId}:create
upload:{actionInstanceId}:finalize
```

这样不会让 `IntentTracker` 把两个不同 operation 的并发或恢复状态误判为同一个传输。

### 9.3 摘要 adapter

浏览器原生 `crypto.subtle.digest()` 不是流式接口，不能以一次 `file.arrayBuffer()` 冒充
有界内存实现。生产 `FileDigest` adapter 使用 Web Worker 中的增量 SHA-256 实现，按
固定大小 chunk 读取 File，并报告 `hashProgress`。测试 adapter 返回确定摘要。

实现必须证明：

- 内存使用由 chunk 大小约束，而不是与整个文件大小线性增长；
- 摘要是标准 SHA-256 base64；
- 相同 File 得到相同摘要；
- 中断不会继续申请 upload intent；
- 日志和错误不包含文件内容或 signed URL。

### 9.4 状态与错误

状态为：

```text
idle -> hashing -> requesting_grant -> transferring
     -> finalizing -> awaiting_verification
```

`awaiting_verification` 只表示 finalize 返回 202，不表示文件已经通过验证。最终状态来自
Operation。

两个 Gateway 阶段均可能返回 `ReconciliationRequired`。对象存储错误不伪装成
Problem Details，而是规范化为 `TransferError`；它可以包含安全的 HTTP status、origin
和用户可读消息，但不得包含完整 capability URL、query、签名字段或上传表单字段。

## 10. 私有下载

### 10.1 接口

```ts
requestDownload({ objectRef, filename, actionInstanceId })
  => Promise<DownloadStartResult>
```

`objectRef` 必须映射到生成的 `CreateDownloadIntentRequest`：owner kind/id、committed
blob key 和精确 storage version。调用方不能提供任意公开 URL。

```ts
type DownloadStartResult =
  | { kind: 'started' }
  | { kind: 'problem'; problem: ProblemDetails }
  | ReconciliationRequired
  | { kind: 'transfer_error'; error: TransferError };
```

模块：

1. 调用 `DomainGateway.createDownloadIntent`；
2. 使用现有 `BrowserBlobTransferClient.validatedDownloadUrl(grant)` 校验 exact origin；
3. 通过注入的 `BrowserNavigator` adapter 发起用户导航；
4. 不向调用方返回、不缓存、不记录 signed URL。

### 10.2 capability 语义

BFF 在签发 grant 前重新验证会话、Workspace membership、对象 Workspace 和精确对象
revision。签发后的 URL 是短期 bearer capability：

- S3 presigned GET 和本地 HMAC GET 在过期前可以重复使用；
- 对象存储验证签名、过期时间、HTTP method 和精确对象 version；
- 对象存储不会在每次下载时重新读取 Workspace membership；
- 因此 URL 泄漏者可能在 TTL 内重复下载，任何日志、分析、错误和浏览器持久化都不得
  保存该 URL；
- 真正 single-use 下载不在 #26 范围，若未来需要，必须新增消费状态或新的存储合同。

## 11. Storage origin policy 与 CSP

### 11.1 复用现有模块

扩展现有 `web/lib/domain-client/storage-origin-policy.ts`，不新建第二套同名 policy。
抽取 Edge/browser-safe 的纯解析接口：

```ts
parseStorageOrigins(raw, { production }): string[]
```

生产和测试 adapter 分别把 `NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS` 或显式字符串传入。
同一规范化 origin 数组同时用于：

- `StorageOriginPolicyImpl` 的 grant URL allowlist；
- middleware `generateCSP()` 的 `connect-src`；
- 配置和 CSP 合同测试。

### 11.2 配置校验

每项必须是 exact origin，仅含 scheme、host 和有效 port。拒绝：

- wildcard；
- username/password；
- path、query 或 fragment；
- `data:`、`blob:` 或宽泛 scheme source；
- production 中的非 HTTPS；
- 与 `DOMAIN_API_BASE_URL` 相同的 origin。

本地开发允许明确的 HTTP loopback storage origin。生产校验和开发例外均有独立单元
测试；不能用 E2E 的本地 HTTP 配置证明生产允许列表正确。

### 11.3 CSP

`connect-src` 为：

```text
'self' <validated-storage-origin>...
```

不得包含 FastAPI origin、`*` 或宽泛 scheme。浏览器领域查询/命令只能访问同源 BFF；
对象存储是唯一允许的跨源连接。对象存储 CORS 另行只允许应用 origin，它与 CSP 分别
约束请求的目标和来源，缺一不可。

## 12. 键盘与无障碍

无障碍要求进入每个相关模块的接口和测试，而不是另建一个事后扫描层：

- Workspace selector 优先使用原生 `<select>`；若使用自定义样式，必须完整实现所选
  WAI-ARIA listbox/menu pattern，包括焦点、方向键、Enter、Escape 和退出行为；
- WorkspaceLayout 顶部提供首个可聚焦的 skip-to-content；
- 主内容有稳定 `id`；每页只有一个可聚焦 `<h1 tabIndex={-1}>`；
- `useEffect([router.asPath])` 在新内容挂载后把焦点移到 `<h1>`；
- 所有互动元素可由键盘到达并拥有 accessible name；icon-only button 使用
  `aria-label`；
- 颜色不是状态的唯一表达，状态同时有文字和图标；
- `OperationStatus` 使用 `aria-live="polite"`，错误额外使用 `role="alert"`；
- `StaleIndicator` 只在首次跨过 stale 阈值时宣布，不随每次计时刷新重复播报；
- `ForbiddenState` 挂载时聚焦标题，不使用冗余 live region；
- prefers-reduced-motion 下不依赖动画表达状态。

## 13. 测试策略

### 13.1 接口级测试

测试通过模块公开接口断言行为，不测试 SWR、router 或 adapter 内部状态。

- `getWorkspacePageProps`：401、403、404、Owner/Member 和安全 props；
- `WorkspaceNavigator`：六区保留、Owner-only 回退、失败不切换；
- domain hooks：Workspace key、Problem Details、stale timer、intent reuse；
- `useOperation`：ETag、304、Retry-After、terminal 和 uncertain；
- `useUpload`：摘要、两 phase identity、transfer、finalize、reconciliation 和错误脱敏；
- `useDownload`：intent、origin 拒绝、导航、不泄漏 capability；
- storage origin/CSP：开发 loopback、生产 HTTPS、FastAPI 排除和 drift；
- UX：状态映射、焦点、live region 和 accessible name。

当前 Jest 默认环境是 Node。需要 DOM 的 hook/渲染测试使用文件级
`@jest-environment jsdom` 或独立的 DOM Jest 配置；不能让全部服务器 handler 测试隐式
切换到 jsdom。

### 13.2 Playwright 目录与配置

Playwright 位于：

```text
web/e2e/workspace/
web/e2e/.auth/           # gitignored，运行时生成
web/playwright.config.ts
```

不放在 `web/__tests__/`，避免被当前 Jest `__tests__/**/*.spec.ts` 收集。

新增：

- `@playwright/test` dev dependency；
- `test:e2e` package script；
- CI 中 Chromium 安装；
- 独立 E2E job；
- PostgreSQL、Next.js、FastAPI 和 local blob transfer；
- 确定性 fixture 和清理。

E2E 使用至少一个 mixed-role 用户：该用户在 Workspace A 是 Owner、在 Workspace B 是
Member，因而能真实验证同一会话切换后的角色降级。可以另建纯 Owner/Member 用户覆盖
邀请和隔离，但不能用“Owner 在两个 Workspace 都是 Owner”冒充角色回退证据。

登录 storageState 每次 CI 生成，包含敏感 Cookie 的 `.auth/` 永不提交。

本地跨源文件流可使用 Next dev server 和 HTTP loopback storage。生产模式另有
`parseStorageOrigins(..., { production: true })`、`generateCSP(..., true)`、production
build 和静态 CSP 检查；开发 CSP 运行证据不能替代生产配置合同。

### 13.3 六个浏览器测试文件

1. `session.spec.ts`
   - 未登录访问 Workspace 路由重定向到登录；
   - 使用中撤销/过期会话后干净重定向；
   - 公开页面不残留认证 Workspace 内容。
2. `workspace-switch.spec.ts`
   - mixed-role 用户切换 Workspace；
   - URL、Context 和显示数据原子变化；
   - 不闪现旧 Workspace 数据；
   - 从 Owner-only 页面切换到 Member Workspace 落到 `/generate`。
3. `owner-member.spec.ts`
   - Owner 看见 settings/members，Member 不看见；
   - Member 直达 Owner-only 页面得到服务端 403；
   - Owner-only 控制面 handler 同样拒绝 Member。
4. `navigation-refresh.spec.ts`
   - 六区导航保持同一个布局实例；
   - 刷新恢复 WorkspaceContext；
   - back/forward 正确；
   - 页面可显示从持久索引恢复并由 FastAPI 重读的 Operation 状态。
5. `network-boundary.spec.ts`
   - 正常领域请求只到同源 BFF；
   - 配置的 storage origin 可以完成上传；
   - 页面主动 `fetch()` 测试 FastAPI origin 时被 CSP 阻止；
   - 浏览器 console 出现 connect-src violation；
   - FastAPI 测试 listener 证明收到零个该直连请求；
   - 响应 CSP 含 `'self'` 和 exact storage origins，不含 FastAPI、wildcard 或宽泛 scheme。
6. `keyboard.spec.ts`
   - 导航 tab 顺序；
   - Workspace selector 键盘操作；
   - skip-to-content；
   - 路由后焦点进入新 `<h1>`；
   - 状态不用颜色单独表达。

Operation 恢复算法主要由注入 Gateway/storage/timer 的接口级测试证明，不使用
Playwright route mock 冒充集成证据。浏览器测试只验证真实页面在刷新后使用恢复索引并
重新读取 Operation。

## 14. CSP 和网络负向证据

被动观察正常流程没有访问 FastAPI 不足以证明强制边界。`network-boundary.spec.ts`
必须主动尝试直连测试 FastAPI origin，并同时观察：

- 浏览器 fetch rejection；
- CSP violation；
- FastAPI listener 零命中。

反方向也必须证明 exact storage origin 可用，否则一个“阻止所有跨源连接”的 CSP 会
错误通过安全测试，却让 signed upload 无法运行。

## 15. 实现顺序

建议按以下顺序交付，每一步都保持可构建：

1. Workspace bootstrap 类型、SSR loader、列表 role 和服务端 dashboard redirect；
2. WorkspaceContext、Navigator、Layout、六区/管理路由和 route boundary；
3. typed domain query/command/Operation hooks；
4. 共享操作 UX；
5. FileDigest、upload/download orchestration、storage origin parser 和 CSP；
6. 接口级测试、Playwright fixture、六个浏览器测试和 CI job；
7. 完整 `npm run verify`、E2E 和人工键盘/读屏检查。

#18 可以与本 Issue 并行。#26 不在 Bootstrap 中预留未定义的 entitlement shape；#18
落地后通过明确的 resolved decision 接入 `CapabilityNotice`/后续 EntitlementGate。

## 16. 验收清单

Issue #26 只有在以下证据同时存在时完成：

- 九个 Workspace 路由均在服务端验证会话、slug、成员关系和适用角色；
- 六区导航和持久布局在客户端路由间保持；
- Workspace 切换不把旧数据渲染在新 URL 下；
- Owner/Member 在导航、页面和控制面 handler 三层得到一致结果；
- 所有领域请求经过生成 allowlist 与同源 BFF，浏览器不持有领域 JWT；
- 查询、命令、Operation hooks 保持 Workspace cache/intent/polling 不变量；
- 上传完成三阶段并等待异步验证，不把 transfer 成功当成领域成功；
- 下载 capability 不进入日志、缓存或持久浏览器状态；
- CSP 允许 exact storage origins 并主动阻止 FastAPI；
- loading、empty、problem、forbidden、stale、uncertain 和 partial success 有一致表达；
- 键盘、skip link、路由焦点、live region 和 accessible name 测试通过；
- 六个 Playwright 文件及接口级测试进入 CI；
- `npm run verify`、production build 和 CSP 检查通过；
- 页面没有加入 #27/#28 的假业务数据，也没有复制 #18 的 Entitlement 规则。
