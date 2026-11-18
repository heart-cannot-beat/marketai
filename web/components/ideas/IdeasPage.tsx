import { useEffect, useId, useMemo, useState, type ReactNode } from 'react';
import { useTranslation } from 'next-i18next';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { GuardedLink } from '@/components/workspace/GuardedLink';
import { useWorkspaceProducts } from '@/hooks/products/useWorkspaceProducts';
import { KeepFailed, useIdeasProject } from '@/hooks/ideas/useIdeasProject';
import { useFirstBatch } from '@/hooks/ideas/useFirstBatch';
import {
  useIdeasCommands,
  type IdeasCommands,
} from '@/hooks/ideas/useIdeasCommands';
import {
  useImageSettings,
  type UseImageSettings,
} from '@/hooks/ideas/useImageSettings';
import { usePaidImage } from '@/hooks/ideas/usePaidImage';
import { errorMessage, errorCodeMessage } from '@/lib/i18n/domain-errors';
import { adsMade, costLabel, costsMore } from '@/lib/ideas/first-batch-view';
import { firstBatchHref } from '@/lib/workspace-navigator';
import {
  checkView,
  firstBatchChecking,
  ideasView,
  openFindings,
  type ClaimGroup,
  type IdeaSlot,
  type IdeasAd,
} from '@/lib/ideas/ideas-view';
import {
  productViewHref,
  type WorkspaceProduct,
} from '@/lib/products/workspace-products';
import { AdCard } from './AdCard';
import { ChecksButton } from './ChecksButton';
import { CheckSheet } from './CheckSheet';
import { EditAd } from './EditAd';
import { AddClaim, AddTelling } from './IdeasAdditions';
import {
  answersOf,
  GuidingQuestions,
  ImageSettingsPanel,
} from './IdeasSettings';
import { PaidImageAction, BUTTON } from './PaidImageAction';
import { PRODUCT_PANEL_ID, ProductPanel } from './ProductPanel';
import { UploadOwnImage } from './UploadOwnImage';

/** Problems whose shared copy speaks of something else; the page says its own. */
const KEEP_PROBLEM_COPY: Record<string, string> = {
  ad_not_found: 'ideas-keep-ad-gone',
  marketing_project_not_found: 'ideas-keep-ad-gone',
};

type Screen =
  | { kind: 'ideas' }
  | { kind: 'check' }
  | { kind: 'edit'; assetVersionId: string };

/**
 * A product's ad ideas (ADR 0018, design §5.3, #196, #201): the ads grouped
 * by claim, each a draft that can be kept or edited; more claims and ways of
 * telling, and the user's own images; the facts the AI read, the image
 * settings and optional guiding questions beside them; and a bottom bar with
 * how many are kept, which leads to the check before downloading.
 *
 * Left for later on purpose: taking ads to a test (step 2), changing or
 * removing a claim and the image model's own instructions (no server path
 * yet), checking product photos (#199).
 */
export function IdeasPage({ product }: { product: WorkspaceProduct }) {
  const { t } = useTranslation('ideas');
  const projectId = product.ideas?.projectId;
  if (!projectId)
    return (
      <p className="text-sm text-gray-600 dark:text-gray-400">
        {t('ideas-none')}
      </p>
    );
  return <IdeasProject product={product} projectId={projectId} />;
}

