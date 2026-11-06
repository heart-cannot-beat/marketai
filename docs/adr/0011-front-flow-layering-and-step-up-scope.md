# ADR 0011: Front-flow layering and step-up scope

**Status:** Proposed (Accepted on merge)  
**Date:** 2026-10-10  
**Part of:** 前台两项配置改造 Epic  
**Relates to:** ADR 0003 (Workspace identity boundary), ADR 0007 (Library drafts / versions / snapshots), `docs/product/front-flow-two-config-contract.md`

## Context

Trying the deployed Beta on 2026-10-10 showed that the v9 six-area workspace
asks the user to do every zero-to-one step by hand: type a `lib_`/`prod_` id,
build dimension templates value by value, pick provider, model, mode, format
and variants, then preview and submit. Product owners want two configurations
("set the product", "set the ad test") with AI producing the first draft.

Two existing decisions get in the way of that flow:

1. Every Library publish, including a product or a dimension template, requires
   an MFA step-up grant (`PROTECTED_STEP_UP_ACTIONS` in
   `web/lib/security/step-up-policy.ts`, `PROTECTED_OPERATIONS` in
   `app/platform/step_up.py`). The grant is valid ten minutes after an MFA
   login, so an ordinary "confirm product" click re-prompts for an
   authenticator code most of the time.
2. The six areas are the whole information architecture. There is no place for
   a guided front flow without either removing areas (breaking the golden path
   and the v9 acceptance contract) or adding a seventh peer area (which keeps
   the complexity visible).

## Decision

### Front-flow layering

- The six v9 areas stay, with their routes, tests and golden path, and become
  the **advanced workbench**: collapsed in navigation, reachable by anyone.
- A new **front flow** (`/w/[slug]/products`, `/w/[slug]/tests/...`) becomes
  the Workspace home. It only orchestrates existing domain commands (Library
  entries, generation drafts/previews/runs, review, experiments). AI-drafted
  content lands as the same immutable versions and snapshots.
- The front flow may hide parameters behind Workspace defaults; it may not
  weaken any v9 §2 invariant.

### Step-up scope

> **已被 [ADR 0015](0015-mfa-only-at-login.md) 取代（2026-11-04，#378）。** MFA 只在登录时验证；下表的 14 个操作只按角色授权，整条 step-up 机制已删除。本节保留为历史记录，「Front-flow layering」一节继续有效。

Step-up is reserved for actions with external or financial side effects.
Publishing or withdrawing a `product` or `dimension_template` version requires
only an authenticated session with Workspace membership (and the existing
role rules in `app/library/service.py`).

| Action | Step-up |
|---|---|
| `publish_library_entry` for `product`, `dimension_template` | **no** |
| `publish_library_entry` for every other entry type (data_source, prompt_policy, reference_asset, person, …) | yes |
| `withdraw_library_version` for `product`, `dimension_template` | no (Owner still required) |
| `withdraw_library_version` for other types | yes |
| credential create / rotate / bind / revoke, Meta connection, data-source credential binding, spend guardrail, Scale targets | yes (unchanged) |

The action names stay identical on both sides so the existing cross-boundary
contract test still compares the two sets; scoping by entry type is a
function (`step_up_required(action, entry_type)`) implemented and tested on
both sides. The BFF never issues, and FastAPI never demands, an assertion for
an exempt action; a client that sends one anyway is still validated.

## Consequences

- "Confirm product" is one click. Product and template publishes are still
  audited (append-only audit events) and still produce immutable versions.
- Withdrawing a product version remains Owner-only; losing step-up on it is
  acceptable because withdraw blocks new external actions but never deletes
  evidence.
- The Playwright golden path keeps exercising the six areas; a new front-flow
  golden path is added by the Epic.
- Any future action that writes to a third party must be added to the
  protected set on both sides before it ships.
