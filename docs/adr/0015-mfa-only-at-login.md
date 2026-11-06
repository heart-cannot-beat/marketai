# ADR 0015: MFA 只在登录时验证

**Status:** Proposed (Accepted on merge)  
**Date:** 2026-11-04  
**Decided in:** #378（维护者决定，2026-11-04）  
**Supersedes:** ADR 0011「Step-up scope」一节；ADR 0011「Front-flow layering」继续有效  
**Relates to:** ADR 0003（Workspace 身份边界）、ADR 0005（访问、用量与 Provider 成本）、ADR 0010（围绕问题与有边界的证据组织营销测试）

## 背景

ADR 0011 要求 14 个有外部或资金副作用的操作先做一次 MFA 二次验证（step-up）：BFF 的 `/api/auth/step-up/grant` 只在会话的 `mfaAuthenticatedAt` 距今不超过 10 分钟时签发一次性授权，BFF 把它作为 `X-Step-Up-Assertion` 转给 FastAPI，FastAPI 校验后把 jti 记进 `library_stepup_assertions` 防重放。控制面里涉及 Owner 的成员操作（邀请 Owner、提升为 Owner、移除 Owner）也要求 10 分钟内的新鲜 MFA。

结果是用户 MFA 登录超过 10 分钟后，每次发布数据源、绑定凭据、确认花费护栏、发布 Scale 目标或改成员角色，都要再输一次验证码；没绑定 MFA 的成员即使角色允许，也会收到 `mfa_enrollment_required`，做不了这些操作。维护者 2026-11-04 在 #378 决定：MFA 只在登录时验证。

## 决定

下面 14 个操作**只按角色授权，不再要求任何 MFA**：既不要求 10 分钟内的新鲜 MFA，也不要求本次登录用过 MFA。没绑定 MFA 的成员，只要角色允许，也能执行。

| 类别 | 操作 |
|---|---|
| 创意库 | `publish_library_entry`、`withdraw_library_version`（所有条目类型）、`bind_library_data_source_credential` |
| 凭据 | `create_credential`、`rotate_credential`、`bind_credential`、`revoke_credential`、`revoke_credential_version` |
| Meta 连接 | `activate_meta_connection`、`supersede_meta_binding`、`retire_meta_connection` |
| 花费护栏 | `confirm_spend_guardrail` |
| Scale 目标 | `create_scale_target`、`withdraw_scale_target` |

- **控制面的成员操作**（邀请 Owner、提升为 Owner、移除 Owner）同样去掉 MFA 时效检查，**保留输入确认短语**（`INVITE OWNER`、`PROMOTE TO OWNER`、`REMOVE OWNER`）。确认短语防的是误点，与 MFA 无关。
- **整条 step-up 机制删除**：两边的受保护清单（`PROTECTED_STEP_UP_ACTIONS`、`PROTECTED_OPERATIONS`）、按条目类型的豁免函数与 `contracts/step-up/exemption-matrix.json`、跨边界契约测试、BFF 的授权签发接口和 `X-Step-Up-Assertion` 转发、FastAPI 的 `validate_step_up_assertion`，以及前端的二次验证弹窗。OpenAPI 删掉 8 个操作的 `X-Step-Up-Assertion` 参数。
- **不变的**：
  - 登录时的 MFA：已绑定 MFA 的用户登录仍要输验证码；MFA 的绑定、重置与恢复流程不变。
  - Workspace 开通时首个 Owner 必须完成 MFA 绑定的门槛不变；它是开通条件，不是操作前的二次验证。
  - 每个操作的角色规则不变：例如凭据、花费护栏和 Scale 目标仍然只有 Owner 能做；创意库版本的撤回仍限 Owner，各条目类型的发布角色按[创意库合同 §4](../product/creative-library-ownership-and-reuse-contract.md#4-角色与权限)。
  - 每个操作已有的二次确认、服务端 Preview、影响分析、幂等、`If-Match` 与成本预留不变。
  - 审计事件照常追加写入。
  - Meta 写入在开发环境仍然关闭。

我们没有选择「把授权有效期延长到与登录会话相同」或「要求本次登录用过 MFA」：前者仍多一次请求和弹窗，两者都让没绑定 MFA 的成员做不了角色允许的操作，与维护者的决定矛盾。

## 取代

ADR 0011「Step-up scope」一节（含其操作表、`step_up_required(action, entry_type)` 和「新增写第三方的操作必须先加入两边受保护清单」的要求）由本 ADR 取代。ADR 0011「Front-flow layering」一节继续有效。ADR 0010（围绕问题与有边界的证据组织营销测试）中「ADR 0011 的认证、MFA 与按条目类型约束 step-up 的规则继续生效」改按本 ADR 理解：认证与登录 MFA 继续生效，step-up 规则已取消。

## 后果

- 用户登录之后不会再被要求输入验证码。没绑定 MFA 的成员能做角色允许的全部操作。账户安全只靠登录这一道 MFA 和会话本身（会话撤销、授权 epoch、成员关系校验）：会话被盗用时，攻击者在会话有效期内能做该角色允许的操作，不再被第二道验证码拦下。我们接受这一点，换取不被打断的操作。
- 审计照常：这些操作的审计事件（actor、时间、命令、目标）照常写入。新审计行不再带 `step_up` 标记（`exempt` / `verified`）；历史行保持原样，不改写。
- `StepUpAssertionRecord`（表 `library_stepup_assertions`）保留，停止写入。已有记录是审计证据，不做删除迁移，以后需要时再另议。控制面的 `StepUpGrant` 表同样保留、停止写入。
- 客户端不再收到 `mfa_step_up_required`、`mfa_enrollment_required` 和 `step_up_*` 这类错误码；没有产生方的码连同文案一起删除。
- 以后新增有外部或资金副作用的操作，不再加入任何受保护清单；它的保护来自角色、确认、Preview、幂等、成本预留（ADR 0005）与审计。
