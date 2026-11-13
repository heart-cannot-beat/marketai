# 开发与运行入口

新版用户操作见[使用教程](user-guide.md)。本文面向开发与运维；托管环境配置以[Railway 运行手册](operations/railway-beta-runbook.md)及[发布手册](operations/release-runbook.md)为准。

## 服务与本地启动

`web/`（控制面）、`app/`（FastAPI 领域 API）和 `worker/` 独立安装、独立启动、
独立迁移，使用不同的数据库角色。完整边界、启动命令与受控上游升级流程见
[控制面 Fork 边界与运行手册](architecture/control-plane-fork-boundary.md)。

```bash
# PostgreSQL 角色与两个数据库（密码来自 Secret Manager，仓库不保存）
./deploy/postgres/apply-roles.sh

# 控制面 Web（http://localhost:4002）
# .env 是运行时角色；.env.migrate 是执行 DDL 的 schema owner，只在迁移步骤加载
cd web
cp .env.example .env
cp .env.migrate.example .env.migrate
npm ci
(set -a; . ./.env.migrate; set +a; npm run db:migrate:deploy)
npm run dev

# FastAPI 领域 API 与 Worker（下面是 SQLite 组合启动，不含新版登录）
cd ..                     # 上面的 `cd web` 之后必须回到仓库根目录
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env  # 本地配置；正式 Key 仍在 Workspace 设置中管理
.venv/bin/alembic upgrade head
.venv/bin/python run.py  # 已同时启动 Worker；不要重复启动
```

`run.py` 是 SQLite 本地组合启动器。托管 PostgreSQL 中 API 与 Worker 必须分开
部署，并分别把 `MARKETAI_DATABASE_URL` 注入为 `marketai_domain_api` 与
`marketai_domain_worker` 的连接；两者与 `make db-release` 还必须共享一个至少
32 字节的 `MARKETAI_TENANT_CONTEXT_SECRET`。数据库用这个密钥验证 Workspace 与
authorization epoch 的 HMAC，运行角色自行改 GUC 不能伪造租户。进程启动时会
校验 `current_user`，错配直接退出。数据库发布统一走 `make db-release`。

`run.py` 会显式启用仅限 SQLite 的本地 Owner 身份，浏览器可以直接使用工作台；
直接启动 `uvicorn app.main:app` 不会获得这个身份，缺少控制面委托身份仍返回 401，
而在 PostgreSQL 上误开本地身份会拒绝请求。部署级 Meta 凭证还必须用
`META_CREDENTIAL_WORKSPACE_ID` 绑定到一个 Workspace，不能作为所有租户的默认账户。

现有 SQLite 数据迁入 PostgreSQL 时，目标必须是已迁移且为空的
`marketai_domain_owner` 数据库。工具以只读方式打开源库，先升级一份私有快照，
再在单个 PostgreSQL 事务中复制并核对逐表计数、按币种分组的真实金额字段合计和规范化内容哈希：

```bash
export MARKETAI_DATABASE_URL=postgresql+psycopg://marketai_domain_owner:...@host/marketai_domain
.venv/bin/python -m scripts.migrate_sqlite_to_postgres \
  --source data/marketai.db \
  --report data/import-reports/sqlite-postgres.json
```

验证：

```bash
make verify        # FastAPI/Worker 单测与 Alembic 漂移检查
make web-verify    # 控制面 format / lint / types / 单测 / 生产构建 / CSP 检查 / 依赖清单
make verify-all    # 两者
```

SQLite 没有数据库角色，所以 `make verify` 带一个授权守护（#59，`tests/support/grant_guard.py`）：
测试里的每个 HTTP 请求按 `marketai_domain_api` 运行，Worker 的 `run_once` 和 Job handler 按
`marketai_domain_worker` 运行（`app/platform/runtime_role.py` 的 `@runs_as`，只在测试里生效）。
它们发出的每条语句都要在 `deploy/postgres/domain-tenant-roles.sql` 里有授权，包括：

- INSERT、UPDATE（按列）和 DELETE；
- 读到的每张表的 SELECT。角色只有列级授权时，按语句点名的列检查；写语句的 `WHERE`、`SET` 表达式、冲突目标和 `RETURNING` 读到的列也算；
- `SELECT … FOR UPDATE/SHARE` 锁住的每张表上至少一列的 UPDATE。SQLAlchemy 在 SQLite 上不输出 `FOR UPDATE`，守护从语句对象读锁，所以加锁不要写成只在 PostgreSQL 上加。

缺授权时，第一个触发它的测试会在 teardown 报错，并写明角色、表、操作和发出语句的代码行。
修法是补授权（连同需要它的迁移）；PostgreSQL 走另一条路径的语句，登记到 `SQLITE_ONLY_WRITES`。
新增 Worker handler 时，给它加上 `@runs_as(WORKER_ROLE)`。

控制面健康检查：`GET /api/health`（liveness）、`GET /api/readiness`（配置与数据库）。
控制面不持有领域数据库凭据，也不实现领域规则。

## 新版 AI 配置

Owner 在 Settings → Provider credentials 配置各 Workspace 的供应商 Key；生成、策划和营销 QC 使用 `generation` 范围的绑定。保存 Key 不验证供应商鉴权。权益、USD Spend Guardrail 与模型最大报价缺一不可；Key 和 `.env` 不提交到 GitHub。

API 与 Worker 配置保持一致。图片每张上限和价格版本使用 `MARKETAI_GENERATION_MAX_COSTS_JSON`、`MARKETAI_PRICE_REGISTRY_VERSION`；策划及结果解读使用 `MARKETAI_PLANNING_PROVIDER`、`MARKETAI_PLANNING_MODEL`、`MARKETAI_PLANNING_MAX_COST_USD`；视觉检查使用 `MARKETAI_MARKETING_QC_PROVIDER`、`MARKETAI_MARKETING_QC_MODEL`、`MARKETAI_MARKETING_QC_MAX_COST_USD`。配置格式见[供应商选择](product/generation-provider-selection.md)、[AI 策划](product/ai-marketing-planning-slice.md)、[营销 QC](product/ai-marketing-ai-qc.md)和根目录 [.env.example](../.env.example)。示例报价是部署预留上限，不是供应商现价或账单。

`run.py` 只启动旧版 SQLite 本地组合；验证新版登录/BFF/隔离必须按运行手册配置 Web、委托身份、数据库、权益与共享私有存储。自动化测试中依赖特定数据库或外部环境的跳过项，不视为已验证。生产操作前按发布手册备份并检查角色与存储；不要把本地 Owner 身份用于生产。
