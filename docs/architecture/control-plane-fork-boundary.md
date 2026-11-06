# 控制面 Fork 边界与运行手册

状态：Accepted
定案日期：2026-08-26
适用范围：#15 控制面 tracer bullet；邀请制自助 Beta

本文件记录 MarketAI 如何在同一仓库内引入 BoxyHQ 控制面 fork，以及三个部署单元
如何独立安装、启动、迁移与升级。它服从 ADR 0001–0004 与
《SaaS UI 与 FastAPI 集成契约》；冲突以 ADR 与契约为准。

## 1. 三个部署单元

| 单元 | 目录 | 运行时 | 数据库身份 | 迁移工具 |
|---|---|---|---|---|
| 控制面 Web / BFF | `web/` | Node 22 / Next.js 15 | 运行时 `marketai_control_app`；迁移 `marketai_control_owner` | Prisma |
| Domain API | `app/` | Python 3.12 / FastAPI | `marketai_domain_api` | Alembic |
| Worker | `worker/` | Python 3.12 | `marketai_domain_worker` | Alembic |

三者使用独立进程、独立镜像、独立服务身份与独立数据库角色。控制面数据库
`marketai_control` 只包含身份、Workspace、成员关系、邀请与账户管理记录；领域
数据库 `marketai_domain` 由 SQLAlchemy 独占。

`web/prisma/schema.prisma` 不得出现任何领域模型，`app/`、`worker/` 不得读取控制面
数据。两条断言分别由 `web/__tests__/fork/disabled-capabilities.spec.ts` 与
`tests/test_control_plane_boundary.py` 强制。

## 2. 干净检出后的启动

三个单元互不依赖对方的启动顺序，可分别验证。

### 2.1 PostgreSQL 角色与数据库

```bash
export CONTROL_OWNER_PASSWORD=... CONTROL_APP_PASSWORD=...
export DOMAIN_OWNER_PASSWORD=...  DOMAIN_APP_PASSWORD=...
export DOMAIN_API_PASSWORD=...    DOMAIN_WORKER_PASSWORD=...
export DOMAIN_READONLY_PASSWORD=...
export SUPERUSER_DATABASE_URL=postgresql://postgres@localhost:5432/postgres
make db-roles          # == ./deploy/postgres/apply-roles.sh
```

一条命令建立全部七个角色：控制面 owner/app、领域 owner/app，以及领域内的
最小权限运行角色 api / worker / readonly。只跑其中一层会得到看起来正常、但
FastAPI 无法连接的半配置集群，因此两个 SQL 文件由同一个入口按固定顺序应用
（`domain-tenant-roles.sql` 运行在前一个文件创建的 `marketai_domain` 上）。
CI 调用的是同一个入口，不另建内联路径。

脚本创建两个数据库、七个登录角色，并撤销跨库 `CONNECT`。密码来自调用方的
Secret Manager，仓库内不保存任何口令。领域内只有 `marketai_domain_owner`
持有 `BYPASSRLS`（迁移必须看到所有 Workspace）；api / worker / readonly 明确
`NOBYPASSRLS`，不拥有任何表，也没有 `CREATE` 权限。

两个 SQL 文件在同一个 psql 进程内按顺序执行，切库一律用
`\connect -reuse-previous=on`。脚本不解析、不重建连接串：先前用
`"${SUPERUSER_DATABASE_URL%/*}/marketai_domain"` 派生第二个 URL 会在最后一个
斜杠处截断，静默丢掉 `?sslmode=require`（此时正在传输七个角色口令）、改坏
含 `/` 的参数值、并彻底破坏无 path 的 URL。`-reuse-previous=on` 复用已经建立
的连接参数，包含 `sslmode`。三类连接串的回归测试在
`tests/test_apply_roles_script.py`。

#### 已退休的共享角色：`marketai_domain_app`

FastAPI 与 Worker 分别以 `marketai_domain_api`、`marketai_domain_worker` 连接；
`marketai_domain_app` 不再是运行身份，并显式声明为 `NOBYPASSRLS`。显式声明不能
删掉：`rolbypassrls` 是持久集群状态，省略语句不会撤销旧部署已经获得的豁免。
`make db-release` 会把已有集群收敛到该状态，同时撤销它对现有表、序列、schema
以及 owner 默认权限的全部 ACL；保留 CONNECT 只用于发布目标误配时给出明确诊断。

