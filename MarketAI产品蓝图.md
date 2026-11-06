# MarketAI 产品蓝图：从"跑组合"到创意实验平台

*基于 2026-08-20 的调研 · 对应 user story：接图片生成 API key → 定产品 → 想角度（有没有人 / 生活化 vs 工业化 vs 产品化 / 出不出现汽车）→ 排列组合 → Meta 跑测试 → 找到最优组合 → 放量投放*

---

## 0. 一句话定位

**给电商 / 出海卖家的 AI 创意实验平台：把"哪种广告图能卖货"从靠猜，变成可积累、可复利的实验数据。**

不是"AI 生成广告图的工具"（这个已经商品化了），而是"假设 → 生成 → 实验 → 学习 → 放量"的完整闭环。生成只是闭环里成本最低的一环。

---

## 1. 核心洞察：学习的单位不是"图"，是"维度"

你的 user story 本质上是把**实验设计（Design of Experiments）**用在广告创意上——这个方向是对的，而且目前没有开源项目、也几乎没有商业产品把这个闭环完整做掉。但要从流程变成产品，需要一次关键升维：

> **系统学到的东西，不应该是"第 7 张图赢了"，而应该是"'有人 + 生活化场景 + 出现汽车'这个组合赢了，其中'有人'贡献了 +37% CTR"。**

为什么这一步决定生死：

单张赢的图，7–14 天就会创意疲劳，生命周期结束，价值归零。但**维度级的结论可以跨图复用**——知道"充电机广告里出现汽车比不出现好"，下一轮的每一张新图都受益；跨轮次积累，就形成这个产品的**创意基因组（Creative Genome）**：每张图携带结构化属性标签，投放数据回流后按属性归因，沉淀为品类知识库，反过来指导下一轮生成什么。

这就是数据飞轮：用得越久 → 品类知识越厚 → 首轮命中率越高 → 客户测试预算浪费越少。同品类跨客户脱敏聚合后（"车充品类里家庭场景普遍跑赢工业场景"），新客户第一天就能站在存量学习之上。**这是纯生成工具（AdCreative.ai 之类）没有的护城河。**

---

## 2. 产品闭环：六个模块

```
┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
│ ① 产品接入 │ → │ ② 角度引擎 │ → │ ③ 实验规划 │ → │ ④ 创意工厂 │ → │ ⑤ 投放执行 │ → │ ⑥ 学习放量 │
│  Intake   │   │  Angles   │   │  Planner  │   │  Factory  │   │  Launch   │   │  Learn    │
└──────────┘   └──────────┘   └──────────┘   └──────────┘   └──────────┘   └────┬─────┘
                    ↑                                                            │
                    └────────────── 品类知识库（创意基因组）←──────────────────────┘
```

### ① 产品接入（Intake）

用户给一个商品 URL（亚马逊 / Shopify / 独立站）或上传产品图。系统抓取：产品图（抠出主体，供后续"场景置换"用）、卖点、价格、评论关键词、品牌资产（logo、色板）。didoo-marketing-suite 的"从 URL 一键建 campaign"是这个体验的参考。

### ② 角度引擎（Angle Engine）

LLM 基于三个输入生成**结构化维度 schema**，而不是直接生成 prompt：产品理解、Meta Ad Library 里同品类竞品在投广告、品类知识库里的历史结论。输出示例（充电机）：

```json
{
  "人物":   ["无人", "男性车主", "一家人"],
  "场景":   ["家庭车库", "户外露营", "工业车间", "纯产品棚拍"],
  "汽车":   ["出现", "不出现"],
  "文案钩子": ["充电速度", "安全防护", "便携性"],
  "视觉风格": ["实拍感", "3D渲染感"]
}
```

注意维度不只是画面——**文案 hook、痛点角度、格式（图 / 轮播 / 视频）同样是维度**，而且往往比视觉维度对转化的影响更大。用户可以在 UI 里增删改维度和取值（这是人机协作的关键触点：用户贡献行业直觉，系统负责穷举和验证）。

### ③ 实验规划器（Planner）——防组合爆炸，这是技术含量最高的一环

5 个维度各 3 个值 = 243 个全组合，预算跑不起，也没必要。三层手段：

