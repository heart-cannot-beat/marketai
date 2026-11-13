import {
  DomainProblemError,
  errorMessageOf,
  type DomainProblem,
} from '@/lib/i18n/domain-errors';
import { metricsIssueText } from './marketing-issues';
import { MarketingImageEdit } from './MarketingImageEdit';
import { MarketingImageTools } from './MarketingImageTools';
import { MarketingVersionHistory } from './MarketingVersionHistory';
import { MarketingEvidenceHandoff } from './MarketingEvidenceHandoff';
import {
  MarketingBoard,
  type BoardActions,
  type BoardExecution,
} from './MarketingBoard';
import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'next-i18next';
import useSWR from 'swr';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { useWorkspace } from './WorkspaceProvider';
import {
  MarketingAIQCPanel,
  MarketingCreativeReview as CreativeReview,
} from './MarketingAIQC';
import {
  MarketingNextPlan,
  MarketingReusePicker,
} from './MarketingContinuation';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import {
  MarketingFeedbackForm,
  MarketingExplanationPanel,
} from './MarketingFollowup';
import {
  boardExecutions,
  MainActionProvider,
  roundFocus,
  useEmphasis,
  type RoundFocus,
} from './marketing-focus';
import { CopyValue, TechnicalDetails } from './MarketingTechnicalInfo';
import { isCompetitorKind } from './MarketingPlanReview';

type Project = components['schemas']['MarketingProjectDetail'];
type Protocol = components['schemas']['FreezeRound']['protocol'];
type Evaluation = components['schemas']['WorkflowEvaluation'];
export type MarketingRound = components['schemas']['MarketingRound'];
type Workflow = components['schemas']['WorkflowEnvelope'];
type Reporting = components['schemas']['PlannerCapabilities'];
type Attribution = components['schemas']['FreezeRound']['attribution_window'];
type Translate = ReturnType<typeof useTranslation>['t'];
type HandoffEntry = components['schemas']['HandoffEntry'];
/**
 * An exported entry's name as the package's ads.csv writes it
 * (`export_text.ads_rows`): the display name, with its execution number and
 * version ("… · Execution 1 v2") in the package language, else the ad name,
 * whose `#1v2` suffix says the same. Angle and expression alone cannot tell
 * two executions, or a revision, of one combination apart (#162).
 */
const exportedName = (entry: HandoffEntry) =>
  entry.display_name || entry.ad_name;
/** `common:marketing-attribution-window-<window>` (`state-codes.spec.ts`). */
const attributionLabel = (t: Translate, window: string) =>
  t(`marketing-attribution-window-${window}`);
const INPUT =
  'w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm';
const BUTTON =
  'rounded-lg bg-gray-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40';
const SECONDARY =
  'rounded-lg border border-gray-300 px-4 py-2 text-sm disabled:opacity-40';

function metricValue(metric: string, value: string | number) {
  return metric === 'ctr'
    ? `${(Number(value) * 100).toFixed(2)}%`
    : Number(value).toFixed(2);
}

function valueOf<T>(r: {
  kind?: string;
  data?: T;
  problem?: DomainProblem;
}): T {
  if (r?.kind === 'problem') throw new DomainProblemError(r.problem);
  if (!r || r.data === undefined)
    throw new Error('marketing-action-needs-reconciliation');
  return r.data;
}

const PROJECTED_LANGUAGE_KEYS = [
  'ad_language',
  'language_confirmation_required',
  'strategy_language',
] as const;

const without = (item: object, key: string): Record<string, unknown> =>
  Object.fromEntries(Object.entries(item).filter(([name]) => name !== key));

/**
 * The project's plan as the current round's read returns the plan it froze,
 * so the two compare key for key. Each step drops only what the frozen plan
 * lacks and keeps the other keys in order; a projection added to the round
 * read (`app/marketing/workflow.py` `round_dict`) belongs here too.
 * - A plan from before J-02 reads with an inferred advertising language its
 *   frozen snapshot never had (#310).
 * - A round never shows its plan's competitor review (#273): the citation,
 *   each angle's overlap, the findings' observations and the competitor
 *   findings, as `competitor_review.legacy_projection` drops them (#367).
 */
function asFrozen(
  plan: Project['plan'],
  frozen: MarketingRound['snapshot']['plan']
) {
  if (!plan) return plan;
  let result: Record<string, unknown> = { ...plan };
  if (plan.language_confirmation_required && !frozen.ad_language)
    for (const key of PROJECTED_LANGUAGE_KEYS)
      if (!(key in frozen)) delete result[key];
  if (!('competitor_snapshot_id' in frozen))
    result = without(result, 'competitor_snapshot_id');
  if (!frozen.angles.some((angle) => 'competitor_overlap' in angle))
    result.angles = plan.angles.map((angle) =>
      without(angle, 'competitor_overlap')
    );
  const frozenReview = frozen.plan_review ?? [];
  if (plan.plan_review) {
    let review: object[] = plan.plan_review;
    if (!frozenReview.some((item) => isCompetitorKind(item.kind)))
      review = plan.plan_review.filter((item) => !isCompetitorKind(item.kind));
    if (!frozenReview.some((item) => 'observations' in item))
      review = review.map((item) => without(item, 'observations'));
    result.plan_review = review;
  }
  return result;
}

const RUNNING = ['queued', 'running'];

/**
 * Whether a round read shows a job still running, from its state fields only:
 * an AI check, the export, the explanation, an image edit or an image being
 * generated. An `uncertain` call waits for a person, not for a poll.
 */
function showsRunningJob(round: MarketingRound | null | undefined) {
  if (!round) return false;
  return (
    round.assets.some((asset) =>
      [asset.ai_qc, asset.paired_ai_qc].some((call) =>
        RUNNING.includes(call?.state ?? '')
      )
    ) ||
    RUNNING.includes(round.handoff?.state ?? '') ||
    RUNNING.includes(round.explanation?.state ?? '') ||
    (round.image_edits ?? []).some((edit) => edit.state === 'queued') ||
    boardExecutions(round).some((execution) => execution.stage === 'generating')
  );
}

function Input({
  label,
  value,
  onChange,
  type = 'text',
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  type?: string;
}) {
  const id = useId();
  return (
    <label htmlFor={id} className="block space-y-1 text-sm">
      <span>{label}</span>
      <input
        id={id}
        className={INPUT}
        type={type}
        step={type === 'number' ? 'any' : undefined}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  );
}