**退出门禁是可执行的，不是文档承诺。**
`tests/test_domain_app_transition.py` 读取该文件声明的状态，与它用 AST 在
`app/` 中找到的「未租户化入口」比对——真实的 `config.DEFAULT_WORKSPACE_ID`
读取，以及真实的未租户化 `session_scope()` 调用。扫描解析 import 别名与赋值别名，
`from .config import DEFAULT_WORKSPACE_ID as WS`、
`from .database import session_scope as uow`、以及
`uow = session_scope` / `uow = database.session_scope`（含多级链式赋值，迭代到不动点）
都会被算入；只提名字而不调用（`register(session_scope)`）同样算——名字一旦离开本
模块，静态分析就跟不下去了；常量自身的定义（Store
上下文）、`session_scope` 的 `def`、以及 `tenancy.py` 里解释该常量的文档字符串
都不算；`Cache().session_scope()` 与 `self.session_scope()` 这类同名方法也不算。
没有按文件整体排除任何模块——排除整文件会让日后出现在
`config.py` / `database.py` 里的真实调用被漏掉。两个方向都会失败：

- 入口重新出现而声明为 `NOBYPASSRLS` → 失败（代码与部署身份不一致）；
- 入口为零而声明为 `BYPASSRLS` → 失败（旧豁免没有真正退休）。

同一套测试还在**已经持有该豁免的集群上**演练撤销与恢复（需要
`DOMAIN_SUPERUSER_DATABASE_URL`，因为只有超级用户能改 `BYPASSRLS`），并断言集群
实际状态与文件声明一致——漂移会被 CI 抓到。CI 的 `domain-rls` job 另外以
`marketai_domain_app` 连接迁移后的数据库实测读空、写拒，并把集群先置成相反
状态再跑 `make db-release`，双向验证收敛。

### 2.1.1 发布入口：`make db-release`

```bash
# 领域运行角色口令 + 超级用户连接（只有超级用户能改 BYPASSRLS）；
# 不需要任何控制面口令。
export DOMAIN_API_PASSWORD=... DOMAIN_WORKER_PASSWORD=... DOMAIN_READONLY_PASSWORD=...
export SUPERUSER_DATABASE_URL=postgresql://postgres@localhost:5432/postgres
export MARKETAI_DATABASE_URL=postgresql+psycopg://marketai_domain_owner:...@host:5432/marketai_domain
make db-release        # == ./deploy/postgres/release.sh
```

领域数据库**唯一受支持的发布入口**。它按固定顺序做四件事：

1. `apply-domain-roles.sh` 只收敛**领域**角色与桥接状态，并把本次发布生成的
   nonce 盖在 `marketai_domain` 上（`ALTER DATABASE ... SET marketai.release_nonce`）；
2. 在**迁移目标自身**上校验：它带着本次发布刚盖的 nonce、`current_database()` 是
   `marketai_domain`、`current_user` 是 `marketai_domain_owner`、
   `marketai_domain_app` 的 `rolbypassrls` 与文件声明一致——任何一项不符直接拒绝迁移；
3. 以 schema owner 执行 `alembic upgrade head`；
4. 再应用一次领域角色文件，把显式 API/Worker 权限矩阵授予本次迁移新建的表。

第 2 步是「原子」的实际含义：**迁移不会跑在角色状态不符合本次发布预期的集群
上**。顺序反过来——先迁移、后建角色，或者对着本次发布之前配置的集群单独跑
`alembic upgrade head`——就会精确重现读空 / 拒写的中断，而 Alembic 自身无法察觉
这件事。因此这个检查放在发布脚本里，而不是放在文档里。

校验必须走 `MARKETAI_DATABASE_URL`（Alembic 即将使用的那条连接），而不是收敛角色
用的超级用户连接：两者是彼此独立的字符串，前者指向 staging、后者指向 production
时，检查会在 staging 通过、迁移落在 production 上。

**但只比对「状态」不够。** 两个都已初始化的集群，库名、连接角色、桥接状态三项
答案完全相同——因此身份必须来自「只有这一次收敛才可能留下的痕迹」：nonce。
它是数据库级设置；校验直接读取 `pg_db_role_setting` 中 `setrole = 0` 的数据库记录，
而不是读取可被角色默认值或连接 `options=-c...` 覆盖的 `current_setting`，且天然只
作用于某个集群上的某个数据库。CI 用两个 PostgreSQL service 实测两种串线
都被拒绝——第二个集群未准备时（库名/角色/角色缺失三条一起报），以及第二个集群
**同样完整准备**时（只有 nonce 能区分）——并断言两种情况下都不留下任何 schema。

