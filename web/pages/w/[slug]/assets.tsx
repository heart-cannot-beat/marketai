import type { ReactElement } from 'react';
import WorkspaceLayout from '@/components/layouts/WorkspaceLayout';
import { AssetsProvider, useAssets } from '@/components/assets/AssetsProvider';
import { BatchRail } from '@/components/assets/BatchRail';
import { GalleryZone } from '@/components/assets/GalleryZone';
import { LightboxModal } from '@/components/assets/LightboxModal';
import { ReviewMode } from '@/components/assets/ReviewMode';
import { ExportFlow } from '@/components/assets/ExportFlow';
import { ExternalCreativesToolbar } from '@/components/external-creatives';
import { useDomainPaginatedQuery } from '@/hooks/domain/useDomainPaginatedQuery';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { NextPageWithLayout } from 'types';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';
import { useTranslation } from 'next-i18next';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AssetDirectionGroups } from '@/components/assets/AssetDirectionGroups';
import { DirectionViewToggle } from '@/components/direction-groups/DirectionViewToggle';
import { useRunDirectionGroups } from '@/hooks/test-plans/useRunDirectionGroups';
import { useRunOrigins } from '@/hooks/assets/useRunOrigins';
import { useProductDirectory } from '@/hooks/products/useProductDirectory';

interface Props {
  bootstrap: WorkspaceBootstrap;
  errorCode?: number;
  errorContext?: string;
}

interface RunListItem {
  id: string;
  status?: string;
  done?: number;
  failed?: number;
}

/** Counts how often review mode or Asset Detail has closed, so the grouped
 * view rereads review values a review there just changed. */
function useReviewTick(reviewMode: boolean, overlay: string | undefined) {
  const [tick, setTick] = useState(0);
  const open = reviewMode || Boolean(overlay);
  const wasOpen = useRef(open);
  useEffect(() => {
    if (wasOpen.current && !open) setTick((value) => value + 1);
    wasOpen.current = open;
  }, [open]);
  return tick;
}

function AssetsContent() {
  const { t } = useTranslation('common');
  const { t: tDirections } = useTranslation('directions');
  const { urlState } = useAssets();

  const runs = useDomainPaginatedQuery('list_generation_runs', {});
  // Batches are named by product / test, never by `gr_` id (Issue #191).
  const runIds = useMemo(
    () => (runs.items as RunListItem[]).map((run) => run.id),
    [runs.items]
  );
  const { origins } = useRunOrigins(runIds);
  const { nameFor: productNameFor } = useProductDirectory();
  // Bumped after external creatives are confirmed so the gallery reloads.
  const [galleryVersion, setGalleryVersion] = useState(0);

  const handleSelectRun = useCallback(
    (runId: string | undefined) => {
      urlState.setFilter('runId', runId);
    },
    [urlState]
  );

  // "按方向" (Issue #170): a Run filtered from a test plan can be shown
  // grouped by the plan's directions. Its counts reread when the Run moves
  // and after a review (review mode or Asset Detail closing).
  const runId = urlState.filters.runId;
  const reviewTick = useReviewTick(urlState.reviewMode, urlState.overlay);
  const runSummary = (runs.items as RunListItem[]).find(
    (run) => run.id === runId
  );
  const grouped = useRunDirectionGroups(
    runId,
    [runSummary?.status, runSummary?.done, runSummary?.failed, reviewTick].join(
      ':'
    )
  );
  const groupable = Boolean(runId) && grouped.status === 'found';
  const view = urlState.view ?? (groupable ? 'directions' : 'all');
  const showGrouped = groupable && view === 'directions';
  // Until we know whether the Run has a plan, an unchosen view waits
  // rather than flashing the flat gallery and then regrouping it.
  const deciding = grouped.status === 'loading' && !urlState.view;

  return (
    <>
      <div className="flex h-full min-h-0">
        <div className="hidden lg:block w-64 shrink-0 border-r border-base-300 overflow-y-auto">
          <BatchRail
            runs={runs.items as any[]}
            activeRunId={urlState.filters.runId}
            onSelectRun={handleSelectRun}
            isLoading={runs.isLoading}
            origins={origins}
            productNameFor={productNameFor}
          />
        </div>
        <div className="flex-1 min-w-0 flex flex-col">
          <h1 className="sr-only">{t('workspace-page-assets')}</h1>
          {/* External creatives (Issue #168) share the row with #170's view
              toggle. External images belong to no Run, so the All / Generated /
              External switch only shows while no Run is selected. */}
          <ExternalCreativesToolbar
            originKind={urlState.filters.originKind}
            onOriginKindChange={(originKind) =>
              urlState.setFilter('originKind', originKind)
            }
            productId={urlState.filters.productId}
            onChanged={() => setGalleryVersion((version) => version + 1)}
            showSourceSwitch={!runId}
          >
            {groupable && (
              <DirectionViewToggle
                value={view}
                onChange={(next) => urlState.setView(next)}
                allLabel={tDirections('view-all-assets')}
              />
            )}
          </ExternalCreativesToolbar>
          <div className="flex-1 min-h-0">
            {showGrouped && runId ? (
              <div className="h-full overflow-y-auto">
                <AssetDirectionGroups runId={runId} grouped={grouped} />
              </div>
            ) : deciding ? (
              <div className="flex justify-center py-16" role="status">
                <span className="loading loading-spinner loading-lg" />
              </div>
            ) : (
              <GalleryZone
                key={galleryVersion}
                runs={runs.items as any[]}
                activeRunId={urlState.filters.runId}
                onSelectRun={handleSelectRun}
                origins={origins}
                productNameFor={productNameFor}
              />
            )}
          </div>
        </div>
      </div>

      {urlState.overlay && (
        <LightboxModal
          versionId={urlState.overlay}
          onClose={() => urlState.setOverlay(undefined)}
          onNavigate={(versionId) => urlState.setOverlay(versionId)}
        />
      )}

      {urlState.reviewMode && (
        <ReviewMode onExit={() => urlState.setReviewMode(false)} />
      )}

      {urlState.exportId && (
        <ExportFlow
          packageId={
            urlState.exportId === 'new' ? undefined : urlState.exportId
          }
          onClose={() => urlState.setExportId(undefined)}
        />
      )}
    </>
  );
}

const AssetsPage: NextPageWithLayout<Props> = () => {
  return (
    <AssetsProvider>
      <AssetsContent />
    </AssetsProvider>
  );
};

AssetsPage.getLayout = function getLayout(page: ReactElement) {
  const { bootstrap, errorCode, errorContext } = page.props as Props;
  return (
    <WorkspaceLayout
      bootstrap={bootstrap}
      errorCode={errorCode}
      errorContext={errorContext}
    >
      {page}
    </WorkspaceLayout>
  );
};

export const getServerSideProps = getWorkspacePageProps({
  namespaces: ['common', 'assets', 'directions', 'external'],
});
export default AssetsPage;
