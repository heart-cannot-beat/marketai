export type CapabilityDecision =
  | 'allowed'
  | 'not-available'
  | 'limit-reached'
  | 'requires-upgrade';

interface CapabilityNoticeProps {
  decision: CapabilityDecision;
  message?: string;
}

const DECISION_CONFIG: Record<
  Exclude<CapabilityDecision, 'allowed'>,
  { label: string; className: string }
> = {
  'not-available': { label: 'Not available', className: 'alert-warning' },
  'limit-reached': { label: 'Limit reached', className: 'alert-error' },
  'requires-upgrade': { label: 'Upgrade required', className: 'alert-info' },
};

/**
 * Pure display of a capability/entitlement decision — this component does
 * not resolve entitlements itself (that's the entitlement service, tracked
 * separately). `allowed` renders nothing since there's nothing to notice
 * the caller about.
 */
export function CapabilityNotice({ decision, message }: CapabilityNoticeProps) {
  if (decision === 'allowed') return null;
  const config = DECISION_CONFIG[decision];
  return (
    <div role="status" className={`alert ${config.className}`}>
      <span className="font-medium">{config.label}</span>
      {message && <span className="text-sm">{message}</span>}
    </div>
  );
}
