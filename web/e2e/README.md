# E2E suites

Playwright suites for the control plane and the six areas. Only the
`golden-path` project runs on pull requests (`ci.yml` `golden-path-e2e`, and
`golden-path-pr.yml` for changes under `e2e/golden-path/`). The other suites
run when someone runs them, locally or through the manual workflow below; on a
pull request they get only `npm run check-types:e2e` and the copy guard
(below).

## Suites

| Suite                                          | Backend                                            | How to run                                                   |
| ---------------------------------------------- | -------------------------------------------------- | ------------------------------------------------------------ |
| `golden-path/full-journey`, `products`, `i18n` | Compose stack                                      | `npx playwright test --project golden-path --workers 1` (CI) |
| `localization`                                 | Compose stack and its Worker                       | `npm run test:e2e:localization` (below)                      |
| `scale`                                        | Compose stack, its API and Worker                  | `npm run test:e2e:scale` (below)                             |
| `experiments`                                  | Compose stack, its API and Worker                  | `npm run test:e2e:experiments` (below)                       |
| `library`, `workspace`                         | Compose stack                                      | `chromium` project                                           |
| `competitors`                                  | `page.route` doubles; three specs need a real API¹ | `chromium` project                                           |
| `assets`, `generation`                         | `page.route` doubles and a signed-in page          | `chromium` project                                           |
| `onboarding`                                   | Compose stack                                      | `onboarding` project                                         |

¹ With `C31_REAL_BACKEND` or `C32_REAL_BACKEND` set.

Without `--project`, the `chromium`, `tablet-chromium` and `mobile-chromium`
projects each run every spec, so a real-backend suite seeds three times. The
runners pin `--project=chromium --workers=1`.

## Golden-path seed exceptions

