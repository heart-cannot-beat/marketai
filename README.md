# MarketAI · AI 营销测试

MarketAI 帮用户提出营销假设、组织测试、解释结果。用户提供产品信息、判断和微调，AI 负责起草和组织，最终交付值得继续投放的具体广告组合、支持证据和下一步建议。项目目前是邀请制 Beta。

[打开 MarketAI](https://marketai-web-production.up.railway.app) · [图文教程](docs/tutorials/marketai-tutorial.html) · [文档目录](docs/README.md) · [开发与运行](docs/development.md)

> 本仓库于 2026-11-06 从私有仓库迁移公开，不含此前的提交历史。文档中较早的 `#编号`、Epic 和 `marketai-archive` 链接指向迁移前的私有仓库，公开读者无法打开。

## 主流程

第一次使用：**粘贴产品 → 确认 AI 理解 → 确认测试方案**。

之后在同一张营销测试页完成：**生成 → AI 质量检查（QC）→ 反馈修订 → 人工确认 → 导出 → 在 Meta 人工投放 → 回传每日 CSV → 看结论和下一步**。

接受 Workspace 邀请、登录并完成 MFA 后进入 **Marketing Tests**（`/w/{slug}/experiments`）。逐步操作见[图文教程](docs/tutorials/marketai-tutorial.html)（GitHub 只显示 HTML 源码，下载后可离线打开，见[阅读说明](docs/tutorials/README.md)）或[文字速查](docs/user-guide.md)。

几条边界：

- **Approve 是质量通过，不代表广告有效。** 产品事实、AI 推测和投放证据分开保存。
- **业务达标与相对胜出分开判断。** 一轮可以有多个达标对象，也可以以“证据不足”结束；没测过的搭配保持未知。
- **MarketAI 不在 Meta 创建或启用广告。** 广告在 Ads Manager 人工创建；回传数据是用户提供的每日 CSV，不是 Meta API 独立核验。
- **付费调用先报价、确认再执行。** 调用使用 Workspace 自己的 Key（BYOK），受 Owner 设置的 USD Spend Guardrail 和部署方配置的最高报价约束；结果不确定的调用先对账，不自动重发。
- **受控 Mock 必须显式选择。** 没有 Key 也能免费走完流程，模拟素材和结果带标识，不能成为真实投放证据。

完整规则见[改版规格](docs/product/ai-marketing-testing-spec.md)和[交接、回传与评估契约](docs/product/ai-marketing-delivery-loop.md)。

## 功能区

| 功能区   | 入口                          | 做什么                                                                                                           | 规则来源                                                                                                                                                                     |
| -------- | ----------------------------- | ---------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 营销测试 | Marketing Tests               | 产品理解、按问题组织的小轮与大轮、角度/表达看板、生图、AI QC、修订、导出、回传和评估                             | [改版规格](docs/product/ai-marketing-testing-spec.md)、[ADR 0010](docs/adr/0010-organize-marketing-tests-around-questions-and-scoped-evidence.md)                            |
| 竞品扫描 | Marketing Tests，产品理解之后 | 确认竞品的 Meta 主页后付费扫描其公开广告，整理购买主张与拥挤度供起草参考；只回答“别人在讲什么”，不回答“什么有效” | [扫描规格](docs/product/competitor-scan-spec.md)、[ADR 0014](docs/adr/0014-competitor-observations-inform-hypotheses-not-evidence.md)                                        |
| 创意库   | Library                       | Workspace 内可复用的创意输入；草稿发布为不可变版本，任务只引用准确版本，可撤回、分叉                             | [创意库契约](docs/product/creative-library-ownership-and-reuse-contract.md)、[ADR 0007](docs/adr/0007-separate-library-drafts-published-versions-and-execution-snapshots.md) |
| 本地化   | Localization                  | 按冻结的真值包把已确认的 Winner 素材改成多个市场的版本；价格、折扣等事实只来自可追溯数据，不由模型推断           | [本地化真值契约](docs/product/localization-truth-package-contract.md)、[ADR 0006](docs/adr/0006-freeze-offer-specific-market-truth-before-localization.md)                   |
| 放量     | Scale                         | 对已验证的 Winner 在具体投放上下文中评估健康度并给出建议；不拥有启停、预算或任何广告修改命令                     | [放量建议契约](docs/product/scale-advice-semantics-contract.md)、[ADR 0008](docs/adr/0008-separate-scale-evidence-advice-and-delivery-control.md)                            |
| 设置     | Settings、Members             | Owner 管理成员、Spend Guardrail 和 Provider 凭据：OpenAI、Gemini、Seedream 用于生成，ScrapeCreators 用于竞品扫描 | [供应商与报价](docs/product/generation-provider-selection.md)、[ADR 0005](docs/adr/0005-separate-commercial-access-usage-and-provider-cost.md)                               |

独立生成（Generate）和素材（Assets）页面属于支持工作台，见[支持工作台参考](docs/tutorials/advanced-reference.html)。各功能已验收的范围和未验部分见[上线记录](docs/product/ai-marketing-production-release.md)。

## 架构

| 单元       | 目录      | 职责                                                                                                                |
| ---------- | --------- | ------------------------------------------------------------------------------------------------------------------- |
| Web / BFF  | `web/`    | Next.js 控制面：登录、MFA、Workspace、成员、邀请和订阅；页面经同源 BFF 调用领域 API。派生自 BoxyHQ SaaS Starter Kit |
| Domain API | `app/`    | FastAPI：营销状态、版本、权限、费用与证据规则                                                                       |
| Worker     | `worker/` | 持久化任务、供应商调用、私有文件和恢复                                                                              |

其余目录：`migrations/`（领域库 Alembic 迁移）、`contracts/`（OpenAPI 与 E2E 契约）、`deploy/`（Docker、PostgreSQL 角色、Railway 和发布脚本）、`tests/`、`test-support/`（受控供应商等测试替身）、`docs/`。

设计上的几条不变式：

- 控制面拥有身份、成员、邀请和订阅；领域数据、规则、任务、审计和外部副作用只归 FastAPI 和 Worker。
- FastAPI 从经过校验的 BFF 声明得出 Workspace 和操作者身份；生产环境用 PostgreSQL RLS 和分开的数据库角色隔离租户。
- 历史决策保存为不可变版本和只追加的证据，当前状态是投影，不改写历史。
- 外部和付费调用走持久化任务，带幂等、成本预留和可恢复的证据。
- 开发环境的 Meta 写权限保持关闭，放量功能不做任何 Meta 写操作。

边界细节见[控制面 Fork 边界](docs/architecture/control-plane-fork-boundary.md)、[Web/BFF/API 契约](docs/architecture/saas-ui-fastapi-integration-contract.md)和[架构决策](docs/adr/)。

## 本地开发

需要 Python 3.12、Node 22（与 CI 一致）和 Docker。

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
make verify        # FastAPI/Worker 单测与 Alembic 漂移检查
make web-install
make web-verify    # 控制面格式、lint、类型、单测、生产构建、CSP 检查和依赖清单
```

`make compose-up` 用仓库内的 compose 专用配置在本机起一套生产形态的栈：PostgreSQL、控制面 Web（`localhost:4002`）、领域 API（`localhost:8787`）、Worker 和本地 blob 存储，供集成测试使用；`make compose-down` 停止并删除数据卷。

分别启动各服务、配置数据库角色和供应商 Key 的步骤见[开发与运行](docs/development.md)与 [Web README](web/README.md)；托管部署见 [Railway 运行手册](docs/operations/railway-beta-runbook.md)和[发布手册](docs/operations/release-runbook.md)。`python run.py` 只启动冻结的旧版 SQLite 工作台，见[历史说明](docs/legacy-workbench.md)。

## 文档

- [领域词表](CONTEXT.md)：术语以此为准。
- [AI 营销测试改版规格](docs/product/ai-marketing-testing-spec.md)与各功能区契约：产品规则与验收场景。
- [架构决策（ADR）](docs/adr/)：实现边界和取舍。
- [上线记录](docs/product/ai-marketing-production-release.md)：各版本范围、验收证据和未验部分。
- [完整文档目录](docs/README.md)。

根目录的《MarketAI产品蓝图》《MarketAI后端架构》《Meta广告数据完整参考》写于 2026-08，是早期背景材料；与新版规格冲突时以规格和 ADR 为准。

## 参与开发

- 需求、规格和实现任务都记在本仓库的 GitHub Issues。可以开工的 Issue 带 `ready-for-agent` 或 `ready-for-human` 标签，且所有 `Blocked by` 的 Issue 都已关闭。
- 契约和 ADR 是事实来源。代码与契约冲突时，先在 Issue 上报告，不直接改契约。
- 从最新 `main` 切分支；PR 引用对应 Issue，写明迁移、测试证据、运行影响和剩余风险，提交前跑 `make verify` 和 Issue 要求的其他检查。
- 这是公开仓库：测试、夹具和文档示例只用虚构品牌，自家品牌是 Acme Charge，竞品是 Brightvolt、Northpeak、Kestrel Power，域名一律用 `*.example`。不提交真实客户名称、素材、账户 ID 或生产环境截图；CI 的 `customer-denylist` 检查会拦下命中客户名单的改动。
- 用编码 agent 开发时的完整流程见 [CLAUDE.md](CLAUDE.md) 和 [AGENTS.md](AGENTS.md)。

## 许可

`web/` 派生自 Apache-2.0 许可的 [BoxyHQ SaaS Starter Kit](https://github.com/boxyhq/saas-starter-kit)，许可证原文见 [web/LICENSE](web/LICENSE)，依赖许可清单见 [web/THIRD-PARTY-LICENSES.md](web/THIRD-PARTY-LICENSES.md)。仓库其余部分尚未附开源许可证。
