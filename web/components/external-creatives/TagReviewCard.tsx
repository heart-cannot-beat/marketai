import { useEffect, useId, useState } from 'react';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { CheckCircleIcon } from '@heroicons/react/20/solid';
import {
  useAssetImageUrl,
  type AssetThumbnailLink,
} from '@/hooks/assets/useAssetImageUrl';
import {
  buildConfirmBody,
  directionKey,
  initialChoices,
  initialDirection,
  isConfirmable,
  isSettled,
  parseDirectionKey,
  suggestionFor,
  type DirectionChoiceValue,
  type ExternalConfirmRequest,
  type ExternalCreative,
  type TagChoices,
  type VocabularyFactor,
} from '@/lib/external-creatives/model';
import { errorCodeMessage } from '@/lib/i18n/domain-errors';

interface TagReviewCardProps {
  creative: ExternalCreative;
  confirming: boolean;
  onConfirm: (creativeId: string, body: ExternalConfirmRequest) => void;
}

function Preview({ creative }: { creative: ExternalCreative }) {
  const { t } = useTranslation('external');
  const { blobUrl, isLoading } = useAssetImageUrl(
    creative.asset_version_id ?? undefined,
    { thumbnail: (creative.thumbnail as AssetThumbnailLink | null) ?? null }
  );
  return (
    <div className="relative flex h-40 w-32 shrink-0 items-center justify-center overflow-hidden rounded-md bg-base-200">
      {blobUrl ? (
        <img
          src={blobUrl}
          alt={t('preview-alt')}
          className="h-full w-full object-contain"
        />
      ) : (
        (isLoading || !isSettled(creative.status)) && (
          <span
            className="loading loading-spinner loading-sm text-base-content/40"
            aria-hidden="true"
          />
        )
      )}
    </div>
  );
}

interface FactorRowProps {
  creative: ExternalCreative;
  factor: VocabularyFactor;
  value: string | null;
  disabled: boolean;
  onChange: (value: string | null) => void;
}

/**
 * One factor as a single-choice chip row. The AI's pick carries a thin bar
 * filled to its confidence -- the one place the card shows how sure the
 * model was, so a weak guess reads as weak before anyone trusts it.
 */
function FactorRow({
  creative,
  factor,
  value,
  disabled,
  onChange,
}: FactorRowProps) {
  const { t } = useTranslation('external');
  const labelId = useId();
  const suggested = suggestionFor(creative, factor.factor_id);
  const options: { id: string | null; label: string }[] = [
    ...(factor.values ?? []).map((item) => ({
      id: item.value_id,
      label: item.label,
    })),
    { id: null, label: t('none-of-these') },
  ];
  return (
    <div className="space-y-1">
      <p id={labelId} className="text-sm font-medium">
        {factor.label}
      </p>
      <div
        role="radiogroup"
        aria-labelledby={labelId}
        className="flex flex-wrap gap-1.5"
      >
        {options.map((option) => {
          const checked = value === option.id;
          const pick =
            suggested && (suggested.value_id ?? null) === option.id
              ? Math.round(suggested.confidence * 100)
              : null;
          const isAiPick = pick !== null;
          return (
            <button
              key={option.id ?? 'none'}
              type="button"
              role="radio"
              aria-checked={checked}
              disabled={disabled}
              onClick={() => onChange(option.id)}
              title={
                isAiPick
                  ? option.id
                    ? t('ai-pick', { percent: pick })
                    : t('ai-none')
                  : undefined
              }
              className={classNames(
                'relative overflow-hidden rounded-full border px-3 pb-1.5 pt-1 text-sm transition-colors',
                'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary',
                checked
                  ? 'border-primary bg-primary text-primary-content'
                  : 'border-base-300 bg-base-100 hover:border-primary/60',
                option.id === null && !checked && 'text-base-content/60',
                disabled && 'cursor-not-allowed opacity-60'
              )}
            >
              {option.label}
              {isAiPick && option.id && (
                <span
                  aria-hidden="true"
                  data-testid={`confidence-${factor.factor_id}`}
                  className={classNames(
                    'absolute bottom-0 left-0 h-1',
                    checked ? 'bg-primary-content/70' : 'bg-primary/70'
                  )}
                  style={{ width: `${pick}%` }}
                />
              )}
            </button>
          );
        })}
      </div>
      {/* A controlled Mock reason is a fixed server sentence, not a model's
          reading of this image; the card's `ai-mock` note already says so. */}
      {suggested?.reason && creative.suggestion?.execution_mode !== 'mock' && (
        <p className="text-xs text-base-content/60">{suggested.reason}</p>
      )}
    </div>
  );
}