The golden path goes through the product's own pages and APIs, except where
no product entry exists yet. For those, `golden-path/fixtures.ts` and
`golden-path/golden_path_seed.py` seed the data directly. This is the list of
what they still seed ([#63](https://github.com/heart-cannot-beat/marketai/issues/63)).
When a ticket ships the product entry, it removes the seed and crosses its
item off here.

- The provisioned Workspace's SpendGuardrail (`fixtures.ts`), until
  provisioning creates it with the contract defaults
  ([#126](https://github.com/heart-cannot-beat/marketai/issues/126)).
- ~~Review approval of the Winner-market MarketAssetBinding~~: removed by
  [#128](https://github.com/heart-cannot-beat/marketai/issues/128). The
  journey approves the localized AssetVersion in the Assets area, and that
  review moves the binding to `ready_for_experiment`.
- ~~The published ScalePolicyVersion~~: removed by
  [#127](https://github.com/heart-cannot-beat/marketai/issues/127). A
  migration publishes the Beta default Policy, and the golden path and the
  Scale suite evaluate under it.
- ~~The curated-scenario DeliveryTarget, with its delivery_configuration
  contract generation and contract set~~: removed by
  [#129](https://github.com/heart-cannot-beat/marketai/issues/129). In New
  Enrollment the journey picks the Winner's observed sandbox ad, and the
  server makes the DeliveryTarget and the connection's Scale contract set.
- ~~The landing_page LibraryVersion keyed by the Winner's frozen
  `landing_page_evidence_hash`~~: removed by
  [#129](https://github.com/heart-cannot-beat/marketai/issues/129). The
  lineage gate compares the frozen hash itself.
- The attestation of the picked DeliveryTarget's delivery_configuration
  generation, which activation needs (`seed-scale-activation-evidence`). The
  sandbox's scheduled read publishes one only at the connection's next daily
  slot, so the journey cannot wait for it
  ([#129](https://github.com/heart-cannot-beat/marketai/issues/129)).

## Localization, Scale and Experiments on a local stack

1. Start a disposable Compose stack from the repository root:

   ```bash
   docker compose -p marketai-e2e --env-file deploy/docker/.env.compose up -d --build
   scripts/compose-wait.sh 180 marketai-e2e
   ```

   It publishes 5432, 4002, 8787 and 9000. If 5432 is taken, publish
   Postgres elsewhere with an override file (`ports: !override`) and export
   `COMPOSE_FILE` with both files, so the suites' own `docker compose -p …`
   calls see the same stack. The seeds refuse any database host other than
   localhost or the Compose `postgres` service.

2. Copy `web/.env.e2e.localization.example` to `web/.env.e2e.localization`,
   `web/.env.e2e.scale.example` to `web/.env.e2e.scale` and
   `web/.env.e2e.experiments.example` to `web/.env.e2e.experiments`. Take
   the database URLs and `MARKETAI_TENANT_CONTEXT_SECRET` from
   `deploy/docker/.env.compose` (with `localhost` as the host), set every
   `*_COMPOSE_PROJECT` value to `marketai-e2e`, and point `E2E_PYTHON` at a
   Python with `requirements.txt` installed.

3. From `web/`, sign up the test identities before each run of the suites:

   ```bash
   E2E_IDENTITIES_ALLOW_MUTATIONS=1 npx playwright test --project identities
   ```

   It needs the provisioning variables `ci.yml`'s `golden-path-e2e` job sets
   (`CONTROL_PLANE_DATABASE_URL`, `DOMAIN_OWNER_DATABASE_URL`,
   `E2E_DOMAIN_BASE_URL`, the entitlement token and signing key and
   `MFA_ENCRYPTION_KEY`). It provisions a Workspace, signs its Owner up with
   MFA and a Member through an invitation, then provisions a second
   Workspace for Experiments that the same Owner and Member join through
   their invitations, and a third Workspace with an Owner of its own that
   neither belongs to (the Experiments cross-Workspace checks). It writes
   `e2e/.auth/owner.json`, `e2e/.auth/member.json` and
   `e2e/.auth/identities.env`. The `setup` project cannot sign in an Owner
   with MFA, so once `identities.env` exists the runners load it and pass
   `--no-deps`. Each run needs a fresh Workspace: the Scale seed adds an
   active Meta connection per mode, and a second one in the same Workspace
   fails `uq_meta_connection_active`. Experiments has its own Workspace
   because each of its seeds retires the Workspace's earlier Meta
   connections.

4. Run the suites:

   ```bash
   npm run test:e2e:localization
   npm run test:e2e:scale
   npm run test:e2e:experiments
   ```

   Extra arguments go to Playwright, for example
   `npm run test:e2e:scale -- --retries=0 -g "smoke"` or
   `npm run test:e2e:experiments -- -g "Scenario 6"`. With `CI` unset,
   `playwright.config.ts` starts `npm run dev` on 4002 unless something
   already answers there.

5. Tear the stack down, data included:

   ```bash
   docker compose -p marketai-e2e down -v
   ```

The Localization suite stops and restarts the stack's Worker. The Scale suite
restarts the API and the Worker and drains Jobs with a one-shot Worker; its
seed needs the Worker running, because it waits for the Worker to finish the
Localization batch its fixture confirms. On a local Compose stack the
Localization and Scale suites each finish in about a minute. The Experiments
suite's Launch recovery stops, starts and restarts the Worker and restarts the
API; the Member scenario uses `e2e/.auth/member.json`. It takes about six and
a half minutes, five of them in Scenario 5c, which waits out the Worker's
default 300-second Job lease.

## Manual workflow

`.github/workflows/e2e-suites-manual.yml` runs steps 1–5 on a GitHub runner.
It starts only from **Actions → E2E suites (manual) → Run workflow**, never on
pull requests or a schedule. Choose `localization-and-scale`, `localization`,
`scale` or `experiments`; the job log lists every scenario's result. Retries
are off, so a flaky step shows as a failure.

## Interface copy

Read interface copy from the locale instead of writing it out:

```ts
import { en } from '../support/copy';

await page.getByLabel(en('localization', 'new-batch.source-label'));
await expect(page.getByTestId('export-dedup')).toContainText(
  en('assets', 'manifest-deduplicated', { dedupCount: 1, uniqueCount: 1 })
);
```

`en()` reads `web/locales/en`; a renamed or removed key fails
`npm run check-types:e2e`. `web/__tests__/locales/e2e-copy.spec.ts` (part of
`npm test`) reads every text literal the suites still match on — `getBy*`,
`hasText`, `text=` and `:has-text()` selectors, `toHaveText`,
`toContainText`, `aria-label` — and fails when it is not in the `en` locale
(or `zh-CN`, in a suite that switches to it). Test data and text a component
writes itself go in its `NOT_COPY` list with a reason.