**领域发布只碰领域库。** `domain-tenant-roles.sql` 每次发布都会执行，因此不允许
出现任何 `marketai_control`：那会让每次领域发布改写另一个部署单元的 ACL，而在
只有领域库的集群上更会直接失败。跨库撤权（撤销领域运行角色对 `marketai_control`
的访问）移到 `cross-database-denials.sql`，只由一次性 bootstrap 应用——它必须排在
创建这些角色的文件之后。CI 另建一个「只有 `marketai_domain`、没有
`marketai_control`」的集群，实测 `apply-domain-roles.sh` 在其上正常收敛。

**发布不碰控制面。** `make db-roles` 是一次性的全拓扑 bootstrap，跨两个部署单元、
因此需要控制面口令；`make db-release` 只收敛领域一半。否则每次领域发布都会重设
`marketai_control_owner` 的口令——控制面凭据在别处轮换后，领域发布要么失败，要么
悄悄把过期口令写回去。CI 在把控制面口令从环境中清空的情况下再跑一次
`make db-release`，证明这条解耦。

### 2.2 控制面 Web

```bash
cd web
cp .env.example .env                  # NEXTAUTH_SECRET 与 DATABASE_URL（运行时角色）
cp .env.migrate.example .env.migrate  # MIGRATION_DATABASE_URL（schema owner）
npm ci

# 迁移步骤是唯一加载 schema owner 凭据的地方；必须用子 Shell，
# 因为 `set +a` 只停止后续导出，不会撤销已经导出的变量
(set -a; . ./.env.migrate; set +a; npm run db:migrate:deploy)

npm run dev               # 运行时只持有 DATABASE_URL
```

同一个控制面数据库上有两个身份，且刻意分成两个文件：`MIGRATION_DATABASE_URL` 是
schema owner，是唯一能执行 DDL 的身份；`DATABASE_URL` 是运行时角色，没有 DDL 权限。
`web/.env` 会被 Next.js 自动加载并成为运行进程的环境，因此 schema owner 凭据不能放在
其中；生产部署只把它注入迁移 Job。两者都由
`deploy/postgres/control-plane-domain-roles.sql` 创建。`npm run db:migrate:*`
在缺少 `MIGRATION_DATABASE_URL` 时直接失败，不会退回运行时角色。

健康检查：`GET /api/health`（liveness）与 `GET /api/readiness`（配置与数据库依赖）。

