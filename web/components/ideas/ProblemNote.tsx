import { useTranslation } from 'next-i18next';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import { ideasProblem } from '@/lib/ideas/ideas-problems';
import { SpendLimitLink, isSpendLimit } from './SpendLimitLink';

/**
 * A refused paid step on the ads-first screens, said in one sentence; when
 * the refusal is a spend limit, the Owner also gets the way to the limits
 * (design "errors and things waiting for you", #204).
 */
export function ProblemNote({ problem }: { problem: ProblemDetails | null }) {
  const { t } = useTranslation('ideas');
  return (
    <>
      <p className="text-sm text-red-700 dark:text-red-300" role="alert">
        {ideasProblem(t, problem)}
      </p>
      {isSpendLimit(problem?.code) && <SpendLimitLink />}
    </>
  );
}
