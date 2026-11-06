import { useTranslation } from 'next-i18next';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { formatDateTime } from '@/lib/i18n/format';
import { CompetitorResolvePanel } from './CompetitorResolvePanel';
import { SimulatedBadge } from './CredentialNotice';
import {
  ACTIVE_STATUSES,
  confirmedPageName,
  dataOf,
  useCommand,
  type Competitor,
} from './shared';

const SECONDARY =
  'rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm disabled:opacity-40';

/**
 * One competitor: its confirmation, `role` and last scan; "Resolve" opens
 * the paid search and the confirmation card. Archiving is the Owner's.
 * A page confirmed by link has no name of its own: the row shows the name
 * its scans observed, and the page ID with a hint until the first scan. A
 * scan after the confirmation that still left it unnamed saw no ads from
 * the page: the hint then says so, and that the ID may be wrong (#355).
 */
export function CompetitorRow({
  competitor,
  country,
  open,
  onToggle,
  onChanged,
}: {
  competitor: Competitor;
  country: string;
  open: boolean;
  onToggle: (open: boolean) => void;
  onChanged: () => Promise<unknown>;
}) {
  const { t, i18n } = useTranslation('competitors');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const command = useCommand();
  const confirmation = competitor.confirmation;
  const confirmed = competitor.confirmation_status === 'confirmed';
  const resolving =
    !!competitor.latest_resolution &&
    ACTIVE_STATUSES.includes(competitor.latest_resolution.status);
  const named =
    confirmedPageName(competitor) ||
    competitor.name ||
    (competitor.input_page_alias ? `@${competitor.input_page_alias}` : null);
  const title = named || confirmation?.page_id || t('candidate-unnamed');
  const lastScannedAt = competitor.last_scanned_at ?? null;
  // `last_scanned_at` is the competitor's, not this page's: a page changed
  // since the last scan has not been scanned yet.
  const scannedSinceConfirmed =
    !!lastScannedAt &&
    !!confirmation?.confirmed_at &&
    Date.parse(lastScannedAt) > Date.parse(confirmation.confirmed_at);

  async function archive() {
    const archived = await command.run(
      `archive:${competitor.competitor_id}`,
      async (action) =>
        dataOf(
          await gateway.archiveCompetitor({
            workspaceSlug: current.slug,
            actionInstanceId: action,
            competitorId: competitor.competitor_id,
          })
        )
    );
    if (archived) await onChanged();
  }

  return (
    <li
      className="space-y-3 py-3"
      data-testid="competitor-row"
      data-competitor-id={competitor.competitor_id}
      data-confirmation={competitor.confirmation_status}
      data-role={competitor.role}
    >
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 space-y-1">
          <p className="flex flex-wrap items-center gap-2">
            <span className="break-words font-medium">{title}</span>
            <span
              className={`rounded px-1.5 py-0.5 text-xs ${
                competitor.role === 'self'
                  ? 'bg-blue-50 text-blue-800'
                  : 'bg-gray-100 text-gray-700'
              }`}
            >
              {t(`role-${competitor.role}`)}
            </span>
            <span
              className={`rounded px-1.5 py-0.5 text-xs ${
                confirmed
                  ? 'bg-green-50 text-green-800'
                  : 'bg-amber-50 text-amber-800'
              }`}
              data-testid="competitor-confirmation-status"
            >
              {confirmed
                ? t(`confirmation-${confirmation?.source ?? 'user'}`)
                : resolving
                  ? t('confirmation-resolving')
                  : t('confirmation-unconfirmed')}
            </span>
            {competitor.latest_resolution?.execution_mode === 'mock' && (
              <SimulatedBadge />
            )}
          </p>
          {confirmation && (
            <p className="break-all text-xs text-gray-600">
              {confirmation.page_alias ? `@${confirmation.page_alias} · ` : ''}
              {t('candidate-page-id', { id: confirmation.page_id })}
            </p>
          )}
          {confirmation && !named && (
            <p
              className="text-xs text-gray-500"
              data-testid="competitor-name-after-scan"
              data-scanned={scannedSinceConfirmed}
            >
              {scannedSinceConfirmed
                ? t('row-name-no-ads')
                : t('row-name-after-scan')}
            </p>
          )}
          {competitor.website_domain && (
            <p className="break-all text-xs text-gray-600">
              {t('row-website', { domain: competitor.website_domain })}
            </p>
          )}
          <p
            className="text-xs text-gray-500"
            data-testid="competitor-last-scan"
          >
            {lastScannedAt
              ? t('row-last-scan', {
                  time: formatDateTime(i18n.language, lastScannedAt, {
                    dateStyle: 'medium',
                    timeStyle: 'short',
                  }),
                })
              : t('row-never-scanned')}
          </p>
          {!confirmed && !competitor.can_resolve && (
            <p className="text-xs text-amber-700">{t('row-needs-link')}</p>
          )}
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          <button
            type="button"
            className={SECONDARY}
            aria-expanded={open}
            onClick={() => onToggle(!open)}
          >
            {open
              ? t('close')
              : confirmed
                ? t('row-change-page')
                : t('row-resolve')}
          </button>
          {current.role === 'OWNER' && (
            <button
              type="button"
              className={SECONDARY}
              disabled={!!command.busy}
              onClick={() => void archive()}
            >
              {t('row-archive')}
            </button>
          )}
        </div>
      </div>
      {command.errorFor('archive:') && (
        <p className="text-sm text-red-700" role="alert">
          {command.errorFor('archive:')}
        </p>
      )}
      {open && (
        <CompetitorResolvePanel
          competitor={competitor}
          country={country}
          onChanged={onChanged}
          onClose={() => onToggle(false)}
        />
      )}
    </li>
  );
}
