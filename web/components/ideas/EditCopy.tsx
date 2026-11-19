import { useEffect, useId, useState, type FormEvent } from 'react';
import { useTranslation } from 'next-i18next';
import { GuardedLink } from '@/components/workspace/GuardedLink';
import type {
  CommandOutcome,
  IdeasCommands,
} from '@/hooks/ideas/useIdeasCommands';
import type { UseImageSettings } from '@/hooks/ideas/useImageSettings';
import { usePaidImage } from '@/hooks/ideas/usePaidImage';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import { errorMessage } from '@/lib/i18n/domain-errors';
import {
  COPY_LIMIT,
  copyChanged,
  copyImages,
  copyOf,
  copyReady,
  trimmedCopy,
  type EditCopyBody,
} from '@/lib/ideas/copy-edit';
import {
  placeOf,
  type IdeasAd,
  type IdeasDetail,
} from '@/lib/ideas/ideas-view';
import { PaidImageAction } from './PaidImageAction';

const INPUT =
  'w-full rounded-lg border border-gray-300 bg-white p-3 text-base dark:border-gray-600 dark:bg-gray-900';

interface Failure {
  message: string;
  /** Someone changed the ideas first: refreshing comes before trying again. */
  stale: boolean;
  /** The words need a source: the product profile is where one is added. */
  source: boolean;
}

/**
 * The copy of "Edit this ad" (design §5.3, #228): the headline and body
 * text, changed in the user's own words. Saving is free and changes only the
 * copy of this ad's place (a claim told one way); the images made there with
 * the old copy need making again, so the screen says how many first and,
 * once saved, offers one image with the new copy at the preview's maximum.
 *
 * An uploaded image keeps its own words, and an image of earlier content has
 * no copy here any more: both show the copy without a form.
 */
export function EditCopy({
  detail,
  ad,
  commands,
  image,
  profileHref,
  uploaded,
  onSaved,
}: {
  detail: IdeasDetail;
  ad: IdeasAd;
  commands: IdeasCommands;
  image: UseImageSettings;
  /** The product profile, where a source for the words is added. */
  profileHref?: string;
  uploaded: boolean;
  /** The copy was saved from this screen. */
  onSaved?: () => void;
}) {
  const { t } = useTranslation('ideas');
  // The place this screen changes: this ad's, while the ad is that place's
  // ad when the screen opens. Saving makes this image one of earlier copy,
  // and the screen goes on with the place's new copy.
  const [place] = useState(() =>
    placeOf(detail, ad).headline !== null ? ad.combination_id : null
  );
  const current = place && !uploaded ? copyOf(detail, place) : null;
  if (!place || !current) {
    const shown = placeOf(detail, ad);
    return (
      <>
        <dl className="space-y-2 text-sm">
          <div>
            <dt className="font-medium text-gray-600 dark:text-gray-400">
              {t('edit-copy-headline')}
            </dt>
            <dd lang={detail.ad_language} className="text-base">
              {shown.headline ?? t('edit-copy-gone')}
            </dd>
          </div>
          <div>
            <dt className="font-medium text-gray-600 dark:text-gray-400">
              {t('edit-copy-body')}
            </dt>
            <dd lang={detail.ad_language}>
              {shown.bodyCopy ?? t('edit-copy-gone')}
            </dd>
          </div>
        </dl>
        {uploaded && (
          <p className="text-sm text-gray-600 dark:text-gray-400">
            {t('edit-copy-uploaded')}
          </p>
        )}
      </>
    );
  }
  return (
    <CopyForm
      detail={detail}
      combinationId={place}
      current={current}
      commands={commands}
      image={image}
      profileHref={profileHref}
      onSaved={onSaved}
    />
  );
}

