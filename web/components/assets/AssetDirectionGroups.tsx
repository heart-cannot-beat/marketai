import { useEffect } from 'react';
import Link from 'next/link';
import { useTranslation } from 'next-i18next';
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
import type { UseRunDirectionGroupsResult } from '@/hooks/test-plans/useRunDirectionGroups';
import {
  groupVersionIds,
  type DirectionGroup,
} from '@/lib/test-plans/direction-groups';
import { takeGroupExport } from '@/lib/test-plans/group-export-handoff';
import { useAssets } from './AssetsProvider';

interface AssetDirectionGroupsProps {
  runId: string;
  grouped: UseRunDirectionGroupsResult;
}

/**
 * The Assets page's "按方向" view of one test-plan Run (Issue #170): the
 * Run's AssetVersions under the plan's directions. The exits drive the
 * page's existing overlays -- 审核本组 seeds the review queue with the
 * group (as "review selected" does) and opens review mode; 导出本组
 * selects the group and opens the export dialog on that selection; a
 * thumbnail opens Asset Detail.
 */
export function AssetDirectionGroups({
  runId,
  grouped,
}: AssetDirectionGroupsProps) {
  const { t } = useTranslation('directions');
  const { current } = useWorkspace();
  const slug = current.slug;
  const { urlState, selection } = useAssets();
  const { selectAll } = selection;
  const { setExportId } = urlState;

  // 导出本组 pressed on the Run page: select what it stashed and open the
  // export dialog once.
  useEffect(() => {
    const ids = takeGroupExport(slug, runId);
    if (!ids) return;
    selectAll(ids);
    setExportId('new');
  }, [slug, runId, selectAll, setExportId]);

  const review = (group: DirectionGroup) => {
    seedReviewQueueSnapshot(
      reviewQueueStorageKey(slug, urlState.filters),
      groupVersionIds(group)
    );
    urlState.setReviewMode(true);
  };

  const exportGroup = (group: DirectionGroup) => {
    selection.selectAll(groupVersionIds(group));
    urlState.setExportId('new');
  };

  const plan = grouped.plan;
  const name = plan?.product_name?.trim() || '';
  const source =
    grouped.stage === 'sample'
      ? 'groups-source-sample'
      : grouped.stage === 'full'
        ? 'groups-source-full'
        : 'groups-source-other';

  return (
    <div className="space-y-4 p-4" data-testid="asset-direction-groups">
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
        <div className="flex justify-center py-12" role="status">
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
              {group.versions.length > 0 && (
                <ul
                  className="grid gap-2"
                  style={{
                    gridTemplateColumns:
                      'repeat(auto-fill, minmax(min(8rem, 30%), 1fr))',
                  }}
                  aria-label={t('group-images-label', { title })}
                >
                  {group.versions.map((version, index) => (
                    <li key={version.id}>
                      <button
                        type="button"
                        className="relative block aspect-square w-full overflow-hidden rounded-lg bg-base-200 hover:ring-1 hover:ring-primary focus:outline-none focus-visible:ring-2 focus-visible:ring-primary"
                        aria-label={t('image-label', {
                          title,
                          index: index + 1,
                        })}
                        onClick={() => urlState.setOverlay(version.id)}
                        data-testid="group-asset"
                      >
                        <GroupThumb
                          versionId={version.id}
                          thumbnail={version.thumbnail ?? null}
                        />
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </DirectionGroupSection>
          );
        })
      )}
    </div>
  );
}
