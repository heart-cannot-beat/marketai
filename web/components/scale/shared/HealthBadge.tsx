import { useTranslation } from 'next-i18next';

/**
 * `app.scale.types.ScaleHealth` (spec §4.4 "Health classification" axis) --
 * one of six values, plus `null` for an Enrollment/Episode with no accepted
 * Evaluation yet (evidence still warming) or one whose Evaluation left health
 * unclassified (it then gives a `health_reason`). Rendered independently of
 * lifecycle/maturity/evidence/advice -- a healthy Enrollment can still be
 * paused, and an unhealthy one can still be actively delivering.
 */
const HEALTH_CLASS: Record<string, string> = {
  healthy: 'badge-success',
  fatigue_watch: 'badge-warning',
  creative_fatigue: 'badge-warning',
  economics_decline: 'badge-error',
  delivery_anomaly: 'badge-error',
  not_delivering: 'badge-error',
};

interface HealthBadgeProps {
  health: string | null;
  reasonCode?: string | null;
}

export function HealthBadge({ health, reasonCode }: HealthBadgeProps) {
  const { t } = useTranslation('scale');

  if (health == null) {
    // An evaluation gives a reason exactly when it leaves health
    // unclassified (`app/scale/evaluator.py`); the evidence view words the
    // reason next to the badge.
    const label = reasonCode
      ? t('health.unclassified')
      : t('health.no-evidence');
    return (
      <span className="badge badge-sm badge-ghost" aria-label={label}>
        {label}
      </span>
    );
  }

  const className = HEALTH_CLASS[health] ?? 'badge-ghost';
  const label = t(`health.${health}`, { defaultValue: health });

  return (
    <span className={`badge badge-sm ${className}`} aria-label={label}>
      {label}
    </span>
  );
}