/** The sentence for a copy change that did not go through. */
function useFailureOf() {
  const { t } = useTranslation('ideas');
  const { t: common } = useTranslation('common');
  return (outcome: CommandOutcome<unknown>): Failure | null => {
    if (outcome.kind === 'done') return null;
    const plain = { stale: false, source: false };
    if (outcome.kind === 'uncertain')
      return { message: t('copy-problem-uncertain'), ...plain };
    const problem: ProblemDetails = outcome.problem;
    const params = (problem as { params?: Record<string, unknown> }).params;
    switch (problem.code) {
      case 'stale_marketing_revision':
        return { message: t('copy-problem-stale'), stale: true, source: false };
      case 'combination_not_found':
        return { message: t('copy-problem-gone'), stale: true, source: false };
      case 'copy_unsupported': {
        const claims = params?.claims as string[] | undefined;
        return {
          message: claims?.length
            ? t('copy-problem-unsupported', {
                claims: claims.join(t('list-separator')),
              })
            : t('copy-problem-unsupported-none'),
          stale: false,
          source: true,
        };
      }
      case 'copy_language_mismatch':
        return { message: t('copy-problem-language'), ...plain };
      case 'ad_copy_too_long':
        return {
          message: t('copy-problem-too-long', {
            count: Number(params?.over ?? 0),
          }),
          ...plain,
        };
      case 'validation_error':
        return {
          message: t('copy-problem-length', { limit: COPY_LIMIT }),
          ...plain,
        };
      case 'first_batch_running':
        return { message: t('copy-problem-first-batch'), ...plain };
      default:
        return { message: errorMessage(common, problem), ...plain };
    }
  };
}

