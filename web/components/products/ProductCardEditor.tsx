import { useMemo, useState, type ReactNode } from 'react';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { PlusIcon, TrashIcon, PhotoIcon } from '@heroicons/react/24/outline';
import { useIntakeImageUrl } from '@/hooks/products/useIntakeImageUrl';
import {
  buildConfirmRequest,
  cardDraftFrom,
  MAX_AUDIENCES,
  MAX_BOUND_IMAGES,
  MAX_MUST_MATCH,
  MAX_SELLING_POINTS,
  MUST_MATCH_TYPES,
  hasVariantImages,
  needsVariantChoice,
  selectVariant,
  validateCard,
  variantDifferences,
  variantImages,
  type CardDraft,
  type ConfirmRequest,
  type IntakeCard,
  type IntakeImage,
  type IntakeVariant,
  type MustMatchType,
  type Severity,
} from '@/lib/products/intake-model';
import { MockIntakeBadge } from './IntakeProgress';

interface ProductCardEditorProps {
  intakeId: string;
  card: IntakeCard;
  executionMode?: string;
  confirming: boolean;
  /** Already translated message from a refused confirm. */
  confirmProblem: string | null;
  onConfirm: (body: ConfirmRequest) => void;
  onStartOver: () => void;
}

function Section({
  id,
  title,
  hint,
  children,
}: {
  id: string;
  title: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <section aria-labelledby={id} className="border-t border-base-300 pt-6">
      <h3 id={id} className="text-base font-semibold">
        {title}
      </h3>
      {hint && <p className="mt-0.5 text-sm text-base-content/60">{hint}</p>}
      <div className="mt-3">{children}</div>
    </section>
  );
}

/** One "选择款式" choice: cover image, name and what differs. */
function VariantChoice({
  intakeId,
  card,
  variant,
  checked,
  onChoose,
}: {
  intakeId: string;
  card: IntakeCard;
  variant: IntakeVariant;
  checked: boolean;
  onChoose: () => void;
}) {
  const { t } = useTranslation('products');
  const cover = variantImages(card, variant)[0] ?? null;
  const { blobUrl } = useIntakeImageUrl(
    intakeId,
    cover?.image_id ?? null,
    Boolean(cover)
  );
  const differences = variantDifferences(variant, (key, value) =>
    t('card-variant-spec', { name: key, value })
  );
  return (
    <label
      className={classNames(
        'flex cursor-pointer gap-3 rounded-lg border p-3',
        checked ? 'border-primary bg-primary/5' : 'border-base-300'
      )}
      data-testid="variant-choice"
    >
      <input
        type="radio"
        name="intake-variant"
        aria-label={variant.label}
        className="radio radio-sm mt-0.5"
        checked={checked}
        onChange={onChoose}
      />
      <span className="flex h-14 w-14 shrink-0 items-center justify-center overflow-hidden rounded bg-base-200">
        {blobUrl ? (
          <img
            src={blobUrl}
            alt={t('card-variant-cover', { label: variant.label })}
            className="h-full w-full object-contain"
          />
        ) : (
          <PhotoIcon
            className="h-6 w-6 text-base-content/30"
            aria-hidden="true"
          />
        )}
      </span>
      <span className="min-w-0">
        <span className="block font-medium">{variant.label}</span>
        {differences.length > 0 && (
          <span className="block text-sm text-base-content/60">
            {differences.join(t('list-separator'))}
          </span>
        )}
      </span>
    </label>
  );
}

