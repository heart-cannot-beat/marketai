# internal_validation 只读 canary 操作手册（#112）

部署 #112 之后，由维护者在生产环境的平台 Workspace 里，对平台自己的测试广告账户跑一次只读 canary。生产从 `main` 经 Railway 部署，没有 staging，所以这次 canary 就是 internal_validation 第一次读真实的 Meta。

**证据只放私有位置**：本地 `data/`（gitignored）或私有仓库 `marketai-ops`。真实的 Workspace id、广告账户 id、连接 id、token、app secret、Meta 的响应和截图，都不要写进本仓库、PR 或 Issue。下文的 `<...>` 都是占位符。

## 这次部署改了什么

- internal_validation 的 Scale 合同代次一建好，就自动有一条每日计划：账户本地时间 04:00，回看 14 天，只在平台 Workspace 里建。
- Worker 读这些计划时，用 `HttpMetaReadAdapter`。每读一页都重新从连接绑定的两个 CredentialVersion 解出 access token 和 app secret，只发 GET。
- Meta 的 usage header 超过 75% 时，批次 Job 推迟 15 分钟（90% 以上推迟 30 分钟；Meta 给了恢复时间就按它的来，范围 5 到 60 分钟），不消耗重试次数，之后从断点续读。
- token 过期、被撤销或没有权限时，当天的 run 立即失败，不产生 attestation，也不重试。

## 前置条件

1. 合并 #112 的提交已部署到 API 和 Worker。Worker 的启动检查通过，`META_WRITE_ACCESS_TOKEN` 在所有服务上为空。
2. API 和 Worker 的 `MARKETAI_CREDENTIAL_LOCAL_KMS_SECRET` 相同。bootstrap 时 API 用它加密凭据，Worker 读取时用它解密。两边不一致时，每次读取都会以 `meta_read_credential_unavailable` 失败，并且不会发出请求。
3. API 配置了 `PLATFORM_OPERATOR_JWKS_URL`，你手上有平台运营方 JWT，它的 `workspace_id` claim 是平台 Workspace。
4. 一个平台自己的测试广告账户，以及它的系统用户 token（只给 `ads_read`）和 app secret。不要用客户的账户。
5. 账户的币种和时区，以 Ads Manager 里的设置为准。

下文的 `$API` 是 API 服务地址，`$PLATFORM_JWT` 是平台运营方 JWT。

## 步骤

### 1. bootstrap 连接（已有连接可跳过）

```bash
curl -sS -X POST "$API/api/platform/v1/meta/bootstrap" \
  -H "Authorization: Bearer $PLATFORM_JWT" -H "Content-Type: application/json" \
  --data @"$PRIVATE_DIR/bootstrap.json"
```

`bootstrap.json` 只放在私有目录，内容是 `{"workspace_id": "<平台 Workspace>", "meta_mode": "internal_validation", "ad_account_id": "act_<测试账户>", "access_token_material": "<token>", "app_secret_material": "<secret>"}`，用完就删。不要把 token 写在命令行里，以免进入 shell 历史。

响应里有 `connection_id`、`binding_id` 和两个 credential version id，记到私有目录。

### 2. 建 Scale 读取合同

```bash
curl -sS -X POST "$API/api/platform/v1/scale/internal_validation/metric-contract-sets" \
  -H "Authorization: Bearer $PLATFORM_JWT" -H "Content-Type: application/json" \
  -d '{"meta_connection_id": "<connection_id>", "currency": "<USD 之类>", "timezone_name": "<IANA 时区>"}'
```

预期：200，`created: true`，`generations` 有三项（`daily`、`exact_window`、`delivery_configuration`），每项 `schedule_state` 为 `active`，`next_fire_at` 是下一个账户本地 04:00（以 UTC 表示）。重复调用返回同一个合同组，`created: false`，不会多出计划。

币种或时区填错时，合同照样建，但投放配置读回来会和合同对不上。Scale 收集时会报 `currency_mismatch` 或 `timezone_mismatch`。

### 3. 等第一次读取

Worker 在 `next_fire_at` 之后一分钟内分发。目标日 D 是触发时刻在账户本地的日期减一天。04:15 以后再检查。

### 4. 核对（全部只读）

1. **挑广告入口（#129）**：

   ```bash
   curl -sS "$API/api/platform/v1/scale/internal_validation/observed-ads?meta_connection_id=<connection_id>" \
     -H "Authorization: Bearer $PLATFORM_JWT"
   ```

   预期：`items` 列出测试账户的广告，`source_type` 是 `evidence_observation`；`unavailable` 为空。若仍是 `delivery_configuration_not_observed`，说明配置读取还没成功，看第 2、3 项。

