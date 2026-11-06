import { useTranslation } from 'next-i18next';

export interface ProvenanceViewModel {
  mode: 'real' | 'real-read-only' | 'sandbox' | 'controlled-mock';
  provider?: string;
  model?: string;
  mockScenario?: string;
  correlationId?: string;
}

const MODE_LABELS: Record<
  ProvenanceViewModel['mode'],
  { labelKey: string; className: string }
> = {
  real: { labelKey: 'execution-mode-real', className: 'badge-success' },
  'real-read-only': {
    labelKey: 'execution-mode-real-read-only',
    className: 'badge-info',
  },
  sandbox: { labelKey: 'execution-mode-sandbox', className: 'badge-warning' },
  'controlled-mock': {
    labelKey: 'execution-mode-controlled-mock',
    className: 'badge-error',
  },
};

/**
 * Pure display of what produced the data on screen (real call, sandbox, or
 * Controlled Mock) plus its provider/model/correlation id. Receives an
 * already-normalized `ProvenanceViewModel` — no data fetching here.
 */
export function ModeProvenance({
  provenance,
  label,
}: {
  provenance: ProvenanceViewModel;
  /** Replaces the mode's default badge text; the colour stays the mode's. */
  label?: string;
}) {
  const { t } = useTranslation('common');
  const config = MODE_LABELS[provenance.mode] ?? MODE_LABELS.real;
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs">
      <span
        className={`badge badge-sm ${config.className}`}
        data-testid="execution-mode-badge"
        data-mode={provenance.mode}
      >
        {label ?? t(config.labelKey)}
      </span>
      {provenance.provider && (
        <span className="text-gray-500">{provenance.provider}</span>
      )}
      {provenance.model && (
        <span className="text-gray-500">{provenance.model}</span>
      )}
      {provenance.mockScenario && (
        <span className="text-gray-400 italic">{provenance.mockScenario}</span>
      )}
      {provenance.correlationId && (
        <span className="font-mono text-gray-300">
          {provenance.correlationId}
        </span>
      )}
    </div>
  );
}
