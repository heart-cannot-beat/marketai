import { useTranslation } from 'next-i18next';

/** `app.scale.types.EvidenceMaturity` (spec §4.4 "Episode phase and
 * maturity" axis). Orthogonal to health -- an Episode can be
 * `economics_ready` and unhealthy, or `warming` and healthy so far. */
const MATURITY_CLASS: Record<string, string> = {
  warming: 'badge-ghost',
  monitorable: 'badge-info',
  economics_ready: 'badge-success',
  insufficient: 'badge-warning',
};

interface MaturityIndicatorProps {
  maturity: string;
}

export function MaturityIndicator({ maturity }: MaturityIndicatorProps) {
  const { t } = useTranslation('scale');
  const className = MATURITY_CLASS[maturity] ?? 'badge-ghost';
  const label = t(`maturity.${maturity}`, { defaultValue: maturity });

  return (
    <span
      className={`badge badge-sm badge-outline ${className}`}
      aria-label={label}
    >
      {label}
    </span>
  );
}
