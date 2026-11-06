import type { components } from '@/lib/domain-client/generated/domain-v1';

type Plan = components['schemas']['FrozenTestPlan'];
type Combination = Plan['combinations'][number];

// Plans frozen before executions per combination existed generated one image each.
export const LEGACY_EXECUTIONS = 1;

export function plannedExecutions(plan: Plan, combination: Combination) {
  return (
    combination.executions ??
    plan.executions_per_combination ??
    LEGACY_EXECUTIONS
  );
}

// What the cost previews will cover: N combinations × M executions.
export function executionSummary(plan: Plan) {
  const counts = plan.combinations.map((c) => plannedExecutions(plan, c));
  return {
    combinations: counts.length,
    executions: new Set(counts).size === 1 ? counts[0] : null,
    images: counts.reduce((sum, n) => sum + n, 0),
  };
}
