import { useState } from 'react';
import { useTranslation } from 'next-i18next';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import { RecoveryPreviewDisplay } from './RecoveryPreviewDisplay';
import { ReconciliationGuidance } from './ReconciliationGuidance';
import { CancelConfirmation } from './CancelConfirmation';
import { newActionId } from '../shared/newActionId';
import { errorMessage as problemMessage } from '@/lib/i18n/domain-errors';

type ActionEligibility = components['schemas']['ActionEligibility'];
type GenerationRecoveryPreview =
  components['schemas']['GenerationRecoveryPreview'];

/**
 * Minimal shape `RecoveryPanel` needs from a `GenerationRunDetail` (spec:
 * `GET /generation/runs/{id}`, `run_queries.get_run_detail`). Kept narrower
 * than the full generated type so a caller that already has one (e.g. the
 * run page shell's `useRunProgress().run`) can pass it directly --
 * `GenerationRunDetail`'s required fields are a structural superset of this
 * interface's optional ones -- while a test can construct a minimal
 * fixture without every field.
 */
export interface RecoveryPanelRun {
  id: string;
  /** `GenerationRunDetail.revision` -- threaded as `ifMatch` on every
   * command below for optimistic concurrency (mirrors
   * `DraftComposer.tsx`'s `ifMatch: draft.revision`); omitted entirely
   * when the caller doesn't have it (e.g. a test fixture). */
  revision?: number;
  /** `GenerationRunDetail.status` (`derive_run_state`) -- `uncertain` is
   * the one non-terminal status that still needs an operator, so the
   * reconciliation guidance keys off it. */
  status?: string;
  total?: number;
  done?: number;
  failed?: number;
  /** `run_queries.run_actions`'s `_RUN_ACTIONS = ["cancel", "retry_failed",
   * "resume_paused", "reconcile"]` -- the authoritative gate for which
   * buttons below render and whether they're enabled ("ActionEligibility
   * drives UI eligibility"). */
  allowed_actions?: ActionEligibility[];
}

interface RecoveryPanelProps {
  run: RecoveryPanelRun;
  /** Called once a retry/resume/cancel/reconcile command lands successfully
   * -- the caller (the run page shell) uses this to force its own
   * `useRunProgress` poller to pick up the new state immediately rather
   * than waiting out its next scheduled interval. */
  onRecoveryComplete: () => void;
}

type RecoveryCommand = 'retry_failed_items' | 'resume_paused_items';

type PanelView =
  | { phase: 'idle' }
  | { phase: 'cancel_confirm' }
  | {
      phase: 'preview';
      command: RecoveryCommand;
      preview: GenerationRecoveryPreview;
    };

const RETRY_RESUME_ACTIONS: Array<{
  /** `ActionEligibility.action` id (spec: `run_queries.run_actions`). */
  actionId: 'retry_failed' | 'resume_paused';
  command: RecoveryCommand;
  /** `RecoveryPreviewSelection.kind` (spec: `response_models.py`). */
  selectionKind: 'all_eligible_failed' | 'all_resumable_paused';
  labelKey: string;
}> = [
  {
    actionId: 'retry_failed',
    command: 'retry_failed_items',
    selectionKind: 'all_eligible_failed',
    labelKey: 'generation-recovery-retry-failed',
  },
  {
    actionId: 'resume_paused',
    command: 'resume_paused_items',
    selectionKind: 'all_resumable_paused',
    labelKey: 'generation-recovery-resume-paused',
  },
];

/**
 * Root display for a GenerationRun's recovery actions (spec: Task 11,
 * Generation Workspace UI plan) -- rendered by the run page shell's
 * `recoverySlot` for every loaded Run, since `Cancel Run` is only eligible
 * while the Run is still active and `Reconcile` while it is `uncertain`
 * (neither is a terminal state). Drives four server-computed
 * `ActionEligibility` entries (`cancel` / `retry_failed` / `resume_paused` /
 * `reconcile`, `run_queries.run_actions`): retry/resume go through a
 * `create_generation_run_recovery_preview` -> confirm round trip so the
 * caller sees targets and cost before committing
 * (`RecoveryPreviewDisplay`); cancel goes through an interstitial warning
 * (`CancelConfirmation`); reconcile collects a required operator note
 * inline (`ReconciliationGuidance`).
 */
