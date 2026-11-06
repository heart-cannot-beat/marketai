import { useTranslation } from 'next-i18next';

interface PromptInspectorProps {
  /** `GenerationPreviewDetail.final_prompt` — the fully-resolved prompt sent
   * to the provider for this preview's combinations. Read-only: the preview
   * is a frozen snapshot, not an editable draft. */
  finalPrompt: string;
}

export function PromptInspector({ finalPrompt }: PromptInspectorProps) {
  const { t } = useTranslation('common');

  return (
    <details className="collapse collapse-arrow bg-base-200">
      <summary className="collapse-title text-sm font-medium">
        {t('generation-preview-prompt-heading')}
      </summary>
      <div className="collapse-content">
        <pre className="whitespace-pre-wrap text-xs font-mono bg-base-300 p-3 rounded overflow-x-auto max-w-full">
          {finalPrompt}
        </pre>
      </div>
    </details>
  );
}