function IdeasProject({
  product,
  projectId,
}: {
  product: WorkspaceProduct;
  projectId: string;
}) {
  const { t, i18n } = useTranslation('ideas');
  const { current } = useWorkspace();
  const products = useWorkspaceProducts();
  const { detail, failed, isLoading, refresh, setKept } =
    useIdeasProject(projectId);
  const commands = useIdeasCommands(projectId, detail);
  const image = useImageSettings(projectId, detail);
  const view = useMemo(() => (detail ? ideasView(detail) : null), [detail]);
  const [busyAd, setBusyAd] = useState<string | null>(null);
  const [keepProblem, setKeepProblem] = useState<string | null>(null);
  const [screen, setScreen] = useState<Screen>({ kind: 'ideas' });
  const [uploading, setUploading] = useState(false);
  const [telling, setTelling] = useState<string | null>(null);
  const [picked, setPicked] = useState<Record<string, string[]>>({});
  const [own, setOwn] = useState<Record<string, string>>({});
  const profileHref = productViewHref(
    current.slug,
    product.productId,
    'profile'
  );

  // A new screen starts at its top, as a new page would.
  useEffect(() => {
    if (typeof window !== 'undefined' && typeof window.scrollTo === 'function')
      try {
        window.scrollTo({ top: 0 });
      } catch {
        /* jsdom and old browsers */
      }
  }, [screen.kind]);

  // The first batch's failed images are tried again together, confirming
  // what continuing costs (#193). Only then is the batch itself read.
  const firstBatch = detail?.first_batch ?? null;
  const { batch, resume } = useFirstBatch(
    firstBatch?.needs_attention && firstBatch.reason === 'step_failed'
      ? firstBatch.id
      : null
  );
  const retryOffer =
    batch?.state === 'needs_attention' &&
    batch.attention?.reason === 'step_failed' &&
    batch.attention.step === 'images' &&
    (batch.attention.combination_ids ?? []).length > 0
      ? batch.resume
      : null;
  const [retrying, setRetrying] = useState(false);
  async function retryImages() {
    if (!retryOffer || retrying) return;
    setRetrying(true);
    setKeepProblem(null);
    try {
      const outcome = await resume({ quoteHash: retryOffer.quote_hash });
      if (outcome.kind === 'resumed') {
        await refresh();
        void products.refresh();
      } else
        setKeepProblem(
          outcome.uncertain
            ? t('ideas-keep-uncertain')
            : errorMessage(t, outcome.problem)
        );
    } finally {
      setRetrying(false);
    }
  }
  const batchStopped =
    firstBatch?.state === 'needs_attention' || firstBatch?.state === 'failed';
  // Images outside the first batch are made here; while the batch is still
  // on its way (or stopped), its own steps make them.
  const batchEnded =
    !firstBatch || firstBatch.state === 'done' || firstBatch.state === 'failed';
  // Done, or (S9) every ad made and being checked: nothing to point back to.
  const notice = firstBatch && !adsMade(firstBatch) && (
    <FirstBatchNotice
      href={firstBatchHref(current.slug, firstBatch.id)}
      stopped={batchStopped}
      retriesTogether={retryOffer !== null}
    />
  );

  if (isLoading)
    return (
      <div className="flex items-center gap-3 py-8" role="status">
        <span className="loading loading-spinner" aria-hidden="true" />
        <span className="text-sm">{t('ideas-loading')}</span>
      </div>
    );

  if (failed || !detail || !view)
    return (
      <div className="space-y-3">
        <p className="text-sm">{t('ideas-problem')}</p>
        <button
          type="button"
          className="btn btn-sm"
          onClick={() => void refresh()}
        >
          {t('ideas-retry')}
        </button>
      </div>
    );

  if (!detail.ideas)
    return (
      <div className="space-y-6">
        {notice}
        <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_20rem]">
          {/* A stopped first batch is not preparing anything: the notice says what waits. */}
          {batchStopped ? (
            <div />
          ) : (
            <p
              className="text-sm text-gray-600 dark:text-gray-400"
              role="status"
            >
              {t('ideas-preparing')}
            </p>
          )}
          <ProductPanel understanding={detail.understanding} />
        </div>
      </div>
    );

  async function toggleKept(item: IdeasAd) {
    setBusyAd(item.asset_version_id);
    setKeepProblem(null);
    try {
      await setKept(item.asset_version_id, !item.kept);
      void products.refresh();
    } catch (error) {
      setKeepProblem(
        error instanceof KeepFailed && error.problem
          ? KEEP_PROBLEM_COPY[error.problem.code]
            ? t(KEEP_PROBLEM_COPY[error.problem.code])
            : errorMessage(t, error.problem)
          : error instanceof KeepFailed && error.uncertain
            ? t('ideas-keep-uncertain')
            : t('ideas-keep-failed')
      );
    } finally {
      setBusyAd(null);
    }
  }

  const answers = answersOf(detail.ideas.guiding_questions ?? [], picked, own);

  if (screen.kind === 'check')
    return (
      <CheckSheet
        projectId={projectId}
        detail={detail}
        commands={commands}
        settings={image.settings}
        profileHref={profileHref}
        onClose={() => setScreen({ kind: 'ideas' })}
        onEdit={(item) =>
          setScreen({ kind: 'edit', assetVersionId: item.asset_version_id })
        }
        onDrop={(item) => (item.kept ? toggleKept(item) : undefined)}
        keepProblem={keepProblem}
      />
    );

  if (screen.kind === 'edit') {
    const editing = detail.ads.find(
      (item) => item.asset_version_id === screen.assetVersionId
    );
    const angle = detail.ideas.angles.find((a) => a.id === editing?.angle_id);
    return (
      <div className="space-y-5">
        <EditAd
          projectId={projectId}
          detail={detail}
          assetVersionId={screen.assetVersionId}
          commands={commands}
          image={image}
          onClose={() => setScreen({ kind: 'ideas' })}
          onSwitch={(id) => setScreen({ kind: 'edit', assetVersionId: id })}
          onToggleKept={(item) => void toggleKept(item)}
          onAnotherTelling={(angleId) =>
            setTelling(telling === angleId ? null : angleId)
          }
        />
        {angle && telling === angle.id && (
          <div className="mx-auto max-w-5xl">
            <AddTelling
              angle={angle}
              open
              onOpenChange={(open) => setTelling(open ? angle.id : null)}
              detail={detail}
              commands={commands}
              answers={answers}
            />
          </div>
        )}
        {keepProblem && (
          <p className="text-sm text-red-700" role="alert">
            {keepProblem}
          </p>
        )}
      </div>
    );
  }

  const card = (slot: IdeaSlot) => (
    <AdCard
      key={slot.key}
      slot={slot}
      busy={slot.kind === 'ad' && busyAd === slot.ad.asset_version_id}
      onToggleKept={(item) => void toggleKept(item)}
      onEdit={(item) =>
        setScreen({ kind: 'edit', assetVersionId: item.asset_version_id })
      }
      check={slot.kind === 'ad' ? checkView(slot.ad, detail) : undefined}
      findings={slot.kind === 'ad' ? openFindings(slot.ad) : undefined}
      onRetry={retryOffer ? () => void retryImages() : undefined}
      retryLabel={
        retryOffer && costsMore(retryOffer.additional_cost)
          ? t('ideas-ad-retry-cost', {
              cost: costLabel(i18n.language, retryOffer.additional_cost),
            })
          : undefined
      }
      makeAction={
        batchEnded &&
        (slot.kind === 'failed' || slot.kind === 'not_made') &&
        slot.combinationId && (
          <MakeAd
            combinationId={slot.combinationId}
            failed={slot.kind === 'failed'}
            commands={commands}
            image={image}
            revision={detail.revision}
          />
        )
      }
    />
  );

  const unchecked = detail.ads.some(
    (item) => checkView(item, detail) === 'unchecked'
  );
  const call = detail.latest_call;
  const addingNow =
    call?.addition && (call.state === 'queued' || call.state === 'running');
  const additionFailed = call?.addition && call.state === 'failed';

  return (
    <div className="space-y-6">
      <div className="space-y-1">
        <h2 className="text-xl font-bold sm:text-2xl">
          {t('ideas-title', {
            count: view.adCount,
            claims: view.claims.length,
          })}
        </h2>
        <p className="text-sm text-gray-600 dark:text-gray-400">
          {t('ideas-intro')}
        </p>
        {view.makingCount > 0 && (
          <p className="text-sm text-gray-600 dark:text-gray-400" role="status">
            {t('ideas-making', { count: view.makingCount })}
          </p>
        )}
        {firstBatchChecking(detail) && (
          <p className="text-sm text-gray-600 dark:text-gray-400" role="status">
            {t('ideas-batch-checking')}
          </p>
        )}
        {addingNow && (
          <p className="text-sm text-gray-600 dark:text-gray-400" role="status">
            {t(`ideas-adding-${call!.addition!.kind}`)}
          </p>
        )}
        {additionFailed && (
          <p className="text-sm text-red-700 dark:text-red-300" role="alert">
            {call?.error_code === 'planner_output_invalid'
              ? t('ideas-adding-refused')
              : t('ideas-adding-failed', {
                  reason: errorCodeMessage(t, call?.error_code),
                })}
          </p>
        )}
      </div>

      {notice}

      <div className="flex flex-wrap items-start gap-3">
        <AddClaim detail={detail} commands={commands} answers={answers} />
        <button
          type="button"
          className={BUTTON}
          aria-expanded={uploading}
          onClick={() => setUploading((open) => !open)}
        >
          {t('upload-open')}
        </button>
      </div>
      {uploading && (
        <UploadOwnImage
          detail={detail}
          commands={commands}
          onClose={() => setUploading(false)}
        />
      )}

      {unchecked && batchEnded && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-gray-200 bg-gray-50 p-3 text-sm dark:border-gray-700 dark:bg-gray-900">
          <p className="min-w-0 flex-1 basis-60">{t('ideas-unchecked-hint')}</p>
          <ChecksButton
            projectId={projectId}
            detail={detail}
            commands={commands}
          />
        </div>
      )}

      <a
        href={`#${PRODUCT_PANEL_ID}`}
        className="flex min-h-11 items-center rounded-lg border border-amber-200 bg-amber-50 px-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200 lg:hidden"
      >
        {t('ideas-panel-jump')}
      </a>

      {view.withoutPhotoCount > 0 && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-gray-200 bg-gray-50 p-3 text-sm dark:border-gray-700 dark:bg-gray-900">
          <p className="min-w-0 flex-1 basis-60">{t('ideas-no-photo-hint')}</p>
          <GuardedLink href={profileHref} className="btn btn-sm min-h-11">
            {t('ideas-no-photo-action')}
          </GuardedLink>
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="min-w-0 space-y-5">
          {view.claims.map((group) => {
            const angle = detail.ideas!.angles.find((a) => a.id === group.key);
            return (
              <ClaimSection
                key={group.key}
                group={group}
                card={card}
                more={
                  angle && (
                    <AddTelling
                      angle={angle}
                      explain={false}
                      open={telling === angle.id}
                      onOpenChange={(open) =>
                        setTelling(open ? angle.id : null)
                      }
                      detail={detail}
                      commands={commands}
                      answers={answers}
                    />
                  )
                }
              />
            );
          })}
          {view.earlier.length > 0 && (
            <EarlierSection slots={view.earlier} card={card} />
          )}
        </div>
        <div className="min-w-0 space-y-4">
          <ProductPanel understanding={detail.understanding} />
          <ImageSettingsPanel image={image} />
          <GuidingQuestions
            questions={detail.ideas.guiding_questions ?? []}
            picked={picked}
            own={own}
            onPick={(id, values) => setPicked({ ...picked, [id]: values })}
            onOwn={(id, text) => setOwn({ ...own, [id]: text })}
          />
        </div>
      </div>
      {/* Last on the page, so it stays at the bottom of the screen past the
          facts too, where a phone shows them (design §5.8). */}
      <KeptBar
        keptCount={view.keptCount}
        problem={keepProblem}
        onCheck={() => setScreen({ kind: 'check' })}
      />
    </div>
  );
}

