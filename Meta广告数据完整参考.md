# Meta 广告数据完整参考（不含视频指标）

> 基于 Marketing API **v25.0** · 整理于 2026-08-23 · 适用于 MarketAI 投放实验模块（`app/meta/insights.py`）
> 范围：广告主在 Ads Manager / Insights API 中能看到的全部数据。视频类指标（ThruPlay、播放进度等）按需求从略；创意资产细分中仅保留 `video_asset` 名称备查。

---

## 0. 数据从哪来、怎么查

Ads Manager 报表和 Insights API 是同一套数据。API 端点：

```
GET /v25.0/act_{AD_ACCOUNT_ID}/insights     # 也可挂在 campaign/adset/ad 对象下
```

**关键请求参数**

| 参数 | 说明 |
|---|---|
| `level` | 汇总层级：`account` / `campaign` / `adset` / `ad` |
| `fields` | 要取的指标字段（见下文各节） |
| `breakdowns` | 细分维度（见 §6） |
| `action_breakdowns` | actions 数组的拆分方式，默认 `action_type` |
| `action_attribution_windows` | 归因窗口：`1d_click` / `7d_click` / `1d_view` / `1d_ev`，可一次传多个对比 |
| `time_range` / `time_ranges` / `date_preset` | 日期范围；`date_preset` 默认 `last_30_days` |
| `time_increment` | 按几天聚合：`1`（按天）～`90` / `monthly` / `all_days` |
| `action_report_time` | 转化记账时点：`impression`（默认，记在展示当天）/ `conversion`（记在成交当天）/ `mixed` |
| `filtering` | 过滤器数组，如按 `ad.id IN [...]`、`spend > 0` |
| `sort` / `summary` / `export_format` | 排序 / 汇总行 / 导出 CSV·XLS |

**数据时效与保留**

- 常规指标（花费/展示/点击）延迟约分钟级～数小时；转化类 24–48h 才稳定，且随归因窗口持续回补（见 §5）。
- 保留期：常规数据 **37 个月**；`unique_*` 与 hourly 细分 **13 个月**；`frequency_value` 细分 **6 个月**（2026 新规）。
- Insights 有独立限流（响应头 `x-fb-ads-insights-throttle`）；大批量拉取用异步任务：`POST .../insights` → 得 `report_run_id` → 轮询 → 取结果。v0 规模（几个 ad set）同步 GET 足够。

---

## 1. 花费与投放量

| 指标 | API 字段 | 说明 |
|---|---|---|
| 花费 | `spend` | 账户币种（`account_currency`） |
| 展示 | `impressions` | 计费口径的展示次数 |
| 覆盖人数 | `reach` | **估算值**，跨日期段不可相加 |
| 频次 | `frequency` | = impressions / reach，估算 |
| 千次展示成本 | `cpm` | |
| 千人覆盖成本 | `cpp` | |
| 标识与上下文 | `account_id` `campaign_id` `adset_id` `ad_id` 及对应 `*_name`、`objective`、`optimization_goal`、`buying_type`、`attribution_setting`、`date_start` `date_stop` | 每行数据自带，落库时直接用 |

---

## 2. 点击类（三个口径，别混用）

| 口径 | 计数 | 率 | 成本 | 含义 |
|---|---|---|---|---|
| 全部点击 | `clicks`、`unique_clicks` | `ctr`、`unique_ctr` | `cpc`、`cost_per_unique_click` | 含点头像、点赞、展开"查看更多"等一切点击，**噪音大** |
| 链接点击 | `inline_link_clicks`、`unique_inline_link_clicks` | `inline_link_click_ctr` | `cost_per_inline_link_click` | 点了广告里的链接（含跳 Meta 站内如 Shops） |
| 出站点击 | `outbound_clicks` | `outbound_clicks_ctr` | `cost_per_outbound_click` | 跳出 Meta 站外的点击；这三个是 actions 列表格式 |

- **MarketAI 初筛用 `inline_link_click_ctr`（或出站口径），不要用默认 `ctr`。**
- 漏斗再往下一格：`actions` 里的 `landing_page_view`（落地页真正加载完成）。链接点击 → LPV 的落差能看出落地页加载流失，对 Shopify 站点值得带上。

---

## 3. 互动类

大多不是独立字段，而是以 `actions` 数组里的 `action_type` 出现：

| action_type | 含义 |
|---|---|
| `post_engagement` / `page_engagement` | 帖子/主页互动合计 |
| `post_reaction` | 表情（可用 action_breakdown `action_reaction` 拆 like/love/haha…） |
| `comment` | 评论 |
| `post` | 分享 |
| `onsite_conversion.post_save` | 收藏 |
| `like` | 主页新增赞 |
| `link_click` / `photo_view` | 链接点击 / 图片查看 |

顶层另有 `inline_post_engagement` 及对应成本字段。对拉新电商测图，互动是弱信号，做参考列即可，不进判定。

---

## 4. 转化类（Pixel/CAPI，你的终判层）

