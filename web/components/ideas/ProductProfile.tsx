import { useId, useState, type FormEvent, type ReactNode } from 'react';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { GuardedLink } from '@/components/workspace/GuardedLink';
import { useIdeasProject } from '@/hooks/ideas/useIdeasProject';
import {
  useProfileCommands,
  type ProfileOutcome,
} from '@/hooks/ideas/useProfileCommands';
import { useProductDirectory } from '@/hooks/products/useProductDirectory';
import { errorMessage } from '@/lib/i18n/domain-errors';
import { stashProductHandoff } from '@/lib/products/handoff';
import type { WorkspaceProduct } from '@/lib/products/workspace-products';
import type { IdeasDetail } from '@/lib/ideas/ideas-view';
import {
  factUse,
  MAX_FACTS,
  withAudience,
  withFactAdded,
  withFactChanged,
  withFactRemoved,
  withUnknownAnswered,
  withUnsourcedAnswered,
  withUnsourcedDropped,
  type Fact,
  type FactText,
  type Understanding,
} from '@/lib/ideas/profile-view';
import { ProfileBrand, ProfilePhotos } from './ProfileAssets';

/**
 * A product's profile (ADR 0018, design §5.6, #202): what the AI read about
 * a product of the new flow, in one place. Facts can be checked, changed,
 * deleted and added; claims the AI read without a source are listed apart,
 * never used, and can be given a source (#224); the AI's guesses stay apart
 * and read as guesses, the audience among them can be corrected and checked
 * (#224); photos and brand follow. Only what the user checks becomes
 * confirmed content.
 *
 * A product made before the new flow has no ad ideas: it keeps its product
 * card, which this view points to.
 */
export function ProductProfile({ product }: { product: WorkspaceProduct }) {
  const projectId = product.ideas?.projectId;
  if (!projectId) return <EarlierProduct productId={product.productId} />;
  return <Profile product={product} projectId={projectId} />;
}

function EarlierProduct({ productId }: { productId: string }) {
  const { t } = useTranslation('ideas');
  const { current } = useWorkspace();
  return (
    <div
      className="max-w-prose space-y-3 rounded-xl border border-gray-200 p-5 dark:border-gray-700"
      data-testid="profile-earlier-product"
    >
      <h2 className="text-base font-semibold">{t('profile-earlier-title')}</h2>
      <p className="text-sm text-gray-600 dark:text-gray-400">
        {t('profile-earlier-text')}
      </p>
      <GuardedLink
        href={`/w/${current.slug}/products`}
        className="btn btn-sm min-h-11"
        onClick={() =>
          stashProductHandoff('highlight', current.slug, productId)
        }
      >
        {t('profile-earlier-open')}
      </GuardedLink>
    </div>
  );
}

function Profile({
  product,
  projectId,
}: {
  product: WorkspaceProduct;
  projectId: string;
}) {
  const { t } = useTranslation('ideas');
  const { detail, failed, isLoading, refresh } = useIdeasProject(projectId);

  if (!detail) {
    if (failed && !isLoading)
      return (
        <div role="alert" className="space-y-3">
          <p className="text-sm">{t('profile-problem')}</p>
          <button
            type="button"
            className="btn btn-sm min-h-11"
            onClick={() => void refresh()}
          >
            {t('profile-retry')}
          </button>
        </div>
      );
    return (
      <div className="flex items-center gap-3 py-8" role="status">
        <span className="loading loading-spinner" aria-hidden="true" />
        <span className="text-sm">{t('profile-loading')}</span>
      </div>
    );
  }

  return (
    <div className="max-w-3xl space-y-8">
      <p className="text-sm text-gray-600 dark:text-gray-400">
        {t('profile-intro')}
      </p>
      {detail.understanding ? (
        <>
          <Facts
            detail={detail}
            understanding={detail.understanding}
            projectId={projectId}
            refresh={refresh}
          />
          <Unsourced
            detail={detail}
            understanding={detail.understanding}
            projectId={projectId}
            refresh={refresh}
          />
          <Guesses
            detail={detail}
            understanding={detail.understanding}
            projectId={projectId}
            refresh={refresh}
          />
        </>
      ) : (
        <p className="rounded-lg border border-gray-200 p-4 text-sm dark:border-gray-700">
          {t('profile-reading')}
        </p>
      )}
      <ProfilePhotos
        productId={product.productId}
        projectId={projectId}
        detail={detail}
      />
      <ProfileBrand productId={product.productId} />
    </div>
  );
}

