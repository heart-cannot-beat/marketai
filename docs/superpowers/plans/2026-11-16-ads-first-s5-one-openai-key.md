# 新开头 S5：一个 OpenAI Key 跑完全程，保存 Key 时校验 · 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 工作区只绑定了 OpenAI Key、部署配置的策划供应商是 Gemini 时，策划、结果解读和视觉检查也改用 OpenAI；保存 OpenAI Key 时可以要求先向 OpenAI 校验，被拒就不保存。

**Architecture:** 新模块 `app/marketing/provider_choice.py` 负责“这次用哪家、哪个模型、最高多少钱”：先用部署配置的那一家，它没有 Key 而 OpenAI 有时改用 OpenAI。`planner_capability` 和 `ai_qc.capability` 都改成调它，返回字段不变；Worker 本来就按调用记录里冻结的供应商取 Key，不用改。新模块 `app/entitlements/provider_key_check.py` 向 OpenAI 发一次 `GET /models` 判断 Key，`create_credential`、`rotate_credential` 在请求体 `verify` 为真时先调它，照搬现有 ScrapeCreators 校验的形状（先校验、被拒不保存、校验本身留审计证据）。

**Tech Stack:** FastAPI、SQLAlchemy、httpx（测试用 `httpx.MockTransport`）、unittest；OpenAPI 生成脚本和 web 生成的客户端。

**Spec:** [`docs/superpowers/specs/2026-11-16-ads-first-opening-design.md`](../specs/2026-11-16-ads-first-opening-design.md) §5.1、§8；第 1 步计划 [`2026-11-16-ads-first-step-1.md`](2026-11-16-ads-first-step-1.md) 的 S5 一节；Issue heart-cannot-beat/marketai#192。

## 开工前先确认（已核实，2026-11-16）

**1. 用哪个不收费的请求验证 OpenAI Key：`GET {OPENAI_BASE_URL}/models`。**