export function TagReviewCard({
  creative,
  confirming,
  onConfirm,
}: TagReviewCardProps) {
  const { t } = useTranslation('external');
  const directionId = useId();
  const [choices, setChoices] = useState<TagChoices>(() =>
    initialChoices(creative)
  );
  const [direction, setDirection] = useState<DirectionChoiceValue | null>(() =>
    initialDirection(creative)
  );
  const suggestionArrived = Boolean(creative.suggestion);

  // The suggestion lands after the card first rendered (the Worker was
  // still tagging): adopt it once, before the user touched anything.
  useEffect(() => {
    if (suggestionArrived) setChoices(initialChoices(creative));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [suggestionArrived]);

  const confirmable = isConfirmable(creative);
  const confirmation = creative.confirmation;
  const factors = creative.vocabulary.factors ?? [];
  const directions = creative.vocabulary.directions ?? [];
  const mock = creative.suggestion?.execution_mode === 'mock';

  return (
    <article
      className="flex flex-col gap-4 rounded-lg border border-base-300 p-4 sm:flex-row"
      aria-label={creative.filename || creative.external_creative_id}
      data-testid="tag-review-card"
    >
      <Preview creative={creative} />
      <div className="min-w-0 flex-1 space-y-3">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <p className="truncate text-sm font-medium">
            {creative.filename || t('preview-alt')}
          </p>
          <p
            className={classNames(
              'flex items-center gap-1 text-sm',
              creative.status === 'confirmed' && 'text-success',
              (creative.status === 'rejected' ||
                creative.status === 'failed') &&
                'text-error'
            )}
            role="status"
          >
            {creative.status === 'confirmed' && (
              <CheckCircleIcon className="h-4 w-4" aria-hidden="true" />
            )}
            {!isSettled(creative.status) && (
              <span
                className="loading loading-spinner loading-xs"
                aria-hidden="true"
              />
            )}
            {t(`status-${creative.status}`)}
          </p>
        </div>
        {creative.asset_code && (
          <p className="text-xs text-base-content/60">
            {t('asset-code', { code: creative.asset_code })}
          </p>
        )}
        {creative.failure && !confirmable && (
          <p className="text-sm text-error" role="alert">
            {errorCodeMessage(t, creative.failure.code)}
          </p>
        )}

        {confirmable && factors.length === 0 && (
          <p className="max-w-prose text-sm text-base-content/70">
            {t('no-vocabulary')}
          </p>
        )}
        {confirmable && mock && (
          <p className="text-xs text-warning">{t('ai-mock')}</p>
        )}
        {confirmable &&
          factors.map((factor) => (
            <FactorRow
              key={factor.factor_id}
              creative={creative}
              factor={factor}
              value={choices[factor.factor_id] ?? null}
              disabled={confirming}
              onChange={(value) =>
                setChoices((previous) => ({
                  ...previous,
                  [factor.factor_id]: value,
                }))
              }
            />
          ))}
        {confirmable && directions.length > 0 && (
          <label className="form-control max-w-sm" htmlFor={directionId}>
            <span className="label-text text-sm">{t('direction-label')}</span>
            <select
              id={directionId}
              className="select select-bordered select-sm"
              value={directionKey(direction)}
              disabled={confirming}
              onChange={(event) =>
                setDirection(parseDirectionKey(event.target.value))
              }
            >
              <option value="">{t('direction-none')}</option>
              {directions.map((item) => (
                <option
                  key={`${item.plan_id}::${item.direction_key}`}
                  value={`${item.plan_id}::${item.direction_key}`}
                >
                  {t('direction-option', {
                    round: item.round ?? 1,
                    title: item.title,
                  })}
                </option>
              ))}
            </select>
          </label>
        )}

        {confirmation && (
          <ul className="space-y-0.5 text-sm text-base-content/80">
            <li>
              {confirmation.experiment_ready
                ? t('confirmed-ready')
                : t('confirmed-not-ready')}
            </li>
            {confirmation.attributable && (
              <li>
                {t('confirmed-attributable', {
                  round: confirmation.round ?? 1,
                })}
              </li>
            )}
          </ul>
        )}

        {confirmable && (
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={confirming}
            onClick={() =>
              onConfirm(
                creative.external_creative_id,
                buildConfirmBody(creative, choices, direction)
              )
            }
          >
            {confirming
              ? t('confirming')
              : confirmation
                ? t('confirm-update')
                : t('confirm')}
          </button>
        )}
      </div>
    </article>
  );
}
