import { useState } from 'react';
import { useTranslation } from 'next-i18next';
import type { PlanRunItem, TestPlanDetail } from '@/lib/test-plans/plan-model';

interface PlanInternalViewProps {
  plan: TestPlanDetail;
  unmatched: PlanRunItem[];
}

/**
 * "内部视图" (contract §1 principle 2): plan / Run / Operation ids,
 * signatures, factor keys, the frozen defaults and drafting evidence, for
 * whoever has to debug a plan. Off by default and not even rendered until
 * opened, so the ids never reach the page text otherwise.
 */
export function PlanInternalView({ plan, unmatched }: PlanInternalViewProps) {
  const { t } = useTranslation('tests');
  const [open, setOpen] = useState(false);

  const snapshot = open
    ? {
        plan_id: plan.plan_id,
        revision: plan.revision,
        status: plan.status,
        operation_id: plan.operation_id,
        product_id: plan.product_id,
        product_entry_id: plan.product_entry_id,
        product_version_id: plan.product_version_id,
        planner_version: plan.planner_version,
        scale: plan.scale,
        seed_asset_version_ids: plan.seed_asset_version_ids,
        failure: plan.failure,
        factors: (plan.factors ?? []).map((factor) => ({
          key: factor.key,
          values: (factor.values ?? []).map(
            (value) =>
              `${value.id}${value.proposed_by ? ` (${value.proposed_by})` : ''}`
          ),
        })),
        constraints: plan.constraints,
        directions: (plan.directions ?? []).map((direction) => ({
          direction_id: direction.direction_id,
          removed: direction.removed,
          hypothesis: direction.hypothesis,
          factor_values: direction.factor_values,
          selected_in: direction.selected_in,
          combination_signatures: direction.combination_signatures,
        })),
        stages: plan.stages,
        expansion: plan.expansion,
        runs: plan.runs,
        defaults: plan.defaults,
        drafting: plan.drafting,
        unmatched_items: unmatched.map((item) => ({
          id: item.id,
          state: item.state,
          fingerprint: item.combination_fingerprint,
        })),
      }
    : null;

  return (
    <section className="border-t border-base-300 pt-4">
      <button
        type="button"
        className="btn btn-ghost btn-xs text-base-content/60"
        aria-expanded={open}
        aria-controls="plan-internal-view"
        onClick={() => setOpen((value) => !value)}
      >
        {open ? t('internal-toggle-hide') : t('internal-toggle-show')}
      </button>
      {open && snapshot && (
        <div
          id="plan-internal-view"
          className="mt-2 space-y-2"
          data-testid="plan-internal-view"
        >
          <h2 className="text-sm font-semibold">{t('internal-title')}</h2>
          <p className="text-xs text-base-content/60">{t('internal-intro')}</p>
          <pre className="max-h-[32rem] overflow-auto rounded-box bg-base-200 p-3 text-xs leading-relaxed">
            {JSON.stringify(snapshot, null, 2)}
          </pre>
        </div>
      )}
    </section>
  );
}
