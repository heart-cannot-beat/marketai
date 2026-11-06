# Validate the customer Meta access path

Date: 2026-09-01
Issue: [#9](https://github.com/heart-cannot-beat/marketai-archive/issues/9)
Scope: Meta Marketing API requirements for self-service customers to connect their own ad accounts for read-only insights

## Source policy

This note uses only first-party Meta and Meta for Developers material checked on 2026-09-01. Meta changes dashboard labels and review requirements independently of this repository, so the App Dashboard is the operational authority at submission time. Where Meta does not publish a fixed document list or service-level agreement, this note does not invent one.

## Decision and viable path

MarketAI should use Facebook Login for Business with a Business Integration System User (BISU) access token. Meta recommends BISU tokens for automated retrieval of ads insights that must continue without a person being present. The token is attached to the customer business portfolio rather than to the person who authorizes it. [Facebook Login for Business](https://developers.facebook.com/documentation/facebook-login/facebook-login-for-business)

The production path has separate gates:

1. **Build and role-test now.** Create a Business-type app, add Marketing API, associate the app with a provider business portfolio that MarketAI fully controls, create a Facebook Login for Business configuration, and implement the authorization-code exchange, encrypted credential storage, ad-account selection, Insights reads, throttling, and re-authentication UX. A BISU flow tester must have both an app role and full control of the client business. [Facebook Login for Business](https://developers.facebook.com/documentation/facebook-login/facebook-login-for-business)
2. **Complete Business Verification for MarketAI's provider business.** Meta requires an app that requests Advanced Access and lets other businesses access their own data to be connected to a verified business. The current Business Manager flow determines what evidence, if any, must be supplied. [Business Verification](https://developers.facebook.com/docs/development/release/business-verification/)
3. **Request Advanced Access for each required permission.** `ads_read` is required. Request `business_management` only if the chosen business-asset discovery flow actually needs it. Each requested permission needs its own use-case description and review evidence. [Permissions reference](https://developers.facebook.com/docs/permissions/)
4. **Request the Marketing API Access Tier upgrade separately.** Full Access is a feature upgrade in App Dashboard; it is not automatically granted when the usage counters cross their thresholds. The app must meet the current usage/error requirements and click **Upgrade** for the Marketing API Access Tier feature. [Marketing API authorization](https://developers.facebook.com/docs/marketing-api/get-started/authorization)
5. **Launch self-service only after the relevant approvals are active.** Until then, restrict live Meta authorization to eligible app-role testers and keep the controlled Mock as the customer-facing evidence path.

## 1. Two independent access controls

Meta uses similar words for two different controls. They must not be collapsed into one approval.

| Control | Levels | What it controls | MarketAI action |
|---|---|---|---|
| Permission/feature access | Standard / Advanced | Whether a permission such as `ads_read` can be granted by people outside app roles | Request Advanced Access for each required permission through App Review |
| Marketing API Access Tier | Limited / Full | Rate limits, Business Manager/Catalog API access, and system-user capacity | Meet the usage/error criteria, then explicitly request the Marketing API Access Tier upgrade |

Meta's current authorization guide says Limited Access is granted when Marketing API is added. Full Access requires the app to click **+Upgrade** for the Marketing API Access Tier feature and satisfy the dashboard requirements. As of Meta's May 4, 2026 update, the published thresholds are at least 500 successful Marketing API calls in the previous 15 days and less than a 15% error rate over the most recent 500 calls. These thresholds qualify an app to request/maintain access; they do not themselves perform the upgrade. [Marketing API authorization](https://developers.facebook.com/docs/marketing-api/get-started/authorization), [Marketing API Access Tier update](https://developers.meta.com/blog/updates-to-ads-management-standard-access-feature/)

### Limited Access

- Intended for development rather than production use with live advertisers.
- One system user plus one admin system user.
- Ad-account-level score limit of 60, 300-second decay, and a 300-second block after reaching the limit.
- Calls operate on production data; Meta does not provide a separate staging dataset for ordinary calls.

### Full Access

- Intended for production use.
- Ten system users plus one admin system user.
- Ad-account-level score limit of 9,000, 300-second decay, and a 60-second block after reaching the limit.

The system-user capacities come from [Marketing API authorization](https://developers.facebook.com/docs/marketing-api/get-started/authorization); the score limits come from [Marketing API rate limiting](https://developers.facebook.com/docs/marketing-api/overview/rate-limiting/).

## 2. Required permissions

| Permission | Decision | Reason |
|---|---|---|
| `ads_read` | **Required; request Advanced Access** | Meta defines it as the read-only permission for Ads Insights reports on ad accounts the app owns or has been granted access to. Its allowed use includes custom dashboards and data analytics. |
| `business_management` | **Conditional** | Meta defines it as read/write access to Business Manager APIs for managing business assets. Add it only if the selected account-discovery endpoint requires it; it expands App Review scope. |
| `ads_management` | **Do not request** | It permits ad creation and management. MarketAI's Scale boundary is read-only and must never mutate delivery or budgets. |
| `read_insights` | **Do not request** | It covers Page, app, and web-domain insights, not Marketing API ad-account reports. |

These definitions and the current per-permission review evidence are in Meta's [permissions reference](https://developers.facebook.com/docs/permissions/). `ads_read` has no listed permission dependency, so `business_management` must not be treated as an automatic dependency. Before requesting it, prove the exact discovery call in Graph API Explorer with an app-role test business and record the endpoint and returned asset set in the implementation issue.

## 3. App Review

### Permission review versus tier review

For self-service customers who do not hold an app role, request Advanced Access to `ads_read` and any other permission demonstrated as necessary. The generic submission guide and permission reference require each permission request to carry its own description and review evidence, including the permission-specific recording described below. Separately request the Marketing API Access Tier feature upgrade for Full Access. Meta's May 4, 2026 update explicitly removed the screen-recording upload from that tier-upgrade submission; its eligibility evidence is the usage/error progress shown in App Dashboard. [App Review submission guide](https://developers.facebook.com/docs/resp-plat-initiatives/individual-processes/app-review/submission-guide), [permissions reference](https://developers.facebook.com/docs/permissions/), [Marketing API Access Tier update](https://developers.meta.com/blog/updates-to-ads-management-standard-access-feature/)

### Current submission evidence

Before submission:

- Make the app publicly reachable or provide complete reviewer access instructions.
- Make at least one successful API call with each permission requested for Advanced Access within 30 days of submission. Meta notes that logging can take up to two days.
- Give each permission and feature a distinct use-case description; do not reuse copied text.
- Record the complete login/grant flow and show the app using the granted data.
- For `ads_read`, show a business opening ad performance data in MarketAI and visibly render metrics such as impressions, conversions, spend, clicks, or reach.
- If `business_management` is requested, separately show the business-asset use that requires it and follow its current permission-reference evidence.

Meta's current recording guidance is high-resolution (1080p or better), English UI where possible, monitor width of 1440px or less, visible mouse interaction/cursor, and no audio. These are submission-guide instructions, not a substitute for the permission-specific evidence in the permissions reference. [App Review submission guide](https://developers.facebook.com/docs/resp-plat-initiatives/individual-processes/app-review/submission-guide), [permissions reference](https://developers.facebook.com/docs/permissions/)

Meta says its reviewers test with their own test accounts. Provide credentials for MarketAI itself if the product requires them, but never provide a personal Meta account's credentials. Do not plan on Meta supplying a customer ad account with historical Insights data. [App Review submission guide](https://developers.facebook.com/docs/resp-plat-initiatives/individual-processes/app-review/submission-guide)

No fixed review duration is used in this plan. Track the status shown in App Dashboard and leave launch gated on actual approval.

## 4. Business Verification

For this use case, the app must be connected to a business that has completed Business Verification because it requests Advanced Access and enables other businesses to access their own data. App-role-only testing is the documented exception. [Business Verification](https://developers.facebook.com/docs/development/release/business-verification/)

The current process is:

1. In App Dashboard, open **Settings → Basic → Verification** and connect the app to a business where the operator is an administrator.
2. If that business is already verified, the connection completes the requirement.
3. Otherwise, a business administrator completes the flow in Business Manager.
4. Supply the legal details and supporting documents requested by the live Business Manager flow and its linked Meta Help Center guidance.

This note intentionally does not prescribe articles of incorporation, a utility bill, a bank statement, government ID, or a fixed combination of them. Meta's developer guide delegates the current evidence list to Business Manager/Help Center, and the requested evidence can depend on what Meta can verify. It also does not publish a completion SLA, so the release plan must not depend on a guessed number of days or weeks. [Business Verification](https://developers.facebook.com/docs/development/release/business-verification/), [Meta Business Help Center](https://www.facebook.com/business/help/2058515294227817)

## 5. Facebook Login for Business and BISU

### Hard prerequisites

Meta documents these prerequisites for the recommended BISU flow:

- The Meta app is a Business-type app.
- BISU login is invoked from a web surface.
- Each onboarding customer has, or is willing to create, a client business portfolio.
- MarketAI's app is associated with a provider business portfolio that MarketAI fully controls; it is separate from the customer's portfolio.
- For pre-approval testing, the tester has a role on the app and full control of the client business.
- The customer authorizer is able to grant the selected business assets and permissions.

Source: [Facebook Login for Business](https://developers.facebook.com/documentation/facebook-login/facebook-login-for-business).

### Authorization-code flow

1. Create a Facebook Login for Business configuration with a System User access-token type, the required ad-account assets, and the minimum reviewed permissions. Save its `config_id`.
2. Invoke the login dialog with `config_id`, `response_type: 'code'`, and `override_default_response_type: true`. Do not replace the configuration with a free-form `scope` list for the BISU flow.
3. Send the returned authorization code to MarketAI's backend and exchange it server-to-server:

   ```text
   GET https://graph.facebook.com/<API_VERSION>/oauth/access_token
     ?client_id=<APP_ID>
     &client_secret=<APP_SECRET>
     &code=<CODE>
   ```

4. Read the client business ID from the token:

   ```text
   GET https://graph.facebook.com/<API_VERSION>/me
     ?fields=client_business_id
     &access_token=<BISU_TOKEN>
   ```

5. Resolve only the ad accounts explicitly granted through the customer configuration, let the user select the Scale source, and store the token as an encrypted Workspace `CredentialVersion`.

The flow and parameters above come from [Facebook Login for Business](https://developers.facebook.com/documentation/facebook-login/facebook-login-for-business). `<API_VERSION>` is deliberate: implementation must pin a supported Graph API version and upgrade it through normal dependency/version management rather than copying a soon-stale version number from this note.

### Lifecycle and invalidation

A BISU token defaults to no expiration for the common offline server-to-server case, but it is revocable. It is associated with the client business portfolio, not with the personal account of the administrator who completed the flow. Meta documents removal of the connected app in the customer's Business Settings as the customer-controlled invalidation path. [Facebook Login for Business](https://developers.facebook.com/documentation/facebook-login/facebook-login-for-business)

Therefore MarketAI must:

- treat authorization failures as a connection-health state rather than assuming a token is permanent;
- stop sync attempts after a terminal authorization error and avoid retry storms;
- show the Workspace Owner which connection needs re-authorization;
- rerun the BISU flow and create a new `CredentialVersion` without rewriting historical Scale evidence.

Do not invalidate BISU credentials merely because the granting administrator changes a personal password; Meta describes the token as business-associated, not user-associated.

## 6. Test options before approval

### Marketing API sandbox ad account

Meta's sandbox provides one sandbox ad account per app and supports a **subset** of Marketing API calls. Ads do not deliver, sandbox accounts cannot be operated in Ads Manager, and the Insights API is not supported because the sandbox produces no spend, clicks, impressions, or reportable insights. It is useful for supported API-shape tests, not for MarketAI's core historical-insights proof. [Marketing API Sandbox](https://developers.facebook.com/blog/post/2023/06/21/marketing-api-sandbox-capability-now-re-enabled/)

### App-role client business

Before approval, the complete BISU flow can be tested by a person who has both an app role and full control of the client business. Use an explicitly designated internal/test business and real ad account with known non-sensitive insights. Limited Access throttles apply. [Facebook Login for Business](https://developers.facebook.com/documentation/facebook-login/facebook-login-for-business), [Marketing API authorization](https://developers.facebook.com/docs/marketing-api/get-started/authorization)

### App Review reviewer accounts

Meta reviewers use their own test accounts and follow the submitted recording/access instructions. This is review infrastructure, not a source of realistic historical ad data for MarketAI development. [App Review submission guide](https://developers.facebook.com/docs/resp-plat-initiatives/individual-processes/app-review/submission-guide)

### MarketAI controlled Mock

The controlled Mock remains the pre-approval customer-facing path and the deterministic CI path. This is a **MarketAI architecture decision**, not a Meta sandbox capability. It exercises real domain state, reconciliation, quality gates, persistence, and audit behavior while replacing only the external network adapter, as required by the repository's controlled-Mock definition and Scale ADR.

## 7. What can be implemented before approval

### Implement and verify locally now

| Component | Pre-approval verification |
|---|---|
| Business-type app and Facebook Login for Business configuration | Dashboard configuration plus documented config inventory |
| Authorization-code callback and token exchange | Unit/integration tests; live role-test flow |
| `CredentialVersion` storage | Encryption, redaction, Workspace isolation, and rotation tests |
| Connection health and re-authorization | Controlled invalid-token scenarios; live removal/reconnect role test |
| Ad-account discovery/selection | Mock and role-test business; record whether `business_management` is actually required |
| Insights query, parsing, reconciliation, and evidence snapshots | Fixtures/Mock plus live role-test reads |
| Throttling/backoff | Header fixtures and Limited Access role testing |
| Scale enrollment UX | Full controlled-Mock path and role-test connection path |

### Blocked until approval

| Capability | Gate |
|---|---|
| A non-role customer grants `ads_read` | Advanced Access for `ads_read` and completed Business Verification |
| A non-role customer grants `business_management`, if retained | Advanced Access for `business_management` and completed Business Verification |
| Production Marketing API rate limits and larger system-user capacity | Explicit Full Access upgrade for Marketing API Access Tier after meeting current requirements |
| General self-service launch | All permissions/features used by the launch flow are approved and active |

The implementation can be complete before approval; only live authorization by general customers and production-tier capacity remain gated.

## 8. Rate-limit behavior to implement

Meta currently documents the `ads_insights` Business Use Case hourly quota as:

- Limited/development tier: `600 + 400 × active ads`
- Full tier: `190,000 + 400 × active ads − 0.001 × user errors`

MarketAI must read the response headers instead of assuming the formula guarantees capacity. In particular, parse `X-Ad-Account-Usage`, `X-Business-Use-Case`, and `X-FB-Ads-Insights-Throttle`; the latter includes `app_id_util_pct`, `acc_id_util_pct`, and `ads_api_access_tier`. Back off on throttling and use asynchronous Insights reports for queries that exceed the synchronous workflow's practical limits. [Marketing API rate limiting](https://developers.facebook.com/docs/marketing-api/overview/rate-limiting/), [Insights best practices](https://developers.facebook.com/docs/marketing-api/insights/best-practices/)

No capacity claim is made for MarketAI's eventual customer count. Measure real headers and query shapes during role testing, then set sync concurrency from observed limits.

## 9. Remaining decisions and evidence to capture

1. **Is `business_management` necessary?** Prove the exact account-discovery path. If the customer-selected assets available through Facebook Login for Business plus `ads_read` are sufficient, omit `business_management` and its separate review burden.
2. **Which supported Graph API version will launch?** Pin it in code and add an upgrade owner/date; do not copy a version from this research note.
3. **What does the live Business Verification flow request?** Capture only a redacted checklist/status, never identity documents, in the project issue.
4. **What does App Dashboard require for the Marketing API Access Tier feature at submission time?** Capture the feature row and required evidence when the usage threshold is met.
5. **How will token revocation surface?** Record the exact Graph error/subcode observed when the role-test customer removes the connected app and turn it into a deterministic adapter test.

## 10. Recommended implementation sequence

```text
Phase 1: Build and role-test
├── Create Business-type Meta app and separate provider business portfolio
├── Add Marketing API and configure Facebook Login for Business
├── Implement BISU code exchange and Workspace CredentialVersion storage
├── Implement granted-account discovery/selection and prove minimum permissions
├── Implement Insights read/reconcile, connection health, and throttling
├── Exercise controlled Mock end to end
├── Test BISU with an app-role user who has full control of the client business
└── Accumulate qualifying successful calls while keeping errors below the current threshold

Phase 2: Verify provider business
├── Connect the app to MarketAI's provider business
├── Complete the live Business Manager verification flow
└── Gate progress on actual verified status, not an estimated date

Phase 3: Request reviews
├── Request Advanced Access for ads_read
├── Request Advanced Access for business_management only if proven necessary
├── Submit distinct descriptions and current permission-specific recordings
├── Request/upgrade the separate Marketing API Access Tier feature
└── Gate launch on the actual permission and feature statuses

Phase 4: Launch and operate
├── Enable self-service connections for non-role customers
├── Monitor permission/tier status, headers, sync health, and revocations
├── Re-authorize by creating a new CredentialVersion when needed
└── Complete any ongoing reviews or data-use checks shown in App Dashboard
```

## Primary references

- [Marketing API authorization](https://developers.facebook.com/docs/marketing-api/get-started/authorization)
- [Marketing API Access Tier update](https://developers.meta.com/blog/updates-to-ads-management-standard-access-feature/)
- [Marketing API rate limiting](https://developers.facebook.com/docs/marketing-api/overview/rate-limiting/)
- [Graph API access levels](https://developers.facebook.com/docs/graph-api/overview/access-levels/)
- [Permissions reference](https://developers.facebook.com/docs/permissions/)
- [App Review submission guide](https://developers.facebook.com/docs/resp-plat-initiatives/individual-processes/app-review/submission-guide)
- [Business Verification](https://developers.facebook.com/docs/development/release/business-verification/)
- [Facebook Login for Business](https://developers.facebook.com/documentation/facebook-login/facebook-login-for-business)
- [Marketing API Sandbox](https://developers.facebook.com/blog/post/2023/06/21/marketing-api-sandbox-capability-now-re-enabled/)
- [Ads Insights API](https://developers.facebook.com/documentation/ads-commerce/marketing-api/insights)
- [Insights best practices](https://developers.facebook.com/docs/marketing-api/insights/best-practices/)
