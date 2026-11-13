# ADR 0016: Clone Product 永不复制 Offer、价格、映射和 Landing Page

**Status:** Proposed (Accepted on merge)  
**Date:** 2026-11-09  
**Decided in:** #62（维护者决定，2026-11-09）  
**Amends:** [创意库合同](../product/creative-library-ownership-and-reuse-contract.md) §5.2 最后一段、§12 的 Clone Product 一条、§19 第 18 条；[v9 功能对齐验收合同](../product/v9-functional-alignment-acceptance.md) §3.6 的 Clone Product 一句  
**Relates to:** ADR 0006（本地化前冻结 Offer 专属的市场真值）、ADR 0007（创意库草稿、发布版本与执行快照分离）

## 背景

创意库合同 §5.2 原来写的是：Clone Product 默认只复制描述、must-match、维度和参考 Binding；Offer、价格、DataSource Mapping 和 Landing Page「必须由用户逐项明确选择，默认不复制」。按这句话，用户可以选择把这四项带进新产品。

服务端 `clone_product` 一直不是这样做的。请求的 `include_optional` 里只要出现 `offers`、`price`、`market_source_mappings` 或 `landing_page_url`（`CLONE_PRODUCT_NEVER_COPIED`），就返回 `library_clone_field_forbidden`。能逐项选择的只有 `brand_sku` 和 `prompt_policy_version_id`（`CLONE_PRODUCT_OPTIONAL_KEYS`）。#25 上线的克隆对话框照服务端行为做，没有这四项的选项。#62 记录了合同和代码的这处冲突。

#54 之后，这四项和新产品之间的关系很清楚：

- 人工事实集（ManualFactSetVersion）的每一行用 `offer_entry_id` 写明属于哪个 Offer 条目。解析 Truth 时只读本地化批次所用 Offer 条目的行；某个市场没有这个 Offer 的行，就返回 422（[本地化真值包合同](../product/localization-truth-package-contract.md)）。
- MarketSourceMappingVersion 用 `offer_version_id` 指向一个具体的 OfferVersion。
- 价格是带有效期的商业事实，只存在于人工事实集和数据源里，既不在 ProductVersion 里，也不在 OfferVersion 里。Landing Page 是 OfferVersion 的字段（`landing_page_url`），ProductVersion 不能保存它。

所以商业真值挂在 Offer 的稳定身份上，不在 Offer 的内容里。

## 决定

Clone Product **永不复制** Offer、价格、DataSource Mapping 和 Landing Page，用户也不能选择复制。

- Clone Product 创建新的 ProductProfile Draft，只复制描述、must-match、维度和参考 Binding 这类生成语义。`brand_sku` 和 `prompt_policy_version_id` 仍可由用户逐项选择。
- 请求里带上 Offer、价格、DataSource Mapping 或 Landing Page，服务端继续用 `library_clone_field_forbidden` 拒绝。错误码和服务端行为不变，界面也不提供这几项的选项。
- 新产品需要的 Offer、Mapping 和 Landing Page，由用户按新产品另行创建：新建 Offer 条目并发布引用新 ProductVersion 的 OfferVersion（Landing Page 写在其中），为它发布 MarketSourceMappingVersion，再在人工事实集里为它补上各市场的行。
- `library_clone_field_forbidden` 的报错文案（en、zh-CN）提示用户这几项要为新产品另行创建。

理由：

- **复制出来的 Offer 不能用，看起来却能用。** 复制出来的 Offer 是新的条目身份。原 Offer 的事实行写的是原条目 id，Mapping 指向的是原 OfferVersion，都不会指向新 Offer。新 Offer 在列表里有名称、有 Landing Page，看起来完整，实际解析不出商业真值：本地化批次用它解析 Truth 时没有可读的事实行，也没有映射到外部商品。用户要等下游失败才会发现。
- **依赖从一开始就要真实。** 由用户为新产品另行创建，Offer、Mapping 和事实行都是用户确认过、经过各自校验和发布权限（合同 §4、§11.4）的对象。DependencyIndex、影响分析和 Withdraw 传播从第一天起就沿着真实的依赖走。

## 被否决的方案

**逐项选择复制**（原合同 §5.2，#62 的选项一）：Clone 请求体允许逐项选择 Offer、价格、Mapping 和 Landing Page，复制时为它们生成新的稳定身份并登记依赖。否决的原因：

- 复制 Offer 时必须决定事实行和 Mapping 跟不跟着走。不跟，得到的就是上面那种解析不出真值的 Offer。跟，就要复制事实行并改写 `offer_entry_id`、复制 Mapping 并改写 `offer_version_id`。这等于一次克隆替用户声明「新产品在这些市场的价格、外部商品和落地页与原产品相同」，并绕过人工事实导入的逐行校验和 Owner 发布边界。
- 让新产品共享原 Offer（不生成新身份）也不行。OfferVersion 显式引用一个 ProductVersion（`product_version_id`），共享会让新产品挂在原产品的销售方案下，混用 Product 和 Offer 的身份（合同 §1 不变量 5）。
- 单独复制价格或 Landing Page 没有对象可放：ProductVersion 不保存这两项，OfferVersion 也不保存价格。
- 审计还要记录每次复制出的身份和原身份的对应关系，以及复制出的事实在源事实过期或撤回后如何处理。这些复杂度换来的只是少几步手工创建。

## 后果

- 服务端和 #25 的界面都不用改，合同与代码一致。合同 §5.2、§12、§19 第 18 条和 v9 功能对齐验收合同 §3.6 按本 ADR 改写。
- 克隆后的新产品在用户创建并发布自己的 Offer 前，不能进入需要 Offer 的下游流程，例如本地化的 Truth 解析。这是有意的：在用户补齐之前，系统不替新产品假设任何商业真值。
- 克隆比整套复制多几步手工操作。用户想以原 Offer 为起点，可以对原 OfferVersion 做 Fork（合同 §12）：得到一个新的 Offer Draft，改成引用新产品后按正常校验和 Owner 权限发布；它的事实行和 Mapping 仍要另行补上。我们接受这些额外步骤，换取商业真值的依赖从一开始就真实可查。
- 审计不变：`cloned` 事件和 `clone` 审计继续记录 `included_optional` 和 `skipped`。
