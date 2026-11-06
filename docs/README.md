# MarketAI 文档目录

新用户先读[HTML 图文教程](tutorials/marketai-tutorial.html)（[下载与离线打开说明](tutorials/README.md)），开发与运维先读[开发与运行](development.md)。当前主流程是 AI 营销测试，冻结的旧版工作台另见[历史说明](legacy-workbench.md)。

## 使用与配置

- [HTML 图文教程](tutorials/marketai-tutorial.html)：34 个步骤、72 张逐步截图，覆盖工作区、三家 Key、产品理解、动态方案、实物参考、生成、QC 修订、裁剪旋转、放大/去背景/画笔擦除、版本分叉与对比、重新确认、导出、回传/更正和下一轮；[文字速查](user-guide.md)保留相同步骤。
- [HTML 支持工作台参考](tutorials/advanced-reference.html)：26 张逐步截图，讲解独立生成、版本、费用、恢复、下载和下游证据边界。
- [供应商与生成配置](product/generation-provider-selection.md)：模型、输出设置、报价、成本预留和不确定任务。
- [AI 策划配置](product/ai-marketing-planning-slice.md)、[AI 营销 QC](product/ai-marketing-ai-qc.md)：文本理解/策划/解读与视觉检查的独立配置。
- [Meta 交接、CSV 与评估](product/ai-marketing-delivery-loop.md)：文件格式、冻结口径、更正、停止和结论边界。

## 产品规则与验收

- [改版规格及 12 个场景](product/ai-marketing-testing-spec.md)、[实施计划](product/ai-marketing-testing-implementation-plan.md)。
- [首轮分层验收](product/ai-marketing-testing-acceptance.md)、[最新规格复核](product/ai-marketing-final-reaudit.md)。
- [生产上线记录](product/ai-marketing-production-release.md)、[故障修复上线及 Safari 复验](product/marketing-fault-release-verification.md)、[看板与出图质量版本上线（2026-10-27）](operations/board-image-quality-release-2026-10-27.md)。
- [Key 自助入口验收](product/provider-credentials-self-service-acceptance.md)、[供应商/尺寸/故障验收](product/provider-device-fault-acceptance.md)、[隔离故障补验](product/marketing-isolated-fault-acceptance.md)。

验收记录按日期保留当时范围；早期未覆盖的项目以较新记录为准。12 个场景的受控检查、真实 OpenAI 路径、桌面 Safari 与真实 Meta 业务效果是不同证据。Gemini/Seedream 真实附图等 Key，真实业务效果等报表；其余实机、多产品和运行故障仍按各自范围验收。

## 架构与运行

- [领域词表](../CONTEXT.md)、[架构决策](adr/)。
- [控制面与服务边界](architecture/control-plane-fork-boundary.md)、[Web/BFF/API 契约](architecture/saas-ui-fastapi-integration-contract.md)。
- [Web README](../web/README.md)、[Railway 运行手册](operations/railway-beta-runbook.md)、[发布手册](operations/release-runbook.md)。
- [备份](operations/backup-runbook.md)、[灾难恢复](operations/disaster-recovery-runbook.md)、[前向回滚](operations/forward-rollback-procedure.md)。
- [创意库契约](product/creative-library-ownership-and-reuse-contract.md)、[本地化真值](product/localization-truth-package-contract.md)、[放量建议](product/scale-advice-semantics-contract.md)。

`superpowers/` 中的旧设计与计划、v9 验收及历史工作台说明保留实现背景；与当前测试组织冲突时，以新版改版规格和相关 ADR 为准。客户资料、私有下载链接、回执原件和 Key 不进入公共文档。