- OpenAI API 参考的 List models：`GET /models`，“Lists the currently available models”，用 `Authorization: Bearer <key>` 认证，返回 `{"object": "list", "data": [Model…]}`（[参考](https://developers.openai.com/api/reference/resources/models/methods/list)）。它不调用任何模型、不消耗 token，也不生成图片；OpenAI 按 token 和图片计费，这个请求不在计费项里。参考页本身没有写“免费”二字，这一点记为剩余风险。
- 错误码以 OpenAI 的错误码指南为准（[error codes](https://developers.openai.com/api/docs/guides/error-codes)）：`401` 是 Key 不对、Key 不属于任何组织或 IP 不在允许名单；`403` 是所在国家或地区不支持；`429` 带 `error.code`（`insufficient_quota`、`credit_balance_exhausted`、`organization_spend_limit_exceeded`、`project_spend_limit_exceeded`、`organization_usage_limit_exceeded`）时是账户没有余额或到了花费上限，不带这些 code 的 `429` 是限流；`500`、`503` 是 OpenAI 自己的问题。
- 不用 `GET /models/{model}`：参考页没有说模型对这个 Key 不可用时返回什么，按它判断“有没有出图权限”没有依据。

**2. “Key 有效但没有出图权限”怎么和“Key 无效”区分：保存时只能判断后者，前者要到第一次出图才知道。**

- GPT Image 模型要求组织先完成 OpenAI 的组织验证。没验证的组织调用出图接口时，OpenAI 返回 `403`、`type: invalid_request_error`、`code: null`，只有说明文字里写着要先验证组织。这个检查发生在出图请求上，而出图请求是收费的。
- `GET /models` 的文档没有说返回的列表会按 Key、项目或组织验证状态过滤，所以“列表里有 `gpt-image-…`”不能证明能出图，“没有”也不能证明不能出图。不用它做判断，以免给出错误的结论。
- 也不用“故意发一个参数错误的出图请求”去探权限：OpenAI 先查权限还是先查参数没有文档，哪天顺序变了就会变成一次真实收费。
- 因此本块的结论是：`401` 判为 Key 无效（`key_invalid`），`403` 判为供应商拒绝访问（`access_denied`），没有余额或到了花费上限判为 `quota_exhausted`，三者都不保存；`200` 表示 Key 有效，保存。“有效但组织没验证”的 Key 会被保存，第一次出图时由现有的出图失败处理报出来。出图失败时把这种 `403` 翻译成“请先在 OpenAI 完成组织验证”属于出错状态的收尾，留给 S16。

## Global Constraints

- 分支 `claude/issue-192-one-openai-key`，一个 PR，引用 #192，不合并。
- `verify` 不传（或为 `false`）时，`create_credential`、`rotate_credential` 的行为、返回和副作用和现在完全一样，不发任何外部请求。
- 只有 Owner 能添加和替换 Key；非 Owner 的请求在发出任何外部请求之前就被拒。
- Key 只出现在发给 OpenAI 的请求头里。返回体、日志、审计证据都不能出现 Key，也不能出现 OpenAI 的错误说明文字（它可能带着部分 Key），只记录状态码和 `error.code`。
- 测试只用 `httpx.MockTransport`，不发真实请求；Key 一律是 `fixture-…` 这样的假字符串。
- 旧的六个环境变量 `MARKETAI_PLANNING_PROVIDER`、`MARKETAI_PLANNING_MODEL`、`MARKETAI_PLANNING_MAX_COST_USD`、`MARKETAI_MARKETING_QC_PROVIDER`、`MARKETAI_MARKETING_QC_MODEL`、`MARKETAI_MARKETING_QC_MAX_COST_USD` 继续有效；不设新配置的部署，`get_marketing_capabilities` 和视觉检查报价的结果和现在一模一样。
- `flow = "plan_first"` 的项目行为不变，现有测试全部通过。
- 跑 `make verify`；生成的 web 客户端变了，还要跑 `make web-verify`。

## 两个新配置

`MARKETAI_PLANNING_MODELS_JSON` 和 `MARKETAI_MARKETING_QC_MODELS_JSON`，形状相同，按供应商给模型和每次最高费用：

```json
{"openai": {"model": "gpt-4.1-mini-2025-04-14", "max_cost_usd": "0.10"},
 "gemini": {"model": "<已核实的模型>", "max_cost_usd": "0.10"}}
```

规则：

- 一条有效的条目：供应商是 `gemini` 或 `openai`；`model` 是非空字符串；`max_cost_usd` 是字符串形式的有限小数，`0 < x <= 10`（和现在的上限一致）。无效的条目当作没有，整段 JSON 解析不了就当作空表并记一条警告日志。
- 部署配置的那一家（`*_PROVIDER`，默认 `gemini`）：只要旧的 `*_MODEL` 或 `*_MAX_COST_USD` 设了任何一个，就用旧的两个值，照今天的规则校验，表里这一家的条目不看；两个都没设才用表里这一家的条目。
- OpenAI 作为备选只从表里读（旧变量只描述部署配置的那一家）。

## 选择顺序

1. 部署配置的那一家条目有效，且工作区绑定了它的 Key：用它。
2. 否则，部署配置的不是 OpenAI、表里 OpenAI 条目有效、工作区绑定了 OpenAI 的 Key：用 OpenAI。
3. 都不成立时不可用。报告的供应商是第一家条目有效的（部署配置的那家优先，其次 OpenAI），原因是 `credential_required`；两家都没有有效条目时报告部署配置的那家，原因是“未配置”（策划 `planner_not_configured`，视觉检查 `qc_not_configured`）。
4. 选中之后再做现有的 `authorize_paid`，策划还做 `check_budget`；这两步的结果和原因码不变。

两家 Key 都有时仍用部署配置的那家。`quote_hash` 的算法不变（供应商、模型、上限、`planning-v1`），所以选中的供应商变了，旧报价自然失效。

## 保存 Key 时的校验

- `CreateCredentialRequest`、`RotateCredentialRequest` 加 `verify: bool = False`。
- `verify` 为真、供应商是 OpenAI：先在一个事务里查重（`credential_key_active` 409，和数据供应商的做法一样，查重先于外部请求），再发 `GET /models`。
  - 有效：照常保存，在同一个事务里给新版本追加一条审计事件 `credential.provider_key_checked`（`resource_type = "credential_version"`）。
  - 被拒：返回 422 `provider_key_rejected`，`params = {"provider": "openai", "reason": "key_invalid" | "access_denied" | "quota_exhausted"}`，不保存；审计事件记在 `credential_key` 上。
  - 没能判断（超时、网络错误、限流、5xx、返回体不是模型列表）：返回 503 `provider_key_check_unavailable`，`retryable: true`，不保存；审计事件同上。
- `verify` 为真、供应商没有校验（目前是 Gemini 和其他 AI 供应商）：返回 422 `provider_key_check_unsupported`，不发请求、不保存。调用方要求了校验，就不能悄悄跳过，否则开始页会把没校验的 Key 当成校验过。
- 数据供应商（ScrapeCreators）不管 `verify` 是什么，照旧总是校验。
- 替换时被拒，当前版本保持有效、绑定不变。
- 返回体不变：`last_check` 仍然只给数据供应商填，OpenAI 的校验结果只在审计事件里。

## Review Focus

1. **部署只设了旧变量（今天所有部署的状态）。** 只有 OpenAI Key、部署配的是 Gemini 时，结果仍是 `credential_required`、供应商 `gemini`，直到运维设了 `MARKETAI_PLANNING_MODELS_JSON`。测试 `test_without_the_table_the_answer_is_unchanged`（Task 1）。
2. **OpenAI 返回 401 时说明文字里带着部分 Key。** 返回体、审计事件、日志都不能出现它。测试 `test_rejection_never_echoes_the_key_or_the_provider_message`（Task 3）。
3. **校验通过之后、保存之前，另一个请求先存了同名 Key。** 第二个保存仍按现有规则返回 409，不会出现两条有效版本。测试 `test_a_duplicate_is_refused_before_openai_is_asked`（Task 4）。
4. **Member 带着 `verify: true` 来存 Key。** 403，并且没有发出任何外部请求（不能借 Member 的手探测 Key）。测试 `test_members_cannot_add_or_replace_a_key_and_cause_no_check`（Task 4）。
5. **另一个工作区有 OpenAI Key。** 本工作区不能借用：策划仍报 `credential_required`；拿别人的 `credential_key` 去替换返回 404 且不发请求。测试 `test_another_workspace_key_is_never_borrowed`（Task 1）和 `test_another_workspace_cannot_replace_the_key`（Task 4）。

---

### Task 1：策划改用 OpenAI 的选择逻辑

**Files:**
- Create: `app/marketing/provider_choice.py`
- Modify: `app/marketing/service.py:310-329`（`planner_capability`）
- Test: `tests/test_marketing_provider_choice.py`

**Interfaces:**
- Produces:
  - `provider_choice.SUPPORTED = ("gemini", "openai")`，`provider_choice.FALLBACK = "openai"`
  - `@dataclass(frozen=True) class ProviderChoice: provider: str; model: str; maximum: str; amount: Decimal | None; reason: str | None`（`reason` 取 `None`、`"credential_required"`、`"not_configured"`；`amount` 只在条目有效时有值）
  - `provider_choice.choose(session, ctx, prefix: str) -> ProviderChoice`，`prefix` 是 `"MARKETAI_PLANNING"` 或 `"MARKETAI_MARKETING_QC"`，读 `{prefix}_PROVIDER`、`{prefix}_MODEL`、`{prefix}_MAX_COST_USD`、`{prefix}_MODELS_JSON`
  - `planner_capability(session, ctx)` 返回字段不变：`provider`、`model`、`available`、`unavailable_reason`、`maximum_cost`、`currency`、`quote_hash`

- [ ] **Step 1：写失败的测试**（`MarketingPlanningFixtures` 上，经 `GET /api/v1/marketing/capabilities`）

```python
GEMINI_DEPLOYMENT = {"MARKETAI_PLANNING_PROVIDER": "gemini", "MARKETAI_PLANNING_MODEL": "gemini-text-test",
                     "MARKETAI_PLANNING_MAX_COST_USD": "0.20"}
TABLE = json.dumps({"openai": {"model": "openai-text-test", "max_cost_usd": "0.10"}})

def test_an_openai_key_alone_plans_when_the_deployment_prefers_gemini(self):
    self.paid_setup()  # binds only an OpenAI key
    paid = self.capabilities({**GEMINI_DEPLOYMENT, "MARKETAI_PLANNING_MODELS_JSON": TABLE})
    assert (paid["available"], paid["provider"], paid["model"], paid["maximum_cost"]) == (True, "openai", "openai-text-test", "0.10")
    assert paid["unavailable_reason"] is None

def test_the_fallback_is_frozen_on_the_paid_call(self):
    # byok planning started with that quote: PlanningCallRecord.provider == "openai", model == "openai-text-test",
    # maximum_cost == "0.10"
def test_both_keys_bound_keeps_the_deployment_provider(self):  # provider "gemini", model "gemini-text-test"
def test_without_the_table_the_answer_is_unchanged(self):
    # only OpenAI key, GEMINI_DEPLOYMENT, no table → available False, reason "credential_required", provider "gemini",
    # maximum_cost "0"
def test_legacy_settings_win_over_the_table_for_the_deployment_provider(self):
    # PROVIDER=openai, legacy MODEL "text-test"/MAX "0.10", table openai entry "other-model" → model "text-test"
def test_the_table_alone_configures_the_deployment_provider(self):
    # PROVIDER=openai, no legacy vars, table openai entry → available, model from table
def test_unusable_entries_are_not_configured(self):
    # for each of: malformed JSON "{", max "11", max "0", max 0.1 (number), model "" →
    # PROVIDER=gemini without legacy vars → reason "planner_not_configured"
def test_a_table_entry_without_its_key_reports_that_provider(self):
    # PROVIDER=gemini, no legacy vars, table has openai only, no key bound → reason "credential_required",
    # provider "openai"
def test_another_workspace_key_is_never_borrowed(self):
    # this workspace binds OpenAI; a second workspace (self._make_context()) with the same env gets
    # available False, reason "credential_required"
```

`capabilities(env)` 是测试里的小助手：`patch.dict("os.environ", env, clear=False)` 并先把六个策划变量清掉，再读 `paid`。

- [ ] **Step 2：跑测试，确认失败**

Run: `/Users/happy/Desktop/marketai-public/.venv/bin/python -m pytest tests/test_marketing_provider_choice.py -q`
Expected: 回退相关的用例 FAIL（`provider` 是 `gemini`，`credential_required`），`test_without_the_table_the_answer_is_unchanged` 和旧变量相关的用例 PASS。

- [ ] **Step 3：实现 `provider_choice.choose`，`planner_capability` 改成调它**

`not_configured` 映射为 `planner_not_configured`；`reason is None` 时再做 `authorize_paid` 和 `check_budget(choice.amount)`。

- [ ] **Step 4：跑测试，确认通过**；再跑 `tests/test_marketing_planning.py tests/test_marketing_planner_failures.py tests/test_marketing_architecture.py`，确认旧用例不变。

- [ ] **Step 5：Commit**

### Task 2：视觉检查同样回退

**Files:**
- Modify: `app/marketing/ai_qc.py:138-156`（`capability`）
- Test: `tests/test_marketing_provider_choice.py`

**Interfaces:**
- Consumes: Task 1 的 `provider_choice.choose(session, ctx, "MARKETAI_MARKETING_QC")`
- Produces: `ai_qc.capability(session, ctx, row)` 返回字段不变：`provider`、`model`、`unit_maximum`、`reason`；`row.execution_mode == "mock"` 时仍返回 Mock，不读配置。

- [ ] **Step 1：写失败的测试**（直接调 `ai_qc.capability`，`row = SimpleNamespace(execution_mode="byok")`）

```python
def test_vision_checks_fall_back_to_openai(self):
    # QC PROVIDER=gemini + legacy gemini vars, MARKETAI_MARKETING_QC_MODELS_JSON openai entry "vision-test"/"0.10",
    # only an OpenAI key → {"provider": "openai", "model": "vision-test", "unit_maximum": "0.10", "reason": None}
def test_vision_checks_without_the_table_are_unchanged(self):
    # same without the table → reason "credential_required", provider "gemini", unit_maximum "0"
def test_vision_checks_with_no_usable_entry_are_not_configured(self):  # reason "qc_not_configured"
def test_mock_rounds_never_read_the_settings(self):  # execution_mode "mock" → provider "mock"
```

- [ ] **Step 2：跑测试，确认失败**
- [ ] **Step 3：实现**（`not_configured` 映射为 `qc_not_configured`；`reason is None` 时再做 `authorize_paid`）
- [ ] **Step 4：跑 `tests/test_marketing_provider_choice.py tests/test_marketing_ai_qc.py`，确认通过**
- [ ] **Step 5：Commit**

### Task 3：OpenAI Key 校验

**Files:**
- Create: `app/entitlements/provider_key_check.py`
- Test: `tests/test_provider_key_check.py`

**Interfaces:**
- Produces:
  - `CHECK_EVENT = "credential.provider_key_checked"`
  - `@dataclass(frozen=True) class ProviderKeyCheck: provider: str; outcome: Literal["valid", "rejected", "unavailable"]; reason: str | None; http_status: int | None; elapsed_ms: int; diagnostic: str`
  - `checks_key(provider: str) -> bool`（目前只有 `"openai"`）
  - `async check_key(provider: str, plaintext: bytes, *, transport: httpx.AsyncBaseTransport | None = None) -> ProviderKeyCheck`
  - `record_check(session, check: ProviderKeyCheck, *, credential_version_id: str | None = None, credential_key: str | None = None) -> None`（`append_event`，details 是 `provider`、`outcome`、`reason`、`http_status`、`elapsed_ms`、`diagnostic`）

- [ ] **Step 1：写失败的测试**（`check_key("openai", key, transport=httpx.MockTransport(fake))`，`config.OPENAI_BASE_URL` 打成 `https://openai.test/v1`）

```python
async def test_a_model_list_means_the_key_is_valid(self):
    # 200 {"object": "list", "data": [{"id": "fixture-model", "object": "model"}]} → outcome "valid", reason None
    # exactly one request: GET https://openai.test/v1/models, Authorization "Bearer fixture-openai-key", empty body
async def test_401_is_an_invalid_key(self):          # outcome "rejected", reason "key_invalid", http_status 401
async def test_403_is_access_denied(self):           # "rejected", "access_denied"
async def test_no_credit_or_spend_limit_is_quota_exhausted(self):
    # 429 with error.code in each of the five codes → "rejected", "quota_exhausted"
async def test_anything_unreadable_is_unavailable(self):
    # 429 rate limit (code None), 500, 503, timeout, ConnectError, non-JSON 200, 200 without a data list → "unavailable"
async def test_a_key_a_header_cannot_carry_is_rejected_without_a_request(self):
    # b"", b"fixture key with spaces", "fixture-ключ".encode() → "rejected", "key_invalid", zero requests
async def test_rejection_never_echoes_the_key_or_the_provider_message(self):
    # 401 body {"error": {"message": "Incorrect API key provided: fixture-***-key", "code": "invalid_api_key"}} →
    # diagnostic == "models answered 401 (invalid_api_key)"; neither the key nor the message appears in repr(check)
```

- [ ] **Step 2：跑测试，确认失败**（`ModuleNotFoundError`）
- [ ] **Step 3：实现**

`httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False, transport=transport)`；Key 去掉首尾空白后必须全是可见 ASCII 才发请求（同 ScrapeCreators）；`error.code` 只有匹配 `^[a-z0-9_]{1,64}$` 才写进 `diagnostic`；`check_key` 记一行日志：`provider`、`outcome`、`reason`、`http_status`、`elapsed_ms`、`diagnostic`。

- [ ] **Step 4：跑测试，确认通过**
- [ ] **Step 5：Commit**

### Task 4：`create_credential`、`rotate_credential` 的 `verify`

**Files:**
- Modify: `app/entitlements/api.py`（两个请求体、`_coded` 加 `params` 和 `retryable`、新的 `_check_provider_key`、两个接口）
- Test: `tests/test_provider_key_check.py`（API 部分，沿用 `tests/test_market_data_credentials.py` 的 `TestClient` 和打补丁方式：把 `provider_key_check.httpx.AsyncClient` 换成带 `MockTransport` 的）

**Interfaces:**
- Consumes: Task 3 的 `checks_key`、`check_key`、`record_check`、`CHECK_EVENT`
- Produces: `CreateCredentialRequest.verify: bool = False`、`RotateCredentialRequest.verify: bool = False`；错误码 `provider_key_rejected`（422）、`provider_key_check_unavailable`（503）、`provider_key_check_unsupported`（422）

- [ ] **Step 1：写失败的测试**

```python
def test_a_verified_openai_key_is_checked_once_then_stored(self):
    # 200 list → 200; one request with the Bearer key; listed has the version; one CHECK_EVENT on
    # ("credential_version", version_id) with outcome "valid"; no secret in the response or the event
def test_a_rejected_key_is_not_stored_and_says_why(self):
    # 401 → problem 422 provider_key_rejected, params {"provider": "openai", "reason": "key_invalid"};
    # listed == []; one CHECK_EVENT on ("credential_key", "openai_api_key") with outcome "rejected"
def test_a_check_that_could_not_run_stores_nothing(self):
    # 503 → problem 503 provider_key_check_unavailable, retryable True; listed == []
def test_without_verify_nothing_changes(self):
    # create and rotate with no verify field, and again with verify False → 200, zero requests, no CHECK_EVENT,
    # last_check None
def test_replacing_with_a_rejected_key_keeps_the_current_one(self):
    # create (no verify), bind; rotate verify True → 401 → 422; the listed version and the generation binding
    # are unchanged; rotate verify True → 200 → version_no 2
def test_providers_without_a_check_refuse_verify(self):
    # provider "gemini", verify True → 422 provider_key_check_unsupported, zero requests, listed == []
def test_a_duplicate_is_refused_before_openai_is_asked(self):  # second create verify True → 409, one request total
def test_members_cannot_add_or_replace_a_key_and_cause_no_check(self):
    # member context: create and rotate with verify True → 403 owner_role_required, zero requests
def test_another_workspace_cannot_replace_the_key(self):
    # other workspace owner: rotate "openai_api_key" verify True → 404 credential_not_found, zero requests;
    # the original workspace still lists its version
```

- [ ] **Step 2：跑测试，确认失败**
- [ ] **Step 3：实现**

`verify` 的分支放在现有的数据供应商分支之后（`elif`），数据供应商的路径一个字不改；有效的检查在保存的同一个事务里 `record_check(..., credential_version_id=...)`。

- [ ] **Step 4：跑 `tests/test_provider_key_check.py tests/test_market_data_credentials.py tests/test_entitlements*.py`，确认通过**
- [ ] **Step 5：Commit**

### Task 5：契约、生成文件、文档

**Files:**
- Modify: `contracts/openapi/…`（`python scripts/generate_openapi.py`）和 `bash scripts/generate_all.sh` 生成的文件（含 web 的生成客户端）
- Modify: `.env.example`、`docs/development.md`、`docs/product/ai-marketing-planning-slice.md`、`docs/product/ai-marketing-ai-qc.md`（两个新配置、选择顺序、旧变量优先）

- [ ] **Step 1：重新生成**，确认 diff 只有两个请求体多了 `verify`。
- [ ] **Step 2：更新文档**
- [ ] **Step 3：`make verify PYTHON=… ALEMBIC=…` 和 `make web-verify`（`web/node_modules` 链到主目录）全部通过**
- [ ] **Step 4：Commit，推送，开 PR**（引用 #192；写明测试证据、两个新配置的运维影响、剩余风险）

## 剩余风险（写进 PR）

- OpenAI 的参考页没有明写 `GET /models` 不收费，依据是它不调用模型、不消耗 token。
- 组织没做验证的 OpenAI Key 会被保存，第一次出图才失败；翻译这类失败留给 S16。
- 只有 OpenAI 有保存时校验；Gemini 的 Key 带 `verify: true` 会被拒为 `provider_key_check_unsupported`。
- 部署不设 `MARKETAI_PLANNING_MODELS_JSON`（和 QC 的那个）时没有回退；要让 S5 生效，生产要配上 OpenAI 的条目。
- 只有 Gemini Key 时首批报价返回 `openai_key_required` 的测试，按第 1 步计划在 S4 合并后补。
