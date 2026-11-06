export interface AssetActionPermissions {
  canReview: boolean;
  canRegenerate: boolean;
  canDownload: boolean;
}

export interface ActionEligibility {
  action?: string;
  allowed?: boolean;
  reason_code?: string | null;
  requirements?: string[];
}

export function isActionAllowed(
  allowedActions: ActionEligibility[] | undefined,
  action: string
): boolean {
  return (
    allowedActions?.some(
      (eligibility) =>
        eligibility.action === action && eligibility.allowed === true
    ) ?? false
  );
}

// Deliberately a plain function rather than a `useMemo`-backed hook: the
// underlying computation is a cheap `Set` lookup over a handful of action
// codes, so memoization buys nothing, and calling `useMemo` outside of an
// active React render (as the brief's tests do, invoking this directly
// rather than through `renderHook`) throws -- React's hook dispatcher is
// only set up while a component is actually rendering. Keeping this a pure
// function makes it callable both from within a component (as a "hook"
// name implies) and directly from tests.
export function useAssetActions(
  allowedActions: ActionEligibility[] | undefined
): AssetActionPermissions {
  return {
    canReview: isActionAllowed(allowedActions, 'review'),
    canRegenerate: isActionAllowed(allowedActions, 'regenerate'),
    canDownload: isActionAllowed(allowedActions, 'download'),
  };
}
