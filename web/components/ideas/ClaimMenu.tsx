import { useId, useState, type FormEvent } from 'react';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import type {
  CommandOutcome,
  IdeasCommands,
} from '@/hooks/ideas/useIdeasCommands';
import { errorMessage } from '@/lib/i18n/domain-errors';
import {
  claimChanged,
  claimFormStart,
  claimImages,
  MIN_CLAIMS,
  removedFactsOf,
  type EditClaimBody,
} from '@/lib/ideas/claim-edit';
import type { IdeasDetail } from '@/lib/ideas/ideas-view';
import { BUTTON } from './PaidImageAction';

type Angle = components['schemas']['Angle'];

/** What is open under a claim's "More". */
export type ClaimPanel = 'menu' | 'edit' | 'delete' | null;

interface Failure {
  message: string;
  /** Someone changed the ideas first: refreshing comes before trying again. */
  stale: boolean;
}

const INPUT =
  'w-full rounded-lg border border-gray-300 bg-white p-3 text-base dark:border-gray-600 dark:bg-gray-900 sm:text-sm';

/**
 * A claim's "More" (design §5.7, #224): change the claim in the user's own
 * words, or delete it. Both are free. The images made for the claim before
 * a change need making again; after a delete they move to the earlier ads
 * at the bottom of the page and can never be downloaded.
 */
export function ClaimMenu({
  angle,
  number,
  detail,
  commands,
  panel,
  onPanelChange,
}: {
  angle: Angle;
  /** The claim's number on the page, 1-based. */
  number: number;
  detail: IdeasDetail;
  commands: IdeasCommands;
  panel: ClaimPanel;
  onPanelChange: (panel: ClaimPanel) => void;
}) {
  const { t } = useTranslation('ideas');
  return (
    <div className="space-y-2" data-testid="claim-menu">
      <button
        type="button"
        className="min-h-11 px-2 text-sm font-medium underline"
        aria-expanded={panel !== null}
        aria-label={t('claim-more-label', { number })}
        onClick={() => onPanelChange(panel === null ? 'menu' : null)}
      >
        {t('claim-more')}
      </button>
      {panel === 'menu' && (
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            className={BUTTON}
            onClick={() => onPanelChange('edit')}
          >
            {t('claim-edit-open')}
          </button>
          <button
            type="button"
            className={BUTTON}
            onClick={() => onPanelChange('delete')}
          >
            {t('claim-delete-open')}
          </button>
        </div>
      )}
      {panel === 'edit' && (
        <ClaimForm
          angle={angle}
          number={number}
          detail={detail}
          commands={commands}
          onClose={() => onPanelChange(null)}
        />
      )}
      {panel === 'delete' && (
        <DeleteClaim
          angle={angle}
          number={number}
          detail={detail}
          commands={commands}
          onClose={() => onPanelChange(null)}
        />
      )}
    </div>
  );
}

/** The sentence for a claim command that did not go through. */
function useFailureOf() {
  const { t } = useTranslation('ideas');
  const { t: common } = useTranslation('common');
  return (outcome: CommandOutcome<unknown>): Failure | null => {
    if (outcome.kind === 'done') return null;
    if (outcome.kind === 'uncertain')
      return { message: t('claim-problem-uncertain'), stale: false };
    const problem: ProblemDetails = outcome.problem;
    if (problem.code === 'stale_marketing_revision')
      return { message: t('claim-problem-stale'), stale: true };
    if (problem.code === 'claim_fact_not_found')
      return { message: t('claim-problem-facts-gone'), stale: true };
    if (problem.code === 'claim_unsupported') {
      const claims = (problem as { params?: { claims?: string[] } }).params
        ?.claims;
      if (claims?.length)
        return {
          message: t('claim-problem-unsupported', {
            claims: claims.join(t('list-separator')),
          }),
          stale: false,
        };
    }
    return { message: errorMessage(common, problem), stale: false };
  };
}

