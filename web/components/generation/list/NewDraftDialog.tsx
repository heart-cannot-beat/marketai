import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { useTranslation } from 'next-i18next';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import { useGenerationNav } from '@/components/generation/shared/useGenerationNav';
import { newActionId } from '@/components/generation/shared/newActionId';
import { ProductPicker } from '@/components/products/ProductPicker';
import { useProductDirectory } from '@/hooks/products/useProductDirectory';
import { errorMessage } from '@/lib/i18n/domain-errors';

interface NewDraftDialogProps {
  open: boolean;
  onClose: () => void;
  /** Preselects a product (the product home's "开始新测试"). */
  initialProductId?: string | null;
}

type ModelInfo = {
  display_name?: string;
  format_limits?: Record<string, unknown>;
};

/**
 * Collects product, provider, model and execution mode, then creates a
 * Generation draft (`create_generation_draft`) and navigates to it.
 *
 * Provider/model/mode options are sourced from `get_generation_capabilities`
 * (`GenerationCapabilities`: `providers: string[]`, `models: dict[provider
 * -> dict[model_id -> {display_name, format_limits}]]`, `modes: string[]`).
 * The product is picked from its card (`list_products`, Issue #162) -- the
 * user never types or sees a `prod_` id.
 */
export function NewDraftDialog({
  open,
  onClose,
  initialProductId = null,
}: NewDraftDialogProps) {
  const { t } = useTranslation('common');
  const { navigateToDraft } = useGenerationNav();
  const { data: capabilities, isLoading: capabilitiesLoading } = useDomainQuery(
    'get_generation_capabilities'
  );
  const { execute, isPending, problem } = useDomainCommand(
    'create_generation_draft'
  );

  const [productId, setProductId] = useState(initialProductId ?? '');
  const { products } = useProductDirectory({ enabled: open });

  useEffect(() => {
    if (initialProductId) setProductId(initialProductId);
  }, [initialProductId]);

  useEffect(() => {
    // One usable product: choose it, so the first draft needs no picking.
    if (productId) return;
    const ready = products.filter((product) => product.ready);
    if (ready.length === 1) setProductId(ready[0].product_id);
  }, [productId, products]);
  const [provider, setProvider] = useState('');
  const [model, setModel] = useState('');
  const [executionMode, setExecutionMode] = useState('');

  const providers = useMemo(
    () => capabilities?.providers ?? [],
    [capabilities]
  );
  const modes = useMemo(() => capabilities?.modes ?? [], [capabilities]);

  const modelsForProvider = useMemo(() => {
    if (!provider) return {} as Record<string, ModelInfo>;
    const raw = capabilities?.models?.[provider];
    return (raw ?? {}) as Record<string, ModelInfo>;
  }, [capabilities, provider]);

  useEffect(() => {
    // Controlled Mock is the first-value default, but only when the Domain
    // advertises the complete capability. Never overwrite an existing choice:
    // credential readiness can revalidate while this dialog is open.
    if (provider || model || executionMode) return;
    if (!providers.includes('mock') || !modes.includes('mock')) return;
    const mockModels = capabilities?.models?.mock ?? {};
    const defaultModel = Object.prototype.hasOwnProperty.call(
      mockModels,
      'mock-v1'
    )
      ? 'mock-v1'
      : Object.keys(mockModels)[0];
    if (!defaultModel) return;
    setProvider('mock');
    setModel(defaultModel);
    setExecutionMode('mock');
  }, [capabilities, executionMode, model, modes, provider, providers]);

  const canSubmit =
    productId.trim() !== '' &&
    provider !== '' &&
    model !== '' &&
    executionMode !== '' &&
    !isPending;

  if (!open) return null;

  function handleProviderChange(nextProvider: string) {
    setProvider(nextProvider);
    setModel('');
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSubmit) return;

    const outcome = await execute({
      actionInstanceId: newActionId(),
      body: {
        product_id: productId.trim(),
        provider,
        model,
        execution_mode: executionMode,
      },
    });

    if (outcome.kind === 'success') {
      onClose();
      navigateToDraft(outcome.data.id);
    }
  }

  return (
    <dialog
      open
      className="modal modal-open"
      aria-label={t('generation-new-draft-title')}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="modal-box" onClick={(event) => event.stopPropagation()}>
        <h3 className="text-lg font-bold">{t('generation-new-draft-title')}</h3>

        {problem && (
          <div className="alert alert-error mt-4" role="alert">
            <span>
              {t('generation-new-draft-error')}: {errorMessage(t, problem)}
            </span>
          </div>
        )}

        <form onSubmit={handleSubmit}>
          <div className="mt-4">
            <ProductPicker
              id="new-draft-product"
              value={productId}
              onChange={setProductId}
              disabled={isPending}
            />
          </div>

          <div className="form-control mt-2">
            <label className="label" htmlFor="new-draft-provider">
              <span className="label-text">
                {t('generation-new-draft-provider-label')}
              </span>
            </label>
            <select
              id="new-draft-provider"
              className="select select-bordered"
              value={provider}
              onChange={(event) => handleProviderChange(event.target.value)}
              disabled={capabilitiesLoading}
            >
              <option value="">
                {t('generation-new-draft-provider-placeholder')}
              </option>
              {providers.map((providerId) => (
                <option key={providerId} value={providerId}>
                  {providerId}
                </option>
              ))}
            </select>
          </div>

          <div className="form-control mt-2">
            <label className="label" htmlFor="new-draft-model">
              <span className="label-text">
                {t('generation-new-draft-model-label')}
              </span>
            </label>
            <select
              id="new-draft-model"
              className="select select-bordered"
              value={model}
              onChange={(event) => setModel(event.target.value)}
              disabled={!provider}
            >
              <option value="">
                {t('generation-new-draft-model-placeholder')}
              </option>
              {Object.entries(modelsForProvider).map(([modelId, info]) => (
                <option key={modelId} value={modelId}>
                  {info?.display_name ?? modelId}
                </option>
              ))}
            </select>
          </div>

          <div className="form-control mt-2">
            <label className="label" htmlFor="new-draft-mode">
              <span className="label-text">
                {t('generation-new-draft-mode-label')}
              </span>
            </label>
            <select
              id="new-draft-mode"
              className="select select-bordered"
              value={executionMode}
              onChange={(event) => setExecutionMode(event.target.value)}
              disabled={capabilitiesLoading}
            >
              <option value="">
                {t('generation-new-draft-mode-placeholder')}
              </option>
              {modes.map((mode) => (
                <option key={mode} value={mode}>
                  {mode}
                </option>
              ))}
            </select>
          </div>

          <div className="modal-action">
            <button type="button" className="btn" onClick={onClose}>
              {t('generation-new-draft-cancel')}
            </button>
            <button
              type="submit"
              className="btn btn-primary"
              disabled={!canSubmit}
            >
              {isPending
                ? t('generation-new-draft-creating')
                : t('generation-new-draft-create')}
            </button>
          </div>
        </form>
      </div>
    </dialog>
  );
}
