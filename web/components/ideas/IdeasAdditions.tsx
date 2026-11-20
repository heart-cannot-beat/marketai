import { useId, useState } from 'react';
import { useTranslation } from 'next-i18next';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import { costLabel } from '@/lib/ideas/first-batch-view';
import { ideasProblem, quoteReason } from '@/lib/ideas/ideas-problems';
import { TELLING_TYPES } from '@/lib/ideas/mechanisms';
import type { IdeasDetail } from '@/lib/ideas/ideas-view';
import {
  usePlanningQuote,
  type IdeasAddition,
  type IdeasCommands,
} from '@/hooks/ideas/useIdeasCommands';
import { BUTTON, PRIMARY } from './PaidImageAction';
import { ProblemNote } from './ProblemNote';
import { SpendLimitLink, isSpendLimit } from './SpendLimitLink';

type Angle = components['schemas']['Angle'];
type GuidingAnswer = components['schemas']['IdeasGuidingAnswer'];

const OTHER = 'other';
const INPUT =
  'w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-base dark:border-gray-600 dark:bg-gray-900';

/**
 * Whether the ideas can take an addition now (#200): not while the first
 * batch is still on its way (its own image step makes every ad), nor while a
 * drafting call runs.
 */
export function additionBlocked(
  detail: IdeasDetail
):
  | 'first_batch_running'
  | 'first_batch_stopped'
  | 'planning_in_progress'
  | null {
  const batch = detail.first_batch?.state;
  if (batch === 'needs_attention') return 'first_batch_stopped';
  if (batch && batch !== 'done' && batch !== 'failed')
    return 'first_batch_running';
  const call = detail.latest_call?.state;
  if (call === 'queued' || call === 'running') return 'planning_in_progress';
  return null;
}

/**
 * The note an addition carries for a chosen way of telling (#200): the
 * planner reads it as the user's wish. A catalog type is named by its key,
 * so the planner can use exactly that picture mechanism; "other" is the
 * user's own description.
 */
export function tellingNote(mechanism: string, description: string): string {
  const own = description.trim();
  if (mechanism === OTHER) return own;
  const wish = `Use the visual mechanism "${mechanism}" for this way of telling.`;
  return own ? `${wish} ${own}` : wish;
}

interface AdditionProps {
  detail: IdeasDetail;
  commands: Pick<IdeasCommands, 'addIdeas'>;
  /** The user's answers to the guiding questions, sent with the addition. */
  answers: GuidingAnswer[];
}

/**
 * Starts one paid drafting call that adds to the ideas (#200), confirming the
 * planner quote's maximum: the button carries it, the click confirms it.
 */
function useAddition({ detail, commands, answers }: AdditionProps) {
  const quote = usePlanningQuote();
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<ProblemDetails | null | undefined>(
    undefined
  );
  const blocked = additionBlocked(detail);
  async function add(addition: Omit<IdeasAddition, 'guiding_answers'>) {
    if (quote.kind !== 'ok' || !quote.data.available || busy) return false;
    setBusy(true);
    setProblem(undefined);
    try {
      const outcome = await commands.addIdeas(
        { ...addition, guiding_answers: answers.slice(0, 3) },
        quote.data.quote_hash
      );
      if (outcome.kind === 'done') return true;
      if (outcome.kind === 'refused') {
        setProblem(outcome.problem);
        if (outcome.problem.code === 'planner_unavailable')
          await quote.refresh();
      } else setProblem(null);
      return false;
    } finally {
      setBusy(false);
    }
  }
  return { quote, busy, problem, blocked, add };
}

function QuoteState({
  quote,
  blocked,
}: {
  quote: ReturnType<typeof usePlanningQuote>;
  blocked: ReturnType<typeof additionBlocked>;
}) {
  const { t } = useTranslation('ideas');
  if (blocked)
    return (
      <p className="text-sm text-gray-600 dark:text-gray-400" role="note">
        {t(`add-blocked-${blocked}`)}
      </p>
    );
  if (quote.kind === 'problem')
    return (
      <p className="text-sm text-red-700 dark:text-red-300" role="alert">
        {ideasProblem(t, quote.problem)}
      </p>
    );
  if (quote.kind === 'ok' && !quote.data.available)
    return (
      <>
        <p className="text-sm text-gray-600 dark:text-gray-400" role="note">
          {t('add-unavailable', {
            reason: quoteReason(t, quote.data.unavailable_reason),
          })}
        </p>
        {isSpendLimit(quote.data.unavailable_reason) && <SpendLimitLink />}
      </>
    );
  return null;
}

/**
 * "Think of another claim · up to $X" and "Add my own claim" (design §5.3,
 * #200): one more claim with up to two ways of telling, one ad each. The
 * ads' images are a separate, separately quoted step (two confirmations).
 */