function FailureNote({
  failure,
  onRefresh,
}: {
  failure: Failure | null;
  onRefresh: () => void;
}) {
  const { t } = useTranslation('ideas');
  if (!failure) return null;
  return (
    <div
      role="alert"
      className="space-y-2 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-900 dark:border-red-800 dark:bg-red-950 dark:text-red-200"
    >
      <p>{failure.message}</p>
      {failure.stale && (
        <button
          type="button"
          className="btn btn-sm min-h-11"
          onClick={onRefresh}
        >
          {t('claim-refresh')}
        </button>
      )}
    </div>
  );
}

/** What saving or deleting does to the images already made for the claim. */
function ImagesNote({
  prefix,
  count,
  kept,
}: {
  prefix: 'claim-form' | 'claim-delete';
  count: number;
  kept: number;
}) {
  const { t } = useTranslation('ideas');
  if (count === 0) return null;
  return (
    <p
      role="note"
      className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200"
    >
      {t(prefix === 'claim-form' ? 'claim-form-redo' : 'claim-delete-ads', {
        count,
      })}
      {kept > 0 &&
        ` ${t(prefix === 'claim-form' ? 'claim-form-kept' : 'claim-delete-kept', { count: kept })}`}
    </p>
  );
}

function ClaimForm({
  angle,
  number,
  detail,
  commands,
  onClose,
}: {
  angle: Angle;
  number: number;
  detail: IdeasDetail;
  commands: IdeasCommands;
  onClose: () => void;
}) {
  const { t } = useTranslation('ideas');
  const ids = useId();
  const failureOf = useFailureOf();
  const start = claimFormStart(detail, angle);
  const [proposition, setProposition] = useState(start.proposition);
  const [rationale, setRationale] = useState(start.rationale);
  const [factIds, setFactIds] = useState<string[]>(start.fact_ids ?? []);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);
  const facts = detail.understanding?.facts ?? [];
  const removed = removedFactsOf(detail, angle.id);
  const images = claimImages(detail, angle.id);
  const ready = proposition.trim() !== '' && rationale.trim() !== '';

  function toggle(factId: string) {
    setFactIds((current) =>
      current.includes(factId)
        ? current.filter((id) => id !== factId)
        : // In the understanding's order, so the same choice is the same body.
          facts
            .map((fact) => fact.id)
            .filter((id) => id === factId || current.includes(id))
    );
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!ready || busy) return;
    const body: EditClaimBody = {
      proposition: proposition.trim(),
      rationale: rationale.trim(),
      fact_ids: factIds,
    };
    if (!claimChanged(detail, angle, body)) {
      onClose();
      return;
    }
    setBusy(true);
    setFailure(null);
    const outcome = await commands.editClaim(angle.id, body);
    setBusy(false);
    const failed = failureOf(outcome);
    if (failed) setFailure(failed);
    else onClose();
  }

  return (
    <form
      className="space-y-3 rounded-lg border border-gray-200 p-3 dark:border-gray-700"
      onSubmit={submit}
      noValidate
      aria-labelledby={`${ids}-title`}
      data-testid="claim-form"
    >
      <h4 id={`${ids}-title`} className="text-sm font-semibold">
        {t('claim-form-title', { number })}
      </h4>
      <ImagesNote
        prefix="claim-form"
        count={images.ads.length}
        kept={images.kept}
      />
      <label className="block text-sm" htmlFor={`${ids}-proposition`}>
        <span className="font-semibold">{t('claim-form-proposition')}</span>
        <span className="block text-xs text-gray-600 dark:text-gray-400">
          {t('claim-form-proposition-hint')}
        </span>
      </label>
      <textarea
        id={`${ids}-proposition`}
        className={INPUT}
        rows={2}
        maxLength={2000}
        value={proposition}
        onChange={(event) => setProposition(event.target.value)}
      />
      <label
        className="block text-sm font-semibold"
        htmlFor={`${ids}-rationale`}
      >
        {t('claim-form-rationale')}
      </label>
      <textarea
        id={`${ids}-rationale`}
        className={INPUT}
        rows={3}
        maxLength={2000}
        value={rationale}
        onChange={(event) => setRationale(event.target.value)}
      />
      <fieldset className="space-y-2">
        <legend className="text-sm font-semibold">
          {t('claim-form-facts')}
        </legend>
        <p className="text-xs text-gray-600 dark:text-gray-400">
          {t('claim-form-facts-hint')}
        </p>
        {facts.length === 0 ? (
          <p className="text-sm text-gray-600 dark:text-gray-400">
            {t('claim-form-facts-none')}
          </p>
        ) : (
          <ul role="list" className="space-y-1">
            {facts.map((fact) => {
              const checked = fact.confirmation === 'confirmed';
              return (
                <li key={fact.id}>
                  <label className="flex min-h-11 cursor-pointer items-start gap-2 py-1 text-sm">
                    <input
                      type="checkbox"
                      className="checkbox checkbox-sm mt-0.5"
                      checked={factIds.includes(fact.id)}
                      onChange={() => toggle(fact.id)}
                    />
                    <span className="min-w-0 flex-1">
                      {fact.statement}{' '}
                      <span
                        className={classNames(
                          'inline-block rounded-full px-2 py-px text-xs font-semibold',
                          checked
                            ? 'bg-emerald-100 text-emerald-900'
                            : 'bg-amber-100 text-amber-900'
                        )}
                      >
                        {checked
                          ? t('profile-fact-checked')
                          : t('profile-fact-unchecked')}
                      </span>
                    </span>
                  </label>
                </li>
              );
            })}
          </ul>
        )}
        {removed.length > 0 && (
          <p className="text-sm text-red-900 dark:text-red-200" role="note">
            {t('claim-form-removed', { count: removed.length })}
          </p>
        )}
      </fieldset>
      <p className="text-xs text-gray-600 dark:text-gray-400">
        {t('claim-form-free')}
      </p>
      <FailureNote
        failure={failure}
        onRefresh={() => void commands.refresh()}
      />
      <div className="flex flex-wrap gap-2">
        <button
          type="submit"
          className="btn btn-primary btn-sm min-h-11"
          disabled={!ready || busy}
        >
          {busy ? t('claim-saving') : t('claim-save')}
        </button>
        <button
          type="button"
          className="btn btn-ghost btn-sm min-h-11"
          disabled={busy}
          onClick={onClose}
        >
          {t('claim-cancel')}
        </button>
      </div>
    </form>
  );
}

