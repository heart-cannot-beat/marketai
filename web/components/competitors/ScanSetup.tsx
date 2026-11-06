import { useEffect, useId, useState } from 'react';
import { useTranslation } from 'next-i18next';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { CredentialHint, SimulatedBadge } from './CredentialNotice';
import { ModeChoice } from './CompetitorResolvePanel';
import { MARKET_OPTIONS, countryName } from './market';
import { ScanQuote } from './ScanQuote';
import {
  codeMessage,
  confirmedPageName,
  dataOf,
  formatUsd,
  problemMessage,
  useCommand,
  type Competitor,
  type CompetitorScan,
  type CompetitorScanQuote,
  type CompetitorScanRequest,
  type ExecutionMode,
} from './shared';

const BUTTON =
  'rounded-lg bg-gray-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40';
const INPUT =
  'w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm focus:border-gray-600 focus:outline-none focus:ring-2 focus:ring-gray-200';

const MAX_KEYWORDS = 5;
// The contract's defaults (spec §5.1); this page does not expose them.
const KEYWORD_PAGES = 2;
const MEDIA_REFRESHES = 10;
const DISCOVER_ADVERTISERS = 30;

// Commas, newlines and the CJK enumeration and full-width commas, escaped so
// the source stays free of CJK.
const KEYWORD_SEPARATORS = new RegExp('[,\\n\\u3001\\uff0c]');

export function keywordsOf(text: string) {
  return Array.from(
    new Set(
      text
        .split(KEYWORD_SEPARATORS)
        .map((keyword) => keyword.trim())
        .filter(Boolean)
    )
  );
}

/**
 * A new scan (spec §5.1): market, pages per competitor, keywords and the
 * visual-tagging cap, with both quote lines refreshed as they change. A real
 * scan needs the `market_data` key and the AI key; without either, only
 * Controlled Mock is offered and the whole scan is simulated.
 */
