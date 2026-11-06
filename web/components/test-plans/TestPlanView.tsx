import { useMemo } from 'react';
import Link from 'next/link';
import { useTranslation } from 'next-i18next';
import { ArrowLeftIcon, ChevronDownIcon } from '@heroicons/react/24/outline';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useTestPlan } from '@/hooks/test-plans/useTestPlan';
import { useRunItems } from '@/hooks/test-plans/useRunItems';
import {
  activeDirections,
  directionImages,
  groupItemsByDirection,
  isRunActive,
  isRunSettled,
  planPhase,
  stageRun,
  type RunStage,
  type TestPlanDetail,
  type TestPlanRunView,
} from '@/lib/test-plans/plan-model';
import { DirectionCard, type DirectionImage } from './DirectionCard';
import { LegacyTestsNotice } from './LegacyTestsNotice';
import { PlanInternalView } from './PlanInternalView';
import { PlanNextRoundSlot } from './PlanNextRoundSlot';
import { PlanProblemAlert } from './PlanProblemAlert';
import { errorCodeMessage } from '@/lib/i18n/domain-errors';

function runVersion(run: TestPlanRunView | undefined): string {
  if (!run) return '';
  return [run.status, run.run_status, run.items_done, run.items_failed].join(
    ':'
  );
}

/** The scale "展开完整测试" filled in (`app.plans.service.expansion_scale`). */
function expansionScale(plan: TestPlanDetail): 'small' | 'full' {
  return plan.scale === 'small' ? 'small' : 'full';
}

/** In-page anchor of the n-th (1-based) active direction card; ordinal,
 * so no internal id reaches the URL. */
function directionAnchor(position: number): string {
  return `plan-direction-${position}`;
}

/** What the plan covers: its directions, its size and what goes untested. */
function PlanSummary({ plan }: { plan: TestPlanDetail }) {
  const { t } = useTranslation('tests');
  const untested = plan.stages?.[expansionScale(plan)]?.untested?.summary;
  return (
    <section
      className="rounded-box border border-base-300 px-4 py-3"
      aria-label={t('totals-label')}
      data-testid="plan-totals"
    >
      <p className="flex flex-wrap gap-x-4 gap-y-1 font-semibold">
        <span>
          {t('totals-directions', { count: activeDirections(plan).length })}
        </span>
        <span className="font-normal text-base-content/70">
          {t(`scale-${plan.scale}`, { defaultValue: plan.scale })}
        </span>
      </p>
      {untested && (
        <p
          className="mt-2 max-w-prose text-sm text-base-content/70"
          data-testid="untested-summary"
        >
          {untested}
        </p>
      )}
    </section>
  );
}

function RunResultsSummary({
  plan,
  slug,
  unmatched,
}: {
  plan: TestPlanDetail;
  slug: string;
  unmatched: number;
}) {
  const { t } = useTranslation('tests');
  const runs = (['sample', 'full'] as RunStage[])
    .map((stage) => ({ stage, run: stageRun(plan, stage) }))
    .filter(({ run }) => run && run.status === 'submitted' && run.run_id);
  if (runs.length === 0) return null;
  const fullRun = stageRun(plan, 'full');
  const settledStage: 'sample' | 'full' | null = isRunSettled(fullRun)
    ? 'full'
    : isRunSettled(stageRun(plan, 'sample'))
      ? 'sample'
      : null;

  return (
    <section className="space-y-3" aria-labelledby="plan-results-title">
      <h2 id="plan-results-title" className="text-lg font-semibold">
        {t('results-title')}
      </h2>
      <ul className="space-y-2">
        {runs.map(({ stage, run }) => (
          <li
            key={stage}
            className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-base-300 px-4 py-2 text-sm"
          >
            <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <span className="font-medium">
                {t(
                  stage === 'sample' ? 'results-sample-run' : 'results-full-run'
                )}
              </span>
              <span>
                {t('results-progress', {
                  done: run!.items_done,
                  total: run!.items_total,
                })}
              </span>
              {run!.items_failed > 0 && (
                <span className="text-error">
                  {t('results-failed', { count: run!.items_failed })}
                </span>
              )}
              {isRunActive(run) && (
                <span
                  className="loading loading-dots loading-xs"
                  aria-hidden="true"
                />
              )}
            </span>
            <Link
              href={`/w/${slug}/assets?run=${encodeURIComponent(run!.run_id!)}&reviewMode=1`}
              className="link link-primary"
            >
              {t('results-review-all')}
            </Link>
          </li>
        ))}
      </ul>
      {unmatched > 0 && (
        <p className="text-sm text-base-content/60">
          {t('results-unmatched', { count: unmatched })}
        </p>
      )}
      {settledStage && (
        <PlanNextRoundSlot plan={plan} settledStage={settledStage} />
      )}
    </section>
  );
}

