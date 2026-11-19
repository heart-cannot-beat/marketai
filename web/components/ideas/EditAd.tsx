import { useId, useMemo, useState } from 'react';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { MarketingImageEdit } from '@/components/workspace/MarketingImageEdit';
import { MarketingImageTools } from '@/components/workspace/MarketingImageTools';
import {
  MarketingVersionHistory,
  type HistoryAsset,
} from '@/components/workspace/MarketingVersionHistory';
import { ideasProblem } from '@/lib/ideas/ideas-problems';
import { mainReasons } from '@/lib/ideas/check-sheet';
import { removedFactsOf } from '@/lib/ideas/claim-edit';
import { renewRestores } from '@/lib/ideas/photos';
import { GuardedLink } from '@/components/workspace/GuardedLink';
import {
  checkView,
  fromUpload,
  openFindings,
  placeOf,
  versionsOf,
  type IdeasAd,
  type IdeasDetail,
} from '@/lib/ideas/ideas-view';
import type { IdeasCommands } from '@/hooks/ideas/useIdeasCommands';
import type { UseImageSettings } from '@/hooks/ideas/useImageSettings';
import { usePaidImage } from '@/hooks/ideas/usePaidImage';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import { AdImage, AdThumbnail } from './AdCard';
import { ChecksButton } from './ChecksButton';
import { FindingList } from './FindingList';
import { EditCopy } from './EditCopy';
import { BUTTON, PaidImageAction, PRESSED } from './PaidImageAction';

const SECTION =
  'space-y-4 rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900 sm:p-5';
const STATUS_STYLE: Record<IdeasAd['status'], string> = {
  draft: 'bg-amber-100 text-amber-900',
  needs_redo: 'bg-red-100 text-red-900',
  exportable: 'bg-emerald-100 text-emerald-900',
};

/**
 * Picture changes the user can ask for with one tap (design §5.3). The note
 * goes to the image model as written, so it is in English, which the server
 * accepts for every ad language (#200).
 */
const QUICK_NOTES = [
  ['bigger-product', 'Make the product larger in the frame.'],
  ['daytime', 'Change the scene to daytime.'],
  ['other-scene', 'Use a different setting for the scene.'],
  ['bigger-text', 'Make the text larger and easier to read.'],
] as const;

export interface EditAdProps {
  /** Where a product photo whose check ran out is renewed (#233). */
  renewHref?: string;
  /** The product profile, where a source for the copy's words is added (#228). */
  profileHref?: string;
  projectId: string;
  detail: IdeasDetail;
  assetVersionId: string;
  commands: IdeasCommands;
  image: UseImageSettings;
  onClose: () => void;
  /** Shows another version of this ad on this screen. */
  onSwitch: (assetVersionId: string) => void;
  onToggleKept: (ad: IdeasAd) => void;
  /** Opens "another way of telling" for this ad's claim. */
  onAnotherTelling?: (angleId: string) => void;
  /** Opens "change this claim" for this ad's claim on the ideas page (#224). */
  onEditClaim?: (angleId: string) => void;
}

/**
 * "Edit this ad" (ADR 0018, design §5.3, #200, #201): what the ad stands on,
 * its check's findings one by one, and the ways to change it -- redo it from
 * a note and the findings, make it once more as it is, crop and rotate it
 * (free), or upscale, remove the background or erase an area (paid). Every
 * change is a new version: the old one stays an ad, to compare or keep.
 */
