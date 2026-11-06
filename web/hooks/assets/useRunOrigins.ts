import { useEffect, useMemo, useState } from 'react';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { buildRunOrigins, type RunOrigin } from '@/lib/assets/run-display';
import type {
  TestPlanDetail,
  TestPlanSummary,
} from '@/lib/test-plans/plan-model';

export interface UseRunOriginsResult {
  /** run id -> the test plan it came from; Runs made in the advanced
   * workbench are absent. */
  origins: ReadonlyMap<string, RunOrigin>;
  isLoading: boolean;
}

const EMPTY: ReadonlyMap<string, RunOrigin> = new Map();

/**
 * Which test plan each listed Run came from, so the Assets rail can name a
 * batch「<产品> 的广告测试 · 样图」instead of its `gr_` id (Issue #191).
 *
 * `list_test_plans` carries no Run ids, so this reads the Workspace's plans
 * once and then only the plans that have submitted a stage. A failed read
 * leaves those Runs unmatched -- they then read as workbench batches, which
 * still never shows an id.
 */
export function useRunOrigins(runIds: readonly string[]): UseRunOriginsResult {
  const { current } = useWorkspace();
  const slug = current.slug;
  const gateway = useDomainGateway();
  const runKey = useMemo(() => [...runIds].sort().join(','), [runIds]);
  const [state, setState] = useState<UseRunOriginsResult>({
    origins: EMPTY,
    isLoading: false,
  });

  useEffect(() => {
    if (!runKey) {
      setState({ origins: EMPTY, isLoading: false });
      return;
    }
    let cancelled = false;
    setState((previous) => ({ ...previous, isLoading: true }));
    (async () => {
      const listed = await gateway.listTestPlans({
        workspaceSlug: slug,
        limit: 100,
      });
      if (cancelled) return;
      const summaries =
        listed.kind === 'ok'
          ? ((listed.data as { items?: TestPlanSummary[] }).items ?? [])
          : [];
      const withRuns = summaries.filter(
        (plan) => (plan.run_stages ?? []).length > 0
      );
      const details = await Promise.all(
        withRuns.map(async (plan) => {
          const detail = await gateway.getTestPlan({
            workspaceSlug: slug,
            planId: plan.plan_id,
          });
          return detail.kind === 'ok' ? (detail.data as TestPlanDetail) : null;
        })
      );
      if (cancelled) return;
      setState({
        origins: buildRunOrigins(
          details.filter((detail): detail is TestPlanDetail => !!detail)
        ),
        isLoading: false,
      });
    })().catch(() => {
      if (!cancelled) setState({ origins: EMPTY, isLoading: false });
    });
    return () => {
      cancelled = true;
    };
  }, [gateway, runKey, slug]);

  return state;
}