/** What is open on the facts: one form or one question at a time. */
type Open =
  | { kind: 'add' }
  | { kind: 'edit'; factId: string }
  | { kind: 'delete'; factId: string }
  | null;

interface Failure {
  message: string;
  /** Someone changed the profile first: refreshing comes before trying again. */
  stale: boolean;
}

/** The sentence for a command that did not go through. */
function useFailureOf() {
  const { t } = useTranslation('ideas');
  return (outcome: ProfileOutcome): Failure | null => {
    if (outcome.kind === 'done') return null;
    if (outcome.kind === 'uncertain')
      return { message: t('profile-uncertain'), stale: false };
    const code = outcome.problem.code;
    if (code === 'stale_marketing_revision')
      return { message: t('profile-stale'), stale: true };
    const own: Record<string, string> = {
      fact_source_missing: 'profile-source-missing',
      fact_claim_unsupported: 'profile-claim-unsupported',
      planning_in_progress: 'profile-busy',
      fact_not_found: 'profile-fact-gone',
      verification_empty: 'profile-stale',
    };
    if (own[code])
      return { message: t(own[code]), stale: code === 'fact_not_found' };
    return { message: errorMessage(t, outcome.problem), stale: false };
  };
}

function FailureNote({
  failure,
  refresh,
}: {
  failure: Failure | null;
  refresh: () => Promise<void>;
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
          onClick={() => void refresh()}
        >
          {t('profile-refresh')}
        </button>
      )}
    </div>
  );
}

function Section({
  title,
  intro,
  children,
  testId,
}: {
  title: string;
  intro?: ReactNode;
  children: ReactNode;
  testId: string;
}) {
  const heading = useId();
  return (
    <section
      aria-labelledby={heading}
      className="space-y-3"
      data-testid={testId}
    >
      <div className="space-y-1">
        <h2 id={heading} className="text-lg font-semibold">
          {title}
        </h2>
        {intro && (
          <p className="text-sm text-gray-600 dark:text-gray-400">{intro}</p>
        )}
      </div>
      {children}
    </section>
  );
}

