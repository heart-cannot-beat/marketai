import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import {
  CheckIcon,
  ExclamationTriangleIcon,
  MinusIcon,
} from '@heroicons/react/24/outline';
import {
  stepStates,
  type IntakeDetail,
  type StepKey,
  type StepState,
} from '@/lib/products/intake-model';

const STEPS: StepKey[] = ['fetch', 'extract', 'images'];

export function MockIntakeBadge({ mode }: { mode: string | undefined }) {
  const { t } = useTranslation('products');
  if (mode !== 'mock' && mode !== 'controlled_mock') return null;
  return (
    <span
      className="badge badge-warning badge-outline gap-1 whitespace-normal py-3 text-left"
      title={t('mock-badge-hint')}
      data-testid="intake-mock-badge"
    >
      {t('mock-badge')}
    </span>
  );
}

function StepMarker({ state, index }: { state: StepState; index: number }) {
  const base =
    'flex h-7 w-7 shrink-0 items-center justify-center rounded-full border text-xs font-semibold';
  if (state === 'done')
    return (
      <span
        className={classNames(
          base,
          'border-success bg-success text-success-content'
        )}
      >
        <CheckIcon className="h-4 w-4" aria-hidden="true" />
      </span>
    );
  if (state === 'failed')
    return (
      <span
        className={classNames(base, 'border-error bg-error text-error-content')}
      >
        <ExclamationTriangleIcon className="h-4 w-4" aria-hidden="true" />
      </span>
    );
  if (state === 'skipped')
    return (
      <span
        className={classNames(base, 'border-base-300 text-base-content/40')}
      >
        <MinusIcon className="h-4 w-4" aria-hidden="true" />
      </span>
    );
  if (state === 'active')
    return (
      <span className={classNames(base, 'border-primary text-primary')}>
        <span
          className="loading loading-spinner loading-xs"
          aria-hidden="true"
        />
      </span>
    );
  return (
    <span className={classNames(base, 'border-base-300 text-base-content/50')}>
      {index + 1}
    </span>
  );
}

interface IntakeProgressProps {
  detail: IntakeDetail | null;
  stalled: boolean;
  onStartOver: () => void;
  onKeepWaiting: () => void;
}

/**
 * "识别中" (front-flow contract §3.1): the three server steps with their
 * state as text and icon (never colour alone). Abandoning only stops this
 * page from waiting; there is no remote cancel for an intake.
 */
export function IntakeProgress({
  detail,
  stalled,
  onStartOver,
  onKeepWaiting,
}: IntakeProgressProps) {
  const { t } = useTranslation('products');
  const states = detail
    ? stepStates(detail)
    : ({ fetch: 'pending', extract: 'pending', images: 'pending' } as Record<
        StepKey,
        StepState
      >);
  const queued = !detail || detail.status === 'queued';

  return (
    <section
      className="rounded-box border border-base-300 bg-base-100 p-6"
      aria-labelledby="intake-progress-title"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 id="intake-progress-title" className="text-lg font-semibold">
          {t('progress-title')}
        </h2>
        <MockIntakeBadge mode={detail?.execution_mode} />
      </div>
      <p className="mt-1 text-sm text-base-content/60" aria-live="polite">
        {queued ? t('progress-queued') : null}
      </p>

      <ol className="mt-4 space-y-3" aria-live="polite">
        {STEPS.map((step, index) => (
          <li
            key={step}
            className="flex items-center gap-3"
            data-testid={`intake-step-${step}`}
            data-state={states[step]}
          >
            <StepMarker state={states[step]} index={index} />
            <span
              className={classNames(
                'font-medium',
                states[step] === 'pending' || states[step] === 'skipped'
                  ? 'text-base-content/50'
                  : undefined
              )}
            >
              {t(`step-${step}`)}
            </span>
            <span className="text-sm text-base-content/60">
              {t(`step-state-${states[step]}`)}
            </span>
          </li>
        ))}
      </ol>

      {stalled && (
        <div className="alert mt-4" role="status">
          <span>{t('progress-stalled')}</span>
          <button type="button" className="btn btn-sm" onClick={onKeepWaiting}>
            {t('progress-keep-waiting')}
          </button>
        </div>
      )}

      <div className="mt-6">
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={onStartOver}
        >
          {t('progress-start-over')}
        </button>
      </div>
    </section>
  );
}
