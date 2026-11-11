import fs from 'node:fs';
import { test, expect } from './fixtures/index';
import {
  advanceManifest,
  approveLocalizedAssetVersionThroughAssets,
  confirmLocalizationBatchThroughUi,
  createLocalizationBatchThroughUi,
  prepareLocalizationBatchThroughUi,
  seedExperimentMetrics,
  seedLocalizationInfrastructure,
  seedScaleInfrastructure,
} from './domain-helpers';
import { en } from '../support/copy';
import type {
  EntityKind,
  EntityRef,
  LocalizationOutcomesEvidence,
  ScaleEvidence,
  ScaleEnrollmentEvidence,
} from './artifacts/schemas';

function scaleEntityRef<K extends EntityKind>(
  kind: K,
  runtimeId: string,
  observedVia: 'dom' | 'same_origin_response'
): EntityRef<K> {
  return {
    kind,
    runtimeId: runtimeId as EntityRef<K>['runtimeId'],
    observedVia,
  };
}

function readMailSinkToken(): string {
  const direct = process.env.MAIL_SINK_TOKEN?.trim();
  if (direct) return direct;
  const tokenFile =
    process.env.MAIL_SINK_TOKEN_FILE || '/run/secrets/mail_sink_token';
  return fs.readFileSync(tokenFile, 'utf8').trim();
}

