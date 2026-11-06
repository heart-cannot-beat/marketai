import { useState } from 'react';
import { useTranslation } from 'next-i18next';
import useSWR from 'swr';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { formatDateTime } from '@/lib/i18n/format';
import { AdThumbnail } from './AdThumbnail';
import { AdTags, type TagLabels } from './AdTags';
import {
  RELEVANCE_VALUES,
  dataOf,
  problemMessage,
  useCommand,
  type CompetitorAd,
  type JudgmentSource,
  type Relevance,
} from './shared';

/** Formats the Ad Library reports; anything else reads "Other (X)". */
export const KNOWN_FORMATS = [
  'IMAGE',
  'VIDEO',
  'CAROUSEL',
  'DCO',
  'DPA',
  'MULTI_IMAGES',
  'MULTI_VIDEOS',
] as const;

export function useFormatLabel() {
  const { t } = useTranslation('competitors');
  return (format: string | null | undefined) => {
    if (!format) return t('board-format-unknown');
    return (KNOWN_FORMATS as readonly string[]).includes(format)
      ? t(`board-format-${format}`)
      : t('board-format-other', { format });
  };
}

const RELEVANCE_TONE: Record<Relevance | 'unassessed', string> = {
  relevant: 'bg-green-100 text-green-900',
  adjacent: 'bg-sky-100 text-sky-900',
  unrelated: 'bg-gray-200 text-gray-800',
  unassessed: 'bg-gray-100 text-gray-600',
};

/**
 * Who judged it. Only the scan's AI step is inference ("AI inference"); a
 * §7.1 rule and a member's flip are not (maintainer decision on #271).
 */
export function JudgmentSourceLabel({
  source,
  rule,
}: {
  source: JudgmentSource;
  rule?: string | null;
}) {
  const { t } = useTranslation('competitors');
  return (
    <span
      className={`rounded px-1.5 py-0.5 text-xs ${
        source === 'ai'
          ? 'bg-amber-100 font-medium text-amber-900'
          : 'bg-gray-100 text-gray-700'
      }`}
      data-testid="competitor-judgment-source"
      data-source={source}
    >
      {t(`card-source-${source}`)}
      {source === 'rule' && rule && ` · ${t(`card-rule-${rule}`)}`}
    </span>
  );
}

/**
 * One observation on the board (spec §9): the archived thumbnail with its
 * source, what the ad says, and the judgments a member can change — a
 * relevance flip and the tags, each with its revision history. Nothing here
 * says the ad works (ADR 0014).
 */
