import type { useTranslation } from 'next-i18next';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import { errorCodeMessage, errorMessage } from '@/lib/i18n/domain-errors';

type Translate = ReturnType<typeof useTranslation>['t'];

/**
 * Refusals the ads-first screens explain in their own words (#200, #201):
 * the shared error copy speaks of steps these screens do not have (changing
 * a claim, preparing a draft by hand), so the page says what the user can
 * do here instead. Every other code reads as the shared copy.
 */
const OWN = new Set([
  'claim_cites_removed_fact',
  'ad_needs_redo',
  'first_batch_running',
  'ideas_prompt_changed',
  'ideas_changed',
  'ideas_drafts_already_prepared',
  'external_creative_not_regenerable',
  'stale_marketing_revision',
  'checks_quote_changed',
  'check_changed',
  'planner_unavailable',
  'preview_expired',
  'stale_draft_revision',
]);

/** One refusal (or, with `null`, an answer that never came) as one sentence. */
export function ideasProblem(
  t: Translate,
  problem: ProblemDetails | null
): string {
  if (!problem) return t('ideas-uncertain', { ns: 'ideas' });
  if (problem.code === 'checks_unavailable') {
    const params = (problem as { params?: { reason?: string } | null }).params;
    const reason = params?.reason;
    if (reason === 'credential_required')
      return t('ideas-refusal-checks-key', { ns: 'ideas' });
  }
  if (OWN.has(problem.code))
    return t(`ideas-refusal-${problem.code}`, { ns: 'ideas' });
  return errorMessage(t, problem);
}

/**
 * Reasons a paid quote gives for "not now" that no service raises as an
 * error code (nothing has `error-<code>` copy for them), in these screens'
 * own words (#204: the ideas page showed `planner_not_configured` itself).
 */
export const QUOTE_REASON_COPY: Readonly<Record<string, string>> = {
  planner_not_configured: 'quote-unavailable-planner_not_configured',
  qc_not_configured: 'quote-unavailable-qc_not_configured',
};

/** Why a quote is unavailable, as one sentence: never the code itself. */
export function quoteReason(t: Translate, code: string | null | undefined) {
  const own = code ? QUOTE_REASON_COPY[code] : undefined;
  return own ? t(own, { ns: 'ideas' }) : errorCodeMessage(t, code);
}
