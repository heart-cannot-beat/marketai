import { useState, type FormEvent } from 'react';
import Link from 'next/link';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import {
  ChevronDownIcon,
  PencilSquareIcon,
  TrashIcon,
} from '@heroicons/react/24/outline';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import {
  buildDirectionEdit,
  draftFromDirection,
  type DirectionDraft,
  type DirectionEdit,
  type DirectionView,
  type FactorView,
  type PlanRunItem,
} from '@/lib/test-plans/plan-model';
import { AssetThumb } from './AssetThumb';

const GENERATED_STATES = new Set(['succeeded', 'reused', 'qc_pending']);
const FAILED_STATES = new Set(['failed', 'cancelled', 'skipped']);

export interface DirectionImage {
  item: PlanRunItem;
  runId: string;
}

interface DirectionCardProps {
  /** In-page anchor of the card (ordinal, never an internal id). */
  anchorId?: string;
  /** Changes whenever the page asks this card to open its editor
   * (the totals bar's "编辑第一个方向", Issue #193). */
  editRequest?: number;
  direction: DirectionView;
  /** The plan's factor catalog (labels and every value id). */
  factors: FactorView[];
  /** Images this direction contributes to the sample / the expansion. */
  sampleImages: number;
  expansionImages: number | null;
  expansionLabel: string;
  images: DirectionImage[];
  imagesHeading: string | null;
  editable: boolean;
  busy: boolean;
  onRemove: () => void;
  onSaveEdit: (edit: DirectionEdit) => Promise<boolean>;
}

function ImageTile({
  image,
  index,
  slug,
}: {
  image: DirectionImage;
  index: number;
  slug: string;
}) {
  const { t } = useTranslation('tests');
  const { item, runId } = image;
  const versionId = item.bound_asset_version_id;
  if (versionId && GENERATED_STATES.has(item.state)) {
    return (
      <Link
        href={`/w/${slug}/assets?run=${encodeURIComponent(runId)}&overlay=${encodeURIComponent(versionId)}`}
        className="block overflow-hidden rounded-md ring-primary focus-visible:ring-2"
        aria-label={t('direction-image-open', { index: index + 1 })}
        data-testid="direction-image"
      >
        <AssetThumb
          versionId={versionId}
          thumbnail={item.thumbnail}
          alt=""
          className="aspect-square w-full"
        />
      </Link>
    );
  }
  const failed = FAILED_STATES.has(item.state);
  return (
    <div
      className="flex aspect-square w-full items-center justify-center rounded-md bg-base-200 p-1 text-center text-xs text-base-content/60"
      data-testid="direction-image-placeholder"
    >
      {failed ? (
        t('direction-image-failed')
      ) : (
        <span className="flex flex-col items-center gap-1">
          <span
            className="loading loading-spinner loading-xs"
            aria-hidden="true"
          />
          {t('direction-image-pending')}
        </span>
      )}
    </div>
  );
}

/**
 * One AI-drafted (or user-added) direction (contract §3.2): title, what it
 * tests (the card's lead), scene, copy, folded factor values and how many
 * images it adds -- plus, once a Run exists, its own images. Edits are
 * sent as only the changed fields; the page re-plans and refreshes counts.
 */
