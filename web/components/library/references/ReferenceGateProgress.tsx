import { useTranslation } from 'next-i18next';
import {
  CONTENT_SAFETY_UNAVAILABLE,
  REFERENCE_GATE_REJECTED,
  type UploadProgress,
  type UploadPhase,
} from '@/hooks/library/useLibraryReferenceUpload';
import { errorMessage } from '@/lib/i18n/domain-errors';

interface ReferenceGateProgressProps {
  progress: UploadProgress;
}

const PHASE_ORDER: UploadPhase[] = [
  'hashing',
  'creating_intent',
  'transferring',
  'finalizing',
  'processing',
  'creating_reference',
  'reference_processing',
  'promoting',
  'released',
];

export function ReferenceGateProgress({
  progress,
}: ReferenceGateProgressProps) {
  const { t } = useTranslation('library');
  const currentIndex = PHASE_ORDER.indexOf(progress.phase);

  if (progress.phase === 'failed') {
    const safetyUnavailable =
      progress.failureCode === CONTENT_SAFETY_UNAVAILABLE;
    return (
      <div role="alert">
        <p className="text-sm font-medium text-red-600 dark:text-red-400">
          {safetyUnavailable
            ? t('references.content-safety-unavailable')
            : t('references.upload-failed')}
        </p>
        {safetyUnavailable && (
          <p className="mt-1 text-xs text-red-500 dark:text-red-400">
            {t('references.content-safety-unavailable-hint')}
          </p>
        )}
        {progress.problem && !safetyUnavailable && (
          <p className="mt-1 text-xs text-red-500 dark:text-red-400">
            {errorMessage(t, progress.problem)}
          </p>
        )}
      </div>
    );
  }

  if (progress.phase === 'rejected') {
    return (
      <div role="alert">
        <p className="text-sm font-medium text-amber-600 dark:text-amber-400">
          {t('references.upload-rejected')}
        </p>
        {progress.problem &&
          progress.problem.code !== REFERENCE_GATE_REJECTED && (
            <p className="mt-1 text-xs text-amber-500 dark:text-amber-400">
              {errorMessage(t, progress.problem)}
            </p>
          )}
      </div>
    );
  }

  const pct =
    progress.totalBytes > 0
      ? Math.round((progress.bytesTransferred / progress.totalBytes) * 100)
      : 0;

  return (
    <div aria-busy="true">
      <p className="text-sm font-medium text-gray-900 dark:text-gray-100">
        {progress.fileName}
      </p>
      <div className="mt-2 space-y-1">
        {PHASE_ORDER.map((phase, i) => {
          const isDone = i < currentIndex;
          const isCurrent = phase === progress.phase;
          return (
            <div
              key={phase}
              className={`flex items-center gap-2 text-xs ${
                isDone
                  ? 'text-green-600 dark:text-green-400'
                  : isCurrent
                    ? 'text-blue-600 dark:text-blue-400 font-medium'
                    : 'text-gray-400 dark:text-gray-600'
              }`}
            >
              <span className="w-3 text-center">
                {isDone ? '✓' : isCurrent ? '●' : '○'}
              </span>
              <span>{t(`references.phase.${phase}`)}</span>
              {isCurrent && phase === 'transferring' && (
                <span className="ml-auto tabular-nums">{pct}%</span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
