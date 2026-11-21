import { useEffect, useId, useRef, useState } from 'react';
import { useRouter } from 'next/router';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { GuardedLink } from '@/components/workspace/GuardedLink';
import { useWorkspaceProducts } from '@/hooks/products/useWorkspaceProducts';
import { useIdeasProject } from '@/hooks/ideas/useIdeasProject';
import {
  useFirstBatch,
  useFirstBatchIntake,
  type ResumeInput,
  type ResumeOutcome,
} from '@/hooks/ideas/useFirstBatch';
import { errorMessage } from '@/lib/i18n/domain-errors';
import type { IdeasDetail } from '@/lib/ideas/ideas-view';
import {
  costLabel,
  costsMore,
  adsMade,
  isRunning,
  progressSteps,
  readFromImagesOnly,
  stopView,
  type FirstBatch,
  type ProgressStepState,
  type StopView,
} from '@/lib/ideas/first-batch-view';
import { productViewHref } from '@/lib/products/workspace-products';
import { newProductHref } from '@/lib/workspace-navigator';
import { PhotoStatusBadge, PhotoThumbnail, usePhotoLabel } from './PhotoParts';
import { SpendLimitLink } from './SpendLimitLink';
import { StartForm } from './StartForm';

/** Why a step failed, said plainly, for the codes that have a plain reason. */
export const STEP_FAILED_WHY: Record<string, string> = {
  budget_exceeded: 'stop-why-budget',
  // OpenAI refused the key for images: often an organisation not verified
  // for its image models, which saving the key cannot tell (#192, #204).
  credential_rejected: 'stop-why-image-key',
  credential_required: 'stop-why-key',
  openai_key_required: 'stop-why-key',
  planner_not_configured: 'stop-why-not-set-up',
  price_unavailable: 'stop-why-not-set-up',
  qc_not_configured: 'stop-why-not-set-up',
};

const CARD =
  'rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900 sm:p-5';

/**
 * The reading page (ADR 0018, design §5.2, #195): the first batch's progress
 * from reading the product to making the ads, what was read as it arrives,
 * and -- when it stops -- what to do about it. It asks again every couple of
 * seconds; once the ads are made it refreshes the product list (so the new
 * product is in the sidebar) and opens the product's ad ideas.
 */
export function ReadingPage({ batchId }: { batchId: string }) {
  const { t } = useTranslation('ideas');
  const { current } = useWorkspace();
  const { batch, notFound, failed, isLoading, refresh, resume } =
    useFirstBatch(batchId);

  if (isLoading)
    return (
      <div className="flex items-center gap-3 py-8" role="status">
        <span className="loading loading-spinner" aria-hidden="true" />
        <span className="text-sm">{t('reading-loading')}</span>
      </div>
    );

  if (failed || !batch)
    return (
      <div className="mx-auto max-w-3xl space-y-3" role="alert">
        <p className="text-sm">
          {notFound ? t('reading-not-found') : t('reading-problem')}
        </p>
        <div className="flex flex-wrap gap-2">
          {!notFound && (
            <button
              type="button"
              className="btn btn-sm min-h-11"
              onClick={() => void refresh()}
            >
              {t('start-retry')}
            </button>
          )}
          <GuardedLink
            href={newProductHref(current.slug)}
            className="btn btn-ghost btn-sm min-h-11"
          >
            {t('reading-back-to-start')}
          </GuardedLink>
        </div>
      </div>
    );

  return batch.project_id ? (
    <ProjectReading
      batch={batch}
      projectId={batch.project_id}
      resume={resume}
    />
  ) : (
    <Reading batch={batch} detail={undefined} resume={resume} />
  );
}

type Resume = (input: ResumeInput) => Promise<ResumeOutcome>;

/** Once the product exists, what was read comes from its ad ideas. */
function ProjectReading({
  batch,
  projectId,
  resume,
}: {
  batch: FirstBatch;
  projectId: string;
  resume: Resume;
}) {
  const { detail, refresh } = useIdeasProject(projectId);
  // The ideas move on between the batch's steps; ask again when it does.
  useEffect(() => {
    void refresh();
  }, [batch.state, batch.updated_at, refresh]);
  return <Reading batch={batch} detail={detail} resume={resume} />;
}