function DeleteClaim({
  angle,
  number,
  detail,
  commands,
  onClose,
}: {
  angle: Angle;
  number: number;
  detail: IdeasDetail;
  commands: IdeasCommands;
  onClose: () => void;
}) {
  const { t } = useTranslation('ideas');
  const title = useId();
  const failureOf = useFailureOf();
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);
  const images = claimImages(detail, angle.id);
  const tooFew = (detail.ideas?.angles.length ?? 0) <= MIN_CLAIMS;

  async function remove() {
    if (busy) return;
    setBusy(true);
    setFailure(null);
    const outcome = await commands.deleteClaim(angle.id);
    setBusy(false);
    const failed = failureOf(outcome);
    if (failed) setFailure(failed);
    // On success the claim is gone, and so is this question.
  }

  return (
    <div
      role="group"
      aria-labelledby={title}
      className="space-y-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200"
      data-testid="claim-delete"
    >
      <p id={title} className="font-semibold">
        {t('claim-delete-title', { number })}
      </p>
      {tooFew ? (
        <p>{t('claim-delete-too-few')}</p>
      ) : (
        <>
          {images.ads.length > 0 && (
            <p>
              {t('claim-delete-ads', { count: images.ads.length })}
              {images.kept > 0 &&
                ` ${t('claim-delete-kept', { count: images.kept })}`}
            </p>
          )}
          <p>{t('claim-delete-undo')}</p>
        </>
      )}
      <FailureNote
        failure={failure}
        onRefresh={() => void commands.refresh()}
      />
      <div className="flex flex-wrap gap-2">
        {!tooFew && (
          <button
            type="button"
            className="btn btn-error btn-sm min-h-11"
            disabled={busy}
            onClick={() => void remove()}
          >
            {busy ? t('claim-deleting') : t('claim-delete-confirm')}
          </button>
        )}
        <button
          type="button"
          className="btn btn-ghost btn-sm min-h-11"
          disabled={busy}
          onClick={onClose}
        >
          {t('claim-cancel')}
        </button>
      </div>
    </div>
  );
}