2. **run 和 Job 的状态**：用数据库 superuser 开只读事务查询。下面的查询只输出状态和计数，结果可以存进私有目录。

   ```sql
   BEGIN READ ONLY;
   -- 计划：三条 active，cron 0 4 * * *，last_fired_at 是刚才的触发时刻
   SELECT g.contract_type, s.state, s.cron_expression, s.last_fired_at, s.next_fire_at
     FROM meta_sync_schedule_records s
     JOIN observation_contract_generation_records g
       ON g.id = s.contract_generation_id AND g.workspace_id = s.workspace_id AND g.meta_mode = s.meta_mode
    WHERE s.workspace_id = '<平台 Workspace>' AND s.meta_mode = 'internal_validation';
   -- run：D 当天 5 条 succeeded（daily、current_7d、baseline_7d、rolling_14d、configuration）
   SELECT split_part(occurrence_id, ':', 4) AS instance, state, total_pages, total_rows
     FROM meta_sync_run_records
    WHERE workspace_id = '<平台 Workspace>' AND meta_mode = 'internal_validation'
      AND split_part(occurrence_id, ':', 3) = '<D>';
   -- 每次调用：只有 GET，write_permitted 全为 false，HTTP 200，usage 峰值远低于 75
   SELECT i.meta_api_method, i.write_permitted, o.http_status, o.error_category,
          max((o.response_summary -> 'usage' ->> 'max_utilisation_pct')::numeric) AS peak_usage_pct,
          count(*)
     FROM meta_call_intent_records i
     LEFT JOIN meta_call_outcome_records o ON o.intent_id = i.id
    WHERE i.workspace_id = '<平台 Workspace>' AND i.meta_mode = 'internal_validation'
    GROUP BY 1, 2, 3, 4;
   -- 批次 Job：succeeded；被推迟过的话，审计里有 job.deferred
   SELECT type, state, attempt, error_code FROM jobs
    WHERE workspace_id = '<平台 Workspace>' AND type IN ('meta_observation_tick', 'meta_observation_sync')
    ORDER BY created_at DESC LIMIT 6;
   ROLLBACK;
   ```

3. **attestation**：五个 run 各有一条 `valid` 的 attestation，三个合同代次各有一行 D 的覆盖账本。

4. **API 版本和字段（#101 风险 1）**：合同冻结的是 Graph API `v21.0`。如果某个 run 以 `meta_read_failed` 失败，`error_detail` 带 `(code 100 ...)` 或版本已废弃的提示，说明这个版本或字段组合在真实账户上不可用。这正是 canary 要发现的问题：记录下来另开 issue，修改合同就是发布新版本的合同。

5. **日志**：Worker 日志里没有 token、app secret，也没有 `appsecret_proof=` 后面跟着的值。httpx 的请求日志会把它显示成 `[redacted]`。

### 5. 留证（私有）

把第 4 步的查询结果、observed-ads 的广告数量和响应码，存到 `data/canary/<日期>/` 或 `marketai-ops`。不要存 Meta 的原始响应。

## 怎么停下来

- **立刻停止读取**：撤销 access token 的 credential version。之后每次读取都在发请求之前失败（`meta_read_credential_unavailable`），当天的 run 记为失败，不产生 attestation。

  ```bash
  curl -sS -X POST "$API/api/platform/v1/credentials/<access_token_version_id>/revoke" \
    -H "Authorization: Bearer $PLATFORM_JWT" -H "Content-Type: application/json" -d '{"reason": "canary stop"}'
  ```

  然后在 Meta Business Manager 里也撤销这个系统用户 token。
- **回滚部署**：在 Railway 重新部署上一个版本。旧版本的 `resolve_adapter` 拒绝 internal_validation，计划还会分发，但 run 会失败，不会访问 Meta。最好先按上一条撤销凭据。

## 失败时的样子

| 现象 | 含义 | 处理 |
|---|---|---|
| run `failed`，outcome `auth_failure`，代码 `meta_read_auth_failed` | token 过期或在 Meta 侧被撤销（code 190/102） | 换 token。平台路由目前没有轮换凭据的入口：撤销后重新 bootstrap 会建一个新连接，旧连接的计划要另外退役（后续事项） |
| 同上，代码 `meta_read_permission_denied` | token 没有读这个账户的权限（code 10、200–299） | 给系统用户补 `ads_read` 和账户权限 |
| run `failed`，outcome `network_attempted=false`，代码 `meta_read_credential_unavailable` | credential 已撤销，或两边的 `MARKETAI_CREDENTIAL_LOCAL_KMS_SECRET` 不同 | 核对前置条件 2 |
| 批次 Job `retry_wait`，`error_code=meta_read_rate_limited`，审计有 `job.deferred` | 触到了限流阈值，正在等待 | 不用处理，会自动续读；推迟到第二天的计划时刻以后才会失败 |
| 批次 Job `failed`，run 的原因是 `meta_platform_workspace_required` | 这个 Workspace 不是平台 Workspace | 不应出现，查 `workspaces.is_platform_workspace` |
