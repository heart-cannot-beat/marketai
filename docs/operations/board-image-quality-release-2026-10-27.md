# 看板与出图质量版本上线记录（J-51）

日期：2026-10-27。对应 [#283](https://github.com/heart-cannot-beat/marketai-archive/issues/283)，属于 [Epic #218](https://github.com/heart-cannot-beat/marketai-archive/issues/218)。发布提交 `b570d43c`（main，含 #302、#303、#304 与 #305）。线上入口：[MarketAI](https://marketai-web-production.up.railway.app)。

本次线上操作逐项经用户在对话中同意后执行。真实付费调用 0 次，Meta 写入 0 次，未创建、启用或发布广告，未写入维护者的正式工作区或客户工作区。部署编号、备份位置与校验值、工作区和业务记录编号等真实回执在私有仓库，不在本仓库。

## 与工单设想的差异：生产随 main 自动部署

工单按“切生产工作树 → `make db-release` → 依次发布 API/Worker/Web”编写。开工核查发现 Railway 项目的 production 环境接通了 GitHub `main` 自动部署：每次合并都会同时重新部署 Release、Worker、API、Web 与 Dispatcher，Release 服务按既有路径执行“私有 Bucket 检查 → 两库备份 → Prisma → `deploy/postgres/release.sh`（角色收敛 → nonce 目标核验 → Alembic → 权限复核）→ 无漂移检查”。

因此本版的数据库与应用发布已经在合并时由 Release 服务完成。经用户同意，本次没有重复执行 `make db-release` 或重新部署，而是只读核对每一次自动运行的回执，并把这些运行记为本版的发布证据（见下节）。本机的生产工作树仅作记账，已切到 `b570d43c`（detached）；它不参与部署。

## 发布前本地核查（main `30e0e56f`，`b570d43c` 只改前端文案与测试）

- `alembic heads`：单一 `zy0f1a2b3c4d`。
- 自上一份发布记录（10-19，`zu6b7c8d9e0f`）以来的迁移：`zv7c8d9e0f1a`（10-20 已上线）、`zw8d9e0f1a2b`（`product_brand_kits`，带 RLS）、`zx9e0f1a2b3c`（`marketing_ai_qc_calls.kind`）、`zy0f1a2b3c4d`（`marketing_ai_qc_calls.error_detail`）；均为新增，均有 downgrade。
- 控制面另有 Prisma 迁移 `20261026090000_user_locale`（`User.locale`，可空）。工单正文未列出；`make db-release` 本身不跑 Prisma，由 Release 服务执行。
- `deploy/postgres/domain-tenant-roles.sql`：+19 行，为 `marketing_image_edits` 与 `product_brand_kits` 授权。
- 新增必填环境变量：无（新增的 env 读取都在测试、e2e 或脚本中）。
- OpenAPI 契约与 `web/lib/domain-client/generated/`（含 allowlist）重新生成后无差异。
- `make verify`：3877 passed、307 skipped、447 subtests；`alembic check` 无漂移。`cd web && npm run verify`：236 组 / 2284 项通过，生产构建通过。
- main 的 CI 中只有 golden-path Playwright 自 10-24 起持续失败（`New Draft` 选择器匹配两个按钮），已由 #305（`b570d43c`）修复。

## 数据库发布证据（Release 服务自动运行，只读核对）

| 日期（UTC） | 提交 | 变更 |
| --- | --- | --- |
| 10-24 08:09 | `b9ed8652` | Alembic `zv7c8d9e0f1a → zw8d9e0f1a2b` |
| 10-25 03:15 | `2d6b7d1d` | Alembic `zw8d9e0f1a2b → zx9e0f1a2b3c` |
| 10-26 07:05 | `5563faff` | Prisma `20260910150000_active_invitation_uniqueness → 20261026090000_user_locale` |
| 10-27 08:29 | `0751740e` | Alembic `zx9e0f1a2b3c → zy0f1a2b3c4d` |
| 10-27 12:19 | `b570d43c` | 无新迁移；Prisma 干净，`alembic check` 无漂移，head `zy0f1a2b3c4d` |

每次运行都先把两库备份到私有 Railway Bucket，并在迁移后重新收敛运行时授权。日志中的 `role … has not been granted membership …` 警告来自角色脚本对不存在成员关系的幂等 REVOKE，不是故障。

回滚点：`zy0f1a2b3c4d` 迁移前两库各有一份私有备份。本版迁移均为新增列/表，应用回滚不需要降级数据库；不要在新格式数据写入后执行 `alembic downgrade`。

## 应用部署（`b570d43c`，全部 SUCCESS）

Release、Worker、API、Web 与 Dispatcher 五个服务在 `b570d43c` 上均为 SUCCESS。应用回滚参考为 `30e0e56f` 上的部署；J-50 验收所在的 `c5f757fc` 也可作为更早的应用回滚点（API、Worker、Web）。

## 专用验收工作区

10-17 的验收工作区只属于自动化账户，其保存的凭据已无法登录，本次不再使用。经用户同意，用代码库的运维脚本在线上 Web 容器内新开通了一个专用工作区：

- `npm run ops:provision-workspace -- begin` 开通专用验收工作区，Owner 为用户本人账户（已完成 MFA），Beta 方案开启 `controlled_mock`。邀请链接只写入本机私有文件，未出现在会话中。
- 用户接受邀请时网页已把工作区激活；随后的 `complete` 因工作区已为 ACTIVE 返回 409，没有写入。派发服务送达权益快照后页面恢复正常。
- 只读确认：没有任何供应商 Key，生成默认值为“示例模式（不计费）”，Meta 未连接。

## 线上受控闭环（全部 Mock，0 费用）

在专用验收工作区新建一个测试和小轮，全部通过网页操作：

1. 产品卡“J51 Acceptance Charger”（虚构产品）→ 受控 Mock 产品理解 → 确认 1 条事实。
2. Mock 测试方案：2 个角度、5 种表达、每组合 2 个执行；生成前检查给出证据缺口与预算不足两类建议（非阻塞）。确认方案后页面注明“没有冻结产品照片，因此产品外观保持待核实”。
3. 5 个草稿逐个预览（`mock · mock-v1`，最高 USD 0）后生成，5 个任务共 10 张图全部成功。
4. 本轮设为受控模拟，2026-10-01 起，Asia/Hong_Kong，7 天点击 / 1 天浏览。
5. 看板：两个角度、五种表达，每种“执行 1 / 执行 2”，顶部显示“产品外观待核实：这些图片没有当前已核对的产品照片作为依据”。
6. 单图 QC 10 次：`marketing-vision-qc-single-v3`（#294 新策略）、`marketing-evidence-gates-v4`，文案检查因 Mock 未读出图中文字为 `rendered_text_missing` 待确认，产品外观为 `product_reference_missing` 待确认。
7. 对照 QC 10 次：`marketing-vision-qc-paired-v2`，每张与 2–3 张图对比；均为“需人工确认”，没有自动通过任何素材。
8. 人工审核：每个组合的执行 1 共 5 张，12 项检查人工改为通过并写明“受控 Mock 验收，仅验证流程”；审核来源 `ai_assisted_human_review`，AI 原始结论与逐项覆盖均保留。
9. 导出包就绪，5 条，manifest 已生成，广告名称随广告语言为英文。
10. 绑定 5 个虚构广告 ID（账户 123，广告 1000–1004），不调用 Meta。
11. 回传 35 行模拟日报，预览 0 个问题，批次标为 `controlled_mock` 后确认。
12. 评估结果为 `simulated`，0 个合格素材。未运行 AI 结果解读。

## 服务重启后恢复

逐个执行 `railway restart`（不重建镜像），每个服务单独获准：

| 服务 | 命令（UTC） | Railway 状态更新 | 依据 |
| --- | --- | --- | --- |
| marketai-api | 15:03:09 | 15:03:14 | 日志：Uvicorn 关闭 → Application startup complete |
| marketai-worker | 15:08:50 | 15:08:57 | 部署实例 RUNNING；Worker 启动时不输出日志 |
| marketai-web | 15:23:09 | 15:23:13 | 日志：`next start` → Ready in 489ms（15:23:21） |

三个部署编号均未变化，状态 SUCCESS，Web 健康检查 200。登录会话保持。重新打开同一小轮后，与重启前快照逐项一致：`evaluated`、revision 17、5 个批准版本、10 条单图 QC 与 10 条对照 QC 编号、导出包与 manifest、5 条广告绑定、已确认批次（35 行）及评估输入哈希。重启后没有再运行新的 Worker 任务，Worker 处理新任务的能力本次未单独验证。

## 本版前创建的旧轮次

在维护者的正式工作区只读打开本版之前创建的 4 个项目、5 个小轮（未点击任何写入操作，未下载）：

| 项目 | 小轮 | 状态 | 看板 | 产品外观 |
| --- | --- | --- | --- | --- |
| 项目 A | 小轮 A1 | reviewing | 200，5 个执行 | 待核实（5 个组合） |
| 项目 B | 小轮 B1 | reviewing | 200，7 个执行 | 待核实（6 个组合） |
| 项目 C | 小轮 C1 | closed | 200，3 个执行 | 有参考图依据 |
| 项目 C | 小轮 C2 | handoff | 200，4 个执行 | 有参考图依据 |
| 项目 D | 小轮 D1 | evaluated | 200，4 个执行 | 待核实（2 个组合） |

网页实际显示“执行 1”“执行 1 v2”“执行 1 v3”，没有报错。没有冻结产品照片的旧小轮显示“产品外观待核实：这些图片没有当前已核对的产品照片作为依据”；10-17 冻结了已核对 Library 参考图的真实客户产品项目如实显示为有参考图依据，不显示待核实。旧表达缺少讲法时显示“讲法待补充”。

## 观察到的问题（未在本票修复）

- 对照 QC 全部完成后，看板有一张图仍显示“对照质检（缺少）”、页面提示“9 张等待审核”；刷新后恢复为 10 张。疑似轮询在最后一个任务完成前停止，数据本身正确。
- 中文界面下，审核抽屉中 12 个检查项有 10 个名称为英文（来自方案的检查项定义，如 “Product appearance and use are accurate”）。
- 新工作区激活前，营销页把错误码 `projection_sync_unavailable` 原样显示，没有本地化文案。
- 工单写的“执行①”在界面上实际为“执行 1”（`marketing-board-execution`），与 J-50 场景 16 的记录一致。
- 旧项目（没有广告语言字段）页面顶部提示“确认方案前，请先确认广告语言”，主按钮为“确认方案并准备素材”，即使小轮已在投放或评估阶段；本次未点击。

## 运维影响与剩余风险

- **自动部署没有发布门禁**：合并到 main 即同时部署全部服务，Release 迁移与 API/Worker 新版本并行启动，迁移落地前新代码可能短暂运行（`zy0f1a2b3c4d` 约晚 4 分钟）。#284 带迁移，合并即上线；是否保留自动部署或改为人工推进需要另行决定。
- 每次合并都会在私有 Bucket 新增两份全库备份，需要关注保留与成本。
- 10-26 有几次自动部署失败（Release/API @ `2d028fc0`，Worker @ `0b979fa2`，Dispatcher @ `f1cc9a5d`、`2d028fc0`、`0b979fa2`），均被后续提交覆盖；本次未查看其日志。
- 真实视觉模型对错字的逐字转写（#294）在线上尚未用真实调用复核。可选的单次复核（OpenAI `gpt-4.1-mini`，单次上限）只能在有 Key 的正式工作区中进行，会写入新的 QC 与费用预留，经用户决定本次跳过。
- 10-17 的验收工作区已无法登录，保留不动；后续线上验收使用本次新开通的专用验收工作区。
- 真实回执（Railway 部署清单、备份清单、验证日志、开通输出、看板截图、闭环前后快照与模拟 CSV）在私有仓库，不在本仓库。
