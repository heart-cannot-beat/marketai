import { useState } from 'react';
import { useTranslation } from 'next-i18next';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import { costLabel } from '@/lib/ideas/first-batch-view';
import { ideasProblem } from '@/lib/ideas/ideas-problems';
import type { IdeasDetail } from '@/lib/ideas/ideas-view';
import {
  useChecksQuote,
  type IdeasCommands,
} from '@/hooks/ideas/useIdeasCommands';
import { BUTTON, PRIMARY } from './PaidImageAction';
import { ProblemNote } from './ProblemNote';
import { SpendLimitLink, isSpendLimit } from './SpendLimitLink';

const KNOWN_UNAVAILABLE = new Set([
  'credential_required',
  'nothing_to_check',
  'checks_in_progress',
  'qc_not_configured',
]);

/**
 * "Check N ads · up to $X" (#197, #200): new versions are not checked by
 * themselves, so the user starts their check. The number is the checks
 * quote's; clicking confirms it (`quote_hash`). The quote counts every ad of
 * the ideas without a finished check, which is what the button says.
 */
export function ChecksButton({
  projectId,
  detail,
  commands,
  primary,
}: {
  projectId: string;
  detail: IdeasDetail;
  commands: Pick<IdeasCommands, 'startChecks'>;
  primary?: boolean;
}) {
  const { t, i18n } = useTranslation('ideas');
  const quote = useChecksQuote(projectId, detail);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<ProblemDetails | null | undefined>(
    undefined
  );
  if (quote.kind === 'loading') return null;
  if (quote.kind === 'problem')
    return (
      <p className="text-sm text-red-700 dark:text-red-300" role="alert">
        {ideasProblem(t, quote.problem)}
      </p>
    );
  const data = quote.data;
  if (!data.available) {
    const reason = data.unavailable_reason ?? '';
    if (reason === 'nothing_to_check') return null;
    return (
      <>
        <p className="text-sm text-gray-700 dark:text-gray-300" role="note">
          {KNOWN_UNAVAILABLE.has(reason)
            ? t(`checks-unavailable-${reason}`)
            : t('checks-unavailable-reason', {
                reason: t(`error-${reason}`, {
                  ns: 'common',
                  defaultValue: t('checks-unavailable'),
                }),
              })}
        </p>
        {isSpendLimit(reason) && <SpendLimitLink />}
      </>
    );
  }
  async function start() {
    setBusy(true);
    setProblem(undefined);
    try {
      const outcome = await commands.startChecks(data.quote_hash);
      if (outcome.kind === 'refused') {
        setProblem(outcome.problem);
        if (outcome.problem.code === 'checks_quote_changed')
          await quote.refresh();
      } else if (outcome.kind === 'uncertain') setProblem(null);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="space-y-2">
      <button
        type="button"
        className={primary ? PRIMARY : BUTTON}
        disabled={busy}
        onClick={() => void start()}
      >
        {t('checks-start', {
          count: data.quantity,
          cost: costLabel(i18n.language, data.maximum_cost),
        })}
      </button>
      {problem !== undefined && <ProblemNote problem={problem} />}
    </div>
  );
}
