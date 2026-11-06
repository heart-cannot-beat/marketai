import {
  useEffect,
  useMemo,
  useState,
  type FormEvent,
  type ReactNode,
} from 'react';
import { useTranslation } from 'next-i18next';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { useGenerationDefaults } from '@/hooks/test-plans/useGenerationDefaults';
import { newActionId } from '@/components/generation/shared/newActionId';
import {
  PLAN_SCALES,
  problemKey,
  type GenerationDefaultsView,
  type PlanScale,
} from '@/lib/test-plans/plan-model';

type Mode = 'mock' | 'byok';

interface ModelOption {
  id: string;
  name: string;
  aspectRatios: string[];
  maxVariants: number;
}

/** The registry's providers for each mode (`app.plans.defaults._validate`:
 * mock needs the mock provider, byok a real one -- OpenAI is the only BYOK
 * image provider today). */
const MODE_PROVIDER: Record<Mode, string> = { mock: 'mock', byok: 'openai' };

const FALLBACK_MODELS: Record<Mode, ModelOption[]> = {
  mock: [
    {
      id: 'mock-v1',
      name: 'Mock Generator v1',
      aspectRatios: ['1:1', '4:3', '16:9', '9:16'],
      maxVariants: 4,
    },
  ],
  byok: [
    {
      id: 'gpt-image-2',
      name: 'GPT Image 2',
      aspectRatios: ['1:1', '4:5', '9:16', '2:3', '3:4', '16:9', '3:2', '4:3'],
      maxVariants: 4,
    },
  ],
};

/** Placement-first ratios (contract §4: Feed 1:1, Story 9:16). */
const PREFERRED_RATIOS = ['1:1', '9:16', '4:5'];

/** Models a mode can use, from `get_generation_capabilities` when it lists
 * them, else the known registry entries. */
export function modelOptions(
  capabilities: { models?: Record<string, unknown> } | undefined,
  mode: Mode
): ModelOption[] {
  const provider = MODE_PROVIDER[mode];
  const raw = capabilities?.models?.[provider];
  if (!raw || typeof raw !== 'object') return FALLBACK_MODELS[mode];
  const options = Object.entries(raw as Record<string, any>).map(
    ([id, info]) => {
      const limits = (info?.format_limits ?? {}) as Record<string, unknown>;
      return {
        id,
        name: typeof info?.display_name === 'string' ? info.display_name : id,
        aspectRatios: Array.isArray(limits.aspect_ratios)
          ? (limits.aspect_ratios as string[])
          : ['1:1'],
        maxVariants: Number(limits.max_variants_per_combination) || 1,
      };
    }
  );
  return options.length > 0 ? options : FALLBACK_MODELS[mode];
}

export function aspectChoices(model: ModelOption | undefined): string[] {
  const supported = model?.aspectRatios ?? ['1:1'];
  const preferred = PREFERRED_RATIOS.filter((ratio) =>
    supported.includes(ratio)
  );
  return preferred.length > 0 ? preferred : supported.slice(0, 3);
}

export function aspectKey(ratio: string): string {
  return `defaults-aspect-${ratio.replace(':', 'x')}`;
}

interface FormState {
  mode: Mode;
  model: string;
  aspectRatio: string;
  variants: Record<PlanScale, number>;
}

function formFrom(defaults: GenerationDefaultsView): FormState {
  return {
    mode: defaults.execution_mode === 'byok' ? 'byok' : 'mock',
    model: defaults.model,
    aspectRatio: defaults.aspect_ratio,
    variants: {
      sample: Number(defaults.variants?.sample) || 1,
      small: Number(defaults.variants?.small) || 2,
      full: Number(defaults.variants?.full) || 2,
    },
  };
}

/**
 * Settings →「生成默认值」(contract §4, Issue #165): execution mode, model,
 * aspect ratio and variants per scale that every front-flow test Run
 * freezes when it is submitted. Owners edit (`PUT`, If-Match = revision,
 * "0" before the first save); Members see the same values read-only.
 */
