import { useEffect, useMemo, useState } from 'react';
import { useRouter } from 'next/router';
import { useTranslation } from 'next-i18next';
import { useDomainPaginatedQuery } from '@/hooks/domain/useDomainPaginatedQuery';
import { FeatureEmptyState } from '@/components/workspace';
import { DraftList } from '@/components/generation/list/DraftList';
import type { DraftSummary } from '@/components/generation/list/DraftList';
import { NewDraftDialog } from '@/components/generation/list/NewDraftDialog';
import { RunList } from '@/components/generation/list/RunList';
import type { RunSummaryItem } from '@/components/generation/list/RunList';
import { ProductName } from '@/components/products/ProductName';
import { takeProductHandoff } from '@/lib/products/handoff';

/** Raw item shape returned by `list_generation_drafts`
 * (`app/generation/run_queries.py::_draft_summary`) — no `title` and no
 * `allowed_actions`; see `DraftSummary` in `DraftList.tsx` for how this maps
 * to the display type. */
interface GenerationDraftListItem {
  id: string;
  product_id: string;
  provider: string;
  model: string;
  execution_mode: string;
  state: string;
  revision: number;
  updated_at: string | null;
}

/** Raw item shape returned by `list_generation_runs`
 * (`app/generation/run_queries.py::_run_summary`) -- matches `RunSummaryItem`
 * (`RunList.tsx`) directly, no mapping needed. */
type GenerationRunListItem = RunSummaryItem;

export function GenerationListView() {
  const { t } = useTranslation('common');
  const router = useRouter();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [preselectedProductId, setPreselectedProductId] = useState<
    string | null
  >(null);

  // `?new=1` opens New Draft, with a product from a one-shot `new-draft`
  // handoff when one was stashed (never the URL).
  const wantsNewDraft = router?.query?.new === '1';
  useEffect(() => {
    if (!wantsNewDraft) return;
    const slug = String(router.query.slug ?? '');
    setPreselectedProductId(takeProductHandoff('new-draft', slug));
    setDialogOpen(true);
    const rest = { ...router.query };
    delete rest.new;
    void router.replace({ pathname: router.pathname, query: rest }, undefined, {
      shallow: true,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wantsNewDraft]);

  const {
    items,
    hasMore,
    isLoading,
    isLoadingMore,
    problem,
    loadMore,
    refresh,
  } = useDomainPaginatedQuery<
    'list_generation_drafts',
    GenerationDraftListItem
  >('list_generation_drafts');

  const drafts: DraftSummary[] = useMemo(
    () =>
      items.map((draft) => ({
        id: draft.id,
        // Product name, never the `prod_` id (front-flow contract §1).
        title: <ProductName productId={draft.product_id} />,
        execution_mode: draft.execution_mode,
        updated_at: draft.updated_at ?? '',
        allowed_actions: [],
        provider: draft.provider,
        model: draft.model,
        state: draft.state,
      })),
    [items]
  );

  const isEmpty = !isLoading && drafts.length === 0 && !problem;

  const {
    items: runItems,
    hasMore: runsHasMore,
    isLoading: runsIsLoading,
    isLoadingMore: runsIsLoadingMore,
    problem: runsProblem,
    loadMore: loadMoreRuns,
    refresh: refreshRuns,
  } = useDomainPaginatedQuery<'list_generation_runs', GenerationRunListItem>(
    'list_generation_runs'
  );

  const runs: RunSummaryItem[] = useMemo(
    () =>
      runItems.map((run) => ({
        id: run.id,
        product_id: run.product_id,
        status: run.status,
        done: run.done,
        total: run.total,
        created_at: run.created_at,
      })),
    [runItems]
  );

  return (
    <>
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold outline-none">
          {t('workspace-page-generate')}
        </h1>
        <button
          type="button"
          className="btn btn-primary btn-sm"
          onClick={() => setDialogOpen(true)}
        >
          {t('generation-list-new-draft')}
        </button>
      </div>

      {isLoading && (
        <div className="mt-8 flex justify-center" aria-busy="true">
          <span className="loading loading-spinner" />
        </div>
      )}

      {!isLoading && problem && drafts.length === 0 && (
        <div className="alert alert-error mt-8" role="alert">
          <span>{t('generation-list-error')}</span>
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => refresh()}
          >
            {t('generation-list-retry')}
          </button>
        </div>
      )}

      {isEmpty && (
        <FeatureEmptyState
          title={t('generation-list-empty-title')}
          description={t('generation-list-empty-description')}
          action={
            <button
              type="button"
              className="btn btn-primary btn-sm mt-2"
              onClick={() => setDialogOpen(true)}
            >
              {t('generation-list-empty-action')}
            </button>
          }
        />
      )}

      {!isLoading && drafts.length > 0 && (
        <div className="mt-6">
          <h2 className="text-lg font-semibold mb-3">
            {t('generation-list-drafts-heading')}
          </h2>
          <DraftList drafts={drafts} />

          {problem && (
            <p className="text-xs text-error mt-2" role="alert">
              {t('generation-list-error')}
            </p>
          )}

          <div className="mt-4 text-center">
            {hasMore ? (
              <button
                type="button"
                className="btn btn-sm btn-ghost w-full"
                onClick={loadMore}
                disabled={isLoadingMore}
              >
                {isLoadingMore
                  ? t('generation-list-loading')
                  : t('generation-list-load-more')}
              </button>
            ) : (
              <p className="text-xs text-base-content/60">
                {t('generation-list-end')}
              </p>
            )}
          </div>
        </div>
      )}

      {!isLoading && (
        <div className="mt-6">
          <h2 className="text-lg font-semibold mb-3">
            {t('generation-list-runs-heading')}
          </h2>

          {runsIsLoading ? (
            <div className="flex justify-center" aria-busy="true">
              <span className="loading loading-spinner" />
            </div>
          ) : runsProblem && runs.length === 0 ? (
            <div className="alert alert-error" role="alert">
              <span>{t('generation-list-runs-error')}</span>
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => refreshRuns()}
              >
                {t('generation-list-retry')}
              </button>
            </div>
          ) : (
            <>
              <RunList runs={runs} />

              {runsProblem && (
                <p className="text-xs text-error mt-2" role="alert">
                  {t('generation-list-runs-error')}
                </p>
              )}

              {runs.length > 0 && (
                <div className="mt-4 text-center">
                  {runsHasMore ? (
                    <button
                      type="button"
                      className="btn btn-sm btn-ghost w-full"
                      onClick={loadMoreRuns}
                      disabled={runsIsLoadingMore}
                    >
                      {runsIsLoadingMore
                        ? t('generation-list-loading')
                        : t('generation-list-load-more')}
                    </button>
                  ) : (
                    <p className="text-xs text-base-content/60">
                      {t('generation-list-end')}
                    </p>
                  )}
                </div>
              )}
            </>
          )}
        </div>
      )}

      <NewDraftDialog
        open={dialogOpen}
        onClose={() => {
          setDialogOpen(false);
          setPreselectedProductId(null);
        }}
        initialProductId={preselectedProductId}
      />
    </>
  );
}