export function ScanSetup({
  productId,
  competitors,
  country,
  marketUnmatched,
  planMarket,
  onCountryChange,
  onStarted,
}: {
  productId: string;
  competitors: Competitor[];
  country: string;
  marketUnmatched: boolean;
  planMarket: string | null;
  onCountryChange: (country: string) => void;
  onStarted: (scan: CompetitorScan) => Promise<void>;
}) {
  const { t, i18n } = useTranslation('competitors');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const command = useCommand();
  const ids = {
    market: useId(),
    pages: useId(),
    keywords: useId(),
    tagged: useId(),
  };
  const [selected, setSelected] = useState<string[]>(() =>
    competitors.map((c) => c.competitor_id)
  );
  // Newly confirmed competitors join the scan unless the user opted out.
  const competitorKey = competitors.map((c) => c.competitor_id).join(',');
  const [known, setKnown] = useState(competitorKey);
  if (known !== competitorKey) {
    setKnown(competitorKey);
    const previous = new Set(known.split(','));
    setSelected((ids) => [
      ...ids.filter((id) => competitors.some((c) => c.competitor_id === id)),
      ...competitors
        .map((c) => c.competitor_id)
        .filter((id) => !previous.has(id)),
    ]);
  }
  const [pages, setPages] = useState(3);
  const [keywordText, setKeywordText] = useState('');
  const [tagged, setTagged] = useState(60);
  const [mode, setMode] = useState<ExecutionMode | null>(null);
  const keywords = keywordsOf(keywordText);
  const tooManyKeywords = keywords.length > MAX_KEYWORDS;
  const taggedValid = Number.isInteger(tagged) && tagged >= 0 && tagged <= 200;

  // The quote follows the request through its serialized form.
  const request: CompetitorScanRequest | null =
    selected.length && !tooManyKeywords && taggedValid
      ? {
          product_id: productId,
          competitor_ids: selected,
          keywords: keywords.slice(0, MAX_KEYWORDS),
          country,
          max_pages_per_competitor: pages,
          keyword_pages: KEYWORD_PAGES,
          max_media_refreshes: MEDIA_REFRESHES,
          max_tagged_ads: tagged,
          max_discover_advertisers: DISCOVER_ADVERTISERS,
          ...(mode ? { execution_mode: mode } : {}),
        }
      : null;

  const [quote, setQuote] = useState<{
    key: string;
    data?: CompetitorScanQuote;
    error?: string;
  } | null>(null);
  const requestKey = request ? JSON.stringify(request) : '';
  useEffect(() => {
    if (!request) {
      setQuote(null);
      return;
    }
    let cancelled = false;
    const timer = setTimeout(async () => {
      try {
        const data = dataOf(
          await gateway.previewCompetitorScan({
            workspaceSlug: current.slug,
            actionInstanceId: crypto.randomUUID(),
            body: request,
          })
        );
        if (!cancelled) setQuote({ key: requestKey, data });
      } catch (e) {
        if (!cancelled)
          setQuote({ key: requestKey, error: problemMessage(t, e) });
      }
    }, 300);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestKey, current.slug]);
  const fresh = quote?.key === requestKey ? quote : null;
  const preview = fresh?.data;
  const missing = (preview?.credentials ?? []).filter((c) => !c.ready);
  const mockOnly = !!preview && !preview.available_modes.includes('byok');
  const hints = mockOnly
    ? missing.length
      ? missing.map((credential) =>
          credential.code
            ? codeMessage(t, credential.code, credential.params)
            : t(`credential-missing-${credential.binding_scope}`)
        )
      : [
          codeMessage(
            t,
            preview.byok_unavailable_code ?? 'market_data_credential_missing',
            preview.byok_unavailable_params
          ),
        ]
    : [];

  async function start() {
    if (!request || !preview) return;
    const body = { ...request, execution_mode: preview.execution_mode };
    const scan = await command.run(
      `scan:${JSON.stringify(body)}`,
      async (action) =>
        dataOf(
          await gateway.createCompetitorScan({
            workspaceSlug: current.slug,
            actionInstanceId: action,
            body,
          })
        )
    );
    if (scan) await onStarted(scan);
  }

  const markets: string[] = MARKET_OPTIONS.includes(
    country as (typeof MARKET_OPTIONS)[number]
  )
    ? [...MARKET_OPTIONS]
    : [country, ...MARKET_OPTIONS];

  return (
    <div className="space-y-4" data-testid="competitor-scan-setup">
      <fieldset className="space-y-2 text-sm" disabled={!!command.busy}>
        <legend className="font-medium">{t('scan-competitors')}</legend>
        {competitors.map((competitor) => (
          <label key={competitor.competitor_id} className="flex gap-2">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={selected.includes(competitor.competitor_id)}
              onChange={(e) =>
                setSelected((ids) =>
                  e.target.checked
                    ? [...ids, competitor.competitor_id]
                    : ids.filter((id) => id !== competitor.competitor_id)
                )
              }
            />
            <span className="min-w-0 break-words">
              {confirmedPageName(competitor) ||
                competitor.name ||
                competitor.confirmation?.page_id}
              {competitor.role === 'self' && (
                <span className="ml-2 text-xs text-gray-500">
                  {t('role-self')}
                </span>
              )}
            </span>
          </label>
        ))}
        {!selected.length && (
          <p className="text-red-700">{t('scan-competitors-required')}</p>
        )}
      </fieldset>
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-1 text-sm">
          <label htmlFor={ids.market} className="font-medium">
            {t('scan-market')}
          </label>
          <select
            id={ids.market}
            className={INPUT}
            value={country}
            disabled={!!command.busy}
            onChange={(e) => onCountryChange(e.target.value)}
          >
            {markets.map((code) => (
              <option key={code} value={code}>
                {countryName(i18n.language, code)} ({code})
              </option>
            ))}
          </select>
          <p
            className={
              marketUnmatched ? 'text-amber-700' : 'text-xs text-gray-500'
            }
          >
            {marketUnmatched
              ? t('scan-market-unmatched', { market: planMarket ?? '' })
              : planMarket
                ? t('scan-market-from-plan', { market: planMarket })
                : t('scan-market-hint')}
          </p>
        </div>
        <div className="space-y-1 text-sm">
          <label htmlFor={ids.pages} className="font-medium">
            {t('scan-pages')}
          </label>
          <select
            id={ids.pages}
            className={INPUT}
            value={pages}
            disabled={!!command.busy}
            onChange={(e) => setPages(Number(e.target.value))}
          >
            {[1, 2, 3, 4, 5].map((n) => (
              <option key={n} value={n}>
                {t('scan-pages-option', { count: n, ads: 30 + (n - 1) * 10 })}
              </option>
            ))}
          </select>
          <p className="text-xs text-gray-500">{t('scan-pages-hint')}</p>
        </div>
        <div className="space-y-1 text-sm">
          <label htmlFor={ids.keywords} className="font-medium">
            {t('scan-keywords')}
          </label>
          <input
            id={ids.keywords}
            className={INPUT}
            value={keywordText}
            disabled={!!command.busy}
            placeholder={t('scan-keywords-placeholder')}
            onChange={(e) => setKeywordText(e.target.value)}
            aria-invalid={tooManyKeywords || undefined}
          />
          <p
            className={
              tooManyKeywords ? 'text-red-700' : 'text-xs text-gray-500'
            }
          >
            {tooManyKeywords
              ? t('scan-keywords-too-many', { limit: MAX_KEYWORDS })
              : t('scan-keywords-hint')}
          </p>
        </div>
        <div className="space-y-1 text-sm">
          <label htmlFor={ids.tagged} className="font-medium">
            {t('scan-tagged')}
          </label>
          <input
            id={ids.tagged}
            type="number"
            min={0}
            max={200}
            step={1}
            className={INPUT}
            value={Number.isNaN(tagged) ? '' : tagged}
            disabled={!!command.busy}
            onChange={(e) => setTagged(e.target.valueAsNumber)}
            aria-invalid={!taggedValid || undefined}
          />
          <p className={taggedValid ? 'text-xs text-gray-500' : 'text-red-700'}>
            {taggedValid ? t('scan-tagged-hint') : t('scan-tagged-invalid')}
          </p>
        </div>
      </div>

      {request && !fresh && (
        <p className="text-sm text-gray-500" role="status">
          {t('quote-loading')}
        </p>
      )}
      {fresh?.error && (
        <p className="text-sm text-red-700" role="alert">
          {fresh.error}
        </p>
      )}
      {preview && (
        <>
          {preview.available_modes.includes('byok') && (
            <ModeChoice
              name="competitor-scan-mode"
              value={preview.execution_mode}
              onChange={setMode}
              disabled={!!command.busy}
            />
          )}
          <CredentialHint messages={hints} />
          <ScanQuote quote={preview} />
          <button
            type="button"
            className={BUTTON}
            disabled={!!command.busy}
            onClick={() => void start()}
            data-testid="competitor-scan-start"
          >
            {preview.execution_mode === 'mock' ? (
              <span className="inline-flex items-center gap-2">
                <SimulatedBadge />
                {t('scan-start-mock')}
              </span>
            ) : (
              t('scan-start', {
                usd: formatUsd(i18n.language, preview.max_usd),
              })
            )}
          </button>
        </>
      )}
      {command.errorFor('scan:') && (
        <p className="text-sm text-red-700" role="alert">
          {command.errorFor('scan:')}
        </p>
      )}
    </div>
  );
}