**核心四件套**（都按 `action_type` 组织）：

| 字段 | 内容 |
|---|---|
| `actions` | 各事件的**次数**：`[{action_type, value, 1d_click, 7d_click, ...}]` |
| `action_values` | 各事件的**金额**（购买金额在这里） |
| `cost_per_action_type` | 各事件 **CPA** |
| `purchase_roas` | 购买 ROAS（另有 `website_purchase_roas` / `mobile_app_purchase_roas` 分渠道） |

**标准事件的 action_type 值（网站类）**：`purchase`、`add_to_cart`、`initiate_checkout`、`view_content`、`search`、`lead`、`complete_registration`、`add_payment_info`、`add_to_wishlist`、`subscribe`、`start_trial`、`contact`、`schedule`、`submit_application`、`donate`、`find_location`；自定义事件为 `offsite_conversion.custom.{id}`。

**渠道前缀（同一个"购买"有多个视角，取数时固定一个）**：

- `omni_purchase` —— 网站+APP+店铺全渠道汇总（Ads Manager"购买"列即此）
- `offsite_conversion.fb_pixel_purchase` —— 仅网站 Pixel/CAPI
- `app_custom_event.fb_mobile_purchase` —— 仅 APP
- `onsite_web_purchase` 等 —— Meta 站内 Shops

> MarketAI 建议：终判取 `omni_purchase`（Shopify 场景下 ≈ Pixel 购买），解析时精确匹配 action_type，避免 `purchase` 被多个前缀重复累计。

**其他**：`conversions` / `conversion_values` / `cost_per_conversion`（自定义转化+线下转化）；`results` / `cost_per_result`（按 `optimization_goal` 自动对齐的"成效"，带 indicator 标明对应哪个事件——省去自己映射，但落库仍建议存原始 actions）；`unique_actions` / `cost_per_unique_action_type`（去重人数口径）。

**Events Manager 是另一块给用户看的数据**：事件量、事件匹配质量分（EMQ）、Pixel/CAPI 去重率、事件覆盖诊断。接 Shopify 前先在这里确认 purchase 事件回传与 `event_id` 去重正常——这是终判层数据可信的前提。

---

## 5. 归因（读数之前必须懂）

- 当前可用窗口：`1d_click`、`7d_click`、`1d_view`、`1d_ev`（参与观看）。**默认 7 天点击 + 1 天浏览**。`28d_click` 于 2021 年停用；**2026-01-12 起 `7d_view` / `28d_view` 也已移除**。
- `attribution_setting` 字段回显该行数据用的口径；`action_attribution_windows` 传多个值时，actions 每项会带各窗口的分值，可做窗口敏感性对比。
- **回补机制**：默认按展示时间记账（`action_report_time=impression`），点击后 7 天内的成交会回填到当初展示那一天 → 近几天的 CPA/ROAS 必然虚高、历史数据"越看越好"。这正是「初筛期购买列压暗、Day 7 点亮」的依据。想按成交日记账用 `action_report_time=conversion`。
- iOS：SKAN/AEM 聚合口径，延迟高、小量抹零，相关 breakdown 见 §6。
- 增量归因（incremental attribution，2025 起逐步开放）：只计增量转化的更严口径，v0 不用，知道即可。

---

## 6. 细分维度（breakdowns）

| 类别 | 值 | 备注 |
|---|---|---|
| 人口 | `age`、`gender` | 可组合 age+gender |
| 地理 | `country`、`region`、`dma` | dma 仅美国 |
| 版位 | `publisher_platform`（facebook/instagram/audience_network/messenger/threads）、`platform_position`（feed/story/reels/search/right_hand_column…）、`device_platform`（mobile/desktop）、`impression_device` | 版位×平台可组合 |
| 时间 | `hourly_stats_aggregated_by_advertiser_time_zone` / `..._by_audience_time_zone`、`frequency_value`（频次分布） | 见下方 2026-08 新限制 |
| 商品 | `product_id` | 目录广告 |
| 创意资产 | `image_asset`、`title_asset`、`body_asset`、`description_asset`、`call_to_action_asset`、`link_url_asset`、`ad_format_asset`（另有 `video_asset`） | 动态创意 / **flexible ads** 专用 |
| APP/iOS | `app_id`、`skan_campaign_id`、`skan_conversion_id`、`is_conversion_id_modeled` | SKAN 口径 |

**action_breakdowns**（拆 actions 数组本身）：`action_type`（默认）、`action_device`、`action_destination` / `conversion_destination`（转化发生地：网站/APP/Shops）、`action_target_id`、`action_reaction`、`standard_event_content_type`、`signal_source_bucket`（**区分转化来自 Pixel 还是 CAPI，排查回传问题有用**）、轮播卡片 `action_carousel_card_id/name`。

**规则与限制**

