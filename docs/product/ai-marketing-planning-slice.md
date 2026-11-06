# AI 营销策划：行为与配置

状态：已随新版营销测试上线，默认入口为 `/w/{slug}/experiments`。本页说明策划行为与部署配置；逐步操作见[新版教程](../user-guide.md)，后续 QC、Meta 交接、数据回传和评估见[完整闭环](ai-marketing-delivery-loop.md)，生产版本与真实/受控验收范围见[上线记录](ai-marketing-production-release.md)。

## 当前行为

入口 `/w/{slug}/experiments`，导航为 Marketing Tests。支持粘贴资料或选择已有产品、保存/恢复测试、AI 理解产品、核对产品事实、修改推测、确认理解、AI 起草方案、编辑角度及各自表达、增加新表达、确认方案与准备关联生成草稿。事实确认、方案确认都不产生效果结论。

补充原始资料创建新修订，重新起草并确认理解与方案；已有 Product、素材与历史快照不被覆盖。相同请求重放复用资源；草稿修改使用 If-Match；进行中或不确定的策划不能被新付费请求覆盖。

方案包含一个具体小轮问题、显式测试组/组合关系、控制条件、预算、目标、数据要求和停止规则。筛选角度默认需要不同表达；预算仅支持有限表达时须显式声明局限。完整组合比较不能标成单因素结论，单方案复测允许不做相对比较。

每个组合连接已有 GenerationDraft。图片生成仍需独立确认费用。测试意图由服务端当前已确认方案解析后写入配方，包含主张、表达、文案、检查项与控制条件；客户端不能伪造关联。方案变更后旧草稿不能创建有效新预览或提交，新生成版本不会继承效果结论。

策划本身不产生投放结论。后续闭环使用独立冻结小轮、AI 营销 QC 与人工确认、真实 ZIP 和用户提供的 Meta CSV，按冻结规则评估，并把已结束小轮的范围与结果交给后续策划参考。生成后的文件有效性检查不表示营销语义已经通过。旧 Experiment/Launch 记录继续保留原含义，不自动转换。默认 Workspace 首页已切换为营销测试页；独立生成页保留为支持入口。

## 正式 AI 策划配置

托管部署使用当前 `main` 的迁移链和 `make db-release`，不能复制首个本地切片的迁移编号到生产。当前上线迁移头为 `zs4f5a6b7c8d`；初始 `q1`–`w1` 本机编号仅属历史。新表具有 Workspace 复合引用、RLS 与修订历史；发布需同步 API/Worker 及新表最小权限，见[闭环部署说明](ai-marketing-delivery-loop.md#部署与验证)。

API 与 Worker 配置：

```dotenv
MARKETAI_PLANNING_PROVIDER=gemini
MARKETAI_PLANNING_MODEL=<已核实支持 JSON 输出的文本模型>
MARKETAI_PLANNING_MAX_COST_USD=<已核实的每次最高费用>
```

仅支持已有 Gemini/OpenAI 文本 API 协议。模型必须由部署方显式设置；最大费用必须为大于零且不超过 10 的十进制 USD 金额，不是内置供应商价格。一次请求完整输入（含 Schema）限制为 96000 UTF-8 bytes，输出最多 8192 tokens；原始产品资料最多 16000 字，报价需覆盖完整输入输出边界。模型不兼容协议时任务失败，不自动换模型或使用共享 Key。

Workspace 需 active、有效生成权益、USD Spend Guardrail，以及所选供应商 `binding_scope=generation` 的有效凭据；Owner 可在 Settings → Provider credentials 配置。策划报价与图片报价独立；产品理解、方案起草和结果 AI 解读共用此文本配置。缺配置时保留输入并说明原因，用户可显式体验免费受控 Mock，不会默认回落。

原子预留费用后，Worker 在短事务内检查资格、读取本 Workspace 凭据、记录调用边界；提交后才发出请求。成功返回按上限估算结算；JSON/证据校验失败保留失败状态并结算已知调用，不能冒充有效策略。超时或调用边界后中断保留 uncertain 和费用暴露，不盲目重发；当前不提供 uncertain 自助对账界面，需核对外部记录后处理。

PostgreSQL 的 API 只读取 `workspace_provider_bindings` 中的非敏感就绪信息，不能为了判断可用性取得加密秘密；Worker 的凭据读取与解密仍在原有 KMS 边界。模型/Prompt 与事实引用进入持久化记录，凭据值不会进入计划或响应。

## 首个切片的历史验证范围

以下是首个切片当时的验证范围，不表示当前版本尚未部署。后续真实登录/PostgreSQL、OpenAI 及生产复验见[上线记录](ai-marketing-production-release.md)与[最新故障/Safari 复验](marketing-fault-release-verification.md)。Gemini/Seedream 真实附图及真实 Meta 业务效果仍分别待验。

- 新后端测试覆盖事实引用、推测隔离、不等表达数量、显式组成员、预算/结论范围、版本冲突、Workspace 隔离、防重、付费超时、无效 AI 输出和生成意图解析。
- 页面测试覆盖保存输入、显式 Mock、事实核对、按角度展示表达、同意图重试；原生成页面仍运行相关回归。
- 实际组件以隔离数据在浏览器检查桌面和手机布局，并验证新增表达；这不替代真实登录跨服务 E2E。
- PostgreSQL 专项测试需要独立测试数据库；未配置的专项会跳过，不能据此声称 RLS 或线上权限已实测。没有产生正式 AI 或图片费用，也没有部署或改动真实 Meta 投放。
