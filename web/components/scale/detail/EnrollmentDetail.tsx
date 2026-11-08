import { useState } from 'react';
import Link from 'next/link';
import { useTranslation } from 'next-i18next';
import { formatDateTime } from '@/lib/i18n/format';
import { useScaleEnrollment } from '@/hooks/scale/useScaleEnrollments';
import {
  useCreateScaleActivationPreview,
  usePauseScaleEnrollment,
  useResumeScaleEnrollment,
  useCloseScaleEnrollment,
} from '@/hooks/scale/useScaleEnrollmentCommands';
import {
  useCreateScaleExport,
  useCreateScaleExportDownloadIntent,
} from '@/hooks/scale/useScaleExport';
import { useOperation } from '@/hooks/domain/useOperation';
import { useScaleNav } from '@/hooks/scale/useScaleNav';
import { LifecycleBadge } from '@/components/scale/shared/LifecycleBadge';
import { AllowedActionButton } from '@/components/scale/shared/AllowedActionButton';
import { EpisodePanel } from '@/components/scale/detail/EpisodePanel';
import { BreakdownTable } from '@/components/scale/detail/BreakdownTable';
import { ActivateEnrollmentDialog } from '@/components/scale/detail/ActivateEnrollmentDialog';
import { LifecycleReasonDialog } from '@/components/scale/detail/LifecycleReasonDialog';
import { AdviceThreadList } from '@/components/scale/advice/AdviceThreadList';
import { metaModeLabelKey } from '@/lib/scale/meta-mode';
import type { ScaleMetaMode } from '@/lib/scale/meta-mode';
import { errorMessage } from '@/lib/i18n/domain-errors';

interface EnrollmentDetailProps {
  enrollmentId: string;
  metaMode: ScaleMetaMode;
}

const EXPORT_KIND = 'evidence_summary';

interface ExportPanelProps {
  enrollmentId: string;
  episodeId: string | null;
  metaMode: ScaleMetaMode;
}

/**
 * Export trigger + status (spec §16, §22, Issue #87 Task 5/7): creates a
 * durable export Operation, tracks it to a terminal state via
 * `useOperation`, then offers a download-intent button once it succeeds.
 * There is no `get_scale_export` read to recover a lost export id across a
 * refresh -- this panel only tracks the export it itself just created.
 */
function ExportPanel({ enrollmentId, episodeId, metaMode }: ExportPanelProps) {
  const { t, i18n } = useTranslation('scale');
  const [exportId, setExportId] = useState<string | null>(null);
  const [operationId, setOperationId] = useState<string | null>(null);
  const exportCmd = useCreateScaleExport();
  const downloadIntentCmd = useCreateScaleExportDownloadIntent();
  const operation = useOperation({
    operationRecordId: operationId,
    purpose: 'scale-export',
    actionInstanceId: `scale-export:${enrollmentId}`,
  });

  const handleExport = async () => {
    if (!episodeId) return;
    downloadIntentCmd.reset();
    const outcome = await exportCmd.run({
      metaMode,
      enrollmentId,
      episodeId,
      exportKind: EXPORT_KIND,
    });
    if (outcome.kind === 'success') {
      setExportId(outcome.data.id);
      setOperationId(outcome.data.operation_id);
    }
  };

  const handleDownloadIntent = async () => {
    if (!exportId) return;
    await downloadIntentCmd.run({ exportId, metaMode });
  };

  const succeeded = operation.operation?.state === 'succeeded';
  const failed =
    operation.isTerminal && !succeeded && !operation.isPartialSuccess;

  return (
    <div className="flex flex-wrap items-center gap-2">
      <button
        type="button"
        className="btn btn-sm btn-ghost"
        disabled={
          !episodeId ||
          exportCmd.isPending ||
          (!!operationId && !operation.isTerminal)
        }
        onClick={handleExport}
      >
        {exportCmd.isPending
          ? t('export.creating')
          : operationId && !operation.isTerminal
            ? t('export.processing')
            : t('export.action')}
      </button>

      {operationId && !operation.isTerminal && (
        <span className="loading loading-spinner loading-xs" aria-busy="true" />
      )}
      {failed && (
        <span className="text-sm text-error" role="alert">
          {operation.problem
            ? errorMessage(t, operation.problem)
            : t('export.failed')}
        </span>
      )}
      {succeeded && !downloadIntentCmd.result && (
        <button
          type="button"
          className="btn btn-sm btn-outline"
          disabled={downloadIntentCmd.isPending}
          onClick={handleDownloadIntent}
        >
          {downloadIntentCmd.isPending
            ? t('export.requesting-download')
            : t('export.get-download')}
        </button>
      )}
      {downloadIntentCmd.result && (
        <span className="text-sm text-base-content/70">
          {t('export.download-ready', {
            expiresAt: formatDateTime(
              i18n.language,
              downloadIntentCmd.result.expires_at
            ),
          })}
        </span>
      )}
      {downloadIntentCmd.problem && (
        <span className="text-sm text-error" role="alert">
          {errorMessage(t, downloadIntentCmd.problem)}
        </span>
      )}
      {exportCmd.problem && (
        <span className="text-sm text-error" role="alert">
          {errorMessage(t, exportCmd.problem)}
        </span>
      )}
    </div>
  );
}