function CopyForm({
  detail,
  combinationId,
  current,
  commands,
  image,
  profileHref,
  onSaved,
}: {
  detail: IdeasDetail;
  combinationId: string;
  current: EditCopyBody;
  commands: IdeasCommands;
  image: UseImageSettings;
  profileHref?: string;
  onSaved?: () => void;
}) {
  const { t } = useTranslation('ideas');
  const ids = useId();
  const failureOf = useFailureOf();
  const [form, setForm] = useState<EditCopyBody>(current);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);
  // The revision the save produced: one image with the new copy is offered
  // from it, quoted as soon as the screen holds that revision.
  const [saved, setSaved] = useState<number | null>(null);
  const [quoteAt, setQuoteAt] = useState<number | null>(null);
  const make = usePaidImage(commands);
  const { quote, cancel } = make;
  const settings = image.settings;
  const changed = copyChanged(current, form);
  const ready = copyReady(form);
  const images = copyImages(detail, combinationId);
  const revision = detail.revision;

  useEffect(() => {
    if (quoteAt === null || revision < quoteAt || !settings) return;
    setQuoteAt(null);
    void quote(() =>
      commands.draftFor(
        combinationId,
        image.candidates(revision),
        image.prepared
      )
    );
  }, [quoteAt, revision, settings, quote, commands, combinationId, image]);

  function edit(next: EditCopyBody) {
    setForm(next);
    setFailure(null);
    // Other words than the saved ones: the image offered was for those.
    if (saved !== null) {
      setSaved(null);
      setQuoteAt(null);
      cancel();
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!changed || !ready || busy) return;
    const body = trimmedCopy(form);
    setBusy(true);
    setFailure(null);
    const outcome = await commands.editCopy(combinationId, body);
    setBusy(false);
    if (outcome.kind === 'done') {
      setForm(body);
      setSaved(outcome.data.revision);
      setQuoteAt(outcome.data.revision);
      onSaved?.();
    } else setFailure(failureOf(outcome));
  }

  return (
    <form
      className="space-y-3"
      onSubmit={submit}
      noValidate
      data-testid="copy-form"
    >
      <label className="block text-sm font-semibold" htmlFor={`${ids}-h`}>
        {t('edit-copy-headline')}
      </label>
      <input
        id={`${ids}-h`}
        className={INPUT}
        lang={detail.ad_language}
        maxLength={COPY_LIMIT}
        value={form.headline}
        onChange={(event) => edit({ ...form, headline: event.target.value })}
      />
      <label className="block text-sm font-semibold" htmlFor={`${ids}-b`}>
        {t('edit-copy-body')}
      </label>
      <textarea
        id={`${ids}-b`}
        className={INPUT}
        rows={3}
        lang={detail.ad_language}
        maxLength={COPY_LIMIT}
        value={form.body_copy}
        onChange={(event) => edit({ ...form, body_copy: event.target.value })}
      />

      <div
        role="group"
        aria-labelledby={`${ids}-p`}
        className="space-y-1 rounded-lg border border-dashed border-gray-300 bg-gray-50 p-3 dark:border-gray-600 dark:bg-gray-800"
        data-testid="copy-preview"
      >
        <p
          id={`${ids}-p`}
          className="text-xs font-semibold text-gray-600 dark:text-gray-400"
        >
          {t('edit-copy-preview')}
        </p>
        <p
          lang={detail.ad_language}
          className="break-words text-lg font-bold leading-snug"
        >
          {form.headline.trim() || t('edit-copy-preview-empty')}
        </p>
        <p lang={detail.ad_language} className="break-words text-sm">
          {form.body_copy.trim() || t('edit-copy-preview-empty')}
        </p>
        <p className="text-xs text-gray-600 dark:text-gray-400">
          {t('edit-copy-preview-note')}
        </p>
      </div>

      {changed && ready && images.ads.length > 0 && (
        <p
          role="note"
          className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200"
        >
          {t('edit-copy-redo', { count: images.ads.length })}
          {images.kept > 0 && ` ${t('edit-copy-kept', { count: images.kept })}`}
        </p>
      )}
      {changed && !ready && (
        <p className="text-sm text-red-700 dark:text-red-300" role="note">
          {t('copy-problem-length', { limit: COPY_LIMIT })}
        </p>
      )}
      <p className="text-xs text-gray-600 dark:text-gray-400">
        {t('edit-copy-free')}
      </p>

      {failure && (
        <div
          role="alert"
          className="space-y-2 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-900 dark:border-red-800 dark:bg-red-950 dark:text-red-200"
        >
          <p>{failure.message}</p>
          {failure.stale && (
            <button
              type="button"
              className="btn btn-sm min-h-11"
              onClick={() => void commands.refresh()}
            >
              {t('claim-refresh')}
            </button>
          )}
          {failure.source && profileHref && (
            <GuardedLink
              href={profileHref}
              className="link font-medium underline-offset-2"
            >
              {t('copy-problem-source-link')}
            </GuardedLink>
          )}
        </div>
      )}

      {saved === null ? (
        <div className="flex flex-wrap gap-2">
          <button
            type="submit"
            className="btn btn-primary btn-sm min-h-11"
            disabled={!changed || !ready || busy}
          >
            {busy ? t('claim-saving') : t('edit-copy-save')}
          </button>
          {changed && (
            <button
              type="button"
              className="btn btn-ghost btn-sm min-h-11"
              disabled={busy}
              onClick={() => edit(current)}
            >
              {t('edit-copy-reset')}
            </button>
          )}
        </div>
      ) : (
        <div className="space-y-2 border-t border-gray-200 pt-3 dark:border-gray-700">
          <p className="text-sm" role="status">
            {t('edit-copy-saved')}
          </p>
          <PaidImageAction
            paid={make}
            primary
            startLabel={t('edit-copy-make-start')}
            confirmLabel={(cost) => t('edit-copy-make-confirm', { cost })}
            disabled={!settings}
            onStart={() =>
              settings &&
              void quote(() =>
                commands.draftFor(
                  combinationId,
                  image.candidates(revision),
                  image.prepared
                )
              )
            }
          />
          {!settings && (
            <p className="text-sm" role="note">
              {t('settings-unavailable')}
            </p>
          )}
        </div>
      )}
    </form>
  );
}
