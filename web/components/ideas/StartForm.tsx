import { useId, useState } from 'react';
import { useRouter } from 'next/router';
import { useTranslation } from 'next-i18next';
import { PasteBox } from '@/components/products/PasteBox';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { GuardedLink } from '@/components/workspace/GuardedLink';
import { MARKET_OPTIONS, countryName } from '@/components/competitors/market';
import {
  useFirstBatchQuote,
  useSaveOpenAIKey,
  useStartFirstBatch,
  type KeySaveOutcome,
  type StartProblem,
} from '@/hooks/ideas/useFirstBatch';
import { errorMessage, type DomainProblem } from '@/lib/i18n/domain-errors';
import { costLabel, costsMore, quoteState } from '@/lib/ideas/first-batch-view';
import { firstBatchHref } from '@/lib/workspace-navigator';

/** Where OpenAI lists a user's API keys ("How do I get one?"). */
export const OPENAI_KEYS_URL = 'https://platform.openai.com/api-keys';

export const AD_LANGUAGES = ['en-US', 'zh-CN'] as const;

/** Why starting is not possible, by the quote's code; anything else is generic. */
const UNAVAILABLE_COPY: Record<string, string> = {
  price_unavailable: 'start-unavailable-price',
  budget_exceeded: 'start-unavailable-budget',
  planner_not_configured: 'start-unavailable-not-set-up',
  model_unavailable: 'start-unavailable-not-set-up',
  qc_not_configured: 'start-unavailable-not-set-up',
};

/** A refused key, by the reason OpenAI gave (#192). */
const KEY_REJECTED_COPY: Record<string, string> = {
  key_invalid: 'start-key-rejected-invalid',
  access_denied: 'start-key-rejected-access',
  quota_exhausted: 'start-key-rejected-quota',
};

interface Notice {
  tone: 'error' | 'success';
  text: string;
  /** Point to Settings, where keys can be switched on or replaced. */
  settings?: boolean;
}

export interface StartTarget {
  market: string;
  adLanguage: string;
}

interface StartFormProps {
  /** The paste box's label. */
  label: string;
  /** Fixed market and language (starting again after a stop); otherwise chosen here. */
  target?: StartTarget;
  compact?: boolean;
  autoFocus?: boolean;
}

/**
 * The start form (ADR 0018, design §5.1, #195): a link, text or images, where
 * the ads go, the workspace's OpenAI key when it has none, and one button that
 * carries the most the first batch can cost. Clicking it is the one
 * confirmation of that cost. Without a key, only the Owner can add one here;
 * anyone else is told to ask the Owner and cannot start.
 */
