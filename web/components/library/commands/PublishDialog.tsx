import { useTranslation } from 'next-i18next';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useLibraryCommand } from '@/hooks/library/useLibraryCommand';
import { useLibraryCache } from '@/hooks/library/useLibraryCache';
import {
  useLibraryCommandImpact,
  type ImpactProjection,
} from '@/hooks/library/useLibraryCommandImpact';
import { errorMessage } from '@/lib/i18n/domain-errors';
import { FindingText } from '@/components/library/FindingText';

interface PublishDialogProps {
  entryId: string;
  versionId?: string | null;
  /** Warning codes already acknowledged elsewhere (e.g. the draft editor). */
  acknowledgedWarnings?: string[];
  open: boolean;
  onClose: () => void;
  onPublished: () => void;
}

const NO_WARNINGS: string[] = [];

export function PublishDialog({
  entryId,
  acknowledgedWarnings = NO_WARNINGS,
  open,
  onClose,
  onPublished,
}: PublishDialogProps) {
  const { t } = useTranslation('library');
  const dialogRef = useRef<HTMLDialogElement>(null);
  const publishCmd = useLibraryCommand('publish');
  const { invalidate } = useLibraryCache();
  const impactHook = useLibraryCommandImpact();
  const [warningsAcknowledgedHere, setWarningsAcknowledgedHere] =
    useState(false);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open && typeof dialog.showModal === 'function') {
      try {
        dialog.showModal();
      } catch {
        // jsdom
      }
    }
  }, [open]);

  useEffect(() => {
    if (open && entryId) {
      impactHook.fetch('publish', entryId);
    }
    if (!open) {
      impactHook.clear();
    }
  }, [open, entryId]); // eslint-disable-line react-hooks/exhaustive-deps

  // A fresh impact projection may carry different warnings; the previous
  // acknowledgement never carries over to it.
  const impactId = impactHook.impact?.impactId ?? null;
  useEffect(() => {
    setWarningsAcknowledgedHere(false);
  }, [impactId]);

  // FastAPI refuses a publish whose non-blocking warnings were not all
  // acknowledged by code, so the dialog must collect that acknowledgement
  // for every warning the impact projection reports and the caller has not
  // already acknowledged.
  const pendingWarningCodes = useMemo(() => {
    const findings = impactHook.impact?.warnings ?? [];
    return findings
      .map((finding) => finding.code)
      .filter((code) => !acknowledgedWarnings.includes(code));
  }, [impactHook.impact, acknowledgedWarnings]);

  if (!open) return null;

  const needsAcknowledgement = pendingWarningCodes.length > 0;
  const canConfirm =
    impactHook.impact &&
    !impactHook.isStale &&
    impactHook.impact.blocking.length === 0 &&
    (!needsAcknowledgement || warningsAcknowledgedHere);

  const handlePublish = async () => {
    if (!impactHook.impact) return;
    if (needsAcknowledgement && !warningsAcknowledgedHere) return;

    impactHook.confirm();

    const acknowledged = Array.from(
      new Set([
        ...acknowledgedWarnings,
        ...(warningsAcknowledgedHere ? pendingWarningCodes : []),
      ])
    );

    const outcome = await publishCmd.run({
      entry_id: entryId,
      ifMatch: impactHook.impact.targetRevision,
      body: {
        impact_id: impactHook.impact.impactId,
        impact_hash: impactHook.impact.impactHash,
        acknowledged_warnings: acknowledged,
      },
    });

    if (outcome.kind === 'success') {
      publishCmd.reset();
      await invalidate({
        resources: [
          'entry',
          'entryList',
          'versions',
          'events',
          'impact',
          'bindings',
        ],
      });
      onPublished();
    }
  };

  const handleClose = () => {
    publishCmd.reset();
    impactHook.clear();
    setWarningsAcknowledgedHere(false);
    onClose();
  };

  return (
    <>
      <dialog
        ref={dialogRef}
        className="modal modal-open"
        aria-labelledby="publish-title"
        onClose={handleClose}
      >
        <div className="modal-box max-w-lg">
          <h3
            id="publish-title"
            className="text-lg font-bold text-gray-900 dark:text-gray-100"
          >
            {t('commands.publish-title')}
          </h3>

          {impactHook.isLoading && (
            <div className="mt-4" aria-busy="true">
              <p className="text-sm text-gray-500 dark:text-gray-400">
                {t('commands.loading-impact')}
              </p>
            </div>
          )}

          {impactHook.problem && (
            <div
              className="mt-4 text-sm text-red-600 dark:text-red-400"
              role="alert"
            >
              {errorMessage(t, impactHook.problem)}
            </div>
          )}

          {impactHook.impact && (
            <ImpactSummary
              impact={impactHook.impact}
              acknowledgedWarnings={acknowledgedWarnings}
              needsAcknowledgement={needsAcknowledgement}
              warningsAcknowledged={warningsAcknowledgedHere}
              onToggleWarningsAcknowledged={setWarningsAcknowledgedHere}
            />
          )}

          {publishCmd.problem && (
            <div
              className="mt-3 text-sm text-red-600 dark:text-red-400"
              role="alert"
            >
              {errorMessage(t, publishCmd.problem)}
            </div>
          )}

          <div className="modal-action mt-6">
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={handleClose}
            >
              {t('commands.cancel')}
            </button>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={!canConfirm || publishCmd.isPending}
              onClick={handlePublish}
            >
              {publishCmd.isPending
                ? t('commands.publishing')
                : t('commands.confirm-publish')}
            </button>
          </div>
        </div>
        <form method="dialog" className="modal-backdrop">
          <button type="submit">{t('commands.cancel')}</button>
        </form>
      </dialog>
    </>
  );
}

