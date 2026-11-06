import { useState } from 'react';
import { useTranslation } from 'next-i18next';
import { formatDateTime } from '@/lib/i18n/format';
import type { AdviceThreadSummary } from '@/hooks/scale/useScaleAdvice';
import {
  useCreateScaleAdviceDisposition,
  useCreateScaleAdviceSnooze,
  useConvertScaleAdviceOccurrence,
} from '@/hooks/scale/useScaleAdviceCommands';
import { LifecycleBadge } from '@/components/scale/shared/LifecycleBadge';
import { AllowedActionButton } from '@/components/scale/shared/AllowedActionButton';
import type { ScaleMetaMode } from '@/lib/scale/meta-mode';
import { errorMessage } from '@/lib/i18n/domain-errors';

const SEVERITY_CLASS: Record<string, string> = {
  info: 'badge-ghost',
  warning: 'badge-warning',
  critical: 'badge-error',
};

interface AdviceThreadCardProps {
  thread: AdviceThreadSummary;
  metaMode: ScaleMetaMode;
  onChanged: () => void;
}

/**
 * One Advice thread (spec §4.2, §4.4 "Advice state" axis): lifecycle
 * (active/resolved/superseded/expired) rendered independently of
 * disposition (acknowledged/dismissed/converted_to_draft) and snooze --
 * a thread can be `active` and already carry a prior disposition from an
 * earlier occurrence. Every action button here is gated by the thread's own
 * `allowed_actions` (`acknowledge`/`dismiss`/`snooze`/`convert`), computed
 * server-side from `thread.lifecycle` and, for `convert`, `advice_type`
 * (`app.scale.v1_router._thread_actions`) -- never re-derived client-side.
 */
export function AdviceThreadCard({
  thread,
  metaMode,
  onChanged,
}: AdviceThreadCardProps) {
  const { t, i18n } = useTranslation('scale');
  const [snoozeOpen, setSnoozeOpen] = useState(false);
  const [snoozeUntil, setSnoozeUntil] = useState('');
  const [conversionReceipt, setConversionReceipt] = useState<{
    generationDraftId: string | null;
    scaleTestPlanDraftId: string | null;
  } | null>(null);

  const dispositionCmd = useCreateScaleAdviceDisposition();
  const snoozeCmd = useCreateScaleAdviceSnooze();
  const convertCmd = useConvertScaleAdviceOccurrence();

  const actionMap = new Map(thread.allowed_actions.map((a) => [a.action, a]));

  const handleAcknowledge = async () => {
    const outcome = await dispositionCmd.run({
      threadId: thread.id,
      metaMode,
      kind: 'acknowledged',
      occurrenceId: thread.latest_occurrence_id,
    });
    if (outcome.kind === 'success') onChanged();
  };

  const handleDismiss = async () => {
    const outcome = await dispositionCmd.run({
      threadId: thread.id,
      metaMode,
      kind: 'dismissed',
      occurrenceId: thread.latest_occurrence_id,
    });
    if (outcome.kind === 'success') onChanged();
  };

  const handleSnoozeConfirm = async () => {
    if (!snoozeUntil) return;
    const outcome = await snoozeCmd.run({
      threadId: thread.id,
      metaMode,
      until: new Date(snoozeUntil).toISOString(),
    });
    if (outcome.kind === 'success') {
      setSnoozeOpen(false);
      onChanged();
    }
  };

  const handleConvert = async () => {
    const outcome = await convertCmd.run({
      occurrenceId: thread.latest_occurrence_id,
      metaMode,
    });
    if (outcome.kind === 'success') {
      setConversionReceipt({
        generationDraftId: outcome.data.generation_draft_id,
        scaleTestPlanDraftId: outcome.data.scale_test_plan_draft_id,
      });
      onChanged();
    }
  };

  return (
    <li className="card card-compact bg-base-100 shadow-sm">
      <div className="card-body">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="font-medium">
            {t(`advice-type.${thread.advice_type}`, {
              defaultValue: thread.advice_type,
            })}
          </span>
          <div className="flex items-center gap-2">
            <span
              className={`badge badge-sm ${SEVERITY_CLASS[thread.severity] ?? 'badge-ghost'}`}
            >
              {t(`severity.${thread.severity}`, {
                defaultValue: thread.severity,
              })}
            </span>
            <LifecycleBadge scope="advice" value={thread.lifecycle} />
          </div>
        </div>

        <p className="text-xs text-base-content/60">
          {thread.reason_signature}
        </p>

        {thread.disposition_kind && (
          <p className="text-xs text-base-content/70">
            {t('advice-card.disposition', { kind: thread.disposition_kind })}
          </p>
        )}
        {thread.snooze_until && (
          <p className="text-xs text-base-content/70">
            {t('advice-card.snoozed-until', {
              until: formatDateTime(i18n.language, thread.snooze_until),
            })}
          </p>
        )}

        {(dispositionCmd.problem ||
          snoozeCmd.problem ||
          convertCmd.problem) && (
          <p className="text-xs text-error" role="alert">
            {errorMessage(
              t,
              dispositionCmd.problem ?? snoozeCmd.problem ?? convertCmd.problem
            )}
          </p>
        )}

        {conversionReceipt && (
          <div className="alert alert-success py-2 text-xs" role="status">
            <span>
              {t('advice-card.conversion-created')}{' '}
              <strong className="font-mono">
                {conversionReceipt.generationDraftId ??
                  conversionReceipt.scaleTestPlanDraftId}
              </strong>
              . {t('advice-card.conversion-unsubmitted')}
            </span>
          </div>
        )}

        <div className="mt-2 flex flex-wrap items-center gap-2" role="toolbar">
          <AllowedActionButton
            action={actionMap.get('acknowledge')}
            label={t('advice-card.acknowledge')}
            onClick={handleAcknowledge}
            variant="primary"
            busy={dispositionCmd.isPending}
          />
          <AllowedActionButton
            action={actionMap.get('dismiss')}
            label={t('advice-card.dismiss')}
            onClick={handleDismiss}
            busy={dispositionCmd.isPending}
          />
          <AllowedActionButton
            action={actionMap.get('snooze')}
            label={t('advice-card.snooze')}
            onClick={() => setSnoozeOpen(true)}
          />
          <AllowedActionButton
            action={actionMap.get('convert')}
            label={t('advice-card.convert')}
            onClick={handleConvert}
            variant="warning"
            busy={convertCmd.isPending}
          />
        </div>

        {snoozeOpen && (
          <div className="mt-2 flex items-center gap-2">
            <input
              type="datetime-local"
              className="input input-bordered input-sm"
              value={snoozeUntil}
              onChange={(e) => setSnoozeUntil(e.target.value)}
              aria-label={t('advice-card.snooze-until-label')}
            />
            <button
              type="button"
              className="btn btn-sm btn-primary"
              disabled={!snoozeUntil || snoozeCmd.isPending}
              onClick={handleSnoozeConfirm}
            >
              {t('advice-card.snooze-confirm')}
            </button>
            <button
              type="button"
              className="btn btn-sm btn-ghost"
              onClick={() => setSnoozeOpen(false)}
            >
              {t('advice-card.snooze-cancel')}
            </button>
          </div>
        )}
      </div>
    </li>
  );
}