export function AddClaim(props: AdditionProps) {
  const { t, i18n } = useTranslation('ideas');
  const noteId = useId();
  const { quote, busy, problem, blocked, add } = useAddition(props);
  const [own, setOwn] = useState(false);
  const [note, setNote] = useState('');
  const [added, setAdded] = useState(false);
  const cost =
    quote.kind === 'ok' && quote.data.available
      ? costLabel(i18n.language, quote.data.maximum_cost)
      : null;
  const usable = Boolean(cost) && !blocked && !busy;
  async function start(withNote: string) {
    setAdded(false);
    if (await add({ kind: 'claim', note: withNote })) {
      setAdded(true);
      setOwn(false);
      setNote('');
    }
  }
  return (
    <div className="space-y-2" data-testid="add-claim">
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          className={BUTTON}
          disabled={!usable}
          onClick={() => void start('')}
        >
          {cost ? t('add-claim', { cost }) : t('add-claim-plain')}
        </button>
        <button
          type="button"
          className={BUTTON}
          aria-expanded={own}
          disabled={Boolean(blocked)}
          onClick={() => setOwn((open) => !open)}
        >
          {t('add-own-claim')}
        </button>
      </div>
      {own && (
        <div className="space-y-2 rounded-lg border border-gray-200 p-3 dark:border-gray-700">
          <label htmlFor={noteId} className="block text-sm font-medium">
            {t('add-own-claim-label')}
          </label>
          <textarea
            id={noteId}
            className={INPUT}
            maxLength={1000}
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
          <p className="text-xs text-gray-600 dark:text-gray-400">
            {t('add-own-claim-hint')}
          </p>
          <button
            type="button"
            className={PRIMARY}
            disabled={!usable || !note.trim()}
            onClick={() => void start(note.trim())}
          >
            {cost ? t('add-own-claim-confirm', { cost }) : t('add-claim-plain')}
          </button>
        </div>
      )}
      <QuoteState quote={quote} blocked={blocked} />
      {problem !== undefined && <ProblemNote problem={problem} />}
      {added && (
        <p className="text-sm" role="status">
          {t('add-started')}
        </p>
      )}
    </div>
  );
}

/**
 * "Another way of telling · up to $X" under a claim (design §5.3, #200). One
 * click lets the AI pick a picture mechanism this claim does not use yet;
 * "choose the type" picks one of the ten, or describes a new one, first.
 */
export function AddTelling({
  angle,
  open,
  onOpenChange,
  explain = true,
  ...props
}: AdditionProps & {
  angle: Angle;
  /** The type picker is open (the edit screen can open it too). */
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /**
   * Says why adding is not possible now. The ideas page says it once, beside
   * "another claim", rather than under every claim.
   */
  explain?: boolean;
}) {
  const { t, i18n } = useTranslation('ideas');
  const { t: common } = useTranslation('common');
  const pickId = useId();
  const describeId = useId();
  const { quote, busy, problem, blocked, add } = useAddition(props);
  const used = new Set(angle.expressions.map((e) => e.visual_mechanism));
  const firstFree = TELLING_TYPES.find((key) => !used.has(key)) ?? OTHER;
  const [mechanism, setMechanism] = useState<string>(firstFree);
  const [description, setDescription] = useState('');
  const [added, setAdded] = useState(false);
  const cost =
    quote.kind === 'ok' && quote.data.available
      ? costLabel(i18n.language, quote.data.maximum_cost)
      : null;
  const usable = Boolean(cost) && !blocked && !busy;
  async function start(note: string) {
    setAdded(false);
    if (await add({ kind: 'expression', angle_id: angle.id, note })) {
      setAdded(true);
      onOpenChange(false);
      setDescription('');
    }
  }
  const valid = mechanism !== OTHER || description.trim().length > 0;
  return (
    <div className="space-y-2" data-testid="add-telling">
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          className={BUTTON}
          disabled={!usable}
          onClick={() => void start('')}
        >
          {cost ? t('add-telling', { cost }) : t('add-telling-plain')}
        </button>
        <button
          type="button"
          className="min-h-11 px-2 text-sm font-medium underline disabled:opacity-50"
          aria-expanded={open}
          disabled={Boolean(blocked)}
          onClick={() => onOpenChange(!open)}
        >
          {t('add-telling-choose')}
        </button>
      </div>
      {open && (
        <div className="space-y-2 rounded-lg border border-gray-200 p-3 dark:border-gray-700">
          <label htmlFor={pickId} className="block text-sm font-medium">
            {t('add-telling-type')}
          </label>
          <select
            id={pickId}
            className={INPUT}
            value={mechanism}
            onChange={(e) => setMechanism(e.target.value)}
          >
            {TELLING_TYPES.map((key) => (
              <option key={key} value={key} disabled={used.has(key)}>
                {common(`marketing-visual-mechanism-${key}`)}
                {used.has(key) ? ` · ${t('add-telling-used')}` : ''}
              </option>
            ))}
            <option value={OTHER}>{t('add-telling-other')}</option>
          </select>
          <p className="text-xs text-gray-600 dark:text-gray-400">
            {t('add-telling-distinct')}
          </p>
          <label htmlFor={describeId} className="block text-sm font-medium">
            {mechanism === OTHER
              ? t('add-telling-describe')
              : t('add-telling-describe-optional')}
          </label>
          <textarea
            id={describeId}
            className={INPUT}
            maxLength={900}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
          <button
            type="button"
            className={PRIMARY}
            disabled={!usable || !valid}
            onClick={() => void start(tellingNote(mechanism, description))}
          >
            {cost ? t('add-telling-confirm', { cost }) : t('add-telling-plain')}
          </button>
        </div>
      )}
      {explain && <QuoteState quote={quote} blocked={blocked} />}
      {problem !== undefined && <ProblemNote problem={problem} />}
      {added && (
        <p className="text-sm" role="status">
          {t('add-started')}
        </p>
      )}
    </div>
  );
}
