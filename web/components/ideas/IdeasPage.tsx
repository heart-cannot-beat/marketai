import { useId, useMemo, useState } from 'react';
import { useTranslation } from 'next-i18next';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { GuardedLink } from '@/components/workspace/GuardedLink';
import { useWorkspaceProducts } from '@/hooks/products/useWorkspaceProducts';
import { KeepFailed, useIdeasProject } from '@/hooks/ideas/useIdeasProject';
import { errorMessage } from '@/lib/i18n/domain-errors';
import {
  ideasView,
  type ClaimGroup,
  type IdeaSlot,
  type IdeasAd,
} from '@/lib/ideas/ideas-view';
import {
  productViewHref,
  type WorkspaceProduct,
} from '@/lib/products/workspace-products';
import { AdCard } from './AdCard';
import { PRODUCT_PANEL_ID, ProductPanel } from './ProductPanel';

/** Problems whose shared copy speaks of something else; the page says its own. */
const KEEP_PROBLEM_COPY: Record<string, string> = {
  ad_not_found: 'ideas-keep-ad-gone',
  marketing_project_not_found: 'ideas-keep-ad-gone',
};

/**
 * A product's ad ideas (ADR 0018, design §5.3, #196), the read-only part of
 * the third screen: the ads grouped by claim, each a draft that can be kept;
 * the facts the AI read beside them; and a bottom bar with how many are kept.
 * Editing an ad, another way of telling, downloading images only and taking
 * ads to a test come with later slices.
 */
export function IdeasPage({ product }: { product: WorkspaceProduct }) {
  const { t } = useTranslation('ideas');
  const projectId = product.ideas?.projectId;
  if (!projectId)
    return (
      <p className="text-sm text-gray-600 dark:text-gray-400">
        {t('ideas-none')}
      </p>
    );
  return <IdeasProject product={product} projectId={projectId} />;
}

