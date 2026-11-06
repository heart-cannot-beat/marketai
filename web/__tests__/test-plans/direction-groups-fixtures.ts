import { getI18n } from 'react-i18next';
import directions from '../../locales/en/directions.json';
import type { DirectionView, PlanRunItem } from '@/lib/test-plans/plan-model';

/** jest.setup.js loads common/assets/products/tests; these suites add the
 * grouped view's own namespace. */
export function loadDirectionsNamespace() {
  const i18n = getI18n();
  if (!i18n.hasResourceBundle('en', 'directions')) {
    i18n.addResourceBundle('en', 'directions', directions, true, true);
  }
}

export function direction(
  id: string,
  title: string,
  overrides: Partial<DirectionView> = {}
): DirectionView {
  return {
    direction_id: id,
    ordinal: Number(id.slice(-1)),
    title,
    what_it_tests: `${title}是否更打动通勤族`,
    hypothesis: '',
    scene_description: '',
    copy: { headline: '', primary_text: '' },
    factors: [],
    factor_values: { scene: ['night'] },
    combination_count: 1,
    combination_signatures: [`direction=${id}|scene=night`],
    selected_in: { sample: 1, small: 1, full: 1 },
    removed: false,
    user_added: false,
    ...overrides,
  };
}

/** A Run item tagged the way plan Runs are: `<template entry>:<dimension>`. */
export function item(
  id: string,
  ordinal: number,
  directionId: string,
  overrides: Partial<PlanRunItem> = {}
): PlanRunItem {
  return {
    id,
    ordinal,
    state: 'succeeded',
    combination_fingerprint: `lib_t:direction=${directionId}:lib_t:scene=night`,
    variant_index: 0,
    tags: { 'lib_t:direction': directionId, 'lib_t:scene': 'night' },
    bound_asset_version_id: `av_${id}`,
    thumbnail: null,
    ...overrides,
  };
}

export function version(
  id: string,
  overrides: Partial<{
    asset_id: string;
    tags: Record<string, unknown>;
    effective_review_value: string | null;
  }> = {}
) {
  return {
    id,
    asset_id: `asset_${id}`,
    product_id: 'prod_1',
    run_id: 'gr_1',
    provider: 'mock',
    model: 'mock-v1',
    status: 'succeeded',
    aspect_ratio: '1:1',
    tags: {},
    effective_review_value: null as string | null,
    origin_kind: 'generated',
    execution_mode: 'mock',
    byte_size: 10,
    thumbnail: null,
    ...overrides,
  };
}
