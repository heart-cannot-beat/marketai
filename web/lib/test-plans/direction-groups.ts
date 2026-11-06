/**
 * "按方向" view of one test-plan Run (front-flow contract §3.3, Issue
 * #170): the Run's items and the AssetVersions they produced, grouped under
 * the direction cards of the plan the Run came from, with each group's
 * generation status and review progress.
 *
 * The item -> direction mapping is `directionMatcher` from `plan-model`
 * (the same one the plan page uses); this module only adds the AssetVersion
 * side and the counts. Nothing here renders, and nothing here puts an id
 * into copy: a group is shown by its direction's title, or as "未匹配到方向".
 */
import {
  directionMatcher,
  groupItemsByDirection,
  type DirectionView,
  type PlanRunItem,
} from './plan-model';

/** Key of the bucket for items / versions no direction of the plan claims. */
export const UNMATCHED_GROUP_KEY = '__unmatched__';

/** The AssetVersion fields the grouped view reads (`AssetVersionSummary`). */
export interface GroupAssetVersion {
  id: string;
  asset_id: string;
  /** The producing item's frozen recipe tags (`AssetRecord.tags_json`). */
  tags?: unknown;
  effective_review_value?: string | null;
}

export interface StatusCounts {
  queued: number;
  running: number;
  done: number;
  failed: number;
  total: number;
}

export interface ReviewCounts {
  approved: number;
  needs_changes: number;
  rejected: number;
  unreviewed: number;
  total: number;
}

export interface DirectionGroup<
  V extends GroupAssetVersion = GroupAssetVersion,
> {
  /** The direction's id, or `UNMATCHED_GROUP_KEY`. Never shown. */
  key: string;
  /** `null` for the unmatched bucket. */
  direction: DirectionView | null;
  items: PlanRunItem[];
  versions: V[];
  status: StatusCounts;
  review: ReviewCounts;
}

const STATUS_BUCKET: Record<string, keyof Omit<StatusCounts, 'total'>> = {
  pending: 'queued',
  queued: 'queued',
  paused: 'queued',
  running: 'running',
  qc_pending: 'running',
  uncertain: 'running',
  succeeded: 'done',
  reused: 'done',
  failed: 'failed',
  cancelled: 'failed',
};

/** A RunItem state (`ck_run_item_state`) as one of the four counts the
 * group shows; a state added later counts as still queued. */
export function statusBucket(state: string): keyof Omit<StatusCounts, 'total'> {
  return STATUS_BUCKET[state] ?? 'queued';
}

export function countStatuses(
  items: Pick<PlanRunItem, 'state'>[]
): StatusCounts {
  const counts: StatusCounts = {
    queued: 0,
    running: 0,
    done: 0,
    failed: 0,
    total: items.length,
  };
  for (const item of items) counts[statusBucket(item.state)] += 1;
  return counts;
}

/** Effective review values (`project_effective_review`); no head, or a
 * cleared one, is unreviewed -- the same rule as the gallery facet. */
export function countReviews(
  versions: Pick<GroupAssetVersion, 'effective_review_value'>[]
): ReviewCounts {
  const counts: ReviewCounts = {
    approved: 0,
    needs_changes: 0,
    rejected: 0,
    unreviewed: 0,
    total: versions.length,
  };
  for (const version of versions) {
    const value = version.effective_review_value;
    if (
      value === 'approved' ||
      value === 'needs_changes' ||
      value === 'rejected'
    ) {
      counts[value] += 1;
    } else {
      counts.unreviewed += 1;
    }
  }
  return counts;
}

function tagsOf(
  version: GroupAssetVersion
): Record<string, unknown> | undefined {
  const tags = version.tags;
  return tags && typeof tags === 'object' && !Array.isArray(tags)
    ? (tags as Record<string, unknown>)
    : undefined;
}

/**
 * Groups a Run's items and AssetVersions by direction.
 *
 * An AssetVersion belongs to the group of the item it is bound to; a
 * regenerated version (same asset, newer version) follows its asset; a
 * version no item points at is matched by its own combination tags; the
 * rest land in the unmatched bucket. Groups follow the plan's direction
 * order (a removed direction keeps the images it already produced); a
 * direction with nothing in this Run has no group; the unmatched bucket,
 * when present, comes last.
 */
export function buildDirectionGroups<V extends GroupAssetVersion>(
  directions: DirectionView[],
  items: PlanRunItem[],
  versions: V[]
): DirectionGroup<V>[] {
  const { byDirection, unmatched } = groupItemsByDirection(directions, items);
  const match = directionMatcher(directions);

  const ownerByVersion = new Map<string, string>();
  byDirection.forEach((groupItems, key) => {
    for (const item of groupItems) {
      if (item.bound_asset_version_id) {
        ownerByVersion.set(item.bound_asset_version_id, key);
      }
    }
  });
  for (const item of unmatched) {
    if (item.bound_asset_version_id) {
      ownerByVersion.set(item.bound_asset_version_id, UNMATCHED_GROUP_KEY);
    }
  }
  const ownerByAsset = new Map<string, string>();
  for (const version of versions) {
    const owner = ownerByVersion.get(version.id);
    if (owner) ownerByAsset.set(version.asset_id, owner);
  }

  const versionsByKey = new Map<string, V[]>();
  for (const version of versions) {
    const owner =
      ownerByVersion.get(version.id) ??
      ownerByAsset.get(version.asset_id) ??
      match({ tags: tagsOf(version) }) ??
      UNMATCHED_GROUP_KEY;
    const list = versionsByKey.get(owner) ?? [];
    list.push(version);
    versionsByKey.set(owner, list);
  }

  const group = (
    key: string,
    direction: DirectionView | null,
    groupItems: PlanRunItem[]
  ): DirectionGroup<V> => {
    const groupVersions = versionsByKey.get(key) ?? [];
    return {
      key,
      direction,
      items: groupItems,
      versions: groupVersions,
      status: countStatuses(groupItems),
      review: countReviews(groupVersions),
    };
  };

  const groups = [...directions]
    .sort((a, b) => a.ordinal - b.ordinal)
    .filter(
      (direction) =>
        (byDirection.get(direction.direction_id)?.length ?? 0) > 0 ||
        (versionsByKey.get(direction.direction_id)?.length ?? 0) > 0
    )
    .map((direction) =>
      group(
        direction.direction_id,
        direction,
        byDirection.get(direction.direction_id) ?? []
      )
    );
  if (unmatched.length > 0 || versionsByKey.has(UNMATCHED_GROUP_KEY)) {
    groups.push(group(UNMATCHED_GROUP_KEY, null, unmatched));
  }
  return groups;
}

/** Version ids of a group, in the order the group shows them. */
export function groupVersionIds(group: DirectionGroup): string[] {
  return group.versions.map((version) => version.id);
}
