import { useId, useMemo, useState, type ReactNode } from 'react';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { GuardedLink } from '@/components/workspace/GuardedLink';
import type { DownloadGrant } from '@/lib/domain-client/blob-transfer';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import { useBlobTransferClient } from '@/lib/domain-client/upload-deps';
import {
  allTicked,
  checkSheet,
  factKey,
  findingKey,
  leftToTick,
  refusedAds,
  verifyBody,
  type RefusedAd,
  type WaitingWhy,
} from '@/lib/ideas/check-sheet';
import { ideasProblem } from '@/lib/ideas/ideas-problems';
import {
  placeOf,
  type IdeasAd,
  type IdeasDetail,
} from '@/lib/ideas/ideas-view';
import { saveBlob } from '@/lib/ideas/save-file';
import type {
  IdeasCommands,
  IdeasDownload,
  ImageSettings,
} from '@/hooks/ideas/useIdeasCommands';
import { usePaidImage } from '@/hooks/ideas/usePaidImage';
import { AdThumbnail } from './AdCard';
import { ChecksButton } from './ChecksButton';
import { FindingList } from './FindingList';
import { BUTTON, PaidImageAction } from './PaidImageAction';

const SECTION =
  'space-y-4 rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900 sm:p-6';
const ROW =
  'flex flex-wrap items-start gap-3 rounded-lg border border-gray-200 p-3 dark:border-gray-700';
const CHECKBOX = 'mt-0.5 h-6 w-6 shrink-0 accent-gray-900';

type Save = 'saving' | 'saved' | 'failed';
type Phase =
  | { kind: 'idle' }
  | { kind: 'working' }
  | { kind: 'refused'; ads: RefusedAd[] }
  | { kind: 'problem'; problem: ProblemDetails | null }
  | {
      kind: 'done';
      downloads: IdeasDownload['downloads'];
      saves: Record<string, Save>;
      /** Ready ads the confirmation did not make exportable, with why. */
      left: IdeasAd[];
    };

export interface CheckSheetProps {
  projectId: string;
  detail: IdeasDetail;
  commands: IdeasCommands;
  settings: ImageSettings | null;
  profileHref: string;
  onClose: () => void;
  onEdit: (ad: IdeasAd) => void;
  /** Takes an ad out of this download ("I don't want this one"). */
  onDrop: (ad: IdeasAd) => Promise<void> | void;
}

/**
 * The check before downloading (ADR 0018 §3, design §5.3, #198, #201): only
 * what the kept ads use -- the facts they cite, their product's look or
 * photo, the check findings still open -- confirmed once. Confirming records
 * the check (`verify_marketing_ideas`) and then downloads the ads, one file
 * each (`download_marketing_ideas`).
 */