export function ObservationCard({
  ad,
  tagLabels,
  onChanged,
}: {
  ad: CompetitorAd;
  tagLabels: TagLabels;
  onChanged: () => Promise<unknown>;
}) {
  const { t, i18n } = useTranslation('competitors');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const command = useCommand();
  const formatLabel = useFormatLabel();
  const [historyOpen, setHistoryOpen] = useState(false);
  const [flipped, setFlipped] = useState(false);
  const relevance = ad.relevance ?? null;
  const value = relevance?.relevance ?? 'unassessed';
  const card = (ad.cards ?? []).find((item) => item.title) ?? ad.cards?.[0];
  const cta = (ad.cards ?? []).find((item) => item.cta_text)?.cta_text;

  async function flip(next: Relevance) {
    const done = await command.run(`flip:${ad.ad_id}:${next}`, async (action) =>
      dataOf(
        await gateway.flipCompetitorAdRelevance({
          workspaceSlug: current.slug,
          actionInstanceId: action,
          scanId: ad.scan_id,
          adId: ad.ad_id,
          body: { relevance: next },
        })
      )
    );
    if (done) {
      setFlipped(true);
      await onChanged();
    }
  }

  return (
    <li
      className="space-y-3 rounded-xl border border-gray-200 bg-white p-3"
      data-testid="competitor-ad-card"
      data-ad-id={ad.ad_id}
      data-archive-id={ad.ad_archive_id}
      data-relevance={value}
      data-relevance-source={relevance?.source ?? ''}
      data-role={ad.role ?? ''}
    >
      <AdThumbnail ad={ad} />
      <div className="min-w-0 space-y-1 text-sm">
        <p className="break-words font-medium">
          {ad.advertiser_name ||
            ad.advertiser_page_id ||
            t('card-no-advertiser')}
          {ad.role === 'self' && (
            <span className="ml-2 text-xs font-normal text-gray-500">
              {t('role-self')}
            </span>
          )}
        </p>
        <p className="text-gray-600">
          {ad.days_running != null
            ? t('card-days-running', { count: ad.days_running })
            : t('card-days-unknown')}
          {' · '}
          {formatLabel(ad.display_format)}
        </p>
        {card?.title && <p className="break-words">{card.title}</p>}
        {cta && <p className="text-gray-600">{t('card-cta', { cta })}</p>}
      </div>

      <div className="space-y-2 border-t border-gray-100 pt-2 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <span
            className={`rounded px-1.5 py-0.5 text-xs font-medium ${RELEVANCE_TONE[value]}`}
            data-testid="competitor-ad-relevance"
          >
            {t(`card-relevance-${value}`)}
          </span>
          {relevance && (
            <JudgmentSourceLabel
              source={relevance.source}
              rule={relevance.rule}
            />
          )}
        </div>
        {relevance?.source === 'ai' && relevance.reason && (
          <p className="break-words text-xs text-gray-600">
            {relevance.reason}
          </p>
        )}
        <div
          className="flex flex-wrap items-center gap-1"
          role="group"
          aria-label={t('card-flip-label')}
        >
          <span className="mr-1 text-xs text-gray-500">
            {t('card-flip-label')}
          </span>
          {RELEVANCE_VALUES.map((option) => (
            <button
              key={option}
              type="button"
              className={`rounded-lg border px-2 py-1 text-xs disabled:opacity-40 ${
                option === value
                  ? 'border-gray-900 bg-gray-900 text-white'
                  : 'border-gray-300 bg-white'
              }`}
              aria-pressed={option === value}
              disabled={!!command.busy || option === value}
              onClick={() => void flip(option)}
              data-testid={`competitor-ad-flip-${option}`}
            >
              {t(`card-relevance-${option}`)}
            </button>
          ))}
        </div>
        {flipped && (
          <p className="text-xs text-gray-600" role="status">
            {t('card-flipped')}
          </p>
        )}
        {command.errorFor('flip:') && (
          <p className="text-xs text-red-700" role="alert">
            {command.errorFor('flip:')}
          </p>
        )}
        {relevance && (
          <button
            type="button"
            className="text-xs underline"
            aria-expanded={historyOpen}
            onClick={() => setHistoryOpen((open) => !open)}
            data-testid="competitor-ad-history-toggle"
          >
            {historyOpen
              ? t('card-history-hide')
              : t('card-history-show', { count: relevance.revision })}
          </button>
        )}
        {historyOpen && relevance && (
          <RelevanceHistory
            scanId={ad.scan_id}
            adId={ad.ad_id}
            revision={relevance.revision}
            locale={i18n.language}
          />
        )}
      </div>

      {(ad.tags || value === 'relevant') && (
        <AdTags ad={ad} labels={tagLabels} onChanged={onChanged} />
      )}
    </li>
  );
}

function RelevanceHistory({
  scanId,
  adId,
  revision,
  locale,
}: {
  scanId: string;
  adId: string;
  revision: number;
  locale: string;
}) {
  const { t } = useTranslation('competitors');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  // Keyed by the revision in effect, so a flip shows up at once.
  const history = useSWR(
    ['competitor-ad-assessments', current.workspaceId, adId, revision],
    async () =>
      dataOf(
        await gateway.listCompetitorAdAssessments({
          workspaceSlug: current.slug,
          scanId,
          adId,
        })
      )
  );
  if (history.error)
    return (
      <p className="text-xs text-red-700" role="alert">
        {problemMessage(t, history.error)}
      </p>
    );
  if (!history.data)
    return (
      <p className="text-xs text-gray-500" role="status">
        {t('card-history-loading')}
      </p>
    );
  return (
    <ol
      className="space-y-1 rounded-lg bg-gray-50 p-2 text-xs"
      data-testid="competitor-ad-history"
    >
      {[...(history.data.items ?? [])].reverse().map((item) => (
        <li
          key={item.assessment_id}
          className="space-y-0.5"
          data-testid="competitor-ad-history-entry"
          data-source={item.source}
          data-relevance={item.relevance}
        >
          <p className="flex flex-wrap items-center gap-1">
            <span className="font-medium">
              {t('card-history-revision', { revision: item.revision })}
            </span>
            <span>{t(`card-relevance-${item.relevance}`)}</span>
            <JudgmentSourceLabel source={item.source} rule={item.rule} />
            {item.created_at && (
              <span className="text-gray-500">
                {formatDateTime(locale, item.created_at, {
                  dateStyle: 'medium',
                  timeStyle: 'short',
                })}
              </span>
            )}
          </p>
          {item.source === 'ai' && item.reason && (
            <p className="break-words text-gray-600">{item.reason}</p>
          )}
        </li>
      ))}
    </ol>
  );
}