/**
 * Makes one ad's image (#200, #201): one never made (a new claim or way of
 * telling), or one that failed outside the first batch -- the same
 * generation draft previewed and submitted again. Two clicks: the first asks
 * what it costs at most, the second carries that maximum and starts it.
 */
function MakeAd({
  combinationId,
  failed,
  commands,
  image,
  revision,
}: {
  combinationId: string;
  failed: boolean;
  commands: IdeasCommands;
  image: UseImageSettings;
  revision: number;
}) {
  const settings = image.settings;
  const { t } = useTranslation('ideas');
  const paid = usePaidImage(commands);
  return (
    <PaidImageAction
      paid={paid}
      startLabel={failed ? t('make-retry') : t('make-start')}
      confirmLabel={(cost) =>
        failed ? t('make-retry-confirm', { cost }) : t('make-confirm', { cost })
      }
      disabled={!settings}
      onStart={() =>
        settings &&
        void paid.quote(() =>
          commands.draftFor(
            combinationId,
            image.candidates(revision),
            image.prepared
          )
        )
      }
    />
  );
}

/**
 * The product's first batch is still on its way, or stopped and waits for
 * the user (#195): a line and a link to its progress page, where every stop
 * says what to do.
 */
function FirstBatchNotice({
  href,
  stopped,
  retriesTogether,
}: {
  href: string;
  stopped: boolean;
  retriesTogether: boolean;
}) {
  const { t } = useTranslation('ideas');
  return (
    <div
      className={
        stopped
          ? 'flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200'
          : 'flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-gray-200 bg-gray-50 p-3 text-sm dark:border-gray-700 dark:bg-gray-900'
      }
      data-testid="ideas-first-batch"
    >
      <div className="min-w-0 flex-1 basis-60 space-y-1">
        <p>{stopped ? t('ideas-batch-stopped') : t('ideas-batch-running')}</p>
        {retriesTogether && <p>{t('ideas-batch-retry-together')}</p>}
      </div>
      <GuardedLink href={href} className="btn btn-sm min-h-11">
        {stopped ? t('ideas-batch-sort-out') : t('ideas-batch-progress')}
      </GuardedLink>
    </div>
  );
}

