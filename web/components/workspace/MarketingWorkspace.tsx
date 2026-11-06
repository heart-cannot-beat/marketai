import { useTranslation } from 'next-i18next';
import { strategyLanguageFor } from '@/lib/i18n/locales';
import { useEffect, useId, useRef, useState } from 'react';
import useSWR, { useSWRConfig } from 'swr';
import useSWRInfinite from 'swr/infinite';
import { MarketingCallRecovery } from './MarketingCallRecovery';
import { useRouter } from 'next/router';
import { useWorkspace } from './WorkspaceProvider';
import {
  MarketingProductReferences,
  type ProductReferences,
} from './MarketingProductReferences';
import { MarketingExecution } from './MarketingExecution';
import {
  MarketingExpressionDetails,
  MarketingPlanReview,
  nextMechanism,
  planBody,
  planDetailsMissing,
  planReviewBlocked,
  type ExpressionDetails,
} from './MarketingPlanReview';
import { MarketingComparisonView } from './MarketingContinuation';
import { MarketingBrandStyle, styleBody } from './MarketingBrandStyle';
import { LEGACY_EXECUTIONS, executionSummary } from './marketing-executions';
import { GenerationComposer } from './GenerationComposer';
import {
  MAIN_BUTTON,
  MarketingFocusBar,
  type FocusAction,
  type MoreAction,
} from './MarketingFocusBar';
import {
  MainActionProvider,
  STEP_TARGET,
  goTo,
  type RoundFocus,
} from './marketing-focus';
import { ProductPicker } from '@/components/products/ProductPicker';
import { CompetitorsArea } from '@/components/competitors/CompetitorsArea';
import { takeProductHandoff } from '@/lib/products/handoff';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { ReferenceThumbnail } from '@/components/library/references/ReferenceThumbnail';
import {
  DomainProblemError,
  errorCodeMessage,
  errorMessageOf,
  fieldErrorMessage,
  fieldPath,
  type DomainProblem,
} from '@/lib/i18n/domain-errors';
import { CsrfTokenFetchError } from '@/lib/domain-client/csrf';
import { transientRetriesExhausted } from '@/lib/domain-client/transient-problems';
import type { components } from '@/lib/domain-client/generated/domain-v1';

type Project = components['schemas']['MarketingProjectDetail'];
type Understanding = components['schemas']['Understanding'];
type Plan = NonNullable<Project['plan']>;
const INPUT =
  'w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm focus:border-gray-600 focus:outline-none focus:ring-2 focus:ring-gray-200';
const BUTTON = MAIN_BUTTON;
const SECONDARY =
  'rounded-lg border border-gray-300 px-4 py-2 text-sm disabled:opacity-40';
const PRESSED =
  'rounded-lg border border-gray-900 bg-gray-100 px-4 py-2 text-sm font-medium disabled:opacity-40';
const SIGN_IN_REQUIRED = 'marketing-sign-in-required';

function dataOf<T>(result: {
  kind: string;
  data?: T;
  problem?: DomainProblem;
  status?: number;
}): T {
  if (result.kind === 'problem' && result.status === 401)
    throw new Error(SIGN_IN_REQUIRED);
  if (
    result.kind === 'problem' &&
    [
      'copy_language_mismatch',
      'image_brief_language_mismatch',
      'ad_language_confirmation_required',
      'planning_language_mismatch',
    ].includes(result.problem?.code ?? '')
  )
    throw new Error(`marketing-${result.problem?.code}`);
  if (result.kind === 'problem') throw new DomainProblemError(result.problem);
  if (result.kind === 'reconciliation_required')
    throw new Error(
      'marketing-the-previous-request-needs-reconciliation-refresh-to-check-saved-tasks'
    );
  if (result.data === undefined)
    throw new Error('marketing-unable-to-load-the-result-please-try-again');
  return result.data;
}

function Field({
  label,
  value,
  onChange,
  rows = 2,
  errors = [],
  path,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  rows?: number;
  errors?: string[];
  path?: string;
}) {
  const id = useId();
  return (
    <div data-validation-field={path} className="block space-y-1.5 text-sm">
      <label htmlFor={id} className="font-medium text-gray-700">
        {label}
      </label>
      <textarea
        id={id}
        className={INPUT}
        aria-invalid={errors.length > 0 || undefined}
        aria-describedby={errors.length ? `${id}-errors` : undefined}
        rows={rows}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
      {errors.length > 0 && (
        <p id={`${id}-errors`} className="text-sm text-red-700">
          {errors.join(' ')}
        </p>
      )}
    </div>
  );
}

export function MarketingWorkspace() {
  const { current } = useWorkspace();
  return <MarketingWorkspaceContent key={current.workspaceId} />;
}

