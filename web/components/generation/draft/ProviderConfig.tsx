import { useTranslation } from 'next-i18next';
import { ExecutionModeTag } from '@/components/generation/shared/ExecutionModeTag';

interface ProviderConfigProps {
  provider: string;
  model: string;
  executionMode: string;
}

/**
 * Displays the draft's provider/model/execution-mode via `ExecutionModeTag`,
 * the shared provenance chip every generation view must show.
 *
 * `GenerationDraftDetail` (`lib/domain-client/generated/domain-v1.d.ts`)
 * fixes `provider`/`model`/`execution_mode` at draft creation
 * (`create_generation_draft`); `UpdateDraftInput` only accepts `selectors`,
 * `combination_strategy`, `format_config` and `exclusions`, so there is no
 * CAS path that changes provider or model on an existing draft.
 *
 * This group used to carry a "Seed" input that wrote `selectors.seed`. No
 * server path reads that key (`app/generation/preview.py` freezes
 * `selectors` verbatim and no adapter forwards a seed), so it was removed in
 * Issue #157 rather than left looking effective. It comes back once a
 * Provider adapter exposes determinism; until then regeneration is the only
 * "new seed" intent (`app/generation/regeneration.py`).
 */
export function ProviderConfig({
  provider,
  model,
  executionMode,
}: ProviderConfigProps) {
  const { t } = useTranslation('common');

  return (
    <div>
      <span className="label-text font-medium block mb-1">
        {t('generation-draft-provider-label')}
      </span>
      <ExecutionModeTag
        mode={executionMode}
        provider={provider}
        model={model}
      />
    </div>
  );
}
