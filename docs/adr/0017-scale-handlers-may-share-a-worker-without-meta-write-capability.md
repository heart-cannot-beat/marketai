# ADR 0017: Scale handler 可以跑在没有 Meta 写能力的共享 Worker 里

**Status:** Proposed (Accepted on merge)  
**Date:** 2026-11-09  
**Decided in:** #73（维护者决定，2026-11-09）  
**Amends:** [Scale 合同](../product/scale-advice-semantics-contract.md) §20 最后一段、§24 验收第 23 条  
**Relates to:** [ADR 0008](0008-separate-scale-evidence-advice-and-delivery-control.md)（Scale 证据、建议与投放控制分离）、[ADR 0010（Railway 生产拓扑）](0010-railway-production-topology-and-trust-boundaries.md)

## 背景

Scale 合同 §20 原来要求 Scale Worker 使用「不含写环境变量、Launch dispatch 和写侧数据库权限的独立 composition root」，并写明「把 Scale handler 放进已加载 Meta write port 的共享 Worker 不满足本合同」。

实际部署的 Worker 只有一个：`python -m worker.main`，入口是 `app.worker_runtime.main`。它一直转发 `scale_evaluate`，#55 之后也转发 `scale_export`。按合同写的独立运行时 `ScaleWorkerRuntime`（`app/scale/worker.py`）只在 e2e 里启动过。所以生产上的 Scale Job 一直跑在共享 Worker 里，和合同冲突（#73）。

本 ADR 依据共享 Worker 现在的写侧情况来判断：

- composition root（`_main`）构造 `WorkerRuntime()` 时，既不注入 Meta 客户端，也不注入写端口，`meta_client` 和 `launch_adapter` 都是 `None`。
- 共享 Worker 路由三种 Launch Job：
  - `launch_provisioning` 和 `launch_recovery` 只会解析到沙箱适配器 `FakeMetaAdapter`（`customer_sandbox`、`controlled_mock`）。遇到 `internal_validation` 时拒绝解析。每条调用意图的 `write_permitted` 都被数据库约束固定为 false。这两条路径不发任何网络请求。
  - 能到达真实写客户端的只有 `launch_canary`。没有注入写端口时，`launch_service.execute_operation` 会自己构造 `HttpMetaWriteAdapter`。只要 `config.meta_write_ready()` 为假，这个构造函数就拒绝。`meta_write_ready()` 要求 `META_WRITE_ACCESS_TOKEN` 非空，另外还要求写开关、App Secret、广告账户、API 版本和 Launch 确认密钥都已配置。
- 也就是说，进程里有写侧代码，但没有写能力：没有写 Token，就构造不出能写 Meta 的客户端。
- 在所有服务的 Railway 环境合同里，`META_WRITE_ACCESS_TOKEN` 都是 `DENIED`，`deploy/railway/validate-static.sh` 会检查这一点（[ADR 0010（Railway 生产拓扑）](0010-railway-production-topology-and-trust-boundaries.md) 第 6 条）。但这只检查合同文件里的声明。部署时，没有任何机制能阻止有人给运行中的服务设上这个 Token。

## 决定

修订 Scale 合同 §20：**这个进程同时满足下面三条时，Scale handler 才可以运行在共享 Worker 里。**任一条不满足，就必须先拆出独立的 Scale Worker，然后才能改动共享 Worker。

1. **没有 Meta 写端口和写侧客户端。** composition root 不给运行时注入任何写端口；类上定义了 `ensure_step` 或 `ensure_object` 的对象都算写端口。按进程当前的配置，也构造不出写侧客户端。
2. **没有 Launch dispatch。** 这里指能到达真实 Meta 写客户端的 Launch Job 路径。共享 Worker 可以继续路由 `launch_*` Job，因为这些路径今天都到不了真实写客户端：provisioning 和 recovery 只用沙箱适配器，`launch_canary` 会停在 `HttpMetaWriteAdapter` 的构造函数。因此第 2 条最终归结为第 1 条里的「构造不出写侧客户端」，由同一个检查覆盖。
3. **没有 `META_WRITE_ACCESS_TOKEN`。** 进程的环境变量和 `app.config` 里的写 Token 都为空。

合同原文还列了第三项「写侧数据库权限」，放宽后的条件不包含它。共享 Worker 使用数据库角色 `marketai_domain_worker`，它必须能写 Launch 的表才能跑沙箱 saga，但这不是 Meta 写入。按合同写的 `ScaleWorkerRuntime` 用的也是这个角色（`assert_runtime_role("marketai_domain_worker")`），所以两个运行时原本都不满足这一项。以后如果要恢复这项要求，拆分时需要给 Scale 一个单独的数据库角色。

**绊线**分两层：

