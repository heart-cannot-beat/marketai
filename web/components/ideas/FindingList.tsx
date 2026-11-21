import type { ReactNode } from 'react';
import { useTranslation } from 'next-i18next';
import type { IdeasFinding } from '@/lib/ideas/ideas-view';

/** Checks the page names in its own words; an ad's own checklist items read as written. */
const KNOWN_CHECKS = new Set([
  'file_valid',
  'product_fidelity',
  'facts_supported',
  'proposition_clear',
  'expression_realized',
  'copy_consistent',
  'language_correct',
  'brand_style',
  'text_readable',
]);
/**
 * Reasons that say what is wrong on their own. "The check wasn't sure"
 * (`low_confidence`) does not: such a finding is named by what was checked,
 * with its "needs you to look" mark.
 */
const KNOWN_GATES = new Set([
  'rendered_text_mismatch',
  'rendered_text_missing',
  'claim_evidence_missing',
  'product_reference_missing',
  'supporting_facts_missing',
  'brand_logo_missing',
  // #265: the image shows words besides its copy.
  'extra_text',
  'extra_text_unsupported',
]);

/** A finding's one-line name: what went wrong, else what the check asks. */
export function useFindingTitle() {
  const { t } = useTranslation('ideas');
  return (finding: IdeasFinding) =>
    finding.gate_reason && KNOWN_GATES.has(finding.gate_reason)
      ? t(`finding-gate-${finding.gate_reason}`)
      : KNOWN_CHECKS.has(finding.check_id)
        ? t(`finding-check-${finding.check_id}`)
        : finding.description;
}

/**
 * One check's findings, each on its own (#197, #201): what it is about,
 * whether it failed or could not be settled from the image, why, what to
 * change, and -- for the copy -- what the image says against what the ad
 * says, and the words it shows besides the copy (#265). `actions` renders
 * the controls of one finding.
 */
export function FindingList({
  findings,
  language,
  actions,
}: {
  findings: IdeasFinding[];
  /** The language the image and copy are in (the ad language). */
  language?: string;
  actions?: (finding: IdeasFinding) => ReactNode;
}) {
  const { t } = useTranslation('ideas');
  const title = useFindingTitle();
  return (
    <ul role="list" className="space-y-3">
      {findings.map((finding) => (
        <li
          key={finding.check_id}
          className="space-y-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-950 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100"
          data-testid="finding"
        >
          <p className="font-semibold">
            {title(finding)}
            <span className="ml-2 inline-block rounded-full bg-white/70 px-2 py-px text-xs font-semibold text-amber-900 dark:bg-black/30 dark:text-amber-200">
              {t(`finding-status-${finding.status}`)}
            </span>
          </p>
          {(finding.rendered_text_diff ?? []).map((diff) => (
            <p key={diff.field}>
              {t(`finding-text-${diff.field}`, {
                rendered: diff.rendered || t('finding-text-unreadable'),
                expected: diff.expected,
              })}
            </p>
          ))}
          {(finding.extra_text ?? []).length > 0 && (
            <div>
              <p>{t('finding-extra-text')}</p>
              <ul className="list-disc pl-5" lang={language}>
                {finding.extra_text!.map((text) => (
                  <li key={text}>{text}</li>
                ))}
              </ul>
            </div>
          )}
          {(finding.unsupported_claims ?? []).length > 0 && (
            <div>
              <p>{t('finding-unsupported')}</p>
              <ul className="list-disc pl-5" lang={language}>
                {finding.unsupported_claims!.map((claim) => (
                  <li key={claim}>{claim}</li>
                ))}
              </ul>
            </div>
          )}
          {finding.rationale && (
            <p>{t('finding-why', { text: finding.rationale })}</p>
          )}
          {finding.suggested_fix && (
            <p>{t('finding-fix', { text: finding.suggested_fix })}</p>
          )}
          {actions && (
            <div className="flex flex-wrap gap-2">{actions(finding)}</div>
          )}
        </li>
      ))}
    </ul>
  );
}