const SECTION =
  'space-y-4 rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900 sm:p-5';
const AD_GRID = 'grid grid-cols-2 gap-3 sm:grid-cols-3 sm:gap-4';

function ClaimSection({
  group,
  card,
  more,
}: {
  group: ClaimGroup;
  card: (slot: IdeaSlot) => JSX.Element;
  more?: ReactNode;
}) {
  const { t } = useTranslation('ideas');
  const id = useId();
  return (
    <section aria-labelledby={`${id}-n ${id}-t`} className={SECTION}>
      <div className="space-y-1">
        <p
          id={`${id}-n`}
          className="w-fit rounded-full bg-gray-100 px-2.5 py-0.5 text-xs font-semibold text-gray-700 dark:bg-gray-800 dark:text-gray-300"
        >
          {t('ideas-claim-label', { number: group.number })}
        </p>
        <h3 id={`${id}-t`} className="text-lg font-bold leading-snug">
          {group.proposition}
        </h3>
        {group.competitors && (
          <p className="text-sm text-gray-600 dark:text-gray-400">
            {t(`ideas-competitors-${group.competitors}`)}
          </p>
        )}
        <details className="text-sm">
          <summary className="min-h-11 cursor-pointer py-2 text-gray-700 dark:text-gray-300 sm:min-h-0">
            {t('ideas-claim-why')}
          </summary>
          <p className="text-gray-600 dark:text-gray-400">{group.rationale}</p>
        </details>
      </div>
      <div className={AD_GRID}>{group.slots.map(card)}</div>
      {more}
    </section>
  );
}

