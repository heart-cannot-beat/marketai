# 新版生成：供应商选择

> 2026-10-16：本文记录已接通的生成切片。后续主流程以 [AI 营销测试改版规格](ai-marketing-testing-spec.md) 为准，由确认的测试方案填充生成意图；保留这里的供应商、成本、任务与恢复能力，不把手写创意描述或多维排列组合作为用户必经步骤。

当前操作见[新版教程](../user-guide.md)。营销测试页已上线，能够选择权威实物参考图并串联生成、AI QC、反馈修订与导出；下文独立 `/generate` 页的专项边界不代表营销测试页也缺这些能力。生产状态见[上线记录](ai-marketing-production-release.md)。

`/w/{slug}/generate` 复用现有 Gemini、OpenAI、Seedream Adapter，通过同源 BFF 调用版本化 Generation API。

本次接通产品选择、供应商与模型版本、画幅、输出设置、1–4 张变体、创意描述、草稿保存/重开、成本预览/确认、任务进度和单张文件下载。Gemini/Seedream 展示 1K/2K；现有 OpenAI Adapter 按画幅固定像素尺寸，因此展示 Standard/High quality，而不把 high quality 声称为 2K 输出（内部报价配置继续使用 1K/2K 两档）。已发布的创意库产品可以直接选择；兼容已有 Product 身份。产品文本和生成参数在预览中冻结，Worker 使用冻结内容。

此切片是单创意的 Prompt 变体流程。参考素材选择、多维度组合规划、审核、批量导出和 uncertain 人工对账的完整界面仍由各自专项负责；不表示整个新建生成区或跨区黄金路径已经验收。

## 正式生成配置

- Workspace 为 active，具有未过期、签名有效的 Workspace 商业访问快照（与现有 Generation/llm_chat 共用准入规则）。
- 通过现有凭据接口创建供应商凭据并绑定到 `binding_scope=generation`。Owner 管理凭据；Member 可以在预算内生成。新版不读取全局图片 Key 来判断就绪或替代缺失的 Workspace Key。
- Owner 设置页已提供 OpenAI、Gemini、Seedream 的独立自助入口，支持添加、启用、替换和移除，并复用 Owner/MFA/加密/工作区隔离规则。用户暂无两家真实 Key，真实附图验收仍暂停；入口分层验收见 [自助配置记录](provider-credentials-self-service-acceptance.md)。
- 配置 Workspace 的 USD Spend Guardrail。
- 部署配置 `MARKETAI_GENERATION_MAX_COSTS_JSON`，键为 Provider → 模型 → 分辨率，值为每张图片最大预留金额的十进制字符串。价格须由运营核实，包含请求输入和输出的成本边界；旧版 `COST_PER_IMAGE` 只是粗略提示，不用作正式报价。
- 修改价格时同步更新 `MARKETAI_PRICE_REGISTRY_VERSION`。Worker 与 API 使用同一部署配置、凭据保护配置和 BlobStore。

例如以下仅演示格式，金额不是供应商现价：

```json
{ "openai": { "gpt-image-1.5": { "1K": "0.30", "2K": "0.50" } } }
```

未配置价格的模型仍可查看和保存草稿，但不能提交正式生成。缺少 Key、权益、预算或报价时页面显示相应原因。用户可以明确选择受控 Mock，真实应付金额为零；正式生成不会失败后自动切换到 Mock。

## 付费调用与恢复

确认时重新校验草稿 revision、产品和报价，再原子预留每个 RunItem 的成本。Worker 在短事务中记录 ProviderAttempt 和 dispatch fence，事务提交后调用供应商，最后将图片存入 BlobStore、创建 AssetVersion 并记录结算。当前成本以报价上限作估算结算，Provider Cost Ledger 保留 estimated_settlement 属性，待供应商账单对账调整。

同一请求的重复提交返回同一 Run；草稿和预览创建也按幂等键重放。页面刷新从服务端恢复草稿和任务，丢失提交响应时先查询该预览是否已经生成 Run。

Worker 在 fence 之前中断可以继续；fence 之后中断或供应商超时进入 uncertain，保留成本暴露，不再次发送付费请求。成功 RunItem 重投递只读取已有状态。生成结果经过文件有效性检查，人工审核仍为待审核。

## 验证

- `tests/test_generation_selection.py`：真实 HTTP、领域持久化和 Worker 路径；仅替换外部图片 Adapter。
- `web/__tests__/components/workspace/generation-composer.spec.tsx`：供应商/模型联动、凭据提示、预览确认、响应丢失后的同请求重试、刷新恢复。
- `web/__tests__/domain-client/gateway-provider-ssr.spec.tsx`：新版工作区首屏可以在服务端渲染，不提前访问浏览器存储。

单元与接口测试使用 SQLite。PostgreSQL RLS、真实登录跨服务 E2E 和三家供应商付费实测需要相应运行环境，不能由这些测试替代。
