import { useTranslation } from 'next-i18next';
import { SimulatedBadge } from './CredentialNotice';
import {
  formatCount,
  formatUsd,
  quoteLineCounts,
  type CompetitorScanQuote,
} from './shared';

/**
 * The two lines of a scan's maximum (spec §5.1): the data provider's credits
 * and USD, and the LLM ceiling. Both are reserved under one Operation when
 * the scan (or its resume) starts; whatever is not used is released.
 */
export function ScanQuote({ quote }: { quote: CompetitorScanQuote }) {
  const { t, i18n } = useTranslation('competitors');
  const locale = i18n.language;
  const mock = quote.execution_mode === 'mock';
  const amount = (usd: string, simulated?: string | null) =>
    mock
      ? t('quote-mock-amount', {
          estimate: simulated
            ? formatUsd(locale, simulated)
            : t('quote-no-estimate'),
        })
      : formatUsd(locale, usd);

  return (
    <div
      className="space-y-3 rounded-lg border border-gray-200 bg-white p-3 text-sm"
      data-testid="competitor-scan-quote"
      data-kind={quote.kind}
      data-mode={quote.execution_mode}
    >
      {mock && (
        <p className="flex flex-wrap items-center gap-2">
          <SimulatedBadge />
          <span className="text-gray-700">{t('quote-mock-note')}</span>
        </p>
      )}
      <div data-testid="competitor-scan-quote-provider">
        <p className="flex flex-wrap justify-between gap-x-3 font-medium">
          <span>{t('quote-provider-line')}</span>
          <span>
            {mock
              ? amount(
                  quote.market_data.max_usd,
                  quote.market_data.simulated_usd
                )
              : t('quote-provider-amount', {
                  credits: formatCount(locale, quote.market_data.max_credits),
                  usd: formatUsd(locale, quote.market_data.max_usd),
                })}
          </span>
        </p>
        <ul className="mt-1 space-y-0.5 text-gray-600">
          {(quote.market_data.lines ?? []).map((line) => (
            <li key={line.operation}>
              {t(
                `quote-line-${line.operation}`,
                quoteLineCounts(t, locale, line)
              )}
            </li>
          ))}
        </ul>
      </div>
      <div data-testid="competitor-scan-quote-llm">
        <p className="flex flex-wrap justify-between gap-x-3 font-medium">
          <span>{t('quote-llm-line')}</span>
          <span>
            {mock
              ? amount(quote.llm.max_usd, quote.llm.simulated_usd)
              : t('quote-llm-amount', {
                  usd: formatUsd(locale, quote.llm.max_usd),
                })}
          </span>
        </p>
        <p className="mt-1 text-gray-600">
          {t('quote-llm-detail', {
            calls: quote.llm.calls,
            model: quote.llm.model,
          })}
        </p>
      </div>
      <p className="flex flex-wrap justify-between gap-x-3 border-t border-gray-100 pt-2 font-medium">
        <span>{t('quote-total')}</span>
        <span data-testid="competitor-scan-quote-total">
          {amount(quote.max_usd, quote.simulated_usd)}
        </span>
      </p>
      <p className="text-xs text-gray-500">
        {quote.kind === 'resume' ? t('quote-resume-note') : t('quote-note')}
      </p>
    </div>
  );
}