type DialogKind = 'activate' | 'pause' | 'close' | null;

/**
 * Enrollment detail (spec §4.1/§4.3/§4.4). Renders the five orthogonal
 * state axes independently: Enrollment lifecycle (this component's header
 * badge + actions), Episode phase/maturity and Evidence condition
 * (`EpisodePanel`), Health (inside `EpisodePanel`'s evidence), and Advice
 * state (`AdviceThreadList`, scoped to this Enrollment). Every lifecycle
 * button is gated purely by `data.allowed_actions` from
 * `get_scale_enrollment` -- never a client-side guess from `lifecycle`
 * alone (spec: "the browser never reconstructs eligibility client-side").
 */
export function EnrollmentDetail({
  enrollmentId,
  metaMode,
}: EnrollmentDetailProps) {
  const { t, i18n } = useTranslation('scale');
  const { buildListUrl, buildBreakdownsUrl, buildEvidenceUrl } = useScaleNav();
  const [dialog, setDialog] = useState<DialogKind>(null);

  const {
    data: enrollment,
    problem,
    isLoading,
    refresh,
  } = useScaleEnrollment(enrollmentId, metaMode);

  const previewCmd = useCreateScaleActivationPreview();
  const pauseCmd = usePauseScaleEnrollment();
  const resumeCmd = useResumeScaleEnrollment();
  const closeCmd = useCloseScaleEnrollment();

  if (isLoading) {
    return (
      <div className="mt-8 flex justify-center" aria-busy="true">
        <span className="loading loading-spinner" />
      </div>
    );
  }

  if (!enrollment) {
    return (
      <div className="alert alert-error mt-8" role="alert">
        <span>
          {problem ? errorMessage(t, problem) : t('detail.not-found')}
        </span>
        <Link href={buildListUrl()} className="btn btn-sm">
          {t('detail.back-to-list')}
        </Link>
      </div>
    );
  }

  const actionMap = new Map(
    enrollment.allowed_actions.map((a) => [a.action, a])
  );

  const handleResume = async () => {
    const preview = await previewCmd.run({
      enrollmentId,
      metaMode,
      expectedRevision: enrollment.revision,
    });
    if (preview.kind !== 'success') return;
    const outcome = await resumeCmd.run({
      enrollmentId,
      revision: enrollment.revision,
      metaMode,
      activationPreviewId: preview.data.id,
    });
    if (outcome.kind === 'success') refresh();
  };

  return (
    <>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <Link
            href={buildListUrl()}
            className="text-sm text-base-content/60 hover:underline"
          >
            {t('detail.back-to-list')}
          </Link>
          <h1 className="mt-1 text-2xl font-bold outline-none">
            {enrollment.id}
          </h1>
        </div>
        <div className="flex items-center gap-2">
          <LifecycleBadge scope="enrollment" value={enrollment.lifecycle} />
        </div>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-2" role="toolbar">
        <AllowedActionButton
          action={actionMap.get('activate')}
          label={t('detail.activate')}
          onClick={() => setDialog('activate')}
          variant="primary"
        />
        <AllowedActionButton
          action={actionMap.get('pause')}
          label={t('detail.pause')}
          onClick={() => setDialog('pause')}
          variant="warning"
        />
        <AllowedActionButton
          action={actionMap.get('resume')}
          label={t('detail.resume')}
          onClick={handleResume}
          variant="primary"
          busy={previewCmd.isPending || resumeCmd.isPending}
        />
        <AllowedActionButton
          action={actionMap.get('close')}
          label={t('detail.close')}
          onClick={() => setDialog('close')}
          variant="error"
        />
        <Link
          href={buildEvidenceUrl(enrollmentId)}
          className="btn btn-sm btn-ghost"
        >
          {t('detail.view-evidence')}
        </Link>
        <Link
          href={buildBreakdownsUrl(enrollmentId)}
          className="btn btn-sm btn-ghost"
        >
          {t('detail.view-breakdowns')}
        </Link>
      </div>

      {(previewCmd.problem ?? resumeCmd.problem) && (
        <p className="mt-2 text-sm text-error" role="alert">
          {errorMessage(t, previewCmd.problem ?? resumeCmd.problem)}
        </p>
      )}

      <dl className="mt-4 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
        <div>
          <dt className="text-xs text-base-content/60">{t('detail.market')}</dt>
          <dd>{enrollment.market_code}</dd>
        </div>
        <div>
          <dt className="text-xs text-base-content/60">
            {t('detail.meta-mode')}
          </dt>
          <dd>{t(metaModeLabelKey(enrollment.meta_mode))}</dd>
        </div>
        <div>
          <dt className="text-xs text-base-content/60">
            {t('detail.revision')}
          </dt>
          <dd>{enrollment.revision}</dd>
        </div>
        <div>
          <dt className="text-xs text-base-content/60">
            {t('detail.updated-at')}
          </dt>
          <dd>{formatDateTime(i18n.language, enrollment.updated_at)}</dd>
        </div>
      </dl>

      <div className="mt-4 rounded-lg border p-4">
        <h3 className="text-sm font-semibold">{t('export.heading')}</h3>
        <div className="mt-2">
          <ExportPanel
            enrollmentId={enrollmentId}
            episodeId={enrollment.current_episode_id}
            metaMode={metaMode}
          />
        </div>
      </div>

      <div className="mt-6 grid grid-cols-1 gap-4 lg:grid-cols-2">
        <EpisodePanel
          enrollmentId={enrollmentId}
          episodeId={enrollment.current_episode_id}
          metaMode={metaMode}
        />
        <BreakdownTable enrollmentId={enrollmentId} metaMode={metaMode} />
      </div>

      <div className="mt-6">
        <h2 className="text-lg font-semibold">{t('detail.advice-heading')}</h2>
        <AdviceThreadList metaMode={metaMode} enrollmentId={enrollmentId} />
      </div>

      <ActivateEnrollmentDialog
        open={dialog === 'activate'}
        enrollmentId={enrollmentId}
        revision={enrollment.revision}
        metaMode={metaMode}
        onClose={() => setDialog(null)}
        onActivated={() => {
          setDialog(null);
          refresh();
        }}
      />

      <LifecycleReasonDialog
        open={dialog === 'pause'}
        titleKey="detail.pause-dialog-title"
        confirmKey="detail.pause"
        problem={pauseCmd.problem}
        isPending={pauseCmd.isPending}
        onCancel={() => setDialog(null)}
        onConfirm={async (reason) => {
          const outcome = await pauseCmd.run({
            enrollmentId,
            revision: enrollment.revision,
            metaMode,
            reason,
          });
          if (outcome.kind === 'success') {
            setDialog(null);
            refresh();
          }
        }}
      />

      <LifecycleReasonDialog
        open={dialog === 'close'}
        titleKey="detail.close-dialog-title"
        confirmKey="detail.close"
        problem={closeCmd.problem}
        isPending={closeCmd.isPending}
        onCancel={() => setDialog(null)}
        onConfirm={async (reason) => {
          const outcome = await closeCmd.run({
            enrollmentId,
            revision: enrollment.revision,
            metaMode,
            reason,
          });
          if (outcome.kind === 'success') {
            setDialog(null);
            refresh();
          }
        }}
      />
    </>
  );
}
