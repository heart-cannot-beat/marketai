import { OperationStatus } from '@/components/workspace';
import type { ProblemDetails } from '@/lib/domain-client/result-types';

/** `OperationDetailResponse` (`get_operation`, spec §5.6) -- the Operation
 * behind a Run's most recent command (submit/retry/resume/cancel/reconcile,
 * `GenerationRunCommandSummary`). */
interface OperationTrackerOperation {
  state: string;
  result: { resource_type: string; resource_id: string } | null;
}

interface OperationTrackerProps {
  operation: OperationTrackerOperation | null;
  /** A poll-transport failure from `useRunProgress` (network blip, not the
   * Operation itself failing) -- surfaced as `OperationStatus`'s
   * `pollingProblem`, distinct from the Operation's own terminal `state`. */
  transportError: Error | null;
}

/**
 * Thin adapter from `useRunProgress`'s `latestCommandOperation`/
 * `transportError` to the shared `OperationStatus` display
 * (`components/workspace/OperationStatus.tsx`) -- renders nothing while the
 * Run has no command yet (`operation` is `null`, e.g. a freshly-submitted
 * Run whose first command hasn't posted, though in practice Atomic
 * Submission always creates one immediately).
 */
export function OperationTracker({
  operation,
  transportError,
}: OperationTrackerProps) {
  if (!operation) return null;

  const pollingProblem: ProblemDetails | undefined = transportError
    ? {
        type: 'transport_error',
        title: transportError.message,
        status: 0,
        detail: transportError.message,
        instance: '',
        code: 'transport_error',
        trace_id: '',
        retryable: true,
        field_errors: [],
      }
    : undefined;

  return (
    <OperationStatus
      operation={{
        status: operation.state,
        result_ref: operation.result?.resource_id ?? null,
      }}
      pollingProblem={pollingProblem}
    />
  );
}
