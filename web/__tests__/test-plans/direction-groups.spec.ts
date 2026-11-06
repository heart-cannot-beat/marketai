import {
  UNMATCHED_GROUP_KEY,
  buildDirectionGroups,
  countReviews,
  countStatuses,
  groupVersionIds,
  statusBucket,
} from '@/lib/test-plans/direction-groups';
import { directionMatcher } from '@/lib/test-plans/plan-model';
import { direction, item, version } from './direction-groups-fixtures';

const directions = [
  direction('dir-2', '周末露营'),
  direction('dir-1', '雨夜车库'),
  direction('dir-3', '老小区'),
];

describe('directionMatcher', () => {
  it('matches by plan signature, then by direction value, else null', () => {
    const match = directionMatcher(directions);
    expect(
      match({ tags: { 'lib_t:direction': 'dir-1', 'lib_t:scene': 'night' } })
    ).toBe('dir-1');
    // Factor values edited after the Run: the direction still owns it.
    expect(
      match({ tags: { 'lib_t:direction': 'dir-2', 'lib_t:scene': 'day' } })
    ).toBe('dir-2');
    // No tags: the fingerprint carries the combination.
    expect(
      match({
        combination_fingerprint: 'lib_t:direction=dir-3:lib_t:scene=night',
      })
    ).toBe('dir-3');
    expect(match({ tags: { 'lib_t:direction': 'dir-9' } })).toBeNull();
    expect(match({ tags: {} })).toBeNull();
  });
});

describe('buildDirectionGroups', () => {
  it('groups items and their versions by direction in plan order, unmatched last', () => {
    const items = [
      item('i3', 2, 'dir-2'),
      item('i1', 0, 'dir-1'),
      item('i2', 1, 'dir-1', {
        state: 'running',
        bound_asset_version_id: null,
      }),
      item('i9', 3, 'dir-9'),
    ];
    const versions = [
      version('av_i1', { effective_review_value: 'approved' }),
      version('av_i3', { effective_review_value: 'rejected' }),
      version('av_i9'),
    ];
    const groups = buildDirectionGroups(directions, items, versions);

    expect(groups.map((g) => g.key)).toEqual([
      'dir-1',
      'dir-2',
      UNMATCHED_GROUP_KEY,
    ]);
    // dir-3 produced nothing in this Run: no empty group.
    const [rain, camp, unmatched] = groups;
    expect(rain.direction?.title).toBe('雨夜车库');
    expect(rain.items.map((i) => i.id)).toEqual(['i1', 'i2']);
    expect(rain.versions.map((v) => v.id)).toEqual(['av_i1']);
    expect(rain.status).toEqual({
      queued: 0,
      running: 1,
      done: 1,
      failed: 0,
      total: 2,
    });
    expect(rain.review).toEqual({
      approved: 1,
      needs_changes: 0,
      rejected: 0,
      unreviewed: 0,
      total: 1,
    });
    expect(camp.review.rejected).toBe(1);
    expect(unmatched.direction).toBeNull();
    expect(unmatched.items.map((i) => i.id)).toEqual(['i9']);
    expect(groupVersionIds(unmatched)).toEqual(['av_i9']);
  });

  it('puts a regenerated version with its asset and an unbound version by its tags', () => {
    const items = [item('i1', 0, 'dir-1')];
    const versions = [
      // Regeneration: a newer version of the same asset, no item bound to it.
      version('av_new', { asset_id: 'asset_av_i1', tags: {} }),
      version('av_i1'),
      // Not bound to any item of this page, but tagged with a direction.
      version('av_other', {
        tags: { 'lib_t:direction': 'dir-3', 'lib_t:scene': 'night' },
      }),
      version('av_lost', { tags: { 'lib_t:direction': 'dir-7' } }),
    ];
    const groups = buildDirectionGroups(directions, items, versions);
    const byKey = Object.fromEntries(groups.map((g) => [g.key, g]));
    expect(groupVersionIds(byKey['dir-1'])).toEqual(['av_new', 'av_i1']);
    expect(groupVersionIds(byKey['dir-3'])).toEqual(['av_other']);
    expect(byKey['dir-3'].items).toEqual([]);
    expect(groupVersionIds(byKey[UNMATCHED_GROUP_KEY])).toEqual(['av_lost']);
  });

  it('keeps a removed direction that already has images', () => {
    const groups = buildDirectionGroups(
      [direction('dir-1', '雨夜车库', { removed: true })],
      [item('i1', 0, 'dir-1')],
      []
    );
    expect(groups).toHaveLength(1);
    expect(groups[0].direction?.removed).toBe(true);
  });

  it('is empty for a Run with nothing in it', () => {
    expect(buildDirectionGroups(directions, [], [])).toEqual([]);
  });
});

describe('counts', () => {
  it('buckets every RunItem state into queued / running / done / failed', () => {
    expect(
      [
        'pending',
        'queued',
        'paused',
        'running',
        'qc_pending',
        'uncertain',
        'succeeded',
        'reused',
        'failed',
        'cancelled',
        'brand_new',
      ].map(statusBucket)
    ).toEqual([
      'queued',
      'queued',
      'queued',
      'running',
      'running',
      'running',
      'done',
      'done',
      'failed',
      'failed',
      'queued',
    ]);
    expect(
      countStatuses([
        { state: 'failed' },
        { state: 'succeeded' },
        { state: 'pending' },
      ])
    ).toEqual({
      queued: 1,
      running: 0,
      done: 1,
      failed: 1,
      total: 3,
    });
  });

  it('counts a missing or cleared review as unreviewed', () => {
    expect(
      countReviews([
        { effective_review_value: 'approved' },
        { effective_review_value: 'needs_changes' },
        { effective_review_value: 'rejected' },
        { effective_review_value: null },
        { effective_review_value: 'cleared' },
      ])
    ).toEqual({
      approved: 1,
      needs_changes: 1,
      rejected: 1,
      unreviewed: 2,
      total: 5,
    });
  });
});