function Facts({
  detail,
  understanding,
  projectId,
  refresh,
}: {
  detail: IdeasDetail;
  understanding: Understanding;
  projectId: string;
  refresh: () => Promise<void>;
}) {
  const { t } = useTranslation('ideas');
  const { editUnderstanding, checkFact } = useProfileCommands(
    projectId,
    detail
  );
  const directory = useProductDirectory();
  const failureOf = useFailureOf();
  const [open, setOpen] = useState<Open>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [failure, setFailure] = useState<{
    at: string;
    failure: Failure;
  } | null>(null);
  const facts = understanding.facts ?? [];

  async function attempt(at: string, command: () => Promise<ProfileOutcome>) {
    if (busy) return false;
    setBusy(at);
    setFailure(null);
    const outcome = await command();
    setBusy(null);
    const failed = failureOf(outcome);
    if (failed) {
      setFailure({ at, failure: failed });
      return false;
    }
    return true;
  }

  const failureAt = (at: string) =>
    failure?.at === at ? failure.failure : null;

  async function check(fact: Fact) {
    const done = await attempt(`check:${fact.id}`, () => checkFact(fact.id));
    // Checking may publish the product's confirmed details for the first time.
    if (done) void directory.refresh();
  }

  async function save(at: string, next: Understanding, material: string) {
    const done = await attempt(at, () => editUnderstanding(next, material));
    if (done) setOpen(null);
  }

  return (
    <Section
      title={t('profile-facts-title', { number: facts.length })}
      intro={t('profile-facts-intro')}
      testId="profile-facts"
    >
      {facts.length === 0 && (
        <p className="text-sm text-gray-600 dark:text-gray-400">
          {t('profile-facts-none')}
        </p>
      )}
      <ul role="list" className="space-y-3">
        {facts.map((fact) => {
          const use = factUse(detail, fact.id);
          const checked = fact.confirmation === 'confirmed';
          const editing = open?.kind === 'edit' && open.factId === fact.id;
          const deleting = open?.kind === 'delete' && open.factId === fact.id;
          return (
            <li
              key={fact.id}
              className="space-y-3 rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900"
              data-testid="profile-fact"
            >
              <div className="space-y-1">
                <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                  <p className="text-sm font-semibold">{fact.statement}</p>
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
                </div>
                <p className="break-words text-xs text-gray-600 dark:text-gray-400">
                  {t('profile-fact-source', { quote: fact.source_quote })}
                </p>
                {use.ads > 0 && (
                  <p className="text-xs text-gray-600 dark:text-gray-400">
                    {t('profile-fact-used', { count: use.ads })}
                  </p>
                )}
              </div>
              {!editing && !deleting && (
                <div className="flex flex-wrap gap-2">
                  {!checked && (
                    <button
                      type="button"
                      className="btn btn-sm min-h-11"
                      disabled={busy !== null}
                      onClick={() => void check(fact)}
                    >
                      {busy === `check:${fact.id}`
                        ? t('profile-saving')
                        : t('profile-fact-check')}
                    </button>
                  )}
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm min-h-11"
                    disabled={busy !== null}
                    aria-label={t('profile-fact-edit-label', {
                      fact: fact.statement,
                    })}
                    onClick={() => {
                      setFailure(null);
                      setOpen({ kind: 'edit', factId: fact.id });
                    }}
                  >
                    {t('profile-fact-edit')}
                  </button>
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm min-h-11"
                    disabled={busy !== null}
                    aria-label={t('profile-fact-delete-label', {
                      fact: fact.statement,
                    })}
                    onClick={() => {
                      setFailure(null);
                      setOpen({ kind: 'delete', factId: fact.id });
                    }}
                  >
                    {t('profile-fact-delete')}
                  </button>
                </div>
              )}
              {!editing && !deleting && (
                <FailureNote
                  failure={failureAt(`check:${fact.id}`)}
                  refresh={refresh}
                />
              )}
              {editing && (
                <FactForm
                  initial={fact}
                  warning={
                    use.ads > 0
                      ? t('profile-edit-warning', { count: use.ads })
                      : null
                  }
                  note={t('profile-form-unchecked-note')}
                  busy={busy === `edit:${fact.id}`}
                  failure={failureAt(`edit:${fact.id}`)}
                  refresh={refresh}
                  onCancel={() => setOpen(null)}
                  onSave={(text, material) => {
                    if (
                      text.statement.trim() === fact.statement &&
                      text.source_quote.trim() === fact.source_quote &&
                      !material.trim()
                    ) {
                      setOpen(null);
                      return;
                    }
                    void save(
                      `edit:${fact.id}`,
                      withFactChanged(understanding, fact.id, text),
                      material
                    );
                  }}
                />
              )}
              {deleting && (
                <DeleteQuestion
                  ads={use.ads}
                  claims={use.claims}
                  busy={busy === `delete:${fact.id}`}
                  failure={failureAt(`delete:${fact.id}`)}
                  refresh={refresh}
                  onCancel={() => setOpen(null)}
                  onDelete={() =>
                    void save(
                      `delete:${fact.id}`,
                      withFactRemoved(understanding, fact.id),
                      ''
                    )
                  }
                />
              )}
            </li>
          );
        })}
      </ul>
      {open?.kind === 'add' ? (
        <div className="rounded-xl border border-gray-200 p-4 dark:border-gray-700">
          <FactForm
            initial={null}
            warning={null}
            note={t('profile-form-new-note')}
            busy={busy === 'add'}
            failure={failureAt('add')}
            refresh={refresh}
            onCancel={() => setOpen(null)}
            onSave={(text, material) =>
              void save('add', withFactAdded(understanding, text), material)
            }
          />
        </div>
      ) : facts.length < MAX_FACTS ? (
        <div className="space-y-1">
          <button
            type="button"
            className="btn btn-sm min-h-11"
            disabled={busy !== null}
            onClick={() => {
              setFailure(null);
              setOpen({ kind: 'add' });
            }}
          >
            {t('profile-fact-add')}
          </button>
          <p className="text-xs text-gray-600 dark:text-gray-400">
            {t('profile-fact-add-hint')}
          </p>
        </div>
      ) : (
        <p className="text-sm text-gray-600 dark:text-gray-400">
          {t('profile-fact-limit', { limit: MAX_FACTS })}
        </p>
      )}
    </Section>
  );
}

