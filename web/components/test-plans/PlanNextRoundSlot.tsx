import { PlanResults } from '@/components/learnings/PlanResults';
import type { TestPlanDetail } from '@/lib/test-plans/plan-model';

/**
 * Mount point of the results page's former main action "按结论生成下一轮"
 * (front-flow contract §3.3, Issue #166). Since the legacy flow was frozen
 * (#337) it only shows this round's results per direction and factor
 * value: no results import, no next round.
 */
export interface PlanNextRoundSlotProps {
  plan: TestPlanDetail;
  /** `sample` after the sample Run only, `full` once the full test ran. */
  settledStage: 'sample' | 'full';
}

export function PlanNextRoundSlot({ plan }: PlanNextRoundSlotProps) {
  return <PlanResults planId={plan.plan_id} />;
}
