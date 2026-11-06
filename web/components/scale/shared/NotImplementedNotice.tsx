import { useTranslation } from 'next-i18next';
import type { ProblemDetails } from '@/lib/domain-client/result-types';

/**
 * Renders the "no service module yet" seam consistently across Breakdowns,
 * Targets, Candidates and Export -- every one of those routes is a stable,
 * allowlisted `501 not_implemented` stub (`app.scale.v1_router` module
 * docstring: "mounted now ... but every handler returns 501 rather than
 * fabricating behavior"). This is not an error to retry; it's a documented
 * seam a later task fills in with a real service module.
 */
export function NotImplementedNotice({
  problem,
  featureLabel,
}: {
  problem: ProblemDetails | undefined;
  featureLabel: string;
}) {
  const { t } = useTranslation('scale');
  if (!problem || problem.status !== 501) return null;

  return (
    <div className="alert alert-info mt-4" role="status">
      <span>{t('not-implemented', { feature: featureLabel })}</span>
    </div>
  );
}
