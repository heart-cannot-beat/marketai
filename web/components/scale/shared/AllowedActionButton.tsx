import { useTranslation } from 'next-i18next';
import type { components } from '@/lib/domain-client/generated/domain-v1';

export type ActionEligibility = components['schemas']['ActionEligibilityModel'];

interface AllowedActionButtonProps {
  action: ActionEligibility | undefined;
  label: string;
  onClick: () => void;
  variant?: 'primary' | 'warning' | 'error' | 'default';
  busy?: boolean;
}

const VARIANT_CLASSES: Record<string, string> = {
  primary: 'btn-primary',
  warning: 'btn-warning',
  error: 'btn-error',
  default: 'btn-outline',
};

/**
 * Renders one server-gated action button (spec §4.4: "server-driven
 * allowed_actions ... never inferred client-side"). If the server's
 * `allowed_actions` array doesn't list this action at all, it renders
 * nothing -- the caller never sees a control for a command the resource
 * type doesn't support. If it's listed but `allowed: false`, the button
 * renders disabled with the server's `reason_code` as its tooltip.
 */
export function AllowedActionButton({
  action,
  label,
  onClick,
  variant = 'default',
  busy = false,
}: AllowedActionButtonProps) {
  const { t } = useTranslation('scale');
  if (!action) return null;

  const denied = !action.allowed || busy;

  return (
    <button
      type="button"
      className={`btn btn-sm ${VARIANT_CLASSES[variant]} ${denied ? 'btn-disabled opacity-60' : ''}`}
      aria-disabled={denied || undefined}
      disabled={busy}
      title={
        !action.allowed && action.reason_code
          ? t(`action-denied.${action.reason_code}`, {
              defaultValue: action.reason_code,
            })
          : undefined
      }
      onClick={(e) => {
        if (!action.allowed || busy) {
          e.preventDefault();
          return;
        }
        onClick();
      }}
      data-action={action.action}
    >
      {label}
    </button>
  );
}