/** Reading usually takes well under this; past it the page says it is still going (#204). */
export const SLOW_READING_MS = 2 * 60_000;

/** True once the batch has been reading for longer than usual. */
function useSlowReading(batch: FirstBatch): boolean {
  const reading = batch.state === 'reading';
  const since = Date.parse(batch.created_at);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!reading) return;
    const timer = setInterval(() => setNow(Date.now()), 15_000);
    return () => clearInterval(timer);
  }, [reading]);
  return reading && Number.isFinite(since) && now - since > SLOW_READING_MS;
}

function Reading({
  batch,
  detail,
  resume,
}: {
  batch: FirstBatch;
  detail: IdeasDetail | undefined;
  resume: Resume;
}) {
  const { t } = useTranslation('ideas');
  const router = useRouter();
  const { current } = useWorkspace();
  const products = useWorkspaceProducts();
  const intake = useFirstBatchIntake(
    batch.intake_id,
    batch.state === 'reading'
  );
  const imagesOnly = readFromImagesOnly(intake?.input);
  const name =
    detail?.understanding?.product_name || intake?.card?.name || null;
  const view = stopView(batch);
  const running = isRunning(batch);
  const slow = useSlowReading(batch);

  // The new product belongs in the sidebar as soon as it exists.
  const listedProduct = useRef<string | null>(null);
  const refreshProducts = products.refresh;
  useEffect(() => {
    if (!batch.product_id || listedProduct.current === batch.product_id) return;
    listedProduct.current = batch.product_id;
    void refreshProducts();
  }, [batch.product_id, refreshProducts]);

  // Done: refresh the product list first, then open the product's ad ideas.
  const leaving = useRef(false);
  useEffect(() => {
    // Done, or (S9) every ad made and the checks running: the ideas page shows them.
    if (
      !adsMade({ state: batch.state }) ||
      !batch.product_id ||
      leaving.current
    )
      return;
    leaving.current = true;
    const productId = batch.product_id;
    void (async () => {
      await refreshProducts();
      await router.replace(productViewHref(current.slug, productId, 'ideas'));
    })();
  }, [batch.state, batch.product_id, current.slug, refreshProducts, router]);

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold">
          {name ? t('reading-title', { name }) : t('reading-title-unnamed')}
        </h1>
        {running && (
          <p className="text-sm text-gray-600 dark:text-gray-400">
            {t('reading-intro')}
          </p>
        )}
        {slow && (
          <p
            className="rounded-lg border border-gray-200 bg-gray-50 p-3 text-sm dark:border-gray-700 dark:bg-gray-900"
            role="status"
            data-testid="reading-slow"
          >
            {t('reading-slow')}
          </p>
        )}
        {adsMade(batch) && (
          <p className="text-sm" role="status">
            {t('reading-done')}
          </p>
        )}
      </div>

      {view && (
        // A new stop starts with an empty panel; the same stop keeps what was typed.
        <StopPanel
          key={`${view.reason}:${view.step}:${batch.updated_at}`}
          batch={batch}
          view={view}
          resume={resume}
        />
      )}

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_18rem]">
        <section aria-labelledby="reading-found" className={CARD}>
          <h2 id="reading-found" className="text-base font-semibold">
            {t('reading-found')}
          </h2>
          <ReadFacts detail={detail} imagesOnly={imagesOnly} />
          <ReadGuessesAndPhotos detail={detail} />
        </section>
        <div className="order-first space-y-3 lg:order-none">
          <Progress batch={batch} made={detail?.ads.length ?? 0} />
          {(batch.state === 'generating' || view?.step === 'checks') &&
            batch.product_id && (
              <GuardedLink
                href={productViewHref(current.slug, batch.product_id, 'ideas')}
                className="btn btn-outline btn-sm min-h-11 w-full"
              >
                {t('reading-see-ads')}
              </GuardedLink>
            )}
        </div>
      </div>
    </div>
  );
}

const STEP_MARK: Record<ProgressStepState, string> = {
  done: '✓',
  active: '',
  pending: '○',
  stopped: '!',
};

