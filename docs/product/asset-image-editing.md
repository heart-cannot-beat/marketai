# 素材图片编辑：第一批

2026-10-20：本批接通原图输入、父子版本、本地裁剪旋转，以及 Gemini、OpenAI、Seedream 的图生图修改。发布状态和线上验收范围见 [发布 PR #208](https://github.com/heart-cannot-beat/marketai-archive/pull/208)。

## 用户流程

在营销测试页检查阶段，每张素材提供两个入口：

- **Crop and rotate**：显示原图和裁剪区域，按原图百分比输入左/上位置与宽高，再选择顺时针 0/90/180/270 度旋转。先裁剪后旋转。提交后由后台完成，不调用图片供应商、不预留供应商费用。
- **Modify with AI**：输入希望修改的内容，选择本轮可用供应商。准备草稿后继续使用现有生成预览、最高费用确认和后台任务。已有审核意见仍可作为修改依据；没有审核意见时需要用户提供修改描述。

新结果保留原 Asset 身份，创建准确的子 AssetVersion；原图、原审核和已导出清单均不覆盖。子版本出现在同一小轮，重新进行 AI 检查/人工审核，审核通过后才可显式选入投放包。只做裁剪也不继承原批准或投放证据。

输入和输出字节相同，或结果已存在于同一个 Asset 下时，记录复用/未变化，不制造新的执行证据。放大、去背景和矩形选区擦除进入[第二批开发](supplier-image-tools.md)，尚未发布；自由笔刷和 OCR 精确排字属于后续范围。当前 AI 修改可能改变其他细节。

## 原图与生成链路

`selection.source_image` 冻结准确 AssetVersion、SHA-256、存储版本和输入角色，并进入预览/配方 hash。预览、提交及 Worker 重新验证租户、产品、模式、方案和来源；Worker 从 BlobStore 读取并校验源字节。源文件缺失或不符时不退回文生图。

裁剪后的自定义比例会选择最接近的供应商支持画幅作为新草稿默认值；预览中仍可调整输出画幅，发送的原图像素不变。

三家 Adapter 的统一 `execute()` 消费 `reference_inputs`。Gemini 使用 inline image，OpenAI 有源图时使用 image edits，Seedream 使用 image data URL。原图保留原 MIME、像素和 alpha，不经过旧参考图片转 RGB JPEG 的路径。允许 PNG/JPEG/WebP 单帧，单图最多 20MiB、25,000,000 像素。

图生图继续沿用 Workspace 凭据、权益、报价、预留、dispatch fence 和 uncertain 防重。报价配置必须包含源图输入及输出的费用上界；不自动回落到平台 Key 或 Mock。

## 本地编辑与持久化

`marketing_image_edits` 冻结源图、原小轮意图、裁剪、旋转和算法版本 `crop-rotate-v1`。请求有 If-Match 和幂等键，后台使用普通 Job/Operation；相同意图复用同一任务。服务器处理真实像素，保留透明度，保存到 BlobStore。

本地子版本使用 `origin_kind=edit`，没有 ProviderAttempt 或虚构的供应商成本；execution_mode 继承来源以保持真实/模拟证据隔离。父子关系通过 `parent_version_id` 保存，操作配方保存在编辑记录，营销查询通过该记录显式关联回原组合。导出 Manifest 使用 `image_edit` 来源，保存父版本、原图 hash、裁剪/旋转及算法版本。

GPU/网络请求不属于本地处理任务。Worker 重启可以安全重放确定性裁剪/旋转，最终持久化受编辑记录及 Asset 锁保护。失败保留原版本；已冻结或已变更的方案不接收迟到的编辑版本。

## 发布与验证

需要发布迁移 `zv7c8d9e0f1a`（接在生产 `zu6b7c8d9e0f` 之后） 和更新的领域角色脚本，再先发布领域 API/Worker，最后发布对应 BFF/UI。新增查询参数 `include_image_edits=true` 才返回编辑进度和父版本；默认响应保持旧字段。旧投放记录视图将本地编辑如实展示为 `origin_kind=edit` 的既有素材，原始 Manifest 和新版投影保留完整操作来源。正式数据库遵循既有 `db-release` 流程，不单独绕过角色收敛。新增表具有 Workspace 组合外键和强制 RLS，Worker 只能修改结果状态字段，不能改写冻结输入。

验证入口：

- `tests/test_marketing_image_editing.py`：真实 HTTP、实际像素、本地持久化、父子版本、幂等、不继承审核、导出来源及原图传递。
- `tests/test_provider_image_inputs.py`：三家供应商真实请求编码，替换 HTTP transport，验证原始 PNG/alpha 未丢失；非法图片不会降级到文生图。
- `tests/test_generation_selection.py`、`tests/test_marketing_followup.py`：既有费用、uncertain、刷新恢复与审核回归。
- `web/__tests__/components/workspace/marketing-image-edit.spec.tsx`：原图预览、裁剪/旋转提交与非法区域阻塞。

PostgreSQL 已验证当前生产版本升级、回退、再次升级、无结构漂移以及真实运行角色和租户隔离。HTTP transport 测试不代表供应商付费效果验收。部署与线上结果保存在独立发布记录中。