export function RecoveryPanel({ run, onRecoveryComplete }: RecoveryPanelProps) {
  const { t } = useTranslation('common');
  const previewCommand = useDomainCommand(
    'create_generation_run_recovery_preview'
  );
  const retryCommand = useDomainCommand('retry_generation_run_failed_items');
  const resumeCommand = useDomainCommand('resume_generation_run_paused_items');
  const cancelCommand = useDomainCommand('cancel_generation_run');
  const reconcileCommand = useDomainCommand('reconcile_generation_run');

  const [view, setView] = useState<PanelView>({ phase: 'idle' });
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const allowedActions = run.allowed_actions ?? [];

  function getAction(actionId: string): ActionEligibility | undefined {
    return allowedActions.find((a) => a.action === actionId);
  }
  function isVisible(actionId: string): boolean {
    return getAction(actionId) !== undefined;
  }
  function isAllowed(actionId: string): boolean {
    return getAction(actionId)?.allowed === true;
  }

  const isPreviewBusy = previewCommand.isPending;
  const isConfirmBusy = retryCommand.isPending || resumeCommand.isPending;
  const isCancelBusy = cancelCommand.isPending;
  const isReconcileBusy = reconcileCommand.isPending;
  const isAnyBusy =
    isPreviewBusy || isConfirmBusy || isCancelBusy || isReconcileBusy;

  async function requestPreview(
    command: RecoveryCommand,
    selectionKind: 'all_eligible_failed' | 'all_resumable_paused'
  ) {
    setErrorMessage(null);
    const outcome = await previewCommand.execute({
      pathParams: { run_id: run.id },
      body: { command, selection: { kind: selectionKind } },
      actionInstanceId: newActionId(),
      ifMatch: run.revision,
    });

    if (outcome.kind === 'success') {
      setView({ phase: 'preview', command, preview: outcome.data });
    } else if (outcome.kind === 'problem') {
      setErrorMessage(problemMessage(t, outcome.problem));
    }
  }

  async function confirmPreview() {
    if (view.phase !== 'preview') return;
    const { command, preview } = view;
    const hook =
      command === 'retry_failed_items' ? retryCommand : resumeCommand;

    const outcome = await hook.execute({
      pathParams: { run_id: run.id },
      body: { preview_id: preview.id, preview_hash: preview.preview_hash },
      actionInstanceId: newActionId(),
      ifMatch: run.revision,
    });

    if (outcome.kind === 'success') {
      setView({ phase: 'idle' });
      onRecoveryComplete();
    } else if (outcome.kind === 'problem') {
      setErrorMessage(problemMessage(t, outcome.problem));
    }
  }

  async function handleCancel() {
    setErrorMessage(null);
    const outcome = await cancelCommand.execute({
      pathParams: { run_id: run.id },
      actionInstanceId: newActionId(),
      ifMatch: run.revision,
    });

    if (outcome.kind === 'success') {
      setView({ phase: 'idle' });
      onRecoveryComplete();
    } else if (outcome.kind === 'problem') {
      setErrorMessage(problemMessage(t, outcome.problem));
    }
  }

  async function handleReconcile(operatorNote: string) {
    setErrorMessage(null);
    const outcome = await reconcileCommand.execute({
      pathParams: { run_id: run.id },
      body: { operator_note: operatorNote },
      actionInstanceId: newActionId(),
      ifMatch: run.revision,
    });

    if (outcome.kind === 'success') {
      setView({ phase: 'idle' });
      onRecoveryComplete();
    } else if (outcome.kind === 'problem') {
      setErrorMessage(problemMessage(t, outcome.problem));
    }
  }

  if (view.phase === 'cancel_confirm') {
    // `total`/`done`/`failed` aren't guaranteed on the minimal
    // `RecoveryPanelRun` shape -- default to warning (the safer assumption)
    // when a caller hasn't supplied them.
    const hasInFlightItems =
      typeof run.total === 'number' &&
      typeof run.done === 'number' &&
      typeof run.failed === 'number'
        ? run.done + run.failed < run.total
        : true;

    return (
      <CancelConfirmation
        hasInFlightItems={hasInFlightItems}
        onConfirm={handleCancel}
        onCancel={() => setView({ phase: 'idle' })}
        isSubmitting={isCancelBusy}
      />
    );
  }

  if (view.phase === 'preview') {
    return (
      <RecoveryPreviewDisplay
        preview={view.preview}
        onConfirm={confirmPreview}
        onCancel={() => setView({ phase: 'idle' })}
        isSubmitting={isConfirmBusy}
      />
    );
  }

  const cancelAction = getAction('cancel');
  const reconcileAction = getAction('reconcile');

  // Reconciliation is owed only while `run_queries.run_actions` says so
  // (`has_uncertain`) or the Run projection itself reads `uncertain`; for
  // every other Run the guidance box would announce "0 item(s) have
  // uncertain outcomes" under a succeeded Run, or count in-flight items as
  // uncertain under a running one.
  const isUncertainRun = run.status === 'uncertain';
  const showReconciliation =
    isVisible('reconcile') &&
    (reconcileAction?.allowed === true || isUncertainRun);

  // `run_queries.run_actions`'s `has_uncertain` check has no exposed count
  // on `GenerationRunDetail` -- approximate it from `total - done - failed`
  // (items neither succeeded nor failed) when those counts are available
  // and the Run is settled enough for that difference to mean "uncertain"
  // (an active Run's in-flight items would be counted too), otherwise
  // leave the guidance text generic.
  const uncertainCount =
    isUncertainRun &&
    typeof run.total === 'number' &&
    typeof run.done === 'number' &&
    typeof run.failed === 'number'
      ? Math.max(0, run.total - run.done - run.failed)
      : undefined;

  return (
    <div className="space-y-3 rounded-lg border border-base-300 p-4">
      <h3 className="font-semibold">{t('generation-recovery-heading')}</h3>

      {errorMessage && (
        <div className="alert alert-error text-sm" role="alert">
          {errorMessage}
        </div>
      )}

      {RETRY_RESUME_ACTIONS.map(
        ({ actionId, command, selectionKind, labelKey }) => {
          if (!isVisible(actionId)) return null;
          const action = getAction(actionId);
          const allowed = action?.allowed === true;
          return (
            <div key={actionId}>
              <button
                type="button"
                className="btn btn-sm btn-outline"
                disabled={!allowed || isAnyBusy}
                onClick={() => requestPreview(command, selectionKind)}
              >
                {isPreviewBusy
                  ? t('generation-recovery-preview-loading')
                  : t(labelKey)}
              </button>
              {!allowed && action?.reason_code && (
                <p className="mt-1 text-xs text-base-content/60">
                  {t('generation-recovery-action-blocked', {
                    reason: action.reason_code,
                  })}
                </p>
              )}
            </div>
          );
        }
      )}

      {isVisible('cancel') && (
        <div>
          <button
            type="button"
            className="btn btn-sm btn-outline btn-error"
            disabled={!isAllowed('cancel') || isAnyBusy}
            onClick={() => setView({ phase: 'cancel_confirm' })}
          >
            {t('generation-recovery-cancel-run')}
          </button>
          {cancelAction?.allowed !== true && cancelAction?.reason_code && (
            <p className="mt-1 text-xs text-base-content/60">
              {t('generation-recovery-action-blocked', {
                reason: cancelAction.reason_code,
              })}
            </p>
          )}
        </div>
      )}

      {showReconciliation && (
        <ReconciliationGuidance
          uncertainCount={uncertainCount}
          isAllowed={reconcileAction?.allowed === true}
          reasonCode={reconcileAction?.reason_code}
          onReconcile={handleReconcile}
          isSubmitting={isReconcileBusy}
        />
      )}
    </div>
  );
}
