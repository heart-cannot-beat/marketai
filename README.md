# MarketAI · AI 营销测试

> 本仓库于 2026-11 从私有仓库迁移为公开仓库，不含此前的提交历史。文档中的 #编号 指迁移前私有仓库里的 issue / PR，与本仓库的编号无关。

**AI 帮用户提出营销假设、组织测试、解释结果。** 用户提供产品信息、判断和微调，最终拿到值得继续投放的具体广告组合、支持证据和下一步建议。

[打开 MarketAI](https://marketai-web-production.up.railway.app) · [HTML 图文教程](docs/tutorials/marketai-tutorial.html) · [文档目录](docs/README.md) · [开发与运行](docs/development.md)

HTML 教程已按新版流程整篇重写，主教程有 28 个操作步骤、59 张新截图；下载后可用浏览器离线打开，支持目录和图片放大；GitHub 页面默认显示源码。阅读方式见[离线教程说明](docs/tutorials/README.md)，文字速查见[操作步骤](docs/user-guide.md)。

## 从产品资料开始

第一次使用：**粘贴产品 → 确认 AI 理解 → 确认测试方案**。

之后在同一张营销测试页完成：**生成 → AI 质量检查（QC）→ 反馈修订 → 人工确认 → 导出 → 在 Meta 投放 → 回传数据 → 看结论和下一步**。

1. 接受 Workspace 邀请、登录并完成 MFA，进入 **Marketing Tests**。新版默认入口是 `/w/{slug}/experiments`。
2. 粘贴产品介绍、目标客户、使用情境和有依据的声明，或选择已有产品。AI 分别起草“为什么需要这类产品”和“为什么考虑我们这款”，用户核对事实与推测。
3. 确认小轮要回答的问题、比较对象、业务目标、预算和停止规则。角度及各自的表达由 AI 起草，用户直接改，无需从零填变量或拼组合。
4. 选择权威实物参考图、图片供应商和模型，确认生成费用。确认本轮投放条件及判断规则后，AI 批量检查实际图片、文案和测试意图；从 QC 反馈准备修订草稿，再生成新版本、检查并人工确认。
5. 完成审核并冻结导出清单，生成私有 Meta 导出包。广告在 Meta Ads Manager 人工创建；需要验证效果时，回到同页绑定实际 Ad ID、预检并确认每日 CSV，再评估结果。

没有 Key 时，可以显式选择免费的 **Controlled Mock** 体验流程。模拟素材与结果保留标识，不能成为真实投放证据。详细步骤、费用和故障处理见[HTML 图文教程](docs/tutorials/marketai-tutorial.html)。

## 本次改版方向

2026-10-23「角度/表达看板 + 出图质量」以[规格 §3–9、§12 场景 13–16](docs/product/ai-marketing-testing-spec.md)及 [ADR 0013](docs/adr/0013-ad-language-brand-baseline-and-style-variation-are-plan-fields.md)为合同，实施与依赖见 [Epic #218](https://github.com/heart-cannot-beat/marketai-archive/issues/218)；本地验收见 J-50（[#282](https://github.com/heart-cannot-beat/marketai-archive/issues/282)），已于 2026-10-27 上线。

## 当前能力与验证范围

截至 2026-10-27，生产运行 `main` 提交 `b570d43c`：Release、Worker、API、Web、Dispatcher 均为 SUCCESS，领域库 `zy0f1a2b3c4d`，控制面 `20261026090000_user_locale`。生产随 `main` 合并自动部署，迁移由 Release 服务在备份后执行。线上已在专用验收工作区用受控 Mock 走完产品卡 → 方案 → 生成 → 看板 → 单图/对照 QC → 人工审核 → 导出 → 广告绑定 → 日报回传 → 评估，三个服务重启后恢复同一小轮；旧小轮只读打开正常。详见[本版上线记录](docs/operations/board-image-quality-release-2026-10-27.md)，更早的发布与验收见[上线记录](docs/product/ai-marketing-production-release.md)。

| 能力                                              | 当前状态                                                           |
| ------------------------------------------------- | ------------------------------------------------------------------ |
| 产品理解、动态角度/表达、问题驱动的小轮与大轮     | 已接通；表达按角度组织，共享与独有表达可并存                       |
| 角度/表达看板、多执行版本、外观待核实、品牌底线   | 2026-10-27 上线；线上受控闭环与旧小轮只读兼容已验收                |
| 单图/对照 AI QC、图中文字转写比对、无效输出留证   | 已上线；线上为 Mock 验收，真实模型逐字转写尚未在线上复核           |
| 界面与 AI 输出语言（en / zh-CN）、广告语言独立    | 已上线；部分方案检查项名称在中文界面仍为英文                       |
| 生图、AI QC、反馈再生成、人工确认、精确版本 ZIP   | 已上线；真实客户产品的权威参考图与真实 OpenAI 流程已验收           |
| OpenAI、Gemini、Seedream 自助 Key 设置            | Owner 可添加、启用、替换、移除；需 MFA，保存不验证 Key 有效性      |
| Gemini、Seedream 真实附图调用                     | 代码及受控契约已验证；等待 Key、最大报价及真实调用验收             |
| Meta 广告对应、CSV 预检/确认/更正、评估和 AI 解读 | 受控闭环已验收；实际业务效果等待真实 Meta 报表                     |
| 营销证据用于本地化和放量准备                      | 已接通显式交接；达标素材、组合复用、商业真值与实际投放对象各有门槛 |
| 刷新、断连恢复、不可变导出和桌面 Safari 下载      | 隔离及线上分层验收通过；不代表所有设备、iOS 或全部故障均已覆盖     |

[12 个验收场景](docs/product/ai-marketing-testing-spec.md) 最新映射至 47 项检查，详见[再次复核](docs/product/ai-marketing-final-reaudit.md)、[隔离故障验收](docs/product/marketing-isolated-fault-acceptance.md)与[部署及 Safari 复验](docs/product/marketing-fault-release-verification.md)。视频、更广设备覆盖、多产品真实 AI 质量和真实 Meta 业务效果仍分别待验。

## Key、模型与费用

Owner 在 **Settings → Provider credentials** 分别配置 **OpenAI、Google Gemini、Seedream（Volcengine Ark）**。新版使用 Workspace 自有 Key（BYOK），不借用全局 Key 或其他工作区凭据。Member 可在授权预算内操作。

正式调用还需要有效权益、Owner 设置的 **USD Spend Guardrail**，以及部署方为所选模型配置的最大费用。生图、AI 策划/解读和视觉 QC 各自显示报价，确认后才执行；保存 Key、选择参考图或准备草稿本身不发起付费模型调用。

模型以页面服务端目录及就绪状态为准。Gemini/Seedream 的 1K/2K 和 OpenAI 的 Standard/High quality 含义不同，High quality 不表示 2K 输出。配置说明见[供应商与报价](docs/product/generation-provider-selection.md)、[策划](docs/product/ai-marketing-planning-slice.md)和[AI QC](docs/product/ai-marketing-ai-qc.md)。

## 如何理解结论

- **Approve 是质量通过**，不表示广告有效；产品事实、AI 推测、投放证据分别保存。
- 广告角度是购买主张，表达方式是讲法，元素配置是人物、场景、标题等选择；一个组合可以有多条具体素材。
- 默认路线为**筛选角度 → 匹配表达 → 确认组合**。比较整套组合只能得出组合结论，不能把效果全部归因于角度。
- **业务达标与相对胜出分开**。可以有多个达标对象，也可以以证据不足或没有达标素材结束。未测搭配保持未知。
- 只有一张广告有效时，先认定这张广告有效；组合复用需要新执行版本及另一小轮的新增投放支持。
- 当前营销回传是用户提供的 CSV，不是 Meta API 独立核验。营销页的导出、证据交接和放量建议不创建或启用广告。

完整规则见[改版规格](docs/product/ai-marketing-testing-spec.md)及[交接、回传与评估契约](docs/product/ai-marketing-delivery-loop.md)。

## 开发与部署

| 单元       | 目录      | 责任                                        |
| ---------- | --------- | ------------------------------------------- |
| Web / BFF  | `web/`    | Next.js 登录、Workspace、页面及同源领域请求 |
| Domain API | `app/`    | 营销状态、版本、权限、费用与证据规则        |
| Worker     | `worker/` | 持久化任务、供应商调用、私有文件和恢复      |

Web 控制面与领域服务使用独立数据库和运行角色。生产使用私有存储与工作区隔离，付费调用结果不确定时保留费用暴露，先对账再决定，不自动重发。

本地安装、迁移、运行与验证命令见[开发与运行](docs/development.md)、[Web README](web/README.md)及[Railway 运行手册](docs/operations/railway-beta-runbook.md)。`python run.py` 只启动冻结的旧版 SQLite 工作台，不能替代新版登录和托管部署；旧版操作见[历史工作台说明](docs/legacy-workbench.md)。

## 项目文档

- [HTML 图文教程](docs/tutorials/marketai-tutorial.html)：28 个步骤、59 张新版截图，从第一次登录到修订、导出、日报更正和下一轮；附[支持工作台参考](docs/tutorials/advanced-reference.html)。
- [改版规格与 12 个验收场景](docs/product/ai-marketing-testing-spec.md)、[实施计划](docs/product/ai-marketing-testing-implementation-plan.md)：产品规则与阶段门槛。
- [上线记录](docs/product/ai-marketing-production-release.md)、[最新故障修复与 Safari 复验](docs/product/marketing-fault-release-verification.md)：版本、证据及未验范围。
- [领域词表](CONTEXT.md)、[架构决策](docs/adr/)与[完整文档目录](docs/README.md)：实现边界及专项说明。

当前 GitHub 项目为 [heart-cannot-beat/marketai](https://github.com/heart-cannot-beat/marketai)。代码与文档通过 PR 同步；新规格的 GitHub 原生 Issue、子任务和依赖图尚未全部整理完成。


## 2026-10-19 架构修复发布

六项修复基于当前生产版本集成，发布顺序为带私有备份的 Release 迁移 → Worker → API → Web。生产迁移接续至 zu6b7c8d9e0f，开发目录中的旧迁移编号不用于生产。详细范围、验证与上线回执见 [发布记录](docs/operations/architecture-fixes-release-2026-10-19.md)。
