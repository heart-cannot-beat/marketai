import type { components } from '@/lib/domain-client/generated/domain-v1';

type EnrollmentEvidence = components['schemas']['EnrollmentEvidenceResponse'];

/**
 * Which evidence state an Enrollment's active Episode is in (contract §22,
 * #114), from the two projections `GET .../evidence` returns: the
 * evidence-readiness projection the source collector moves with every
 * manifest (`readiness`), and the accepted Evaluation (`evaluation_id` and
 * the fields beside it).
 *
 * - `waiting`: nothing collected yet -- waiting for the first complete
 *   account-local day.
 * - `collected`: a complete day is in, its first evaluation is running.
 * - `blocked`: the latest manifest is blocked (contract §8). With an older
 *   Evaluation, `stale` says that Evaluation is history, not the current
 *   state; no health is shown as current while data quality is blocked
 *   (contract §5).
 * - `evaluated`: the accepted Evaluation is shown; `pending` says a newer
 *   complete day is in and its evaluation is running. Warming, healthy,
 *   watch and the other health states are the Evaluation's own.
 *
 * Blocked wins whenever the readiness says so: a blocked projection is never
 * hidden behind an Evaluation.
 */
export type EvidenceView =
  | { kind: 'waiting' }
  | { kind: 'collected'; day: string | null }
  | { kind: 'blocked'; stale: boolean }
  | { kind: 'evaluated'; pending: boolean };

export function evidenceView(evidence: EnrollmentEvidence): EvidenceView {
  const readiness = evidence.readiness;
  const evaluated = evidence.evaluation_id !== null;
  if (readiness?.quality_state === 'blocked')
    return { kind: 'blocked', stale: evaluated && evidence.evaluation_stale };
  if (!evaluated)
    return readiness
      ? { kind: 'collected', day: readiness.last_complete_local_day }
      : { kind: 'waiting' };
  return { kind: 'evaluated', pending: evidence.evaluation_stale };
}
