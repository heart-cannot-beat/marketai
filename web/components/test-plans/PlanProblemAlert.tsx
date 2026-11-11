import Link from 'next/link';
import { useTranslation } from 'next-i18next';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { problemKey } from '@/lib/test-plans/plan-model';
import { PROVIDER_CREDENTIALS_ANCHOR } from './GenerationDefaultsBanner';
import { SPEND_GUARDRAIL_ANCHOR } from '@/lib/entitlements/spend-guardrail';

/** Problems the Owner fixes in Settings (key, guardrail, defaults). */
const SETTINGS_CODES = new Set([
  'credential_required',
  'mode_unavailable',
  'model_unavailable',
  'guardrail_exceeded',
  'spend_guardrail_exceeded',
  'provider_mode_mismatch',
  'aspect_ratio_unsupported',
  'variants_invalid',
  'provider_unavailable',
]);

/** Of those, the ones fixed in Settings' spending limits (#126). */
const GUARDRAIL_CODES = new Set([
  'guardrail_exceeded',
  'spend_guardrail_exceeded',
]);

interface PlanProblemAlertProps {
  code: string;
  onDismiss?: () => void;
  /** Optional sentence the copy is wrapped in (e.g. "没能开始起草：…"). */
  wrapKey?: string;
  /**
   * False where the command can no longer be retried (the frozen plan
   * page, #337): the reason shows without pointing to Settings or the Owner.
   */
  fixable?: boolean;
}

/**
 * A refused command explained in the page's own Chinese copy. The
 * server's message is never shown: it can carry plan, direction or asset
 * ids (contract §1 principle 2).
 */
export function PlanProblemAlert({
  code,
  onDismiss,
  wrapKey,
  fixable = true,
}: PlanProblemAlertProps) {
  const { t } = useTranslation('tests');
  const { current } = useWorkspace();
  const reason = t(problemKey(code));
  const needsSettings = fixable && SETTINGS_CODES.has(code);
  const isOwner = current.role === 'OWNER';

  return (
    <div
      className="alert alert-error text-sm"
      role="alert"
      data-problem-code={code}
    >
      <div className="flex flex-1 flex-wrap items-center gap-x-3 gap-y-1">
        <span>{wrapKey ? t(wrapKey, { reason }) : reason}</span>
        {needsSettings &&
          (isOwner ? (
            <Link
              href={`/w/${current.slug}/settings#${
                GUARDRAIL_CODES.has(code)
                  ? SPEND_GUARDRAIL_ANCHOR
                  : PROVIDER_CREDENTIALS_ANCHOR
              }`}
              className="link font-medium"
            >
              {t('problem-go-settings')}
            </Link>
          ) : (
            <span>{t('problem-ask-owner')}</span>
          ))}
      </div>
      {onDismiss && (
        <button
          type="button"
          className="btn btn-ghost btn-xs"
          onClick={onDismiss}
        >
          {t('dismiss')}
        </button>
      )}
    </div>
  );
}