function CandidateImage({
  intakeId,
  image,
  index,
  included,
  main,
  variantTag,
  onToggle,
  onMakeMain,
}: {
  intakeId: string;
  image: IntakeImage;
  index: number;
  included: boolean;
  main: boolean;
  /** "通用" or the variant's name, when the card ties images to variants. */
  variantTag: string | null;
  onToggle: () => void;
  onMakeMain: () => void;
}) {
  const { t } = useTranslation('products');
  const { blobUrl, isLoading } = useIntakeImageUrl(
    intakeId,
    image.image_id,
    image.selectable
  );
  const alt = t('card-image-alt', { index: index + 1 });

  return (
    <li
      className={classNames(
        'flex flex-col overflow-hidden rounded-lg border',
        main ? 'border-primary ring-2 ring-primary/30' : 'border-base-300',
        !image.selectable && 'opacity-60'
      )}
      data-testid="intake-image"
    >
      <div className="relative flex aspect-square items-center justify-center bg-base-200">
        {blobUrl ? (
          <img
            src={blobUrl}
            alt={alt}
            className="h-full w-full object-contain"
          />
        ) : isLoading ? (
          <span
            className="loading loading-spinner loading-sm"
            aria-hidden="true"
          />
        ) : (
          <PhotoIcon
            className="h-8 w-8 text-base-content/30"
            aria-label={alt}
          />
        )}
        {main && (
          <span className="badge badge-primary badge-sm absolute left-2 top-2">
            {t('card-image-main')}
          </span>
        )}
        {variantTag && (
          <span
            className="badge badge-ghost badge-sm absolute bottom-2 left-2 max-w-[90%] truncate"
            data-testid="intake-image-variant"
          >
            {variantTag}
          </span>
        )}
      </div>
      {image.selectable ? (
        <div className="space-y-1 p-2 text-sm">
          <label className="flex cursor-pointer items-center gap-2">
            <input
              type="checkbox"
              className="checkbox checkbox-sm"
              checked={included}
              onChange={onToggle}
              aria-label={`${t('card-image-include')} (${alt})`}
            />
            <span>{t('card-image-include')}</span>
          </label>
          <label className="flex cursor-pointer items-center gap-2">
            <input
              type="radio"
              name="intake-main-image"
              className="radio radio-sm"
              checked={main}
              onChange={onMakeMain}
              aria-label={`${t('card-image-set-main')} (${alt})`}
            />
            <span>{t('card-image-set-main')}</span>
          </label>
        </div>
      ) : (
        <p className="p-2 text-xs text-base-content/60">
          {t('card-image-unavailable')}
        </p>
      )}
    </li>
  );
}

/**
 * The AI's product card as an editable first draft (front-flow contract
 * §3.1): images with a main-image choice, the model when the page sells
 * several, name, description, specs, selling points, audience cards with a
 * primary choice and must-match items with their severity. Everything can
 * be changed or removed; nothing is saved until "确认产品".
 */
