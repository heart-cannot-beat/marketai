/** @jest-environment jsdom */
import { render, screen, within } from '@testing-library/react';
import {
  MarketingPlanReview,
  planReviewBlocked,
} from '@/components/workspace/MarketingPlanReview';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import fixture from '../../fixtures/marketing-plan.json';
import en from '../../../locales/en/common.json';
import zhCN from '../../../locales/zh-CN/common.json';
import enCompetitors from '../../../locales/en/competitors.json';
import zhCNCompetitors from '../../../locales/zh-CN/competitors.json';

/**
 * #273: the three competitor findings in plan review, worded by the real
 * `competitors` locale files in both interface languages, each pointing at
 * its source observations on the Meta Ad Library.
 */
type Plan = components['schemas']['FrozenTestPlan'];
type Messages = Record<string, Record<string, string>>;

let mockMessages: Messages = { common: en, competitors: enCompetitors };
jest.mock('next-i18next', () => ({
  useTranslation: (ns?: string | string[]) => ({
    i18n: { language: 'en' },
    t: (key: string, options?: Record<string, unknown>) => {
      const namespace =
        (options?.ns as string | undefined) ??
        (Array.isArray(ns) ? ns[0] : ns) ??
        'common';
      const plural =
        options?.count === 1 && `${key}_one` in mockMessages[namespace]
          ? `${key}_one`
          : key;
      return (mockMessages[namespace][plural] ?? `${namespace}:${key}`).replace(
        /\{\{(\w+)\}\}/g,
        (_, name: string) => String(options?.[name] ?? '')
      );
    },
  }),
}));

const LIBRARY = 'https://www.facebook.com/ads/library/?id=1234567890';

const plan: Plan = {
  ...(fixture.plan as unknown as Plan),
  competitor_snapshot_id: 'csnap_1',
  plan_review: [
    ...(fixture.plan.plan_review as Plan['plan_review'] & object),
    {
      id: 'system-competitor-copy-reuse-1',
      kind: 'competitor_copy_reuse',
      severity: 'blocking',
      source: 'system',
      resolved: false,
      target_ids: ['c1_1'],
      finding: 'The headline repeats in-market copy of "Acme Charge".',
      suggestion: 'Rewrite it in your own words.',
      observations: [
        {
          ad_id: 'cad_1',
          advertiser_name: 'Acme Charge',
          library_url: LIBRARY,
          days_running: 1,
        },
      ],
    },
    {
      id: 'system-competitor-crowded-1',
      kind: 'competitor_crowded',
      severity: 'advisory',
      source: 'system',
      resolved: false,
      target_ids: ['a1'],
      finding: '3 advertisers other than your own page make this claim.',
      suggestion: 'Consider a differentiated way of telling.',
      observations: [
        {
          ad_id: 'cad_2',
          cluster_id: 'c1',
          advertiser_name: 'Northpeak',
          library_url: `${LIBRARY}2`,
          days_running: 120,
        },
      ],
    },
    {
      id: 'system-competitor-gap-1',
      kind: 'competitor_gap',
      severity: 'advisory',
      source: 'system',
      resolved: false,
      target_ids: ['a2'],
      finding: 'This may be white space, or others may have tried it.',
      suggestion: 'Treat it as a hypothesis to test.',
    },
  ],
};

const renderReview = () =>
  render(
    <MarketingPlanReview
      plan={plan}
      savedQuestions={[]}
      disabled={false}
      redraftDisabled={false}
      costLabel=""
      messages={() => []}
      onChange={jest.fn()}
      onSave={jest.fn()}
      onRedraft={jest.fn()}
    />
  );

const item = (finding: RegExp) =>
  screen.getByText(finding).closest('li') as HTMLElement;

describe.each([
  ['en', { common: en, competitors: enCompetitors }],
  ['zh-CN', { common: zhCN, competitors: zhCNCompetitors }],
])('competitor findings (%s)', (_, messages: Messages) => {
  beforeEach(() => {
    mockMessages = messages;
  });

  it('words each kind from the competitors namespace and links its observations', () => {
    renderReview();
    const words = messages.competitors;
    const copy = item(/repeats in-market copy/);
    expect(
      within(copy).getByText(words['review-kind-competitor_copy_reuse'])
    ).toBeTruthy();
    expect(
      within(copy).getByText(words['review-system-competitor_copy_reuse'])
    ).toBeTruthy();
    expect(within(copy).getByText(words['review-observations'])).toBeTruthy();
    const link = within(copy).getByRole('link', {
      name: words['review-observation-source'],
    });
    expect(link.getAttribute('href')).toBe(LIBRARY);
    expect(link.getAttribute('rel')).toBe('noopener noreferrer');
    expect(copy.textContent).toContain('Acme Charge');
    expect(copy.textContent).toContain(
      words['review-observation-days_one'].replace('{{count}}', '1')
    );
    expect(copy.className).toContain('border-red-200');

    const crowded = item(/advertisers other than your own page/);
    expect(
      within(crowded).getByText(words['review-kind-competitor_crowded'])
    ).toBeTruthy();
    expect(crowded.textContent).toContain(
      words['review-observation-days'].replace('{{count}}', '120')
    );
    expect(
      within(crowded)
        .getByRole('link', { name: words['review-observation-source'] })
        .getAttribute('href')
    ).toBe(`${LIBRARY}2`);

    const gap = item(/white space/);
    expect(
      within(gap).getByText(words['review-kind-competitor_gap'])
    ).toBeTruthy();
    expect(within(gap).queryByRole('link')).toBeNull();
    // No raw key reaches the page.
    expect(document.body.textContent).not.toMatch(
      /(?:common|competitors):|review-kind-|review-system-/
    );
  });

  it('leaves blocking on reused copy to the server, which recomputes it on confirmation', () => {
    expect(planReviewBlocked(plan)).toBe(false);
  });
});
