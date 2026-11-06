import { useEffect, useMemo, useRef, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { ArrowLeftIcon } from '@heroicons/react/24/outline';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { useProductDirectory } from '@/hooks/products/useProductDirectory';
import { useGenerationDefaults } from '@/hooks/test-plans/useGenerationDefaults';
import { newActionId } from '@/components/generation/shared/newActionId';
import { ProductPicker } from '@/components/products/ProductPicker';
import { ProductThumbnail } from '@/components/products/ProductThumbnail';
import { takeProductHandoff } from '@/lib/products/handoff';
import {
  recallNewTestProduct,
  recallNewTestSetup,
  rememberNewTestProduct,
  rememberNewTestSetup,
} from '@/lib/test-plans/handoff';
import {
  audienceCardsFromContent,
  buildCreatePayload,
  defaultAnswers,
  PLAN_SCALES,
  ruleOfThumbImages,
  type PlanGoal,
  type ThreeAnswers,
} from '@/lib/test-plans/plan-model';
import { emitTestPlanEvent } from '@/lib/test-plans/telemetry';
import { GenerationDefaultsBanner } from './GenerationDefaultsBanner';
import { PlanProblemAlert } from './PlanProblemAlert';
import { SeedAssetPicker } from './SeedAssetPicker';

const GOALS: PlanGoal[] = ['explore', 'optimize'];

interface ChoiceProps {
  name: string;
  checked: boolean;
  onSelect: () => void;
  title: string;
  hint?: string;
  isDefault?: boolean;
  disabled?: boolean;
  testId?: string;
}

/** One answer of a question: a bordered radio row, the default tagged. */
function Choice({
  name,
  checked,
  onSelect,
  title,
  hint,
  isDefault,
  disabled,
  testId,
}: ChoiceProps) {
  const { t } = useTranslation('tests');
  return (
    <label
      className={classNames(
        'flex cursor-pointer items-start gap-3 rounded-lg border p-3 transition-colors',
        checked
          ? 'border-primary bg-primary/5'
          : 'border-base-300 hover:border-base-content/30',
        disabled && 'cursor-not-allowed opacity-60'
      )}
      data-testid={testId}
    >
      <input
        type="radio"
        className="radio radio-sm radio-primary mt-0.5"
        name={name}
        checked={checked}
        disabled={disabled}
        onChange={onSelect}
        aria-label={title}
      />
      <span className="min-w-0">
        <span className="flex flex-wrap items-center gap-2 font-medium">
          {title}
          {isDefault && (
            <span className="badge badge-ghost badge-sm font-normal">
              {t('new-default-tag')}
            </span>
          )}
        </span>
        {hint && (
          <span className="mt-0.5 block text-sm text-base-content/70">
            {hint}
          </span>
        )}
      </span>
    </label>
  );
}

/**
 * `/w/{slug}/tests/new` (Issue #165, contract §3.2): at most three
 * questions -- goal, audience, scale -- each with a default, and a
 * one-click "按默认开始". The product comes from the product card's
 * one-shot handoff (or, for a deep link, an opaque `?product=` that is
 * read once and stripped from the address bar), never typed.
 */
export function NewTestFlow() {
  const { t } = useTranslation('tests');
  const router = useRouter();
  const { current } = useWorkspace();
  const slug = current.slug;
  const gateway = useDomainGateway();
  const directory = useProductDirectory();
  const { defaults } = useGenerationDefaults();

  const [productId, setProductId] = useState<string | null>(null);
  const [resolvedSlug, setResolvedSlug] = useState<string | null>(null);
  const resolved = resolvedSlug === slug;
  const [picking, setPicking] = useState(false);
  const [answers, setAnswers] = useState<ThreeAnswers>(defaultAnswers(0));
  const [audienceTouched, setAudienceTouched] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [problemCode, setProblemCode] = useState<string | null>(null);
  const submittingRef = useRef(false);
  const restoredAnswersRef = useRef(false);

  // Which product: the card's handoff, a deep link's `?product=`, or what
  // this tab chose before a refresh.
  useEffect(() => {
    if (!router.isReady) return;
    const query = router.query.product;
    const fromQuery = typeof query === 'string' && query ? query : null;
    const setup = recallNewTestSetup(slug);
    const chosen =
      takeProductHandoff('new-test', slug) ??
      fromQuery ??
      recallNewTestProduct(slug) ??
      setup?.productId ??
      null;
    if (setup && setup.productId === chosen) {
      restoredAnswersRef.current = true;
      setAnswers(setup.answers);
      setAudienceTouched(true);
    } else {
      restoredAnswersRef.current = false;
      setAnswers(defaultAnswers(0));
      setAudienceTouched(false);
    }
    if (chosen) rememberNewTestProduct(slug, chosen);
    setProductId(chosen);
    setResolvedSlug(slug);
    if (fromQuery) {
      void router.replace(`/w/${slug}/tests/new`, undefined, {
        shallow: true,
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [router.isReady, slug]);

  useEffect(() => {
    if (resolved && productId) {
      rememberNewTestSetup(slug, { productId, answers });
    }
  }, [resolved, productId, answers, slug]);

  const ready = directory.products.filter((item) => item.ready);
  const product = directory.find(productId);
  const productUsable = Boolean(product?.ready);

  // Only one product can be tested: take it.
  useEffect(() => {
    if (!resolved || directory.isLoading) return;
    if ((!productId || (product && !product.ready)) && ready.length === 1) {
      setProductId(ready[0].product_id);
      rememberNewTestProduct(slug, ready[0].product_id);
    }
  }, [resolved, directory.isLoading, productId, product, ready, slug]);

  const entryId = product?.entry_id ?? '';
  const entry = useDomainQuery(
    'get_library_entry',
    { entry_id: entryId },
    { enabled: Boolean(entryId) }
  );
  const cards = useMemo(() => {
    const data = entry.data as
      | {
          current_version_id?: string | null;
          versions?: { id: string; content?: Record<string, unknown> }[];
        }
      | undefined;
    const version = data?.versions?.find(
      (item) => item.id === data.current_version_id
    );
    return audienceCardsFromContent(version?.content);
  }, [entry.data]);

  // The audience default follows the product's cards once they load.
  useEffect(() => {
    if (audienceTouched || restoredAnswersRef.current) return;
    setAnswers((prev) => ({
      ...prev,
      audienceCardIndex: cards.length > 0 ? 0 : null,
    }));
  }, [cards.length, audienceTouched]);

  function update(patch: Partial<ThreeAnswers>) {
    setProblemCode(null);
    setAnswers((prev) => ({ ...prev, ...patch }));
  }

  async function start(useDefaults: boolean) {
    if (!productId || submittingRef.current) return;
    const chosen = useDefaults ? defaultAnswers(cards.length) : answers;
    if (chosen.goal === 'optimize' && chosen.seedAssetVersionIds.length === 0) {
      setProblemCode('seed_assets_required');
      return;
    }
    submittingRef.current = true;
    setSubmitting(true);
    setProblemCode(null);
    const result = await gateway.createTestPlan({
      workspaceSlug: slug,
      actionInstanceId: newActionId(),
      body: buildCreatePayload(productId, chosen),
    });
    if (result.kind === 'accepted') {
      emitTestPlanEvent({
        event: 'test_plan_started',
        goal: chosen.goal,
        scale: chosen.scale,
        usedDefaults: useDefaults,
        replayed: result.data.replayed,
      });
      rememberNewTestProduct(slug, null);
      rememberNewTestSetup(slug, null);
      await router.push(`/w/${slug}/tests/${result.data.plan_id}`);
      return;
    }
    const code =
      result.kind === 'problem'
        ? result.problem.code || 'unknown'
        : 'command_unconfirmed';
    setProblemCode(code);
    emitTestPlanEvent({
      event: 'test_plan_failed',
      step: 'create',
      failureCode: code,
    });
    submittingRef.current = false;
    setSubmitting(false);
  }

  const productName = product?.name?.trim() || t('unnamed-product');
  const firstAudience = cards[0]?.who;
  const quickSummary = firstAudience
    ? t('new-quick-summary', {
        goal: t('goal-explore'),
        audience: firstAudience,
        scale: t('scale-sample'),
      })
    : t('new-quick-summary-no-audience', {
        goal: t('goal-explore'),
        scale: t('scale-sample'),
      });
  const showPicker = resolved && (picking || !product || !productUsable);
  const canStart = Boolean(productId && productUsable) && !submitting;
  const seedsMissing =
    answers.goal === 'optimize' && answers.seedAssetVersionIds.length === 0;

  return (
    <div className="mx-auto max-w-3xl space-y-6 pb-12">
      <div>
        <Link
          href={`/w/${slug}/products`}
          className="link-hover link inline-flex items-center gap-1 text-sm text-base-content/70"
        >
          <ArrowLeftIcon className="h-4 w-4" aria-hidden="true" />
          {t('new-back')}
        </Link>
        <h1 className="mt-2 text-2xl font-bold outline-none">
          {t('new-title')}
        </h1>
        <p className="mt-1 max-w-prose text-sm text-base-content/70">
          {t('new-intro')}
        </p>
      </div>

      <GenerationDefaultsBanner />

      {product && !picking && (
        <div className="flex items-center gap-3" data-testid="new-test-product">
          <ProductThumbnail
            referenceVersionId={product.primary_reference_version_id}
            name={productName}
            className="h-14 w-14 shrink-0 rounded-lg border border-base-300"
          />
          <div className="min-w-0 flex-1">
            <p className="truncate font-semibold">{productName}</p>
            {product.variant_label && (
              <p className="truncate text-sm text-base-content/60">
                {product.variant_label}
              </p>
            )}
          </div>
          {directory.products.length > 1 && (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => setPicking(true)}
            >
              {t('new-product-change')}
            </button>
          )}
        </div>
      )}

      {product && !product.ready && (
        <div className="alert alert-warning text-sm" role="alert">
          <span>{t('new-product-not-ready')}</span>
        </div>
      )}

      {showPicker && (
        <div className="rounded-box border border-base-300 p-4">
          <ProductPicker
            id="new-test-product"
            value={productId ?? ''}
            onChange={(id) => {
              setProductId(id);
              rememberNewTestProduct(slug, id);
              setPicking(false);
              restoredAnswersRef.current = false;
              setAudienceTouched(false);
              update({
                audienceCardIndex: null,
                audienceText: '',
                seedAssetVersionIds: [],
              });
            }}
          />
        </div>
      )}

      {problemCode && (
        <PlanProblemAlert
          code={problemCode}
          wrapKey="new-problem"
          onDismiss={() => setProblemCode(null)}
        />
      )}

      <section
        className="flex flex-wrap items-center justify-between gap-4 rounded-box border-2 border-primary/40 bg-primary/5 p-4"
        aria-labelledby="quick-start-title"
      >
        <div className="min-w-0 max-w-prose">
          <h2 id="quick-start-title" className="font-semibold">
            {t('new-quick-title')}
          </h2>
          <p className="mt-0.5 text-sm text-base-content/80">{quickSummary}</p>
        </div>
        <button
          type="button"
          className="btn btn-primary"
          disabled={!canStart}
          onClick={() => void start(true)}
        >
          {submitting ? t('new-starting') : t('new-quick-start')}
        </button>
      </section>

      <fieldset className="space-y-3" disabled={submitting}>
        <legend className="mb-3 text-lg font-semibold">{t('q-goal')}</legend>
        <div className="grid gap-2 sm:grid-cols-2">
          {GOALS.map((goal) => (
            <Choice
              key={goal}
              name="test-goal"
              checked={answers.goal === goal}
              onSelect={() => update({ goal })}
              title={t(`goal-${goal}`)}
              hint={t(`q-goal-${goal}-hint`)}
              isDefault={goal === 'explore'}
            />
          ))}
        </div>
        {answers.goal === 'optimize' && productId && (
          <div className="rounded-lg border border-base-300 p-3">
            <SeedAssetPicker
              productId={productId}
              value={answers.seedAssetVersionIds}
              onChange={(ids) => update({ seedAssetVersionIds: ids })}
              disabled={submitting}
            />
          </div>
        )}
      </fieldset>

      <fieldset className="space-y-3" disabled={submitting}>
        <legend className="mb-3 text-lg font-semibold">
          {t('q-audience')}
        </legend>
        {entry.isLoading && cards.length === 0 && (
          <p className="text-sm text-base-content/60" role="status">
            {t('q-audience-loading')}
          </p>
        )}
        {cards.length > 0 && (
          <div className="grid gap-2">
            {cards.map((card, index) => (
              <Choice
                key={`${index}-${card.who}`}
                name="test-audience"
                checked={answers.audienceCardIndex === index}
                onSelect={() => {
                  setAudienceTouched(true);
                  update({ audienceCardIndex: index });
                }}
                title={card.who}
                hint={card.why || card.pain || undefined}
                isDefault={index === 0}
              />
            ))}
            <Choice
              name="test-audience"
              checked={answers.audienceCardIndex === null}
              onSelect={() => {
                setAudienceTouched(true);
                update({ audienceCardIndex: null });
              }}
              title={t('q-audience-custom')}
            />
          </div>
        )}
        {!entry.isLoading && cards.length === 0 && (
          <p className="text-sm text-base-content/70">{t('q-audience-none')}</p>
        )}
        {answers.audienceCardIndex === null && (
          <label className="form-control">
            <span className="label-text mb-1 text-sm">
              {t('q-audience-custom-label')}
            </span>
            <textarea
              className="textarea textarea-bordered"
              rows={2}
              maxLength={500}
              value={answers.audienceText}
              placeholder={t('q-audience-custom-placeholder')}
              onChange={(event) => {
                setAudienceTouched(true);
                update({ audienceText: event.target.value });
              }}
            />
          </label>
        )}
      </fieldset>

      <fieldset className="space-y-3" disabled={submitting}>
        <legend className="mb-3 text-lg font-semibold">{t('q-scale')}</legend>
        <div className="grid gap-2 sm:grid-cols-3">
          {PLAN_SCALES.map((scale) => {
            const thumb = ruleOfThumbImages(scale, defaults);
            return (
              <Choice
                key={scale}
                name="test-scale"
                checked={answers.scale === scale}
                onSelect={() => update({ scale })}
                title={t(`scale-${scale}`)}
                hint={t(`q-scale-${scale}-hint`, {
                  combinations: thumb.maxCombinations,
                  variants: thumb.variants,
                  images: thumb.maxImages,
                })}
                isDefault={scale === 'sample'}
              />
            );
          })}
        </div>
        <p className="text-xs text-base-content/60">{t('q-scale-note')}</p>
      </fieldset>

      <div className="flex flex-wrap items-center justify-end gap-3 border-t border-base-300 pt-4">
        {seedsMissing && (
          <span className="text-sm text-warning-content">
            {t('q-seeds-required')}
          </span>
        )}
        <button
          type="button"
          className="btn btn-outline"
          disabled={!canStart || seedsMissing}
          onClick={() => void start(false)}
        >
          {submitting ? t('new-starting') : t('new-start')}
        </button>
      </div>
    </div>
  );
}