export function EditAd({
  projectId,
  detail,
  assetVersionId,
  commands,
  image,
  onClose,
  onSwitch,
  onToggleKept,
  onAnotherTelling,
  onEditClaim,
  renewHref,
  profileHref,
}: EditAdProps) {
  const { t } = useTranslation('ideas');
  const ad = detail.ads.find(
    (item) => item.asset_version_id === assetVersionId
  );
  const versions = useMemo(
    () => versionsOf(detail, assetVersionId),
    [detail, assetVersionId]
  );
  // The image whose copy was just changed here: the copy offers one image
  // with the new copy, so "make it again" below would offer the same (#228).
  const [copySavedOn, setCopySavedOn] = useState<string | null>(null);
  if (!ad)
    return (
      <div className="mx-auto max-w-5xl space-y-4" data-testid="edit-ad">
        <BackButton onClose={onClose} />
        <p className="text-sm" role="alert">
          {t('edit-gone')}
        </p>
      </div>
    );
  const place = placeOf(detail, ad);
  const facts = (detail.understanding?.facts ?? []).filter((fact) =>
    (ad.cited_fact_ids ?? []).includes(fact.id)
  );
  const settings = image.settings;
  const reasons = new Set((ad.reasons ?? []).map((reason) => reason.code));
  // A crop of an uploaded image is still the user's own image (#200).
  const uploaded = fromUpload(detail, ad);
  const needsRedo = ad.status === 'needs_redo';
  const findings = openFindings(ad);
  const check = checkView(ad, detail);
  const label = place.headline ?? t('ideas-ad-image');

  return (
    <div className="mx-auto max-w-5xl space-y-5" data-testid="edit-ad">
      <BackButton onClose={onClose} />
      <div className="space-y-1">
        <h1 className="text-2xl font-bold sm:text-3xl">{t('edit-title')}</h1>
        <p className="text-base text-gray-600 dark:text-gray-400">
          {[
            place.claimNumber !== null && place.proposition
              ? t('edit-claim', {
                  number: place.claimNumber,
                  claim: place.proposition,
                })
              : null,
            place.telling
              ? t('ideas-ad-telling', { name: place.telling.name })
              : t('ideas-ad-telling-unknown'),
            facts.length > 0
              ? t('edit-facts', {
                  facts: facts
                    .map((fact) => fact.statement)
                    .join(t('list-separator')),
                })
              : null,
          ]
            .filter(Boolean)
            .join(' · ')}
        </p>
      </div>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
        <div className="min-w-0 space-y-4">
          <div className="space-y-2">
            <AdImage versionId={ad.asset_version_id} alt={label} />
            <p className="text-sm font-semibold">
              <span
                className={classNames(
                  'mr-2 inline-block rounded-full px-2 py-px text-xs font-semibold',
                  STATUS_STYLE[ad.status]
                )}
              >
                {t(`ideas-ad-status-${ad.status}`)}
              </span>
              {t(`ideas-ad-origin-${ad.origin ?? 'generated'}`)}
            </p>
            <button
              type="button"
              aria-pressed={ad.kept}
              className={classNames(ad.kept ? PRESSED : BUTTON, 'w-full')}
              onClick={() => onToggleKept(ad)}
            >
              {ad.kept ? t('ideas-ad-kept') : t('ideas-ad-keep')}
            </button>
          </div>

          <ImageWork
            ad={ad}
            detail={detail}
            commands={commands}
            blocked={needsRedo}
            uploaded={uploaded}
          />

          <Versions current={ad} versions={versions} onSwitch={onSwitch} />
        </div>

        <div className="min-w-0 space-y-4">
          <section className={SECTION} aria-labelledby="edit-copy">
            <h2 id="edit-copy" className="text-base font-semibold">
              {t('edit-copy-title')}
            </h2>
            <EditCopy
              // Another version is another image: its own form (#228).
              key={ad.asset_version_id}
              detail={detail}
              ad={ad}
              commands={commands}
              image={image}
              profileHref={profileHref}
              uploaded={uploaded}
              onSaved={() => setCopySavedOn(ad.asset_version_id)}
            />
          </section>

          <section className={SECTION} aria-labelledby="edit-check">
            <h2 id="edit-check" className="text-base font-semibold">
              {t('edit-check-title')}
            </h2>
            {check === 'checking' && (
              <p className="text-sm" role="status">
                {t('ideas-ad-checking')}
              </p>
            )}
            {check === 'unchecked' && (
              <div className="space-y-2">
                <p className="text-sm">{t('edit-check-none')}</p>
                {!needsRedo && (
                  <ChecksButton
                    projectId={projectId}
                    detail={detail}
                    commands={commands}
                  />
                )}
              </div>
            )}
            {check === 'clear' && (
              <p className="text-sm">{t('edit-check-clear')}</p>
            )}
            {findings.length > 0 && (
              <>
                <p className="text-sm">
                  {t('edit-check-findings', { count: findings.length })}
                </p>
                <FindingList
                  findings={findings}
                  language={detail.ad_language}
                />
              </>
            )}
            {needsRedo && (
              <div className="space-y-1 text-sm text-red-900 dark:text-red-200">
                <p className="font-semibold">{t('edit-needs-redo')}</p>
                <ul className="list-disc pl-5">
                  {mainReasons(ad).map((code) => (
                    <li key={code}>{t(`ideas-reason-${code}`)}</li>
                  ))}
                </ul>
              </div>
            )}
          </section>

          <Redo
            ad={ad}
            commands={commands}
            image={image}
            revision={detail.revision}
            uploaded={uploaded}
            // What blocks a redo is the claim as it is now still naming a
            // deleted fact (#224): once the claim is changed it is gone.
            removedFact={removedFactsOf(detail, ad.angle_id).length > 0}
            onEditClaim={
              onEditClaim &&
              detail.ideas?.angles.some((a) => a.id === ad.angle_id)
                ? () => onEditClaim(ad.angle_id)
                : undefined
            }
            ideasChanged={reasons.has('ideas_changed')}
            again={copySavedOn !== ad.asset_version_id}
            photoGone={reasons.has('photo_unavailable')}
            renewHref={
              renewHref && renewRestores(detail, ad) ? renewHref : undefined
            }
            hasFindings={findings.length > 0}
          />

          {onAnotherTelling &&
            detail.ideas?.angles.some((a) => a.id === ad.angle_id) && (
              <section className={SECTION} aria-labelledby="edit-telling">
                <h2 id="edit-telling" className="text-base font-semibold">
                  {t('edit-telling-title')}
                </h2>
                <p className="text-sm text-gray-600 dark:text-gray-400">
                  {t('edit-telling-note')}
                </p>
                <button
                  type="button"
                  className={BUTTON}
                  onClick={() => onAnotherTelling(ad.angle_id)}
                >
                  {t('edit-telling-open')}
                </button>
              </section>
            )}

          {settings && (
            <details className={SECTION}>
              <summary className="flex min-h-11 cursor-pointer items-center text-sm font-semibold">
                {t('edit-technical')}
              </summary>
              <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-sm">
                <dt className="text-gray-600 dark:text-gray-400">
                  {t('settings-provider')}
                </dt>
                <dd>{settings.provider}</dd>
                <dt className="text-gray-600 dark:text-gray-400">
                  {t('settings-model')}
                </dt>
                <dd>{settings.model}</dd>
                <dt className="text-gray-600 dark:text-gray-400">
                  {t('settings-size')}
                </dt>
                <dd>{t(`settings-size-${settings.image_size}`)}</dd>
              </dl>
              <p className="text-xs text-gray-600 dark:text-gray-400">
                {t('edit-technical-note')}
              </p>
            </details>
          )}
        </div>
      </div>
    </div>
  );
}