function Progress({ batch, made }: { batch: FirstBatch; made: number }) {
  const { t } = useTranslation('ideas');
  return (
    <section aria-labelledby="reading-progress" className={CARD}>
      <h2 id="reading-progress" className="text-base font-semibold">
        {t('reading-progress')}
      </h2>
      <ol className="mt-3 space-y-3" data-testid="reading-progress">
        {progressSteps(batch).map((step) => (
          <li
            key={step.key}
            data-state={step.state}
            className={classNames(
              'flex items-center gap-3 text-sm',
              step.state === 'pending' && 'text-gray-500 dark:text-gray-400',
              step.state === 'stopped' &&
                'font-semibold text-red-800 dark:text-red-300'
            )}
          >
            <span
              aria-hidden="true"
              className="flex h-5 w-5 shrink-0 items-center justify-center"
            >
              {step.state === 'active' ? (
                <span className="loading loading-spinner loading-xs" />
              ) : (
                STEP_MARK[step.state]
              )}
            </span>
            <span>
              {step.key === 'images' && batch.ads_planned
                ? t('reading-step-images-count', {
                    done: Math.min(made, batch.ads_planned),
                    count: batch.ads_planned,
                  })
                : t(`reading-step-${step.key}`)}
              <span className="sr-only">
                {' '}
                {t(`reading-step-state-${step.state}`)}
              </span>
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}

function ReadFacts({
  detail,
  imagesOnly,
}: {
  detail: IdeasDetail | undefined;
  imagesOnly: boolean;
}) {
  const { t } = useTranslation('ideas');
  const facts = detail?.understanding?.facts ?? [];
  // Read but not sourced (design §5.2, #224): listed, never used.
  const unsourced = detail?.understanding?.unsupported_claims ?? [];
  const claims = detail?.ideas?.angles ?? [];
  if (facts.length === 0)
    return (
      <p className="mt-3 text-sm text-gray-600 dark:text-gray-400">
        {t('reading-found-waiting')}
      </p>
    );
  return (
    <div className="mt-3 space-y-5">
      <ul role="list" className="space-y-3" data-testid="reading-facts">
        {facts.map((fact) => (
          <li key={fact.id} className="space-y-1">
            <p className="text-sm">
              <span aria-hidden="true" className="mr-2 text-emerald-700">
                {STEP_MARK.done}
              </span>
              {fact.statement}
            </p>
            {fact.source_quote && (
              <p className="text-xs text-gray-500 dark:text-gray-400">
                {imagesOnly
                  ? t('reading-fact-source-images', {
                      quote: fact.source_quote,
                    })
                  : t('reading-fact-source', { quote: fact.source_quote })}
              </p>
            )}
          </li>
        ))}
      </ul>
      {unsourced.length > 0 && (
        <ul role="list" className="space-y-2" data-testid="reading-unsourced">
          {unsourced.map((claim) => (
            <li key={claim.id} className="text-sm">
              <span
                aria-hidden="true"
                className="mr-2 font-bold text-amber-700"
              >
                !
              </span>
              {t('reading-unsourced', { claim: claim.text })}
            </li>
          ))}
        </ul>
      )}
      <p className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200">
        {t('reading-facts-unchecked')}
      </p>
      {claims.length > 0 && (
        <div className="space-y-2">
          <h3 className="text-sm font-semibold">{t('reading-claims')}</h3>
          <ol className="list-decimal space-y-1 pl-5 text-sm">
            {claims.map((angle) => (
              <li key={angle.id}>{angle.proposition}</li>
            ))}
          </ol>
        </div>
      )}
    </div>
  );
}

/**
 * What else the reading found (design §5.2, #204): the AI's guesses -- never
 * facts, never checked -- and the photos that may show the product, which
 * ads may use before anyone checks them.
 */
function ReadGuessesAndPhotos({ detail }: { detail: IdeasDetail | undefined }) {
  const { t } = useTranslation('ideas');
  const label = usePhotoLabel();
  const understanding = detail?.understanding;
  if (!detail || !understanding || (understanding.facts ?? []).length === 0)
    return null;
  const guesses = [
    understanding.audience,
    ...understanding.inferences.map((inference) => inference.statement),
  ].filter((guess) => guess?.trim());
  const photos = (detail.photos ?? []).filter(
    (photo) => photo.status !== 'rejected'
  );
  return (
    <div className="mt-5 space-y-5">
      {guesses.length > 0 && (
        <section className="space-y-2" data-testid="reading-guesses">
          <h3 className="text-sm font-semibold">{t('reading-guesses')}</h3>
          <p className="text-xs text-gray-600 dark:text-gray-400">
            {t('reading-guesses-note')}
          </p>
          <ul role="list" className="space-y-1">
            {guesses.map((guess, index) => (
              <li key={index} className="text-sm italic">
                {guess}
              </li>
            ))}
          </ul>
        </section>
      )}
      {photos.length > 0 && (
        <section className="space-y-2" data-testid="reading-photos">
          <h3 className="text-sm font-semibold">{t('reading-photos')}</h3>
          <p className="text-xs text-gray-600 dark:text-gray-400">
            {t('reading-photos-note')}
          </p>
          <ul role="list" className="flex flex-wrap gap-3">
            {photos.map((photo) => {
              const { role } = label(photo);
              return (
                <li key={photo.photo_id} className="space-y-1">
                  <PhotoThumbnail
                    photo={photo}
                    projectId={detail.id}
                    alt={role}
                  />
                  <PhotoStatusBadge status={photo.status} />
                </li>
              );
            })}
          </ul>
        </section>
      )}
    </div>
  );
}

/**
 * What to do about a stop (design §5.2): choose a model, paste something else
 * and start again, add material, or continue. Continuing that costs more says
 * how much more on its button; clicking it confirms the new maximum.
 */
function StopPanel({
  batch,
  view,
  resume,
}: {
  batch: FirstBatch;
  view: StopView;
  resume: Resume;
}) {
  const { t, i18n } = useTranslation('ideas');
  const { current } = useWorkspace();
  const ids = useId();
  const [variant, setVariant] = useState<string | null>(null);
  const [material, setMaterial] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const offer = view.resume;
  const withCost = (action: string) =>
    offer && costsMore(offer.additional_cost)
      ? t('stop-with-cost', {
          action,
          cost: costLabel(i18n.language, offer.additional_cost),
        })
      : action;

  async function go(input: Omit<ResumeInput, 'quoteHash'>) {
    if (!offer || busy) return;
    setBusy(true);
    setNotice(null);
    try {
      const outcome = await resume({ ...input, quoteHash: offer.quote_hash });
      if (outcome.kind === 'refused')
        setNotice(
          outcome.uncertain
            ? t('stop-unresolved')
            : errorMessage(t, outcome.problem)
        );
    } finally {
      setBusy(false);
    }
  }

  // Ads that did not come out; a stop before any was admitted reads as the step failing.
  const imagesFailed = view.failedAds > 0;
  const title =
    view.kind === 'start_again'
      ? t(
          `stop-${view.reason === 'page_unreadable' ? 'page_unreadable' : 'reading_failed'}-title`
        )
      : imagesFailed
        ? t('stop-images-failed-title', { count: view.failedAds })
        : view.reason === 'step_failed' || view.reason === 'fault'
          ? t(`stop-${view.reason}-title`, {
              step: t(`reading-step-${view.step}`),
            })
          : t(`stop-${view.reason}-title`);
  const body =
    view.kind === 'start_again'
      ? t(
          `stop-${view.reason === 'page_unreadable' ? 'page_unreadable' : 'reading_failed'}-body`
        )
      : imagesFailed
        ? t('stop-images-failed-body')
        : view.reason === 'over_budget'
          ? t('stop-over_budget-body', {
              cost: costLabel(i18n.language, batch.confirmed_max_cost),
            })
          : view.reason === 'uncertain' && view.runIds.length > 0
            ? t('stop-uncertain-images-body')
            : t(`stop-${view.reason}-body`);
  const why =
    view.reason === 'step_failed' && batch.attention?.code
      ? STEP_FAILED_WHY[batch.attention.code]
      : undefined;

  const button = (label: string, onClick: () => void, disabled = false) => (
    <button
      type="button"
      className="btn btn-primary min-h-12 w-full sm:w-auto"
      disabled={disabled || busy}
      onClick={onClick}
    >
      {busy && (
        <span
          className="loading loading-spinner loading-xs"
          aria-hidden="true"
        />
      )}
      {busy ? t('stop-busy') : withCost(label)}
    </button>
  );

  return (
    <section
      aria-labelledby={`${ids}-title`}
      data-testid="reading-stop"
      data-reason={view.reason}
      className="space-y-4 rounded-xl border border-amber-300 bg-amber-50 p-4 dark:border-amber-800 dark:bg-amber-950 sm:p-5"
    >
      <div className="space-y-1">
        <h2 id={`${ids}-title`} className="text-lg font-bold">
          {title}
        </h2>
        <p className="text-sm">{body}</p>
        {why && <p className="text-sm">{t(why)}</p>}
        {why === STEP_FAILED_WHY.budget_exceeded && <SpendLimitLink />}
        {view.kind !== 'start_again' && (
          <p className="text-sm text-gray-700 dark:text-gray-300">
            {t('stop-kept')}
          </p>
        )}
      </div>

      {view.kind === 'start_again' && (
        <StartForm
          label={t('stop-start-again-label')}
          target={{ market: batch.market, adLanguage: batch.ad_language }}
          compact
        />
      )}

      {view.kind === 'choose_model' && offer && (
        <fieldset className="space-y-3">
          <legend className="mb-2 text-sm font-semibold">
            {t('stop-several_models-choose')}
          </legend>
          <div className="space-y-2">
            {view.variants.map((label) => (
              <label
                key={label}
                className="flex min-h-11 cursor-pointer items-center gap-3 rounded-lg border border-gray-300 bg-white px-3 dark:border-gray-600 dark:bg-gray-900"
              >
                <input
                  type="radio"
                  name={`${ids}-variant`}
                  className="radio radio-sm"
                  checked={variant === label}
                  onChange={() => setVariant(label)}
                />
                <span className="text-sm">{label}</span>
              </label>
            ))}
          </div>
          {button(
            t('stop-several_models-action'),
            () => void go({ selectedVariant: variant ?? undefined }),
            variant === null
          )}
        </fieldset>
      )}

      {view.kind === 'add_material' && offer && (
        <div className="space-y-3">
          <label className="form-control w-full">
            <span className="mb-1 text-sm font-semibold">
              {t('stop-too_few_claims-field')}
            </span>
            <textarea
              className="textarea textarea-bordered min-h-32 w-full text-base"
              placeholder={t('stop-too_few_claims-placeholder')}
              value={material}
              disabled={busy}
              onChange={(event) => setMaterial(event.target.value)}
            />
          </label>
          {button(
            t('stop-too_few_claims-action'),
            () => void go({ additionalText: material.trim() }),
            material.trim() === ''
          )}
        </div>
      )}

      {(view.kind === 'continue' || view.kind === 'uncertain') &&
        offer &&
        button(
          view.reason === 'step_failed' || view.reason === 'fault'
            ? t('stop-retry')
            : t('stop-continue'),
          () => void go({})
        )}

      {view.kind === 'uncertain' && !offer && view.runIds.length > 0 && (
        // The existing reconciliation of a generation run (#204): say there
        // whether the image was made; continuing then becomes possible here.
        <ul role="list" className="flex flex-wrap gap-2">
          {view.runIds.map((runId, index) => (
            <li key={runId}>
              <GuardedLink
                href={`/w/${current.slug}/generate/runs/${encodeURIComponent(runId)}`}
                className="btn btn-primary min-h-12 w-full sm:w-auto"
                data-testid="reading-stop-reconcile"
              >
                {view.runIds.length === 1
                  ? t('stop-uncertain-reconcile')
                  : t('stop-uncertain-reconcile-numbered', {
                      number: index + 1,
                    })}
              </GuardedLink>
            </li>
          ))}
        </ul>
      )}

      {view.kind !== 'start_again' && !offer && (
        <p className="text-sm font-semibold" data-testid="reading-stop-waiting">
          {view.kind === 'uncertain'
            ? t('stop-uncertain-waiting')
            : t('stop-cannot-continue')}
        </p>
      )}

      {notice && (
        <p className="text-sm text-red-800 dark:text-red-300" role="alert">
          {notice}
        </p>
      )}
    </section>
  );
}