- 站外转化（Pixel 类）指标**不能**与 `region`/`dma`/hourly 组合；`action_device`、`product_id` 等组合会排除部分移动端指标。
- hourly 细分不能与 `reach`/`frequency`/`unique_*` 同查。
- 小样本触发隐私阈值 → 行被省略或空值，属正常。
- **2026-08-06 起**：`impression_device`、两种 hourly、`frequency_value` 需在 Ads Manager 做账户级开通，否则返回空。
- breakdown 越多行数越多，注意限流；每加一维，CI 所需样本翻倍——**概念间对比不加任何 breakdown**（1 组合=1 ad set 已天然隔离），`image_asset` 只用于概念内 flexible ads 微调。

---

## 7. 质量与诊断

- **相关性诊断三项**（ad 级、同受众 ≥500 展示才出值）：`quality_ranking`、`engagement_rate_ranking`、`conversion_rate_ranking`；取值 `above_average` / `average` / `below_average_35|20|10`（数字=落后到最差百分位段）。免费的相对竞争力信号，建议进战况表做参考列。
- **学习期**：ad set 对象（非 insights）的 `learning_stage_info`——status（学习中/已完成/学习受限）+ 累计转化数。学习受限 ≈ 周转化 <50，小预算测试大概率常驻此态，读 CPA 时要有心理预期。
- **投放与审核状态**：对象上的 `effective_status`、`issues_info`；被拒时 ad 的 `ad_review_feedback` 给具体拒审原因——**你们「⛔ pill → lightbox 拒审原因」的数据源就是它**。
- **仅 UI、API 拿不全的**：Inspect 面板（拍卖竞争趋势、拍卖重叠、受众饱和/首次展示比率）、Delivery Insights、账户机会分（Opportunity Score）。人工巡检用，不进系统。

---

## 8. 拿不到的与常见坑

- 没有任何用户级数据（谁看了/谁买了）、没有单次拍卖出价、没有竞对数据。
- `reach`/`frequency` 是估算+采样：跨天的 reach 不可相加，两段时间的 frequency 不可平均。
- "天"的边界由**广告账户时区**决定；UI 与 API 偶有四舍五入差异，对账以 API 原始值为准。
- 已删除/归档的广告在时间范围内仍可查到历史数据。
- 部分指标带 estimated / in development 标注（估算或口径调整中）。

---

## 9. MarketAI v0 字段映射

**初筛（每日同步，CTR/CPC + stop-loss）**

```
level=ad
fields=ad_id,ad_name,adset_id,adset_name,spend,impressions,reach,frequency,
       inline_link_clicks,inline_link_click_ctr,cpc,cpm,
       quality_ranking,actions
time_increment=1
time_range={'since':'2026-08-16','until':'2026-08-23'}
filtering=[{'field':'ad.id','operator':'IN','value':[...]}]
```

- CTR 95% CI：分子 `inline_link_clicks`、分母 `impressions`，Wilson 区间；`actions` 里顺手取 `landing_page_view`。
- stop-loss 看 `spend` + `cpc`。

**终判（Day 7 点亮，CPA/ROAS）**

```
fields=...,actions,action_values,cost_per_action_type,purchase_roas
```

- 购买 = `omni_purchase`（精确匹配）；CPA = `cost_per_action_type` 同名项；ROAS = `purchase_roas`。
- 结论要等该批展示的 7 天点击窗口走完再下。

**同步节奏**：每天拉一次，但每次**回看最近 7 天**并按 `(ad_id, date)` upsert——覆盖归因回补导致的历史数据变化；这也让「系统靠同步自动发现 ACTIVE」和数据刷新共用一条轮询。

**响应解析示例**（actions 类字段结构）：

```json
{
  "ad_id": "1203...",
  "spend": "48.72",
  "impressions": "10493",
  "inline_link_clicks": "162",
  "inline_link_click_ctr": "1.543886",
  "actions": [
    {"action_type": "landing_page_view", "value": "141"},
    {"action_type": "omni_purchase", "value": "3"}
  ],
  "action_values": [{"action_type": "omni_purchase", "value": "217.85"}],
  "cost_per_action_type": [{"action_type": "omni_purchase", "value": "16.24"}],
  "purchase_roas": [{"action_type": "omni_purchase", "value": "4.471374"}],
  "date_start": "2026-08-22", "date_stop": "2026-08-22"
}
```

> 注意：数值全部是**字符串**，解析时统一转型；actions 类数组里某事件为 0 时**整项缺席**而非值为 0。

---

## 参考

- [Meta Insights API 官方文档](https://developers.facebook.com/docs/marketing-api/insights/) · [参数](https://developers.facebook.com/docs/marketing-api/insights/parameters/) · [Breakdowns](https://developers.facebook.com/docs/marketing-api/insights/breakdowns/)
- [Kitchn：Meta Marketing API Q2 2026 更新](https://www.kitchn.io/blog/meta-marketing-api-q2-2026-update)（v25.0、归因窗口移除、保留期）
- [Dataslayer：2026 Facebook 指标变更](https://support.dataslayer.ai/understanding-upcoming-removal-of-metrics-on-facebook)（2026-08-06 breakdown 开通要求）