function EarlierSection({
  slots,
  card,
}: {
  slots: IdeaSlot[];
  card: (slot: IdeaSlot) => JSX.Element;
}) {
  const { t } = useTranslation('ideas');
  const title = useId();
  return (
    <section aria-labelledby={title} className={SECTION}>
      <div className="space-y-1">
        <h3 id={title} className="text-lg font-bold">
          {t('ideas-earlier-title')}
        </h3>
        <p className="text-sm text-gray-600 dark:text-gray-400">
          {t('ideas-earlier-description')}
        </p>
      </div>
      <div className={AD_GRID}>{slots.map(card)}</div>
    </section>
  );
}

/**
 * "N kept" stays at the bottom of the screen (design §5.8). Its button leads
 * to the check before downloading (#201). With nothing kept it cannot be used
 * (design §5.3).
 */
function KeptBar({
  keptCount,
  problem,
  onCheck,
}: {
  keptCount: number;
  problem: string | null;
  onCheck: () => void;
}) {
  const { t } = useTranslation('ideas');
  return (
    <div
      className="sticky bottom-0 z-10 -mx-4 space-y-2 border-t border-gray-800 bg-gray-900 px-4 py-3 text-white sm:mx-0 sm:rounded-xl sm:border sm:px-5"
      data-testid="ideas-kept-bar"
    >
      {problem && (
        <p className="text-sm text-red-200" role="alert">
          {problem}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
        <div className="min-w-0 flex-1 basis-56">
          <p className="text-base font-semibold">
            {t('ideas-kept-count', { count: keptCount })}
          </p>
          <p className="text-sm text-gray-300">
            {keptCount === 0 ? t('ideas-kept-none-hint') : t('ideas-kept-hint')}
          </p>
        </div>
        <button
          type="button"
          className="min-h-12 w-full rounded-lg bg-white px-5 text-sm font-semibold text-gray-900 disabled:cursor-not-allowed disabled:opacity-40 sm:w-auto"
          disabled={keptCount === 0}
          onClick={onCheck}
        >
          {t('ideas-download-images')}
        </button>
      </div>
    </div>
  );
}
