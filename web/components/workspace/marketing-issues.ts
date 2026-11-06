import type { useTranslation } from 'next-i18next';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import { errorCodeMessage } from '@/lib/i18n/domain-errors';

type Translate = ReturnType<typeof useTranslation>['t'];

export type MetricsIssue = components['schemas']['MetricsIssue'];

/**
 * One CSV preflight issue in the reader's language (#255). The server sends
 * a `code`, the `field` it points at and, for codes that take them, `params`
 * (`ISSUE_PARAMS` in `app/marketing/metrics.py`); it never sends a sentence.
 * The code reads from the shared `error-<code>` table (#253).
 */
export function metricsIssueText(t: Translate, issue: MetricsIssue): string {
  const params = Object.fromEntries(
    Object.entries(issue.params ?? {}).map(([name, value]) => [
      name,
      Array.isArray(value) ? value.join(t('list-comma')) : value,
    ])
  );
  const message = errorCodeMessage(t, issue.code, params);
  const field = t(`marketing-issue-field-${issue.field}`, {
    defaultValue: issue.field,
  });
  // Row 0 is the file as a whole (context, account, headers).
  return issue.row > 0
    ? t('marketing-row-issue', { row: issue.row, field, issue: message })
    : t('marketing-file-issue', { field, issue: message });
}
