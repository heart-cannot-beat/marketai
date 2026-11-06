import { useTranslation } from 'next-i18next';
import {
  ModeProvenance,
  type ProvenanceViewModel,
} from '@/components/workspace/ModeProvenance';
import {
  executionModeKind,
  executionModeLabelKey,
} from '@/lib/assets/status-labels';

interface ExecutionModeTagProps {
  mode: string;
  provider?: string;
  model?: string;
  scenario?: string;
  correlationId?: string;
}

/**
 * The execution-mode badge of a generation Run / asset: 真实 / 沙箱 /
 * 示例（受控 Mock）(Issue #191), in the colours `ModeProvenance` gives the
 * real / sandbox / controlled-mock modes.
 */
export function ExecutionModeTag({
  mode,
  provider,
  model,
  scenario,
  correlationId,
}: ExecutionModeTagProps) {
  const { t } = useTranslation('common');
  const provenance: ProvenanceViewModel = {
    mode: executionModeKind(mode),
    provider,
    model,
    mockScenario: scenario,
    correlationId,
  };
  return (
    <ModeProvenance
      provenance={provenance}
      label={t(executionModeLabelKey(mode))}
    />
  );
}
