import type { components } from '@/lib/domain-client/generated/domain-v1';

type ProductSummary = components['schemas']['ProductSummary'];
type IdeasProjectSummary = components['schemas']['IdeasProjectSummary'];

/**
 * The one thing to do now for a product (design §5.6). The first five are the
 * overview's order: the first that holds wins. `preparing` and `no_ideas`
 * apply only when none of them does.
 */
export type NextStepKind =
  | 'attention'
  | 'report_missing'
  | 'conclusion_unseen'
  | 'kept_untested'
  | 'drafts_unhandled'
  | 'preparing'
  | 'no_ideas';

export interface ProductSignals {
  /** An error waiting for the user: a first batch of one of the product's
   * ad ideas stopped and waits (`first_batch.needs_attention`, #193, #195). */
  needsAttention: boolean;
  /** A running test whose delivery report is missing (step 2). */
  reportMissing: boolean;
  /** A result the user has not opened yet (step 3). */
  conclusionUnseen: boolean;
  /** Kept ads that no test uses yet; in step 1 no ad is tested. */
  keptUntested: number;
  /** Ads the user has neither kept nor otherwise dealt with. */
  draftsUnhandled: number;
  /** Whether the product has ad ideas at all. */
  hasIdeas: boolean;
}

export function nextStep(signals: ProductSignals): NextStepKind {
  if (signals.needsAttention) return 'attention';
  if (signals.reportMissing) return 'report_missing';
  if (signals.conclusionUnseen) return 'conclusion_unseen';
  if (signals.keptUntested > 0) return 'kept_untested';
  if (signals.draftsUnhandled > 0) return 'drafts_unhandled';
  return signals.hasIdeas ? 'preparing' : 'no_ideas';
}

export interface ProductIdeas {
  /** The most recently changed ads-first project of the product. */
  projectId: string;
  adCount: number;
  keptCount: number;
}

export interface WorkspaceProduct {
  productId: string;
  /** `null` when nothing names the product; render "Unnamed product". */
  name: string | null;
  ideas: ProductIdeas | null;
  signals: ProductSignals;
  step: NextStepKind;
  /** The step's count, for the copy that names one. */
  stepCount: number;
}

const newest = (a: string | null | undefined, b: string | null | undefined) =>
  (a ?? '') >= (b ?? '') ? (a ?? '') : (b ?? '');

/**
 * The sidebar's and the overview's product list: `list_products` merged with
 * `list_marketing_ideas` by product id, most recently changed first. A
 * product that only an ideas project names (its card is not listed) still
 * appears, under the name the project carries.
 */
export function mergeWorkspaceProducts(
  products: ProductSummary[],
  ideasProjects: IdeasProjectSummary[]
): WorkspaceProduct[] {
  const rows = new Map<
    string,
    {
      name: string | null;
      changedAt: string;
      ideas: (ProductIdeas & { changedAt: string }) | null;
      needsAttention?: boolean;
    }
  >();

  for (const product of products) {
    rows.set(product.product_id, {
      name: product.name?.trim() || null,
      changedAt: newest(product.updated_at, product.created_at),
      ideas: null,
    });
  }

  for (const project of ideasProjects) {
    const row = rows.get(project.product_id) ?? {
      name: project.product_name?.trim() || null,
      changedAt: '',
      ideas: null,
    };
    const ideas = row.ideas;
    const isNewer = !ideas || project.updated_at >= ideas.changedAt;
    row.ideas = {
      projectId: isNewer ? project.id : ideas.projectId,
      changedAt: isNewer ? project.updated_at : ideas.changedAt,
      adCount: (ideas?.adCount ?? 0) + project.ad_count,
      keptCount: (ideas?.keptCount ?? 0) + project.kept_count,
    };
    row.changedAt = newest(row.changedAt, project.updated_at);
    row.needsAttention =
      row.needsAttention || Boolean(project.first_batch?.needs_attention);
    rows.set(project.product_id, row);
  }

  return Array.from(rows.entries())
    .sort(([, a], [, b]) => (a.changedAt < b.changedAt ? 1 : -1))
    .map(([productId, row]) => {
      const ideas = row.ideas
        ? {
            projectId: row.ideas.projectId,
            adCount: row.ideas.adCount,
            keptCount: row.ideas.keptCount,
          }
        : null;
      const signals: ProductSignals = {
        needsAttention: row.needsAttention ?? false,
        reportMissing: false,
        conclusionUnseen: false,
        keptUntested: ideas?.keptCount ?? 0,
        draftsUnhandled: ideas ? ideas.adCount - ideas.keptCount : 0,
        hasIdeas: ideas !== null,
      };
      const step = nextStep(signals);
      return {
        productId,
        name: row.name,
        ideas,
        signals,
        step,
        stepCount:
          step === 'kept_untested'
            ? signals.keptUntested
            : step === 'drafts_unhandled'
              ? signals.draftsUnhandled
              : 0,
      };
    });
}

/** `common` keys for a step: the short status (sidebar, overview badge),
 * the overview's sentence and its main button. */
export const STEP_COPY: Record<
  NextStepKind,
  { status: string; line: string; action: string }
> = {
  attention: {
    status: 'product-status-attention',
    line: 'overview-step-attention',
    action: 'overview-action-attention',
  },
  report_missing: {
    status: 'product-status-report-missing',
    line: 'overview-step-report-missing',
    action: 'overview-action-report-missing',
  },
  conclusion_unseen: {
    status: 'product-status-conclusion-unseen',
    line: 'overview-step-conclusion-unseen',
    action: 'overview-action-conclusion-unseen',
  },
  kept_untested: {
    status: 'product-status-kept-untested',
    line: 'overview-step-kept-untested',
    action: 'overview-action-kept-untested',
  },
  drafts_unhandled: {
    status: 'product-status-drafts-unhandled',
    line: 'overview-step-drafts-unhandled',
    action: 'overview-action-drafts-unhandled',
  },
  preparing: {
    status: 'product-status-preparing',
    line: 'overview-step-preparing',
    action: 'overview-action-preparing',
  },
  no_ideas: {
    status: 'product-status-no-ideas',
    line: 'overview-step-no-ideas',
    action: 'overview-action-no-ideas',
  },
};

export const PRODUCT_VIEWS = ['ideas', 'tests', 'results', 'profile'] as const;
export type ProductView = (typeof PRODUCT_VIEWS)[number];

export function productViewHref(
  slug: string,
  productId: string,
  view: ProductView
): string {
  return `/w/${slug}/p/${encodeURIComponent(productId)}/${view}`;
}

/** "Take to test": a product's kept ads become a test (ADR 0019, #256); shown under its Tests view. */
export function takeToTestHref(slug: string, productId: string): string {
  return `/w/${slug}/p/${encodeURIComponent(productId)}/take-to-test`;
}

/** A test once its ad package is made: reports, numbers so far, schedule, result (#257). */
export function testHref(
  slug: string,
  productId: string,
  testId: string
): string {
  return `/w/${slug}/p/${encodeURIComponent(productId)}/tests/${encodeURIComponent(testId)}`;
}

/** The product view the overview's main button opens for a step. */
export function stepView(step: NextStepKind): ProductView {
  if (step === 'report_missing') return 'tests';
  if (step === 'conclusion_unseen') return 'results';
  return 'ideas';
}