test('v9 Beta golden path', async ({
  provisioning,
  onboarding,
  workspaceSetup,
  serviceControl,
  metaScenarios,
  evidenceInspector: evidence,
  runtimeSafety: safety,
  evidenceRecorder: artifacts,
  library,
  generation,
  assets,
  experiments,
  localization,
  scale,
}) => {
  const namespace = `gp-${Date.now()}`;

  // ── Step 0 — Safety, Provisioning, Owner, Member, Emptiness ──

  await test.step('Step 0.1 — Validate topology and safety', async () => {
    await safety.validateTopologyAttestation();
    await safety.probeNoEgress(namespace);
  });

  const { result: provisioningResult, ownerInvitation } =
    await test.step('Step 0.2 — Begin provisioning', async () => {
      return provisioning.begin({
        idempotencyKey: `gp-${namespace}`,
        workspaceName: 'Golden Path Workspace',
        ownerEmail: 'owner@golden-path.test',
        operatorIdentifier: 'golden-path-operator',
        betaPlanVersionId: 'beta-v1',
        namespace,
      });
    });

  const { provisioningRef, workspaceRef, workspaceSlug } = provisioningResult;

  // Area fixtures are created before provisioning, while the real workspace
  // slug is allocated by the provisioning response. Bind it now so every
  // subsequent driver targets the enrolled workspace rather than `/w//`.
  generation.setWorkspaceSlug(workspaceSlug);
  assets.setWorkspaceSlug(workspaceSlug);
  experiments.setWorkspaceSlug(workspaceSlug);
  localization.setWorkspaceSlug(workspaceSlug);
  scale.setWorkspaceSlug(workspaceSlug);
  metaScenarios.setWorkspaceSlug(workspaceSlug);

  const ownerSession = await test.step('Step 0.3 — Enroll Owner', async () => {
    return onboarding.enrollOwner(ownerInvitation);
  });

  await test.step('Step 0.4 — Complete provisioning', async () => {
    return provisioning.complete({ provisioningRef });
  });

  const memberInvitation =
    await test.step('Step 0.5 — Create Member invitation', async () => {
      const page = ownerSession.page();
      await page.goto(`/w/${workspaceSlug}/members`);
      await page.getByRole('button', { name: /invite/i }).click();
      const invitationDialog = page.getByRole('dialog');
      await expect(
        invitationDialog.getByRole('heading', { name: /invite new member/i })
      ).toBeVisible();
      await invitationDialog
        .getByPlaceholder('teammate@example.com')
        .fill('member@golden-path.test');
      await invitationDialog.getByRole('button', { name: /^invite$/i }).click();

      const mailSinkUrl = process.env.MAIL_SINK_URL || 'http://mail-sink:8025';
      const mailSinkToken = readMailSinkToken();
      return onboarding.captureInvitation(mailSinkUrl, mailSinkToken, {
        recipientEmail: 'member@golden-path.test',
        timeout: 30_000,
      });
    });

  await test.step('Step 0.6 — Enroll Member and verify restrictions', async () => {
    const memberSession = await onboarding.enrollMember(memberInvitation);
    const memberPage = memberSession.page();

    // The Member sees exactly this Workspace. A Member invitation signs in to
    // the Workspace's marketing tests page; the slug is an identifier (URL and the
    // selector's value), and the selector lists only the Member's Workspaces.
    await memberPage.waitForURL(`**/w/${workspaceSlug}/experiments`);
    const workspaceSelector = memberPage.getByLabel('Switch Workspace');
    await expect(workspaceSelector).toHaveValue(workspaceSlug);
    await expect(workspaceSelector.locator('option')).toHaveText([
      'Golden Path Workspace',
    ]);

    // Settings is every member's (#126): the Member reads the spending
    // limits provisioning created when the snapshot was delivered -- the
    // contract's Beta defaults, through the real API -- and cannot change
    // them; none of the Owner's sections render.
    const settings = await memberPage.goto(`/w/${workspaceSlug}/settings`);
    expect(settings?.status()).toBe(200);
    const usd = (amount: number) =>
      new Intl.NumberFormat('en', {
        style: 'currency',
        currency: 'USD',
        minimumFractionDigits: 0,
        maximumFractionDigits: 4,
      }).format(amount);
    const betaDefaults = {
      per_operation_limit: 5,
      rolling_24h_limit: 20,
      period_limit: 100,
      outstanding_exposure_limit: 10,
    };
    for (const [field, amount] of Object.entries(betaDefaults)) {
      await expect(
        memberPage.getByTestId(`spend-guardrail-value-${field}`)
      ).toHaveText(usd(amount));
    }
    await expect(
      memberPage.getByTestId('spend-guardrail-owner-only')
    ).toHaveText(en('settings', 'spend-guardrail.owner-only'));
    await expect(memberPage.getByTestId('spend-guardrail-edit')).toHaveCount(0);
    await expect(
      memberPage.getByTestId('provider-credentials-section')
    ).toHaveCount(0);

    // Members stays Owner-only. getWorkspacePageProps answers 200 with
    // owner-only error props and WorkspaceLayout renders ForbiddenState, as
    // e2e/workspace/owner-member.spec.ts establishes.
    const members = await memberPage.goto(`/w/${workspaceSlug}/members`);
    expect(members?.status()).toBe(200);
    await expect(
      memberPage.getByRole('heading', { name: 'Access Denied' })
    ).toBeVisible();
    await expect(
      memberPage.getByText('This page is only accessible to Workspace Owners.')
    ).toBeVisible();
    await memberSession.close();
  });

  await test.step('Step 0.7 — Close temporary Owner context', async () => {
    await ownerSession.close();
  });

  const session =
    await test.step('Step 0.8 — Create main Owner context', async () => {
      return ownerSession.createFreshContext();
    });

  await test.step('Step 0.9 — Create Meta Sandbox connection', async () => {
    await workspaceSetup.createMetaSandboxConnection(session, {
      mode: 'customer_sandbox',
    });
  });

  await test.step('Step 0.10 — Verify Meta provenance', async () => {
    await evidence.assertWorkspaceMetaProvenance(
      workspaceRef,
      'customer_sandbox'
    );
  });

  await test.step('Step 0.11 — Verify zero business entities', async () => {
    await evidence.assertZeroBusinessEntities(workspaceRef);
  });

  // ── Step 1 — Creative Library ──

  const libraryEv = await test.step('Step 1 — Creative Library', async () => {
    return library.setup(session, {
      workspaceSlug,
      productName: 'Golden Path Product',
      offerName: 'Golden Path Offer',
      personaName: 'Golden Path Persona',
      templateName: 'Golden Path Template',
      promptPolicyName: 'Golden Path Policy',
      dataSourceName: 'Golden Path DataSource',
      referenceImagePath: '/e2e-assets/reference-product.png',
      markets: ['US', 'DE', 'CA', 'JP'],
    });
  });

  // ── Step 2 — Generation Submission ──

  await test.step('Step 2.1 — Stop Worker', async () => {
    await serviceControl.stop('worker');
  });

  const submitted =
    await test.step('Step 2.2 — Submit generation', async () => {
      return generation.submit(session, {
        productEntry: libraryEv.product,
        productVersion: libraryEv.productVersion,
        templateVersion: libraryEv.templateVersion,
        policyVersion: libraryEv.promptPolicyVersion,
        dataSourceVersion: libraryEv.dataSourceVersion,
      });
    });

  // ── Step 3 — Worker completion ──

  // The current controlled-mock runtime performs generation in-process and
  // does not expose the retired sidecar barrier/checkpoint protocol. Start
  // the worker and observe its durable run projection directly instead of
  // waiting on an endpoint the runtime cannot produce.
  await test.step('Step 3.1 — Start Worker', async () => {
    await serviceControl.start('worker');
  });

  const completed =
    await test.step('Step 3.2 — Observe completion', async () => {
      return generation.observeCompletion(session, {
        run: submitted.run,
        expectedItemCount: submitted.expectedItemCount,
      });
    });

  await test.step('Step 3.3 — Completion assertions', async () => {
    expect(completed.assetVersions).toHaveLength(submitted.expectedItemCount);
  });

  await test.step('Step 3.11 — Re-probe config after Worker restart', async () => {
    await safety.probeNoEgress(namespace);
  });

  // ── Step 4 — Assets Review ──

  const assetsEv =
    await test.step('Step 4 — Assets review and export', async () => {
      return assets.reviewAndExport(session, {
        approvedAssets: completed.assetVersions.slice(0, 2),
        needsChangesAsset: completed.assetVersions[2],
        rejectedAsset: completed.assetVersions[3],
        skipAsset: completed.assetVersions[4],
      });
    });

  // ── Step 5 — Experiment Freeze and PAUSED Launch ──

  const frozen =
    await test.step('Step 5.1 — Create and freeze experiment', async () => {
      return experiments.createAndFreeze(session, {
        approvedAssets: assetsEv.approvedRefs,
        reApprovedAsset: assetsEv.reApprovedRef,
        productVersion: libraryEv.productVersion,
        offerVersion: libraryEv.offerVersion,
      });
    });

  await test.step('Step 5.2 — Launch PAUSED', async () => {
    return experiments.launchPaused(session, {
      experiment: frozen.experiment,
    });
  });

  // ── Step 6 — Metrics, Judgment, Winner ──

  await test.step('Step 6.1 — Insufficient metrics → inconclusive', async () => {
    await metaScenarios.advanceMetaObservation(session, namespace, {
      scenario: 'insufficient_metrics',
      experiment: frozen.experiment,
    });
    seedExperimentMetrics(
      workspaceRef,
      frozen.experiment.runtimeId,
      ownerInvitation.email,
      {
        days: 3,
        candidateMetrics: frozen.candidateRefs.map((candidate) => ({
          candidateId: candidate.runtimeId,
          dailyImpressions: 0,
          dailyClicks: 0,
          dailyPurchases: 0,
          dailySpend: '0.00',
        })),
      }
    );
    await experiments.recordInconclusive(session, {
      experiment: frozen.experiment,
    });
    await evidence.assertNoWinnerDecision(
      workspaceRef,
      frozen.experiment.runtimeId
    );
  });

  const winner =
    await test.step('Step 6.2 — Decisive metrics → confirm Winner', async () => {
      await metaScenarios.advanceMetaObservation(session, namespace, {
        scenario: 'decisive_winner',
        experiment: frozen.experiment,
        candidateRefs: frozen.candidateRefs,
      });
      seedExperimentMetrics(
        workspaceRef,
        frozen.experiment.runtimeId,
        ownerInvitation.email,
        {
          days: 14,
          dayOffset: 3,
          candidateMetrics: frozen.candidateRefs.map((candidate, index) => ({
            candidateId: candidate.runtimeId,
            dailyImpressions: index === 0 ? 5000 : 800,
            dailyClicks: index === 0 ? 500 : 40,
            dailyPurchases: index === 0 ? 50 : 5,
            dailySpend: '50.00',
          })),
        }
      );
      return experiments.confirmWinner(session, {
        experiment: frozen.experiment,
      });
    });

  // ── Step 7 — Localization ──

  let localizationInfra: ReturnType<typeof seedLocalizationInfrastructure>;
  const localizationBatchId =
    await test.step('Step 7.1 — Seed and create localization batch', async () => {
      localizationInfra = seedLocalizationInfrastructure(
        workspaceRef,
        winner.winnerAssetVersion.runtimeId,
        ownerInvitation.email
      );
      const page = session.page();
      const targetMarkets = localizationInfra.markets.map(
        (market) => market.market
      );
      const batchId = await createLocalizationBatchThroughUi(
        page,
        workspaceSlug,
        winner.winnerAssetVersion.runtimeId,
        targetMarkets
      );
      await prepareLocalizationBatchThroughUi(
        page,
        workspaceSlug,
        batchId,
        localizationInfra.truthPolicyVersionId,
        localizationInfra.inventoryId,
        targetMarkets.length
      );
      const confirmed = await confirmLocalizationBatchThroughUi(
        page,
        workspaceSlug,
        batchId
      );
      expect(confirmed.item_ids.length).toBeGreaterThanOrEqual(1);
      return batchId;
    });

  const localizationEv =
    await test.step('Step 7.2 — Observe outcomes and retry locked-copy item', async (): Promise<LocalizationOutcomesEvidence> => {
      const page = session.page();
      const batchUrl =
        `/api/domain/v1/w/${workspaceSlug}/localization/batches/` +
        `${localizationBatchId}`;
      const readBatch = async () => {
        const response = await page.request.get(batchUrl);
        expect(response.ok()).toBe(true);
        return (await response.json()) as {
          items?: Array<{
            id: string;
            state: string;
            target_market: string;
            attempt_count: number;
          }>;
        };
      };
      await expect
        .poll(
          async () => {
            const snapshot = await readBatch();
            const states = snapshot.items?.map((item) => item.state) ?? [];
            return states.length > 0 &&
              states.every((state) =>
                ['succeeded', 'failed', 'skipped', 'retry_wait'].includes(state)
              )
              ? 'settled'
              : 'running';
          },
          { timeout: 120_000, intervals: [3000] }
        )
        .toBe('settled');

      const settled = await readBatch();
      const retryMarket = localizationInfra.markets.find(
        (market) => market.failsLockedCopyQc
      );
      expect(retryMarket).toBeDefined();
      let retryItem = settled.items?.find(
        (item) => item.target_market === retryMarket!.market
      );
      expect(retryItem?.state).toBe('retry_wait');
      const itemUrl =
        `/api/domain/v1/w/${workspaceSlug}/localization/batches/` +
        `${localizationBatchId}/items/${retryItem!.id}`;
      const itemAttempt = async () => {
        const response = await page.request.get(itemUrl);
        expect(response.ok()).toBe(true);
        const item = (await response.json()) as {
          state: string;
          attempt_count: number;
        };
        return `${item.state}:${item.attempt_count}`;
      };

      for (const expected of ['retry_wait:2', 'failed:3']) {
        await page.goto(
          `/w/${workspaceSlug}/localization?batch=${localizationBatchId}&item=${retryItem!.id}`
        );
        const retryButton = page.getByRole('button', { name: 'Retry item' });
        await expect(retryButton).toBeEnabled({ timeout: 15_000 });
        const retryResponse = page.waitForResponse(
          (response) =>
            response.url().includes('/retry') &&
            response.request().method() === 'POST' &&
            (response.status() === 200 || response.status() === 201)
        );
        await retryButton.click();
        await retryResponse;
        await expect
          .poll(itemAttempt, {
            timeout: 90_000,
            intervals: [250, 500, 1000, 2000, 5000],
          })
          .toBe(expected);
        const refreshed = await page.request.get(itemUrl);
        retryItem = (await refreshed.json()) as typeof retryItem;
      }

      const finalSnapshot = await readBatch();
      const winnerMarket = localizationInfra.markets.find(
        (market) => market.isWinnerMarket
      );
      expect(
        finalSnapshot.items?.find(
          (item) => item.target_market === winnerMarket?.market
        )?.state
      ).toBe('succeeded');

      const itemDetails = await Promise.all(
        (finalSnapshot.items ?? []).map(async (item) => {
          const response = await page.request.get(
            `/api/domain/v1/w/${workspaceSlug}/localization/batches/` +
              `${localizationBatchId}/items/${item.id}`
          );
          return (await response.json()) as {
            lineage?: { localized_asset_version_id?: string | null };
          };
        })
      );
      const localizedAssetVersions = itemDetails.flatMap((item) => {
        const id = item.lineage?.localized_asset_version_id;
        return id
          ? [
              {
                kind: 'asset_version' as const,
                runtimeId:
                  id as LocalizationOutcomesEvidence['localizedAssetVersions'][number]['runtimeId'],
                observedVia: 'same_origin_response' as const,
              },
            ]
          : [];
      });
      return {
        schemaVersion: 1,
        area: 'localization',
        claims: [
          { code: 'localization_outcomes_verified', status: 'passed' },
          { code: 'localization_retry_succeeded', status: 'passed' },
        ],
        batch: {
          kind: 'localization_batch',
          runtimeId:
            localizationBatchId as LocalizationOutcomesEvidence['batch']['runtimeId'],
          observedVia: 'same_origin_response',
        },
        retriedMarket: retryMarket!.market,
        retriedAttemptCount: 3,
        localizedAssetVersions,
      };
    });

  await test.step('Step 7.3 — Approve the Winner-market localized asset in Assets', async () => {
    // The binding the Winner-market item published waits for review; the
    // exact-version approval in Asset Detail makes it experiment-ready
    // (#128), which the Scale lineage gate requires. No seed approves it.
    const winnerMarket = localizationInfra.markets.find(
      (market) => market.isWinnerMarket
    );
    expect(winnerMarket).toBeDefined();
    await approveLocalizedAssetVersionThroughAssets(
      session.page(),
      workspaceSlug,
      localizationBatchId,
      winnerMarket!.market
    );
  });

  // ── Step 8 — Scale ──

  const scaleInfra =
    await test.step('Step 8.0 — Seed Scale infrastructure', async () => {
      const seeded = seedScaleInfrastructure(
        workspaceRef,
        winner.winnerDecision.runtimeId,
        ownerInvitation.email
      );
      expect(seeded.deliveryTargetId).toBeTruthy();
      expect(seeded.policyVersionId).toBeTruthy();
      expect(seeded.marketCode).toBe('US');
      return seeded;
    });

  const enrollmentEv =
    await test.step('Step 8.1 — Create Scale enrollment', async () => {
      const page = session.page();
      await page.goto(`/w/${workspaceSlug}/scale`);
      await expect(
        page.getByRole('heading', { name: 'Scale', exact: true })
      ).toBeVisible({ timeout: 15_000 });
      await page
        .getByRole('button', { name: 'New Enrollment', exact: true })
        .first()
        .click();
      await page
        .getByRole('button', { name: winner.winnerDecision.runtimeId })
        .click();
      const deliveryTargetInput = page.getByLabel(/delivery.*target/i);
      await expect(deliveryTargetInput).toBeVisible({ timeout: 5_000 });
      await deliveryTargetInput.fill(scaleInfra.deliveryTargetId);

      const enrollResponsePromise = page.waitForResponse(
        (response) =>
          response.url().includes('/enrollments') &&
          response.request().method() === 'POST' &&
          (response.status() === 200 || response.status() === 201),
        { timeout: 15_000 }
      );
      await page.getByRole('button', { name: /^Create$/i }).click();
      const enrollment = (await (await enrollResponsePromise).json()) as {
        id: string;
        lifecycle: string;
      };
      expect(enrollment.lifecycle).toBe('draft');
      return {
        schemaVersion: 1,
        area: 'scale',
        claims: [
          { code: 'scale_enrollment_created' as const, status: 'passed' },
        ],
        enrollment: scaleEntityRef(
          'scale_enrollment',
          enrollment.id,
          'same_origin_response'
        ),
      } satisfies ScaleEnrollmentEvidence;
    });

  const enrollmentId = enrollmentEv.enrollment.runtimeId;
  let episodeId = '';

  await test.step('Step 8.2 — Activate Scale enrollment', async () => {
    const page = session.page();
    await page.goto(`/w/${workspaceSlug}/scale/${enrollmentId}`);
    await expect(
      page.getByRole('heading', { name: enrollmentId, exact: true })
    ).toBeVisible({ timeout: 10_000 });
    await page.getByRole('button', { name: 'Activate', exact: true }).click();
    const activateDialog = page.getByRole('dialog', {
      name: 'Activate enrollment',
    });
    await expect(activateDialog).toBeVisible({ timeout: 10_000 });

    const previewResponsePromise = page.waitForResponse(
      (response) =>
        response.url().includes('/activation-previews') &&
        response.request().method() === 'POST' &&
        response.status() === 201,
      { timeout: 15_000 }
    );
    await activateDialog
      .getByRole('button', { name: 'Create preview' })
      .click();
    const preview = (await (await previewResponsePromise).json()) as {
      id: string;
    };
    expect(preview.id).toBeTruthy();

    const activateResponsePromise = page.waitForResponse(
      (response) =>
        response.url().includes('/activate') &&
        response.request().method() === 'PUT' &&
        response.status() === 200,
      { timeout: 15_000 }
    );
    await activateDialog
      .getByRole('button', { name: 'Activate', exact: true })
      .click();
    const activated = (await (await activateResponsePromise).json()) as {
      lifecycle: string;
      current_episode_id: string;
    };
    expect(activated.lifecycle).toBe('active');
    episodeId = activated.current_episode_id;
  });

  await test.step('Step 8.3 — Advance Scale evidence and evaluate', async () => {
    const healthy = {
      spend: '30',
      impressions: 3000,
      clicks: 150,
      purchases: 5,
      purchase_value: '200',
      reach: 1500,
      frequency: '1.4',
    };
    const fatigued = {
      spend: '30',
      impressions: 6000,
      clicks: 60,
      purchases: 1,
      purchase_value: '40',
      reach: 1200,
      frequency: '5.0',
    };

    let advanced: ReturnType<typeof advanceManifest> | undefined;
    for (let day = 1; day <= 24; day += 1) {
      advanced = advanceManifest({
        workspaceId: workspaceRef,
        workspaceSlug,
        ownerUserId: ownerInvitation.email,
        authorizationEpoch: 0,
        metaMode: 'customer_sandbox',
        enrollmentId,
        episodeId,
        policyVersionId: scaleInfra.policyVersionId,
        dayIndex: day,
        kind: 'complete',
        metrics: day <= 14 ? healthy : fatigued,
        baselineMetrics: healthy,
      });
      if (day === 14) {
        expect(advanced.maturity).toBe('economics_ready');
        expect(advanced.health).toBe('healthy');
      }
      if (advanced.adviceThreadIds.length > 0) break;
    }
    expect(advanced?.health).toBe('creative_fatigue');
    expect(advanced?.adviceThreadIds.length).toBeGreaterThanOrEqual(1);
  });

  const scaleEv =
    await test.step('Step 8.4 — Observe advice and convert draft', async () => {
      const page = session.page();
      const adviceUrl =
        `/api/domain/v1/w/${workspaceSlug}/scale/customer_sandbox/` +
        `advice-threads?enrollment_id=${encodeURIComponent(enrollmentId)}`;
      const adviceResponse = await page.request.get(adviceUrl);
      expect(adviceResponse.ok()).toBe(true);
      const advicePayload = (await adviceResponse.json()) as {
        items?: Array<{ id: string; advice_type: string }>;
      };
      const adviceThread = advicePayload.items?.find(
        (thread) => thread.advice_type === 'refresh_creative'
      );
      expect(adviceThread?.id).toBeTruthy();

      await page.goto(`/w/${workspaceSlug}/scale/${enrollmentId}`);
      await expect(
        page.getByRole('heading', { name: enrollmentId, exact: true })
      ).toBeVisible({ timeout: 15_000 });
      const draftResponsePromise = page.waitForResponse(
        (response) =>
          (response.url().includes('/convert') ||
            response.url().includes('/generate-draft')) &&
          response.request().method() === 'POST' &&
          (response.status() === 200 || response.status() === 201),
        { timeout: 15_000 }
      );
      await page
        .getByRole('button', { name: /^Convert$/i })
        .first()
        .click();
      const draft = (await (await draftResponsePromise).json()) as {
        generation_draft_id?: string | null;
        draft_id?: string | null;
      };
      const draftId = draft.generation_draft_id ?? draft.draft_id ?? '';
      expect(draftId).toBeTruthy();

      return {
        schemaVersion: 1,
        area: 'scale',
        claims: [
          { code: 'scale_advice_received' as const, status: 'passed' },
          { code: 'scale_draft_created' as const, status: 'passed' },
        ],
        enrollment: enrollmentEv.enrollment,
        adviceThread: scaleEntityRef(
          'advice_thread',
          adviceThread!.id,
          'same_origin_response'
        ),
        generationDraft: scaleEntityRef(
          'generation_draft',
          draftId,
          'same_origin_response'
        ),
      } satisfies ScaleEvidence;
    });

  await test.step('Step 8.5-8.6 — Scale evidence assertions', async () => {
    await evidence.assertZeroMetaWrites(workspaceRef);
  });

  // ── Step 9 — Pre-Restart Canonical Snapshot ──

  await test.step('Step 9.1 — Quiescence', async () => {
    await evidence.assertNoRunnableOrInFlightJobs(workspaceRef);
    await evidence.assertOperationsSettled(workspaceRef);
  });

  const preRestartSnapshot =
    await test.step('Step 9.2 — Capture snapshot', async () => {
      return evidence.captureCanonicalSnapshot(workspaceRef);
    });

  // ── Step 10 — Restart and Final Verification ──

  for (const svc of ['web', 'api', 'worker'] as const) {
    await test.step(`Step 10.1 — Restart ${svc}`, async () => {
      await serviceControl.restart(svc);
    });
    await test.step(`Step 10.2 — Verify ${svc} network`, async () => {
      await safety.validateRestartedServiceNetwork(namespace, svc);
    });
  }

  await test.step('Step 10.3 — Re-probe config', async () => {
    await safety.probeNoEgress(namespace);
  });

  const freshSession =
    await test.step('Step 10.4 — Fresh Owner context', async () => {
      return session.createFreshContext();
    });

  await test.step('Step 10.5 — Verify all six areas', async () => {
    await library.verifyPersistedState(freshSession, libraryEv);
    await generation.verifyPersistedState(freshSession, completed);
    await assets.verifyPersistedState(freshSession, assetsEv);
    await experiments.verifyPersistedState(freshSession, winner);
    await localization.verifyPersistedState(freshSession, localizationEv);
    await scale.verifyPersistedState(freshSession, scaleEv);
  });

  await test.step('Step 10.6 — Post-restart quiescence', async () => {
    await evidence.assertNoRunnableOrInFlightJobs(workspaceRef);
    await evidence.assertOperationsSettled(workspaceRef);
  });

  const postRestartSnapshot =
    await test.step('Step 10.7 — Post-restart snapshot', async () => {
      return evidence.captureCanonicalSnapshot(workspaceRef);
    });

  await test.step('Step 10.8 — Canonical snapshot equality', async () => {
    const equal = evidence.compareSnapshots(
      preRestartSnapshot,
      postRestartSnapshot
    );
    expect(equal).toBe(true);
  });

  await test.step('Step 10.9 — Final three-layer assertions', async () => {
    await safety.assertNetworkSpyClean();
    await evidence.assertZeroMetaWrites(workspaceRef);
    await evidence.assertNonVacuousMetaEvidence(workspaceRef, {
      expectedProvenance: 'customer_sandbox',
      expectedScenarios: [
        'insufficient_metrics',
        'decisive_winner',
        'scale_warming',
        'scale_declining',
      ],
      frozenCandidateCount: frozen.candidateRefs.length,
    });
    await evidence.assertWorkspaceMetaProvenance(
      workspaceRef,
      'customer_sandbox'
    );
    await evidence.assertCallEvidenceModeConsistency(workspaceRef);
  });

  await test.step('Step 10.10 — Mark complete', async () => {
    const completion = artifacts.recordStepStart(10, 'complete');
    completion.pass();
    artifacts.markPassed();
  });
});