function IdeasProject({
  product,
  projectId,
}: {
  product: WorkspaceProduct;
  projectId: string;
}) {
  const { t } = useTranslation('ideas');
  const { current } = useWorkspace();
  const products = useWorkspaceProducts();
  const { detail, failed, isLoading, refresh, setKept } =
    useIdeasProject(projectId);
  const view = useMemo(() => (detail ? ideasView(detail) : null), [detail]);
  const [busyAd, setBusyAd] = useState<string | null>(null);
  const [keepProblem, setKeepProblem] = useState<string | null>(null);

  if (isLoading)
    return (
      <div className="flex items-center gap-3 py-8" role="status">
        <span className="loading loading-spinner" aria-hidden="true" />
        <span className="text-sm">{t('ideas-loading')}</span>
      </div>
    );

  if (failed || !detail || !view)
    return (
      <div className="space-y-3">
        <p className="text-sm">{t('ideas-problem')}</p>
        <button
          type="button"
          className="btn btn-sm"
          onClick={() => void refresh()}
        >
          {t('ideas-retry')}
        </button>
      </div>
    );

  if (!detail.ideas)
    return (
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <p className="text-sm text-gray-600 dark:text-gray-400" role="status">
          {t('ideas-preparing')}
        </p>
        <ProductPanel understanding={detail.understanding} />
      </div>
    );

  async function toggleKept(item: IdeasAd) {
    setBusyAd(item.asset_version_id);
    setKeepProblem(null);
    try {
      await setKept(item.asset_version_id, !item.kept);
      void products.refresh();
    } catch (error) {
      setKeepProblem(
        error instanceof KeepFailed && error.problem
          ? KEEP_PROBLEM_COPY[error.problem.code]
            ? t(KEEP_PROBLEM_COPY[error.problem.code])
            : errorMessage(t, error.problem)
          : error instanceof KeepFailed && error.uncertain
            ? t('ideas-keep-uncertain')
            : t('ideas-keep-failed')
      );
    } finally {
      setBusyAd(null);
    }
  }

  const card = (slot: IdeaSlot) => (
    <AdCard
      key={slot.key}
      slot={slot}
      busy={slot.kind === 'ad' && busyAd === slot.ad.asset_version_id}
      onToggleKept={(item) => void toggleKept(item)}
    />
  );

  return (
    <div className="space-y-6">
      <div className="space-y-1">
        <h2 className="text-xl font-bold sm:text-2xl">
          {t('ideas-title', {
            count: view.adCount,
            claims: view.claims.length,
          })}
        </h2>
        <p className="text-sm text-gray-600 dark:text-gray-400">
          {t('ideas-intro')}
        </p>
        {view.makingCount > 0 && (
          <p className="text-sm text-gray-600 dark:text-gray-400" role="status">
            {t('ideas-making', { count: view.makingCount })}
          </p>
        )}
      </div>

      <a
        href={`#${PRODUCT_PANEL_ID}`}
        className="flex min-h-11 items-center rounded-lg border border-amber-200 bg-amber-50 px-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200 lg:hidden"
      >
        {t('ideas-panel-jump')}
      </a>

      {view.withoutPhotoCount > 0 && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-gray-200 bg-gray-50 p-3 text-sm dark:border-gray-700 dark:bg-gray-900">
          <p className="min-w-0 flex-1 basis-60">{t('ideas-no-photo-hint')}</p>
          <GuardedLink
            href={productViewHref(current.slug, product.productId, 'profile')}
            className="btn btn-sm min-h-11"
          >
            {t('ideas-no-photo-action')}
          </GuardedLink>
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="min-w-0 space-y-5">
          {view.claims.map((group) => (
            <ClaimSection key={group.key} group={group} card={card} />
          ))}
          {view.earlier.length > 0 && (
            <EarlierSection slots={view.earlier} card={card} />
          )}
          <KeptBar keptCount={view.keptCount} problem={keepProblem} />
        </div>
        <ProductPanel understanding={detail.understanding} />
      </div>
    </div>
  );
}

const SECTION =
  'space-y-4 rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900 sm:p-5';
const AD_GRID = 'grid grid-cols-2 gap-3 sm:grid-cols-3 sm:gap-4';

function ClaimSection({
  group,
  card,
}: {
  group: ClaimGroup;
  card: (slot: IdeaSlot) => JSX.Element;
}) {
  const { t } = useTranslation('ideas');
  const id = useId();
  return (
    <section aria-labelledby={`${id}-n ${id}-t`} className={SECTION}>
      <div className="space-y-1">
        <p
          id={`${id}-n`}
          className="w-fit rounded-full bg-gray-100 px-2.5 py-0.5 text-xs font-semibold text-gray-700 dark:bg-gray-800 dark:text-gray-300"
        >
          {t('ideas-claim-label', { number: group.number })}
        </p>
        <h3 id={`${id}-t`} className="text-lg font-bold leading-snug">
          {group.proposition}
        </h3>
        {group.competitors && (
          <p className="text-sm text-gray-600 dark:text-gray-400">
            {t(`ideas-competitors-${group.competitors}`)}
          </p>
        )}
        <details className="text-sm">
          <summary className="min-h-11 cursor-pointer py-2 text-gray-700 dark:text-gray-300 sm:min-h-0">
            {t('ideas-claim-why')}
          </summary>
          <p className="text-gray-600 dark:text-gray-400">{group.rationale}</p>
        </details>
      </div>
      <div className={AD_GRID}>{group.slots.map(card)}</div>
    </section>
  );
}

function EarlierSection({
  slots,
  card,
}: {
  slots: IdeaSlot[];
  card: (slot: IdeaSlot) => JSX.Element;
}) {
  const { t } = useTranslation('ideas');
  const title = useId();
  return (
    <section aria-labelledby={title} className={SECTION}>
      <div className="space-y-1">
        <h3 id={title} className="text-lg font-bold">
          {t('ideas-earlier-title')}
        </h3>
        <p className="text-sm text-gray-600 dark:text-gray-400">
          {t('ideas-earlier-description')}
        </p>
      </div>
      <div className={AD_GRID}>{slots.map(card)}</div>
    </section>
  );
}

/**
 * "N kept" stays at the bottom of the screen (design §5.8). Its button leads
 * to the check before downloading, which comes with #201 (S13); until then it
 * says so. With nothing kept it cannot be used (design §5.3).
 */
function KeptBar({
  keptCount,
  problem,
}: {
  keptCount: number;
  problem: string | null;
}) {
  const { t } = useTranslation('ideas');
  const [asked, setAsked] = useState(false);
  return (
    <div
      className="sticky bottom-0 z-10 -mx-4 space-y-2 border-t border-gray-800 bg-gray-900 px-4 py-3 text-white sm:mx-0 sm:rounded-xl sm:border sm:px-5"
      data-testid="ideas-kept-bar"
    >
      {problem && (
        <p className="text-sm text-red-200" role="alert">
          {problem}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
        <div className="min-w-0 flex-1 basis-56">
          <p className="text-base font-semibold">
            {t('ideas-kept-count', { count: keptCount })}
          </p>
          <p className="text-sm text-gray-300">
            {keptCount === 0 ? t('ideas-kept-none-hint') : t('ideas-kept-hint')}
          </p>
        </div>
        <button
          type="button"
          className="min-h-12 w-full rounded-lg bg-white px-5 text-sm font-semibold text-gray-900 disabled:cursor-not-allowed disabled:opacity-40 sm:w-auto"
          disabled={keptCount === 0}
          onClick={() => setAsked(true)}
        >
          {t('ideas-check-and-download')}
        </button>
      </div>
      {asked && keptCount > 0 && (
        <p className="text-sm text-gray-200" role="status">
          {t('ideas-check-not-open')}
        </p>
      )}
    </div>
  );
}
