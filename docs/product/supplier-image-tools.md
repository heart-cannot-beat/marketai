# 已有供应商图片工具：第二批

2026-10-20，开发完成后独立审阅；尚未部署，也未执行真实付费图片请求。沿用 Workspace 已有 Gemini、OpenAI、Seedream 凭据和 Generation 链路，不新增 ComfyUI 服务或 GPU 部署。

## 能力和边界

| 操作 | 当前供应商支持 | 结果规则 |
| --- | --- | --- |
| AI 增强放大 | Gemini、OpenAI、Seedream 的已接入模型 | 供应商按现有 2K 档处理原图，再用 Lanczos 缩放到原图 2 倍或 4 倍尺寸。更大的栅格不证明恢复了真实细节，也不是专用超分模型。 |
| 去背景 | OpenAI GPT Image 1.5、1、1 mini | 原生 `background=transparent` 和 PNG 输出；结果必须有非全透明、非全不透明的 alpha，尺寸恢复到原图。产品边缘和文字质量仍需人工审核。 |
| 局部擦除 | OpenAI GPT Image 1.5、1、1 mini、2 | 当前提供一个矩形选区，按原图百分比定位。原生 alpha 蒙版提供生成指导，服务端再合成结果，保证选区外 RGBA 像素与原图一致；保留原图 alpha。 |

OpenAI GPT Image 2 的透明背景尚属预览，本批不把它显示为正式去背景选项。Gemini 和 Seedream 暂仅提供增强放大，不用文字指令冒充原生透明输出或蒙版支持。自由笔刷、多选区和模型效果验收不在这批中。

原生蒙版不保证模型严格遵守边界，选区外保护由服务端像素合成提供。擦除区内的补景效果、产品外观、文字及营销主张仍需要 AI 检查与人工判断。

官方接口依据：[OpenAI 图片编辑参数](https://developers.openai.com/api/reference/resources/images/methods/edit)、[蒙版与透明输出指南](https://developers.openai.com/api/docs/guides/image-generation)。本次同时修正 input_fidelity 的模型区别：1 和 1.5 使用 high，1 mini 与 2 不发送不支持的 high 参数。

## 用户流程与证据

营销小轮检查阶段新增 “Upscale, remove background or erase” 面板：读取准确私有原图、选择操作及支持的供应商/模型，显示目标尺寸，再准备草稿。这个按钮不会调用供应商；用户进入既有预览页面确认最大费用后才提交生成。

`PrepareFeedback.image_tool` 保存类型化操作、准确源尺寸、倍数或归一化矩形和算法版本。冻结在原有 feedback 记录与 Generation recipe/preview hash 中。操作、选区、来源或供应商改变时需要重新准备草稿。图片工具每次一张、固定供应商 2K 处理档，不能在普通草稿表单里改变原图、供应商/模型或画幅来复用旧操作。

能力选择按 Workspace 凭据、已有权益、预算、配置报价及模型支持过滤。最大费用沿用该模型的现有 2K 上界；运营配置必须覆盖源图、产品参考、蒙版和输出的完整费用，不把旧文生图单价当编辑成本上界。

Worker 在付费派发前重新验证原图 hash、真实尺寸、权限和方案。擦除输入按 EXIF 方向转成 RGBA PNG；同尺寸 alpha 蒙版中透明表示擦除、不透明表示保留，蒙版与产品参考分开传递。其他操作保留原图输入字节。响应记录保留规范化输入及蒙版的 hash、原图 hash、供应商原始输出 hash、最终尺寸和处理算法。

结果仍保存到同一个 Asset 下的新 AssetVersion，保留父版本关系；新版本没有旧人工批准或 AI 检查结果，只有重新审核通过后才能显式选入导出。冻结导出保留工具参数和来源；旧客户端仍使用既有兼容投影。内容相同的结果继续按现有 hash 规则复用，不能伪造一次新的组合执行。

源尺寸或输入校验失败会在调用前释放费用预留。供应商已返回但透明输出无效，记录失败并结算已发生的调用；没有可用子版本。超时及存储结果不确定沿用现有 uncertain/对账流程，不自动再次付费请求。

图片保持单帧 PNG/JPEG/WebP，单文件最多 20MiB；最终最多 25,000,000 像素、最长边 8192。原图尺寸不匹配、越界或小于一个像素的擦除区，在派发前拒绝。

## 发布与验证

复用现有 JSON 配方和记录，不需要新增迁移、表、角色权限或服务配置。发布顺序仍先 API/Worker，再 Web；这份开发变更本身不执行部署。

- `tests/test_supplier_image_tools.py`：实际接口、预览费用、原始请求编码、任务持久化、父子版本、透明输出、像素保护、单次调用、费用结算、重新审核及导出来源。
- `tests/test_postgres_architecture.py`：隔离 PostgreSQL 的真实 API/Worker 运行角色，完成三项操作并进入重新审核。
- `web/__tests__/components/workspace/marketing-image-tools.spec.tsx`：私有原图预览、按模型选择、矩形选区、目标尺寸限制和仅准备草稿。

以上验证只替换外部 HTTP/供应商调用，不连接真实供应商。没有真实放大、抠图、补景效果或付费账单的实测结论。
