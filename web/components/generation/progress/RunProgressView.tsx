import { useState, type ReactNode } from 'react';
import { useTranslation } from 'next-i18next';
import { useRunProgress } from '@/components/generation/shared/useRunProgress';
import { RunSummary } from './RunSummary';
import { ProductName } from '@/components/products/ProductName';
import { RunItemGrid } from './RunItemGrid';
import { RunItemDetail } from './RunItemDetail';
import { OperationTracker } from './OperationTracker';
import { RunDirectionGroups } from './RunDirectionGroups';
import {
  DirectionViewToggle,
  type ResultsView,
} from '@/components/direction-groups/DirectionViewToggle';
import type { UseRunDirectionGroupsResult } from '@/hooks/test-plans/useRunDirectionGroups';
import type { GenerationRunItemDetail } from '@/components/generation/shared/RunProgressSource';

interface RunProgressViewProps {
  runId: string;
  /** Recovery-flow UI (cancel/retry/resume/reconcile entry points) --
   * rendered as soon as the Run has loaded, whatever its state: `Cancel Run`
   * is only *eligible* while the Run is still active
   * (`run_queries.run_actions`, `run_not_active` otherwise) and `Reconcile`
   * while it is `uncertain`, which `derive_run_state` does not count as
   * terminal, so gating the slot on `isRunTerminal` made both unreachable.
   * The server's `allowed_actions` decide what is enabled; the slot is just
   * where they render. `RunProgressView` never imports `progress/`'s
   * sibling recovery module itself (no lateral imports between feature
   * modules), the caller supplies this slot instead. */
  recoverySlot?: ReactNode;
  /** The Run's items grouped by the directions of the test plan it came
   * from (Issue #170, `useRunDirectionGroups`). With a plan the items show
   * "按方向" by default, with a switch back to every item; without one --
   * or without this prop -- the item grid is unchanged. */
  directionGroups?: UseRunDirectionGroupsResult;
}

/**
 * Root display for a GenerationRun's live progress (spec: Task 9, wiring
 * Task 8's `useRunProgress`). Polls the Run, its latest command's Operation,
 * and the current RunItems page together, and renders a summary card, the
 * command Operation's status, a selectable RunItem grid, and the selected
 * item's detail.
 */
export function RunProgressView({
  runId,
  recoverySlot,
  directionGroups,
}: RunProgressViewProps) {
  const { t } = useTranslation('common');
  const { t: tDirections } = useTranslation('directions');
  const progress = useRunProgress(runId);
  const [selectedItemId, setSelectedItemId] = useState<string | null>(null);
  const [chosenView, setChosenView] = useState<ResultsView | null>(null);

  const groupable = directionGroups?.status === 'found';
  const view: ResultsView = chosenView ?? (groupable ? 'directions' : 'all');
  const showGrouped = groupable && view === 'directions';
  const deciding = directionGroups?.status === 'loading' && !chosenView;

  // A grouped tile can be an item outside the polled page; its list row
  // carries the same fields the detail panel reads.
  const selectedItem =
    progress.items.find((item) => item.id === selectedItemId) ??
    ((directionGroups?.groups ?? [])
      .flatMap((group) => group.items)
      .find((item) => item.id === selectedItemId) as
      | GenerationRunItemDetail
      | undefined) ??
    null;

  // A definitive problem (e.g. the Run doesn't exist, or access was
  // revoked) before any successful fetch -- distinct from a transient
  // transport blip, which keeps polling and should not read as a dead end.
  if (!progress.run && progress.problem) {
    return (
      <div className="mt-8 text-center text-base-content/60">
        {t('generation-run-load-error')}
      </div>
    );
  }

  if (!progress.run && progress.transportError) {
    return (
      <div className="mt-8 text-center text-warning" role="alert">
        {t('generation-run-connection-issue')}
      </div>
    );
  }

  if (!progress.run) {
    return (
      <div className="flex justify-center mt-12" role="status">
        <span className="loading loading-spinner loading-lg" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold outline-none">
        <ProductName productId={progress.run.product_id} />
      </h1>

      <RunSummary run={progress.run} />

      <OperationTracker
        operation={progress.latestCommandOperation}
        transportError={progress.transportError}
      />

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-lg font-semibold">
              {showGrouped
                ? tDirections('groups-heading')
                : t('generation-run-items-heading')}
            </h2>
            {groupable && (
              <DirectionViewToggle
                value={view}
                onChange={setChosenView}
                allLabel={tDirections('view-all-items')}
              />
            )}
          </div>
          {showGrouped && directionGroups ? (
            <RunDirectionGroups
              runId={runId}
              grouped={directionGroups}
              selectedItemId={selectedItemId}
              onSelectItem={setSelectedItemId}
            />
          ) : deciding ? (
            <div className="flex justify-center py-8" role="status">
              <span className="loading loading-spinner" aria-hidden="true" />
            </div>
          ) : (
            <RunItemGrid
              items={progress.items}
              selectedItemId={selectedItemId}
              onSelectItem={setSelectedItemId}
              pagination={progress.itemsPagination}
              onPageChange={progress.setItemPage}
            />
          )}
        </div>
        <div className="lg:col-span-1">
          {selectedItem ? (
            <RunItemDetail item={selectedItem} />
          ) : (
            <p className="text-sm text-base-content/60">
              {t('generation-run-item-select-hint')}
            </p>
          )}
        </div>
      </div>

      {recoverySlot}
    </div>
  );
}