### 2.3 FastAPI 与 Worker

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/alembic upgrade head
.venv/bin/python run.py            # Domain API
.venv/bin/python -m worker.main    # Worker
```

Python 侧的本地默认仍是 SQLite；SQLite 上 RLS 迁移是 no-op，隔离由带
`TenantContext` 的仓储层承担，`alembic upgrade head` 就是本地的全部。托管的
PostgreSQL 不要单独跑 `alembic upgrade head`，走 `make db-release`：角色状态、
迁移与新表 grants 必须一起推进，理由见 2.1.1。

托管环境必须把 API 和 Worker 作为两个独立进程注入不同的
`MARKETAI_DATABASE_URL`：前者使用 `marketai_domain_api`，后者使用
`marketai_domain_worker`。两个进程启动时都会查询 `current_user`，身份不符立即
退出。API 的 TenantContext 只接受上游认证中间件写入的
`request.state.tenant_context`；浏览器 header、路径 Workspace 或请求体字段都不
是身份来源。#17 负责安装验证 ES256 委托令牌的 BFF 中间件。

## 3. Pin 与受控升级流程

上游身份保存在机器可读的 `web/UPSTREAM.json`：仓库地址、40 位 commit、版本、
许可证与更新策略。`updatePolicy.automaticRebase` 与
`automaticDependencyUpdates` 恒为 `false`，且仓库内没有任何指向上游的自动化
workflow（由 `web/__tests__/fork/upstream-pin.spec.ts` 断言）。

### 受控上游更新流程

1. `./scripts/control_plane_upstream.sh status` —— 读取 pin，取回上游默认分支，
   报告落后多少个 commit。该命令只读，不修改工作区。
2. `./scripts/control_plane_upstream.sh diff [<commit>]` —— 输出 pin 与目标
   commit 之间、且仅限本 fork 仍保留的文件的差异。被移除的能力不会重新出现在
   审阅面里。
3. 人工审阅差异，逐条判断：安全修复、依赖升级、行为变更、或属于本 fork 已删除
   的能力。删除的能力不因上游更新而恢复。
4. 手工挑取需要的变更，更新 `web/UPSTREAM.json` 的 `pinnedCommit`、
   `pinnedCommitDate`、`pinnedVersion`，并同步 `web/NOTICE`。
5. 若依赖发生变化：`npm install` 后运行 `npm run inventory:licenses`，提交更新
   后的 `THIRD-PARTY-LICENSES.md` 与 `dependency-inventory.json`。
6. 若 Prisma schema 发生变化：新增 reviewed migration，不使用 `prisma db push`。
7. 全量门禁：`npm run verify`、`npm run inventory:licenses:check`、数据库角色
   隔离测试、`make verify`。
8. 以单独 PR 合并，PR 正文记录采纳与拒绝的上游变更。

不允许：`git merge upstream/main`、`git rebase upstream/main`、Dependabot 自动
提交、生产镜像自动跟随上游。

## 4. 已禁用的上游能力

完整清单与理由见 `web/HARDENING.md`。原则是**删除而不是开关**：被禁用的能力
没有路由、没有导航项、没有后台 handler、没有依赖。当前禁用的是公开注册、任意
Workspace 创建、Live Stripe、SSO/SAML、SCIM/Directory Sync、客户 Webhook、
Retraced 审计服务、团队 API Key、第三方前端分析与 Workspace 硬删除。

邀请是一次性的、指定邮箱的：`Invitation.email` 为 NOT NULL，表中没有
`sentViaEmail` 与 `allowedDomains` 列，上游的分享链接邀请无法表达；接受邀请在同一
事务内删除邀请并建立成员关系，重复接受直接失败。

客户可见角色只有 Owner 与 Member。角色缩减在 Prisma enum、`lib/roles.ts`、请求
schema 与 `assertCustomerRole` 四处同时强制，任何一处遗漏都会被
`web/__tests__/lib/roles.spec.ts` 打回。

## 5. 发布路径

- 构建：`npm run build` 执行 `prisma generate && next build && npm run build:ops`；
  最后一步把 `scripts/` 下的运维脚本编译到 `.ops/`。三步都不连接数据库、不改 schema。
- 迁移：在独立的发布步骤里加载 `web/.env.migrate`（schema owner），执行
  `npm run db:migrate:status` → 备份 → `npm run db:migrate:deploy`；不在构建镜像时
  进行，运行时进程只持有 `DATABASE_URL`，不接触 schema owner 凭据。
- 会话：JWT Cookie 只是载体，`sid` claim 绑定服务端 Session 记录；注销、改密、
  重置密码和 Manage Sessions 删除记录后，旧 token 在下一次请求即失效。身份
  （email、name）每次都从 Session 行关联的 User 重新读取，不使用 token 中的旧值。
- #17 的委托 ES256 领域令牌仍必须携带 authorization epoch：那种令牌签发后会在
  FastAPI 侧独立生效，控制面删除 Session 行无法使已签发的领域令牌失效。
- 回滚：迁移不可逆时先恢复备份，再回滚镜像；`prisma db push` 在任何环境都不使用。
- 定时任务：`npm run ops:prune-sessions` 每晚清理过期 Session 行（`Session.expires`
  已建索引）；按用户的懒清理只在该账户再次出现时发生，长期休眠账户需要这条全局扫描。
  该脚本按 Next.js 的方式加载 `web/.env`，容器调度只需注入运行时角色的
  `DATABASE_URL`，不得注入 schema owner 凭据；数据库不可达时以脱敏信息非零退出。
- 安全头与 CSP 默认开启，`SECURITY_HEADERS_ENABLED=false` 只允许在本地开发使用。
- 结构化日志经 `web/lib/logger.ts` 脱敏，Cookie、Authorization、JWT、连接串、
  Provider 凭据形态与签名 URL 参数一律不落盘。

## 6. 本 Issue 之外

委托 ES256 身份令牌、BFF 允许清单、领域 RLS、Entitlement 与订阅启用、完整的
Beta 准入与 onboarding 流程、六个功能区页面均属于后续 Issue。本文件只保证边界、
启动方式与升级流程可被自动化验证。
