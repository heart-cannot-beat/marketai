import {
  mergeWorkspaceProducts,
  nextStep,
  productViewHref,
  stepView,
  type ProductSignals,
} from '@/lib/products/workspace-products';
import type { components } from '@/lib/domain-client/generated/domain-v1';

type ProductSummary = components['schemas']['ProductSummary'];
type IdeasProjectSummary = components['schemas']['IdeasProjectSummary'];

const quiet: ProductSignals = {
  needsAttention: false,
  reportMissing: false,
  conclusionUnseen: false,
  keptUntested: 0,
  draftsUnhandled: 0,
  hasIdeas: true,
};

const product = (
  id: string,
  name: string | null,
  updatedAt: string
): ProductSummary => ({
  product_id: id,
  name,
  ready: true,
  updated_at: updatedAt,
});

const ideas = (
  id: string,
  productId: string,
  adCount: number,
  keptCount: number,
  updatedAt: string,
  productName = 'Acme Charge Home'
): IdeasProjectSummary => ({
  id,
  revision: 1,
  state: adCount > 0 ? 'ideas_ready' : 'input',
  product_id: productId,
  product_name: productName,
  ad_count: adCount,
  kept_count: keptCount,
  updated_at: updatedAt,
});

describe('nextStep (#194): the first rule that holds, in the overview order', () => {
  it('puts an error waiting for the user before everything else', () => {
    expect(
      nextStep({
        needsAttention: true,
        reportMissing: true,
        conclusionUnseen: true,
        keptUntested: 4,
        draftsUnhandled: 2,
        hasIdeas: true,
      })
    ).toBe('attention');
  });

  it('then a running test missing its report', () => {
    expect(
      nextStep({
        ...quiet,
        reportMissing: true,
        conclusionUnseen: true,
        keptUntested: 4,
        draftsUnhandled: 2,
      })
    ).toBe('report_missing');
  });

  it('then a result not yet viewed', () => {
    expect(
      nextStep({
        ...quiet,
        conclusionUnseen: true,
        keptUntested: 4,
        draftsUnhandled: 2,
      })
    ).toBe('conclusion_unseen');
  });

  it('then kept ads not yet tested', () => {
    expect(nextStep({ ...quiet, keptUntested: 4, draftsUnhandled: 2 })).toBe(
      'kept_untested'
    );
  });

  it('then drafts not yet handled', () => {
    expect(nextStep({ ...quiet, draftsUnhandled: 2 })).toBe('drafts_unhandled');
  });

  it('falls back to preparing, or to no ideas at all', () => {
    expect(nextStep(quiet)).toBe('preparing');
    expect(nextStep({ ...quiet, hasIdeas: false })).toBe('no_ideas');
  });

  it('sends the report and result steps to their views, the rest to ad ideas', () => {
    expect(stepView('report_missing')).toBe('tests');
    expect(stepView('conclusion_unseen')).toBe('results');
    expect(stepView('kept_untested')).toBe('ideas');
    expect(stepView('no_ideas')).toBe('ideas');
  });
});

describe('mergeWorkspaceProducts (#194): list_products + list_marketing_ideas', () => {
  it('joins by product id and lists the most recently changed first', () => {
    const merged = mergeWorkspaceProducts(
      [
        product('prod_home', 'Acme Charge Home', '2026-11-10T00:00:00Z'),
        product('prod_go', 'Acme Charge Go', '2026-11-12T00:00:00Z'),
      ],
      [ideas('mp_1', 'prod_home', 6, 0, '2026-11-14T00:00:00Z')]
    );
    expect(merged.map((p) => [p.productId, p.step, p.stepCount])).toEqual([
      ['prod_home', 'drafts_unhandled', 6],
      ['prod_go', 'no_ideas', 0],
    ]);
    expect(merged[0].ideas).toEqual({
      projectId: 'mp_1',
      adCount: 6,
      keptCount: 0,
    });
  });

  it('counts kept ads before the remaining drafts', () => {
    const [merged] = mergeWorkspaceProducts(
      [product('prod_home', 'Acme Charge Home', '2026-11-10T00:00:00Z')],
      [ideas('mp_1', 'prod_home', 6, 4, '2026-11-14T00:00:00Z')]
    );
    expect(merged.signals).toMatchObject({
      keptUntested: 4,
      draftsUnhandled: 2,
    });
    expect([merged.step, merged.stepCount]).toEqual(['kept_untested', 4]);
  });

  it('adds up several projects of one product and keeps the newest project id', () => {
    const [merged] = mergeWorkspaceProducts(
      [product('prod_home', 'Acme Charge Home', '2026-11-01T00:00:00Z')],
      [
        ideas('mp_new', 'prod_home', 2, 1, '2026-11-15T00:00:00Z'),
        ideas('mp_old', 'prod_home', 6, 0, '2026-11-03T00:00:00Z'),
      ]
    );
    expect(merged.ideas).toEqual({
      projectId: 'mp_new',
      adCount: 8,
      keptCount: 1,
    });
  });

  it('lists a product only an ideas project names, under its name', () => {
    const merged = mergeWorkspaceProducts(
      [],
      [ideas('mp_1', 'prod_draft', 0, 0, '2026-11-15T00:00:00Z', 'Northpeak')]
    );
    expect(merged).toHaveLength(1);
    expect(merged[0]).toMatchObject({
      productId: 'prod_draft',
      name: 'Northpeak',
      step: 'preparing',
    });
  });

  it('leaves a product without a name unnamed rather than showing its id', () => {
    const [merged] = mergeWorkspaceProducts(
      [product('prod_x', '  ', '2026-11-01T00:00:00Z')],
      []
    );
    expect(merged.name).toBeNull();
  });

  it('nothing reports errors, missing reports or unseen results yet', () => {
    const [merged] = mergeWorkspaceProducts(
      [product('prod_home', 'Acme Charge Home', '2026-11-01T00:00:00Z')],
      [ideas('mp_1', 'prod_home', 6, 2, '2026-11-15T00:00:00Z')]
    );
    expect(merged.signals).toMatchObject({
      needsAttention: false,
      reportMissing: false,
      conclusionUnseen: false,
    });
  });
});

test('productViewHref keeps the product id in one path segment', () => {
  expect(productViewHref('acme', 'prod_1', 'profile')).toBe(
    '/w/acme/p/prod_1/profile'
  );
  expect(productViewHref('acme', 'a/b', 'ideas')).toBe('/w/acme/p/a%2Fb/ideas');
});
