import { useTranslation } from 'next-i18next';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { ExecutionModeTag } from '@/components/generation/shared/ExecutionModeTag';
import { PreviewValidity } from './PreviewValidity';
import { PromptInspector } from './PromptInspector';
import { CombinationSummary } from './CombinationSummary';
import { CostEstimate } from './CostEstimate';
import { CapabilityDecisions } from './CapabilityDecisions';

interface PreviewPanelProps {
  previewId: string;
}

/**
 * Root display for a frozen `GenerationPreviewDetail` (spec: Task 6). Fetches
 * via `get_generation_preview` and renders validity, combination/call
 * counts, cost ceiling, the resolved prompt, and capability decisions. The
 * preview is a read-only snapshot -- nothing here is editable; the draft
 * itself (`DraftWorkspace`/`DraftComposer`) is the only mutable surface.
 */
export function PreviewPanel({ previewId }: PreviewPanelProps) {
  const { t } = useTranslation('common');
  const {
    data: preview,
    isLoading,
    problem,
  } = useDomainQuery(
    'get_generation_preview',
    // Mirrors `DraftWorkspace`'s cast for the identical
    // `pathParams`-intersected-with-empty-`queryParams` shape.
    { preview_id: previewId } as any
  );

  if (isLoading) {
    return (
      <div className="flex justify-center mt-4" role="status">
        <span className="loading loading-spinner" />
      </div>
    );
  }

  // `get_generation_preview` resolves the shared `GET
  // /generation/previews/{id}` route, whose response is the discriminated
  // union `GenerationPreviewDetail | GenerationRecoveryPreview` (spec:
  // `app/generation/response_models.py`) -- `purpose: "initial_generation"`
  // vs. `"run_recovery"`. `PreviewPanel` renders only the draft-generation
  // shape; a recovery preview reaching this component (it shouldn't, given
  // `previewId` here always comes from `create_generation_preview` on a
  // draft) falls back to the not-found state rather than crashing.
  if (!preview || preview.purpose !== 'initial_generation') {
    return (
      <div className="mt-4 text-sm text-base-content/60">
        {problem
          ? t('generation-preview-load-error')
          : t('generation-preview-not-found')}
      </div>
    );
  }

  return (
    <div className="space-y-4 rounded-lg border p-4">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold">{t('generation-preview-heading')}</h3>
        <ExecutionModeTag mode={preview.execution_mode} />
      </div>

      <PreviewValidity
        validUntil={preview.valid_until ?? null}
        invalidationReason={preview.invalidation_reason ?? null}
      />

      <CombinationSummary
        combinationCount={preview.combination_count}
        callCount={preview.call_count}
        exclusionSummary={
          preview.exclusion_summary
            ? {
                excluded_count: preview.exclusion_summary.excluded_count,
                reasons: preview.exclusion_summary.reasons ?? [],
              }
            : null
        }
        strategyApplied={preview.strategy_applied ?? null}
        combinationsDroppedByConstraints={
          preview.combinations_dropped_by_constraints ?? 0
        }
      />

      <CostEstimate maximumCost={preview.maximum_cost ?? null} />

      {preview.final_prompt && (
        <PromptInspector finalPrompt={preview.final_prompt} />
      )}

      <CapabilityDecisions
        decisions={(preview.capability_decisions ?? []).map((d) => ({
          capability: d.capability,
          supported: d.supported,
          detail: d.detail ?? null,
        }))}
      />
    </div>
  );
}