export function ProductCardEditor({
  intakeId,
  card,
  executionMode,
  confirming,
  confirmProblem,
  onConfirm,
  onStartOver,
}: ProductCardEditorProps) {
  const { t } = useTranslation('products');
  const [draft, setDraft] = useState<CardDraft>(() => cardDraftFrom(card));
  const [showProblems, setShowProblems] = useState(false);
  const problems = useMemo(() => validateCard(draft, card), [draft, card]);
  const images = useMemo(
    () =>
      [...(card.images ?? [])].sort(
        (a, b) =>
          Number(b.selectable) - Number(a.selectable) ||
          (a.rank ?? 999) - (b.rank ?? 999)
      ),
    [card.images]
  );
  const variants = card.model_variants ?? [];
  const tagImages = variants.length > 1 && hasVariantImages(card);
  const variantPending = needsVariantChoice(draft, card);

  const update = (patch: Partial<CardDraft>) =>
    setDraft((current) => ({ ...current, ...patch }));

  function chooseVariant(label: string) {
    setDraft((current) => selectVariant(current, card, label));
  }

  function toggleImage(imageId: string) {
    setDraft((current) => {
      const included = current.includedImageIds.includes(imageId)
        ? current.includedImageIds.filter((id) => id !== imageId)
        : [...current.includedImageIds, imageId].slice(-MAX_BOUND_IMAGES);
      const mainImageId =
        current.mainImageId && included.includes(current.mainImageId)
          ? current.mainImageId
          : (included[0] ?? null);
      return { ...current, includedImageIds: included, mainImageId };
    });
  }

  function makeMain(imageId: string) {
    setDraft((current) => ({
      ...current,
      mainImageId: imageId,
      includedImageIds: current.includedImageIds.includes(imageId)
        ? current.includedImageIds
        : [...current.includedImageIds, imageId].slice(-MAX_BOUND_IMAGES),
    }));
  }

  function handleConfirm() {
    if (problems.length > 0) {
      setShowProblems(true);
      return;
    }
    onConfirm(buildConfirmRequest(draft, card));
  }

  const droppedCount = card.dropped_commercial_specs?.length ?? 0;

  return (
    <section
      className="rounded-box border border-base-300 bg-base-100 p-6"
      aria-labelledby="product-card-title"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="max-w-prose">
          <h2 id="product-card-title" className="text-lg font-semibold">
            {t('card-title')}
          </h2>
          <p className="mt-1 text-sm text-base-content/70">{t('card-intro')}</p>
        </div>
        <MockIntakeBadge mode={executionMode} />
      </div>

      <div className="mt-6 space-y-6">
        {variants.length > 1 && (
          <Section
            id="card-variant"
            title={t('card-section-variant')}
            hint={t('card-variant-hint')}
          >
            <div
              role="radiogroup"
              aria-labelledby="card-variant"
              className="grid gap-2 sm:grid-cols-2"
            >
              {variants.map((variant) => (
                <VariantChoice
                  key={variant.label}
                  intakeId={intakeId}
                  card={card}
                  variant={variant}
                  checked={draft.selectedVariant === variant.label}
                  onChoose={() => chooseVariant(variant.label)}
                />
              ))}
            </div>
          </Section>
        )}

        <Section
          id="card-images"
          title={t('card-section-images')}
          hint={images.length > 0 ? t('card-images-hint') : undefined}
        >
          {images.length > 0 ? (
            <ul className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
              {images.map((image, index) => (
                <CandidateImage
                  key={image.image_id}
                  intakeId={intakeId}
                  image={image}
                  index={index}
                  included={draft.includedImageIds.includes(image.image_id)}
                  main={draft.mainImageId === image.image_id}
                  variantTag={
                    tagImages
                      ? (image.variant_label ?? t('card-image-general'))
                      : null
                  }
                  onToggle={() => toggleImage(image.image_id)}
                  onMakeMain={() => makeMain(image.image_id)}
                />
              ))}
            </ul>
          ) : (
            <p className="text-sm text-base-content/60">
              {t('card-images-none')}
            </p>
          )}
        </Section>

        <Section id="card-basics" title={t('card-section-basics')}>
          <div className="grid gap-3">
            <label className="form-control">
              <span className="label-text mb-1">{t('card-name')}</span>
              <input
                className="input input-bordered"
                value={draft.name}
                maxLength={200}
                onChange={(event) => update({ name: event.target.value })}
              />
            </label>
            <label className="form-control">
              <span className="label-text mb-1">{t('card-description')}</span>
              <textarea
                className="textarea textarea-bordered min-h-20"
                value={draft.description}
                maxLength={2000}
                onChange={(event) =>
                  update({ description: event.target.value })
                }
              />
            </label>
          </div>
        </Section>

        <Section id="card-specs" title={t('card-section-specs')}>
          {draft.specs.length === 0 && (
            <p className="mb-2 text-sm text-base-content/60">
              {t('card-specs-empty')}
            </p>
          )}
          <ul className="space-y-2">
            {draft.specs.map((row, index) => (
              <li key={index} className="flex gap-2">
                <input
                  className="input input-bordered input-sm w-2/5"
                  aria-label={`${t('card-spec-key')} ${index + 1}`}
                  placeholder={t('card-spec-key')}
                  value={row.key}
                  onChange={(event) =>
                    update({
                      specs: draft.specs.map((item, i) =>
                        i === index
                          ? { ...item, key: event.target.value }
                          : item
                      ),
                    })
                  }
                />
                <input
                  className="input input-bordered input-sm flex-1"
                  aria-label={`${t('card-spec-value')} ${index + 1}`}
                  placeholder={t('card-spec-value')}
                  value={row.value}
                  onChange={(event) =>
                    update({
                      specs: draft.specs.map((item, i) =>
                        i === index
                          ? { ...item, value: event.target.value }
                          : item
                      ),
                    })
                  }
                />
                <button
                  type="button"
                  className="btn btn-ghost btn-sm btn-square"
                  aria-label={t('card-spec-remove', { index: index + 1 })}
                  onClick={() =>
                    update({ specs: draft.specs.filter((_, i) => i !== index) })
                  }
                >
                  <TrashIcon className="h-4 w-4" aria-hidden="true" />
                </button>
              </li>
            ))}
          </ul>
          <button
            type="button"
            className="btn btn-ghost btn-sm mt-2"
            onClick={() =>
              update({ specs: [...draft.specs, { key: '', value: '' }] })
            }
          >
            <PlusIcon className="h-4 w-4" aria-hidden="true" />
            {t('card-spec-add')}
          </button>
          {droppedCount > 0 && (
            <p className="mt-2 text-sm text-base-content/60">
              {t('card-dropped-commercial', { count: droppedCount })}
            </p>
          )}
        </Section>

        <Section
          id="card-selling"
          title={t('card-section-selling')}
          hint={t('card-selling-hint')}
        >
          <ul className="space-y-2">
            {draft.sellingPoints.map((point, index) => (
              <li key={index} className="flex gap-2">
                <input
                  className="input input-bordered input-sm flex-1"
                  aria-label={t('card-selling-item', { index: index + 1 })}
                  value={point}
                  onChange={(event) =>
                    update({
                      sellingPoints: draft.sellingPoints.map((item, i) =>
                        i === index ? event.target.value : item
                      ),
                    })
                  }
                />
                <button
                  type="button"
                  className="btn btn-ghost btn-sm btn-square"
                  aria-label={t('card-selling-remove', { index: index + 1 })}
                  onClick={() =>
                    update({
                      sellingPoints: draft.sellingPoints.filter(
                        (_, i) => i !== index
                      ),
                    })
                  }
                >
                  <TrashIcon className="h-4 w-4" aria-hidden="true" />
                </button>
              </li>
            ))}
          </ul>
          <button
            type="button"
            className="btn btn-ghost btn-sm mt-2"
            disabled={draft.sellingPoints.length >= MAX_SELLING_POINTS}
            onClick={() =>
              update({ sellingPoints: [...draft.sellingPoints, ''] })
            }
          >
            <PlusIcon className="h-4 w-4" aria-hidden="true" />
            {t('card-selling-add')}
          </button>
        </Section>

        <Section
          id="card-audiences"
          title={t('card-section-audiences')}
          hint={t('card-audiences-hint')}
        >
          <ul className="grid gap-3 md:grid-cols-2">
            {draft.audiences.map((audience, index) => {
              const primary = draft.primaryAudience === index;
              const setField = (field: 'who' | 'why' | 'pain', value: string) =>
                update({
                  audiences: draft.audiences.map((item, i) =>
                    i === index ? { ...item, [field]: value } : item
                  ),
                });
              return (
                <li
                  key={index}
                  className={classNames(
                    'space-y-2 rounded-lg border p-3',
                    primary ? 'border-primary' : 'border-base-300'
                  )}
                  data-testid="audience-card"
                >
                  <div className="flex items-center justify-between gap-2">
                    <label className="flex cursor-pointer items-center gap-2 text-sm font-medium">
                      <input
                        type="radio"
                        name="intake-primary-audience"
                        className="radio radio-sm"
                        checked={primary}
                        onChange={() => update({ primaryAudience: index })}
                        aria-label={`${t('card-audience-primary')} (${t('card-audience-heading', { index: index + 1 })})`}
                      />
                      {primary
                        ? t('card-audience-primary')
                        : t('card-audience-heading', { index: index + 1 })}
                    </label>
                    <button
                      type="button"
                      className="btn btn-ghost btn-xs btn-square"
                      aria-label={t('card-audience-remove', {
                        index: index + 1,
                      })}
                      onClick={() => {
                        const audiences = draft.audiences.filter(
                          (_, i) => i !== index
                        );
                        const primaryAudience =
                          draft.primaryAudience === index
                            ? 0
                            : draft.primaryAudience > index
                              ? draft.primaryAudience - 1
                              : draft.primaryAudience;
                        update({ audiences, primaryAudience });
                      }}
                    >
                      <TrashIcon className="h-4 w-4" aria-hidden="true" />
                    </button>
                  </div>
                  {(['who', 'why', 'pain'] as const).map((field) => (
                    <label key={field} className="form-control">
                      <span className="label-text text-xs text-base-content/60">
                        {t(`card-audience-${field}`)}
                      </span>
                      <input
                        className="input input-bordered input-sm"
                        aria-label={`${t(`card-audience-${field}`)} (${t('card-audience-heading', { index: index + 1 })})`}
                        value={audience[field]}
                        maxLength={500}
                        onChange={(event) =>
                          setField(field, event.target.value)
                        }
                      />
                    </label>
                  ))}
                </li>
              );
            })}
          </ul>
          <button
            type="button"
            className="btn btn-ghost btn-sm mt-2"
            disabled={draft.audiences.length >= MAX_AUDIENCES}
            onClick={() =>
              update({
                audiences: [...draft.audiences, { who: '', why: '', pain: '' }],
              })
            }
          >
            <PlusIcon className="h-4 w-4" aria-hidden="true" />
            {t('card-audience-add')}
          </button>
        </Section>

        <Section
          id="card-must-match"
          title={t('card-section-must-match')}
          hint={t('card-must-match-hint')}
        >
          {draft.mustMatch.length === 0 && (
            <p className="mb-2 text-sm text-base-content/60">
              {t('card-must-match-empty')}
            </p>
          )}
          <ul className="space-y-2">
            {draft.mustMatch.map((item, index) => {
              const patch = (next: Partial<typeof item>) =>
                update({
                  mustMatch: draft.mustMatch.map((entry, i) =>
                    i === index ? { ...entry, ...next } : entry
                  ),
                });
              return (
                <li
                  key={index}
                  className="flex flex-wrap items-center gap-2 rounded-lg border border-base-300 p-2"
                  data-testid="must-match-item"
                >
                  <select
                    className="select select-bordered select-sm"
                    aria-label={`${t('card-must-match-type')} ${index + 1}`}
                    value={item.type}
                    onChange={(event) =>
                      patch({ type: event.target.value as MustMatchType })
                    }
                  >
                    {MUST_MATCH_TYPES.map((type) => (
                      <option key={type} value={type}>
                        {t(`card-must-match-type-${type}`)}
                      </option>
                    ))}
                  </select>
                  <input
                    className="input input-bordered input-sm min-w-[12rem] flex-1"
                    aria-label={t('card-must-match-description', {
                      index: index + 1,
                    })}
                    value={item.description}
                    maxLength={500}
                    onChange={(event) =>
                      patch({ description: event.target.value })
                    }
                  />
                  <div
                    className="join"
                    role="radiogroup"
                    aria-label={`${t('card-must-match-severity')} ${index + 1}`}
                  >
                    {(['blocking', 'warning'] as Severity[]).map((severity) => (
                      <button
                        key={severity}
                        type="button"
                        role="radio"
                        aria-checked={item.severity === severity}
                        className={classNames(
                          'btn join-item btn-xs',
                          item.severity === severity && 'btn-active'
                        )}
                        onClick={() => patch({ severity })}
                      >
                        {t(`card-must-match-severity-${severity}`)}
                      </button>
                    ))}
                  </div>
                  {item.proposedByAi && (
                    <span className="badge badge-ghost badge-sm">
                      {t('card-must-match-ai')}
                    </span>
                  )}
                  <button
                    type="button"
                    className="btn btn-ghost btn-xs btn-square"
                    aria-label={t('card-must-match-remove', {
                      index: index + 1,
                    })}
                    onClick={() =>
                      update({
                        mustMatch: draft.mustMatch.filter(
                          (_, i) => i !== index
                        ),
                      })
                    }
                  >
                    <TrashIcon className="h-4 w-4" aria-hidden="true" />
                  </button>
                </li>
              );
            })}
          </ul>
          <button
            type="button"
            className="btn btn-ghost btn-sm mt-2"
            disabled={draft.mustMatch.length >= MAX_MUST_MATCH}
            onClick={() =>
              update({
                mustMatch: [
                  ...draft.mustMatch,
                  {
                    id: null,
                    type: 'visual',
                    description: '',
                    severity: 'blocking',
                    proposedByAi: false,
                  },
                ],
              })
            }
          >
            <PlusIcon className="h-4 w-4" aria-hidden="true" />
            {t('card-must-match-add')}
          </button>
        </Section>
      </div>

      <div className="sticky bottom-0 -mx-6 -mb-6 mt-8 rounded-b-box border-t border-base-300 bg-base-100 px-6 py-4">
        {showProblems && problems.length > 0 && (
          <ul className="mb-3 space-y-1 text-sm text-error" role="alert">
            {problems.map((problem) => (
              <li key={problem}>{t(`card-problem-${problem}`)}</li>
            ))}
          </ul>
        )}
        {confirmProblem && (
          <p className="mb-3 text-sm text-error" role="alert">
            {confirmProblem}
          </p>
        )}
        <div className="flex flex-wrap items-center justify-end gap-2">
          {variantPending && (
            <p
              id="card-confirm-variant-hint"
              className="mr-auto text-sm text-base-content/70"
            >
              {t('card-confirm-needs-variant')}
            </p>
          )}
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={onStartOver}
            disabled={confirming}
          >
            {t('card-start-over')}
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={handleConfirm}
            disabled={confirming || variantPending}
            aria-describedby={
              variantPending ? 'card-confirm-variant-hint' : undefined
            }
          >
            {confirming && (
              <span
                className="loading loading-spinner loading-xs"
                aria-hidden="true"
              />
            )}
            {confirming ? t('card-confirming') : t('card-confirm')}
          </button>
        </div>
      </div>
    </section>
  );
}
