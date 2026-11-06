import {
  test,
  expect,
  LOCALIZATION_URL,
  WORKSPACE_SLUG,
  createBatchThroughUi,
  seedLocalizationPrerequisites,
} from './fixtures';

test('Member can run Localization commands but is forbidden from the Owner-only Truth withdrawal gate', async ({
  browser,
}) => {
  const seed = seedLocalizationPrerequisites();
  const readyMarket = seed.markets.find(
    (market) => market.expectedReadiness === 'ready'
  );
  expect(readyMarket).toBeDefined();
  const memberContext = await browser.newContext({
    storageState: 'e2e/.auth/member.json',
  });
  try {
    const memberPage = await memberContext.newPage();
    const workspace = await memberPage.request.get(
      `/api/domain/v1/w/${WORKSPACE_SLUG}/localization/workspace`
    );
    expect(workspace.status()).toBe(200);
    expect((await workspace.json()).role).toBe('member');

    await memberPage.goto(LOCALIZATION_URL);
    await expect(
      memberPage.getByRole('heading', { name: 'Localization control room' })
    ).toBeVisible();
    expect(
      await createBatchThroughUi(memberPage, seed, [readyMarket!])
    ).toMatch(/^lb_/);

    // MFA is checked only at login (ADR 0015): the withdrawal itself is
    // refused on the Member's role, with no step-up grant in between.
    const csrf = (await (await memberPage.request.get('/api/csrf')).json())
      .token as string;
    const denied = await memberPage.request.post(
      `/api/domain/v1/w/${WORKSPACE_SLUG}/library/versions/${seed.truthPolicyVersionId}/withdraw`,
      {
        headers: {
          Origin: new URL(memberPage.url()).origin,
          'X-CSRF-Token': csrf,
          'X-Intent-Id': `localization-member-withdraw-${Math.random().toString(36).slice(2)}`,
        },
        data: { reason: 'Member attempt' },
      }
    );
    expect(denied.status()).toBe(403);
    expect((await denied.json()).code).toBe('library_forbidden');
  } finally {
    await memberContext.close();
  }
});

test('foreign Workspace URL and Localization API are denied server-side', async ({
  page,
}) => {
  const seed = seedLocalizationPrerequisites();
  expect(seed.foreignWorkspaceId).not.toBe(
    process.env.E2E_LOCALIZATION_WORKSPACE_ID
  );
  expect(seed.foreignWorkspaceSlug).not.toBe(WORKSPACE_SLUG);

  // The guarded seed transaction has just upserted this exact Team and its
  // separate foreign owner, while explicitly proving neither test actor has a
  // membership. This is therefore a real cross-Workspace attack, not an
  // unknown-slug 404.
  const response = await page.goto(
    `/w/${seed.foreignWorkspaceSlug}/localization`
  );
  expect(response?.status()).toBe(200);
  await expect(page.getByText('Access Denied')).toBeVisible();
  await expect(
    page.getByRole('heading', { name: 'Localization control room' })
  ).toHaveCount(0);

  const denied = await page.request.get(
    `/api/domain/v1/w/${seed.foreignWorkspaceSlug}/localization/workspace`
  );
  expect(denied.status()).toBe(403);
});
