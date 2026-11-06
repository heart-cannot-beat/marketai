import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'next-i18next';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { useTemplateDimensions } from '@/hooks/generation/useTemplateDimensions';
import { ProviderConfig } from './ProviderConfig';
import { DimensionConfigurator } from './DimensionConfigurator';
import {
  CombinationStrategy,
  type CombinationStrategyValue,
} from './CombinationStrategy';
import { FormatConfig } from './FormatConfig';
import { ReferenceSelector } from './ReferenceSelector';
import { ExclusionConfig, type ExclusionConfigValue } from './ExclusionConfig';
import { DraftSaveIndicator, type DraftSaveState } from './DraftSaveIndicator';
import { newActionId } from '../shared/newActionId';
import { errorMessage } from '@/lib/i18n/domain-errors';

/** Shape this composer actually reads off `GenerationDraftDetail`
 * (`lib/domain-client/generated/domain-v1.d.ts`) plus the optional `title`
 * some list views may attach for display. */
export interface DraftComposerDraft {
  id: string;
  title?: string;
  product_id: string;
  provider: string;
  model: string;
  execution_mode: string;
  state: string;
  revision: number;
  selectors?: Record<string, unknown>;
  combination_strategy?: Record<string, unknown>;
  format_config?: Record<string, unknown>;
  exclusions?: Record<string, unknown>;
  allowed_actions?: Array<{
    action?: string;
    allowed?: boolean;
    reason_code?: string | null;
  }>;
}

interface DraftComposerProps {
  draft: DraftComposerDraft;
  onDraftMutated: (revision?: number) => void | Promise<void>;
  onMutationPendingChange?: (pending: boolean) => void;
}

/**
 * Read the resolved model's variant ceiling out of `GenerationCapabilities`
 * (`models[provider][model].format_limits.max_variants_per_combination`).
 */
export function maxVariantsFromCapabilities(
  capabilities: unknown,
  provider: string,
  model: string
): number | null {
  const models = (
    capabilities as { models?: Record<string, unknown> } | undefined
  )?.models;
  const providerModels = models?.[provider] as
    | Record<string, { format_limits?: Record<string, unknown> }>
    | undefined;
  const limit =
    providerModels?.[model]?.format_limits?.['max_variants_per_combination'];
  return typeof limit === 'number' && limit >= 1 ? limit : null;
}

/**
 * Orchestrates every draft-editing sub-component and threads their patches
 * through one CAS `update_generation_draft` call each, carrying the draft's
 * current `revision` as `If-Match`. `UpdateDraftInput` only accepts
 * `selectors`, `combination_strategy`, `format_config` and `exclusions`
 * (`lib/domain-client/generated/domain-v1.d.ts`) -- every sub-component's
 * `onUpdate` patch is one of those four top-level keys.
 *
 * Two controls were removed in Issue #157 because no server path read what
 * they wrote: a Product dropdown (`selectors.product_entry_id`; the real
 * product is the creation-time `product_id`, fixed by
 * `create_generation_draft`) and a Seed input (`selectors.seed`; see
 * `ProviderConfig`). The keys may still sit in older drafts' `selectors`;
 * the preview ignores them.
 */
