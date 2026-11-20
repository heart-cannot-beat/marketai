import { useTranslation } from 'next-i18next';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { GuardedLink } from '@/components/workspace/GuardedLink';
import { SPEND_GUARDRAIL_ANCHOR } from '@/lib/entitlements/spend-guardrail';

/** Codes that mean a paid step would pass the Workspace's AI spend limits. */
export const SPEND_LIMIT_CODES: ReadonlySet<string> = new Set([
  'budget_exceeded',
]);

/** Whether a refusal or a stop was a spend limit. */
export function isSpendLimit(code: string | null | undefined): boolean {
  return !!code && SPEND_LIMIT_CODES.has(code);
}

/**
 * Where the Owner raises the AI spend limits (design "errors and things
 * waiting for you": "this step would pass today's AI spend limit", #204).
 * Members read the limits too but cannot change them, so they get no link;
 * the sentence beside it already says the Owner can raise them.
 */
export function SpendLimitLink() {
  const { t } = useTranslation('ideas');
  const { current } = useWorkspace();
  if (current.role !== 'OWNER') return null;
  return (
    <GuardedLink
      href={`/w/${current.slug}/settings#${SPEND_GUARDRAIL_ANCHOR}`}
      className="link text-sm font-semibold"
      data-testid="spend-limit-link"
    >
      {t('spend-limit-link')}
    </GuardedLink>
  );
}
