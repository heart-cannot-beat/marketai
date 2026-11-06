import { useTranslation } from 'next-i18next';

interface CapabilityDecision {
  capability: string;
  supported: boolean;
  detail: string | null;
}

interface CapabilityDecisionsProps {
  /** `GenerationPreviewDetail.capability_decisions` — the provider
   * capability resolution results the preview was resolved against. */
  decisions: CapabilityDecision[];
}

export function CapabilityDecisions({ decisions }: CapabilityDecisionsProps) {
  const { t } = useTranslation('common');

  if (decisions.length === 0) return null;

  return (
    <details className="collapse collapse-arrow bg-base-200">
      <summary className="collapse-title text-sm font-medium">
        {t('generation-preview-capabilities-heading', {
          count: decisions.length,
        })}
      </summary>
      <div className="collapse-content">
        <ul className="space-y-1">
          {decisions.map((d) => (
            <li key={d.capability} className="flex items-center gap-2 text-xs">
              <span className={d.supported ? 'text-success' : 'text-error'}>
                {d.supported ? '✓' : '✕'}
              </span>
              <span>{d.capability}</span>
              {d.detail && (
                <span className="text-base-content/60">— {d.detail}</span>
              )}
            </li>
          ))}
        </ul>
      </div>
    </details>
  );
}