- **启动检查**：`app.worker_runtime._main` 构造完运行时，调用 `assert_no_meta_write_capability`。三条中任一条不满足，Worker 就在领取任何 Job 之前退出。错误信息只列出不满足的条件名，例如 `meta_write_access_token_set`、`meta_write_port_held:launch_adapter`、`meta_write_client_constructible:HttpMetaWriteAdapter`，不带任何值。在运行中的进程上检查 Token 的，只有这一处。
- **守护测试** `tests/test_shared_worker_meta_write_tripwire.py`：
  - 按今天的方式装配出的 Worker 能正常启动。以下任一情况，启动检查都会拒绝：把 Token 设成非空（无论是环境变量还是 config）；给运行时注入写端口；写侧客户端可以构造，即使没有 Token 也一样。只读的 `MetaClient` 不算写能力。
  - 通过共享 Worker 派发 `launch_canary`：在写开关打开、其他门都通过、只有 Token 为空的情况下，派发停在 `HttpMetaWriteAdapter` 的构造函数（`meta_write_disabled`），不发网络请求。补上完整的写配置后，同样的派发会构造出写客户端。这说明挡住 Launch 写入的正是 Token。
  - 测试还固定了以下静态事实：
    - `app/` 里只有 `HttpMetaWriteAdapter` 一个真实写客户端。其余实现了写端口方法的类都声明 `is_test_adapter = True`，而且这份名单是固定的。
    - 拼 Graph API 地址的只有两个 Meta 适配器，读适配器只发 GET。
    - 构造写客户端的只有 `execute_operation`，共享 Worker 只在 `_launch_canary` 里调用它。
    - 共享 Worker 的 `_scale_*` handler 只导入 `app.scale`，不碰 `meta_client`、`launch_adapter` 或 Launch。
    - `app/scale` 里不出现 Launch Job 类型。
    - Railway 的 Worker 合同把写 Token 列为 `DENIED`，Compose 给 Worker 的写 Token 为空。
  - 以后启用 Meta 写入时，无论是把 Worker 的环境合同改成允许 Token、在 composition root 注入写端口，还是新增一个写客户端，这个测试都会失败，迫使先完成拆分。如果不改仓库、直接在 Railway 上给 Worker 设 Token，由启动检查拦下。

「Scale 永远不做 Meta 写入」这条仓库不变量不变。

## 理由

- 合同要防的是 Scale 跑在一个能写 Meta 的进程里。今天的共享 Worker 没有写能力：
  - 所有服务的环境合同都拒绝写 Token，`validate-static.sh` 会检查；
  - #55 转发 Scale Job 时，handler 拿不到 Worker 的 Meta 客户端；
  - `app/scale` 的 AST、import closure 和运行 spy 测试（`tests/test_scale_worker_isolation.py`、`tests/test_scale_security.py`）证明，Scale 代码到不了写端口，也到不了 Launch。
- 写能力不存在时，独立 composition root 换不来任何额外安全，成本却是实打实的（见下文的拆分前置工作）。
- 原合同只在仓库里声明拒绝 Token。加上启动检查后，「进程里没有写能力」在运行中的进程上也得到了验证。这一层是独立 Scale Worker 原设计里没有的。

## 没有选择的方案

**现在就部署独立的 Scale Worker。** 没有选，原因是：

- 今天没有任何进程具备 Meta 写能力，拆分不能减少实际风险；
- `ScaleWorkerRuntime` 按现状不能和主 Worker 并排部署（见下文），要先补 lane 过滤、在主 Worker 里排除 Scale Job、补部署配置，还要多付一个常驻服务的费用；
- 它和主 Worker 用同一个数据库角色，即使拆出来，也满足不了合同原文里「写侧数据库权限」这一项。

**不改合同，保留冲突。** 没有选。合同是事实来源。如果生产长期运行在一份写明「不满足本合同」的配置上，合同就失去了约束力。

## 将来拆分的前置工作

三条中只要有一条要被打破（最可能的情况是启用 Meta 写入），就要先按下面的顺序完成拆分：

1. `ScaleWorkerRuntime.run_once` 现在调用 `jobs.claim_next(self.owner)` 时不带 lane 过滤，会领取任何类型的 Job，再把非 Scale 的 Job 以 `unsupported_job_type` 标为失败。和主 Worker 并排部署之前，它必须只领取 Scale 的 Job 类型（`claim_next` 已经支持 `job_types=`），主 Worker 必须排除这些类型（`exclude_types=`）。
2. 主 Worker 去掉 `scale_evaluate`、`scale_advice_expire`（#97 补上的转发）、`scale_collect_sources`（#109 新增的来源收集）、`scale_target_change`（#113 新增的 Target 变更后重评）和 `scale_export` 的转发。`tests/test_job_policy.py` 里检查「部署的 Worker 能处理 app 入队的所有类型」的那条测试，要改成按两个 Worker 的并集检查。
3. 补齐部署：新增 Railway 服务（`python -m app.scale.worker`）和它的环境合同（`DENIED META_WRITE_ACCESS_TOKEN`），并在 `validate-static.sh` 里加上对应条目。
4. 如果要满足「写侧数据库权限」这一项，给 Scale 一个没有 Launch 表授权的数据库角色。
5. 拆分完成后，主 Worker 不再注册 Scale handler，`assert_no_meta_write_capability` 和守护测试按本 ADR 退役。到时守护测试的前置检查会先失败，提醒做这一步。

## 后果

- Scale 合同 §20 最后一段和 §24 验收第 23 条按本 ADR 修订。`docs/superpowers/` 下早期的 Scale 设计文档仍然写着专用 Scale Worker，以合同为准。
- 共享 Worker 启动时多了一道检查。如果运行中的 Worker 被设上了写 Token，它会在启动时退出；Railway 按 On Failure 策略重启后仍会失败，所有 Job 都会停下，直到移除 Token 或拆出 Scale Worker。我们接受这一点，因为在这种状态下继续运行 Scale，正是合同所禁止的。
- 今后要启用 Meta 写入（[ADR 0010（Railway 生产拓扑）](0010-railway-production-topology-and-trust-boundaries.md) 第 6 条目前禁止），必须先完成拆分。