export function StartForm({
  label,
  target,
  compact = false,
  autoFocus = false,
}: StartFormProps) {
  const { t, i18n } = useTranslation('ideas');
  const router = useRouter();
  const { current } = useWorkspace();
  const ids = useId();
  const reasonId = `${ids}-reason`;
  const isOwner = current.role === 'OWNER';

  const {
    quote,
    failed: quoteFailed,
    refresh: refreshQuote,
  } = useFirstBatchQuote();
  const { start, phase, uploadProgress } = useStartFirstBatch();
  const saveKey = useSaveOpenAIKey();

  const [raw, setRaw] = useState('');
  const [files, setFiles] = useState<File[]>([]);
  const [market, setMarket] = useState(target?.market ?? 'US');
  const [adLanguage, setAdLanguage] = useState(target?.adLanguage ?? 'en-US');
  const [keyValue, setKeyValue] = useState('');
  const [savingKey, setSavingKey] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);

  const state = quoteFailed ? { kind: 'failed' as const } : quoteState(quote);
  const keyRow = state.kind === 'key_required' && isOwner;
  // When the key is all that is missing the quote names the maximum (#216), so
  // saving the key starts straight away. With something else in the way too it
  // names none, and the button shows the maximum once the key is saved.
  const keyMaximum =
    state.kind === 'key_required' && costsMore(quote?.maximum_cost)
      ? quote!.maximum_cost
      : null;
  const busy = phase !== 'idle' || savingKey;
  const strategyLanguage = i18n.language?.startsWith('zh') ? 'zh-CN' : 'en-US';

  const submitLabel =
    state.kind === 'ready'
      ? t('start-button', { cost: costLabel(i18n.language, state.maximum) })
      : keyRow
        ? keyMaximum
          ? t('start-key-save-and-start', {
              cost: costLabel(i18n.language, keyMaximum),
            })
          : t('start-key-save')
        : state.kind === 'loading'
          ? t('start-button-quoting')
          : t('start-button-plain');
  const busyLabel = savingKey
    ? t('start-key-saving')
    : phase === 'uploading' && uploadProgress
      ? t('start-busy-uploading', {
          done: uploadProgress.done,
          total: uploadProgress.total,
        })
      : t('start-busy-starting');
  const submitDisabled =
    state.kind === 'ready' ? false : keyRow ? keyValue.trim() === '' : true;

  async function begin(quoteHash: string) {
    const outcome = await start({
      raw,
      files,
      market,
      adLanguage,
      strategyLanguage,
      quoteHash,
    });
    if (outcome.kind === 'started') {
      await router.push(firstBatchHref(current.slug, outcome.batch.id));
      return;
    }
    const code = outcome.problem.code;
    if (
      code === 'first_batch_quote_changed' ||
      code === 'first_batch_unavailable'
    )
      void refreshQuote();
    setNotice({ tone: 'error', text: startProblemText(outcome.problem) });
  }

  function startProblemText(problem: StartProblem): string {
    switch (problem.code) {
      case 'first_batch_quote_changed':
        return t('start-problem-cost-changed');
      case 'first_batch_unavailable':
        return t('start-problem-unavailable');
      case 'start_unconfirmed':
        return t('start-problem-unconfirmed');
      case 'upload_rejected':
      case 'upload_failed':
        return t('start-problem-upload');
      default:
        return errorMessage(t, problem.problem ?? { code: problem.code });
    }
  }

  function keyNotice(outcome: Exclude<KeySaveOutcome, { kind: 'saved' }>) {
    if (outcome.kind === 'unresolved')
      return { tone: 'error' as const, text: t('start-key-unresolved') };
    if (outcome.kind === 'saved_not_enabled')
      return {
        tone: 'error' as const,
        text: t('start-key-not-enabled'),
        settings: true,
      };
    const problem: DomainProblem = outcome.problem;
    switch (problem.code) {
      case 'provider_key_rejected': {
        const reason = String(problem.params?.reason ?? '');
        return {
          tone: 'error' as const,
          text: t(KEY_REJECTED_COPY[reason] ?? 'start-key-rejected'),
        };
      }
      case 'provider_key_check_unavailable':
        return {
          tone: 'error' as const,
          text: t('start-key-check-unavailable'),
        };
      case 'provider_key_check_unsupported':
        return {
          tone: 'error' as const,
          text: t('start-key-check-unsupported'),
        };
      case 'credential_key_active':
        return {
          tone: 'error' as const,
          text: t('start-key-exists'),
          settings: true,
        };
      default:
        return { tone: 'error' as const, text: errorMessage(t, problem) };
    }
  }

  async function onSubmit() {
    if (busy) return;
    setNotice(null);
    if (state.kind === 'ready') {
      await begin(state.quoteHash);
      return;
    }
    if (!keyRow || keyValue.trim() === '') return;
    const shown = keyMaximum;
    setSavingKey(true);
    let outcome: KeySaveOutcome;
    try {
      outcome = await saveKey(keyValue.trim());
    } finally {
      setSavingKey(false);
    }
    if (outcome.kind !== 'saved') {
      // A key that may have been stored is never kept in the page.
      if (outcome.kind !== 'refused') setKeyValue('');
      setNotice(keyNotice(outcome));
      if (outcome.kind !== 'refused') void refreshQuote();
      return;
    }
    setKeyValue('');
    setNotice({ tone: 'success', text: t('start-key-saved') });
    const next = await refreshQuote();
    // Start only on the number the click confirmed: the new maximum may not
    // be more than what the button said. Otherwise the button now says it.
    if (shown && next?.available && Number(next.maximum_cost) <= Number(shown))
      await begin(next.quote_hash);
  }

  const reason =
    state.kind === 'key_required' && !isOwner
      ? t('start-key-ask-owner')
      : state.kind === 'unavailable'
        ? t(UNAVAILABLE_COPY[state.reason] ?? 'start-unavailable')
        : state.kind === 'failed'
          ? t('start-quote-failed')
          : null;

  return (
    <PasteBox
      id={`${ids}-paste`}
      label={label}
      value={raw}
      onValueChange={setRaw}
      files={files}
      onFilesChange={setFiles}
      onSubmit={() => void onSubmit()}
      submitLabel={submitLabel}
      busy={busy}
      busyLabel={busyLabel}
      compact={compact}
      autoFocus={autoFocus}
      submitBelow
      submitDisabled={submitDisabled}
      submitDescribedBy={reason ? reasonId : undefined}
    >
      {!target && (
        <fieldset className="mt-4 grid gap-3 sm:grid-cols-2">
          <legend className="sr-only">{t('start-target')}</legend>
          <label className="form-control">
            <span className="mb-1 text-sm font-medium">
              {t('start-market')}
            </span>
            <select
              className="select select-bordered min-h-11 w-full"
              value={market}
              disabled={busy}
              onChange={(event) => setMarket(event.target.value)}
            >
              {MARKET_OPTIONS.map((code) => (
                <option key={code} value={code}>
                  {countryName(i18n.language, code)}
                </option>
              ))}
            </select>
          </label>
          <label className="form-control">
            <span className="mb-1 text-sm font-medium">
              {t('start-ad-language')}
            </span>
            <select
              className="select select-bordered min-h-11 w-full"
              value={adLanguage}
              disabled={busy}
              onChange={(event) => setAdLanguage(event.target.value)}
            >
              {AD_LANGUAGES.map((code) => (
                <option key={code} value={code}>
                  {t(`start-ad-language-${code}`)}
                </option>
              ))}
            </select>
          </label>
        </fieldset>
      )}

      {keyRow && (
        <div className="mt-4 space-y-1" data-testid="start-key-row">
          <label htmlFor={`${ids}-key`} className="block text-sm font-medium">
            {t('start-key-label')}
          </label>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <input
              id={`${ids}-key`}
              name="openai-provider-api-key"
              className="input input-bordered min-h-11 w-full flex-1 basis-64 font-mono"
              type="password"
              autoComplete="new-password"
              data-1p-ignore
              data-lpignore="true"
              spellCheck={false}
              placeholder={t('start-key-placeholder')}
              value={keyValue}
              disabled={busy}
              aria-describedby={`${ids}-key-hint`}
              onChange={(event) => setKeyValue(event.target.value)}
            />
            <a
              href={OPENAI_KEYS_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="link text-sm"
            >
              {t('start-key-how')}
            </a>
          </div>
          <p id={`${ids}-key-hint`} className="text-xs text-base-content/70">
            {t('start-key-hint')}
            {!keyMaximum && ` ${t('start-key-then-start')}`}
          </p>
        </div>
      )}

      {reason && (
        <div
          id={reasonId}
          className="mt-4 space-y-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200"
          data-testid="start-blocked"
        >
          <p>{reason}</p>
          {state.kind === 'failed' && (
            <button
              type="button"
              className="btn btn-sm min-h-11"
              onClick={() => void refreshQuote()}
            >
              {t('start-retry')}
            </button>
          )}
        </div>
      )}

      {notice && (
        <div
          role={notice.tone === 'error' ? 'alert' : 'status'}
          className={
            notice.tone === 'error'
              ? 'mt-4 space-y-2 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-900 dark:border-red-800 dark:bg-red-950 dark:text-red-200'
              : 'mt-4 rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-900 dark:border-emerald-800 dark:bg-emerald-950 dark:text-emerald-200'
          }
        >
          <p>{notice.text}</p>
          {notice.settings && (
            <GuardedLink
              href={`/w/${current.slug}/settings`}
              className="link font-semibold"
            >
              {t('start-key-open-settings')}
            </GuardedLink>
          )}
        </div>
      )}

      {state.kind === 'ready' && (
        <p className="mt-4 text-xs text-base-content/70">
          {t('start-cost-note')}
        </p>
      )}
    </PasteBox>
  );
}
