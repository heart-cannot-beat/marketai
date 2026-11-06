# 竞品广告扫描规格

- 状态：已上线（Epic #265）。2026-11-02 完成真实验收，见[验收记录](competitor-scan-acceptance.md)；切片、迁移、配置与回滚说明见[上线记录](ai-marketing-production-release.md#竞品广告扫描上线epic-265)。供应商选型（ScrapeCreators）已于 2026-10-26 用真实 Key 完成 P0 实测；本规格规定产品与验收边界，实际完成范围见各实施 Issue。
- 更新：2026-10-26（第四版：吸收派活板 §5 的 16 条、§6 的 12 条与 §7 的 9 条矛盾与缺口；此后的发现写到对应 Issue，不再改规格，除非涉及术语或跨 Issue 的合同）；2026-11-02：§13 场景 7 按维护者在 PR #349 的决定改为“可以归入某簇”；2026-11-03：§5.1、§5.2 cluster 行、§9“扫描”与 §13 场景 17 按维护者在 #353 的决定（方案 A）写明“成功的重新聚类取代扫描内未作答的聚类步骤”。
- 依据：2026-10-26 供应商调研与 P0 实测（`data/p0-scrapecreators/runs/20261026-163158/`，未入库；本文与夹具里的品牌、主页和域名都换成了虚构名，数量保留 P0 原值）；[ADR 0014](../adr/0014-competitor-observations-inform-hypotheses-not-evidence.md)。
- 术语：[CONTEXT.md](../../CONTEXT.md)（本规格 §2 列出拟新增条目）；派发与依赖：[Epic #265](https://github.com/heart-cannot-beat/marketai-archive/issues/265)（正文见 [competitor-scan/00-epic.md](competitor-scan/00-epic.md)）。
- 排期：在 Epic #218 的 J-50 #282（验收）与 J-51 #283（发布）关闭之后开工；J-42 至 J-46 不是开工条件，但 Wave 1 开工前要 diff 一次 main。

## 1. 产品承诺与边界

竞品扫描让用户在确认产品理解的同一页看到：同品类竞品在目标市场正在投什么广告、投了多久、讲的是什么购买主张，以及哪些主张已经很拥挤、哪些还没人讲。AI 把这些观察整理成起草角度与表达时的参考，用户据此判断要不要差异化、要不要补证据。

它只回答“别人在讲什么”，不回答“什么有效”。一条广告投得久，只说明广告主还在为它付费，不说明它对用户的产品、客群或市场有效。竞品观察不进入产品事实，不进入投放验证结论，不进入实验与归因，不作为生图参考，不复制竞品文案或画面。

扫描使用 Workspace 自己的 ScrapeCreators 凭据（BYOK，ADR 0005；CONTEXT.md 的 BYOK 条目扩展为“外部付费服务凭据”，涵盖 AI Provider 与数据供应商），按次付费，每次解析与扫描都先报价、预留再执行；未配置凭据只能走受控 Mock。首版只覆盖 Meta 广告库（Facebook / Instagram），只处理图片与视频封面帧，不处理视频文件与口播转录。

## 2. 术语（已写入 CONTEXT.md。编辑顺序：先改本节，再同步 CONTEXT.md；两处不一致时以 CONTEXT.md 为准并补一次同步）

**竞品**:
用户为某个产品确认的对比对象，绑定一个已确认的 Meta 主页 ID、主页别名与官网域名；确认是追加的不可变修订，未确认的候选不能扫描。产品自己的主页可以 `role=self` 加入，作为对照，不计入拥挤度。
_Avoid_: 同名主页、品牌名字符串、按名字搜到的第一个结果

**竞品扫描**:
一次按市场与平台执行的付费抓取任务及其分步证据、凭据版本 ID、每页调用记录与成本；只接受已确认的竞品；同一 `Idempotency-Key` 重放返回同一次扫描，`partial` 可续跑，需要新数据就发起新扫描。
_Avoid_: 页面上的实时查询、无成本记录的抓取、跨 Workspace 共享的缓存、按日期去重

**竞品观察**:
某次扫描在某市场看到的一条公开广告的外部事实：广告主、广告库 ID、首次与最后看到日期、在投状态、形式、文案、素材存档引用、来源供应商、抓取时间与来源路径（按主页拉取或关键词搜索）。它是“看到了什么”，不是“有效什么”。
_Avoid_: 产品事实、投放验证结论、外部素材、参考素材、效果证据

**竞品判断**:
AI 对竞品观察做出的相关性判断（相关 / 相邻 / 无关）、维度标签，以及对整次扫描做出的购买主张聚类，属于 AI 推测；用户可翻转、确认或重新聚类，每次形成追加的不可变修订，最新修订生效；扫描时的 AI 输出语言随扫描参数 `language` 冻结。
_Avoid_: 事实、自动纳入学习库、按名称相同合并

**竞品快照**:
某次扫描中相关观察、其最新判断修订与最新聚类修订的纯函数冻结摘要，带内容 hash，冻结本身不调用 LLM、不付费；方案修订（`marketing_revisions`）以 TestPlan 字段 `competitor_snapshot_id` 引用它，起草时默认取同产品同市场的最新快照，重扫形成新快照，旧方案继续引用旧快照。
_Avoid_: 实时汇总、可变引用、把快照当作方案结论

## 3. 对象关系

```
Product ──1:n── Competitor（role=competitor|self）──1:n── CompetitorIdentityConfirmation（追加修订）
   │
   ├──1:n── CompetitorScan ──1:n── CompetitorAd（竞品观察）──1:n── CompetitorAdAssessment（相关性 / 标签，追加修订）
   │            │                      └──1:n── CompetitorAdOrigin（来源路径，追加）
   │            ├──1:n── CompetitorScanAssessment（购买主张聚类，追加修订）
   │            └──1:n── CompetitorScanSnapshot（纯函数冻结摘要，hash）◀── marketing_revisions.content_json.competitor_snapshot_id
   │
   └──1:n── CompetitorDiscoveryDecision（关键词发现的接受 / 拒绝，追加）
```

- 竞品属于产品，随产品的 Workspace 隔离；一个竞品可以被多次扫描。
- `competitor_ads` 不存竞品归属列：一条观察属于哪些竞品，由追加表 `competitor_ad_origins` 里的 `page:<competitor_id>` 来源行投影得出；关键词发现产生的观察只有 `keyword:<词>` 来源行，只在用户把该广告主确认为竞品后才可被下一次扫描按主页拉取。同一次扫描里同一条广告不论来自几条路径、来自首轮还是续跑，都只存一行观察，任何时候只追加来源行，永远不需要 UPDATE。
- 快照只引用“相关”观察；“相邻”观察出现在汇总的独立段落；“无关”观察保留在扫描证据里，不进快照。`role=self` 的观察单独成组，不计入拥挤度的广告主数。

## 4. 竞品清单与主页确认

### 4.1 输入

| 输入 | 处理 | 是否自动确认 |
| --- | --- | --- |
| Facebook 主页链接（`facebook.com/<alias>` 或 `/profile.php?id=`） | 解析别名或 ID，不搜索 | 是 |
| Ad Library 链接（含 `view_all_page_id`） | 解析 page id，不搜索 | 是 |
| 官网域名 | 作为域名核对依据；配合名字搜索 | 否，除非名字完全一致且域名命中 |
| 名字 | 搜索候选，打分，域名核对 | 否 |

产品自己的主页用同样的输入加入，`role=self`。

### 4.2 搜索打分（只有名字时）

1. 排除 `page_is_deleted`。
2. 名字规范化（去空格、标点、大小写）后完全一致的候选优先。
3. 分类粗过滤：保留商业类（Shopping & Retail、Automotive Parts Store、Brand、Product/Service 等），排除 Event、School、Fan page、Political Organization、Just For Fun 等。
4. 赞数只在前两项平手时参考。`entity_type` 不可靠，不用。P0 中 27 万赞的 Acme Charge Fuel Systems 与 3,509 赞的真正 Acme Charge 同名，是反例。
5. 对前 2–3 个候选各拉一页在投广告（每个 1 额度，由 §4.4 的解析任务承载），比较落地页域名与用户给的官网。P0 中 Acme Charge 58 条全部指向 `acme-charge.example`，Brightvolt 12 条全部指向 `brightvolt.example`。

### 4.3 确认卡片

展示头像、主页名、别名、分类、赞数、Instagram 账号、在投广告数、落地页域名、域名是否命中。用户确认后写入 `CompetitorIdentityConfirmation`（修订追加）；之后扫描只用确认的 page id，不再搜索。用户可以重新确认到另一个主页，旧扫描保留其当时的主页身份。

### 4.4 解析任务

名字搜索与域名探测都是付费调用，由独立的 Job `competitor.resolve` 承载，不属于扫描：

- 报价 = `page_search` × 1 + `probe` × min(3, 候选数)；页面在点击“解析”前显示最高额度与 USD；确认时原子预留，Spend Guardrail 生效；只给链接的输入不发起解析任务。
- 每次调用一个 ProviderCallAttempt + dispatch fence，按 `credits_charged` 结算；候选、打分与探测结果写入解析证据，供确认卡片与确认修订引用。
- 解析结果 72 小时内可重复用于确认，不重复付费（短于头像签名链接约 104 小时的有效期，确认修订不存头像；解析证据保留头像副本，只在可复用窗口内签发其短时签名链接）；超过 72 小时再次确认需重新解析。
- 受控 Mock 的解析用夹具里的候选列表，真实应付为零。

## 5. 扫描

### 5.1 参数、报价与状态

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| 市场 `country` | 方案市场（如 `US`） | 一次扫描一个国家 |
| 平台 | Meta | 首版唯一选项 |
| 状态 | 在投 | 首版不拉历史广告 |
| 每家页数上限 | 3（约 50 条） | 第 1 页 30 条，之后每页 10 条，每页 1 额度 |
| 关键词（可选） | 无 | 用于竞品发现（§8），每个关键词默认 2 页 |
| 排序 | 曝光 | 供应商只保证顺序，不保证曝光数真实 |
| AI 输出语言 `language` | 发起者的界面语言（`User.locale`） | 冻结进参数；相关性理由、标签理由、聚类主张与汇总都用它 |
| 视觉打标上限 `max_tagged_ads` | 60 | 只对在投最久的前 N 条相关观察做视觉打标，用来封顶 LLM 费用 |
| 发现广告主上限 `max_discover_advertisers` | 30 | 关键词发现最多判断 N 家广告主（按观察数排序），用来封顶 LLM 费用 |

扫描只接受已确认的竞品。报价分两行，确认前都可见：（a）供应商额度 = （页数上限 × 竞品数 + 关键词页数 + 素材补拉配额）× 单次额度 × 额度单价；（b）LLM 最高费用：按 `llm_chat` 现有的 `(job_id, call_name)` 粒度在受理时一次性预留到上限——`filter:1..⌈最多观察数 ÷ 20⌉`（最多观察数 = 页数 × 30）、`tag:1..max_tagged_ads`、`cluster:1`、`discover:1..max_discover_advertisers`（有关键词时），每个调用名按 `quote_llm_chat` 的上限预留，未用到的调用名在扫描结束时释放；预留、执行与重放使用同一组序号调用名（§5.2）；不给 `llm_chat` 增加总额预算模式。确认时两项在同一 Operation 下原子预留（成本预留语义同生成），任一失败整体拒绝；Spend Guardrail 对两项合计生效；实际结算后释放余量。受控 Mock 真实应付为零并单独记录模拟估算。

执行模式不混用：`execution_mode=byok` 要求 `market_data` 凭据与 `llm_chat` 所需的 AI 凭据同时就绪；缺任一项，页面只提供受控 Mock，整次扫描（抓取与判断）都是 Mock；真实扫描失败后不切 Mock。

扫描级状态机：`queued → running → completed | partial | failed | uncertain`。`partial`：至少一页成功且有页失败或余额不足；`failed`：无一页成功；`uncertain`：存在 fence 之后中断、尚未对账的页。另有 `stage` 记录最后完成的步骤（fetch / media / filter / tag / cluster / freeze）。`partial` 与 `failed`（402）在充值后可 `resume`：同一扫描 ID，从已存 cursor 继续，只对剩余页数与剩余 LLM 步骤重新报价与预留（调用键含 `job_id`：续跑若沿用原 Job，已记录的答案按原调用名重放；若新建 Job，只为尚未尝试过的序号预留与调用，已有答案的序号直接读取证据，不再预留、不再调用）；扫描内的 cluster 步骤没有作答、但已有一次成功的重新聚类时，该步骤视为已被重新聚类取代，不再计入剩余 LLM 步骤，失败的重新聚类不算（#353）；续跑可使用新的凭据版本，凭据版本以每次 ProviderCallAttempt 记录为准，扫描行只存首个版本并在 `evidence_json.resumes[]` 记录后续版本。想要新数据就发起新扫描；幂等只按 `Idempotency-Key`，不按日期。

同一凭据版本上的 `market_data` 任务（解析、扫描、续跑）在 Workspace 内串行执行：同一时间只有一个在跑，后来的排队并在证据里记录等待。`uncertain` 的扫描或解析结束后不再占轮次，别的任务可以在它与对账之间扣费；续跑对账时从基线 `credits_remaining` 中扣除期间同一凭据版本上所有已知的扣费（扫描页、解析调用、先前已对账的页）；窗口内只要有费用未知的调用（`uncertain`，或 5xx 之后成功），就不自动判定，交给 Owner 通过 `reconcile` 标记。

`uncertain` 页的对账（C-21 负责）：续跑开始时先免费调用 `/v1/account/credit-balance`，与该页之前最后一次成功调用记录的 `credits_remaining` 比较。差额为 0 → 结算“未扣”；差额恰为 1 → 结算“已扣”；其他任何值（负数、大于 1，或该页之前的记录本身来自 `cached` 响应）→ 不自动判定，扫描停在 `uncertain`，由 Owner 通过 `reconcile` 端点标记。用户在 MarketAI 之外用同一 Key 的消费无法排除，差额恰为 1 时最多误判一个额度（约 $0.002），记入风险说明。对账只结算费用，不恢复响应：该页按新尝试重新拉取。

### 5.2 Worker 步骤

同一个 Job，分步执行，每步在 `evidence_json[step]` 记录 `{status, started_at, finished_at, attempts, ...}` 并追加一条 `competitor_scan.step` 审计事件；不在网络或 blob I/O 期间持有事务。

| 步骤 | 付费 | 幂等与恢复 |
| --- | --- | --- |
| **fetch** | 是（每页） | 先全部主页，再关键词；每页一个 ProviderCallAttempt + fence；cursor 写入证据，重试从断点继续；供应商 4xx（非 429）终止该竞品并记录；429/5xx 重试 2 次后标记该竞品失败；fence 之后中断进入 `uncertain`，不重发；按 `credits_charged` 结算；402/401/403 终止扫描并释放未用预留 |
| **media** | 仅补拉 | 下载 `original_image_url` 与 `video_preview_image_url`，sha256 去重，purpose `competitor_media`；派生 512px 缩略图；记录去签名来源 URL、抓取时间、hash、大小、类型与链接过期时间（`oe=` 十六进制 unix 时间，P0 观测约 104 小时）；403 时用 `/ad?id=` 补拉新链接（`ad_refresh`，付费，走 fence，计入补拉配额），超配额标“素材不可用” |
| **filter** | 是（llm_chat 文本） | 按 `collation_id` 合并变体后分批（20–30 条）判断；按 `(Job, "filter:<i>")` 重放已记录答案，不重复付费 |
| **tag** | 是（llm_chat 视觉） | 只对“相关”观察；词表按产品当前维度词表在扫描受理时冻结（复用 `app.external_creatives.vocabulary`），已冻结的词表不改写；DCO 广告逐卡片看图，合成一条标签建议；按 `(Job, "tag:<i>")` 重放 |
| **cluster** | 是（llm_chat 文本，一次） | 购买主张聚类（§7.1），结果写入扫描级判断修订（kind=`clusters`）；按 `(Job, "cluster:1")` 重放；已有成功的重新聚类时，续跑不再预留或调用 `cluster:1`，步骤记录保持原样，生效的是重新聚类的修订（#353） |
| **freeze** | 否 | 纯函数：由观察、最新判断修订与最新聚类修订生成快照与 hash，不调用 LLM |

词表来源（#329，维护者决定 2026-11-02）：(1) 产品各就绪测试方案的维度（按维度键合并）；(2) 产品生成草稿选中的维度模板；(3) 表达机制目录 `expression-catalog-v1`（`app/marketing/expression_catalog.py`）作为一个独立维度「表达机制」（`source=expression_catalog`），每个产品都有。目录维度的取值恰好是目录的 10 个 `visual_mechanism` 键，不接受 `other:`，看不出或不匹配沿用 `none`；每个取值的成员是（目录、`CATALOG_VERSION`、机制键）三元组；它始终占 12 个维度名额中的一个、排在方案与模板维度之后；名称按冻结时的扫描 `language`（en / zh-CN）写入，打标提示词把目录的 `how_to_tell` 作为判断说明。营销项目在产品理解确认时新建的产品锚点没有发布版本、也就没有测试方案，因此在测试方案起草前的第一次扫描就能打标；外部素材打标共用同一词表，没有测试方案的产品也能打标（目录取值不是测试方案的维度概念，不写维度概念键、不进实验身份）。#329 之前受理的扫描与外部素材保留各自冻结的词表（可能为空，续跑仍记 `skipped: no_vocabulary`）。

LLM 调用名在预留、执行、重放三处必须相同（`llm_chat` 按 `(job_id, call_name)` 查找预留；找不到预留的 BYOK 调用以 `reservation_missing` 失败）。`filter:i`、`tag:i`、`discover:i` 都是从 1 开始的序号，`cluster:1` 为固定名；同名重新聚类在它自己的 Job 下使用 `cluster:1`。每步在首次调用前，把“序号 → 对象”对应表冻结进 `evidence_json[step].assignments`：filter 是按 `collation_id` 合并、排序后的批次划分（每批 20–30 条）；tag 是相关观察按在投天数倒序、再按 `ad_archive_id` 排序后的前 `max_tagged_ads` 条；discover（§8）是规则前置后的广告主按观察数倒序、再按 `page_id` 排序后的前 `max_discover_advertisers` 家。重投与重试都读取冻结的对应表，不重算；对应表之外的序号在该步结束时用 `release_llm_chat_reservation` 释放。对应表冻结之后的相关性翻转，只影响下一次重新冻结或重新聚类，不改这次扫描的对应表。

扫描失败不自动切换供应商、不回落到平台凭据、不静默改用 Mock。C-21 交付时 filter / tag / cluster / freeze 以 `skipped: not_implemented` 记录，扫描仍到 `completed`；后续 Issue 填充各步。

### 5.3 规范化：`CompetitorAd`

从供应商响应映射，每个字段记录来源字段名：

| 字段 | 来源 |
| --- | --- |
| `ad_archive_id`、`collation_id`、`collation_count` | 同名 |
| `advertiser_page_id`、`advertiser_name`、`page_profile_uri`、`page_categories` | `page_id`、`page_name`、`snapshot.*` |
| `is_active`、`start_date`、`end_date` | `is_active`、`start_date_string`、`end_date_string` |
| `days_running` | 扫描时间 − `start_date`，派生 |
| `display_format`、`publisher_platforms` | `snapshot.display_format`、`publisher_platform` |
| `cards[]`：`title`、`body`、`cta_text`、`cta_type`、`link_url`、`link_description`、`image_ref`、`video_poster_ref` | `snapshot` 顶层与 `snapshot.cards[]`，顶层视为第 0 张卡片 |
| `landing_domain` | `link_url` 主域，派生 |
| `library_url` | `url` |
| `origins[]`（存于 `competitor_ad_origins`） | `page:<competitor_id>` 或 `keyword:<关键词>`，一条观察可多条来源 |
| `raw_ref` | 原始响应的 blob 引用，供复核 |

P0 字段覆盖：开始日期、在投状态、标题、CTA、落地页 100%；正文 97%；每条都有图片或视频。缺失字段保留为空，不猜测。

## 6. 凭据与成本

- 新增凭据作用域 `binding_scope=market_data`，供应商 `scrapecreators`，请求头 `x-api-key`。Owner 在设置页自助添加、启用、替换、移除，复用 Owner/加密/隔离规则（MFA 只在登录时验证，ADR 0015）。验证凭据用 `/v1/account/credit-balance`：该接口对无效 Key 也返回 200，以 `creditCount > 0` 判定有效。
- 价格表 `MARKETAI_MARKET_DATA_MAX_COSTS_JSON`，键为供应商 → 操作，值为 `{"credits": 1, "credit_usd": "0.00188"}`（Freelance 档 $47 / 25,000）。操作：`page_search`（`/search/companies`）、`probe`（解析时拉一页 `/company/ads`）、`page`（扫描拉一页 `/company/ads`）、`keyword_page`（`/search/ads` 一页）、`ad_refresh`（`/ad`）。修改时同步更新 `MARKETAI_PRICE_REGISTRY_VERSION`。未配置价格不能提交真实解析或扫描。
- 每次付费调用一个 `ProviderCallAttempt`，证据记录实际使用的 CredentialVersion ID（续跑后可与扫描首个版本不同）、请求参数（去凭据）、`credits_charged`、`credits_remaining`、耗时。Provider Cost Ledger 以 `credits_charged × credit_usd` 做估算结算，供应商不出账单，不做追加对账。
- 扫描内的 LLM 调用（filter、tag、cluster、discover）沿用 `llm_chat` 的报价、预留、受理与重放，按调用名预留到上限（§5.1），预留、执行与重放同名（§5.2）；它们的上限在扫描报价里单独一行显示，与供应商额度在同一 Operation 下一起预留。重新聚类是独立的付费操作，有自己的报价与预留。
- 同一凭据版本的解析、扫描与续跑在 Workspace 内串行（§5.1）。
- 余额不足（402）与凭据被拒（401/403）立即终止任务，释放未用预留，页面显示原因。
- 解析任务的费用由发起解析的成员所在 Workspace 预留；Member 可以发起解析与扫描，Owner 管理凭据与归档竞品。

## 7. 判断、快照与起草

### 7.1 相关性、标签与主张聚类

- 相关性判断输入：标题、正文、`link_description`、落地页路径、广告主分类，加上产品理解的 `category_need`、`product_name` 与 `situations`。输出 `relevant | adjacent | unrelated`、置信度与一句理由。不看图，不付视觉费用。
- 规则前置：关键词发现结果中，落地页为 `facebook.com` 个人账号、`alibaba.com` 等批发站、分类为经销商/车行的广告主先标 `unrelated`，不送 LLM。
- 标签：对相关观察按产品维度词表给出每个维度的建议值与理由，服务于元素配置的参考；用户确认或修改形成修订。未确认的标签在汇总里标“AI 建议”。
- 购买主张聚类（cluster 步骤，一次文本 LLM 调用）：把相关观察按“广告在承诺什么购买理由”聚成 `claim_clusters`，每个簇有 ID、一句话主张、广告主列表（标出 `role=self`）、观察 ID 列表、最长在投天数；结果作为扫描级判断修订（kind=`clusters`）追加保存。这是 AI 推测，不是方案角度；簇不与任何维度词表对齐；同样的输入再聚一次可能得到不同的簇，所以每次聚类都是新修订，不承诺可重复。
- 翻转：用户可把任何观察在三种相关性间翻转；翻转后手动“重新冻结”（免费、确定性）生效：翻为无关的观察从其簇中移除，翻为相关的观察进入“未归类”桶，直到用户点“重新聚类”（付费，有报价与预留，生成新的聚类修订）。历史快照不改。
- 语言：扫描时所有 AI 输出使用扫描参数 `language`（发起者的界面语言，冻结）；planner 消费快照时若方案的 `strategy_language` 不同，由 planner 在自己的输出里转述。

快照内容：扫描 ID、参数、相关观察的 ID 列表、每条的最新判断修订 ID、引用的聚类修订 ID 与其 `claim_clusters`（按当前相关性过滤后的成员）、“未归类”列表、按维度值的计数、长跑广告（≥60 天）列表、相邻观察摘要、`role=self` 观察摘要；hash 覆盖以上全部。freeze 是这些修订的纯函数：同样的修订集合两次冻结得到相同 hash；快照按 (scan_id, hash) 唯一，hash 未变的重新冻结返回已有快照，不新建行。

### 7.2 起草时的使用

- `TestPlan` 新增可选字段 `competitor_snapshot_id`（ADR 0013 的方案字段做法，写进 `marketing_revisions.content_json`，不加列）。起草时服务端默认取同产品、同市场的最新快照写入该字段。“同市场”的定义：方案的 `market` 是自由文本，按部署配置 `MARKETAI_MARKET_COUNTRY_ALIASES_JSON`（如 `{"北美": "US", "north america": "US", "usa": "US", "美国": "US"}`，匹配前小写、去空白）规范化为两位国家码，与扫描的 `country` 相等即同市场；规范化失败则不自动选择快照，读取投影给出 `competitor_market_unmatched: true`，界面提示用户在扫描时选对市场。产品级开关 `products.competitor_snapshot_in_drafts`（默认开；列与写接口归 C-23，C-24 读取，C-32 展示）为关时不写入；选择特定快照的功能不在首版。`competitor_snapshot_stale` 为真当且仅当同产品同市场存在创建时间更晚、且 hash 不同的快照，其他市场的快照不参与。planner 的 `inputs` 新增 `competitor_snapshot`：`claim_clusters`（每簇附广告主数、最长在投、库链接）、长跑广告摘要、相邻观察摘要。不传素材，不传完整文案，每簇不超过 200 字，总大小计入现有 96000 字节上限。
- planner 为每个角度输出 `competitor_overlap: [cluster_id…]`（语义匹配，AI 推测），并在引用竞品的角度 `rationale` 中注明“竞品观察”。系统提示词新增约束：竞品观察只能用于说明某个角度“竞品已讲 / 未讲”；不得把竞品的声明写成本产品的事实或证据；不得复用竞品标题、正文或画面描述。
- 服务端据此确定性地生成两类 `advisory`、`source=system` 的 `plan_review`：
  - `competitor_crowded`：角度引用的簇合并后，去掉 `role=self` 的广告主数 ≥3。服务端校验引用的簇 ID 存在于快照，数字只从快照取。
  - `competitor_gap`：角度没有引用任何簇。文案固定附注“可能是空白，也可能别人试过无效”，不自动提升优先级。
- 服务端新增 `blocking`、`source=system` 的 `competitor_copy_reuse`，两条规则任一命中即阻塞：（a）headline 归一化（小写、去标点、合并空白）后与快照内任一相关观察的标题或任一卡片标题整句相同，且长度 ≥3 个词或 ≥6 个 CJK 字符；（b）headline 或 body_copy 与相关观察的标题、正文出现 ≥8 个连续相同的词（拉丁文字）或 ≥12 个连续相同字符（CJK）。`role=self` 观察的文案不进入比对语料（自家在投文案可以复用）；产品名、品牌名、认证名与 CTA 常用词（白名单来自产品理解与配置）不计入。这是 ADR 0014 “不得复用”的落地。
- 重扫后已确认的方案不变，页面提示“有新的竞品快照，重新起草可采用”。

## 8. 竞品发现

关键词搜索 → 规则前置过滤 → 按广告主聚合 → LLM 判断“该广告主是否销售同品类产品”（相关 / 相邻 / 无关 + 理由）→ 推荐列表 → 用户逐个接受或拒绝（写入 `CompetitorDiscoveryDecision`，追加）；接受走 §4.3 的确认卡片，page id 直接来自广告，不再搜索也不付解析费；拒绝过的广告主在后续扫描里不再推荐。

P0 结果：`NACS adapter` 与 `CCS1 adapter` 两个词各 2 页找出 Northpeak、Lumenline、Kestrel Power、Ridgeway Auto Parts 等名单外广告主，也混入车行、批发站、个人账号；Brightvolt 没有出现在关键词结果里，发现不能替代用户维护清单。

## 9. 界面

在产品理解页新增“竞品”区，同页完成清单、确认、扫描与查看。所有竞品相关文案（包括 C-24 三类审查项的文案）放独立的 `competitors` locale 命名空间（en 与 zh-CN），不进 `common.json`；命名空间文件由 C-24 与 C-31 中先合并者创建，后者追加键；`MarketingPlanReview` 同时加载两个命名空间；C-31 先落“竞品”区的容器、路由与命名空间，C-32、C-33 各自在 `web/components/competitors/` 下加组件。

- **清单**：输入框接受 §4.1 四种输入；每行显示确认状态、`role`、最近一次扫描时间；解析前显示解析费用。
- **扫描**：参数、最高费用（供应商额度与 USD、LLM 上限两行）、确认按钮；进度按步骤显示；失败显示步骤与原因；聚类步骤已被重新聚类取代时显示“已由重新聚类完成（第 N 版聚类）”，N 为最近一次成功的重新聚类的修订号，接口里的步骤状态不变（#353）；`partial` 显示“续跑”按钮与剩余报价；`uncertain` 显示待对账页及自动对账结果，Owner 可手动标记。
- **看板**：每条观察一张卡片：512px 缩略图（视频显示封面帧并标“视频”）、广告主、在投天数、形式、标题、CTA、相关性标记、标签建议；每张缩略图下固定显示“来源：Meta Ad Library”与原始链接。筛选：竞品（含自家）、相关性、在投天数（≥30 / ≥60 / ≥90）、形式。
- **汇总**：购买主张簇（几家、几条、最长在投）、“未归类”观察、相邻观察、长跑广告、自家对照；“重新冻结”（免费）与“重新聚类”（付费，先报价）两个动作；“起草时使用竞品快照”开关；一句话结论由 AI 起草并标“AI 推测”；“有新的竞品快照”提示在此区。
- **发现**：推荐的广告主列表与理由，逐个接受或拒绝。
- **方案审查**：`competitor_crowded`、`competitor_gap`、`competitor_copy_reuse` 在现有 `MarketingPlanReview` 中展示（C-24 负责）。
- 手机 390×844 可用；缩略图不可下载、不可拖入参考素材、不进导出包。

## 10. 隐私与权利

- 所有观察、素材存档与判断按 Workspace 隔离（复合外键 + RLS）。清理随 Workspace 生命周期：`deletion_pending` 期间不得发起解析与扫描、不得使用凭据；物理清理由平台统一的 Workspace 清理流程负责（尚未实现），届时追加表按现有追加表的同一处理方式处理，素材 blob 一并删除。本规格不单独定义级联删除。
- 素材只在 Workspace 内展示，不导出、不公开签名、不作为生图参考、不进入创意库与本地化。
- 调用使用用户自己的供应商凭据，供应商条款的使用关系在用户与供应商之间；界面在首次配置凭据时说明这一点。
- 不抓取、不存储广告主以外的个人信息；关键词发现中个人账号的广告按 `unrelated` 处理且不展示头像。

## 11. 数据模型（迁移）

全部表带 `workspace_id` 复合外键与 RLS；金额 `Numeric(24, 12)`；标注“追加”的表由触发器拒绝 UPDATE/DELETE，与现有追加表一致。

| 表 | 关键列 | 性质 | Issue |
| --- | --- | --- | --- |
| `competitors` | id, workspace_id, product_id, name, role(competitor/self), website_domain, status(active/archived), created_at | 当前态 | C-13 |
| `competitor_identity_confirmations` | competitor_id, revision, platform, page_id, page_alias, page_name, resolve_job_id?, evidence_json（候选、打分、域名探测）, confirmed_by, confirmed_at | 追加 | C-13 |
| `competitor_scans` | id, workspace_id, product_id, market, platform, status, stage, params_json（含 language、max_tagged_ads）, provider, initial_credential_version_id, execution_mode(mock/byok), evidence_json（含 resumes[]、对账结果）, cost_json（供应商 + LLM）, job_id, snapshot_id, created_at | 当前态 | C-21 |
| `competitor_ads` | id, workspace_id, scan_id, provider, ad_archive_id, collation_id, advertiser_page_id, advertiser_name, is_active, start_date, end_date, display_format, publisher_platforms_json, cards_json, landing_domain, library_url, media_json, raw_ref, observed_at；唯一 (workspace_id, scan_id, ad_archive_id) | 追加 | C-21 |
| `competitor_ad_origins` | id, workspace_id, competitor_ad_id, origin(page:<competitor_id> / keyword:<词>), attempt_id, observed_at；唯一 (workspace_id, competitor_ad_id, origin) | 追加 | C-21 |
| `competitor_ad_assessments` | competitor_ad_id, revision, kind(relevance/tags), source(ai/user), payload_json, model_evidence_json, created_at | 追加 | C-22（relevance）、C-23（tags） |
| `competitor_scan_assessments` | scan_id, revision, kind(clusters), source(ai), payload_json（claim_clusters）, model_evidence_json, created_at | 追加 | C-23 |
| `competitor_scan_snapshots` | id, workspace_id, scan_id, cluster_revision, hash, summary_json, created_at；唯一 (workspace_id, scan_id, hash) | 追加 | C-23 |
| `products.competitor_snapshot_in_drafts` | 新增布尔列，默认 true | 当前态 | C-23 |
| `competitor_discovery_decisions` | id, workspace_id, product_id, scan_id, advertiser_page_id, decision(accept/reject), reason, decided_by, decided_at | 追加 | C-33 |

方案修订不加列：`competitor_snapshot_id` 是 TestPlan 字段，存于 `marketing_revisions.content_json`（C-24，无迁移）。

## 12. 与既有规格和实现的关系

- 复用：`products/intake_*` 的分步 Worker 与证据写法；`external_creatives` 的词表冻结、AI 打标与确认修订；`providers/llm_chat` 的受治理调用与重放；`entitlements` 的凭据、成本预留、ProviderCallAttempt 与 Spend Guardrail；`workflow.board_thumbnail_refs` 的 512px 派生签名；ADR 0013 的“方案字段写进 content_json”。
- 不改变：ADR 0005（BYOK，术语条目扩展但决策不变）、ADR 0007（创意库版本边界）、ADR 0010（问题与证据边界）、ADR 0013（方案字段）。竞品观察不是外部素材（Issue #168），不走上传门禁与权利声明，也不能进实验。
- 供应商抽象：`app/competitors/sources/base.py` 定义 `AdLibrarySource`（`search_pages`、`ads_for_page`、`search_ads`、`ad_detail`，各自声明平台、国家、计费单位）。首版实现 ScrapeCreators 与受控 Mock；Adyntel 作为备选不在首版实现。
- 迁移与 OpenAPI 的串行链：C-11 → C-13 → C-21 → C-22 → C-23 → C-24 → C-33；C-24 不含迁移，但改 `ReviewItem.kind`、`competitor_overlap` 与 `competitor_snapshot_stale` 会改 OpenAPI，因此占链上一位。
- Wave 验收在本机生产构建（compose 栈）进行；线上只在 C-51 用 `make db-release` 一次性发布全部迁移。

## 13. 关键验收场景

1. 贴 Ad Library 链接 → 直接确认，不发生解析任务，证据里没有 `page_search` 与 `probe` 记录。
2. 只填“Acme Charge”+ 官网 `acme-charge.example` → 解析任务报价 1 + 3 额度，候选 ≥5 个，域名探测后命中 `acmecharge`，卡片显示命中；不填官网时不自动确认。
3. 扫描 3 家 × 3 页 → 报价显示供应商额度与 USD（不含探测）和 LLM 上限两行；确认后两项同一 Operation 预留；完成后结算额度与 LLM 费用均 ≤ 预留，余量释放，证据中每页有 `credits_charged`。
4. 中途 402 → 扫描 `partial`，已拉到的观察可见，未用预留释放，页面显示“额度不足”与续跑按钮。
5. Worker 在某页 fence 之后被杀 → 该页与扫描 `uncertain`，不重发；续跑时用 `credits_remaining` 差额自动对账：差 0 为“未扣”、差 1 为“已扣”、其他留给 Owner 手动标记；该页按新尝试重拉，观察不重复。
6. 素材链接过期（模拟 403）→ `ad_refresh` 补拉一次成功；超过补拉配额时该观察标“素材不可用”，缩略图位显示占位与原始链接。
7. 整车品牌 Halcyon Motors 的自动驾驶广告 → `unrelated`，不产生视觉打标调用；用户翻转为 `relevant` 后重新冻结（免费）进入“未归类”，重新聚类（付费、先报价）后可以归入某簇；模型也可以把主张不清楚的广告留在“未归类”（维护者在 PR #349 对 C-50 场景 7d 的决定）。
8. 快照 hash 在观察、判断修订、聚类修订或参数任一变化时改变；同一组修订两次冻结得到相同 hash（冻结不调用 LLM）；hash 未变的重新冻结返回同一快照、不触发 stale；起草默认取同产品同市场（按别名表规范化）的最新快照，旧方案引用旧快照，`competitor_snapshot_stale` 只对同产品同市场、hash 不同的更新快照为真；`market="北美"` 映射到 US，无法映射时不选快照并提示。
9. 起草时某角度引用的簇合并后有 3 家非自家广告主 → `competitor_crowded` advisory；某角度未引用任何簇 → `competitor_gap` advisory；planner 输出的 headline 与某条竞品标题整句相同（3 个词）或 body_copy 与某条竞品正文有 ≥8 个连续相同的英文词 → `competitor_copy_reuse` 阻塞确认，UI 指出来源观察；与自家（`role=self`）在投标题整句相同不阻塞。
10. 缺 `market_data` 凭据或缺 AI 凭据任一项 → 只能选受控 Mock，整次扫描（抓取与判断）都是 Mock 并在页面、扫描记录与快照上标明模拟；真实扫描不会在失败后自动切换到 Mock。
11. 受控 Mock 夹具来自 P0 响应（保留公开品牌主页名、文案与落地页域名；个人账号化名；签名素材链接、头像与追踪参数去除），覆盖 DCO 多卡片、视频、无正文、`collation_count > 1`、空结果、402 与分页。
12. 权限与隔离：Member 可以发起解析、扫描、翻转与标签确认；只有 Owner 能管理凭据与归档竞品；跨 Workspace 读取竞品、扫描、观察、判断、快照、推荐任一资源返回 404。
13. `partial` 扫描续跑（含“某一家竞品失败、其余完成”的情形）：剩余页数与剩余 LLM 步骤单独报价与预留；续跑拉到的广告若已因关键词落库，只追加 `page:` 来源行，投影里该观察归入该竞品；换 Key 后续跑，新尝试记录新的凭据版本，扫描行保留首个版本；续跑后扫描 ID 不变，来源与观察不重复；同一 `Idempotency-Key` 重放创建请求返回同一扫描。
14. 自家主页以 `role=self` 加入并扫描 → 其观察在看板单独成组，不计入 `competitor_crowded` 的广告主数。
15. 扫描时的 AI 输出语言：zh-CN 用户发起的扫描，相关性理由、聚类主张与汇总为中文；en 用户为英文；planner 在 `strategy_language=en-US` 的方案里引用中文快照时，输出仍为英文。
16. 同一 Workspace 同时发起两次扫描（或扫描中发起解析）→ 第二个排队，证据记录等待；两者完成后各自的对账与费用独立。
17. 扫描内的聚类步骤失败 → 付费重新聚类成功 → 进度里的聚类步骤显示“已由重新聚类完成（第 N 版聚类）”，接口里的步骤状态与原因不变；不再提供只为聚类的续跑（续跑报价与续跑返回 409 `competitor_scan_not_resumable`）；还有页、判断或打标没做完时照常续跑，但不再预留或调用 `cluster:1`，生效的仍是重新聚类那一版，续跑新判断出的相关广告进入“未归类”；只有失败的重新聚类时，聚类步骤仍算未完成（#353）。

## 14. 不做与后续

- 不做：Google / TikTok / LinkedIn 广告库、视频文件存档、口播转录、历史（非在投）广告、定期监控、Adyntel 备选适配器、Meta 官方 Ad Library API（北美商业广告不可见）、竞品素材导出、Workspace 物理清理（随平台统一流程）。
- 后续候选：转录（P0 两条样本均不可用，需更多样本）；按日重扫得到“新上线 / 下线”差分；品类知识库沉淀。