export function DirectionCard({
  anchorId,
  editRequest,
  direction,
  factors,
  sampleImages,
  expansionImages,
  expansionLabel,
  images,
  imagesHeading,
  editable,
  busy,
  onRemove,
  onSaveEdit,
}: DirectionCardProps) {
  const { t } = useTranslation('tests');
  const { current } = useWorkspace();
  const [draft, setDraft] = useState<DirectionDraft | null>(null);
  const [factorError, setFactorError] = useState(false);
  // Open the editor when the page asks (adjusting state while rendering,
  // so no effect re-runs on unrelated prop changes).
  const [seenEditRequest, setSeenEditRequest] = useState(editRequest);
  if (editRequest !== seenEditRequest) {
    setSeenEditRequest(editRequest);
    if (editRequest !== undefined && editable && !draft) {
      setFactorError(false);
      setDraft(draftFromDirection(direction));
    }
  }
  const folded = direction.factors ?? [];
  const title = direction.title || '';

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!draft) return;
    if (Object.values(draft.factorValues).some((ids) => ids.length === 0)) {
      setFactorError(true);
      return;
    }
    const edit = buildDirectionEdit(direction, draft);
    if (!edit) {
      setDraft(null);
      return;
    }
    if (await onSaveEdit(edit)) setDraft(null);
  }

  function toggleValue(factorKey: string, valueId: string) {
    setFactorError(false);
    setDraft((prev) => {
      if (!prev) return prev;
      const currentIds = prev.factorValues[factorKey] ?? [];
      const next = currentIds.includes(valueId)
        ? currentIds.filter((id) => id !== valueId)
        : [...currentIds, valueId];
      return {
        ...prev,
        factorValues: { ...prev.factorValues, [factorKey]: next },
      };
    });
  }

  if (draft) {
    const field = (
      key: keyof Omit<DirectionDraft, 'factorValues'>,
      label: string,
      multiline = false
    ) => (
      <label className="form-control">
        <span className="label-text mb-1 text-sm font-medium">{label}</span>
        {multiline ? (
          <textarea
            className="textarea textarea-bordered text-sm"
            rows={3}
            value={draft[key]}
            onChange={(event) =>
              setDraft({ ...draft, [key]: event.target.value })
            }
          />
        ) : (
          <input
            className="input input-bordered input-sm"
            value={draft[key]}
            onChange={(event) =>
              setDraft({ ...draft, [key]: event.target.value })
            }
          />
        )}
      </label>
    );
    return (
      <li
        id={anchorId}
        className="rounded-box border-2 border-primary/50 bg-base-100 p-5"
        data-testid="direction-card"
        data-editing="true"
      >
        <form className="space-y-3" onSubmit={submit}>
          {field('title', t('direction-field-title'))}
          {field('whatItTests', t('direction-field-tests'), true)}
          {field('sceneDescription', t('direction-field-scene'), true)}
          <div className="grid gap-3 sm:grid-cols-2">
            {field('headline', t('direction-field-headline'))}
            {field('primaryText', t('direction-field-primary'), true)}
          </div>
          {factors.map((factor) => {
            const chosen = draft.factorValues[factor.key];
            if (!chosen) return null;
            return (
              <div key={factor.key} role="group" aria-label={factor.label}>
                <p className="mb-1 text-sm font-medium">{factor.label}</p>
                <div className="flex flex-wrap gap-2">
                  {(factor.values ?? []).map((value) => {
                    const on = chosen.includes(value.id);
                    return (
                      <button
                        key={value.id}
                        type="button"
                        className={classNames(
                          'btn btn-xs',
                          on ? 'btn-primary' : 'btn-outline'
                        )}
                        aria-pressed={on}
                        onClick={() => toggleValue(factor.key, value.id)}
                      >
                        {value.label}
                      </button>
                    );
                  })}
                </div>
              </div>
            );
          })}
          {factorError && (
            <p className="text-sm text-error" role="alert">
              {t('direction-factor-min')}
            </p>
          )}
          <div className="flex justify-end gap-2 pt-1">
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => setDraft(null)}
              disabled={busy}
            >
              {t('direction-cancel')}
            </button>
            <button
              type="submit"
              className="btn btn-primary btn-sm"
              disabled={busy || !draft.title.trim()}
            >
              {busy ? t('direction-saving') : t('direction-save')}
            </button>
          </div>
        </form>
      </li>
    );
  }

  return (
    <li
      id={anchorId}
      className="flex flex-col rounded-box border border-base-300 bg-base-100"
      data-testid="direction-card"
    >
      <div className="space-y-4 p-5">
        <div className="flex items-start justify-between gap-3">
          <h3 className="text-lg font-semibold leading-snug">
            {title}
            {direction.user_added && (
              <span className="badge badge-outline badge-sm ml-2 align-middle font-normal">
                {t('direction-user-added')}
              </span>
            )}
          </h3>
          {editable && (
            <div className="flex shrink-0 gap-1">
              <button
                type="button"
                className="btn btn-ghost btn-sm btn-square"
                aria-label={t('direction-edit-label', { title })}
                title={t('direction-edit')}
                disabled={busy}
                onClick={() => setDraft(draftFromDirection(direction))}
              >
                <PencilSquareIcon className="h-4 w-4" aria-hidden="true" />
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-sm btn-square"
                aria-label={t('direction-remove-label', { title })}
                title={t('direction-remove')}
                disabled={busy}
                onClick={onRemove}
              >
                <TrashIcon className="h-4 w-4" aria-hidden="true" />
              </button>
            </div>
          )}
        </div>

        <div className="border-l-4 border-primary pl-4">
          <p className="text-xs text-base-content/60">{t('direction-tests')}</p>
          <p className="mt-0.5 text-base font-medium leading-relaxed text-base-content">
            {direction.what_it_tests || t('direction-empty-copy')}
          </p>
        </div>

        {imagesHeading && images.length > 0 && (
          <div>
            <p className="mb-2 text-sm text-base-content/70">{imagesHeading}</p>
            <ul
              className="grid grid-cols-3 gap-2 sm:grid-cols-4"
              data-testid="direction-images"
            >
              {images.map((image, index) => (
                <li key={image.item.id}>
                  <ImageTile image={image} index={index} slug={current.slug} />
                </li>
              ))}
            </ul>
          </div>
        )}

        <div className="grid gap-4 text-sm sm:grid-cols-2">
          <div>
            <p className="mb-1 text-xs text-base-content/60">
              {t('direction-scene')}
            </p>
            <p className="leading-relaxed">
              {direction.scene_description || t('direction-empty-copy')}
            </p>
          </div>
          <div>
            <p className="mb-1 text-xs text-base-content/60">
              {t('direction-copy')}
            </p>
            <p className="font-semibold">
              {direction.copy?.headline || t('direction-empty-copy')}
            </p>
            {direction.copy?.primary_text && (
              <p className="mt-1 leading-relaxed text-base-content/80">
                {direction.copy.primary_text}
              </p>
            )}
          </div>
        </div>

        {folded.length > 0 && (
          <details className="group">
            <summary className="flex cursor-pointer list-none items-center gap-1 text-sm text-base-content/70 hover:text-base-content">
              <ChevronDownIcon
                className="h-4 w-4 transition-transform group-open:rotate-180"
                aria-hidden="true"
              />
              {t('direction-factors', { count: folded.length })}
            </summary>
            <dl className="mt-2 grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[auto_1fr]">
              {folded.map((factor) => (
                <div key={factor.key} className="contents">
                  <dt className="text-base-content/60">{factor.label}</dt>
                  <dd className="flex flex-wrap gap-1">
                    {(factor.values ?? []).map((value) => (
                      <span key={value.id} className="badge badge-ghost">
                        {value.label}
                      </span>
                    ))}
                  </dd>
                </div>
              ))}
            </dl>
          </details>
        )}
      </div>

      <div className="mt-auto border-t border-base-200 px-5 py-2 text-xs text-base-content/60">
        {expansionImages === null
          ? t('direction-images-sample-only', { sample: sampleImages })
          : t('direction-images', {
              sample: sampleImages,
              scale: expansionLabel,
              images: expansionImages,
            })}
      </div>
    </li>
  );
}