function ImpactSummary({
  impact,
  acknowledgedWarnings,
  needsAcknowledgement,
  warningsAcknowledged,
  onToggleWarningsAcknowledged,
}: {
  impact: ImpactProjection;
  acknowledgedWarnings: string[];
  needsAcknowledgement: boolean;
  warningsAcknowledged: boolean;
  onToggleWarningsAcknowledged: (checked: boolean) => void;
}) {
  const { t } = useTranslation('library');

  return (
    <div className="mt-4 space-y-3">
      {impact.blocking.length > 0 && (
        <div
          className="rounded-lg border border-red-200 bg-red-50 p-3 dark:border-red-800 dark:bg-red-900/20"
          role="alert"
        >
          <p className="text-sm font-medium text-red-800 dark:text-red-300">
            {t('commands.publish-blocked')}
          </p>
          <ul className="mt-1 list-disc pl-4 text-sm text-red-700 dark:text-red-400">
            {impact.blocking.map((finding, i) => (
              <li key={`${finding.code}-${i}`}>
                <FindingText finding={finding} />
              </li>
            ))}
          </ul>
        </div>
      )}

      {impact.warnings.length > 0 && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 dark:border-amber-800 dark:bg-amber-900/20">
          <p className="text-sm font-medium text-amber-800 dark:text-amber-300">
            {t('commands.publish-warnings')}
          </p>
          <ul className="mt-1 list-disc pl-4 text-sm text-amber-700 dark:text-amber-400">
            {impact.warnings.map((finding, i) => (
              <li key={`${finding.code}-${i}`}>
                <FindingText finding={finding} />
              </li>
            ))}
          </ul>
          {needsAcknowledgement && (
            <label className="mt-3 flex cursor-pointer items-start gap-2 text-sm text-amber-900 dark:text-amber-200">
              <input
                type="checkbox"
                className="checkbox checkbox-sm mt-0.5"
                checked={warningsAcknowledged}
                onChange={(event) =>
                  onToggleWarningsAcknowledged(event.target.checked)
                }
                data-testid="publish-acknowledge-warnings"
              />
              <span>{t('commands.publish-acknowledge-warnings')}</span>
            </label>
          )}
        </div>
      )}

      {impact.affectedSummaries.length > 0 && (
        <div>
          <p className="text-sm font-medium text-gray-700 dark:text-gray-300">
            {t('commands.publish-affected', {
              count: impact.affectedSummaries.length,
            })}
          </p>
          <ul className="mt-1 space-y-1">
            {impact.affectedSummaries.map((s) => (
              <li
                key={s.id}
                className="flex items-center justify-between rounded border border-gray-200 px-2 py-1 text-sm dark:border-gray-700"
              >
                <span className="text-gray-700 dark:text-gray-300">
                  {s.display_name}
                </span>
                <span className="text-xs text-gray-500 dark:text-gray-400">
                  {s.effect}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {acknowledgedWarnings.length > 0 && (
        <p className="text-xs text-gray-500 dark:text-gray-400">
          {t('commands.publish-acknowledged', {
            count: acknowledgedWarnings.length,
          })}
        </p>
      )}
    </div>
  );
}
