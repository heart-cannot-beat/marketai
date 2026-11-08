import { useTranslation } from 'next-i18next';
import { FindingText } from '@/components/library/FindingText';
import type {
  ValidationFinding,
  ValidationResult,
} from '@/hooks/library/useDraftSession';

interface ValidationPanelProps {
  validation: ValidationResult;
  onAcknowledge: (code: string) => void;
}

export function ValidationPanel({
  validation,
  onAcknowledge,
}: ValidationPanelProps) {
  const { t } = useTranslation('library');
  const { blocking, warnings, acknowledged } = validation;

  if (blocking.length === 0 && warnings.length === 0) {
    return (
      <div
        className="rounded-lg border border-green-300 bg-green-50 p-4 dark:border-green-700 dark:bg-green-950"
        role="status"
      >
        <p className="text-sm text-green-800 dark:text-green-200">
          {t('draft.validation-passed')}
        </p>
      </div>
    );
  }

  return (
    <div
      className="space-y-3"
      role="region"
      aria-label={t('draft.validation-results')}
    >
      {blocking.length > 0 && (
        <div className="rounded-lg border border-red-300 bg-red-50 p-4 dark:border-red-700 dark:bg-red-950">
          <p
            className="text-sm font-semibold text-red-800 dark:text-red-200"
            role="status"
          >
            {t('draft.validation-failed')}
          </p>
          <p className="mt-1 text-sm text-red-800 dark:text-red-200">
            {t('draft.validation-errors', { count: String(blocking.length) })}
          </p>
          <ul className="mt-2 space-y-1" role="list">
            {blocking.map((finding, i) => (
              <FindingItem key={`${finding.code}-${i}`} finding={finding} />
            ))}
          </ul>
        </div>
      )}

      {warnings.length > 0 && (
        <div className="rounded-lg border border-amber-300 bg-amber-50 p-4 dark:border-amber-700 dark:bg-amber-950">
          <p className="text-sm font-medium text-amber-800 dark:text-amber-200">
            {t('draft.validation-warnings', { count: String(warnings.length) })}
          </p>
          <ul className="mt-2 space-y-2" role="list">
            {warnings.map((finding, i) => (
              <li
                key={`${finding.code}-${i}`}
                className="text-sm text-amber-700 dark:text-amber-300"
              >
                <div className="flex items-start justify-between gap-2">
                  <span>
                    <FindingText finding={finding} />
                  </span>
                  {!acknowledged.has(finding.code) && (
                    <button
                      type="button"
                      className="btn btn-xs btn-ghost"
                      onClick={() => onAcknowledge(finding.code)}
                      aria-label={t('draft.acknowledge-warning', {
                        code: finding.code,
                      })}
                    >
                      {t('draft.acknowledge')}
                    </button>
                  )}
                  {acknowledged.has(finding.code) && (
                    <span className="badge badge-xs badge-ghost">
                      {t('draft.acknowledged')}
                    </span>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function FindingItem({ finding }: { finding: ValidationFinding }) {
  return (
    <li className="text-sm text-red-700 dark:text-red-300" role="listitem">
      <FindingText finding={finding} />
    </li>
  );
}
