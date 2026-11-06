import { useEffect, useMemo, useState } from 'react';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import {
  buildDirectionGroups,
  type DirectionGroup,
} from '@/lib/test-plans/direction-groups';
import type { RunStage, TestPlanDetail } from '@/lib/test-plans/plan-model';
import { useRunItems } from './useRunItems';
import {
  useRunAssetVersions,
  type RunAssetVersion,
} from './useRunAssetVersions';

export type RunPlanStatus = 'idle' | 'loading' | 'none' | 'found';

export interface UseRunPlanResult {
  status: RunPlanStatus;
  plan: TestPlanDetail | null;
  /** Which stage of the plan this Run is (样图 / 完整测试). */
  stage: RunStage | null;
}

/**
 * The test plan a generation Run came from (`list_test_plans?run_id`, then
 * `get_test_plan`). `none` for a Run made in the advanced workbench -- and
 * also when either read fails: the pages then keep their ungrouped view,
 * which is always correct, rather than show an error for an optional view.
 */
export function useRunPlan(runId: string | null | undefined): UseRunPlanResult {
  const { current } = useWorkspace();
  const slug = current.slug;
  const gateway = useDomainGateway();
  const [state, setState] = useState<UseRunPlanResult>({
    status: 'idle',
    plan: null,
    stage: null,
  });

  useEffect(() => {
    if (!runId) {
      setState({ status: 'idle', plan: null, stage: null });
      return;
    }
    let cancelled = false;
    setState({ status: 'loading', plan: null, stage: null });
    const none = () => {
      if (!cancelled) setState({ status: 'none', plan: null, stage: null });
    };
    (async () => {
      const listed = await gateway.listTestPlans({
        workspaceSlug: slug,
        runId,
        limit: 1,
      });
      if (cancelled) return;
      const planId =
        listed.kind === 'ok'
          ? (listed.data as { items?: { plan_id: string }[] }).items?.[0]
              ?.plan_id
          : undefined;
      if (!planId) return none();
      const detail = await gateway.getTestPlan({ workspaceSlug: slug, planId });
      if (cancelled) return;
      if (detail.kind !== 'ok') return none();
      const plan = detail.data as TestPlanDetail;
      const stage =
        ((plan.runs ?? []).find((run) => run.run_id === runId)?.stage as
          | RunStage
          | undefined) ?? null;
      setState({ status: 'found', plan, stage });
    })().catch(none);
    return () => {
      cancelled = true;
    };
  }, [gateway, runId, slug]);

  return state;
}

export interface UseRunDirectionGroupsResult extends UseRunPlanResult {
  groups: DirectionGroup<RunAssetVersion>[];
  /** Items or versions are still on their first read. */
  isLoading: boolean;
  /** A page of items or versions could not be read; the groups may be
   * incomplete. */
  readFailed: boolean;
}

/**
 * The Run's items and AssetVersions grouped by the directions of its plan
 * (`buildDirectionGroups`). Items and versions are only read once the Run
 * is known to come from a plan. `version` refetches both: the Run page
 * passes the Run's progress, the Assets page bumps it after a review.
 */
export function useRunDirectionGroups(
  runId: string | null | undefined,
  version: string
): UseRunDirectionGroupsResult {
  const runPlan = useRunPlan(runId);
  const groupedRunId = runPlan.status === 'found' ? runId : null;
  const items = useRunItems(groupedRunId, version);
  const assets = useRunAssetVersions(groupedRunId, version);

  const groups = useMemo(
    () =>
      runPlan.plan
        ? buildDirectionGroups(
            runPlan.plan.directions ?? [],
            items.items,
            assets.versions
          )
        : [],
    [runPlan.plan, items.items, assets.versions]
  );

  const firstRead =
    (items.isLoading && items.items.length === 0) ||
    (assets.isLoading && assets.versions.length === 0);

  return {
    ...runPlan,
    groups,
    isLoading: firstRead,
    readFailed: items.failed || assets.failed,
  };
}
