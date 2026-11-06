import Link from 'next/link';
import { useRouter } from 'next/router';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import {
  DirectionGroupSection,
  groupTitle,
} from '@/components/direction-groups/DirectionGroupSection';
import { GroupThumb } from '@/components/direction-groups/GroupThumb';
import {
  reviewQueueStorageKey,
  seedReviewQueueSnapshot,
} from '@/hooks/assets/useReviewMode';
import { runOnlyAssetFilters } from '@/hooks/assets/useAssetsUrlState';
import type { UseRunDirectionGroupsResult } from '@/hooks/test-plans/useRunDirectionGroups';
import {
  groupVersionIds,
  statusBucket,
  type DirectionGroup,
} from '@/lib/test-plans/direction-groups';
import { stashGroupExport } from '@/lib/test-plans/group-export-handoff';
import type { PlanRunItem } from '@/lib/test-plans/plan-model';

interface RunDirectionGroupsProps {
  runId: string;
  grouped: UseRunDirectionGroupsResult;
  selectedItemId: string | null;
  onSelectItem: (itemId: string) => void;
}

function ItemTile({
  item,
  index,
  title,
  selected,
  onSelect,
}: {
  item: PlanRunItem;
  index: number;
  title: string;
  selected: boolean;
  onSelect: (itemId: string) => void;
}) {
  const { t } = useTranslation('directions');
  const bucket = statusBucket(item.state);
  const state = t(`item-state-${bucket}`);
  return (
    <li>
      <button
        type="button"
        className={classNames(
          'relative block aspect-square w-full overflow-hidden rounded-lg bg-base-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary',
          selected && 'ring-2 ring-primary'
        )}
        aria-label={t('item-label', { title, index: index + 1, state })}
        aria-pressed={selected}
        onClick={() => onSelect(item.id)}
        data-testid="group-item"
      >
        {item.bound_asset_version_id ? (
          <GroupThumb
            versionId={item.bound_asset_version_id}
            thumbnail={item.thumbnail ?? null}
          />
        ) : (
          <span
            className={classNames(
              'absolute inset-0 flex items-center justify-center text-xs',
              bucket === 'failed' ? 'text-error' : 'text-base-content/50'
            )}
          >
            {bucket === 'running' && (
              <span
                className="loading loading-dots loading-xs mr-1"
                aria-hidden="true"
              />
            )}
            {state}
          </span>
        )}
      </button>
    </li>
  );
}

/**
 * The Run page's "按方向" view (Issue #170): one section per direction of
 * the test plan the Run came from, each with its item tiles (selecting one
 * opens the existing item detail beside it) and the exits into the Assets
 * page -- review mode on exactly this group, export of this group, and the
 * experiments area.
 */
export function RunDirectionGroups({
  runId,
  grouped,
  selectedItemId,
  onSelectItem,
}: RunDirectionGroupsProps) {
  const { t } = useTranslation('directions');
  const router = useRouter();
  const { current } = useWorkspace();
  const slug = current.slug;
  const assetsHref = `/w/${slug}/assets?run=${encodeURIComponent(runId)}`;
  const plan = grouped.plan;

  const review = (group: DirectionGroup) => {
    // Same queue the gallery's "review selected" seeds, keyed by the
    // filter the Assets page opens with, so review mode walks this group.
    seedReviewQueueSnapshot(
      reviewQueueStorageKey(slug, runOnlyAssetFilters(runId)),
      groupVersionIds(group)
    );
    void router.push(`${assetsHref}&reviewMode=1`);
  };

  const exportGroup = (group: DirectionGroup) => {
    stashGroupExport(slug, runId, groupVersionIds(group));
    void router.push(`${assetsHref}&view=directions`);
  };

  const name = plan?.product_name?.trim() || '';
  const source =
    grouped.stage === 'sample'
      ? 'groups-source-sample'
      : grouped.stage === 'full'
        ? 'groups-source-full'
        : 'groups-source-other';

  return (
    <div className="space-y-4" data-testid="run-direction-groups">
      {plan && (
        <p className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-sm text-base-content/70">
          <span>{t(source, { name })}</span>
          <Link
            href={`/w/${slug}/tests/${encodeURIComponent(plan.plan_id)}`}
            className="link link-primary"
          >
            {t('groups-open-plan')}
          </Link>
        </p>
      )}

      {grouped.readFailed && (
        <p className="text-sm text-warning" role="alert">
          {t('groups-load-failed')}
        </p>
      )}

      {grouped.isLoading ? (
        <div className="flex justify-center py-8" role="status">
          <span className="loading loading-spinner" aria-hidden="true" />
          <span className="sr-only">{t('groups-loading')}</span>
        </div>
      ) : grouped.groups.length === 0 ? (
        <p className="text-sm text-base-content/60">{t('groups-empty')}</p>
      ) : (
        grouped.groups.map((group) => {
          const title = groupTitle(t, group);
          return (
            <DirectionGroupSection
              key={group.key}
              group={group}
              onReview={() => review(group)}
              onExport={() => exportGroup(group)}
              experimentsHref={`/w/${slug}/experiments`}
            >
              <ul
                className="grid grid-cols-3 gap-2 sm:grid-cols-4 xl:grid-cols-6"
                aria-label={t('group-images-label', { title })}
              >
                {group.items.map((item, index) => (
                  <ItemTile
                    key={item.id}
                    item={item}
                    index={index}
                    title={title}
                    selected={selectedItemId === item.id}
                    onSelect={onSelectItem}
                  />
                ))}
              </ul>
            </DirectionGroupSection>
          );
        })
      )}
    </div>
  );
}