/** One fact's words and source, with material the user may paste for the source. */
function FactForm({
  initial,
  context,
  warning,
  note,
  busy,
  failure,
  refresh,
  onCancel,
  onSave,
}: {
  initial: FactText | null;
  /** What the fact answers, as a sentence shown above it. */
  context?: string;
  warning: string | null;
  note: string;
  busy: boolean;
  failure: Failure | null;
  refresh: () => Promise<void>;
  onCancel: () => void;
  onSave: (text: FactText, material: string) => void;
}) {
  const { t } = useTranslation('ideas');
  const ids = useId();
  const [statement, setStatement] = useState(initial?.statement ?? '');
  const [quote, setQuote] = useState(initial?.source_quote ?? '');
  const [material, setMaterial] = useState('');
  const ready = statement.trim() !== '' && quote.trim() !== '';

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!ready || busy) return;
    onSave({ statement, source_quote: quote }, material);
  }

  return (
    <form className="space-y-3" onSubmit={submit} noValidate>
      {context && <p className="text-sm font-semibold">{context}</p>}
      {warning && (
        <p
          role="note"
          className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200"
        >
          {warning}
        </p>
      )}
      <label className="block text-sm" htmlFor={`${ids}-statement`}>
        <span className="font-semibold">{t('profile-form-statement')}</span>
        <span className="block text-xs text-gray-600 dark:text-gray-400">
          {t('profile-form-statement-hint')}
        </span>
      </label>
      <textarea
        id={`${ids}-statement`}
        className="textarea textarea-bordered w-full text-base sm:text-sm"
        rows={2}
        maxLength={2000}
        value={statement}
        onChange={(event) => setStatement(event.target.value)}
      />
      <label className="block text-sm" htmlFor={`${ids}-quote`}>
        <span className="font-semibold">{t('profile-form-quote')}</span>
        <span className="block text-xs text-gray-600 dark:text-gray-400">
          {t('profile-form-quote-hint')}
        </span>
      </label>
      <textarea
        id={`${ids}-quote`}
        className="textarea textarea-bordered w-full text-base sm:text-sm"
        rows={2}
        maxLength={2000}
        value={quote}
        onChange={(event) => setQuote(event.target.value)}
      />
      <label className="block text-sm" htmlFor={`${ids}-material`}>
        <span className="font-semibold">{t('profile-form-material')}</span>
        <span className="block text-xs text-gray-600 dark:text-gray-400">
          {t('profile-form-material-hint')}
        </span>
      </label>
      <textarea
        id={`${ids}-material`}
        className="textarea textarea-bordered w-full text-base sm:text-sm"
        rows={3}
        maxLength={8000}
        value={material}
        onChange={(event) => setMaterial(event.target.value)}
      />
      <p className="text-xs text-gray-600 dark:text-gray-400">{note}</p>
      <FailureNote failure={failure} refresh={refresh} />
      <div className="flex flex-wrap gap-2">
        <button
          type="submit"
          className="btn btn-primary btn-sm min-h-11"
          disabled={!ready || busy}
        >
          {busy ? t('profile-saving') : t('profile-form-save')}
        </button>
        <button
          type="button"
          className="btn btn-ghost btn-sm min-h-11"
          disabled={busy}
          onClick={onCancel}
        >
          {t('profile-form-cancel')}
        </button>
      </div>
    </form>
  );
}

