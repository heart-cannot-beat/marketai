# Epic：竞品广告扫描（依赖图，2026-10-26）

> GitHub：[Epic #265](https://github.com/heart-cannot-beat/marketai-archive/issues/265)。编号映射：C-01 #266 · C-11 #267 · C-12 #268 · C-13 #269 · C-21 #270 · C-22 #271 · C-23 #272 · C-24 #273 · C-31 #274 · C-32 #275 · C-33 #276 · C-50 #277 · C-51 #278。本文件与 GitHub 正文以本文件为准同步（第四版，2026-10-26 吸收派活板 §5 的 16 条、§6 的 12 条与 §7 的 9 条；此后的发现写到对应 Issue）。

> **状态：已完成（2026-11-02）。** 13 个切片全部合并并上线：
> - C-50 在生产完成真实验收，见[验收记录](../competitor-scan-acceptance.md)；
> - C-51 补齐上线记录、配置清单与回滚说明，见[上线记录](../ai-marketing-production-release.md#竞品广告扫描上线epic-265)。
>
> 与下文“合并纪律”最后一条不同：迁移没有在 C-51 一次性发布，而是随各切片合并上线。上线过程中另开并修复了 #318、#327、#328、#329、#330、#332、#345。C-50 之后又修复了 #346、#353、#355，以及与竞品无关的 #347、#354（2026-11-02 至 11-03，见上线记录）；#351（与竞品无关）待分诊。

背景：角度引擎的三个输入里，“同品类竞品在投广告”一直没有数据来源。2026-10-26 完成供应商调研与 P0 实测（ScrapeCreators，美国在投广告，128 条，13 额度，15 次调用全部成功），确认数据可用，同时发现三个必须在设计里解决的问题：按名字找主页不准、品牌不等于竞品、素材链接约 4 天过期。规格见 [`docs/product/competitor-scan-spec.md`](../competitor-scan-spec.md)，决策见 [ADR 0014](../../adr/0014-competitor-observations-inform-hypotheses-not-evidence.md)。

开工条件：#218 的 J-50 #282（验收）与 J-51 #283（发布）关闭后才翻 Wave 1 的 `ready-for-agent`；J-42 至 J-46 不是条件，但 Wave 1 开工前 diff 一次 main 并在受影响的 Issue 评论里收窄范围。用法同 #218：子 Issue 带 `ready-for-agent` 且 `Blocked by` 全部关闭才可开工；每个子 Issue 一个会话、一个 `claude/issue-<n>-<slug>` 分支、一个垂直切片 PR。唯一例外见“合并纪律”里 C-31 的提前开工规则。

## Wave 0 — 合同
- [x] C-01 #266 规格 + ADR 0014 + CONTEXT.md 术语（含 BYOK 条目扩展）（文档）

## Wave 1 — 底座（可并行）
- [x] C-11 #267 `market_data` 凭据作用域 + ScrapeCreators 自助入口 + 价格表（五种操作）
- [x] C-12 #268 `AdLibrarySource` 协议 + ScrapeCreators 适配器 + 受控 Mock 夹具 + `CompetitorAd` 规范化
- [x] C-13 #269 竞品清单、解析任务（付费 Job）与主页确认（Blocked by C-11、C-12）

## Wave 2 — 扫描任务
- [x] C-21 #270 扫描任务：报价、预留、状态机、续跑、Worker fetch + media（Blocked by C-11、C-12、C-13）
- [x] C-22 #271 相关性判断与翻转（Blocked by C-21）
- [x] C-23 #272 维度标签、购买主张聚类（扫描级修订）、重新聚类、纯函数快照冻结、产品级“起草时使用竞品快照”开关（Blocked by C-22）
- [x] C-24 #273 planner 消费快照 + 三类检查（crowded / gap / copy_reuse）+ 方案审查展示（Blocked by C-23；无迁移，但改 OpenAPI）

## Wave 3 — 界面
- [x] C-31 #274 竞品面板壳：清单、解析与确认卡片、扫描报价与进度、`competitors` locale 命名空间（Blocked by C-13、C-21 的 OpenAPI 先行 PR）
- [x] C-32 #275 竞品看板：缩略图 + 来源 + 链接、筛选、翻转、标签确认、汇总、新快照提示（Blocked by C-22、C-23、C-31）
- [x] C-33 #276 竞品发现：关键词 → 广告主聚合 → 推荐接受 / 拒绝（Blocked by C-21、C-22、C-31）

## Wave 4 — 验收与发布
- [x] C-50 #277 验收会话：真实 Key、真实客户品类、场景 1–16（Blocked by C-24、C-32、C-33）
- [x] C-51 #278 发布：迁移、OpenAPI 重生成、`db-release`（Blocked by C-50）

## 整版验收
在产品理解页贴 3 个竞品链接，一次扫描后能看到每家在投什么、投了多久、讲的是哪个购买主张；整车品牌的汽车广告被判为无关且没花视觉打标的钱；起草方案时某个角度与 3 家竞品重叠会出现提醒，照抄竞品文案会被拦下；缩略图下都有“来源：Meta Ad Library”与原始链接；所有费用在确认前可见，在证据里可对账；未配置凭据时只能走受控 Mock。

## 合并纪律
- 迁移与 OpenAPI 串行链：C-11 → C-13 → C-21 → C-22 → C-23 → C-24 → C-33，同一时间只合一个；C-24 不含迁移，但 `ReviewItem.kind` 等字段会改 OpenAPI，所以占链上一位。
- C-13 与 C-21 各自先合一个“OpenAPI 先行”PR（只含 `contracts/openapi/*.json` 与 allowlist）；两个先行 PR 都合并后，总控可给 C-31 加 `ready-for-agent` 并在评论里注明依据的 PR（同 #218 中 J-12 的先例）。
- 前端冲突热点是 `MarketingWorkspace.tsx` 与 locale 文件：C-31 先落“竞品”区容器、路由与 `competitors` locale 命名空间；C-32、C-33 只在 `web/components/competitors/` 下加组件，不再改 `MarketingWorkspace.tsx`；`MarketingPlanReview.tsx` 只在 C-24 改，其文案也走 `competitors` 命名空间；该命名空间文件由 C-24 与 C-31 中先合并者创建，后者追加键。
- 新增 key 同时写 en 与 zh-CN；每个新增错误码必须有文案。
- 每个 Wave 在本机生产构建（compose 栈）过一遍验收再开下一 Wave；线上只在 C-51 一次性发布迁移。