export function DraftComposer({
  draft,
  onDraftMutated,
  onMutationPendingChange,
}: DraftComposerProps) {
  const { t } = useTranslation('common');
  const { execute } = useDomainCommand('update_generation_draft');
  const { data: capabilities } = useDomainQuery('get_generation_capabilities');
  const [saveState, setSaveState] = useState<DraftSaveState>('idle');
  const [saveError, setSaveError] = useState<string | null>(null);

  // A conflict is only resolved once the draft's revision has actually
  // moved past the one that produced the 412 -- reset the indicator back to
  // idle when a fresh `draft` (a new revision) arrives after a refresh.
  const lastRevisionRef = useRef(draft.revision);
  useEffect(() => {
    if (draft.revision !== lastRevisionRef.current) {
      lastRevisionRef.current = draft.revision;
      setSaveState((current) => (current === 'conflict' ? 'idle' : current));
    }
  }, [draft.revision]);

  const canUpdate =
    draft.allowed_actions?.some(
      (action) => action.action === 'update' && action.allowed
    ) ?? true;
  const updateBlockedReason = draft.allowed_actions?.find(
    (action) => action.action === 'update' && !action.allowed
  )?.reason_code;

  const handleUpdate = useCallback(
    async (patch: Record<string, unknown>) => {
      if (!canUpdate) return;
      setSaveState('saving');
      setSaveError(null);
      onMutationPendingChange?.(true);

      try {
        const outcome = await execute({
          pathParams: { draft_id: draft.id },
          body: patch,
          actionInstanceId: newActionId(),
          ifMatch: draft.revision,
        });

        if (outcome.kind === 'success') {
          setSaveState('saved');
          await onDraftMutated(outcome.data.revision);
        } else if (
          outcome.kind === 'problem' &&
          outcome.problem.status === 412
        ) {
          setSaveState('conflict');
          await onDraftMutated();
        } else if (outcome.kind === 'problem') {
          setSaveState('idle');
          setSaveError(errorMessage(t, outcome.problem));
        } else {
          setSaveState('idle');
        }
      } finally {
        onMutationPendingChange?.(false);
      }
    },
    [
      canUpdate,
      draft.id,
      draft.revision,
      execute,
      onDraftMutated,
      onMutationPendingChange,
      t,
    ]
  );

  const selectors = draft.selectors ?? {};
  const combinationStrategy = (draft.combination_strategy ??
    {}) as CombinationStrategyValue;
  const formatConfig = draft.format_config ?? {};
  const exclusions = (draft.exclusions ?? {}) as ExclusionConfigValue;
  const disabled = !canUpdate;
  const dimensionTemplateIds = useMemo(
    () =>
      Array.isArray(selectors.dimension_template_ids)
        ? (selectors.dimension_template_ids as string[])
        : [],
    [selectors.dimension_template_ids]
  );
  const { dimensions, isLoading: dimensionsLoading } =
    useTemplateDimensions(dimensionTemplateIds);
  const maxVariants = maxVariantsFromCapabilities(
    capabilities,
    draft.provider,
    draft.model
  );

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">
          {t('generation-draft-composer-heading')}
        </h2>
        <DraftSaveIndicator state={saveState} />
      </div>

      {!canUpdate && (
        <div className="alert alert-warning" role="alert">
          <span>
            {t('generation-draft-update-blocked', {
              reason:
                updateBlockedReason ?? t('generation-draft-reason-unknown'),
            })}
          </span>
        </div>
      )}

      {saveError && (
        <div className="alert alert-error" role="alert">
          <span>{saveError}</span>
        </div>
      )}

      <ProviderConfig
        provider={draft.provider}
        model={draft.model}
        executionMode={draft.execution_mode}
      />

      <DimensionConfigurator
        dimensionTemplateIds={dimensionTemplateIds}
        disabled={disabled}
        onUpdate={(nextTemplateIds) =>
          handleUpdate({
            selectors: {
              ...selectors,
              dimension_template_ids: nextTemplateIds,
            },
          })
        }
      />

      <CombinationStrategy
        strategy={combinationStrategy}
        disabled={disabled}
        onUpdate={(strategy) =>
          handleUpdate({ combination_strategy: strategy })
        }
      />

      <FormatConfig
        format={formatConfig}
        disabled={disabled}
        maxVariantsPerCombination={maxVariants}
        onUpdate={(format) => handleUpdate({ format_config: format })}
      />

      <ReferenceSelector
        referenceAssetIds={(selectors.reference_asset_ids as string[]) ?? []}
        disabled={disabled}
        onUpdate={(referenceAssetIds) =>
          handleUpdate({
            selectors: { ...selectors, reference_asset_ids: referenceAssetIds },
          })
        }
      />

      <ExclusionConfig
        exclusions={exclusions}
        dimensions={dimensions}
        dimensionsLoading={dimensionsLoading}
        disabled={disabled}
        onUpdate={(nextExclusions) =>
          handleUpdate({ exclusions: nextExclusions })
        }
      />
    </div>
  );
}
