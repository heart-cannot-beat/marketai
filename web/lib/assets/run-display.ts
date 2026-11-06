/**
 * How a generation Run (批次) is named on the Assets page rail (Issue #191):
 * never by its `gr_` id, but by product, origin and time --
 *
 *   来自测试方案:  「Grizzl-E Classic 的广告测试 · 样图」 / 4 个方向 · 10/12 14:05
 *   高级工作台:    「Grizzl-E Classic」 / 高级工作台 · 10/12 14:05
 *
 * plus a Chinese status and done/total. The id is only a tooltip.
 */
import type { useTranslation } from 'next-i18next';
import type { TestPlanDetail } from '@/lib/test-plans/plan-model';
import { formatRunTime, runStatus } from './status-labels';

export interface RunListEntry {
  id: string;
  product_id?: string | null;
  status?: string | null;
  total?: number | null;
  done?: number | null;
  created_at?: string | null;
}

/** The test plan a Run was submitted from. */
export interface RunOrigin {
  planId: string;
  productName: string;
  /** `sample` / `full`, or whatever stage the plan recorded. */
  stage: string | null;
  /** Directions still in the plan (removed ones excluded). */
  directionCount: number;
}

/** run id -> origin, from the plans' linked Runs (`TestPlanDetail.runs`). */
export function buildRunOrigins(
  plans: Pick<
    TestPlanDetail,
    'plan_id' | 'product_name' | 'directions' | 'runs'
  >[]
): Map<string, RunOrigin> {
  const origins = new Map<string, RunOrigin>();
  for (const plan of plans) {
    const directionCount = (plan.directions ?? []).filter(
      (direction) => !direction.removed
    ).length;
    for (const run of plan.runs ?? []) {
      if (!run.run_id || origins.has(run.run_id)) continue;
      origins.set(run.run_id, {
        planId: plan.plan_id,
        productName: plan.product_name?.trim() ?? '',
        stage: run.stage ?? null,
        directionCount,
      });
    }
  }
  return origins;
}

type Translate = ReturnType<typeof useTranslation>['t'];

export interface RunDisplay {
  title: string;
  /** 「N 个方向 · MM/DD HH:mm」 or 「高级工作台 · MM/DD HH:mm」. */
  subtitle: string;
  statusLabel: string;
  badgeClass: string;
  done: number;
  total: number;
  /** Accessible name: title, status and progress -- no id. */
  ariaLabel: string;
}

/**
 * @param productName the product's display name for a Run with no plan:
 *   `undefined` while the product list loads, `null` when unknown.
 * @param t translator over the `assets` namespace.
 * @param tc translator over the `common` namespace (status labels).
 */
export function describeRun(
  run: RunListEntry,
  origin: RunOrigin | undefined,
  productName: string | null | undefined,
  t: Translate,
  tc: Translate
): RunDisplay {
  const status = runStatus(run.status);
  const statusLabel = tc(status.labelKey);
  const done = run.done ?? 0;
  const total = run.total ?? 0;
  const time = formatRunTime(run.created_at);

  let title: string;
  let source: string;
  if (origin) {
    const name = origin.productName || t('run-unnamed-product');
    const stageKey =
      origin.stage === 'sample' || origin.stage === 'full'
        ? `run-stage-${origin.stage}`
        : 'run-stage-other';
    title = `${t('run-plan-title', { name })} · ${t(stageKey)}`;
    source = t('run-directions', { count: origin.directionCount });
  } else {
    title =
      productName === undefined
        ? t('run-product-loading')
        : productName || t('run-unnamed-product');
    source = t('run-source-workbench');
  }

  return {
    title,
    subtitle: time ? `${source} · ${time}` : source,
    statusLabel,
    badgeClass: status.badgeClass,
    done,
    total,
    ariaLabel: t('run-label', { title, status: statusLabel, done, total }),
  };
}