export function MarketingExecution({
  project,
  onRoundStateChange,
  onRoundPlanRevisionChange,
  onFocusChange,
  openRequest,
  onFeedbackDraft,
  onNextPlan,
  reporting,
}: {
  project: Project;
  reporting?: Reporting;
  onNextPlan?: (
    body: components['schemas']['app__marketing__api__PlanRequest']
  ) => Promise<void>;
  onRoundStateChange?: (state: string | null) => void;
  /** The plan revision the current round froze: `undefined` until the
   * workflow is read, `null` without a round or when it cannot be read. */
  onRoundPlanRevisionChange?: (revision: number | null | undefined) => void;
  /** The round's next step, for the page's focus bar (J-32). */
  onFocusChange?: (focus: RoundFocus | null) => void;
  /** Opens one image's drawer when the focus bar asks for it. */
  openRequest?: { key: string; nonce: number } | null;
  onFeedbackDraft?: (draftId: string) => void;
}) {
  const { t, i18n } = useTranslation('common');
  const emphasis = useEmphasis();
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const [actionBusy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(false);
  const [history, setHistory] = useState<MarketingRound | null>(null);
  const [downloadUrl, setDownloadUrl] = useState('');
  const [downloadRoundId, setDownloadRoundId] = useState('');
  const [excludedAssets, setExcludedAssets] = useState<string[]>([]);
  const editorId = useId();
  const [editing, setEditing] = useState({ id: '', request: 0 });
  const [opened, setOpened] = useState<string | null>(null);
  const lastAction = useRef<(() => Promise<void>) | null>(null);
  const workflow = useSWR<Workflow>(
    ['marketing-workflow', current.workspaceId, project.id],
    async () =>
      valueOf(
        await gateway.getMarketingWorkflow({
          includeLanguages: true,
          includeImageEdits: true,
          includeVersionHistory: true,
          includeBoard: true,
          includeExecutionPlan: true,
          includeQcKinds: true,
          includeBoardThumbnails: true,
          includeTextLanguage: true,
          includeQcEvidence: true,
          // The frozen plan as the project read returns the project's (#367).
          includePlanReview: true,
          includeBrandStyle: true,
          workspaceSlug: current.slug,
          projectId: project.id,
          includePolling: true,
        })
      ),
    {
      // The server's hint, unless the frame it came with still shows a job
      // running: a job finishing while the server reads could end polling on
      // that frame (#307). This read always asks for the hint.
      refreshInterval: (data) =>
        Math.max(
          data?.poll_after_ms ?? 0,
          showsRunningJob(data?.current) ? 3000 : 0
        ),
    }
  );
  const activeRoundState = workflow.data?.current?.state || null;
  // Keep local edits mounted during a failed background read. Cached workflow
  // data is for display until refreshed; it cannot admit a new business action.
  const busy = actionBusy || Boolean(workflow.error);
  useEffect(() => {
    onRoundStateChange?.(activeRoundState);
  }, [activeRoundState, onRoundStateChange]);
  const roundPlanRevision = workflow.data
    ? (workflow.data.current?.plan_revision ?? null)
    : workflow.error
      ? null
      : undefined;
  useEffect(() => {
    onRoundPlanRevisionChange?.(roundPlanRevision);
  }, [roundPlanRevision, onRoundPlanRevisionChange]);
  useEffect(
    () => () => onRoundPlanRevisionChange?.(undefined),
    [onRoundPlanRevisionChange]
  );
  const row = history || workflow.data?.current;
  // Placeholder keys repeat across rounds; a drawer belongs to the round shown.
  useEffect(() => setOpened(null), [row?.id]);
  // What follows the current round: planning the next one, while the project
  // still holds the plan it froze, and starting a round from a confirmed plan
  // it has not frozen. The page and its focus bar read the same answer (#367).
  const latest = workflow.data?.current;
  const nextPlanOpen =
    latest?.state === 'closed' &&
    latest.continuation?.can_plan !== false &&
    latest.close?.reason !== 'completed' &&
    !latest.evaluation?.budget_exhausted &&
    !latest.evaluation?.round_limit_reached &&
    !!onNextPlan &&
    JSON.stringify(asFrozen(project.plan, latest.snapshot.plan)) ===
      JSON.stringify(latest.snapshot.plan);
  const freezeOpen =
    !!workflow.data &&
    (!latest ||
      (latest.state === 'closed' && latest.continuation?.can_plan !== false)) &&
    project.state === 'plan_confirmed' &&
    latest?.plan_revision !== project.revision &&
    latest?.close?.reason !== 'completed' &&
    !latest?.evaluation?.budget_exhausted &&
    !latest?.evaluation?.round_limit_reached;
  // The focus bar follows the current round, never a historical one shown here.
  const focus = roundFocus(workflow.data, latest, {
    nextPlan: nextPlanOpen,
    freeze: freezeOpen,
  });
  const focusKey = JSON.stringify(focus);
  useEffect(() => {
    onFocusChange?.(focus);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusKey, onFocusChange]);
  useEffect(() => () => onFocusChange?.(null), [onFocusChange]);
  useEffect(() => {
    if (!openRequest) return;
    setHistory(null);
    setOpened(openRequest.key);
  }, [openRequest]);
  useEffect(() => {
    if (
      !history ||
      !['queued', 'running'].includes(history.explanation?.state || '')
    )
      return;
    let active = true;
    const timer = setTimeout(() => {
      void (async () => {
        try {
          const result = valueOf(
            await gateway.getMarketingRound({
              includeLanguages: true,
              includeImageEdits: true,
              includeVersionHistory: true,
              includeBoard: true,
              includeExecutionPlan: true,
              includeQcKinds: true,
              includeBoardThumbnails: true,
              includeTextLanguage: true,
              includeQcEvidence: true,
              workspaceSlug: current.slug,
              roundId: history.id,
            })
          ) as { data: MarketingRound };
          if (active)
            setHistory((visible) =>
              visible?.id === history.id ? result.data : visible
            );
        } catch {
          if (active) setError(t('marketing-workflow-load-failed'));
        }
      })();
    }, 3000);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [history, current.slug, gateway, t]);
  const exportAssets =
    row?.assets.filter(
      (asset) => asset.ready && !excludedAssets.includes(asset.asset_version_id)
    ) || [];
  const coverageComplete = row?.snapshot.plan.combinations.every((combo) =>
    exportAssets.some((asset) => asset.combination_id === combo.id)
  );
  const params = {
    workspaceSlug: current.slug,
    roundId: row?.id || '',
    expectedRevision: row?.revision,
  };
  function refreshWorkflow() {
    // SWR retains the error for the banner; a read retry must not become an
    // unhandled rejected promise or discard the user's local form state.
    void workflow.mutate().catch(() => undefined);
  }
  async function run(action: (actionId: string) => Promise<unknown>) {
    const actionId = crypto.randomUUID();
    const execute = async () => {
      setBusy(true);
      setError('');
      setRetry(false);
      try {
        const result = valueOf(
          (await action(actionId)) as {
            kind: string;
            data?: { data?: MarketingRound };
          }
        ) as {
          data?: MarketingRound;
        };
        if (history && result.data?.id === history.id) {
          const updated = result.data;
          setHistory((visible) =>
            visible?.id === updated.id ? updated : visible
          );
        }
        await workflow.mutate();
        lastAction.current = null;
      } catch (err) {
        setError(errorMessageOf(t, err));
        setRetry(true);
      } finally {
        setBusy(false);
      }
    };
    lastAction.current = execute;
    await execute();
  }
  function prepareFeedback(body: components['schemas']['PrepareFeedback']) {
    void run(async (actionInstanceId) => {
      const result = valueOf(
        await gateway.prepareMarketingFeedback({
          ...params,
          actionInstanceId,
          body,
        })
      ) as { data: MarketingRound };
      const draft = [...(result.data.feedback_drafts || [])]
        .reverse()
        .find(
          (f) =>
            f.source_asset_version_id === body.asset_version_id &&
            f.request_note === (body.note || '').trim() &&
            f.provider === body.provider &&
            f.model === body.model &&
            f.image_tool?.kind === body.image_tool?.kind &&
            f.image_tool?.source_width === body.image_tool?.source_width &&
            f.image_tool?.source_height === body.image_tool?.source_height &&
            (f.image_tool?.scale || null) ===
              (body.image_tool?.scale || null) &&
            JSON.stringify(f.image_tool?.region || null) ===
              JSON.stringify(body.image_tool?.region || null)
        );
      if (draft) onFeedbackDraft?.(draft.draft_id);
      return result;
    });
  }
  // Round-level controls, shared by the page and the board drawer.
  function exportControls() {
    return (
      <>
        <p className="text-sm">
          {t('marketing-export-selection-count', {
            count: exportAssets.length,
          })}
        </p>
        <button
          className={emphasis('export', BUTTON, SECONDARY)}
          disabled={busy || !coverageComplete || exportAssets.length > 60}
          onClick={() =>
            void run((actionInstanceId) =>
              gateway.createMarketingHandoff({
                ...params,
                actionInstanceId,
                body: {
                  asset_version_ids: exportAssets.map(
                    (a) => a.asset_version_id
                  ),
                },
              })
            )
          }
        >
          {t('marketing-build-meta-package')}
        </button>
        <p className="text-xs text-gray-500">
          {t('marketing-export-coverage-note')}
        </p>
      </>
    );
  }
  function retryExportButton() {
    return (
      <button
        className={emphasis('retry_export', BUTTON, SECONDARY)}
        disabled={busy}
        onClick={() =>
          void run((actionInstanceId) =>
            gateway.retryMarketingHandoff({
              ...params,
              actionInstanceId,
            })
          )
        }
      >
        {t('marketing-retry-export')}
      </button>
    );
  }
  function bindingForm(round: MarketingRound, entryId?: string) {
    return (
      <BindingForm
        row={round}
        busy={busy}
        entryId={entryId}
        onBind={(body) =>
          run((actionInstanceId) =>
            gateway.bindMarketingDelivery({
              ...params,
              actionInstanceId,
              body,
            })
          )
        }
      />
    );
  }
  function metricsForm(round: MarketingRound) {
    return (
      <MetricsForm
        key={
          round.id +
          ':' +
          round.batches
            .filter((b) => b.state === 'confirmed')
            .map((b) => b.id)
            .join(',')
        }
        row={round}
        busy={busy}
        onPreview={(body) =>
          run((actionInstanceId) =>
            gateway.previewMarketingMetrics({
              ...params,
              actionInstanceId,
              body,
            })
          )
        }
        onConfirm={(batchId) =>
          run((actionInstanceId) =>
            gateway.confirmMarketingMetrics({
              ...params,
              actionInstanceId,
              body: { batch_id: batchId },
            })
          )
        }
      />
    );
  }
  function evaluateButton(round: MarketingRound) {
    return (
      <button
        className={emphasis('evaluate', BUTTON, SECONDARY)}
        disabled={busy}
        onClick={() =>
          void run((actionInstanceId) =>
            gateway.evaluateMarketingRound({
              ...params,
              actionInstanceId,
            })
          )
        }
      >
        {t(
          round.state === 'closed'
            ? 'marketing-reevaluate-recorded-data'
            : 'marketing-evaluate-recorded-data'
        )}
      </button>
    );
  }
  function includeInExport(assetVersionId: string) {
    return (
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={!excludedAssets.includes(assetVersionId)}
          onChange={(e) =>
            setExcludedAssets(
              e.target.checked
                ? excludedAssets.filter((id) => id !== assetVersionId)
                : [...excludedAssets, assetVersionId]
            )
          }
        />
        {t('marketing-include-approved-creative')}
      </label>
    );
  }
  // The drawer's primary action follows the server's `next_action`; every
  // other per-image tool stays one click away under "More actions".
  function boardActions(
    round: MarketingRound,
    execution: BoardExecution,
    close: () => void
  ): BoardActions {
    const asset = round.assets.find(
      (a) => a.asset_version_id === execution.asset_version_id
    );
    const editable = !round.handoff && round.state !== 'closed';
    const review = asset && (
      <CreativeReview
        key={
          asset.asset_version_id +
          (asset.quality?.note || '') +
          [asset.ai_qc, asset.paired_ai_qc]
            .map((c) => `${c?.id}:${c?.state}:${c?.current}`)
            .join('|')
        }
        asset={asset}
        row={round}
        busy={busy}
        onReview={(body) =>
          run((actionInstanceId) =>
            gateway.reviewMarketingCreative({
              ...params,
              actionInstanceId,
              body,
            })
          )
        }
      />
    );
    const revise = asset && (
      <MarketingFeedbackForm
        assetVersionId={asset.asset_version_id}
        mode={round.execution_mode}
        busy={busy}
        onPrepare={prepareFeedback}
      />
    );
    const include = asset?.ready && includeInExport(asset.asset_version_id);
    // The drawer shows only the check this image needs next.
    const qcPanel = (only?: 'single' | 'paired') => (
      <MarketingAIQCPanel
        row={round}
        busy={busy}
        onRun={run}
        only={only}
        onResolved={() => {
          void workflow.mutate();
        }}
      />
    );
    const primary: Record<BoardExecution['next_action'], ReactNode> = {
      generate: (
        <button
          className={BUTTON}
          onClick={() => {
            close();
            requestAnimationFrame(() =>
              (
                document.getElementById('marketing-generation-editor') ??
                document.getElementById('marketing-generation-setup')
              )?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
            );
          }}
        >
          {t('marketing-board-open-generation')}
        </button>
      ),
      single_qc: qcPanel('single'),
      paired_qc: qcPanel('paired'),
      ai_qc: qcPanel(),
      review,
      revise,
      export:
        round.handoff?.state === 'failed' ? (
          retryExportButton()
        ) : (
          <div className="space-y-3">
            {include}
            {exportControls()}
          </div>
        ),
      bind: round.handoff && bindingForm(round, execution.delivery?.entry_id),
      import: metricsForm(round),
      evaluate: evaluateButton(round),
      none: null,
    };
    const secondary = asset && editable && (
      <div
        id={editorId + asset.asset_version_id}
        tabIndex={-1}
        className="space-y-3 focus:outline focus:outline-2 focus:outline-gray-400"
      >
        {execution.next_action !== 'export' && include}
        {asset.parent_version_id && (
          <p className="text-sm text-gray-600">
            {t('marketing-image-edit-derived')}
          </p>
        )}
        <MarketingImageEdit
          selected={editing.id === asset.asset_version_id ? editing.request : 0}
          assetVersionId={asset.asset_version_id}
          busy={busy}
          edits={(round.image_edits || []).filter(
            (e) => e.source_asset_version_id === asset.asset_version_id
          )}
          onCreate={(body) =>
            run((actionInstanceId) =>
              gateway.createMarketingImageEdit({
                ...params,
                actionInstanceId,
                body,
              })
            )
          }
        />
        {execution.next_action !== 'review' && review}
        {execution.next_action !== 'revise' && revise}
        <MarketingImageTools
          assetVersionId={asset.asset_version_id}
          mode={round.execution_mode}
          busy={busy}
          onPrepare={prepareFeedback}
        />
        {(round.feedback_drafts || [])
          .filter((f) => f.source_asset_version_id === asset.asset_version_id)
          .map((f, i) => (
            <button
              className={SECONDARY}
              key={f.id}
              onClick={() => onFeedbackDraft?.(f.draft_id)}
            >
              {t('marketing-feedback-open', { number: i + 1 })}
            </button>
          ))}
      </div>
    );
    return {
      primary: (
        <MainActionProvider value={undefined}>
          {primary[execution.next_action]}
        </MainActionProvider>
      ),
      secondary: secondary ? (
        <MainActionProvider value={null}>{secondary}</MainActionProvider>
      ) : undefined,
      secondaryOpen: !!asset && editing.id === asset.asset_version_id,
    };
  }
  if (workflow.error && !workflow.data)
    return (
      <div role="alert" className="space-y-2">
        <p>{t('marketing-workflow-load-failed')}</p>
        <button className={SECONDARY} onClick={refreshWorkflow}>
          {t('marketing-refresh')}
        </button>
      </div>
    );
  if (!workflow.data)
    return <p role="status">{t('marketing-loading-workflow')}</p>;
  if (!row && project.state !== 'plan_confirmed') return null;
  return (
    <section
      className="space-y-6 border-t border-gray-200 pt-8"
      aria-label={t('marketing-delivery-and-results')}
    >
      <header className="space-y-2">
        <h2 className="text-xl font-semibold">
          {t('marketing-delivery-and-results')}
        </h2>
        <p className="text-sm text-gray-600">
          {t('marketing-loop-description')}
        </p>
      </header>
      {workflow.error && (
        <div
          role="alert"
          className="space-y-2 rounded-lg border border-red-200 p-3"
        >
          <p>{t('marketing-workflow-load-failed')}</p>
          <button className={SECONDARY} onClick={refreshWorkflow}>
            {t('marketing-refresh')}
          </button>
        </div>
      )}
      {history && (
        <button className={SECONDARY} onClick={() => setHistory(null)}>
          {t('marketing-back-to-current-round')}
        </button>
      )}
      {error && (
        <div
          role="alert"
          className="space-y-2 rounded-lg border border-red-200 p-3"
        >
          <p>{error}</p>
          {retry && (
            <button
              className={SECONDARY}
              disabled={busy}
              onClick={() => void lastAction.current?.()}
            >
              {t('marketing-retry-the-same-action')}
            </button>
          )}
          <button
            className={SECONDARY}
            disabled={busy}
            onClick={() => {
              if (history)
                void run(() =>
                  gateway.getMarketingRound({
                    includeLanguages: true,
                    includeImageEdits: true,
                    includeVersionHistory: true,
                    includeBoard: true,
                    includeExecutionPlan: true,
                    includeQcKinds: true,
                    includeBoardThumbnails: true,
                    includeTextLanguage: true,
                    includeQcEvidence: true,
                    workspaceSlug: current.slug,
                    roundId: history.id,
                  })
                );
              else refreshWorkflow();
            }}
          >
            {t('marketing-refresh')}
          </button>
        </div>
      )}
      {!history && freezeOpen && (
        <FreezeForm
          reporting={reporting}
          key={project.revision}
          project={project}
          busy={busy}
          onFreeze={(body) =>
            run((actionInstanceId) =>
              gateway.freezeMarketingRound({
                workspaceSlug: current.slug,
                projectId: project.id,
                expectedRevision: project.revision,
                actionInstanceId,
                body,
              })
            )
          }
        />
      )}
      {row && (
        <>
          <div className="space-y-2 border-l-2 border-gray-900 pl-4">
            <p className="font-medium">
              {t('marketing-round-number', { number: row.ordinal })} ·{' '}
              {t(`marketing-state-${row.state}`)}
            </p>
            <p>{row.snapshot.plan.question}</p>
            <p className="text-sm text-gray-600">
              {row.start_date} – {row.end_date} · {row.timezone} ·{' '}
              {t(`marketing-mode-${row.execution_mode}`)} ·{' '}
              {row.snapshot.currency} ·{' '}
              {attributionLabel(t, row.snapshot.attribution_window)}
            </p>
            <p className="text-sm">
              {t('marketing-business-target', {
                metric:
                  row.snapshot.plan.rules.business_metric.toUpperCase() +
                  (row.snapshot.plan.rules.business_metric === 'cpa'
                    ? ' ≤'
                    : ' ≥'),
                threshold: metricValue(
                  row.snapshot.plan.rules.business_metric,
                  row.snapshot.plan.rules.threshold
                ),
              })}
            </p>
          </div>
          {row.execution_mode === 'mock' && (
            <p className="rounded-lg bg-amber-50 p-3 text-sm" role="note">
              {t('marketing-simulation-note')}
            </p>
          )}
          {row.handoff?.quality_current === false && (
            <p className="rounded-lg bg-amber-50 p-3 text-sm" role="alert">
              {t('marketing-export-quality-changed')}
            </p>
          )}
          {row.board ? (
            <div id="marketing-board">
              <MarketingBoard
                key={'board:' + row.id}
                board={row.board}
                productName={row.snapshot.understanding.product_name}
                deliveryEntries={row.handoff?.entries}
                selected={opened}
                onSelect={(key) => {
                  setOpened(key);
                  if (!key) setEditing((e) => ({ id: '', request: e.request }));
                }}
                renderActions={(execution, close) =>
                  boardActions(row, execution, close)
                }
              />
            </div>
          ) : (
            <p className="text-sm text-gray-600" role="note">
              {t('marketing-board-unavailable')}
            </p>
          )}
          <MarketingVersionHistory
            key={row.id}
            assets={row.assets}
            busy={busy}
            editable={
              !row.handoff &&
              row.state === 'reviewing' &&
              project.state === 'plan_confirmed' &&
              project.revision === row.plan_revision
            }
            onEdit={(id) => {
              setOpened(id);
              setEditing((e) => ({ id, request: e.request + 1 }));
              requestAnimationFrame(() => {
                const target = document.getElementById(editorId + id);
                target?.scrollIntoView?.({
                  behavior: 'smooth',
                  block: 'start',
                });
                target?.focus({ preventScroll: true });
              });
            }}
          />
          {!row.handoff && row.state !== 'closed' && (
            <div className="space-y-5">
              <h3 className="font-semibold">
                {t('marketing-review-creatives')}
              </h3>
              <p className="text-sm text-gray-600">
                {t('marketing-human-semantic-qc')}
              </p>
              {!row.assets.length && (
                <p>{t('marketing-generate-before-review')}</p>
              )}
              <MarketingReusePicker
                row={row}
                productId={project.product_id}
                busy={busy}
                onAttach={(body) =>
                  run((actionInstanceId) =>
                    gateway.reuseMarketingCreative({
                      ...params,
                      body,
                      actionInstanceId,
                    })
                  )
                }
              />
              <div id="marketing-ai-qc" className="space-y-3">
                <MarketingAIQCPanel
                  row={row}
                  busy={busy}
                  onRun={run}
                  onResolved={() => {
                    void workflow.mutate();
                  }}
                />
              </div>
              <div id="marketing-export" className="space-y-3">
                {exportControls()}
              </div>
            </div>
          )}
          {row.evaluation && (
            <>
              <EvaluationView evaluation={row.evaluation} />
              <MarketingEvidenceHandoff
                key={row.id + row.evaluation.id}
                roundId={row.id}
                revision={row.revision}
                evaluationId={row.evaluation.id}
              />
              <MarketingExplanationPanel
                call={row.explanation}
                mode={row.execution_mode}
                busy={busy}
                canRequest={
                  ['evaluated', 'closed'].includes(row.state) &&
                  row.evaluation.current !== false
                }
                onRequest={(body) =>
                  void run((actionInstanceId) =>
                    gateway.startMarketingExplanation({
                      ...params,
                      actionInstanceId,
                      body,
                    })
                  )
                }
                onResolved={() => {
                  void workflow.mutate();
                }}
              />
            </>
          )}
          <DeliveryRecords
            collapsed={
              (row.state === 'evaluated' &&
                row.evaluation?.current !== false) ||
              row.state === 'closed'
            }
          >
            {row.handoff && (
              <div id="marketing-handoff" className="space-y-4">
                <h3 className="font-semibold">{t('marketing-meta-handoff')}</h3>
                {row.handoff.state === 'queued' && (
                  <p role="status">{t('marketing-building-export')}</p>
                )}
                {row.handoff.state === 'failed' && (
                  <div className="space-y-2">
                    <p role="alert">{t('marketing-export-failed')}</p>
                    {row.state !== 'closed' && retryExportButton()}
                  </div>
                )}
                {row.handoff.state === 'ready' && (
                  <>
                    <button
                      className={emphasis('download', BUTTON, SECONDARY)}
                      disabled={busy || row.handoff.quality_current === false}
                      onClick={() =>
                        void run(async (actionInstanceId) => {
                          const result = await gateway.downloadMarketingHandoff(
                            {
                              workspaceSlug: current.slug,
                              roundId: row.id,
                              actionInstanceId,
                            }
                          );
                          const data = valueOf(result);
                          if (!data.download_grant?.url)
                            throw new Error('marketing-download-unavailable');
                          setDownloadUrl(data.download_grant.url);
                          setDownloadRoundId(row.id);
                          return result;
                        })
                      }
                    >
                      {t('marketing-download-meta-package')}
                    </button>
                    {row.handoff.quality_current !== false &&
                      !workflow.error &&
                      downloadUrl &&
                      downloadRoundId === row.id && (
                        <a
                          className="block text-sm underline"
                          href={downloadUrl}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {t('marketing-open-zip-download')}
                        </a>
                      )}
                    {row.handoff.quality_current !== false && (
                      <p className="text-sm text-gray-600">
                        {t('marketing-manual-meta-instructions')}
                      </p>
                    )}
                    {row.handoff.language &&
                      row.handoff.language !== i18n?.language && (
                        <p role="note" className="text-xs text-gray-500">
                          {t(
                            `marketing-handoff-language-${row.handoff.language}`
                          )}
                        </p>
                      )}
                    <div className="divide-y border-y border-gray-200">
                      {row.handoff.entries.map((entry) => (
                        <div key={entry.entry_id} className="space-y-1 py-3">
                          <p
                            className="font-medium"
                            data-testid="marketing-export-entry-name"
                          >
                            {exportedName(entry)}
                          </p>
                          <p className="flex flex-wrap items-center gap-2 text-xs text-gray-600">
                            {t('marketing-ad-name-for-meta')}
                            <CopyValue
                              label={t('marketing-ad-name-for-meta')}
                              value={entry.ad_name}
                            />
                          </p>
                          <p className="text-sm">
                            {row.bindings
                              .filter((b) => b.entry_id === entry.entry_id)
                              .map((b) =>
                                t('marketing-bound-ad', { ad: b.ad_id })
                              )
                              .join(' · ') || t('marketing-not-yet-bound')}
                          </p>
                        </div>
                      ))}
                    </div>
                    <div id="marketing-binding">{bindingForm(row)}</div>
                  </>
                )}
              </div>
            )}
            {row.bindings.length > 0 && (
              <div id="marketing-import">{metricsForm(row)}</div>
            )}
            {row.handoff?.state === 'ready' && (
              <div id="marketing-evaluate">{evaluateButton(row)}</div>
            )}
          </DeliveryRecords>
          {row.state !== 'closed' && (
            <CloseForm
              key={row.id + (row.evaluation?.id || 'pending')}
              row={row}
              busy={busy}
              onClose={(body) =>
                run((actionInstanceId) =>
                  gateway.closeMarketingRound({
                    ...params,
                    actionInstanceId,
                    body,
                  })
                )
              }
            />
          )}
          {!history && nextPlanOpen && onNextPlan && (
            <div id="marketing-next-plan">
              <MarketingNextPlan
                key={row.id}
                row={row}
                inferredAdLanguage={project.plan?.ad_language}
                busy={
                  busy ||
                  ['queued', 'running', 'uncertain'].includes(
                    project.latest_call?.state || ''
                  )
                }
                onRequest={onNextPlan}
              />
            </div>
          )}
          {row.close && (
            <div className="space-y-2 rounded-lg bg-gray-50 p-4">
              <p className="text-sm">
                {t('marketing-closed-evidence-append-note')}
              </p>
              <p className="font-medium">
                {t(`marketing-closed-${row.close.reason}`)}
              </p>
              <p>{row.close.note}</p>
              <p className="text-sm">
                {t(
                  row.close.reason === 'completed'
                    ? 'marketing-big-round-complete-note'
                    : row.continuation?.limit_reached
                      ? 'marketing-big-round-limit-note'
                      : 'marketing-next-round-note'
                )}
              </p>
            </div>
          )}
        </>
      )}
      {workflow.data.rounds.length > 1 && (
        <details>
          <summary className="cursor-pointer text-sm font-medium">
            {t('marketing-round-history')}
          </summary>
          <ul className="mt-3 space-y-2">
            {workflow.data.rounds.map((r) => (
              <li key={r.id}>
                {t('marketing-round-number', { number: r.ordinal })} ·{' '}
                {t(`marketing-state-${r.state}`)} {r.close?.note}
                {r.state === 'closed' && (
                  <button
                    className={SECONDARY}
                    disabled={busy}
                    onClick={() => {
                      void (async () => {
                        setBusy(true);
                        try {
                          const result = valueOf(
                            await gateway.getMarketingRound({
                              includeLanguages: true,
                              includeImageEdits: true,
                              includeVersionHistory: true,
                              includeBoard: true,
                              includeExecutionPlan: true,
                              includeQcKinds: true,
                              includeBoardThumbnails: true,
                              includeTextLanguage: true,
                              includeQcEvidence: true,
                              workspaceSlug: current.slug,
                              roundId: r.id,
                            })
                          ) as { data: MarketingRound };
                          setHistory(result.data);
                        } catch {
                          setError(t('marketing-workflow-load-failed'));
                        } finally {
                          setBusy(false);
                        }
                      })();
                    }}
                  >
                    {t('marketing-view-round-evidence')}
                  </button>
                )}
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

function DeliveryRecords({
  collapsed,
  children,
}: {
  collapsed: boolean;
  children: React.ReactNode;
}) {
  const { t } = useTranslation('common');
  return collapsed ? (
    <details>
      <summary className="cursor-pointer text-sm font-medium">
        {t('marketing-view-delivery-records')}
      </summary>
      <div className="mt-4 space-y-5">{children}</div>
    </details>
  ) : (
    <>{children}</>
  );
}

function FreezeForm({
  project,
  reporting,
  busy,
  onFreeze,
}: {
  project: Project;
  busy: boolean;
  reporting?: Reporting;
  onFreeze: (body: components['schemas']['FreezeRound']) => Promise<void>;
}) {
  const { t } = useTranslation('common');
  const emphasis = useEmphasis();
  const [start, setStart] = useState(
    new Date(Date.now() - new Date().getTimezoneOffset() * 60000)
      .toISOString()
      .slice(0, 10)
  );
  const [timezone, setTimezone] = useState(
    Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  );
  const [mode, setMode] = useState<'byok' | 'mock'>('byok');
  const [attribution, setAttribution] =
    useState<NonNullable<Attribution>>('7d_click_1d_view');
  const [protocol, setProtocol] = useState<Protocol>({
    version: 'marketing-eval-v1',
    min_days: 7,
    min_impressions: 1000,
    min_clicks: 20,
    min_purchases: 20,
    min_spend: '20',
    alpha: '0.05',
    attribution_lag_days: 8,
    allocation: 'equal_budget',
    max_spend_ratio: '2',
    checkpoint: 'end_of_window',
  });
  const duration = project.plan?.duration_days || 7;
  const end = new Date(start + 'T00:00:00Z');
  end.setUTCDate(end.getUTCDate() + duration - 1);
  return (
    <form
      id="marketing-freeze"
      className="space-y-4 rounded-lg border border-gray-200 p-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (!Number.isNaN(end.getTime()))
          void onFreeze({
            start_date: start,
            end_date: end.toISOString().slice(0, 10),
            account_timezone: timezone,
            execution_mode: mode,
            protocol,
            attribution_window: attribution,
          });
      }}
    >
      <h3 className="font-semibold">{t('marketing-freeze-round')}</h3>
      <p className="text-sm text-gray-600">{t('marketing-freeze-note')}</p>
      <div className="grid gap-4 sm:grid-cols-2">
        <Input
          label={t('marketing-start-date')}
          type="date"
          value={start}
          onChange={setStart}
        />
        <Input
          label={t('marketing-account-timezone')}
          value={timezone}
          onChange={setTimezone}
        />
      </div>
      <label className="block space-y-1 text-sm">
        <span>{t('marketing-evidence-mode')}</span>
        <select
          className={INPUT}
          value={mode}
          onChange={(e) => setMode(e.target.value as 'byok' | 'mock')}
        >
          <option value="byok">{t('marketing-mode-byok')}</option>
          <option value="mock">{t('marketing-mode-mock')}</option>
        </select>
      </label>
      <label className="block space-y-1 text-sm">
        <span>{t('marketing-attribution-window')}</span>
        <select
          className={INPUT}
          value={attribution}
          onChange={(event) => {
            const value = event.target.value as NonNullable<Attribution>;
            setAttribution(value);
            setProtocol({
              ...protocol,
              attribution_lag_days: Math.max(
                protocol.attribution_lag_days ?? 8,
                reporting?.attribution_min_lag_days?.[value] ?? 8
              ),
            });
          }}
        >
          {(reporting?.attribution_windows ?? ['7d_click_1d_view']).map(
            (window) => (
              <option key={window} value={window}>
                {attributionLabel(t, window)}
              </option>
            )
          )}
        </select>
      </label>
      <p className="text-sm">
        {t('marketing-default-protocol', {
          days: protocol.min_days,
          impressions: protocol.min_impressions,
          clicks: protocol.min_clicks,
          purchases: protocol.min_purchases,
          spend: protocol.min_spend,
          currency: project.plan?.currency,
          lag: protocol.attribution_lag_days,
        })}
      </p>
      <details>
        <summary className="cursor-pointer text-sm font-medium">
          {t('marketing-adjust-protocol')}
        </summary>
        <div className="mt-4 grid gap-4 sm:grid-cols-2">
          {(
            [
              'min_days',
              'min_impressions',
              'min_clicks',
              'min_purchases',
              'min_spend',
              'alpha',
              'attribution_lag_days',
              'max_spend_ratio',
            ] as const
          ).map((key) => (
            <Input
              key={key}
              type="number"
              label={t(`marketing-protocol-${key}`, {
                currency: project.plan?.currency,
              })}
              value={String(protocol[key])}
              onChange={(value) =>
                setProtocol({
                  ...protocol,
                  [key]: ['min_spend', 'alpha', 'max_spend_ratio'].includes(key)
                    ? value
                    : Number(value),
                })
              }
            />
          ))}
          <label className="space-y-1 text-sm">
            <span>{t('marketing-allocation-type')}</span>
            <select
              className={INPUT}
              value={protocol.allocation}
              onChange={(e) =>
                setProtocol({
                  ...protocol,
                  allocation: e.target.value as Protocol['allocation'],
                })
              }
            >
              <option value="equal_budget">
                {t('marketing-allocation-equal')}
              </option>
              <option value="observational">
                {t('marketing-allocation-observational')}
              </option>
            </select>
          </label>
        </div>
      </details>
      <button
        className={emphasis('freeze', BUTTON, SECONDARY)}
        disabled={busy || !start || !timezone}
      >
        {t('marketing-freeze-and-review')}
      </button>
    </form>
  );
}

function BindingForm({
  row,
  busy,
  entryId,
  onBind,
}: {
  row: MarketingRound;
  busy: boolean;
  entryId?: string;
  onBind: (body: components['schemas']['BindDelivery']) => Promise<void>;
}) {
  const { t } = useTranslation('common');
  const [entry, setEntry] = useState(
    entryId || row.handoff?.entries[0]?.entry_id || ''
  );
  const [account, setAccount] = useState(row.bindings[0]?.ad_account_id || '');
  const [ad, setAd] = useState('');
  const [same, setSame] = useState(true);
  const [note, setNote] = useState('');
  const emphasis = useEmphasis();
  return (
    <form
      className="space-y-3"
      onSubmit={(e) => {
        e.preventDefault();
        void onBind({
          entry_id: entry,
          ad_account_id: account,
          ad_id: ad,
          conditions_match: same,
          deviations: same ? [] : [note],
        });
      }}
    >
      <h4 className="font-medium">{t('marketing-match-meta-ad')}</h4>
      <label className="block space-y-1 text-sm">
        <span>{t('marketing-exported-creative')}</span>
        <select
          className={INPUT}
          value={entry}
          onChange={(e) => setEntry(e.target.value)}
        >
          {row.handoff?.entries.map((item) => (
            <option key={item.entry_id} value={item.entry_id}>
              {exportedName(item)}
            </option>
          ))}
        </select>
      </label>
      <div className="grid gap-3 sm:grid-cols-2">
        <Input
          label={t('marketing-meta-account-id')}
          value={account}
          onChange={setAccount}
        />
        <Input label={t('marketing-meta-ad-id')} value={ad} onChange={setAd} />
      </div>
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={same}
          onChange={(e) => setSame(e.target.checked)}
        />
        {t('marketing-delivery-matches-plan')}
      </label>
      {!same && (
        <Input
          label={t('marketing-delivery-deviations')}
          value={note}
          onChange={setNote}
        />
      )}
      <button
        className={emphasis('bind', BUTTON, SECONDARY)}
        disabled={busy || !entry || !account || !ad || (!same && !note.trim())}
      >
        {t('marketing-save-ad-match')}
      </button>
    </form>
  );
}

function MetricsForm({
  row,
  busy,
  onPreview,
  onConfirm,
}: {
  row: MarketingRound;
  busy: boolean;
  onPreview: (body: components['schemas']['PreviewMetrics']) => Promise<void>;
  onConfirm: (id: string) => Promise<void>;
}) {
  const { t } = useTranslation('common');
  const [text, setText] = useState('');
  const [filename, setFilename] = useState('');
  const [error, setError] = useState('');
  const [replacement, setReplacement] = useState('');
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [contextConfirmed, setContextConfirmed] = useState(false);
  const replaced = new Set(
    row.batches
      .filter((b) => b.state === 'confirmed')
      .map((b) => b.replaces_batch_id)
  );
  const active = row.batches.filter(
    (b) => b.state === 'confirmed' && !replaced.has(b.id)
  );
  const preview = [...row.batches].reverse().find((b) => b.state === 'preview');
  const emphasis = useEmphasis();
  return (
    <div className="space-y-4 border-t border-gray-200 pt-5">
      <h3 className="font-semibold">{t('marketing-import-daily-results')}</h3>
      <p className="text-sm text-gray-600">
        {t('marketing-import-context', {
          timezone: row.timezone,
          currency: row.snapshot.currency,
          attribution: attributionLabel(t, row.snapshot.attribution_window),
        })}
      </p>
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          const fields = Object.fromEntries(
            Object.entries(mapping)
              .filter(([, value]) => value.trim())
              .map(([key, value]) => [key, value.trim()])
          );
          setError('');
          void onPreview({
            csv_text: text,
            filename,
            ad_account_id: row.bindings[0].ad_account_id,
            account_timezone: row.timezone,
            currency: row.snapshot.currency,
            attribution_window: row.snapshot.attribution_window,
            source_mode:
              row.execution_mode === 'mock'
                ? 'controlled_mock'
                : 'customer_file',
            field_mapping: fields,
            replaces_batch_id: replacement || null,
          });
        }}
      >
        <label className="block space-y-1 text-sm">
          <span>{t('marketing-daily-csv-file')}</span>
          <input
            className={INPUT}
            type="file"
            accept=".csv,text/csv"
            onChange={(e) => {
              setText('');
              setFilename('');
              setContextConfirmed(false);
              const file = e.target.files?.[0];
              if (!file) return;
              if (file.size > 262144) {
                setError(t('marketing-csv-size-limit'));
                return;
              }
              void file
                .text()
                .then((value) => {
                  setText(value);
                  setFilename(file.name);
                  setError('');
                })
                .catch(() => setError(t('marketing-file-read-failed')));
            }}
          />
        </label>
        <label className="block space-y-1 text-sm">
          <span>{t('marketing-import-or-correct')}</span>
          <select
            className={INPUT}
            value={replacement}
            onChange={(e) => setReplacement(e.target.value)}
          >
            <option value="">{t('marketing-new-data-batch')}</option>
            {active.map((batch, index) => (
              <option key={batch.id} value={batch.id}>
                {t('marketing-replace-batch', {
                  number: index + 1,
                  filename: batch.metadata.filename,
                })}
              </option>
            ))}
          </select>
        </label>
        <details>
          <summary className="cursor-pointer text-sm">
            {t('marketing-custom-column-mapping')}
          </summary>
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            {[
              'date',
              'ad_id',
              'spend',
              'impressions',
              'clicks',
              'purchases',
              'purchase_value',
            ].map((field) => (
              <Input
                key={field}
                label={t(`marketing-column-${field}`)}
                value={mapping[field] || ''}
                onChange={(value) => setMapping({ ...mapping, [field]: value })}
              />
            ))}
          </div>
          <p className="mt-2 text-xs text-gray-500">
            {t('marketing-column-mapping-help')}
          </p>
        </details>
        {error && <p role="alert">{error}</p>}
        <label className="flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            checked={contextConfirmed}
            onChange={(e) => setContextConfirmed(e.target.checked)}
          />
          {t('marketing-confirm-report-context', {
            timezone: row.timezone,
            currency: row.snapshot.currency,
            attribution: attributionLabel(t, row.snapshot.attribution_window),
          })}
        </label>
        <button
          className={
            preview ? SECONDARY : emphasis('import', BUTTON, SECONDARY)
          }
          disabled={busy || !text || !filename || !contextConfirmed}
        >
          {t('marketing-preview-import')}
        </button>
      </form>
      {preview && (
        <div className="space-y-3 rounded-lg border border-gray-200 p-3">
          <p>
            {t('marketing-preflight-summary', {
              filename: preview.metadata.filename,
              count: preview.row_count,
              issues: preview.issues.length,
            })}
          </p>
          {preview.issues.length > 0 ? (
            <ul className="max-h-64 space-y-1 overflow-y-auto text-sm">
              {preview.issues.map((issue, i) => (
                <li key={i}>{metricsIssueText(t, issue)}</li>
              ))}
            </ul>
          ) : (
            <button
              className={emphasis('import', BUTTON, SECONDARY)}
              disabled={busy}
              onClick={() => void onConfirm(preview.id)}
            >
              {t('marketing-confirm-import')}
            </button>
          )}
          <p className="text-xs text-gray-500">
            {t('marketing-import-provenance-note')}
          </p>
        </div>
      )}
      {active.length > 0 && (
        <p className="text-sm">
          {t('marketing-active-data-batches', {
            count: active.length,
            rows: active.reduce((sum, b) => sum + b.row_count, 0),
          })}
        </p>
      )}
    </div>
  );
}

function EvaluationView({ evaluation: e }: { evaluation: Evaluation }) {
  const { t } = useTranslation('common');
  if (e.current === false)
    return (
      <section
        className="space-y-3"
        aria-label={t('marketing-results-and-next-step')}
      >
        <p role="alert" className="rounded-lg bg-amber-50 p-3 text-sm">
          {t('marketing-evaluation-stale')}
        </p>
        <details>
          <summary className="cursor-pointer text-sm font-medium">
            {t('marketing-view-historical-evaluation')}
          </summary>
          <EvaluationView evaluation={{ ...e, current: true }} />
        </details>
      </section>
    );
  return (
    <section
      className="space-y-4 border-t border-gray-200 pt-5"
      aria-label={t('marketing-results-and-next-step')}
    >
      <h3 className="text-lg font-semibold">
        {t('marketing-results-and-next-step')}
      </h3>
      {e.evidence_status === 'not_supplied' && (
        <p role="note" className="rounded-lg bg-amber-50 p-3 text-sm">
          {t('marketing-evidence-not_supplied')}
        </p>
      )}
      <p className="font-medium">{t(`marketing-next-${e.next_action}`)}</p>
      <p className="text-sm">{t(`marketing-relative-${e.relative_status}`)}</p>
      <p className="text-sm">
        {t(
          e.evidence_status === 'not_supplied'
            ? 'marketing-spend-not-supplied'
            : 'marketing-spend-summary',
          {
            round: e.round_spend,
            roundBudget: e.small_round_budget,
            total: e.big_round_spend,
            totalBudget: e.big_round_budget,
            currency: e.currency,
          }
        )}
      </p>
      {!e.mature && (
        <p className="rounded-lg bg-amber-50 p-3 text-sm">
          {t('marketing-wait-for-checkpoint')}
        </p>
      )}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="border-b">
              <th className="min-w-44 py-2 pr-3">
                {t('marketing-compared-option')}
              </th>
              <th className="min-w-32 px-2">
                {t('marketing-business-status')}
              </th>
              <th className="min-w-40 px-2">
                {t('marketing-estimate-and-interval')}
              </th>
              <th className="min-w-32 px-2">{t('marketing-observed-spend')}</th>
            </tr>
          </thead>
          <tbody>
            {e.results.map((result) => (
              <tr
                key={result.id}
                id={`marketing-result-${result.id}`}
                className="border-b align-top"
              >
                <td className="py-3 pr-3">
                  <p>{result.label}</p>
                  <p className="text-xs text-gray-500">
                    {t(`marketing-scope-${result.conclusion_scope}`)}
                  </p>
                </td>
                <td className="p-2">
                  {t(`marketing-business-${result.business_status}`)}
                  {result.reason_codes.map((reason) => (
                    <p key={reason} className="mt-1 text-xs text-gray-500">
                      {t(`marketing-reason-${reason}`)}
                    </p>
                  ))}
                </td>
                <td className="p-2">
                  {result.estimate
                    ? Number(result.estimate).toFixed(
                        e.metric === 'ctr' ? 4 : 2
                      )
                    : t('marketing-not-available')}
                  <p className="text-xs text-gray-500">
                    {result.interval_low &&
                      metricValue(e.metric, result.interval_low)}{' '}
                    –{' '}
                    {result.interval_high
                      ? metricValue(e.metric, result.interval_high)
                      : t('marketing-unbounded')}
                  </p>
                </td>
                <td className="p-2">
                  {e.evidence_status === 'not_supplied'
                    ? t('marketing-not-available')
                    : Number(result.totals.spend).toFixed(2) + ' ' + e.currency}
                  <p className="mt-1 text-xs text-gray-500">
                    {e.evidence_status === 'not_supplied'
                      ? t('marketing-samples-not-supplied')
                      : t('marketing-observed-samples', {
                          days: result.totals.days,
                          impressions: result.totals.impressions,
                          purchases:
                            result.purchases_reported === false ||
                            (e.metric === 'ctr' && !result.purchases_reported)
                              ? t('marketing-not-available')
                              : Number(result.totals.purchases).toFixed(2),
                        })}
                  </p>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-sm">
        {t('marketing-qualified-count', {
          count: e.qualified_asset_version_ids.length,
        })}
      </p>
      <p className="text-sm">
        {t('marketing-confirmed-combination-count', {
          count: e.confirmed_combinations.length,
        })}
      </p>
      <details>
        <summary className="cursor-pointer text-sm font-medium">
          {t('marketing-evidence-and-limits')}
        </summary>
        <div className="mt-3 space-y-3 text-sm">
          <p>{t(`marketing-evidence-${e.evidence_status}`)}</p>
          <dl>
            {Object.entries(e.conditions).map(([key, value]) => (
              <div key={key} className="mt-2">
                <dt className="font-medium">
                  {t(`marketing-condition-${key}`)}
                </dt>
                <dd className="break-words">{value}</dd>
              </div>
            ))}
          </dl>
          <ul className="list-disc space-y-1 pl-5">
            {e.limitations.map((limit, i) => (
              <li key={i}>{limit}</li>
            ))}
          </ul>
          <TechnicalDetails
            className="text-xs"
            facts={[[t('marketing-evidence-fingerprint-label'), e.input_hash]]}
          />
        </div>
      </details>
    </section>
  );
}

function CloseForm({
  row,
  busy,
  onClose,
}: {
  row: MarketingRound;
  busy: boolean;
  onClose: (body: components['schemas']['CloseRound']) => Promise<void>;
}) {
  const { t } = useTranslation('common');
  const evaluation = row.evaluation?.current === false ? null : row.evaluation;
  const [reason, setReason] = useState<
    components['schemas']['CloseRound']['reason']
  >(() =>
    evaluation?.confirmed_combinations.length
      ? 'completed'
      : row.execution_mode === 'byok' &&
          evaluation?.qualified_asset_version_ids.length
        ? 'asset_only'
        : row.execution_mode === 'byok' && evaluation?.all_assets_failed
          ? 'no_qualified'
          : 'insufficient_evidence'
  );
  const [note, setNote] = useState('');
  const emphasis = useEmphasis();
  return (
    <details id="marketing-close">
      <summary className="cursor-pointer text-sm font-medium">
        {t('marketing-end-this-round')}
      </summary>
      <form
        className="mt-4 space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          void onClose({ reason, note });
        }}
      >
        <label className="block space-y-1 text-sm">
          <span>{t('marketing-closing-outcome')}</span>
          <select
            className={INPUT}
            value={reason}
            onChange={(e) => setReason(e.target.value as typeof reason)}
          >
            <option value="insufficient_evidence">
              {t('marketing-closed-insufficient_evidence')}
            </option>
            <option value="user_stopped">
              {t('marketing-closed-user_stopped')}
            </option>
            {row.execution_mode === 'byok' &&
              !!evaluation?.qualified_asset_version_ids.length && (
                <option value="asset_only">
                  {t('marketing-closed-asset_only')}
                </option>
              )}
            {row.execution_mode === 'byok' && evaluation?.all_assets_failed && (
              <option value="no_qualified">
                {t('marketing-closed-no_qualified')}
              </option>
            )}
            {!!evaluation?.confirmed_combinations.length && (
              <option value="completed">
                {t('marketing-closed-completed')}
              </option>
            )}
          </select>
        </label>
        <Input
          label={t('marketing-closing-note')}
          value={note}
          onChange={setNote}
        />
        <button
          className={emphasis('close', BUTTON, SECONDARY)}
          disabled={busy || !note.trim()}
        >
          {t('marketing-save-round-conclusion')}
        </button>
      </form>
    </details>
  );
}
