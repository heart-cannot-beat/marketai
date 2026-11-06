import { useTranslation } from 'next-i18next';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import { errorMessage } from '@/lib/i18n/domain-errors';

type TFunction = ReturnType<typeof useTranslation>['t'];

/** A server code without copy reads as its words, as `StatusBadge` does. */
export function codeWords(code: string): string {
  return code.replaceAll('_', ' ');
}

/**
 * Why a batch or Item action is unavailable (`LocalizationAction.reason_code`,
 * written by `_batch_actions` and `_item_actions` in `queries.py`), worded as
 * `action-reason.<code>` (#384).
 */
export function actionReasonMessage(t: TFunction, code: string): string {
  return t(`action-reason.${code}`, { defaultValue: codeWords(code) });
}

/** A hash short enough for a 390px line; the full value goes in a
 * copyable Technical details panel next to it. */
export function shortHash(value: string | null | undefined): string {
  if (!value) return '';
  return value.length > 16 ? `${value.slice(0, 12)}…` : value;
}

export function LocalizationProblem({
  problem,
  className = '',
}: {
  problem?: ProblemDetails;
  className?: string;
}) {
  const { t } = useTranslation('localization');
  if (!problem) return null;
  return (
    <div
      className={`alert alert-error items-start ${className}`.trim()}
      role="alert"
    >
      <div>
        <div className="font-semibold">{errorMessage(t, problem)}</div>
        <div className="mt-1 text-xs opacity-75">
          {t('problem.trace', {
            code: problem.code,
            trace: problem.trace_id || t('problem.trace-unavailable'),
          })}
        </div>
      </div>
    </div>
  );
}

export function LocalizationSection({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-xl border border-base-300 bg-base-100 p-4 shadow-sm">
      <div className="mb-3">
        <h2 className="text-base font-semibold">{title}</h2>
        {subtitle ? <p className="text-xs opacity-60">{subtitle}</p> : null}
      </div>
      {children}
    </section>
  );
}
