import { useTranslation } from 'next-i18next';

/**
 * Generic pill for any of Scale's independent lifecycle-ish vocabularies
 * (spec §4.4): Enrollment lifecycle (`draft`/`active`/`paused`/`closed`),
 * Episode phase (`collecting`/`evaluating`/`closed`), Advice-thread
 * lifecycle (`active`/`resolved`/`superseded`/`expired`), and Target
 * lifecycle (`active`/`withdrawn`, spec §10/§21). Each is rendered with
 * `scope` selecting its own translation namespace segment and class map --
 * they are independent axes (spec §4.4), never conflated into one "status".
 */
const CLASS_MAPS: Record<string, Record<string, string>> = {
  enrollment: {
    draft: 'badge-ghost',
    active: 'badge-success',
    paused: 'badge-warning',
    closed: 'badge-ghost',
  },
  episode: {
    collecting: 'badge-info',
    evaluating: 'badge-info',
    closed: 'badge-ghost',
  },
  advice: {
    active: 'badge-info',
    resolved: 'badge-success',
    superseded: 'badge-ghost',
    expired: 'badge-ghost',
  },
  target: {
    active: 'badge-success',
    withdrawn: 'badge-ghost',
  },
};

interface LifecycleBadgeProps {
  scope: 'enrollment' | 'episode' | 'advice' | 'target';
  value: string;
}

export function LifecycleBadge({ scope, value }: LifecycleBadgeProps) {
  const { t } = useTranslation('scale');
  const className = CLASS_MAPS[scope]?.[value] ?? 'badge-ghost';
  const label = t(`lifecycle.${scope}.${value}`, { defaultValue: value });

  return (
    <span className={`badge badge-sm ${className}`} aria-label={label}>
      {label}
    </span>
  );
}