export function GenerationDefaultsSection() {
  const { t } = useTranslation('tests');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const canEdit = current.role === 'OWNER';
  const { defaults, isLoading, problem, refresh } = useGenerationDefaults();
  const capabilities = useDomainQuery(
    'get_generation_capabilities',
    undefined,
    {
      enabled: canEdit,
    }
  );
  const [form, setForm] = useState<FormState | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [saveProblem, setSaveProblem] = useState<string | null>(null);

  useEffect(() => {
    if (defaults) setForm(formFrom(defaults));
  }, [defaults]);

  const models = useMemo(
    () =>
      modelOptions(
        capabilities.data as { models?: Record<string, unknown> } | undefined,
        form?.mode ?? 'mock'
      ),
    [capabilities.data, form?.mode]
  );
  const selectedModel =
    models.find((model) => model.id === form?.model) ?? models[0];
  const ratios = aspectChoices(selectedModel);
  const maxVariants = selectedModel?.maxVariants ?? 1;

  if (isLoading && !defaults) {
    return (
      <SectionFrame>
        <p className="text-sm text-base-content/60" role="status">
          {t('defaults-loading')}
        </p>
      </SectionFrame>
    );
  }
  if (!defaults || !form) {
    return (
      <SectionFrame>
        <div className="alert alert-error text-sm" role="alert">
          <span>{t('defaults-error')}</span>
          {problem && (
            <button type="button" className="btn btn-sm" onClick={refresh}>
              {t('retry')}
            </button>
          )}
        </div>
      </SectionFrame>
    );
  }

  const dirty =
    JSON.stringify(form) !== JSON.stringify(formFrom(defaults)) ||
    form.model !== selectedModel?.id;

  function update(patch: Partial<FormState>) {
    setSaved(false);
    setSaveProblem(null);
    setForm((prev) => (prev ? { ...prev, ...patch } : prev));
  }

  function changeMode(mode: Mode) {
    const options = modelOptions(
      capabilities.data as { models?: Record<string, unknown> } | undefined,
      mode
    );
    const model = options[0];
    const ratioList = aspectChoices(model);
    update({
      mode,
      model: model?.id ?? '',
      aspectRatio: ratioList.includes(form!.aspectRatio)
        ? form!.aspectRatio
        : ratioList[0],
    });
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (!form || !defaults || !selectedModel) return;
    setSaving(true);
    setSaveProblem(null);
    const clamp = (value: number) =>
      Math.min(Math.max(1, Math.round(value)), maxVariants);
    const result = await gateway.updateGenerationDefaults({
      workspaceSlug: current.slug,
      actionInstanceId: newActionId(),
      ifMatch: defaults.revision,
      body: {
        provider: MODE_PROVIDER[form.mode],
        model: selectedModel.id,
        execution_mode: form.mode,
        aspect_ratio: ratios.includes(form.aspectRatio)
          ? form.aspectRatio
          : ratios[0],
        variants: {
          sample: clamp(form.variants.sample),
          small: clamp(form.variants.small),
          full: clamp(form.variants.full),
        },
      },
    });
    setSaving(false);
    if (result.kind === 'ok') {
      setSaved(true);
      await refresh();
      return;
    }
    const code = result.kind === 'problem' ? result.problem.code : '';
    setSaveProblem(t(problemKey(code)));
    if (result.kind !== 'problem' || result.status === 409) await refresh();
  }

  const readOnly = !canEdit;

  return (
    <SectionFrame
      badge={
        defaults.source === 'workspace'
          ? t('defaults-source-workspace')
          : t('defaults-source-computed')
      }
    >
      {readOnly && (
        <p className="text-sm text-base-content/70">
          {t('defaults-read-only')}
        </p>
      )}
      <form className="space-y-5" onSubmit={onSubmit}>
        <fieldset disabled={readOnly || saving} className="space-y-5">
          <div>
            <p className="mb-2 text-sm font-medium">{t('defaults-mode')}</p>
            <div
              className="flex flex-wrap gap-x-6 gap-y-2"
              role="radiogroup"
              aria-label={t('defaults-mode')}
            >
              {(['mock', 'byok'] as Mode[]).map((mode) => (
                <label
                  key={mode}
                  className="flex cursor-pointer items-center gap-2"
                >
                  <input
                    type="radio"
                    className="radio radio-sm"
                    name="defaults-mode"
                    value={mode}
                    checked={form.mode === mode}
                    onChange={() => changeMode(mode)}
                  />
                  <span className="text-sm">{t(`defaults-mode-${mode}`)}</span>
                </label>
              ))}
            </div>
            <p className="mt-1 text-xs text-base-content/60">
              {defaults.credential_bound
                ? t('defaults-key-bound')
                : t('defaults-key-missing')}
            </p>
          </div>

          {form.mode === 'byok' && (
            <label className="form-control max-w-xs">
              <span className="label-text mb-1 text-sm font-medium">
                {t('defaults-model')}
              </span>
              <select
                className="select select-bordered select-sm"
                value={selectedModel?.id ?? ''}
                onChange={(event) => update({ model: event.target.value })}
              >
                {models.map((model) => (
                  <option key={model.id} value={model.id}>
                    {model.name}
                  </option>
                ))}
              </select>
            </label>
          )}

          <div>
            <p className="mb-2 text-sm font-medium">{t('defaults-aspect')}</p>
            <div
              className="flex flex-wrap gap-x-6 gap-y-2"
              role="radiogroup"
              aria-label={t('defaults-aspect')}
            >
              {ratios.map((ratio) => (
                <label
                  key={ratio}
                  className="flex cursor-pointer items-center gap-2"
                >
                  <input
                    type="radio"
                    className="radio radio-sm"
                    name="defaults-aspect"
                    value={ratio}
                    checked={form.aspectRatio === ratio}
                    onChange={() => update({ aspectRatio: ratio })}
                  />
                  <span className="text-sm">
                    {t(aspectKey(ratio), {
                      defaultValue: ratio,
                    })}
                  </span>
                </label>
              ))}
            </div>
          </div>

          <div>
            <p className="mb-2 text-sm font-medium">{t('defaults-variants')}</p>
            <div
              className="flex flex-wrap gap-4"
              role="group"
              aria-label={t('defaults-variants')}
            >
              {PLAN_SCALES.map((scale) => (
                <label key={scale} className="form-control w-32">
                  <span className="label-text mb-1 text-sm">
                    {t(`defaults-variants-${scale}`)}
                  </span>
                  <select
                    className="select select-bordered select-sm"
                    value={Math.min(form.variants[scale], maxVariants)}
                    onChange={(event) =>
                      update({
                        variants: {
                          ...form.variants,
                          [scale]: Number(event.target.value),
                        },
                      })
                    }
                  >
                    {Array.from({ length: maxVariants }, (_, i) => i + 1).map(
                      (count) => (
                        <option key={count} value={count}>
                          {t('defaults-variants-value', { count })}
                        </option>
                      )
                    )}
                  </select>
                </label>
              ))}
            </div>
          </div>
        </fieldset>

        {saveProblem && (
          <div className="alert alert-error text-sm" role="alert">
            <span>{saveProblem}</span>
          </div>
        )}

        {canEdit && (
          <div className="flex flex-wrap items-center gap-3">
            <button
              type="submit"
              className="btn btn-primary btn-sm"
              disabled={saving || (!dirty && defaults.source === 'workspace')}
            >
              {saving ? t('defaults-saving') : t('defaults-save')}
            </button>
            {dirty && !saving && (
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={() => {
                  setForm(formFrom(defaults));
                  setSaveProblem(null);
                }}
              >
                {t('defaults-reset')}
              </button>
            )}
            {saved && !dirty && (
              <span className="text-sm text-success" role="status">
                {t('defaults-saved')}
              </span>
            )}
          </div>
        )}
      </form>
    </SectionFrame>
  );
}

function SectionFrame({
  children,
  badge,
}: {
  children: ReactNode;
  badge?: string;
}) {
  const { t } = useTranslation('tests');
  return (
    <section
      className="rounded-box border border-base-300 bg-base-100 p-5"
      aria-labelledby="generation-defaults-title"
      data-testid="generation-defaults-section"
    >
      <div className="mb-4 flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="generation-defaults-title" className="text-lg font-semibold">
          {t('defaults-title')}
        </h2>
        {badge && <span className="badge badge-ghost badge-sm">{badge}</span>}
      </div>
      <p className="mb-4 max-w-prose text-sm text-base-content/70">
        {t('defaults-intro')}
      </p>
      <div className="space-y-4">{children}</div>
    </section>
  );
}
