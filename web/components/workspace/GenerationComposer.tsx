import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { useTranslation } from 'next-i18next';
import { useWorkspace } from './WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import { useEmphasis } from './marketing-focus';
import { TechnicalDetails } from './MarketingTechnicalInfo';

type Draft = components['schemas']['GenerationDraftDetail'];
type Preview = components['schemas']['GenerationPreviewDetail'];
type Run = components['schemas']['GenerationRunDetail'];
type Form = {
  product: string;
  provider: string;
  model: string;
  ratio: string;
  size: string;
  variants: number;
  prompt: string;
};
type PendingSubmission = {
  action: string;
  previewId: string;
  previewHash: string;
  revision: number;
};
const INITIAL_FORM: Form = {
  product: '',
  provider: '',
  model: '',
  ratio: '1:1',
  size: '1K',
  variants: 1,
  prompt: '',
};
const INPUT =
  'w-full rounded-lg border border-gray-300 bg-white px-3 py-2.5 text-sm focus:border-gray-700 focus:outline-none focus:ring-2 focus:ring-gray-200 disabled:bg-gray-50';
const TERMINAL = new Set([
  'succeeded',
  'partially_succeeded',
  'failed',
  'cancelled',
  'uncertain',
]);

class CommandProblem extends Error {
  constructor(public code: string) {
    super(code);
  }
}
function commandData<T>(result: {
  kind: string;
  data?: T;
  problem?: { code: string };
}): T {
  if (result.kind === 'reconciliation_required')
    throw new CommandProblem('reconciliation_required');
  if (result.kind === 'problem')
    throw new CommandProblem(result.problem?.code ?? 'request_failed');
  if (result.data === undefined) throw new CommandProblem('request_failed');
  return result.data;
}

