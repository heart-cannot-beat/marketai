import { useId } from 'react';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { costLabel } from '@/lib/ideas/first-batch-view';
import { previewCost } from '@/hooks/ideas/useIdeasCommands';
import type { PaidImage } from '@/hooks/ideas/usePaidImage';
import { ProblemNote } from './ProblemNote';

export const BUTTON =
  'inline-flex min-h-11 items-center justify-center rounded-lg border border-gray-300 bg-white px-4 text-sm font-semibold text-gray-900 hover:border-gray-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100';
/** A toggle that is on: a style of its own, never `BUTTON` plus overrides (they would clash). */
export const PRESSED =
  'inline-flex min-h-11 items-center justify-center rounded-lg border border-gray-900 bg-gray-900 px-4 text-sm font-semibold text-white dark:border-gray-100 dark:bg-gray-100 dark:text-gray-900';
export const PRIMARY =
  'inline-flex min-h-11 items-center justify-center rounded-lg bg-gray-900 px-4 text-sm font-semibold text-white hover:bg-gray-700 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-gray-100 dark:text-gray-900';

/**
 * A paid image's buttons (#201): the first asks what it costs at most
 * (nothing is spent), the second carries that maximum and starts it. The
 * maximum is the server's preview, so the click is the confirmation.
 */
export function PaidImageAction({
  paid,
  startLabel,
  confirmLabel,
  onStart,
  disabled,
  className,
  primary,
}: {
  paid: PaidImage;
  /** Before the quote, e.g. "Redo this one". */
  startLabel?: string;
  /** With the maximum, e.g. "Redo this one · up to $0.06" (`cost` interpolated). */
  confirmLabel: (cost: string) => string;
  /** Asks for the quote; without it another control starts it, and only the confirmation shows here. */
  onStart?: () => void;
  disabled?: boolean;
  className?: string;
  primary?: boolean;
}) {
  const { t, i18n } = useTranslation('ideas');
  const note = useId();
  const { phase } = paid;
  const style = primary ? PRIMARY : BUTTON;
  if (phase.kind === 'started')
    return (
      <p className="text-sm text-gray-700 dark:text-gray-300" role="status">
        {t('paid-started')}
      </p>
    );
  if (phase.kind === 'quoted' || phase.kind === 'submitting') {
    const cost = costLabel(i18n.language, previewCost(phase.preview));
    return (
      <div className={classNames('space-y-2', className)}>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            className={PRIMARY}
            aria-describedby={note}
            disabled={phase.kind === 'submitting'}
            onClick={() => void paid.confirm()}
          >
            {confirmLabel(cost)}
          </button>
          <button
            type="button"
            className="min-h-11 px-2 text-sm font-medium underline"
            disabled={phase.kind === 'submitting'}
            onClick={paid.cancel}
          >
            {t('paid-cancel')}
          </button>
        </div>
        <p id={note} className="text-xs text-gray-600 dark:text-gray-400">
          {phase.kind === 'quoted' && phase.uncertain
            ? t('paid-uncertain')
            : t('paid-note')}
        </p>
      </div>
    );
  }
  if (!onStart && phase.kind === 'idle') return null;
  return (
    <div className={classNames('space-y-2', className)}>
      {onStart ? (
        <button
          type="button"
          className={style}
          disabled={disabled || phase.kind === 'quoting'}
          onClick={onStart}
        >
          {phase.kind === 'quoting' ? t('paid-quoting') : startLabel}
        </button>
      ) : (
        phase.kind === 'quoting' && (
          <p className="text-sm" role="status">
            {t('paid-quoting')}
          </p>
        )
      )}
      {phase.kind === 'problem' && <ProblemNote problem={phase.problem} />}
    </div>
  );
}