/**
 * `/w/{slug}/tests/{planId}` (Issue #165, contract §3.2 / §3.3), read-only
 * since the legacy flow was frozen (#337): the drafted direction cards
 * (with their sample and full-test images), the deleted ones, what the
 * plan covers and leaves untested, the Runs with links to Assets review
 * and this round's results. Nothing here revises the plan, starts a Run,
 * imports results or drafts again; a notice points to the marketing test.
 */
export function TestPlanView({ planId }: { planId: string }) {
  const { t } = useTranslation('tests');
  const { current } = useWorkspace();
  const slug = current.slug;
  const hook = useTestPlan(planId);
  const { plan } = hook;

  const sampleRun = stageRun(plan, 'sample');
  const fullRun = stageRun(plan, 'full');
  const sampleRunId =
    sampleRun?.status === 'submitted' ? sampleRun.run_id : null;
  const fullRunId = fullRun?.status === 'submitted' ? fullRun.run_id : null;
  const sampleItems = useRunItems(sampleRunId, runVersion(sampleRun));
  const fullItems = useRunItems(fullRunId, runVersion(fullRun));

  const grouped = useMemo(() => {
    const directions = plan?.directions ?? [];
    return {
      sample: groupItemsByDirection(directions, sampleItems.items),
      full: groupItemsByDirection(directions, fullItems.items),
    };
  }, [plan?.directions, sampleItems.items, fullItems.items]);

  if (!plan) {
    if (hook.loadProblem) {
      const notFound =
        hook.loadProblem.status === 404 || hook.loadProblem.status === 403;
      return (
        <div className="mx-auto max-w-4xl space-y-4">
          <BackLink slug={slug} />
          <div className="alert alert-error" role="alert">
            <span>{notFound ? t('plan-not-found') : t('plan-load-error')}</span>
            {!notFound && (
              <button
                type="button"
                className="btn btn-sm"
                onClick={hook.refresh}
              >
                {t('retry')}
              </button>
            )}
          </div>
        </div>
      );
    }
    return (
      <div className="flex justify-center py-12" role="status">
        <span className="loading loading-spinner" aria-hidden="true" />
        <span className="sr-only">{t('plan-loading')}</span>
      </div>
    );
  }

  const phase = planPhase(plan);
  const productName = plan.product_name?.trim() || t('unnamed-product');
  const directions = plan.directions ?? [];
  const active = directions.filter((direction) => !direction.removed);
  const removed = directions.filter((direction) => direction.removed);
  const target = expansionScale(plan);
  const failedRun = [sampleRun, fullRun].find(
    (run) => run?.status === 'failed'
  );

  const imagesFor = (directionId: string): DirectionImage[] => [
    ...(grouped.sample.byDirection.get(directionId) ?? []).map((item) => ({
      item,
      runId: sampleRunId!,
    })),
    ...(grouped.full.byDirection.get(directionId) ?? []).map((item) => ({
      item,
      runId: fullRunId!,
    })),
  ];
  const imagesHeading = fullRunId
    ? t('direction-results')
    : sampleRunId
      ? t('direction-samples')
      : null;

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <div>
        <BackLink slug={slug} />
        <h1 className="mt-2 text-2xl font-bold outline-none">
          {t('plan-title', { name: productName })}
        </h1>
        <p className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-sm text-base-content/70">
          <span>
            {t('plan-meta-goal', {
              goal: t(`goal-${plan.goal}`, { defaultValue: plan.goal }),
            })}
          </span>
          {plan.audience?.text && (
            <span>
              {t('plan-meta-audience', { audience: plan.audience.text })}
            </span>
          )}
        </p>
      </div>

      <LegacyTestsNotice />

      {hook.stalled && (
        <div className="alert text-sm" role="status">
          <span>{t('plan-stalled')}</span>
          <button
            type="button"
            className="btn btn-sm"
            onClick={hook.resumePolling}
          >
            {t('plan-keep-waiting')}
          </button>
        </div>
      )}

      {phase === 'drafting' && (
        <section
          className="space-y-4"
          aria-busy="true"
          data-testid="plan-drafting"
        >
          <div role="status">
            <h2 className="text-lg font-semibold">
              {t('plan-drafting-title')}
            </h2>
            <p className="mt-1 max-w-prose text-sm text-base-content/70">
              {t('plan-drafting-body')}
            </p>
          </div>
          <ul className="grid gap-4 md:grid-cols-2" aria-hidden="true">
            {[0, 1, 2].map((index) => (
              <li
                key={index}
                className="h-48 rounded-box border border-base-300 bg-base-200/60 motion-safe:animate-pulse"
              />
            ))}
          </ul>
        </section>
      )}

      {phase === 'failed' && (
        <section
          className="space-y-3 rounded-box border border-error/40 p-5"
          role="alert"
        >
          <h2 className="text-lg font-semibold">{t('plan-failed-title')}</h2>
          {plan.failure?.code && (
            <p className="text-sm">{errorCodeMessage(t, plan.failure.code)}</p>
          )}
        </section>
      )}

      {plan.status === 'ready' && (
        <>
          {failedRun?.failure?.code && (
            <PlanProblemAlert code={failedRun.failure.code} fixable={false} />
          )}

          <RunResultsSummary
            plan={plan}
            slug={slug}
            unmatched={
              grouped.sample.unmatched.length + grouped.full.unmatched.length
            }
          />

          <section className="space-y-4" aria-labelledby="directions-title">
            <div>
              <h2 id="directions-title" className="text-lg font-semibold">
                {t('directions-title')}
              </h2>
            </div>
            <ul className="grid gap-4 md:grid-cols-2">
              {active.map((direction, index) => (
                <DirectionCard
                  key={direction.direction_id}
                  anchorId={directionAnchor(index + 1)}
                  direction={direction}
                  factors={plan.factors ?? []}
                  sampleImages={directionImages(direction, plan.stages?.sample)}
                  expansionImages={directionImages(
                    direction,
                    plan.stages?.[target]
                  )}
                  expansionLabel={t(`scale-${target}`)}
                  images={imagesFor(direction.direction_id)}
                  imagesHeading={imagesHeading}
                  // Read-only (#337): no edit or delete control renders.
                  editable={false}
                  busy={false}
                  onRemove={() => undefined}
                  onSaveEdit={async () => false}
                />
              ))}
            </ul>
            {removed.length > 0 && (
              <details className="group rounded-box border border-base-300 px-4 py-2">
                <summary className="flex cursor-pointer list-none items-center gap-1 text-sm text-base-content/70">
                  <ChevronDownIcon
                    className="h-4 w-4 transition-transform group-open:rotate-180"
                    aria-hidden="true"
                  />
                  {t('removed-title', { count: removed.length })}
                </summary>
                <ul className="mt-2 divide-y divide-base-200">
                  {removed.map((direction) => (
                    <li
                      key={direction.direction_id}
                      className="flex items-center justify-between gap-3 py-2 text-sm"
                      data-testid="removed-direction"
                    >
                      <span className="text-base-content/70 line-through">
                        {direction.title}
                      </span>
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </section>

          <PlanSummary plan={plan} />
        </>
      )}

      <PlanInternalView
        plan={plan}
        unmatched={[...grouped.sample.unmatched, ...grouped.full.unmatched]}
      />
    </div>
  );
}

function BackLink({ slug }: { slug: string }) {
  const { t } = useTranslation('tests');
  return (
    <Link
      href={`/w/${slug}/tests`}
      className="link-hover link inline-flex items-center gap-1 text-sm text-base-content/70"
    >
      <ArrowLeftIcon className="h-4 w-4" aria-hidden="true" />
      {t('plan-back')}
    </Link>
  );
}
