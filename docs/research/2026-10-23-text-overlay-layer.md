# 标题与参数作为可编辑排版层

日期：2026-10-23。任务：[#231 / J-33](https://github.com/heart-cannot-beat/marketai-archive/issues/231)。状态：研究与本地样例；没有接入生成、QC 或导出主流程。

建议先做 A 的窄切片：**无广告文字的底图 + 已确认英文文案 + 固定排版模板**，合成为新的素材版本，再审核和导出。编辑性来自保留底图、文字与布局的版本；给 Meta 的仍是扁平图片。B 保留为现有兼容路径，C 暂不作为默认。这个建议基于可控性与本次样例，不是投放效果结论。

## 1. 三种方案

| 方案 | 文字与画面 | 修改/本地化 | QC 与追溯 | 取舍 |
| --- | --- | --- | --- | --- |
| A 无字底图 + 叠字 | 模型只做画面，标题/参数由确定性渲染器排版；产品原有标记不应被抹掉 | 在适用的事实、Offer、市场与布局下可以只改文字层；底图有其他市场敏感内容仍需改图 | 底图外观 + 文案事实校验 + 最终合成图复检；冻结全部输入与最终哈希 | 最容易精确改字；必须处理字体覆盖、换行、遮挡和长文溢出 |
| B 生图带字 + QC | 当前模型一次生成画面与文字 | 改字通常涉及再生成/图片编辑，旧版本保留 | 精确生成配方与最终图已有路径；OCR/视觉核对仍需识别错字、漏字与语言漂移 | 保留当前能力；无法保证模型按原文逐字渲染 |
| C 极短 hook 入图 + 其余叠加 | 模型文字与确定性文字并存 | 仅叠加部分可直接编辑；hook 修改仍需改图 | 两套文字来源都必须追踪，并检查重复、冲突、层级与一致性 | 灵活但更难向用户解释，也不能解决 hook 本身的乱码/错字 |

这是架构判断。当前生成配方冻结在 [creative_recipe.py](../../app/generation/creative_recipe.py)，人工检查包含 `copy_consistent`，导出只读取已批准具体素材及哈希，不能把一张新的合成图偷偷替换旧文件。[workflow.py](../../app/marketing/workflow.py)、[handoff_worker.py](../../app/marketing/handoff_worker.py)

## 2. 排版技术与尺寸边界

Pillow 可测量文字包围盒并绘制独立 RGBA 层；其 `direction`、OpenType `features` 与 `language` 参数需要 libraqm 支持。包围盒受字体、锚点和渲染参数影响，不能用字符数或各字宽度相加代替。此处按样例使用的 12.3.0 文档源码核对；浮动 `stable` 文档目前是 13.0.0。[Pillow 12.3.0 ImageDraw 文档源码](https://github.com/python-pillow/Pillow/blob/12.3.0/docs/reference/ImageDraw.rst)

因而第一片只承诺经验证的英文字体/文案范围；本次 ASCII 样例没有验证中文、阿拉伯文或 emoji。生产布局策略还须明确字形覆盖、方向、标点/组合字符、断行、最大行数、行距、字号下限与超限处理，并使用同一字体和 shaping 参数测量、绘制。字形缺失、换行或布局无法容纳时阻断并要求编辑，不能自动省略事实/限定，或靠持续缩字通过。这是从排版能力推导的实现要求，并非本样例已实现的多语言能力。

另一种 A 实现是 HTML/SVG 模板 + Playwright 栅格化：便于复用界面预览，但需固定浏览器、字体文件、视口、缩放及渲染策略，禁止模板读取任意远程 URL。官方截图默认 `scale: "device"`、`animations: "allow"`；可显式用 `scale: "css"` 固定每 CSS 像素一个输出像素，配合明确画布尺寸。`animations: "disabled"` 会把有限动画推进到结束、无限动画回到初态，因此生产模板宜直接禁止动态内容，不能把截图选项当作任意脚本/时间变化都已被冻结的保证。[Playwright screenshot](https://playwright.dev/docs/api/class-page#page-screenshot)

浏览器渲染需显式加载所用字体、检查加载失败，再等待布局完成。W3C 工作草案明确 `document.fonts.ready` 只会 fulfilled，不会 rejected；`document.fonts.check()` 在字体不存在或范围不覆盖而使用回退时也可能返回 true。`document.fonts.load(font, text)` 要检查返回的实际匹配字体，不能把空列表当成功，且加载成功仍不证明全部字形覆盖。建议由冻结的本地字体字节创建准确 FontFace，验证加载状态、字形与最终布局；预览和正式渲染必须使用相同版本与输入。[W3C CSS Font Loading §3.2–3.4（工作草案）](https://www.w3.org/TR/css-font-loading-3/#font-face-set-ready)

本样例输出 **1080×1080（1:1）和 1080×1350（4:5）**，属于研究画布选择。它不声称覆盖 Meta 全部版位或当前推荐最小分辨率；本次 [Meta Feed 图片指南](https://www.facebook.com/business/ads-guide/update/image/facebook-feed) 读取被登录/访问限制阻断，尺寸要求未独立核实。生产片必须按实际版位核对最新官方规格，并冻结 placement policy 版本。不能将 4:5 自动当作 Stories/Reels 的通用布局。

## 3. QC 不会因叠字而消失

| 检查 | 主要输入/执行层 | 最终图仍需检查什么 |
| --- | --- | --- |
| 文件/画幅 | 确定性检查最终输出格式、像素、哈希 | 文件可读，目标画幅正确 |
| 语言/原文 | 对文字层逐字比对已确认文案、语言和允许字符；字形与溢出校验 | 底图没有多余/冲突文字，最终画面无漏字、乱码或遮挡 |
| 参数、事实与限定 | 文字层引用确认事实/Offer/TruthPackage 的准确版本；不翻译后猜数值 | 限定、单位、货币与实际画面一致且可读 |
| 产品外观 | 底图与同一批冻结实物参考 | 排版/Logo 没有遮住接口或改写品牌标记；参考不证明隐藏性能 |
| 品牌/可读性 | 冻结品牌、字体与布局规则，测边界/安全区/对比度 | 真实背景上的视觉对比、裁切和字体层级；包围盒通过不等于视觉可读 |
| 表达/对照 | J-31 的 single/paired 对最终素材版本执行 | 主张是否实现，该变/保持的是否做到；不同文字或模板可能本身就是测试变量 |

这是建议分工，不在本票改变检查 policy。文字层改动、换语言、改字体、改布局或改合成尺寸均创建新素材版本，重新审核；底图通过不能直接给合成图继承 Approved。历史检查与判断保留，已有投放绑定继续指向旧版本。[人工审核与 ready 门禁](../../app/marketing/workflow.py)

## 4. Manifest、版本与本地化

生产候选配方至少冻结：底图 AssetVersion/hash、已确认文案版本和字段映射、排版层版本/hash、字体文件/许可证据引用、brand_kit 精确版本、语言、画幅与 placement policy、渲染器及底层库版本、输出哈希。还应冻结模板与渲染代码/构建产物身份、布局/裁切/颜色/编码策略，以及适用的 shaping 引擎与参数；只记一个可改名的 renderer policy 字符串不足以重放。已核对认证原件由 J-22 的准确参考版本进入受控图片层，同时引用已确认认证事实；不能让上传认证图自动变成认证证明。这些是生产候选字段，不是本地研究 JSON 已满足的生产合同。

当前导出任务按 `entry.content_sha256` 读文件、核哈希并生成 `manifest.json`，并复查当前审核资格。因此正式实现宜**在导出前**以持久化、可恢复、幂等的 render job 创建合成 AssetVersion，QC/人工审核后由现有导出消费；不在 ZIP 打包时追加一层未审核文字。渲染重放相同指纹复用产物；新文字/布局代表新意图，不覆盖旧 AssetVersion。复用仍受 Workspace、来源权限、当前审核与事实有效性约束；字节相同不能合并不同市场/Offer 的业务身份。Manifest 保存准确版本和哈希并指向可保留的输入证据，哈希本身不保存输入，也不构成批准。上述复用规则是对现有门禁和本地化合同的设计推论，本票不更改实现。[handoff_worker.py](../../app/marketing/handoff_worker.py)、[workflow.create_handoff](../../app/marketing/workflow.py)、[本地化合同 §4、§11、§15](../product/localization-truth-package-contract.md)

本地化只换文字层的前提是底图本身适用于目标市场、事实/价格/单位/限定已经由目标真值包确认，并重新完成布局与最终图 QC。品牌标记、包装文字和 C 的模型 hook 也属于源图可见声明，不能因它们不是文字层而跳过 Inventory。原文正确不等于翻译正确，源市场质量通过也不继承到目标版本；锁定事实继续由确定性规则注入，不交给翻译模型改写。[本地化真值合同 §2、§9、§12、§15](../product/localization-truth-package-contract.md)

## 5. 字体许可与来源

样例使用 Pillow 12.3.0 内置的有限字符集 Aileron Regular，没有复制操作系统字体文件。该版本 `load_default(size=...)` 源码内嵌字体字节，并明确说明来源与有限字符集；样例环境有 FreeType，研究 Manifest 记录实际字体名称和字节哈希。作者当前页面说明 Aileron 为 No Rights Reserved，并允许使用、修改及再分发；此处不采信第三方把某个 Aileron 文件标为 OFL 的说法，也不把作者整套下载与 Pillow 内置子集视为相同字节。本票仅将其用作样例字体，并不假装这是任何客户已批准的品牌字体。[Pillow 12.3.0 load_default 源码](https://github.com/python-pillow/Pillow/blob/12.3.0/src/PIL/ImageFont.py#L991-L1006)、[字体作者页面](https://dotcolon.net/fonts/aileron/)

未来 brand_kit 字体需记录精确文件、来源、适用授权/有效状态及服务器渲染或分发方式。不能因为系统已安装或第三方镜像标了开源，就推断某个字体文件的全部使用范围。OFL 官方 FAQ §1.1–1.1.2 区分用字体制作图形成品与再分发、捆绑、修改字体软件；后者需遵守相应条件。输出扁平 PNG 与向客户端发送字体文件是不同使用方式，必须分别核对实际字体授权；非 OFL 字体不能套用其结论。[OFL 官方 FAQ](https://openfontlicense.org/ofl-faq/)

## 6. 本地样例与复现

本票当时用一张真实客户的产品参考照做了本地样例，输出 1:1 与 4:5 两个画幅。参考照属于客户素材，样例 spec、参考照、渲染图、研究 manifest 和检查记录都已移出本仓库，只保留在私有归档仓库。参考照保留产品固有标记，不裁掉接口，也没有调用生图服务；它是参考照而非生成底图，故样例只证明叠字和版本追踪可行，不验证无字底图生成质量或投放授权。

样例 spec 是可编辑的标题、图注、参数位置及样例配色/布局输入。配色明确为样例 brand_kit，不是正式品牌配置；使用中性图注，没有填造电流/功率/认证参数。正式参数必须来自已确认事实，本样例不证明数值注入已完成。

复现时自备有使用权的照片（例如虚构产品示意图）和 spec，用已安装 Pillow 12.3.0 的 Python 运行：

```sh
python scripts/research/render_text_overlay.py --spec path/to/spec.json --output-dir path/to/renders
```

不指定 `--output-dir` 时写到 spec 同目录的 `renders/`。spec 字段以脚本 `render()` 读取的为准：`schema_version`、`research_only`、`source`（照片文件与 SHA-256）、`brand_kit`、`copy`、`ad_language` 和 `layouts`。脚本核对原图哈希，单独保存透明文字层、最终 PNG 和每个画幅的研究 manifest；超过布局边界直接报错，不缩成不可读小字或静默裁切。spec 保留编辑性，PNG 本身没有可编辑文字对象。

当时本地已检查：两个准确画幅、包围盒无越界、相同环境/输入重复渲染哈希相同、只改标题时底图哈希不变而成图哈希改变、超长标题被拒绝。字体、Pillow、FreeType、zlib 和 Python 版本写入 manifest；本次未证明跨平台逐字节一致，也未测试 HTML/SVG 渲染与非英文语言。脚本的 ASCII 限制不等于完整的文案/字形策略；包围盒门禁尚未验证文字层彼此碰撞、产品保护区、实际显示尺度下的可读性或市场安全区。研究 Manifest 明示 `not_a_production_manifest`，没有生产 AssetVersion、事实版本、审核、权限或持久化 Job 证据；这些缺口不影响本地叠字验证，但必须在生产切片补齐。[样例渲染器](../../scripts/research/render_text_overlay.py)

当时分支的 `make verify` 与前端 `npm run verify` 均通过，迁移无漂移。这些检查不代替尚未实现的生产排版流程验收。

## 7. 建议后续最小生产切片

依赖 J-23/#228 的版本化品牌底线、J-22/#227 的参考继承，以及 J-02/#220 的语言合同；QC 接 J-31/#229。只先支持英文、两个固定模板与两个经版位确认的画幅，关闭自由 HTML/SVG/远程字体输入。

1. 字段：版本化 overlay document 保存标题/参数来源、布局选择、精确字体/品牌引用、语言与底图身份；不可把自由输入参数当已确认事实。
2. API/Worker：保存与校验草稿、返回渲染预览指纹、幂等提交 render job、查询输出与错误；绑定真实 TenantContext，测试跨租户引用、权限撤销、预览过期与重放。
3. UI：修改标题/已确认参数、切画幅并看到真实预览；明确文字层修改会生成新版本，溢出给出具体字段提示；技术记录折叠。
4. 交付：最终合成版本重新 QC/审核后走现有导出，Manifest 追加渲染来源；旧配方、素材和投放记录保留。

该切片值得做；本次结果不足以承诺省多少生图费、降低 CPA 或替代人工/对照检查。暂不自动创建新 Issue，不在本票接入主流程。