function DeleteQuestion({
  ads,
  claims,
  busy,
  failure,
  refresh,
  onCancel,
  onDelete,
}: {
  ads: number;
  claims: number;
  busy: boolean;
  failure: Failure | null;
  refresh: () => Promise<void>;
  onCancel: () => void;
  onDelete: () => void;
}) {
  const { t } = useTranslation('ideas');
  const title = useId();
  return (
    <div
      role="group"
      aria-labelledby={title}
      className="space-y-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200"
    >
      <p id={title} className="font-semibold">
        {t('profile-delete-title')}
      </p>
      {ads > 0 && <p>{t('profile-delete-ads', { count: ads })}</p>}
      {claims > 0 && <p>{t('profile-delete-claims', { count: claims })}</p>}
      <p>{t('profile-delete-undo')}</p>
      <FailureNote failure={failure} refresh={refresh} />
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          className="btn btn-error btn-sm min-h-11"
          disabled={busy}
          onClick={onDelete}
        >
          {busy ? t('profile-deleting') : t('profile-delete-confirm')}
        </button>
        <button
          type="button"
          className="btn btn-ghost btn-sm min-h-11"
          disabled={busy}
          onClick={onCancel}
        >
          {t('profile-form-cancel')}
        </button>
      </div>
    </div>
  );
}

/**
 * The AI's guesses about the product (design §5.2, glossary §9): why people
 * need it, who might buy it and so on. They are never confirmed here and must
 * not look like facts. What the AI does not know yet can be answered with a
 * sourced fact, which then joins the facts unchecked.
 */