**a. 部分因子 / 正交设计**：用 9–16 个精心挑选的组合覆盖所有主效应（每个维度取值都出现且均衡），第一轮就能回答"哪个维度重要、哪个取值好"，不需要跑全排列。

**b. 序贯 bandit**：第二轮起用 Thompson Sampling / 贝叶斯优化，把预算逐步移向表现好的区域，同时保留探索。Meta 自己的开源库 [Ax](https://github.com/facebook/Ax)（MIT，2026 年 3 月还在发版）就是干这个的，可以直接用。

**c. 顺应 Meta 的现实（2026 年最重要的坑）**：Andromeda 算法给"语义相似"的创意分配同一个 Entity ID——同一概念的 8 个微调变体会互相蚕食流量，测不出差异。所以正确结构是：**组合间的"概念级差异"（比如"家庭车库有人" vs "工业车间无人"）作为独立创意 / 独立 ad set 去测；维度内的微调（换标题、换 CTA）塞进 Flexible Ads / 动态创意（asset_feed_spec），让 Meta 自己配**。行业共识是每轮 8–15 个真正不同的概念、至少跑 7 天再判胜负。

预算模型要在产品里显式给出：`每概念日预算 × 概念数 × 7 天 = 本轮测试成本`，让用户在点"开跑"之前就知道要花多少钱。

### ④ 创意工厂（Factory）

"接不同图片生成 API key"的正确产品形态是**多模型路由器**：按任务类型选模型（文字排版强的、真实感强的、成本低的），单一模型挂了自动切换，用户也可以自带 key 降低你的成本。2026 年可路由的：Nano Banana（Gemini）、GPT-Image、Flux 2、Seedream/即梦、自托管 ComfyUI。

**真正难的不是生成，是产品保真（Product Fidelity）**：充电机的接口形状、按钮位置、logo 不能画错，画错的图投出去是事故。解法不是"从零文生图"，而是**以实拍产品图为锚点做场景置换**——抠出产品主体，用 inpaint/outpaint/重打光把它放进目标场景。ComfyUI 的电商产品图工作流在这条路线上已经很成熟，可以自托管作为保真路线，闭源 API 作为创意路线，两条腿走路。

生成后必须过**自动 QC（VLM 审核）**：产品一致性（对照原图）、文字拼写、Meta 广告政策预检（曝光皮肤、before/after、夸大承诺等）、品牌合规。不过审自动重生成，全过审才进入人工确认队列。

每张产出物从出生起就携带属性标签，并编码进**素材命名规范**（如 `EVC01_人物-家庭_场景-车库_车-有_钩子-速度_v2`）——Motion / Atria 这些创意分析工具本质上就是靠命名规范做归因的，你在生成端就能把这件事做对。

### ⑤ 投放执行（Launch）

通过 Meta Marketing API 自动建测试 campaign：每概念一个 ad set（或概念内用 Flexible Ads），上传素材、写入命名规范、设预算和自动暂停规则。**必须有人工审批门**——花真钱之前给用户一屏确认（素材 + 结构 + 预算），这既是产品信任问题也是法务问题。

现成的轮子：[meta-ads-mcp](https://github.com/pipeboard-co/meta-ads-mcp)（1.1k★，Python，Meta Business Partner 背书）几乎覆盖全部所需 API 操作——建 campaign/ad set/ad、传图、动态创意、拉 insights。

**权限现实（MVP 别被这个卡死）**：Marketing API 的 standard access 需要 app review + 商务验证，周期不可控。v0 阶段可以先做"半自动"：系统产出素材包 + campaign 结构 + 命名规范，用户在 Ads Manager 批量上传；或先用用户自己 ad account 的 token（development access 就够管自己的账户）。

### ⑥ 学习与放量（Learn & Scale）

每日拉 insights → **属性级归因**：v0 先做简单的"按属性分组平均 + 置信区间"，v1 再上层级贝叶斯模型（同时估计各维度主效应和交互效应，小样本下更稳）。结论写回品类知识库。

赢家组合推进到 Advantage+ 放量 campaign；同时围绕赢家组合生成新变体对抗创意疲劳（保持维度取值不变，换构图 / 换模特 / 换文案措辞）——**"放量"不是一次动作，是持续的 exploit + refresh 循环**。

用户真正愿意付费的输出是这样的报告：

> 本轮 12 个组合、$840、7 天。结论：**"有人"显著优于"无人"**（CTR +37%，95% 置信区间 [21%, 55%]）；场景维度中"家庭车库"最优但与"户外露营"差异不显著。建议：下轮固定"有人"，在"家庭 vs 露营"之间加测，并引入新维度"人物视角（第一人称使用 vs 第三人称展示）"。

---

## 3. MVP 怎么切

**v0（4–6 周，内部工具 + 2–3 个 design partner）**：只做 1–2 个品类（就从充电机 / 车品出海卖家开始）。产品接入手动填；角度引擎用 LLM 生成、人工修改；生成接 1–2 个模型 + 人工挑图；投放半自动（产出素材包和 campaign 结构，手动上传）；分析用脚本每天拉 insights、每周出一页报告。**v0 要验证的不是技术，是一个业务假设：属性级结论是否真的稳定、可复现、值得客户付费。** 如果同一维度结论在两轮实验里翻烧饼，整个产品逻辑要重新想（可能需要更大预算样本，或该品类维度效应本身就弱）。

**v1（self-serve SaaS）**：API 全自动闭环 + 审批流 + 多模型路由 + 自动 QC + 计费。申请 Meta app review 拿 standard access。

**v2（飞轮）**：品类知识库跨客户脱敏聚合；扩视频（AI UGC 数字人口播，Arcads 方向——视频在 Meta 上的量远大于图片）；扩 TikTok / Google（pipeboard 同一家已有 5 平台 MCP，路径是通的）。

---

## 4. 开源项目参考清单（2026-08 核实）

没有任何一个开源项目做完整闭环——**各环节的轮子都有，闭环本身就是产品机会**。按模块对号入座：

| 项目 | 对应模块 | 说明 | License / 活跃度 |
|---|---|---|---|
| [pipeboard-co/meta-ads-mcp](https://github.com/pipeboard-co/meta-ads-mcp) | ⑤ 投放 | Meta Marketing API 的 MCP 封装：建 campaign / ad set / ad、传素材、动态创意、拉 insights，Meta Business Partner 背书 | BSL 1.1（可自用，2029 转 Apache2）· 1.1k★ 活跃 |
| [TheMattBerman/meta-ads-kit](https://github.com/TheMattBerman/meta-ads-kit) | ⑤⑥ 闭环样板 | 开源 AI ads agent：监控 → 疲劳检测 → 预算优化 → 生成文案 → API 上传，闭环思路和你的最像 | MIT |
| [facebook/Ax](https://github.com/facebook/Ax) | ③ 实验规划 | Meta 官方自适应实验平台：贝叶斯优化 + bandit，直接用于组合选择和预算分配 | MIT · 2.8k★ 持续发版 |
| [ComfyUI 电商工作流](https://comfyui.org/en/use-cases/ecommerce) | ④ 生成 | 产品图场景置换 / 换背景 / 重打光的成熟工作流生态，自托管保真路线 | GPL（ComfyUI 本体） |
| [Anil-matcha/Open-AI-Design-Agent](https://github.com/Anil-matcha/Open-AI-Design-Agent) | ④ 生成编排 | brief → 规划 → 多模型路由（200+ 模型）→ 生成的 agent 架构参考，含品牌一致性 | MIT · 早期 |
| [elias-didoo/didoo-marketing-suite](https://github.com/elias-didoo/didoo-marketing-suite) | 全局 | "AI media buyer" 的技能划分（Plan/Execute/Analysis/Recommend/Report）值得抄结构 | 早期 · 9★ |
| [krusemediallc/uni1-image-ad](https://github.com/krusemediallc/uni1-image-ad) | ②④ | 把任意在投广告逆向成可复用模板再套自己品牌——"竞品角度挖掘"的实现参考 | 早期 |
| [facebook-python-business-sdk](https://github.com/facebook/facebook-python-business-sdk) | ⑤ | Meta 官方 Python SDK，不想走 MCP 就直接用它 | 官方维护 |
| [facebookexperimental/Robyn](https://github.com/facebookexperimental/Robyn) | ⑥（远期） | Meta 官方开源 MMM，客户做大后的跨渠道预算归因 | 官方 · 成熟 |
| [LichAmnesia/awesome-ad-video-prompts](https://github.com/LichAmnesia/awesome-ad-video-prompts) | ④（v2 视频） | Seedance / Veo3 / Kling 的广告视频 prompt 库 | 159★ |
| [hyperfx-ai/marketing-skills](https://github.com/hyperfx-ai/marketing-skills) | 全局 | 给 AI agent 用的 marketing 技能集（paid ads / 竞品研究 / 创意生成） | — |

---

## 5. 竞品格局：闭环是空位

| 玩家 | 做什么 | 缺什么 |
|---|---|---|
| AdCreative.ai / 各类 AI 生图广告工具 | 批量生成创意 | 不投放、不学习，产出即终点；已严重同质化 |
| [Icon.com](https://icon.com/) | 曾标榜"AI Admaker"，**2026 年已转向真人 UGC**（$1000/6 条真人视频，主打 "no AI / 100% real"） | ——它的转型本身就是最重要的市场信号，见下 |
| Motion / Atria / Foreplay | 创意分析、竞品广告库、命名规范归因 | 只分析不生成不投放，闭环的"后半段" |
| Madgicx / Smartly | 投放自动化、预算优化 | 创意生成弱，"前半段"缺失 |
| Arcads / Creatify | AI UGC 数字人视频 | 单点生成，无实验体系 |

**Icon 转型的启示**：估值最高的"AI 广告"公司发现纯 AI 生成内容会商品化（模型能力人人可接），于是把价值锚移到了工作流和供给侧。对你的含义：**不要把产品价值押在"会生成图"上，押在"实验体系 + 维度级学习数据"上**——模型是可替换的插件（这也正好呼应你 user story 里"接不同 API key"的设定）。

你的空位：**"假设 → 生成 → 实验 → 维度级学习 → 放量"的全闭环，且学习结果跨轮次、跨客户复利。** 目前没有人同时占住这五段。

---

## 6. 商业模式与主要风险

**定价**：订阅（按品类 / 席位）+ 生成量阶梯 + 管理广告花费的百分比（行业惯例 3–7%）。测试预算由客户的 ad account 直接出，你不碰广告费。

**风险清单**（按优先级）：

1. **统计噪音出伪结论**——小预算下 CTR 差异很容易是噪音。产品必须诚实展示置信度，宁可说"本轮无显著结论、建议加预算"，也不能输出伪结论砸口碑。这是这个产品的生命线。
2. **产品保真翻车**——画错产品接口/logo 的图被投出去。QC 环节 + 人工审批门是底线，保真路线优先用实拍图场景置换。
3. **Meta API 审核周期**——standard access 不可控，v0 用半自动 + 用户自有 token 绕开。
4. **平台自动化的挤压**——Advantage+ / Andromeda 越来越黑盒，Meta 自己也在做"自动生成 + 自动配组合"。应对：你的价值在**平台之上的一层**（跨轮次的维度知识、跨平台复用、品类洞察），且测试结构要顺着平台自动化设计（概念间独立测、概念内交给 Flexible Ads），不要对抗。
5. **单一平台依赖**——v2 尽早扩 TikTok / Google，维度知识天然跨平台。

---

## 附：本次调研主要来源

- Meta 2026 创意测试方法论（Andromeda / Entity ID / 8–15 概念 / 7 天）：[Affect Group](https://affectgroup.com/blog/how-to-test-creatives-in-meta-ads-in-2026-a-working-system-in-the-era-of-advantage-and-andromeda/)、[RocketShip HQ](https://www.rocketshiphq.com/meta-creative-testing-campaign-structure/)、[Extuitive](https://extuitive.com/articles/meta-ads-creative-testing-practices)
- 动态创意 API：[Meta for Developers – Dynamic Creative](https://developers.facebook.com/docs/marketing-api/ad-creative/asset-feed-spec/dynamic-creative/)、[Flexible Ads（Metricool）](https://metricool.com/flexible-ads-meta/)
- 竞品：[Icon.com](https://icon.com/)、[2026 AI 广告工具对比（rework）](https://resources.rework.com/tools/ai-tools/best-ai-ad-creative-tools-2026)、[Atria 创意分析工具综述](https://www.tryatria.com/blog/best-ai-ad-tools-for-creative-analysis)
- 开源项目：见第 4 节表格内各 GitHub 链接