function MarketingWorkspaceContent() {
  const { t } = useTranslation('common');
  const { current } = useWorkspace();
  const [roundState, setRoundState] = useState<string | null>(null);
  const gateway = useDomainGateway();
  const { mutate: revalidate } = useSWRConfig();
  const router = useRouter();
  const selectedId =
    typeof router.query.test === 'string' ? router.query.test : null;
  const [source, setSource] = useState('');
  const [revisedSource, setRevisedSource] = useState('');
  const [selectedProduct, setSelectedProduct] = useState('');
  const [understanding, setUnderstanding] = useState<Understanding | null>(
    null
  );
  const [plan, setPlan] = useState<Plan | null>(null);
  const [adLanguage, setAdLanguage] = useState('en-US');
  const adLanguageHint = useId();
  const [mock, setMock] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<
    NonNullable<DomainProblem['field_errors']>
  >([]);
  const formRef = useRef<HTMLDivElement>(null);
  const [unmappedFields, setUnmappedFields] = useState<
    NonNullable<DomainProblem['field_errors']>
  >([]);
  const fieldMessages = (path: string) =>
    fieldErrors
      .filter((error) => {
        const field = fieldPath(error.field);
        return field === path || field.startsWith(`${path}.`);
      })
      .map((error) => fieldErrorMessage(t, error));
  useEffect(() => {
    const nodes = Array.from(
      formRef.current?.querySelectorAll('[data-validation-field]') ?? []
    );
    const paths = nodes.map(
      (node) => node.getAttribute('data-validation-field')!
    );
    for (const node of nodes) {
      const path = node.getAttribute('data-validation-field')!;
      if (
        !fieldErrors.some(
          (error) =>
            fieldPath(error.field) === path ||
            fieldPath(error.field).startsWith(`${path}.`)
        )
      )
        continue;
      let parent = node.parentElement;
      while (parent) {
        if (parent instanceof HTMLDetailsElement) parent.open = true;
        parent = parent.parentElement;
      }
    }
    setUnmappedFields(
      fieldErrors.filter(
        (error) =>
          !paths.some(
            (path) =>
              fieldPath(error.field) === path ||
              fieldPath(error.field).startsWith(`${path}.`)
          )
      )
    );
  }, [fieldErrors]);
  const [activeDraft, setActiveDraft] = useState<string | null>(null);
  // The round's next step as MarketingExecution reads it from the server.
  const [roundFocus, setRoundFocus] = useState<RoundFocus | null>(null);
  // The plan revision the current round froze, as MarketingExecution reads
  // it: `undefined` until read, `null` without a round or a read (#310).
  const [roundPlanRevision, setRoundPlanRevision] = useState<
    number | null | undefined
  >();
  const [openRequest, setOpenRequest] = useState<{
    key: string;
    nonce: number;
  } | null>(null);
  const [startingProduct, setStartingProduct] = useState(false);
  const [provider, setProvider] = useState('');
  const [productReferences, setProductReferences] = useState<ProductReferences>(
    { reference_version_ids: [], source: '', confirms_real_product: false }
  );
  // Validation paths refer to the submitted document. Editing it can shift
  // nested array indexes, so do not attach stale errors to different fields.
  useEffect(() => {
    setFieldErrors([]);
    setError(null);
  }, [plan, understanding, source, revisedSource, productReferences]);

  const retry = useRef<(() => Promise<void>) | null>(null);
  const pending = useRef<{ identity: string; action: string } | null>(null);
  const saving = useRef(false);

  const capabilities = useSWR(
    ['marketing-capabilities', current.workspaceId],
    async () =>
      dataOf(
        await gateway.getMarketingCapabilities({
          workspaceSlug: current.slug,
          includeReporting: true,
        })
      )
  );
  const generation = useSWR(
    ['marketing-generation-capabilities', current.workspaceId],
    async () =>
      dataOf(
        await gateway.getGenerationCapabilities({ workspaceSlug: current.slug })
      )
  );
  const search = typeof router.query.q === 'string' ? router.query.q : '';
  const [searchDraft, setSearchDraft] = useState(search);
  useEffect(() => setSearchDraft(search), [search]);
  const projects = useSWRInfinite<
    components['schemas']['MarketingProjectPage']
  >(
    (index, previous) =>
      index > 0 && !previous?.next_cursor
        ? null
        : [
            'marketing-projects',
            current.workspaceId,
            search,
            index === 0 ? undefined : previous?.next_cursor,
          ],
    async ([, , q, cursor]: [string, string, string, string | undefined]) =>
      dataOf(
        await gateway.listMarketingProjects({
          workspaceSlug: current.slug,
          limit: 50,
          q,
          cursor,
        })
      )
  );
  const projectItems = Array.from(
    new Map(
      (projects.data ?? [])
        .flatMap((page) => page.items)
        .map((item) => [item.id, item])
    ).values()
  );
  const hasMoreProjects =
    !!projects.data?.[projects.data.length - 1]?.next_cursor;
  const project = useSWR(
    selectedId ? ['marketing-project', current.workspaceId, selectedId] : null,
    async () =>
      dataOf(
        await gateway.getMarketingProject({
          includeLanguages: true,
          includePlanReview: true,
          includeBrandStyle: true,
          includeExecutionPlan: true,
          includeRecovery: true,
          workspaceSlug: current.slug,
          projectId: selectedId!,
        })
      ),
    {
      refreshInterval: (latest) =>
        latest?.latest_call &&
        ['queued', 'running'].includes(latest.latest_call.state)
          ? 1500
          : 0,
    }
  );
  const document = project.data;
  const readError = project.error || capabilities.error || projects.error;
  // The reads stopped retrying a transient access problem (#309).
  const readsStalled = transientRetriesExhausted(readError);
  const running =
    !!document?.latest_call &&
    ['queued', 'running', 'uncertain'].includes(document.latest_call.state);
  const locked = busy || running;
  const paid = capabilities.data?.paid;
  const languageAvailable = document?.language_support === 'v1';
  const reviewAvailable = document?.plan_review_support === 'v1';
  const brandStyleAvailable = document?.brand_style_support === 'v1';
  const mayPlan = languageAvailable && (mock || !!paid?.available);
  // "Draft again · Up to $0.05 per call": the action, then what it may cost.
  const withCost = (label: string) =>
    `${label.replace(/\s*·\s*$/, '')} · ${costLabel}`;
  const costLabel = mock
    ? t('marketing-controlled-mock-free-example')
    : paid
      ? t('marketing-cost-ceiling', { amount: paid.maximum_cost })
      : t('marketing-loading-planning-configuration');
  const executions = plan && executionSummary(plan);
  const executionsLabel =
    executions &&
    t(
      executions.executions === null
        ? 'marketing-executions-cost-summary-mixed'
        : executions.executions === 1
          ? 'marketing-executions-cost-summary-one'
          : 'marketing-executions-cost-summary',
      executions
    );
  const providerOptions = generation.data?.provider_options ?? [];
  const chosenProvider =
    providerOptions.find((p) => p.id === provider) ??
    providerOptions.find((p) => p.id !== 'mock' && p.available) ??
    providerOptions.find((p) => p.id === 'mock');

  useEffect(() => {
    setUnderstanding(document?.understanding ?? null);
    setPlan(document?.plan ?? null);
    setAdLanguage(document?.plan?.ad_language ?? 'en-US');
    setProductReferences({
      reference_version_ids:
        document?.product_references?.reference_version_ids ?? [],
      source: document?.product_references?.source ?? '',
      confirms_real_product:
        document?.product_references?.confirms_real_product ?? false,
    });
    setActiveDraft((selected) =>
      selected && document?.generation_draft_ids.includes(selected)
        ? selected
        : null
    );
    setRevisedSource(document?.source_text ?? '');
    // Polling the same revision must not overwrite local edits.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [document?.id, document?.revision, document?.language_support]);

  async function selectProject(id: string | null) {
    setError(null);
    setFieldErrors([]);
    pending.current = null;
    retry.current = null;
    await router.replace(
      {
        pathname: router.pathname,
        query: {
          slug: current.slug,
          ...(search ? { q: search } : {}),
          ...(id ? { test: id } : {}),
        },
      },
      undefined,
      { shallow: true }
    );
  }

  async function perform(
    identity: string,
    work: (action: string) => Promise<void>
  ) {
    if (saving.current) return;
    saving.current = true;
    setBusy(true);
    setError(null);
    setFieldErrors([]);
    if (pending.current?.identity !== identity)
      pending.current = { identity, action: crypto.randomUUID() };
    const action = pending.current.action;
    const run = () => perform(identity, work);
    retry.current = run;
    try {
      await work(action);
      pending.current = null;
      retry.current = null;
      await projects.mutate();
    } catch (e) {
      setError(
        e instanceof CsrfTokenFetchError && e.status === 401
          ? t(SIGN_IN_REQUIRED)
          : e instanceof DomainProblemError && e.problem.field_errors?.length
            ? // Each field's reason is shown beside it; the summary stays general.
              errorCodeMessage(t, e.problem.code)
            : errorMessageOf(t, e)
      );
      setFieldErrors(
        e instanceof DomainProblemError ? (e.problem.field_errors ?? []) : []
      );
    } finally {
      saving.current = false;
      setBusy(false);
    }
  }

  // "Start test" on a product card hands its product over once (no id in
  // the URL); the test starts from the confirmed card and its photos.
  const handoffTaken = useRef(false);
  useEffect(() => {
    if (selectedId || handoffTaken.current) return;
    handoffTaken.current = true;
    const productId = takeProductHandoff('marketing-test', current.slug);
    if (!productId) return;
    setSelectedProduct(productId);
    setStartingProduct(true);
    void create({ source_text: '', product_id: productId }).finally(() =>
      setStartingProduct(false)
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function create(
    body = { source_text: source, product_id: selectedProduct || null }
  ) {
    await perform(`create:${JSON.stringify(body)}`, async (action) => {
      const created = dataOf(
        await gateway.createMarketingProject({
          includeLanguages: true,
          includePlanReview: true,
          includeBrandStyle: true,
          includeExecutionPlan: true,
          workspaceSlug: current.slug,
          actionInstanceId: action,
          body,
        })
      );
      await selectProject(created.id);
    });
  }

  function planningBody(
    kind: 'understanding' | 'plan',
    next?: components['schemas']['app__marketing__api__PlanRequest']
  ) {
    return {
      kind,
      strategy_language: strategyLanguageFor(router.locale),
      direction: kind === 'plan' ? plan?.stage : undefined,
      execution_mode: mock ? ('mock' as const) : ('byok' as const),
      quote_hash: paid?.quote_hash ?? '',
      decision_question: '',
      ...next,
      ad_language: next?.ad_language ?? adLanguage,
    };
  }

  async function askAI(
    kind: 'understanding' | 'plan',
    next?: components['schemas']['app__marketing__api__PlanRequest']
  ) {
    if (!document || !languageAvailable) return;
    if (next) setMock(next.execution_mode === 'mock');
    const body = planningBody(kind, next);
    await perform(
      `ai:${document.id}:${document.revision}:${JSON.stringify(body)}`,
      async (action) => {
        dataOf(
          await gateway.startMarketingPlanning({
            workspaceSlug: current.slug,
            projectId: document.id,
            expectedRevision: document.revision,
            actionInstanceId: action,
            body,
          })
        );
        await project.mutate();
      }
    );
  }

  // A confirmation may carry the one step that always follows it (J-32):
  // confirming the understanding drafts the plan (its cost is on the
  // button), confirming the plan prepares the free generation drafts. Each
  // command keeps its own idempotency key, so a retry replays both.
  async function save(
    command:
      | 'save_understanding'
      | 'confirm_understanding'
      | 'save_plan'
      | 'confirm_plan',
    then?: 'plan' | 'prepare'
  ) {
    if (!document) return;
    if (command.includes('plan') && !languageAvailable) {
      setError(t('marketing-language-feature-unavailable'));
      return;
    }
    if (command.includes('plan') && !reviewAvailable) {
      setError(t('marketing-plan-review-feature-unavailable'));
      return;
    }
    if (command.includes('plan') && !brandStyleAvailable) {
      setError(t('marketing-brand-style-feature-unavailable'));
      return;
    }
    const body = command.includes('understanding')
      ? { command, understanding }
      : {
          command,
          plan: plan && {
            ...planBody(plan),
            ...styleBody(plan),
            ad_language: adLanguage,
            language_confirmation_required:
              command === 'confirm_plan'
                ? false
                : plan.language_confirmation_required,
          },
        };
    const draftPlan = then === 'plan' && mayPlan ? planningBody('plan') : null;
    const drafts =
      then === 'prepare' && !document.generation_draft_ids.length
        ? prepareBody()
        : null;
    await perform(
      `save:${document.id}:${document.revision}:${JSON.stringify(body)}:${JSON.stringify(draftPlan ?? drafts)}`,
      async (action) => {
        const updated = dataOf(
          await gateway.editMarketingProject({
            includeLanguages: true,
            includePlanReview: true,
            includeBrandStyle: true,
            includeExecutionPlan: true,
            workspaceSlug: current.slug,
            projectId: document.id,
            expectedRevision: document.revision,
            actionInstanceId: draftPlan || drafts ? `${action}:save` : action,
            body,
          })
        );
        await project.mutate(updated, false);
        if (draftPlan) {
          dataOf(
            await gateway.startMarketingPlanning({
              workspaceSlug: current.slug,
              projectId: updated.id,
              expectedRevision: updated.revision,
              actionInstanceId: `${action}:plan`,
              body: draftPlan,
            })
          );
          await project.mutate();
        }
        if (
          drafts &&
          updated.state === 'plan_confirmed' &&
          !updated.plan?.language_confirmation_required
        ) {
          const prepared = dataOf(
            await gateway.prepareMarketingGeneration({
              includeLanguages: true,
              includePlanReview: true,
              includeBrandStyle: true,
              includeExecutionPlan: true,
              workspaceSlug: current.slug,
              projectId: updated.id,
              expectedRevision: updated.revision,
              actionInstanceId: `${action}:prepare`,
              body: drafts,
            })
          );
          await project.mutate(prepared, false);
          setActiveDraft(prepared.generation_draft_ids[0] ?? null);
        }
      }
    );
  }

  async function reviseSource() {
    if (!document) return;
    const body = {
      command: 'revise_source' as const,
      source_text: revisedSource,
    };
    await perform(
      `source:${document.id}:${document.revision}:${revisedSource}`,
      async (action) => {
        const updated = dataOf(
          await gateway.editMarketingProject({
            includeLanguages: true,
            includePlanReview: true,
            includeBrandStyle: true,
            includeExecutionPlan: true,
            workspaceSlug: current.slug,
            projectId: document.id,
            expectedRevision: document.revision,
            actionInstanceId: action,
            body,
          })
        );
        await project.mutate(updated, false);
      }
    );
  }

  const photosReady =
    !productReferences.reference_version_ids.length ||
    (productReferences.confirms_real_product &&
      !!productReferences.source.trim());
  function prepareBody() {
    if (!chosenProvider || !photosReady) return null;
    return {
      provider: chosenProvider.id as 'gemini' | 'openai' | 'seedream' | 'mock',
      model: chosenProvider.default_model,
      aspect_ratio: '4:5' as const,
      image_size: '1K' as const,
      product_references: productReferences,
    };
  }

  async function prepare() {
    const body = prepareBody();
    if (!document || !body || !languageAvailable) return;
    await perform(
      `prepare:${document.id}:${document.revision}:${JSON.stringify(body)}`,
      async (action) => {
        const updated = dataOf(
          await gateway.prepareMarketingGeneration({
            includeLanguages: true,
            includePlanReview: true,
            includeBrandStyle: true,
            includeExecutionPlan: true,
            workspaceSlug: current.slug,
            projectId: document.id,
            expectedRevision: document.revision,
            actionInstanceId: action,
            body,
          })
        );
        await project.mutate(updated, false);
        setActiveDraft(updated.generation_draft_ids[0] ?? null);
      }
    );
  }

  function updateUnderstanding<K extends keyof Understanding>(
    key: K,
    value: Understanding[K]
  ) {
    if (understanding) setUnderstanding({ ...understanding, [key]: value });
  }
  function updatePlan<K extends keyof Plan>(key: K, value: Plan[K]) {
    if (plan) setPlan({ ...plan, [key]: value });
  }

  function updateExpression(
    expressionId: string,
    field: 'name' | 'description',
    value: string
  ) {
    updateSharedExpression(expressionId, { [field]: value });
  }
  // Shared expressions keep one definition: name, telling, mechanism and evidence.
  function updateSharedExpression(
    expressionId: string,
    patch: Partial<ExpressionDetails> & { name?: string; description?: string }
  ) {
    if (!plan) return;
    updatePlan(
      'angles',
      plan.angles.map((angle) => ({
        ...angle,
        expressions: angle.expressions.map((expression) =>
          expression.id === expressionId
            ? { ...expression, ...patch }
            : expression
        ),
      }))
    );
  }

  function removeExpression(angleId: string, expressionId: string) {
    if (!plan) return;
    const angle = plan.angles.find((a) => a.id === angleId);
    if (!angle || angle.expressions.length <= 1) return;
    const combinations = plan.combinations.filter(
      (c) => c.angle_id !== angleId || c.expression_id !== expressionId
    );
    const kept = new Set(combinations.map((c) => c.id));
    const groups = plan.groups
      .map((g) => ({
        ...g,
        combination_ids: g.combination_ids.filter((id) => kept.has(id)),
      }))
      .filter((g) => g.combination_ids.length);
    if (plan.comparison_kind !== 'business_retest' && groups.length < 2) {
      setError(t('marketing-keep-two-comparison-groups'));
      return;
    }
    const angles = plan.angles.map((a) =>
      a.id === angleId
        ? {
            ...a,
            expressions: a.expressions.filter((e) => e.id !== expressionId),
          }
        : a
    );
    const limited =
      plan.stage === 'screen_angles' &&
      angles.some(
        (a) =>
          new Set(
            combinations
              .filter((c) => c.angle_id === a.id)
              .map((c) => c.expression_id)
          ).size < 2
      );
    const note = t('marketing-limited-expression-coverage-note');
    setPlan({
      ...plan,
      angles,
      combinations,
      groups,
      limited_expression_coverage:
        !!plan.limited_expression_coverage || limited,
      limitations:
        limited && !plan.limitations.includes(note)
          ? [...plan.limitations, note]
          : plan.limitations,
    });
  }

  function withoutAngle(angleId: string) {
    if (!plan) return null;
    const combinations = plan.combinations.filter(
      (c) => c.angle_id !== angleId
    );
    const kept = new Set(combinations.map((c) => c.id));
    const groups = plan.groups
      .map((g) => ({
        ...g,
        combination_ids: g.combination_ids.filter((id) => kept.has(id)),
      }))
      .filter((g) => g.combination_ids.length);
    if (
      !combinations.length ||
      (plan.comparison_kind !== 'business_retest' && groups.length < 2)
    )
      return null;
    return {
      ...plan,
      angles: plan.angles.filter((a) => a.id !== angleId),
      combinations,
      groups,
    };
  }

  function addExpression(angleId: string, sharedId?: string) {
    if (!plan) return;
    const angle = plan.angles.find((a) => a.id === angleId)!;
    const shared = sharedId
      ? plan.angles.flatMap((a) => a.expressions).find((e) => e.id === sharedId)
      : undefined;
    if (
      (sharedId && !shared) ||
      angle.expressions.some((e) => e.id === sharedId) ||
      angle.expressions.length >= 8 ||
      plan.combinations.length >= 30
    )
      return;
    const existing = new Set(
      plan.combinations.filter((c) => c.angle_id === angleId).map((c) => c.id)
    );
    const group = plan.groups.find((g) =>
      g.combination_ids.some((id) => existing.has(id))
    );
    if (!group) {
      setError(t('marketing-confirm-a-test-group-for-this-angle-first'));
      return;
    }
    const suffix = crypto.randomUUID().replaceAll('-', '').slice(0, 12);
    const expressionId = shared?.id || `e_${suffix}`,
      combinationId = `c_${suffix}`;
    setPlan({
      ...plan,
      angles: plan.angles.map((a) =>
        a.id === angleId
          ? {
              ...a,
              expressions: [
                ...a.expressions,
                {
                  id: expressionId,
                  name:
                    shared?.name || t('marketing-new-expression-suggestion'),
                  description:
                    shared?.description ||
                    t(
                      'marketing-describe-how-this-expression-communicates-the-purchase-proposition'
                    ),
                  fit_reason: t('marketing-explain-why-it-fits-this-angle'),
                  required_evidence: shared?.required_evidence || [],
                  how_to_tell:
                    shared?.how_to_tell ||
                    t(
                      'marketing-explain-how-this-expression-tells-the-proposition'
                    ),
                  visual_mechanism:
                    shared?.visual_mechanism ||
                    nextMechanism(angle.expressions),
                  evidence_basis: shared?.evidence_basis || {
                    fact_ids: [],
                    gap: t('marketing-expression-evidence-gap-default'),
                  },
                },
              ],
            }
          : a
      ),
      combinations: [
        ...plan.combinations,
        {
          id: combinationId,
          angle_id: angleId,
          expression_id: expressionId,
          elements: {},
          headline: angle.proposition,
          body_copy: t('marketing-review-the-accompanying-copy'),
          visual_brief: t(
            'marketing-add-the-visual-requirements-for-this-expression'
          ),
          checks: [
            t('marketing-proposition-clear', {
              proposition: angle.proposition,
            }),
            t('marketing-use-only-supported-product-claims'),
          ],
        },
      ],
      groups: plan.groups.map((g) =>
        g.id === group.id
          ? { ...g, combination_ids: [...g.combination_ids, combinationId] }
          : g
      ),
    });
  }
  // A plan confirmed before the advertising language (J-02) or the
  // expression details (#222) reads as still to confirm. Once the current
  // round froze it, the page follows that round instead (#310). Until the
  // workflow is read the plan stays confirmed, so no confirmation flashes up.
  const languagePending = !!document?.plan?.language_confirmation_required;
  const legacyPlan =
    languagePending || (reviewAvailable && planDetailsMissing(document?.plan));
  const stage = !document?.understanding
    ? 0
    : !['understanding_confirmed', 'plan_draft', 'plan_confirmed'].includes(
          document.state
        )
      ? 1
      : document.state !== 'plan_confirmed' ||
          (legacyPlan &&
            roundPlanRevision !== undefined &&
            roundPlanRevision !== document.revision)
        ? 2
        : 3;
  // The server refuses new images for a plan without a confirmed language;
  // confirming one now would detach the round from its plan.
  const roundBlocked =
    stage === 3 &&
    languagePending &&
    (roundFocus?.step === 'generate' || roundFocus?.step === 'revise');
  const duplicated = new Set(
    (plan?.plan_review ?? [])
      .filter((r) => r.source === 'system' && r.kind === 'expression_duplicate')
      .flatMap((r) => r.target_ids ?? [])
  );
  // Combinations whose English image brief predates their expression's last edit (#292).
  const staleBriefs = new Set(
    (plan?.plan_review ?? [])
      .filter((r) => r.source === 'system' && r.kind === 'image_brief_stale')
      .flatMap((r) => r.target_ids ?? [])
  );
  const draftIds = document?.generation_draft_ids ?? [];
  const generationOpen =
    stage === 3 && (!roundState || roundState === 'reviewing');
  const unconfirmedFacts = (understanding?.facts ?? []).filter(
    (f) => f.confirmation !== 'confirmed'
  ).length;
  const prepareAction: FocusAction = {
    label: t('marketing-prepare-generation-drafts-no-charge'),
    run: () => void prepare(),
    disabled: locked || !chosenProvider || !languageAvailable || !photosReady,
  };
  // The round's step is committed in its own section; the bar takes the
  // user there (or opens the image that needs them).
  function stepAction(step: RoundFocus): FocusAction | undefined {
    if (step.step === 'generate') {
      if (!draftIds.length) return prepareAction;
      return {
        label: t('marketing-focus-go-generate'),
        jump: true,
        run: () => {
          if (!activeDraft) setActiveDraft(draftIds[0]);
          requestAnimationFrame(() => goTo('marketing-generation-editor'));
        },
      };
    }
    if (step.step === 'review' || step.step === 'revise') {
      const key = step.executionKey;
      return key
        ? {
            label: t(`marketing-focus-go-${step.step}`),
            jump: true,
            run: () => setOpenRequest({ key, nonce: Date.now() }),
          }
        : undefined;
    }
    const target = STEP_TARGET[step.step];
    return target
      ? {
          label: t(`marketing-focus-go-${step.step}`),
          jump: true,
          run: () => goTo(target),
        }
      : undefined;
  }
  const call = document?.latest_call;
  const failed =
    call?.state === 'failed' ? t('marketing-focus-gap-failed') : '';
  const focus: {
    gap: string;
    action?: FocusAction;
    waiting?: boolean;
  } | null = !document
    ? null
    : running
      ? call?.state === 'uncertain'
        ? {
            gap: t('marketing-focus-gap-uncertain'),
            action: {
              label: t('marketing-focus-go-recovery'),
              jump: true,
              run: () => goTo('marketing-call-recovery'),
            },
          }
        : { gap: t('marketing-focus-gap-running'), waiting: true }
      : stage === 0
        ? {
            gap:
              failed ||
              t(
                mayPlan
                  ? 'marketing-focus-gap-understanding'
                  : 'marketing-focus-gap-ai-unavailable'
              ),
            action: {
              label: withCost(t('marketing-draft-product-understanding')),
              run: () => void askAI('understanding'),
              disabled: locked || !mayPlan,
            },
          }
        : stage === 1
          ? {
              gap:
                failed ||
                (unconfirmedFacts
                  ? t('marketing-focus-gap-facts', { count: unconfirmedFacts })
                  : t('marketing-focus-gap-understanding-ready')),
              action: {
                label: mayPlan
                  ? withCost(
                      t('marketing-confirm-understanding-and-draft-plan')
                    )
                  : t('marketing-confirm-understanding-and-continue'),
                run: () => void save('confirm_understanding', 'plan'),
                disabled: locked || !understanding || unconfirmedFacts > 0,
              },
            }
          : stage === 2 && !plan
            ? {
                gap:
                  failed ||
                  t(
                    mayPlan
                      ? 'marketing-focus-gap-plan'
                      : 'marketing-focus-gap-ai-unavailable'
                  ),
                action: {
                  label: withCost(t('marketing-draft-a-test-plan')),
                  run: () => void askAI('plan'),
                  disabled: locked || !mayPlan,
                },
              }
            : stage === 2 && plan
              ? {
                  gap:
                    failed ||
                    t(
                      plan.language_confirmation_required
                        ? 'marketing-focus-gap-language'
                        : reviewAvailable && planReviewBlocked(plan)
                          ? 'marketing-focus-gap-review-blocked'
                          : 'marketing-focus-gap-plan-ready'
                    ),
                  action: {
                    label: t('marketing-confirm-plan-and-prepare-creatives'),
                    run: () => void save('confirm_plan', 'prepare'),
                    disabled:
                      locked ||
                      !adLanguage ||
                      !languageAvailable ||
                      (reviewAvailable && planReviewBlocked(plan)),
                  },
                }
              : !roundFocus
                ? { gap: t('marketing-focus-gap-loading'), waiting: true }
                : roundBlocked
                  ? { gap: t('marketing-focus-gap-round-predates-language') }
                  : {
                      gap: t(`marketing-focus-gap-${roundFocus.step}`, {
                        count: roundFocus.count,
                      }),
                      action: stepAction(roundFocus),
                      waiting: roundFocus.step === 'wait',
                    };
  const more: MoreAction[] = !document
    ? []
    : [
        {
          label: t('marketing-new-test'),
          run: () => void selectProject(null),
          disabled: busy,
        },
        {
          label: t('marketing-update-product-information'),
          run: () => goTo('marketing-source'),
        },
        ...(stage === 1 && understanding
          ? [
              {
                label: t('marketing-save-changes'),
                run: () => void save('save_understanding'),
                disabled: locked,
              },
              {
                label: withCost(t('marketing-draft-again')),
                run: () => void askAI('understanding'),
                disabled: locked || !mayPlan,
              },
            ]
          : []),
        ...(stage === 2 && !plan
          ? [
              {
                label: t('marketing-edit-product-understanding'),
                run: () => void save('save_understanding'),
                disabled: locked,
              },
            ]
          : []),
        ...(stage === 2 && plan
          ? [
              {
                label: t('marketing-save-changes'),
                run: () => void save('save_plan'),
                disabled: locked,
              },
              {
                label: withCost(t('marketing-draft-again')),
                run: () => void askAI('plan'),
                disabled: locked || !mayPlan,
              },
            ]
          : []),
        ...(generationOpen &&
        !draftIds.length &&
        !languagePending &&
        roundFocus?.step !== 'generate'
          ? [prepareAction]
          : []),
        ...(stage === 3
          ? [
              {
                label: t('marketing-edit-plan-confirmation-required'),
                run: () => void save('save_plan'),
                disabled: locked,
              },
            ]
          : []),
      ];
  const planReview = plan && document && (
    <MarketingPlanReview
      plan={plan}
      savedQuestions={document.plan?.guiding_questions}
      disabled={locked || stage === 3}
      redraftDisabled={locked || !mayPlan}
      costLabel={costLabel}
      messages={fieldMessages}
      onChange={setPlan}
      onSave={() => save('save_plan')}
      onRedraft={() => askAI('plan')}
    />
  );
  // Provider and photo overrides apply when the drafts are prepared, which
  // confirming the plan does; they stay folded (J-32).
  const generationSettings = document && (
    <details className="space-y-3 rounded-lg border border-gray-200 p-4 text-sm">
      <summary className="cursor-pointer font-medium">
        {t('marketing-generation-settings')}
      </summary>
      <label className="mt-3 block space-y-2">
        <span>{t('marketing-image-provider')}</span>
        <select
          className={INPUT}
          value={chosenProvider?.id ?? ''}
          onChange={(e) => setProvider(e.target.value)}
        >
          {providerOptions.map((p) => (
            <option key={p.id} value={p.id}>
              {p.label}
              {p.available ? '' : t('marketing--setup-required')}
            </option>
          ))}
        </select>
      </label>
      <MarketingProductReferences
        projectId={document.id}
        value={productReferences}
        onChange={setProductReferences}
        disabled={locked}
      />
    </details>
  );
  // Only the section committing the current step keeps a filled button.
  const mainAction =
    stage === 3 && !roundBlocked ? (roundFocus?.step ?? null) : null;
  const productName =
    understanding?.product_name?.trim() ||
    document?.source_text.split('\n')[0]?.trim() ||
    t('marketing-untitled-test');

  return (
    <div ref={formRef} className="mx-auto max-w-6xl space-y-8 pb-12">
      {document && focus ? (
        <MarketingFocusBar
          productName={productName}
          photoVersionId={
            document.product_references?.reference_version_ids?.[0]
          }
          goal={plan?.rules.business_goal?.trim() || null}
          question={plan?.question?.trim() || null}
          gap={focus.gap}
          waiting={focus.waiting}
          action={focus.action}
          more={more}
        />
      ) : (
        <header className="border-b border-gray-200 pb-6">
          <p className="mb-2 text-xs font-semibold uppercase tracking-widest text-gray-500">
            {t('marketing-marketai-marketing-tests')}
          </p>
          <h1 className="text-3xl font-semibold tracking-tight text-gray-900">
            {t('marketing-find-ads-worth-running')}
          </h1>
          <p className="mt-3 max-w-2xl text-sm leading-6 text-gray-600">
            {t(
              'marketing-share-your-product-information-ai-drafts-purchase-propositions-express'
            )}
          </p>
        </header>
      )}
      <nav
        aria-label={t('marketing-test-progress')}
        className="flex flex-wrap gap-x-6 gap-y-2 text-sm"
      >
        {[
          t('marketing-product-information'),
          t('marketing-review-ai-understanding'),
          t('marketing-confirm-test-plan'),
          t('marketing-prepare-creatives'),
        ].map((label, i) => (
          <span
            key={label}
            aria-current={stage === i ? 'step' : undefined}
            className={
              stage === i ? 'font-semibold text-gray-900' : 'text-gray-400'
            }
          >
            {i + 1}. {label}
          </span>
        ))}
      </nav>
      {(error || capabilities.error || project.error || projects.error) && (
        <div
          role="alert"
          className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800"
        >
          {error || errorMessageOf(t, readError)}
          {unmappedFields.length > 0 && (
            <ul>
              {unmappedFields.map((error, index) => (
                <li key={index}>
                  {error.field}: {fieldErrorMessage(t, error)}
                </li>
              ))}
            </ul>
          )}
          {(error === t(SIGN_IN_REQUIRED) ||
            (readError instanceof Error &&
              readError.message === SIGN_IN_REQUIRED)) && (
            <a
              className="ml-3 underline"
              href={`/auth/login?callbackUrl=${encodeURIComponent(
                `/w/${encodeURIComponent(current.slug)}/experiments${
                  selectedId ? `?test=${encodeURIComponent(selectedId)}` : ''
                }`
              )}`}
              target="_blank"
              rel="noopener noreferrer"
            >
              {t('marketing-sign-in-again')}
            </a>
          )}
          {projects.error && !readsStalled && (
            <button
              className="ml-3 underline"
              onClick={() => void projects.mutate().catch(() => undefined)}
            >
              {t('marketing-retry-the-same-action')}
            </button>
          )}
          {readsStalled && (
            <button
              className="ml-3 underline"
              onClick={() => {
                for (const read of [
                  capabilities,
                  generation,
                  projects,
                  project,
                ])
                  if (read.error) void read.mutate().catch(() => undefined);
              }}
            >
              {t('marketing-refresh')}
            </button>
          )}
          {retry.current && (
            <button
              className="ml-3 underline"
              disabled={busy}
              onClick={() => retry.current?.()}
            >
              {t('marketing-retry-the-same-action')}
            </button>
          )}
        </div>
      )}
      {selectedId && !document && !project.error && (
        <p role="status">{t('marketing-restoring-this-test')}</p>
      )}
      {!selectedId && (
        <section className="max-w-3xl space-y-5">
          <h2 className="text-xl font-semibold">
            {t('marketing-tell-us-about-your-product')}
          </h2>
          <p className="text-sm leading-6 text-gray-600">
            {t(
              'marketing-paste-a-product-description-selling-points-target-customers-and-suppor'
            )}
          </p>
          {startingProduct && (
            <p role="status" className="text-sm">
              {t('marketing-starting-from-product')}
            </p>
          )}
          <div className="space-y-2 text-sm">
            <ProductPicker
              id="marketing-product"
              value={selectedProduct}
              onChange={setSelectedProduct}
              disabled={busy}
            />
            {selectedProduct && (
              <button
                type="button"
                className="underline"
                disabled={busy}
                onClick={() => setSelectedProduct('')}
              >
                {t('marketing-start-with-pasted-information')}
              </button>
            )}
          </div>
          <Field
            label={t(
              selectedProduct
                ? 'marketing-product-details-optional'
                : 'marketing-product-details'
            )}
            path={'source_text'}
            errors={fieldMessages('source_text')}
            value={source}
            onChange={setSource}
            rows={8}
          />
          <button
            className={BUTTON}
            disabled={busy || (!source.trim() && !selectedProduct)}
            onClick={() => void create()}
          >
            {busy
              ? t('marketing-saving')
              : t('marketing-save-product-and-start')}
          </button>
          <div className="border-t border-gray-200 pt-6">
            <form
              className="mb-4 flex gap-2"
              onSubmit={(event) => {
                event.preventDefault();
                void router.replace(
                  {
                    pathname: router.pathname,
                    query: {
                      slug: current.slug,
                      ...(searchDraft.trim() ? { q: searchDraft.trim() } : {}),
                    },
                  },
                  undefined,
                  { shallow: true }
                );
              }}
            >
              <label className="flex-1 text-sm">
                <span>{t('marketing-search-tests')}</span>
                <input
                  className={INPUT}
                  value={searchDraft}
                  onChange={(event) => setSearchDraft(event.target.value)}
                />
              </label>
              <button className={SECONDARY} type="submit">
                {t('marketing-search')}
              </button>
            </form>
            {projects.data && projectItems.length === 0 && (
              <p className="text-sm">{t('marketing-no-saved-tests')}</p>
            )}
            {!!projectItems.length && (
              <div className="border-t border-gray-200 pt-6">
                <h3 className="mb-3 text-sm font-medium">
                  {t('marketing-continue-a-saved-test')}
                </h3>
                <div className="divide-y divide-gray-100">
                  {projectItems.map((p) => (
                    <button
                      key={p.id}
                      className="flex w-full items-center justify-between py-3 text-left text-sm hover:text-gray-500"
                      onClick={() => selectProject(p.id)}
                    >
                      <span>{p.product_name}</span>
                      <span className="text-xs text-gray-500">
                        {p.state === 'plan_confirmed'
                          ? t('marketing-plan-confirmed')
                          : t('marketing-continue-editing')}{' '}
                        →
                      </span>
                    </button>
                  ))}
                </div>
              </div>
            )}
            {hasMoreProjects && (
              <button
                className={SECONDARY}
                disabled={projects.isValidating}
                onClick={() =>
                  void projects
                    .setSize(projects.size + 1)
                    .catch(() => undefined)
                }
              >
                {t(
                  projects.isValidating
                    ? 'marketing-loading'
                    : 'marketing-load-more-tests'
                )}
              </button>
            )}
          </div>
        </section>
      )}
      {document && (
        <>
          <details id="marketing-source" className="text-sm text-gray-600">
            <summary className="cursor-pointer">
              {t('marketing-update-product-information')}
            </summary>
            <div className="mt-4 max-w-3xl space-y-3">
              <Field
                label={t('marketing-product-details')}
                path={'source_text'}
                errors={fieldMessages('source_text')}
                value={revisedSource}
                onChange={setRevisedSource}
                rows={6}
              />
              <p>{t('marketing-source-revision-note')}</p>
              <button
                className={SECONDARY}
                disabled={
                  locked ||
                  !revisedSource.trim() ||
                  revisedSource === document.source_text
                }
                onClick={reviseSource}
              >
                {t('marketing-save-source-revision')}
              </button>
            </div>
          </details>
          {document.latest_call?.execution_mode === 'mock' && (
            <p className="rounded-lg bg-amber-50 p-3 text-sm text-amber-900">
              {t(
                'marketing-controlled-mock-example-for-exploring-the-workflow-these-suggestions-a'
              )}
            </p>
          )}
          {running && (
            <p role="status" className="rounded-lg bg-gray-50 p-4 text-sm">
              {document.latest_call?.state === 'uncertain'
                ? t(
                    'marketing-the-previous-ai-call-needs-reconciliation-its-cost-exposure-is-retaine'
                  )
                : t(
                    'marketing-ai-is-drafting-suggestions-you-can-leave-this-page-and-continue-later'
                  )}
            </p>
          )}
          {document.latest_call?.state === 'failed' && (
            <p role="alert" className="text-sm text-red-700">
              {t(
                'marketing-ai-could-not-complete-a-valid-draft-your-product-information-is-saved-'
              )}
              {document.latest_call.error_code && (
                <span className="mt-1 block">
                  {errorCodeMessage(t, document.latest_call.error_code)}
                </span>
              )}
            </p>
          )}
          {document.latest_call?.state === 'uncertain' && (
            <div id="marketing-call-recovery">
              <MarketingCallRecovery
                key={document.latest_call.id}
                call={document.latest_call}
                kind="planning"
                onResolved={() => {
                  void project.mutate();
                }}
              />
            </div>
          )}
          {!languageAvailable && (
            <p
              role="status"
              className="rounded-lg bg-amber-50 p-3 text-sm text-amber-900"
            >
              {t('marketing-language-feature-unavailable')}
            </p>
          )}
          {stage < 3 && languageAvailable && (
            <div className="max-w-sm space-y-1 text-sm">
              <label className="block space-y-1">
                <span>{t('marketing-ad-language')}</span>
                <select
                  className={INPUT}
                  value={adLanguage}
                  disabled={locked}
                  aria-describedby={
                    plan?.language_confirmation_required
                      ? adLanguageHint
                      : undefined
                  }
                  onChange={(event) => {
                    setAdLanguage(event.target.value);
                    updatePlan('ad_language', event.target.value);
                  }}
                >
                  <option value="en-US">
                    {t('marketing-ad-language-en-US')}
                  </option>
                  <option value="zh-CN">
                    {t('marketing-ad-language-zh-CN')}
                  </option>
                  {!['en-US', 'zh-CN'].includes(adLanguage) && (
                    <option value={adLanguage}>{adLanguage}</option>
                  )}
                </select>
              </label>
              {/* Outside the label: it describes the language, not names it. */}
              {plan?.language_confirmation_required && (
                <p id={adLanguageHint} role="status" className="text-amber-800">
                  {t('marketing-ad-language-needs-confirmation')}
                </p>
              )}
            </div>
          )}
          {stage < 3 && (
            <div className="flex flex-wrap items-center gap-4 rounded-lg border border-gray-200 bg-gray-50 px-4 py-3 text-sm">
              <span>{costLabel}</span>
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={mock}
                  disabled={locked}
                  onChange={(e) => setMock(e.target.checked)}
                />
                {t('marketing-try-a-controlled-mock-example')}
              </label>
              {!mock && paid && !paid.available && (
                <span className="text-gray-600">
                  {t(
                    'marketing-ai-planning-is-unavailable-ask-your-owner-to-configure-workspace-crede'
                  )}
                </span>
              )}
            </div>
          )}
          {stage === 0 && (
            <section className="max-w-3xl space-y-4">
              <h2 className="text-xl font-semibold">
                {t('marketing-ai-starts-with-two-questions')}
              </h2>
              <p className="text-sm leading-7 text-gray-600">
                {t(
                  'marketing-why-do-customers-need-this-type-of-product-and-why-would-they-consider'
                )}
              </p>
              <details className="text-sm text-gray-500">
                <summary>
                  {t('marketing-view-saved-product-information')}
                </summary>
                <p className="mt-3 whitespace-pre-wrap">
                  {document.source_text}
                </p>
              </details>
            </section>
          )}
          {understanding && stage === 1 && (
            <section className="space-y-6">
              <div>
                <h2 className="text-xl font-semibold">
                  {t('marketing-does-this-describe-your-product-accurately')}
                </h2>
                <p className="mt-2 text-sm text-gray-600">
                  {t(
                    'marketing-edit-anything-that-is-inaccurate-agreeing-with-an-inference-makes-it-w'
                  )}
                </p>
              </div>
              <fieldset disabled={locked} className="grid gap-5 md:grid-cols-2">
                <Field
                  label={t('marketing-product-name')}
                  path={'understanding.product_name'}
                  errors={fieldMessages('understanding.product_name')}
                  value={understanding.product_name}
                  onChange={(v) => updateUnderstanding('product_name', v)}
                  rows={1}
                />
                <Field
                  label={t('marketing-target-customers')}
                  path={'understanding.audience'}
                  errors={fieldMessages('understanding.audience')}
                  value={understanding.audience}
                  onChange={(v) => updateUnderstanding('audience', v)}
                  rows={1}
                />
                <Field
                  label={t('marketing-why-this-type-of-product')}
                  path={'understanding.category_need'}
                  errors={fieldMessages('understanding.category_need')}
                  value={understanding.category_need}
                  onChange={(v) => updateUnderstanding('category_need', v)}
                />
                <Field
                  label={t('marketing-why-consider-our-product')}
                  path={'understanding.product_choice_reason'}
                  errors={fieldMessages('understanding.product_choice_reason')}
                  value={understanding.product_choice_reason}
                  onChange={(v) =>
                    updateUnderstanding('product_choice_reason', v)
                  }
                />
                <Field
                  label={t('marketing-usage-situations-one-per-line')}
                  path={'understanding.situations'}
                  errors={fieldMessages('understanding.situations')}
                  value={understanding.situations.join('\n')}
                  onChange={(v) =>
                    updateUnderstanding('situations', v.split('\n'))
                  }
                />
                <Field
                  label={t('marketing-purchase-concerns-one-per-line')}
                  path={'understanding.objections'}
                  errors={fieldMessages('understanding.objections')}
                  value={(understanding.objections ?? []).join('\n')}
                  onChange={(v) =>
                    updateUnderstanding(
                      'objections',
                      v.split('\n').filter(Boolean)
                    )
                  }
                />
              </fieldset>
              <div className="grid gap-8 lg:grid-cols-2">
                <div>
                  <h3 className="mb-3 font-medium">
                    {t('marketing-product-facts-please-verify')}
                  </h3>
                  {(understanding.facts ?? []).map((fact, index) => (
                    <div
                      key={fact.id}
                      className="space-y-2 border-t border-gray-200 py-4"
                    >
                      <Field
                        label={t('marketing-fact-number', {
                          number: index + 1,
                        })}
                        path={`understanding.facts.${index}.statement`}
                        errors={fieldMessages(
                          `understanding.facts.${index}.statement`
                        )}
                        value={fact.statement}
                        onChange={(value) =>
                          updateUnderstanding(
                            'facts',
                            (understanding.facts ?? []).map((f) =>
                              f.id === fact.id ? { ...f, statement: value } : f
                            )
                          )
                        }
                      />
                      <blockquote className="border-l-2 border-gray-200 pl-3 text-xs leading-6 text-gray-500">
                        {t('marketing-source-quote')}
                        {fact.source_quote}
                      </blockquote>
                      <label className="flex items-center gap-2 text-sm">
                        <input
                          type="checkbox"
                          disabled={locked}
                          checked={fact.confirmation === 'confirmed'}
                          onChange={(e) =>
                            updateUnderstanding(
                              'facts',
                              (understanding.facts ?? []).map((f) =>
                                f.id === fact.id
                                  ? {
                                      ...f,
                                      confirmation: e.target.checked
                                        ? 'confirmed'
                                        : 'unconfirmed',
                                    }
                                  : f
                              )
                            )
                          }
                        />
                        {t('marketing-this-fact-and-its-evidence-are-accurate')}
                      </label>
                    </div>
                  ))}
                </div>
                <div>
                  <h3 className="mb-3 font-medium">
                    {t('marketing-ai-inferences-not-validated-by-delivery')}
                  </h3>
                  {understanding.inferences.map((inference, i) => (
                    <div
                      key={inference.id}
                      className="space-y-2 border-t border-gray-200 py-4"
                    >
                      <Field
                        label={t('marketing-inference-number', {
                          number: i + 1,
                        })}
                        path={`understanding.inferences.${i}.statement`}
                        errors={fieldMessages(
                          `understanding.inferences.${i}.statement`
                        )}
                        value={inference.statement}
                        onChange={(value) =>
                          updateUnderstanding(
                            'inferences',
                            understanding.inferences.map((item) =>
                              item.id === inference.id
                                ? { ...item, statement: value }
                                : item
                            )
                          )
                        }
                      />
                      <p className="text-xs leading-6 text-gray-500">
                        {t('marketing-rationale')}
                        {inference.rationale}
                      </p>
                    </div>
                  ))}
                  <h4 className="mb-2 mt-4 text-sm font-medium">
                    {t('marketing-still-unknown')}
                  </h4>
                  <ul className="list-disc space-y-1 pl-5 text-sm text-gray-600">
                    {(understanding.unknowns ?? []).map((u, i) => (
                      <li key={i}>{u}</li>
                    ))}
                  </ul>
                </div>
              </div>
            </section>
          )}
          {stage >= 1 && (
            <CompetitorsArea
              key={document.product_id ?? document.id}
              productId={document.product_id ?? null}
              planMarket={plan?.market ?? null}
              defaultOpen={stage === 1}
            />
          )}
          {stage === 2 && !plan && (
            <section className="max-w-3xl space-y-5">
              <h2 className="text-xl font-semibold">
                {t('marketing-let-ai-plan-your-first-test')}
              </h2>
              <p className="text-sm leading-7 text-gray-600">
                {t(
                  'marketing-ai-uses-your-confirmed-understanding-to-suggest-purchase-propositions-'
                )}
              </p>
            </section>
          )}
          {plan && stage >= 2 && (
            <section className="space-y-6">
              <div>
                <p className="text-xs font-medium text-gray-500">
                  {plan.stage === 'screen_angles'
                    ? t('marketing-screen-angles')
                    : plan.stage === 'match_expressions'
                      ? t('marketing-match-expressions')
                      : plan.stage === 'confirm_combination'
                        ? t('marketing-confirm-combinations')
                        : t('marketing-refine-elements-when-needed')}{' '}
                  {t('marketing--performance-unvalidated')}
                </p>
                <h2 className="mt-2 text-xl font-semibold">
                  {stage === 3
                    ? t('marketing-test-plan-confirmed')
                    : t('marketing-what-will-this-test-answer')}
                </h2>
              </div>
              {reviewAvailable && stage !== 3 && planReview}
              <details open={stage !== 3}>
                <summary className="cursor-pointer text-sm font-medium">
                  {t('marketing-view-confirmed-plan-details')}
                </summary>
                {reviewAvailable && stage === 3 && (
                  <div className="mt-4">{planReview}</div>
                )}
                <fieldset
                  disabled={locked || stage === 3}
                  className="mt-4 space-y-6"
                >
                  <Field
                    label={t('marketing-test-question')}
                    path={'plan.question'}
                    errors={fieldMessages('plan.question')}
                    value={plan.question}
                    onChange={(v) => updatePlan('question', v)}
                  />
                  <label className="block space-y-1.5 text-sm">
                    <span>{t('marketing-media-currency')}</span>
                    <select
                      data-validation-field="plan.currency"
                      className={INPUT}
                      aria-invalid={
                        fieldMessages('plan.currency').length > 0 || undefined
                      }
                      aria-describedby={
                        fieldMessages('plan.currency').length
                          ? 'marketing-plan-currency-errors'
                          : undefined
                      }
                      value={plan.currency}
                      onChange={(event) =>
                        updatePlan(
                          'currency',
                          event.target.value as Plan['currency']
                        )
                      }
                    >
                      {(capabilities.data?.media_currencies ?? ['USD']).map(
                        (currency) => (
                          <option key={currency} value={currency}>
                            {currency}
                          </option>
                        )
                      )}
                    </select>
                    {fieldMessages('plan.currency').length > 0 && (
                      <p
                        id="marketing-plan-currency-errors"
                        className="text-sm text-red-700"
                      >
                        {fieldMessages('plan.currency').join(' ')}
                      </p>
                    )}
                  </label>
                  <div className="grid gap-4 sm:grid-cols-3">
                    {(
                      [
                        [
                          'media_budget',
                          t('marketing-overall-media-budget-usd', {
                            currency: plan.currency,
                          }),
                        ],
                        [
                          'small_round_budget',
                          t('marketing-this-test-media-budget-usd', {
                            currency: plan.currency,
                          }),
                        ],
                        ['duration_days', t('marketing-test-duration-days')],
                      ] as const
                    ).map(([key, label]) => (
                      <div
                        key={key}
                        data-validation-field={`plan.${key}`}
                        className="space-y-1.5 text-sm"
                      >
                        <label htmlFor={`marketing-plan-${key}`}>
                          <span>{label}</span>
                          <input
                            className={INPUT}
                            id={`marketing-plan-${key}`}
                            aria-invalid={
                              fieldMessages(`plan.${key}`).length > 0 ||
                              undefined
                            }
                            aria-describedby={
                              fieldMessages(`plan.${key}`).length
                                ? `marketing-plan-${key}-errors`
                                : undefined
                            }
                            type="number"
                            disabled={key === 'media_budget' && !!roundState}
                            min="1"
                            value={plan[key]}
                            onChange={(e) =>
                              updatePlan(
                                key,
                                key === 'duration_days'
                                  ? Number(e.target.value)
                                  : e.target.value
                              )
                            }
                          />
                        </label>
                        {fieldMessages(`plan.${key}`).length > 0 && (
                          <p
                            id={`marketing-plan-${key}-errors`}
                            className="text-sm text-red-700"
                          >
                            {fieldMessages(`plan.${key}`).join(' ')}
                          </p>
                        )}
                      </div>
                    ))}
                  </div>
                  <div
                    data-validation-field="plan.executions_per_combination"
                    className="space-y-1.5 text-sm"
                  >
                    <label htmlFor="marketing-plan-executions_per_combination">
                      <span>{t('marketing-executions-per-combination')}</span>
                      <select
                        className={INPUT}
                        id="marketing-plan-executions_per_combination"
                        aria-describedby="marketing-plan-executions_per_combination-hint"
                        value={
                          plan.executions_per_combination ?? LEGACY_EXECUTIONS
                        }
                        onChange={(e) =>
                          updatePlan(
                            'executions_per_combination',
                            Number(e.target.value)
                          )
                        }
                      >
                        {[1, 2, 3, 4].map((n) => (
                          <option key={n} value={n}>
                            {n}
                          </option>
                        ))}
                      </select>
                    </label>
                    <p
                      id="marketing-plan-executions_per_combination-hint"
                      className="text-gray-500"
                    >
                      {t('marketing-executions-per-combination-hint')}
                    </p>
                    {fieldMessages('plan.executions_per_combination').length >
                      0 && (
                      <p className="text-red-700">
                        {fieldMessages('plan.executions_per_combination').join(
                          ' '
                        )}
                      </p>
                    )}
                  </div>
                  <div className="space-y-6">
                    {plan.angles.map((angle, ai) => (
                      <div
                        key={angle.id}
                        className="border-t border-gray-200 pt-5"
                      >
                        <Field
                          label={t('marketing-angle-number', {
                            number: ai + 1,
                          })}
                          path={`plan.angles.${ai}.proposition`}
                          errors={fieldMessages(
                            `plan.angles.${ai}.proposition`
                          )}
                          value={angle.proposition}
                          onChange={(v) =>
                            updatePlan(
                              'angles',
                              plan.angles.map((a) =>
                                a.id === angle.id ? { ...a, proposition: v } : a
                              )
                            )
                          }
                          rows={1}
                        />
                        <p className="mt-2 text-sm leading-6 text-gray-500">
                          {angle.rationale}
                        </p>
                        <div className="mt-4 space-y-3 border-l-2 border-gray-100 pl-5">
                          {angle.expressions.map((expression, ei) => (
                            <div key={expression.id} className="py-2">
                              <Field
                                label={t('marketing-expression')}
                                path={`plan.angles.${ai}.expressions.${ei}.name`}
                                errors={fieldMessages(
                                  `plan.angles.${ai}.expressions.${ei}.name`
                                )}
                                value={expression.name}
                                rows={1}
                                onChange={(v) =>
                                  updateExpression(expression.id, 'name', v)
                                }
                              />
                              {reviewAvailable && (
                                <MarketingExpressionDetails
                                  expression={expression}
                                  facts={understanding?.facts ?? []}
                                  path={`plan.angles.${ai}.expressions.${ei}`}
                                  flagged={duplicated.has(expression.id)}
                                  messages={fieldMessages}
                                  onChange={(details) =>
                                    updateSharedExpression(
                                      expression.id,
                                      details
                                    )
                                  }
                                />
                              )}
                              <details className="mt-2 text-sm text-gray-600">
                                <summary>{expression.description}</summary>
                                <div className="mt-3 space-y-3">
                                  <Field
                                    label={t(
                                      'marketing-expression-description'
                                    )}
                                    path={`plan.angles.${ai}.expressions.${ei}.description`}
                                    errors={fieldMessages(
                                      `plan.angles.${ai}.expressions.${ei}.description`
                                    )}
                                    value={expression.description}
                                    onChange={(value) =>
                                      updateExpression(
                                        expression.id,
                                        'description',
                                        value
                                      )
                                    }
                                  />
                                  <Field
                                    label={t('marketing-why-it-fits')}
                                    path={`plan.angles.${ai}.expressions.${ei}.fit_reason`}
                                    errors={fieldMessages(
                                      `plan.angles.${ai}.expressions.${ei}.fit_reason`
                                    )}
                                    value={expression.fit_reason}
                                    onChange={(value) =>
                                      updatePlan(
                                        'angles',
                                        plan.angles.map((a) =>
                                          a.id === angle.id
                                            ? {
                                                ...a,
                                                expressions: a.expressions.map(
                                                  (e) =>
                                                    e.id === expression.id
                                                      ? {
                                                          ...e,
                                                          fit_reason: value,
                                                        }
                                                      : e
                                                ),
                                              }
                                            : a
                                        )
                                      )
                                    }
                                  />
                                </div>
                              </details>
                              <p className="mt-1 text-xs leading-6 text-gray-500">
                                {t('marketing-why-it-fits-message')}
                                {expression.fit_reason}
                              </p>
                              {!!(expression.required_evidence ?? [])
                                .length && (
                                <p className="mt-1 text-xs text-amber-800">
                                  {t('marketing-evidence-needed')}
                                  {(expression.required_evidence ?? []).join(
                                    t('list-separator')
                                  )}
                                </p>
                              )}
                              <details
                                className="mt-2 text-xs text-gray-500"
                                open={
                                  plan.combinations.some(
                                    (c) =>
                                      c.angle_id === angle.id &&
                                      c.expression_id === expression.id &&
                                      staleBriefs.has(c.id)
                                  ) || undefined
                                }
                              >
                                <summary>
                                  {t(
                                    'marketing-view-execution-elements-and-checks'
                                  )}
                                </summary>
                                {plan.combinations
                                  .filter(
                                    (c) =>
                                      c.angle_id === angle.id &&
                                      c.expression_id === expression.id
                                  )
                                  .map((c) => (
                                    <div key={c.id} className="mt-2 space-y-2">
                                      <Field
                                        label={t('marketing-headline')}
                                        path={`plan.combinations.${plan.combinations.findIndex((item) => item.id === c.id)}.headline`}
                                        errors={fieldMessages(
                                          `plan.combinations.${plan.combinations.findIndex((item) => item.id === c.id)}.headline`
                                        )}
                                        value={c.headline}
                                        onChange={(v) =>
                                          updatePlan(
                                            'combinations',
                                            plan.combinations.map((x) =>
                                              x.id === c.id
                                                ? { ...x, headline: v }
                                                : x
                                            )
                                          )
                                        }
                                      />
                                      <Field
                                        label={t('marketing-visual-brief')}
                                        path={`plan.combinations.${plan.combinations.findIndex((item) => item.id === c.id)}.visual_brief`}
                                        errors={fieldMessages(
                                          `plan.combinations.${plan.combinations.findIndex((item) => item.id === c.id)}.visual_brief`
                                        )}
                                        value={c.visual_brief}
                                        onChange={(v) =>
                                          updatePlan(
                                            'combinations',
                                            plan.combinations.map((x) =>
                                              x.id === c.id
                                                ? { ...x, visual_brief: v }
                                                : x
                                            )
                                          )
                                        }
                                      />
                                      <Field
                                        label={t('marketing-ad-copy')}
                                        path={`plan.combinations.${plan.combinations.findIndex((item) => item.id === c.id)}.body_copy`}
                                        errors={fieldMessages(
                                          `plan.combinations.${plan.combinations.findIndex((item) => item.id === c.id)}.body_copy`
                                        )}
                                        value={c.body_copy}
                                        onChange={(v) =>
                                          updatePlan(
                                            'combinations',
                                            plan.combinations.map((x) =>
                                              x.id === c.id
                                                ? { ...x, body_copy: v }
                                                : x
                                            )
                                          )
                                        }
                                      />
                                      {Object.entries(c.elements ?? {}).map(
                                        ([key, value]) => (
                                          <Field
                                            key={key}
                                            label={t('marketing-element-name', {
                                              name: key,
                                            })}
                                            path={`plan.combinations.${plan.combinations.findIndex((item) => item.id === c.id)}.elements.${key}`}
                                            errors={fieldMessages(
                                              `plan.combinations.${plan.combinations.findIndex((item) => item.id === c.id)}.elements.${key}`
                                            )}
                                            value={value}
                                            onChange={(v) =>
                                              updatePlan(
                                                'combinations',
                                                plan.combinations.map((x) =>
                                                  x.id === c.id
                                                    ? {
                                                        ...x,
                                                        elements: {
                                                          ...x.elements,
                                                          [key]: v,
                                                        },
                                                      }
                                                    : x
                                                )
                                              )
                                            }
                                          />
                                        )
                                      )}
                                      <details
                                        className="space-y-3 rounded-lg bg-gray-50 p-3"
                                        open={
                                          staleBriefs.has(c.id) || undefined
                                        }
                                      >
                                        <summary>
                                          {t('marketing-english-image-brief')}
                                        </summary>
                                        <p className="text-xs text-gray-600">
                                          {t(
                                            'marketing-english-image-brief-help'
                                          )}
                                        </p>
                                        {staleBriefs.has(c.id) && (
                                          <p className="text-xs font-medium text-red-700">
                                            {t(
                                              'marketing-image-brief-stale-flag'
                                            )}
                                          </p>
                                        )}
                                        {(
                                          [
                                            'proposition',
                                            'expression',
                                            'hold_constant',
                                            'checks',
                                          ] as const
                                        ).map((key) => (
                                          <Field
                                            key={key}
                                            label={t(
                                              `marketing-image-brief-${key}`
                                            )}
                                            path={`plan.combinations.${plan.combinations.findIndex((item) => item.id === c.id)}.image_brief.${key}`}
                                            errors={fieldMessages(
                                              `plan.combinations.${plan.combinations.findIndex((item) => item.id === c.id)}.image_brief.${key}`
                                            )}
                                            value={
                                              Array.isArray(
                                                c.image_brief?.[key]
                                              )
                                                ? (
                                                    c.image_brief?.[
                                                      key
                                                    ] as string[]
                                                  ).join('\n')
                                                : ((c.image_brief?.[key] as
                                                    | string
                                                    | undefined) ?? '')
                                            }
                                            onChange={(value) =>
                                              updatePlan(
                                                'combinations',
                                                plan.combinations.map(
                                                  (combo) =>
                                                    combo.id === c.id
                                                      ? {
                                                          ...combo,
                                                          image_brief: {
                                                            proposition: '',
                                                            expression: '',
                                                            hold_constant: [],
                                                            checks: [],
                                                            ...combo.image_brief,
                                                            [key]:
                                                              key ===
                                                                'hold_constant' ||
                                                              key === 'checks'
                                                                ? value.split(
                                                                    '\n'
                                                                  )
                                                                : value,
                                                          },
                                                        }
                                                      : combo
                                                )
                                              )
                                            }
                                          />
                                        ))}
                                      </details>
                                      <p>
                                        {t('marketing-checks-after-generation')}
                                        {c.checks.join(t('list-separator'))}
                                      </p>
                                    </div>
                                  ))}
                              </details>
                              {stage === 2 && (
                                <button
                                  type="button"
                                  className="mt-2 text-xs text-gray-500 underline disabled:opacity-40"
                                  disabled={
                                    locked || angle.expressions.length <= 1
                                  }
                                  onClick={() =>
                                    removeExpression(angle.id, expression.id)
                                  }
                                >
                                  {t('marketing-remove-expression')}
                                </button>
                              )}
                            </div>
                          ))}
                          {stage === 2 && (
                            <label className="block space-y-1 text-sm">
                              <span>
                                {t('marketing-share-expression', {
                                  angle: angle.proposition,
                                })}
                              </span>
                              <select
                                className={INPUT}
                                aria-label={t('marketing-share-expression', {
                                  angle: angle.proposition,
                                })}
                                value=""
                                disabled={
                                  locked ||
                                  angle.expressions.length >= 8 ||
                                  plan.combinations.length >= 30
                                }
                                onChange={(e) =>
                                  e.target.value &&
                                  addExpression(angle.id, e.target.value)
                                }
                              >
                                <option value="">
                                  {t('marketing-share-expression-select')}
                                </option>
                                {Array.from(
                                  new Map(
                                    plan.angles
                                      .flatMap((a) => a.expressions)
                                      .map((e) => [e.id, e])
                                  ).values()
                                )
                                  .filter(
                                    (e) =>
                                      !angle.expressions.some(
                                        (own) => own.id === e.id
                                      )
                                  )
                                  .map((e) => (
                                    <option key={e.id} value={e.id}>
                                      {e.name}
                                    </option>
                                  ))}
                              </select>
                              <span className="block text-xs text-gray-500">
                                {t('marketing-share-expression-boundary')}
                              </span>
                            </label>
                          )}
                          {stage === 2 && (
                            <button
                              type="button"
                              className="pt-2 text-sm underline"
                              onClick={() => addExpression(angle.id)}
                              disabled={
                                angle.expressions.length >= 8 ||
                                plan.combinations.length >= 30
                              }
                            >
                              {t('marketing-add-an-expression-for-this-angle')}
                            </button>
                          )}
                        </div>
                        {stage === 2 && (
                          <button
                            type="button"
                            className="mt-3 text-sm underline"
                            disabled={!withoutAngle(angle.id)}
                            onClick={() => setPlan(withoutAngle(angle.id))}
                          >
                            {t('marketing-remove-angle')}
                          </button>
                        )}
                      </div>
                    ))}
                  </div>
                  {(plan.stage === 'match_expressions' ||
                    plan.comparison_kind === 'specific_factor' ||
                    new Set(
                      plan.angles.flatMap((a) => a.expressions.map((e) => e.id))
                    ).size <
                      plan.angles.reduce(
                        (count, a) => count + a.expressions.length,
                        0
                      )) && <MarketingComparisonView plan={plan} />}
                  <div className="border-t border-gray-200 pt-5">
                    <h3 className="mb-3 font-medium">
                      {t('marketing-what-we-are-comparing')}
                    </h3>
                    {plan.limited_expression_coverage && (
                      <p className="mb-3 text-sm text-amber-800">
                        {t('marketing-limited-expression-coverage-note')}
                      </p>
                    )}
                    {plan.groups.map((group) => (
                      <p key={group.id} className="mb-2 text-sm leading-6">
                        <strong>{group.name}</strong> ·{' '}
                        {group.combination_ids.length}{' '}
                        {t('marketing-creative-combinations')}
                        {group.rationale}
                      </p>
                    ))}
                    <p className="mt-3 text-sm text-gray-600">
                      {plan.comparison_kind === 'whole_combinations'
                        ? t(
                            'marketing-this-test-compares-complete-combinations-performance-differences-canno'
                          )
                        : plan.comparison_kind === 'business_retest'
                          ? t(
                              'marketing-this-test-checks-the-business-target-without-making-a-relative-compari'
                            )
                          : t(
                              'marketing-this-test-compares-a-specific-factor-check-the-changed-and-controlled-'
                            )}
                    </p>
                  </div>
                  {brandStyleAvailable && (
                    <MarketingBrandStyle
                      plan={plan}
                      frozen={document.brand_kit}
                      onChange={setPlan}
                      fieldMessages={fieldMessages}
                      errorCodes={fieldErrors.map((error) => error.code ?? '')}
                      disabled={locked}
                    />
                  )}
                  <div className="grid gap-4 md:grid-cols-2">
                    <Field
                      label={t('marketing-target-customers')}
                      value={plan.audience}
                      onChange={(v) => updatePlan('audience', v)}
                    />
                    <Field
                      label={t('marketing-market')}
                      value={plan.market}
                      onChange={(v) => updatePlan('market', v)}
                    />
                    <Field
                      label={t('marketing-offer')}
                      value={plan.offer}
                      onChange={(v) => updatePlan('offer', v)}
                    />
                    <Field
                      label={t('marketing-landing-page')}
                      value={plan.landing_page}
                      onChange={(v) => updatePlan('landing_page', v)}
                    />
                    <Field
                      label={t('marketing-business-goal')}
                      value={plan.rules.business_goal}
                      onChange={(v) =>
                        updatePlan('rules', {
                          ...plan.rules,
                          business_goal: v,
                        })
                      }
                    />
                    <Field
                      label={t('marketing-when-to-stop')}
                      value={plan.rules.stop_rule}
                      onChange={(v) =>
                        updatePlan('rules', { ...plan.rules, stop_rule: v })
                      }
                    />
                  </div>
                  <details className="space-y-4 rounded-lg bg-gray-50 p-4">
                    <summary className="cursor-pointer text-sm font-medium">
                      {t(
                        'marketing-view-comparison-conditions-decision-rules-and-limits'
                      )}
                    </summary>
                    <div className="grid gap-4 pt-4 md:grid-cols-2">
                      <Field
                        label={t('marketing-actively-changed-one-per-line')}
                        path="plan.changed"
                        errors={fieldMessages('plan.changed')}
                        value={plan.changed.join('\n')}
                        onChange={(v) => updatePlan('changed', v.split('\n'))}
                      />
                      <Field
                        label={t('marketing-held-constant-one-per-line')}
                        path="plan.held_constant"
                        errors={fieldMessages('plan.held_constant')}
                        value={plan.held_constant.join('\n')}
                        onChange={(v) =>
                          updatePlan('held_constant', v.split('\n'))
                        }
                      />
                      <Field
                        label={t(
                          'marketing-allowed-supporting-changes-one-per-line'
                        )}
                        value={(plan.allowed_changes ?? []).join('\n')}
                        onChange={(v) =>
                          updatePlan(
                            'allowed_changes',
                            v.split('\n').filter(Boolean)
                          )
                        }
                      />
                      <Field
                        label={t('marketing-known-limitations-one-per-line')}
                        value={plan.limitations.join('\n')}
                        onChange={(v) =>
                          updatePlan('limitations', v.split('\n'))
                        }
                      />
                      <Field
                        label={t('marketing-evidence-requirements')}
                        value={plan.rules.evidence_requirements}
                        onChange={(v) =>
                          updatePlan('rules', {
                            ...plan.rules,
                            evidence_requirements: v,
                          })
                        }
                      />
                      <Field
                        label={t('marketing-evaluation-schedule')}
                        value={plan.rules.evaluation_schedule}
                        onChange={(v) =>
                          updatePlan('rules', {
                            ...plan.rules,
                            evaluation_schedule: v,
                          })
                        }
                      />
                      <label className="space-y-2 text-sm">
                        <span>{t('marketing-primary-metric')}</span>
                        <select
                          className={INPUT}
                          value={plan.rules.business_metric}
                          onChange={(e) =>
                            updatePlan('rules', {
                              ...plan.rules,
                              business_metric: e.target.value as
                                | 'cpa'
                                | 'roas'
                                | 'ctr',
                            })
                          }
                        >
                          <option value="cpa">
                            {t('marketing-cpa-cost-per-purchase')}
                          </option>
                          <option value="roas">
                            {t('marketing-roas-return-on-ad-spend')}
                          </option>
                          <option value="ctr">
                            {t('marketing-ctr-exploratory-clicks-only')}
                          </option>
                        </select>
                      </label>
                      <label className="space-y-2 text-sm">
                        <span>{t('marketing-business-threshold')}</span>
                        <input
                          className={INPUT}
                          type="number"
                          min="0.000001"
                          step="any"
                          value={plan.rules.threshold}
                          onChange={(e) =>
                            updatePlan('rules', {
                              ...plan.rules,
                              threshold: e.target.value,
                            })
                          }
                        />
                      </label>
                      <Field
                        label={t('marketing-delivery-allocation')}
                        value={plan.allocation}
                        onChange={(v) => updatePlan('allocation', v)}
                      />
                      <Field
                        label={t('marketing-baseline-ad')}
                        value={plan.baseline}
                        onChange={(v) => updatePlan('baseline', v)}
                      />
                    </div>
                    <p className="pt-3 text-xs text-gray-500">
                      {t(
                        'marketing-business-success-and-relative-performance-are-separate-a-test-may-end-'
                      )}
                    </p>
                  </details>
                </fieldset>
              </details>
              {stage === 2 && generationSettings}
              {stage === 3 && (!roundState || roundState === 'reviewing') && (
                <div
                  id="marketing-generation-setup"
                  className="space-y-4 border-t border-gray-200 pt-6"
                >
                  <p className="text-sm leading-6 text-gray-600">
                    {t('marketing-confirmed')} {plan.combinations.length}{' '}
                    {t(
                      'marketing-creative-combinations-prepare-each-through-the-existing-providers-cost'
                    )}
                  </p>
                  <p className="text-sm font-medium text-gray-900">
                    {executionsLabel}
                  </p>
                  {document.product_references?.reference_version_ids
                    ?.length ? (
                    <div className="space-y-2 text-sm">
                      <p className="font-medium">
                        {t('marketing-product-photos-used')}
                      </p>
                      <ul className="flex flex-wrap gap-2">
                        {document.product_references.reference_version_ids.map(
                          (id, i) => (
                            <li
                              key={id}
                              className="h-16 w-16 overflow-hidden rounded border border-gray-200"
                            >
                              <ReferenceThumbnail
                                versionId={id}
                                alt={t('marketing-product-photo-number', {
                                  number: i + 1,
                                })}
                              />
                            </li>
                          )
                        )}
                      </ul>
                    </div>
                  ) : (
                    <p className="text-sm text-gray-600">
                      {t(
                        draftIds.length
                          ? 'marketing-product-photos-none-frozen'
                          : 'marketing-product-photos-automatic'
                      )}
                    </p>
                  )}
                  {!draftIds.length && generationSettings}
                  {draftIds.length > 0 && (
                    <div
                      role="group"
                      aria-label={t('marketing-creative-plans')}
                      className="flex flex-wrap gap-2"
                    >
                      {draftIds.map((id, i) => {
                        const combination = plan.combinations[i];
                        const angle = plan.angles.find(
                          (a) => a.id === combination?.angle_id
                        );
                        const expression = angle?.expressions.find(
                          (e) => e.id === combination?.expression_id
                        );
                        return (
                          <button
                            className={activeDraft === id ? PRESSED : SECONDARY}
                            aria-pressed={activeDraft === id}
                            key={id}
                            onClick={() => setActiveDraft(id)}
                          >
                            {draftIds.length === plan.combinations.length &&
                            angle &&
                            expression
                              ? `${angle.proposition} · ${expression.name}`
                              : t('marketing-creative-plan-number', {
                                  number: i + 1,
                                })}
                          </button>
                        );
                      })}
                    </div>
                  )}
                </div>
              )}
            </section>
          )}
          {activeDraft &&
            languageAvailable &&
            (!roundState || roundState === 'reviewing') && (
              <div
                id="marketing-generation-editor"
                className="border-t border-gray-200 pt-8"
              >
                <MainActionProvider value={mainAction}>
                  <GenerationComposer
                    key={activeDraft}
                    embeddedDraftId={activeDraft}
                    onSubmitted={() => {
                      void revalidate([
                        'marketing-workflow',
                        current.workspaceId,
                        document.id,
                      ]);
                    }}
                  />
                </MainActionProvider>
              </div>
            )}
          <MainActionProvider value={mainAction}>
            <MarketingExecution
              key={document.id}
              project={document}
              reporting={capabilities.data}
              onRoundStateChange={setRoundState}
              onRoundPlanRevisionChange={setRoundPlanRevision}
              onFocusChange={setRoundFocus}
              openRequest={openRequest}
              onNextPlan={(body) => askAI('plan', body)}
              onFeedbackDraft={(draftId) => {
                setActiveDraft(draftId);
                requestAnimationFrame(() =>
                  window.document
                    .getElementById('marketing-generation-editor')
                    ?.scrollIntoView({ behavior: 'smooth', block: 'start' })
                );
              }}
            />
          </MainActionProvider>
        </>
      )}
    </div>
  );
}