function Guesses({
  detail,
  understanding,
  projectId,
  refresh,
}: {
  detail: IdeasDetail;
  understanding: Understanding;
  projectId: string;
  refresh: () => Promise<void>;
}) {
  const { t } = useTranslation('ideas');
  const { editUnderstanding } = useProfileCommands(projectId, detail);
  const failureOf = useFailureOf();
  const [answering, setAnswering] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);
  const unknowns = understanding.unknowns ?? [];
  const full = (understanding.facts ?? []).length >= MAX_FACTS;

  async function answer(unknown: string, text: FactText, material: string) {
    if (busy) return;
    setBusy(true);
    setFailure(null);
    const outcome = await editUnderstanding(
      withUnknownAnswered(understanding, unknown, text),
      material
    );
    setBusy(false);
    const failed = failureOf(outcome);
    if (failed) setFailure(failed);
    else setAnswering(null);
  }

  const lists: [string, string[]][] = [
    ['profile-guess-need', [understanding.category_need]],
    ['profile-guess-choice', [understanding.product_choice_reason]],
    ['profile-guess-objections', understanding.objections ?? []],
    ['profile-guess-situations', understanding.situations],
    [
      'profile-guess-inferences',
      understanding.inferences.map((inference) => inference.statement),
    ],
  ];

  return (
    <Section
      title={t('profile-guesses-title')}
      intro={t('profile-guesses-intro')}
      testId="profile-guesses"
    >
      <div className="space-y-4 rounded-xl border border-dashed border-gray-300 bg-gray-50 p-4 dark:border-gray-600 dark:bg-gray-950">
        <Audience
          detail={detail}
          understanding={understanding}
          projectId={projectId}
          refresh={refresh}
        />
        <dl className="space-y-4">
          {lists
            .filter(([, items]) => items.some((item) => item?.trim()))
            .map(([label, items]) => (
              <div key={label} className="space-y-1">
                <dt className="text-xs font-semibold text-gray-600 dark:text-gray-400">
                  {t(label)}
                </dt>
                {items
                  .filter((item) => item?.trim())
                  .map((item, index) => (
                    <dd key={index} className="text-sm italic">
                      {item}
                    </dd>
                  ))}
              </div>
            ))}
        </dl>
        <div className="space-y-2">
          <h3 className="text-xs font-semibold text-gray-600 dark:text-gray-400">
            {t('profile-unknowns-title')}
          </h3>
          {unknowns.length === 0 ? (
            <p className="text-sm text-gray-600 dark:text-gray-400">
              {t('profile-unknowns-none')}
            </p>
          ) : (
            <ul role="list" className="space-y-3">
              {unknowns.map((unknown, index) => (
                <li key={`${index}:${unknown}`} className="space-y-2">
                  <p className="text-sm italic">{unknown}</p>
                  {answering === index ? (
                    <div className="rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900">
                      <FactForm
                        initial={null}
                        context={t('profile-answer-context', { unknown })}
                        warning={null}
                        note={t('profile-answer-note')}
                        busy={busy}
                        failure={failure}
                        refresh={refresh}
                        onCancel={() => setAnswering(null)}
                        onSave={(text, material) =>
                          void answer(unknown, text, material)
                        }
                      />
                    </div>
                  ) : (
                    <button
                      type="button"
                      className="btn btn-sm min-h-11"
                      disabled={busy || full || answering !== null}
                      aria-label={t('profile-unknown-answer-label', {
                        unknown,
                      })}
                      onClick={() => {
                        setFailure(null);
                        setAnswering(index);
                      }}
                    >
                      {t('profile-unknown-answer')}
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </Section>
  );
}

/**
 * Who might buy it (#224): an AI guess like the others until the user says it
 * is right. The words the user checked are what the product's published
 * details name as the audience; changing them needs checking again, and until
 * then the published details name the market the user chose.
 */
function Audience({
  detail,
  understanding,
  projectId,
  refresh,
}: {
  detail: IdeasDetail;
  understanding: Understanding;
  projectId: string;
  refresh: () => Promise<void>;
}) {
  const { t } = useTranslation('ideas');
  const { editUnderstanding, confirmAudience } = useProfileCommands(
    projectId,
    detail
  );
  const directory = useProductDirectory();
  const failureOf = useFailureOf();
  const ids = useId();
  const [editing, setEditing] = useState(false);
  const [words, setWords] = useState(understanding.audience);
  const [busy, setBusy] = useState<'check' | 'save' | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  const checked = detail.audience_confirmed === true;

  async function check() {
    if (busy) return;
    setBusy('check');
    setFailure(null);
    const failed = failureOf(await confirmAudience());
    setBusy(null);
    if (failed) setFailure(failed);
    else void directory.refresh();
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (busy || !words.trim()) return;
    if (words.trim() === understanding.audience) {
      setEditing(false);
      return;
    }
    setBusy('save');
    setFailure(null);
    const failed = failureOf(
      await editUnderstanding(withAudience(understanding, words))
    );
    setBusy(null);
    if (failed) setFailure(failed);
    else setEditing(false);
  }

  return (
    <div className="space-y-2" data-testid="profile-audience">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <h3 className="text-xs font-semibold text-gray-600 dark:text-gray-400">
          {t('profile-guess-audience')}
        </h3>
        <span
          className={classNames(
            'inline-block rounded-full px-2 py-px text-xs font-semibold',
            checked
              ? 'bg-emerald-100 text-emerald-900'
              : 'bg-amber-100 text-amber-900'
          )}
        >
          {checked ? t('profile-fact-checked') : t('profile-fact-unchecked')}
        </span>
      </div>
      {editing ? (
        <form className="space-y-2" onSubmit={save} noValidate>
          <label className="sr-only" htmlFor={`${ids}-audience`}>
            {t('profile-guess-audience')}
          </label>
          <textarea
            id={`${ids}-audience`}
            className="textarea textarea-bordered w-full text-base sm:text-sm"
            rows={2}
            maxLength={2000}
            value={words}
            onChange={(event) => setWords(event.target.value)}
          />
          <p className="text-xs text-gray-600 dark:text-gray-400">
            {t('profile-audience-form-note')}
          </p>
          <FailureNote failure={failure} refresh={refresh} />
          <div className="flex flex-wrap gap-2">
            <button
              type="submit"
              className="btn btn-primary btn-sm min-h-11"
              disabled={busy !== null || !words.trim()}
            >
              {busy === 'save' ? t('profile-saving') : t('profile-form-save')}
            </button>
            <button
              type="button"
              className="btn btn-ghost btn-sm min-h-11"
              disabled={busy !== null}
              onClick={() => {
                setEditing(false);
                setWords(understanding.audience);
              }}
            >
              {t('profile-form-cancel')}
            </button>
          </div>
        </form>
      ) : (
        <>
          <p className={classNames('text-sm', !checked && 'italic')}>
            {understanding.audience}
          </p>
          <p className="text-xs text-gray-600 dark:text-gray-400">
            {checked
              ? t('profile-audience-note-checked')
              : t('profile-audience-note')}
          </p>
          <div className="flex flex-wrap gap-2">
            {!checked && (
              <button
                type="button"
                className="btn btn-sm min-h-11"
                disabled={busy !== null}
                onClick={() => void check()}
              >
                {busy === 'check'
                  ? t('profile-saving')
                  : t('profile-fact-check')}
              </button>
            )}
            <button
              type="button"
              className="btn btn-ghost btn-sm min-h-11"
              disabled={busy !== null}
              aria-label={t('profile-audience-edit-label')}
              onClick={() => {
                setFailure(null);
                setWords(understanding.audience);
                setEditing(true);
              }}
            >
              {t('profile-audience-edit')}
            </button>
          </div>
          <FailureNote failure={failure} refresh={refresh} />
        </>
      )}
    </div>
  );
}

/**
 * Claims the AI read without a source (design §5.2, #224): listed so the user
 * sees them, used in no claim and no ad. Given a source, one becomes an
 * unchecked fact; "don't need it" takes it off the list.
 */
function Unsourced({
  detail,
  understanding,
  projectId,
  refresh,
}: {
  detail: IdeasDetail;
  understanding: Understanding;
  projectId: string;
  refresh: () => Promise<void>;
}) {
  const { t } = useTranslation('ideas');
  const { editUnderstanding } = useProfileCommands(projectId, detail);
  const failureOf = useFailureOf();
  const [sourcing, setSourcing] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [failure, setFailure] = useState<{
    at: string;
    failure: Failure;
  } | null>(null);
  const claims = understanding.unsupported_claims ?? [];
  const full = (understanding.facts ?? []).length >= MAX_FACTS;
  if (claims.length === 0) return null;

  async function attempt(at: string, next: Understanding, material: string) {
    if (busy) return false;
    setBusy(at);
    setFailure(null);
    const failed = failureOf(await editUnderstanding(next, material));
    setBusy(null);
    if (failed) {
      setFailure({ at, failure: failed });
      return false;
    }
    return true;
  }

  const failureAt = (at: string) =>
    failure?.at === at ? failure.failure : null;

  return (
    <Section
      title={t('profile-unsourced-title', { number: claims.length })}
      intro={t('profile-unsourced-intro')}
      testId="profile-unsourced"
    >
      <ul role="list" className="space-y-3">
        {claims.map((claim) => (
          <li
            key={claim.id}
            className="space-y-2 rounded-xl border border-amber-200 bg-amber-50 p-4 dark:border-amber-800 dark:bg-amber-950"
          >
            <p className="text-sm">
              <span aria-hidden="true" className="mr-2 font-bold">
                !
              </span>
              {claim.text}
            </p>
            {sourcing === claim.id ? (
              <div className="rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900">
                <FactForm
                  initial={{ statement: claim.text, source_quote: '' }}
                  context={t('profile-unsourced-context', {
                    claim: claim.text,
                  })}
                  warning={null}
                  note={t('profile-answer-note')}
                  busy={busy === `source:${claim.id}`}
                  failure={failureAt(`source:${claim.id}`)}
                  refresh={refresh}
                  onCancel={() => setSourcing(null)}
                  onSave={(text, material) =>
                    void attempt(
                      `source:${claim.id}`,
                      withUnsourcedAnswered(understanding, claim.id, text),
                      material
                    ).then((done) => done && setSourcing(null))
                  }
                />
              </div>
            ) : (
              <>
                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    className="btn btn-sm min-h-11"
                    disabled={busy !== null || full || sourcing !== null}
                    aria-label={t('profile-unsourced-source-label', {
                      claim: claim.text,
                    })}
                    onClick={() => {
                      setFailure(null);
                      setSourcing(claim.id);
                    }}
                  >
                    {t('profile-unsourced-source')}
                  </button>
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm min-h-11"
                    disabled={busy !== null}
                    aria-label={t('profile-unsourced-drop-label', {
                      claim: claim.text,
                    })}
                    onClick={() =>
                      void attempt(
                        `drop:${claim.id}`,
                        withUnsourcedDropped(understanding, claim.id),
                        ''
                      )
                    }
                  >
                    {busy === `drop:${claim.id}`
                      ? t('profile-saving')
                      : t('profile-unsourced-drop')}
                  </button>
                </div>
                <FailureNote
                  failure={failureAt(`drop:${claim.id}`)}
                  refresh={refresh}
                />
              </>
            )}
          </li>
        ))}
      </ul>
    </Section>
  );
}