export function GenerationComposer({
  embeddedDraftId,
  onSubmitted,
}: { embeddedDraftId?: string; onSubmitted?: () => void } = {}) {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const router = useRouter();
  const { t } = useTranslation('common');
  const emphasis = useEmphasis();
  const capabilities = useDomainQuery('get_generation_capabilities', {});
  const drafts = useDomainQuery('list_generation_drafts', {
    state: 'editing',
    limit: '100',
  });
  const runs = useDomainQuery('list_generation_runs', { limit: '100' });
  const [form, setForm] = useState<Form>(INITIAL_FORM);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [busy, setBusy] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [retryPending, setRetryPending] = useState(false);
  const inFlight = useRef(false);
  const [activeRun, setActiveRun] = useState<string | null>(null);
  const [checkingSubmission, setCheckingSubmission] = useState(false);
  const pending = useRef<(() => Promise<void>) | null>(null);
  const onSubmittedRef = useRef(onSubmitted);
  onSubmittedRef.current = onSubmitted;
  const alive = useRef(true);
  const loadSequence = useRef(0);
  const initialized = useRef(false);
  const selectedDraftId =
    embeddedDraftId ??
    (typeof router.query.draft === 'string' ? router.query.draft : null);
  const submissionStorageKey = `generation-submission:${current.workspaceId}`;
  const options = useMemo(
    () => capabilities.data?.provider_options ?? [],
    [capabilities.data]
  );
  const selectedProvider = options.find((p) => p.id === form.provider);
  const selectedModel = selectedProvider?.models.find(
    (m) => m.id === form.model
  );
  const runRows = useMemo(() => (runs.data?.items ?? []) as Run[], [runs.data]);
  const refreshRuns = runs.refresh;
  const draftRows = (drafts.data?.items ??
    []) as components['schemas']['GenerationDraftDetail'][];

  const finishSubmission = useCallback(
    async (attempt: PendingSubmission) => {
      try {
        const result = commandData(
          await gateway.submitGenerationRun({
            workspaceSlug: current.slug,
            actionInstanceId: attempt.action,
            ifMatch: attempt.revision,
            body: {
              preview_id: attempt.previewId,
              preview_hash: attempt.previewHash,
            },
          })
        );
        if (!alive.current) return;
        try {
          sessionStorage.removeItem(submissionStorageKey);
        } catch {
          /* Storage is optional. */
        }
        setActiveRun(result.generation_run_id);
        onSubmittedRef.current?.();
        setPreview(null);
        await refreshRuns().catch(() => {});
      } catch (e) {
        if (
          e instanceof CommandProblem &&
          e.code !== 'reconciliation_required'
        ) {
          try {
            sessionStorage.removeItem(submissionStorageKey);
          } catch {
            /* Storage is optional. */
          }
        }
        throw e;
      }
    },
    [gateway, current.slug, submissionStorageKey, refreshRuns]
  );

  useEffect(() => {
    let cancelled = false;
    let attempt: PendingSubmission | null = null;
    try {
      attempt = JSON.parse(
        sessionStorage.getItem(submissionStorageKey) ?? 'null'
      );
    } catch {
      /* No recovery hint. */
    }
    if (
      !attempt ||
      typeof attempt.action !== 'string' ||
      typeof attempt.previewId !== 'string' ||
      typeof attempt.previewHash !== 'string' ||
      !Number.isInteger(attempt.revision)
    )
      return;
    const hint = attempt;
    setCheckingSubmission(true);
    const check = async () => {
      const result = commandData(
        await gateway.getGenerationPreview({
          workspaceSlug: current.slug,
          previewId: hint.previewId,
        })
      );
      if (cancelled) return;
      // The browser keeps only a request hint. The server preview supplies
      // the authoritative result; a hint cannot declare a task completed.
      if (
        result.purpose === 'initial_generation' &&
        result.preview_hash === hint.previewHash &&
        result.submitted_run_id
      ) {
        try {
          sessionStorage.removeItem(submissionStorageKey);
        } catch {
          /* Storage is optional. */
        }
        setActiveRun(result.submitted_run_id);
        onSubmittedRef.current?.();
        setError(null);
        setRetryPending(false);
        pending.current = null;
        await refreshRuns().catch(() => {});
      } else {
        pending.current = () => finishSubmission(hint);
        setError('connection_failed');
        setRetryPending(true);
      }
    };
    check()
      .catch(() => {
        if (!cancelled) {
          pending.current = () => finishSubmission(hint);
          setError('connection_failed');
          setRetryPending(true);
        }
      })
      .finally(() => {
        if (!cancelled) setCheckingSubmission(false);
      });
    return () => {
      cancelled = true;
    };
  }, [
    gateway,
    current.slug,
    submissionStorageKey,
    finishSubmission,
    refreshRuns,
  ]);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    if (!capabilities.data || initialized.current || selectedDraftId) return;
    initialized.current = true;
    const provider =
      options.find((p) => p.available && p.id !== 'mock') ??
      options.find((p) => p.id === 'mock');
    if (!provider) return;
    const model =
      provider.models.find(
        (m) => m.available && m.id === provider.default_model
      ) ??
      provider.models.find((m) => m.available) ??
      provider.models[0];
    setForm({
      ...INITIAL_FORM,
      product: capabilities.data.products?.[0]?.id ?? '',
      provider: provider.id,
      model: model?.id ?? '',
      size: model?.image_sizes[0] ?? '1K',
    });
  }, [capabilities.data, options, selectedDraftId]);

  const loadDraft = useCallback(
    async (id: string) => {
      const sequence = ++loadSequence.current;
      setRestoring(true);
      setError(null);
      setPreview(null);
      try {
        const result = await gateway.getGenerationDraft({
          workspaceSlug: current.slug,
          draftId: id,
        });
        const saved = commandData(result);
        if (!alive.current || sequence !== loadSequence.current) return;
        const fmt = saved.format_config ?? {};
        setDraft(saved);
        setForm({
          product: saved.product_id,
          provider: saved.provider,
          model: saved.model,
          ratio: String(fmt.aspect_ratio ?? '1:1'),
          size: String(fmt.image_size ?? '1K'),
          variants: Number(fmt.variants ?? 1),
          prompt: String(saved.selectors?.prompt ?? ''),
        });
        initialized.current = true;
      } catch (e) {
        if (alive.current && sequence === loadSequence.current)
          setError(e instanceof CommandProblem ? e.code : 'connection_failed');
      } finally {
        if (alive.current && sequence === loadSequence.current)
          setRestoring(false);
      }
    },
    [gateway, current.slug]
  );

  useEffect(() => {
    if (selectedDraftId && selectedDraftId !== draft?.id && !pending.current)
      loadDraft(selectedDraftId);
  }, [selectedDraftId, draft?.id, loadDraft]);

  useEffect(() => {
    if (!runRows.some((r) => !TERMINAL.has(r.status))) return;
    const timer = setInterval(() => {
      refreshRuns();
    }, 4000);
    return () => clearInterval(timer);
  }, [runRows, refreshRuns]);

  useEffect(() => {
    if (!preview?.valid_until) return;
    const timer = setTimeout(
      () => {
        setPreview(null);
        setError('preview_expired');
      },
      Math.max(0, Date.parse(preview.valid_until) - Date.now())
    );
    return () => clearTimeout(timer);
  }, [preview]);

  function change(patch: Partial<Form>) {
    setForm((previous) => ({ ...previous, ...patch }));
    setPreview(null);
    setError(null);
  }
  function selectProvider(id: string) {
    const provider = options.find((p) => p.id === id);
    const model =
      provider?.models.find(
        (m) => m.available && m.id === provider.default_model
      ) ??
      provider?.models.find((m) => m.available) ??
      provider?.models[0];
    change({
      provider: id,
      model: model?.id ?? '',
      size: model?.image_sizes[0] ?? '1K',
    });
  }
  async function perform(work: () => Promise<void>) {
    if (inFlight.current) return;
    inFlight.current = true;
    pending.current = work;
    setBusy(true);
    setError(null);
    setRetryPending(false);
    try {
      await work();
      pending.current = null;
    } catch (e) {
      if (!alive.current) return;
      if (e instanceof CommandProblem) {
        setError(e.code);
        if (e.code === 'reconciliation_required') setRetryPending(true);
        else pending.current = null;
      } else {
        setError('connection_failed');
        setRetryPending(true);
      }
    } finally {
      inFlight.current = false;
      if (alive.current) setBusy(false);
    }
  }
  async function saveAndPreview(makePreview: boolean) {
    const body = {
      product_id: form.product,
      provider: form.provider,
      model: form.model,
      execution_mode: selectedProvider?.execution_mode ?? 'byok',
      combination_strategy: { mode: 'single' },
      selectors: { ...(draft?.selectors ?? {}), prompt: form.prompt },
      format_config: {
        aspect_ratio: form.ratio,
        image_size: form.size,
        variants: form.variants,
      },
    };
    // Both IDs remain fixed across a network retry of this logical action.
    const saveAction = crypto.randomUUID(),
      previewAction = crypto.randomUUID();
    let saved = draft;
    let saveFinished = false;
    await perform(async () => {
      if (!saveFinished) {
        saved = commandData(
          await (draft
            ? gateway.updateGenerationDraft({
                workspaceSlug: current.slug,
                actionInstanceId: saveAction,
                draftId: draft.id,
                ifMatch: draft.revision,
                body,
              })
            : gateway.createGenerationDraft({
                workspaceSlug: current.slug,
                actionInstanceId: saveAction,
                body,
              }))
        );
        saveFinished = true;
      }
      if (!alive.current || !saved) return;
      setDraft(saved);
      if (!embeddedDraftId) {
        await router.replace(
          {
            pathname: router.pathname,
            query: { ...router.query, slug: current.slug, draft: saved.id },
          },
          undefined,
          { shallow: true }
        );
      }
      await drafts.refresh().catch(() => {});
      if (makePreview) {
        const frozen = commandData(
          await gateway.createGenerationPreview({
            workspaceSlug: current.slug,
            actionInstanceId: previewAction,
            draftId: saved.id,
            ifMatch: saved.revision,
          })
        );
        if (alive.current) setPreview(frozen);
      }
    });
  }
  async function submit() {
    if (!preview || !draft || inFlight.current) return;
    const attempt = {
      action: crypto.randomUUID(),
      previewId: preview.id,
      previewHash: preview.preview_hash,
      revision: preview.draft_revision,
    };
    try {
      sessionStorage.setItem(submissionStorageKey, JSON.stringify(attempt));
    } catch {
      /* Same-page retry still works. */
    }
    await perform(() => finishSubmission(attempt));
  }
  const locked = busy || restoring || retryPending || checkingSubmission;
  const canSave = Boolean(
    form.product && form.provider && form.model && form.prompt.trim()
  );
  const canPreview = canSave && selectedModel?.available;
  const submitAllowed =
    preview?.allowed_actions?.some((a) => a.action === 'submit' && a.allowed) &&
    Boolean(
      preview.valid_until && Date.parse(preview.valid_until) > Date.now()
    );
  const reason =
    selectedModel?.unavailable_reason ?? selectedProvider?.unavailable_reason;
  const errorText = (code: string) =>
    t(`generation-reason-${code}`, {
      defaultValue: t('generation-request-failed'),
    });

  if (capabilities.isLoading && !capabilities.data)
    return (
      <p role="status" className="py-12 text-gray-500">
        {t('generation-loading')}
      </p>
    );
  if (capabilities.problem || (!capabilities.data && !capabilities.isLoading))
    return (
      <div role="alert" className="mt-6 space-y-3">
        <p>{t('generation-load-failed')}</p>
        <button
          className="btn btn-outline"
          onClick={() => capabilities.refresh()}
        >
          {t('generation-reload')}
        </button>
      </div>
    );
  return (
    <div className="mt-6 space-y-10">
      <p className="max-w-2xl text-sm leading-6 text-gray-500">
        {t('generation-intro')}
      </p>
      {draft?.source_asset_version_id && (
        <p className="max-w-2xl text-sm text-gray-600">
          {t('generation-edit-source-note')}
        </p>
      )}
      {error && (
        <div
          role="alert"
          className="rounded-lg bg-amber-50 p-4 text-sm text-amber-900"
        >
          <p>{errorText(error)}</p>
          {retryPending && (
            <button
              className="btn btn-sm mt-3"
              disabled={busy}
              onClick={() => pending.current && perform(pending.current)}
            >
              {t('generation-retry-request')}
            </button>
          )}
        </div>
      )}
      <div className="grid gap-8 xl:grid-cols-[minmax(0,1fr)_22rem]">
        <form
          className="space-y-6"
          onSubmit={(e) => {
            e.preventDefault();
            if (canPreview && !locked) saveAndPreview(true);
          }}
        >
          <fieldset disabled={locked} className="space-y-6">
            <div hidden={!!embeddedDraftId}>
              <label
                htmlFor="generation-product"
                className="mb-2 block text-sm font-medium"
              >
                {t('generation-product')}
              </label>
              <select
                id="generation-product"
                className={INPUT}
                value={form.product}
                disabled={!!embeddedDraftId}
                onChange={(e) => change({ product: e.target.value })}
              >
                <option value="">{t('generation-product-placeholder')}</option>
                {capabilities.data?.products?.map((product) => (
                  <option key={product.id} value={product.id}>
                    {product.name}
                  </option>
                ))}
              </select>
              {!capabilities.data?.products?.length && (
                <p className="mt-2 text-sm text-gray-500">
                  {t('generation-no-products')}{' '}
                  <Link
                    className="underline"
                    href={`/w/${current.slug}/library`}
                  >
                    {t('generation-open-library')}
                  </Link>
                </p>
              )}
            </div>
            <EmbeddedFold
              folded={!!embeddedDraftId}
              summary={t('generation-settings-and-prompt')}
            >
              <fieldset>
                <legend className="mb-2 text-sm font-medium">
                  {t('generation-provider')}
                </legend>
                <div className="grid gap-3 sm:grid-cols-2">
                  {options.map((provider) => (
                    <label
                      key={provider.id}
                      className={`cursor-pointer rounded-lg border p-4 transition-colors focus-within:ring-2 focus-within:ring-gray-400 ${provider.id === form.provider ? 'border-gray-900 bg-gray-50' : 'border-gray-200 hover:border-gray-400'}`}
                    >
                      <div className="flex items-center gap-3">
                        <input
                          type="radio"
                          name="generation-provider"
                          value={provider.id}
                          checked={provider.id === form.provider}
                          onChange={() => selectProvider(provider.id)}
                        />
                        <span className="text-sm font-medium">
                          {provider.label}
                        </span>
                      </div>
                      <p className="mt-2 pl-6 text-xs text-gray-500">
                        {provider.id === 'mock'
                          ? t('generation-free-mock')
                          : provider.available
                            ? t('generation-ready')
                            : errorText(
                                provider.unavailable_reason ??
                                  'price_unavailable'
                              )}
                      </p>
                    </label>
                  ))}
                </div>
              </fieldset>
              <div>
                <label
                  htmlFor="generation-model"
                  className="mb-2 block text-sm font-medium"
                >
                  {t('generation-model')}
                </label>
                <select
                  id="generation-model"
                  className={INPUT}
                  value={form.model}
                  onChange={(e) => {
                    const model = selectedProvider?.models.find(
                      (m) => m.id === e.target.value
                    );
                    change({
                      model: e.target.value,
                      size: model?.image_sizes[0] ?? '1K',
                    });
                  }}
                >
                  {selectedProvider?.models.map((model) => (
                    <option key={model.id} value={model.id}>
                      {model.id}
                    </option>
                  ))}
                </select>
              </div>
              <div className="grid grid-cols-3 gap-3">
                <div>
                  <label
                    htmlFor="generation-ratio"
                    className="mb-2 block text-sm font-medium"
                  >
                    {t('generation-ratio')}
                  </label>
                  <select
                    id="generation-ratio"
                    className={INPUT}
                    value={form.ratio}
                    onChange={(e) => change({ ratio: e.target.value })}
                  >
                    {selectedProvider?.aspect_ratios.map((ratio) => (
                      <option key={ratio}>{ratio}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label
                    htmlFor="generation-size"
                    className="mb-2 block text-sm font-medium"
                  >
                    {t(
                      form.provider === 'openai'
                        ? 'generation-quality'
                        : 'generation-size'
                    )}
                  </label>
                  <select
                    id="generation-size"
                    className={INPUT}
                    value={form.size}
                    onChange={(e) => change({ size: e.target.value })}
                  >
                    {(selectedModel?.image_sizes.length
                      ? selectedModel.image_sizes
                      : ['1K', '2K']
                    ).map((size) => (
                      <option key={size} value={size}>
                        {form.provider === 'openai'
                          ? t(
                              size === '1K'
                                ? 'generation-quality-standard'
                                : 'generation-quality-high'
                            )
                          : size}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label
                    htmlFor="generation-variants"
                    className="mb-2 block text-sm font-medium"
                  >
                    {t('generation-variants')}
                  </label>
                  <select
                    id="generation-variants"
                    className={INPUT}
                    value={form.variants}
                    onChange={(e) =>
                      change({ variants: Number(e.target.value) })
                    }
                  >
                    {[1, 2, 3, 4].map((n) => (
                      <option key={n} value={n}>
                        {n}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              <div>
                <label
                  htmlFor="generation-prompt"
                  className="mb-2 block text-sm font-medium"
                >
                  {t('generation-prompt')}
                </label>
                <textarea
                  id="generation-prompt"
                  className={INPUT}
                  rows={5}
                  maxLength={4000}
                  value={form.prompt}
                  placeholder={t('generation-prompt-placeholder')}
                  onChange={(e) => change({ prompt: e.target.value })}
                />
                <p className="mt-2 text-xs text-gray-500">
                  {t('generation-prompt-help')}
                </p>
              </div>
            </EmbeddedFold>
          </fieldset>
          <div className="flex flex-wrap gap-3">
            <button
              type="submit"
              className={
                preview
                  ? 'btn btn-outline'
                  : emphasis(
                      'generate',
                      'btn bg-gray-900 text-white hover:bg-gray-800',
                      'btn btn-outline'
                    )
              }
              disabled={!canPreview || locked}
            >
              {busy ? t('generation-working') : t('generation-preview')}
            </button>
            <button
              type="button"
              className="btn btn-ghost"
              disabled={!canSave || locked}
              onClick={() => saveAndPreview(false)}
            >
              {t('generation-save-draft')}
            </button>
          </div>
        </form>
        <aside className="space-y-5 border-t border-gray-200 pt-6 xl:border-l xl:border-t-0 xl:pl-8 xl:pt-0">
          <h2 className="text-lg font-semibold">
            {t('generation-confirmation')}
          </h2>
          {selectedProvider?.execution_mode === 'mock' && (
            <p className="rounded-lg bg-amber-50 p-3 text-sm leading-6 text-amber-900">
              {t('generation-mock-notice')}
            </p>
          )}
          {reason && (
            <p className="text-sm leading-6 text-amber-800">
              {errorText(reason)}{' '}
              {current.role === 'MEMBER' && t('generation-owner-help')}
            </p>
          )}
          {preview ? (
            <div className="space-y-4" aria-live="polite">
              <dl className="space-y-3 text-sm">
                {preview.image_tool && (
                  <div>
                    <dt className="text-gray-500">
                      {t('marketing-image-tools-operation')}
                    </dt>
                    <dd>
                      {t(`marketing-image-tools-${preview.image_tool.kind}`)} ·{' '}
                      {t('marketing-image-tools-output', {
                        width:
                          preview.image_tool.source_width *
                          (preview.image_tool.scale || 1),
                        height:
                          preview.image_tool.source_height *
                          (preview.image_tool.scale || 1),
                      })}
                    </dd>
                  </div>
                )}
                <div>
                  <dt className="text-gray-500">{t('generation-provider')}</dt>
                  <dd>
                    {preview.provider} · {preview.model}
                  </dd>
                </div>
                <div>
                  <dt className="text-gray-500">{t('generation-variants')}</dt>
                  <dd>{preview.quantity}</dd>
                </div>
                <div>
                  <dt className="text-gray-500">
                    {t('generation-maximum-cost')}
                  </dt>
                  <dd className="font-mono text-2xl tabular-nums">
                    {preview.currency} {preview.selection_maximum_cost}
                  </dd>
                </div>
              </dl>
              <p className="text-xs leading-5 text-gray-500">
                {t('generation-cost-help')}
              </p>
              <button
                className={emphasis(
                  'generate',
                  'btn w-full bg-gray-900 text-white hover:bg-gray-800',
                  'btn btn-outline w-full'
                )}
                disabled={!submitAllowed || locked}
                onClick={submit}
              >
                {t('generation-confirm-submit')}
              </button>
            </div>
          ) : (
            <p className="text-sm leading-6 text-gray-500">
              {t('generation-preview-help')}
            </p>
          )}
          {draft && (
            <p className="text-xs text-gray-500" role="status">
              {t('generation-draft-saved')}
            </p>
          )}
          <div
            className="border-t border-gray-200 pt-5"
            hidden={!!embeddedDraftId}
          >
            <label
              htmlFor="generation-saved-draft"
              className="mb-2 block text-sm font-medium"
            >
              {t('generation-open-draft')}
            </label>
            <select
              id="generation-saved-draft"
              className={INPUT}
              disabled={locked || !!embeddedDraftId}
              value={draft?.id ?? ''}
              onChange={(e) => {
                if (e.target.value)
                  router.replace(
                    {
                      pathname: router.pathname,
                      query: {
                        ...router.query,
                        slug: current.slug,
                        draft: e.target.value,
                      },
                    },
                    undefined,
                    { shallow: true }
                  );
              }}
            >
              <option value="">{t('generation-select-draft')}</option>
              {draftRows.map((row) => (
                <option key={row.id} value={row.id}>
                  {row.provider} · {row.model} · {row.id.slice(-6)}
                </option>
              ))}
            </select>
          </div>
        </aside>
      </div>
      <EmbeddedFold
        folded={!!embeddedDraftId}
        summary={t('generation-runs-workspace')}
      >
        <section className="border-t border-gray-200 pt-6">
          <div className="mb-4 flex items-center justify-between">
            <h2 className="text-lg font-semibold">{t('generation-runs')}</h2>
            <button
              className="btn btn-sm btn-ghost"
              onClick={() => refreshRuns()}
            >
              {t('generation-reload')}
            </button>
          </div>
          {runs.problem && (
            <p role="alert" className="text-sm text-amber-800">
              {t('generation-load-failed')}
            </p>
          )}
          {!runRows.length && !runs.isLoading && (
            <p className="py-6 text-sm text-gray-500">
              {t('generation-no-runs')}
            </p>
          )}
          <div className="divide-y divide-gray-200">
            {runRows.map((run) => (
              <GenerationRunRow
                key={run.id}
                run={run}
                highlighted={run.id === activeRun}
              />
            ))}
          </div>
        </section>
      </EmbeddedFold>
    </div>
  );
}

/**
 * Inside a test page (J-32) model settings, the full prompt and the
 * Workspace-wide task history are folded; the standalone workbench shows them.
 */
function EmbeddedFold({
  folded,
  summary,
  children,
}: {
  folded: boolean;
  summary: string;
  children: ReactNode;
}) {
  if (!folded) return <>{children}</>;
  return (
    <details className="rounded-lg border border-gray-200 p-4">
      <summary className="cursor-pointer text-sm font-medium">
        {summary}
      </summary>
      <div className="mt-4 space-y-6">{children}</div>
    </details>
  );
}

function GenerationRunRow({
  run,
  highlighted,
}: {
  run: Run;
  highlighted: boolean;
}) {
  const { current } = useWorkspace();
  const { t } = useTranslation('common');
  const gateway = useDomainGateway();
  const [expanded, setExpanded] = useState(highlighted);
  const [downloadError, setDownloadError] = useState(false);
  const assets = useDomainQuery(
    'list_generation_assets',
    { run_id: run.id, limit: '100' },
    { enabled: expanded }
  );
  const items = useDomainQuery(
    'list_generation_run_items',
    { run_id: run.id, limit: '100' },
    { enabled: expanded }
  );
  const refreshAssets = assets.refresh,
    refreshItems = items.refresh;
  useEffect(() => {
    if (!expanded || TERMINAL.has(run.status)) return;
    const timer = setInterval(() => {
      refreshAssets();
      refreshItems();
    }, 4000);
    return () => clearInterval(timer);
  }, [expanded, run.status, refreshAssets, refreshItems]);
  async function download(id: string) {
    setDownloadError(false);
    try {
      const result = commandData(
        await gateway.createGenerationAssetVersionDownloadIntent({
          workspaceSlug: current.slug,
          versionId: id,
          actionInstanceId: crypto.randomUUID(),
        })
      );
      if (!result.download_grant?.url) throw new Error('missing grant');
      const anchor = document.createElement('a');
      anchor.href = result.download_grant.url;
      anchor.rel = 'noopener';
      anchor.download = `${id}.png`;
      anchor.click();
    } catch {
      setDownloadError(true);
    }
  }
  return (
    <article className="py-5">
      <button
        className="flex w-full flex-wrap items-center justify-between gap-3 rounded text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-gray-500"
        aria-expanded={expanded}
        onClick={() => setExpanded(!expanded)}
      >
        <span>
          <span className="block text-sm font-medium">
            {run.provider} · {run.model}
          </span>
        </span>
        <span className="flex items-center gap-3 text-sm">
          <span
            className={
              run.execution_mode === 'mock' ? 'text-amber-700' : 'text-gray-500'
            }
          >
            {t(
              run.execution_mode === 'mock'
                ? 'generation-free-mock'
                : 'generation-byok'
            )}
          </span>
          <span>
            {t(`generation-state-${run.status}`, { defaultValue: run.status })}
          </span>
          <span className="tabular-nums">
            {run.done}/{run.total}
          </span>
        </span>
      </button>
      {expanded && (
        <div className="mt-4 space-y-3 rounded-lg bg-gray-50 p-4 text-sm">
          {run.status === 'uncertain' && (
            <p className="text-amber-800">{t('generation-uncertain-help')}</p>
          )}
          {(
            items.data?.items as
              | components['schemas']['GenerationRunItemDetail'][]
              | undefined
          )?.map((item: components['schemas']['GenerationRunItemDetail']) => (
            <p key={item.id}>
              {t('generation-variation-number', {
                number: item.variant_index + 1,
              })}{' '}
              ·{' '}
              {t(`generation-state-${item.state}`, {
                defaultValue: item.state,
              })}
            </p>
          ))}
          {assets.data?.items?.map(
            (asset: components['schemas']['AssetVersionSummary']) => (
              <div
                key={asset.id}
                className="flex items-center justify-between gap-3"
              >
                <span>
                  {asset.provider} · {asset.model}
                </span>
                <button
                  className="btn btn-sm btn-outline"
                  onClick={() => download(asset.id)}
                >
                  {t('generation-download')}
                </button>
              </div>
            )
          )}
          {downloadError && (
            <p role="alert" className="text-amber-800">
              {t('generation-download-failed')}
            </p>
          )}
          <Link
            href={`/w/${current.slug}/assets`}
            className="inline-block text-sm underline"
          >
            {t('generation-open-assets')}
          </Link>
          <TechnicalDetails facts={[[t('generation-run-id'), run.id]]} />
        </div>
      )}
    </article>
  );
}