export function CheckSheet({
  projectId,
  detail,
  commands,
  settings,
  profileHref,
  onClose,
  onEdit,
  onDrop,
}: CheckSheetProps) {
  const { t } = useTranslation('ideas');
  const blobClient = useBlobTransferClient();
  const model = useMemo(() => checkSheet(detail), [detail]);
  const [facts, setFacts] = useState<Set<string>>(new Set());
  const [looks, setLooks] = useState<Set<string>>(new Set());
  const [findings, setFindings] = useState<Set<string>>(new Set());
  const [phase, setPhase] = useState<Phase>({ kind: 'idle' });
  const ticks = { facts, looks, findings };
  const left = leftToTick(model, ticks);
  const ready = model.ready.length;
  const canConfirm =
    ready > 0 && allTicked(model, ticks) && phase.kind !== 'working';

  const toggle =
    (set: Set<string>, update: (next: Set<string>) => void) =>
    (key: string, on: boolean) => {
      const next = new Set(set);
      if (on) next.add(key);
      else next.delete(key);
      update(next);
    };
  const tickFact = toggle(facts, setFacts);
  const tickLook = toggle(looks, setLooks);
  const tickFinding = toggle(findings, setFindings);

  async function saveOne(item: IdeasDownload['downloads'][number]) {
    try {
      const blob = await blobClient.downloadBlob(
        item.download_grant as unknown as DownloadGrant
      );
      saveBlob(item.filename, blob);
      return 'saved' as const;
    } catch {
      return 'failed' as const;
    }
  }

  async function saveAll(
    downloads: IdeasDownload['downloads'],
    leftOut: IdeasAd[]
  ) {
    const saves: Record<string, Save> = Object.fromEntries(
      downloads.map((item) => [item.asset_version_id, 'saving' as Save])
    );
    setPhase({ kind: 'done', downloads, saves: { ...saves }, left: leftOut });
    // One file at a time: browsers handle a row of saves better than a burst.
    for (const item of downloads) {
      saves[item.asset_version_id] = await saveOne(item);
      setPhase({ kind: 'done', downloads, saves: { ...saves }, left: leftOut });
    }
  }

  async function download(ids: string[], leftOut: IdeasAd[]) {
    const outcome = await commands.download(ids);
    if (outcome.kind === 'done')
      return saveAll(outcome.data.downloads, leftOut);
    if (outcome.kind === 'refused') {
      await commands.refresh();
      const refused = refusedAds(
        (outcome.problem as { params?: Record<string, unknown> }).params
      );
      return setPhase(
        outcome.problem.code === 'ads_not_exportable' && refused.length > 0
          ? { kind: 'refused', ads: refused }
          : { kind: 'problem', problem: outcome.problem }
      );
    }
    setPhase({ kind: 'problem', problem: null });
  }

  async function confirm() {
    if (!canConfirm) return;
    setPhase({ kind: 'working' });
    const readyIds = new Set(model.ready.map((item) => item.asset_version_id));
    const body = verifyBody(model, ticks);
    let now = detail;
    if (body) {
      const verified = await commands.verify(body);
      if (verified.kind !== 'done') {
        if (verified.kind === 'refused') await commands.refresh();
        return setPhase({
          kind: 'problem',
          problem: verified.kind === 'refused' ? verified.problem : null,
        });
      }
      now = verified.data;
    }
    const after = now.ads.filter((item) => readyIds.has(item.asset_version_id));
    const exportable = after.filter((item) => item.status === 'exportable');
    const leftOut = after.filter((item) => item.status !== 'exportable');
    if (exportable.length === 0)
      return setPhase({
        kind: 'refused',
        ads: leftOut.map((item) => ({
          asset_version_id: item.asset_version_id,
          status: item.status,
          reasons: item.reasons ?? [],
        })),
      });
    await download(
      exportable.map((item) => item.asset_version_id),
      leftOut
    );
  }

  async function saveAgain(id: string) {
    if (phase.kind !== 'done') return;
    const outcome = await commands.download([id]);
    const fresh =
      outcome.kind === 'done'
        ? outcome.data.downloads.find((item) => item.asset_version_id === id)
        : undefined;
    const result = fresh ? await saveOne(fresh) : 'failed';
    setPhase((current) =>
      current.kind === 'done'
        ? { ...current, saves: { ...current.saves, [id]: result } }
        : current
    );
  }

  const adLabel = (ad: IdeasAd) =>
    placeOf(detail, ad).headline ?? t('ideas-ad-image');

  if (phase.kind === 'done')
    return (
      <SheetFrame onClose={onClose}>
        <section className={SECTION} aria-live="polite">
          <h2 className="text-lg font-bold">
            {t('check-done-title', { count: phase.downloads.length })}
          </h2>
          <p className="text-sm text-gray-700 dark:text-gray-300">
            {t('check-done-note')}
          </p>
          <ul role="list" className="space-y-3">
            {phase.downloads.map((item) => {
              const ad = detail.ads.find(
                (a) => a.asset_version_id === item.asset_version_id
              );
              const save = phase.saves[item.asset_version_id];
              return (
                <li key={item.asset_version_id} className={ROW}>
                  <AdThumbnail
                    versionId={item.asset_version_id}
                    alt={ad ? adLabel(ad) : t('ideas-ad-image')}
                  />
                  <div className="min-w-0 flex-1 space-y-1 text-sm">
                    <p className="font-semibold">
                      {ad ? adLabel(ad) : t('ideas-ad-image')}
                    </p>
                    <p role="status">{t(`check-save-${save}`)}</p>
                  </div>
                  {save === 'failed' && (
                    <button
                      type="button"
                      className={BUTTON}
                      onClick={() => void saveAgain(item.asset_version_id)}
                    >
                      {t('check-save-again')}
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
          {phase.left.length > 0 && (
            <NotDownloaded ads={phase.left} label={adLabel} />
          )}
        </section>
      </SheetFrame>
    );

  return (
    <SheetFrame onClose={onClose}>
      <div className="space-y-1">
        <h1 className="text-2xl font-bold sm:text-3xl">{t('check-title')}</h1>
        <p className="text-base text-gray-600 dark:text-gray-400">
          {t('check-intro', { count: model.kept })}
        </p>
      </div>

      {phase.kind === 'refused' && (
        <section
          className="space-y-3 rounded-xl border border-red-200 bg-red-50 p-4 text-red-950 dark:border-red-800 dark:bg-red-950 dark:text-red-100"
          role="alert"
        >
          <p className="font-semibold">{t('check-refused-title')}</p>
          <ul role="list" className="space-y-3">
            {phase.ads.map((item) => {
              const ad = detail.ads.find(
                (a) => a.asset_version_id === item.asset_version_id
              );
              return (
                <li key={item.asset_version_id} className="flex gap-3">
                  <AdThumbnail
                    versionId={item.asset_version_id}
                    alt={ad ? adLabel(ad) : t('ideas-ad-image')}
                  />
                  <div className="min-w-0 text-sm">
                    <p className="font-semibold">
                      {ad ? adLabel(ad) : t('ideas-ad-image')}
                    </p>
                    <Reasons reasons={item.reasons} />
                  </div>
                </li>
              );
            })}
          </ul>
        </section>
      )}
      {phase.kind === 'problem' && (
        <p
          className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-900 dark:border-red-800 dark:bg-red-950 dark:text-red-200"
          role="alert"
        >
          {ideasProblem(t, phase.problem)}
        </p>
      )}

      <section className={SECTION} aria-labelledby="check-facts">
        <div className="space-y-1">
          <h2 id="check-facts" className="text-lg font-bold">
            {t('check-facts-title', { count: model.facts.length })}
          </h2>
          <p className="text-sm text-gray-600 dark:text-gray-400">
            {model.facts.length > 0
              ? t('check-facts-intro')
              : t('check-facts-none')}
            {model.checkedFacts > 0 &&
              ' ' + t('check-facts-checked', { count: model.checkedFacts })}
          </p>
        </div>
        {model.facts.map((fact) => (
          <CheckRow
            key={fact.id}
            checked={facts.has(factKey(fact))}
            onChange={(on) => tickFact(factKey(fact), on)}
            label={fact.statement}
            detail={
              fact.source_quote
                ? t('check-fact-source', { quote: fact.source_quote })
                : undefined
            }
            extra={
              <GuardedLink
                href={profileHref}
                className="inline-flex min-h-11 items-center px-2 text-sm font-medium underline"
              >
                {t('check-fact-change')}
              </GuardedLink>
            }
          />
        ))}
        {model.facts.length > 0 && (
          <p className="text-sm text-gray-600 dark:text-gray-400">
            {t('check-facts-change-note')}
          </p>
        )}
      </section>

      <section className={SECTION} aria-labelledby="check-looks">
        <div className="space-y-1">
          <h2 id="check-looks" className="text-lg font-bold">
            {t('check-looks-title')}
          </h2>
          <p className="text-sm text-gray-600 dark:text-gray-400">
            {model.looks.length > 0
              ? t('check-looks-intro')
              : t('check-looks-none')}
            {model.withPhoto.length > 0 &&
              ' ' + t('check-looks-photo', { count: model.withPhoto.length })}
          </p>
        </div>
        {model.looks.map((ad) => (
          <CheckRow
            key={ad.asset_version_id}
            checked={looks.has(ad.asset_version_id)}
            onChange={(on) => tickLook(ad.asset_version_id, on)}
            label={t('check-look-confirm')}
            detail={adLabel(ad)}
            thumbnail={
              <AdThumbnail
                versionId={ad.asset_version_id}
                alt={adLabel(ad)}
                size="md"
              />
            }
          />
        ))}
      </section>

      <section className={SECTION} aria-labelledby="check-findings">
        <div className="space-y-1">
          <h2 id="check-findings" className="text-lg font-bold">
            {t('check-findings-title')}
          </h2>
          <p className="text-sm text-gray-600 dark:text-gray-400">
            {model.findings.length > 0
              ? t('check-findings-intro')
              : t('check-findings-none')}
            {clearCount(model) > 0 &&
              ' ' + t('check-findings-clear', { count: clearCount(model) })}
          </p>
        </div>
        {model.findings.map(({ ad, checkId, findings: list }) => (
          <div
            key={ad.asset_version_id}
            className="space-y-3 rounded-lg border border-gray-200 p-3 dark:border-gray-700"
            data-testid="check-ad-findings"
          >
            <div className="flex items-center gap-3">
              <AdThumbnail versionId={ad.asset_version_id} alt={adLabel(ad)} />
              <p className="min-w-0 text-sm font-semibold">
                {t('check-findings-ad', {
                  count: list.length,
                  ad: adLabel(ad),
                })}
              </p>
            </div>
            <FindingList
              findings={list}
              language={detail.ad_language}
              actions={(finding) => {
                const key = findingKey(
                  ad.asset_version_id,
                  checkId,
                  finding.check_id
                );
                const on = findings.has(key);
                return (
                  <button
                    type="button"
                    aria-pressed={on}
                    className={classNames(
                      BUTTON,
                      on &&
                        'border-gray-900 bg-gray-900 text-white dark:bg-gray-100 dark:text-gray-900'
                    )}
                    onClick={() => tickFinding(key, !on)}
                  >
                    {on
                      ? t('check-finding-accepted')
                      : t('check-finding-accept')}
                  </button>
                );
              }}
            />
            <div className="flex flex-wrap items-start gap-2">
              <RedoButton ad={ad} commands={commands} settings={settings} />
              <button
                type="button"
                className={BUTTON}
                onClick={() => void onDrop(ad)}
              >
                {t('check-drop')}
              </button>
            </div>
          </div>
        ))}
      </section>

      {(model.waiting.length > 0 || model.redo.length > 0) && (
        <section className={SECTION} aria-labelledby="check-later">
          <div className="space-y-1">
            <h2 id="check-later" className="text-lg font-bold">
              {t('check-later-title', {
                count: model.waiting.length + model.redo.length,
              })}
            </h2>
            <p className="text-sm text-gray-600 dark:text-gray-400">
              {t('check-later-intro')}
            </p>
          </div>
          {model.waiting.some((item) => item.why === 'check_needed') && (
            <ChecksButton
              projectId={projectId}
              detail={detail}
              commands={commands}
            />
          )}
          <ul role="list" className="space-y-3">
            {model.waiting.map(({ ad, why }) => (
              <li key={ad.asset_version_id} className={ROW}>
                <AdThumbnail
                  versionId={ad.asset_version_id}
                  alt={adLabel(ad)}
                />
                <div className="min-w-0 flex-1 space-y-1 text-sm">
                  <p className="font-semibold">{adLabel(ad)}</p>
                  <p role={why === 'checking' ? 'status' : undefined}>
                    {t(`check-waiting-${why}`)}
                  </p>
                </div>
                <WaitingAction
                  why={why}
                  profileHref={profileHref}
                  onEdit={() => onEdit(ad)}
                />
              </li>
            ))}
            {model.redo.map(({ ad, reasons }) => (
              <li key={ad.asset_version_id} className={ROW}>
                <AdThumbnail
                  versionId={ad.asset_version_id}
                  alt={adLabel(ad)}
                />
                <div className="min-w-0 flex-1 space-y-1 text-sm">
                  <p className="font-semibold">
                    {adLabel(ad)}
                    <span className="ml-2 inline-block rounded-full bg-red-100 px-2 py-px text-xs font-semibold text-red-900">
                      {t('ideas-ad-status-needs_redo')}
                    </span>
                  </p>
                  <Reasons reasons={reasons} />
                </div>
                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    className={BUTTON}
                    onClick={() => onEdit(ad)}
                  >
                    {t('ideas-ad-edit')}
                  </button>
                  <button
                    type="button"
                    className={BUTTON}
                    onClick={() => void onDrop(ad)}
                  >
                    {t('check-drop')}
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}

      <div
        className="sticky bottom-0 z-10 -mx-4 space-y-2 border-t border-gray-800 bg-gray-900 px-4 py-3 text-white sm:mx-0 sm:rounded-xl sm:border sm:px-5"
        data-testid="check-confirm-bar"
      >
        <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
          <div className="min-w-0 flex-1 basis-56">
            <p className="text-base font-semibold">
              {ready > 0
                ? t('check-confirm-title', { count: ready })
                : t('check-confirm-none')}
            </p>
            <p className="text-sm text-gray-300">
              {ready === 0
                ? t('check-confirm-none-hint')
                : left > 0
                  ? t('check-confirm-left', { count: left })
                  : t('check-confirm-hint')}
            </p>
          </div>
          <button
            type="button"
            className="min-h-12 w-full rounded-lg bg-white px-5 text-sm font-semibold text-gray-900 disabled:cursor-not-allowed disabled:opacity-40 sm:w-auto"
            disabled={!canConfirm}
            onClick={() => void confirm()}
          >
            {phase.kind === 'working'
              ? t('check-confirm-working')
              : t('check-confirm', { count: ready })}
          </button>
        </div>
      </div>
    </SheetFrame>
  );
}

/** Ready ads whose check has nothing left to look at. */
function clearCount(model: ReturnType<typeof checkSheet>): number {
  const withFindings = new Set(
    model.findings.map(({ ad }) => ad.asset_version_id)
  );
  return model.ready.filter(
    (ad) =>
      ad.check?.state === 'succeeded' && !withFindings.has(ad.asset_version_id)
  ).length;
}

function SheetFrame({
  onClose,
  children,
}: {
  onClose: () => void;
  children: ReactNode;
}) {
  const { t } = useTranslation('ideas');
  return (
    <div className="mx-auto max-w-3xl space-y-5" data-testid="check-sheet">
      <button
        type="button"
        className="inline-flex min-h-11 items-center gap-1 text-sm font-medium underline"
        onClick={onClose}
      >
        {t('back-to-ideas')}
      </button>
      {children}
    </div>
  );
}

function CheckRow({
  checked,
  onChange,
  label,
  detail,
  extra,
  thumbnail,
}: {
  checked: boolean;
  onChange: (on: boolean) => void;
  label: string;
  detail?: string;
  extra?: ReactNode;
  thumbnail?: ReactNode;
}) {
  const id = useId();
  return (
    <div className={ROW}>
      {thumbnail}
      <input
        id={id}
        type="checkbox"
        className={CHECKBOX}
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
      />
      <label
        htmlFor={id}
        className="flex min-h-11 min-w-0 flex-1 basis-48 cursor-pointer flex-col gap-1"
      >
        <span className="text-base font-semibold">{label}</span>
        {detail && (
          <span className="text-sm text-gray-600 dark:text-gray-400">
            {detail}
          </span>
        )}
      </label>
      {extra}
    </div>
  );
}

function Reasons({ reasons }: { reasons: RefusedAd['reasons'] }) {
  const { t } = useTranslation('ideas');
  const codes = Array.from(new Set(reasons.map((reason) => reason.code)));
  return (
    <ul className="list-disc pl-5">
      {codes.map((code) => (
        <li key={code}>{t(`ideas-reason-${code}`)}</li>
      ))}
    </ul>
  );
}

function NotDownloaded({
  ads,
  label,
}: {
  ads: IdeasAd[];
  label: (ad: IdeasAd) => string;
}) {
  const { t } = useTranslation('ideas');
  return (
    <div className="space-y-2 border-t border-gray-200 pt-3 dark:border-gray-700">
      <p className="text-sm font-semibold">
        {t('check-not-downloaded', { count: ads.length })}
      </p>
      <ul role="list" className="space-y-3">
        {ads.map((ad) => (
          <li key={ad.asset_version_id} className="flex gap-3">
            <AdThumbnail versionId={ad.asset_version_id} alt={label(ad)} />
            <div className="min-w-0 text-sm">
              <p className="font-semibold">{label(ad)}</p>
              <Reasons reasons={ad.reasons ?? []} />
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

function WaitingAction({
  why,
  profileHref,
  onEdit,
}: {
  why: WaitingWhy;
  profileHref: string;
  onEdit: () => void;
}) {
  const { t } = useTranslation('ideas');
  if (why === 'photo')
    return (
      <GuardedLink href={profileHref} className={BUTTON}>
        {t('check-waiting-photo-action')}
      </GuardedLink>
    );
  if (why === 'check_outdated')
    return (
      <button type="button" className={BUTTON} onClick={onEdit}>
        {t('ideas-ad-edit')}
      </button>
    );
  return null;
}

/**
 * "Redo this one" from the check sheet (#201): a correction made from the
 * check's findings the user has not accepted (#200); its maximum is the
 * preview's. The new image is a new version that needs its own check.
 */
function RedoButton({
  ad,
  commands,
  settings,
}: {
  ad: IdeasAd;
  commands: IdeasCommands;
  settings: ImageSettings | null;
}) {
  const { t } = useTranslation('ideas');
  const paid = usePaidImage(commands);
  if (ad.origin === 'uploaded') return null;
  return (
    <div className="space-y-1">
      <PaidImageAction
        paid={paid}
        startLabel={t('redo-start')}
        confirmLabel={(cost) => t('redo-confirm', { cost })}
        disabled={!settings}
        onStart={() =>
          settings &&
          void paid.quote(async () => {
            const prepared = await commands.prepareFeedback({
              asset_version_id: ad.asset_version_id,
              note: '',
              provider: settings.provider,
              model: settings.model,
            });
            return prepared.kind === 'done'
              ? { kind: 'done', data: prepared.data.draft_id }
              : prepared;
          })
        }
      />
      {paid.phase.kind === 'started' && (
        <p className="text-xs text-gray-600 dark:text-gray-400">
          {t('check-redo-started')}
        </p>
      )}
    </div>
  );
}