function BackButton({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation('ideas');
  return (
    <button
      type="button"
      className="inline-flex min-h-11 items-center gap-1 text-sm font-medium underline"
      onClick={onClose}
    >
      {t('back-to-ideas')}
    </button>
  );
}

/**
 * Redo this ad from a note and its check's findings (a correction, #200), or
 * make it once more as it is. Both are paid; the maximum is the preview's.
 */
function Redo({
  ad,
  commands,
  image,
  revision,
  uploaded,
  removedFact,
  onEditClaim,
  ideasChanged,
  again: offerAgain,
  photoGone,
  renewHref,
  hasFindings,
}: {
  ad: IdeasAd;
  commands: IdeasCommands;
  image: UseImageSettings;
  revision: number;
  uploaded: boolean;
  removedFact: boolean;
  onEditClaim?: () => void;
  ideasChanged: boolean;
  /** Offer "make it again"; not where the copy just offered the same image (#228). */
  again: boolean;
  /** A product photo it used was rejected or may no longer be used (#229). */
  photoGone: boolean;
  /** Where to renew it, when its check only ran out (#233). */
  renewHref?: string;
  hasFindings: boolean;
}) {
  const { t } = useTranslation('ideas');
  const noteId = useId();
  const settings = image.settings;
  const [note, setNote] = useState('');
  const correction = usePaidImage(commands);
  const again = usePaidImage(commands);
  const canCorrect = !uploaded && !removedFact && !ideasChanged;
  const quick = (text: string) =>
    setNote((current) =>
      current.includes(text) ? current : `${current} ${text}`.trim()
    );

  return (
    <section className={SECTION} aria-labelledby="edit-redo">
      <h2 id="edit-redo" className="text-base font-semibold">
        {t('edit-redo-title')}
      </h2>
      {uploaded && <p className="text-sm">{t('edit-uploaded-note')}</p>}
      {removedFact && (
        <div className="space-y-2">
          <p className="text-sm text-red-900 dark:text-red-200" role="note">
            {t('ideas-refusal-claim_cites_removed_fact')}
          </p>
          {onEditClaim && (
            <button type="button" className={BUTTON} onClick={onEditClaim}>
              {t('edit-change-claim')}
            </button>
          )}
        </div>
      )}
      {ideasChanged && !removedFact && (
        <p className="text-sm" role="note">
          {t('edit-ideas-changed')}
        </p>
      )}
      {photoGone && !uploaded && !removedFact && renewHref && (
        <div className="space-y-1 text-sm" role="note">
          <p>{t('edit-photo-expired')}</p>
          <GuardedLink
            href={renewHref}
            className="link font-medium underline-offset-2"
          >
            {t('ideas-photo-renew-link')}
          </GuardedLink>
        </div>
      )}
      {photoGone && !uploaded && !removedFact && !renewHref && (
        <p className="text-sm" role="note">
          {t('edit-photo-gone')}
        </p>
      )}
      {canCorrect && (
        <>
          <div className="flex flex-wrap gap-2">
            {QUICK_NOTES.map(([key, text]) => {
              const on = note.includes(text);
              return (
                <button
                  key={key}
                  type="button"
                  aria-pressed={on}
                  className={classNames(
                    'min-h-11 rounded-full border px-3 text-sm',
                    on
                      ? 'border-gray-900 bg-gray-900 text-white dark:border-gray-100 dark:bg-gray-100 dark:text-gray-900'
                      : 'border-gray-300 bg-white dark:border-gray-600 dark:bg-gray-900'
                  )}
                  onClick={() =>
                    on
                      ? setNote((current) =>
                          current.replace(text, '').replace(/\s+/g, ' ').trim()
                        )
                      : quick(text)
                  }
                >
                  {t(`edit-quick-${key}`)}
                </button>
              );
            })}
          </div>
          <label htmlFor={noteId} className="block text-sm font-medium">
            {t('edit-note-label')}
          </label>
          <textarea
            id={noteId}
            className="min-h-24 w-full rounded-lg border border-gray-300 bg-white p-3 text-base dark:border-gray-600 dark:bg-gray-900"
            maxLength={1200}
            value={note}
            placeholder={t('edit-note-placeholder')}
            onChange={(e) => {
              setNote(e.target.value);
              if (correction.phase.kind !== 'idle') correction.cancel();
            }}
          />
          <p className="text-xs text-gray-600 dark:text-gray-400">
            {hasFindings ? t('edit-note-findings') : t('edit-note-hint')}
          </p>
          <PaidImageAction
            paid={correction}
            primary
            startLabel={t('redo-start')}
            confirmLabel={(cost) => t('redo-confirm', { cost })}
            disabled={!settings || (!note.trim() && !hasFindings)}
            onStart={() =>
              settings &&
              void correction.quote(async () => {
                const prepared = await commands.prepareFeedback({
                  asset_version_id: ad.asset_version_id,
                  note: note.trim(),
                  provider: settings.provider,
                  model: settings.model,
                });
                return prepared.kind === 'done'
                  ? { kind: 'done', data: prepared.data.draft_id }
                  : prepared;
              })
            }
          />
        </>
      )}
      {/* An image made while the claim names a deleted fact would need redoing too. */}
      {!uploaded && !removedFact && offerAgain && (
        <div className="space-y-1 border-t border-gray-200 pt-3 dark:border-gray-700">
          <p className="text-sm text-gray-600 dark:text-gray-400">
            {t('edit-again-note')}
          </p>
          <PaidImageAction
            paid={again}
            startLabel={t('again-start')}
            confirmLabel={(cost) => t('again-confirm', { cost })}
            disabled={!settings}
            onStart={() =>
              settings &&
              void again.quote(() =>
                commands.draftFor(
                  ad.combination_id,
                  image.candidates(revision),
                  image.prepared
                )
              )
            }
          />
        </div>
      )}
      {!settings && (
        <p className="text-sm" role="note">
          {t('settings-unavailable')}
        </p>
      )}
    </section>
  );
}

/**
 * Crop and rotate (free) and the supplier's image tools (paid), reused from
 * the round's editor with the project's data (#200, #201). An ad that needs
 * redoing is not processed: that would not make it exportable.
 */
function ImageWork({
  ad,
  detail,
  commands,
  blocked,
  uploaded,
}: {
  ad: IdeasAd;
  detail: IdeasDetail;
  commands: IdeasCommands;
  blocked: boolean;
  uploaded: boolean;
}) {
  const { t } = useTranslation('ideas');
  const tools = usePaidImage(commands);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<ProblemDetails | null | undefined>(
    undefined
  );
  const edits = (detail.image_edits ?? [])
    .filter((edit) => edit.source_asset_version_id === ad.asset_version_id)
    .map((edit) => ({
      ...edit,
      result_asset_version_id: edit.result_asset_version_id ?? null,
      error_code: edit.error_code ?? null,
    }));
  return (
    <section className={SECTION} aria-labelledby="edit-image">
      <div className="space-y-1">
        <h2 id="edit-image" className="text-base font-semibold">
          {t('edit-image-title')}
        </h2>
        <p className="text-sm text-gray-600 dark:text-gray-400">
          {blocked ? t('ideas-refusal-ad_needs_redo') : t('edit-image-note')}
        </p>
      </div>
      {!blocked && (
        <>
          <MarketingImageEdit
            assetVersionId={ad.asset_version_id}
            busy={busy}
            edits={edits}
            labels={{
              note: t('edit-crop-note'),
              state: (state) => t(`edit-crop-state-${state}`),
            }}
            onCreate={(body) => {
              setBusy(true);
              setProblem(undefined);
              void commands
                .editImage(body)
                .then((outcome) => {
                  if (outcome.kind === 'refused') setProblem(outcome.problem);
                  else if (outcome.kind === 'uncertain') setProblem(null);
                })
                .finally(() => setBusy(false));
            }}
          />
          {problem !== undefined && (
            <p className="text-sm text-red-700 dark:text-red-300" role="alert">
              {ideasProblem(t, problem)}
            </p>
          )}
          {!uploaded && (
            <>
              <MarketingImageTools
                assetVersionId={ad.asset_version_id}
                mode="byok"
                busy={
                  tools.phase.kind === 'quoting' ||
                  tools.phase.kind === 'submitting'
                }
                labels={{
                  note: t('edit-tools-note'),
                  reviewNote: t('edit-tools-review-note'),
                  prepare: t('edit-tools-quote'),
                }}
                onPrepare={(body) =>
                  void tools.quote(async () => {
                    const prepared = await commands.prepareFeedback(body);
                    return prepared.kind === 'done'
                      ? { kind: 'done', data: prepared.data.draft_id }
                      : prepared;
                  })
                }
              />
              <PaidImageAction
                paid={tools}
                confirmLabel={(cost) => t('tools-confirm', { cost })}
              />
            </>
          )}
        </>
      )}
    </section>
  );
}

/**
 * The versions of this ad (#200): each change made a new one and kept the
 * old, which is still an ad. A version is named by its picture and number,
 * never by an id; any of them can be shown here, compared or kept instead.
 */
function Versions({
  current,
  versions,
  onSwitch,
}: {
  current: IdeasAd;
  versions: IdeasAd[];
  onSwitch: (assetVersionId: string) => void;
}) {
  const { t } = useTranslation('ideas');
  if (versions.length < 2) return null;
  const history: HistoryAsset[] = versions.map((item) => ({
    asset_version_id: item.asset_version_id,
    parent_version_id: item.source_asset_version_id ?? null,
    created_at: item.created_at,
    edit_operation: item.origin ?? 'generated',
  }));
  const byId = new Map(versions.map((item) => [item.asset_version_id, item]));
  return (
    <section className={SECTION} aria-labelledby="edit-versions">
      <h2 id="edit-versions" className="text-base font-semibold">
        {t('edit-versions-title', { count: versions.length })}
      </h2>
      <ol className="space-y-2">
        {versions.map((item, index) => {
          const here = item.asset_version_id === current.asset_version_id;
          return (
            <li
              key={item.asset_version_id}
              className={classNames(
                'flex flex-wrap items-center gap-3 rounded-lg border p-2',
                here
                  ? 'border-gray-900 dark:border-gray-100'
                  : 'border-gray-200 dark:border-gray-700'
              )}
            >
              <AdThumbnail
                versionId={item.asset_version_id}
                alt={t('edit-version', { number: index + 1 })}
                size="xs"
              />
              <div className="min-w-0 flex-1 text-sm">
                <p className="font-medium">
                  {t('edit-version', { number: index + 1 })} ·{' '}
                  {t(`ideas-ad-origin-${item.origin ?? 'generated'}`)}
                </p>
                <p className="text-gray-600 dark:text-gray-400">
                  {t(`ideas-ad-status-${item.status}`)}
                  {item.kept ? ` · ${t('ideas-ad-kept')}` : ''}
                </p>
              </div>
              {here ? (
                <span className="text-xs font-semibold">
                  {t('edit-version-here')}
                </span>
              ) : (
                <button
                  type="button"
                  className={BUTTON}
                  onClick={() => onSwitch(item.asset_version_id)}
                >
                  {t('edit-version-open')}
                </button>
              )}
            </li>
          );
        })}
      </ol>
      <MarketingVersionHistory
        assets={history}
        busy={false}
        editable
        showIds={false}
        onEdit={onSwitch}
        labels={{
          note: t('edit-history-note'),
          continue: t('edit-version-open'),
          operation: (asset) =>
            t(`ideas-ad-origin-${asset.edit_operation ?? 'generated'}`),
          status: (asset) => {
            const item = byId.get(asset.asset_version_id);
            return item ? t(`ideas-ad-status-${item.status}`) : '';
          },
        }}
      />
    </section>
  );
}
