import Link from 'next/link';
import { useTranslation } from 'next-i18next';
import { InformationCircleIcon } from '@heroicons/react/24/outline';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { PROVIDER_CREDENTIALS_ANCHOR } from './shared';

/** Marks anything Controlled Mock produced (spec §5.1, scenario 10). */
export function SimulatedBadge() {
  const { t } = useTranslation('competitors');
  return (
    <span
      className="inline-flex items-center rounded bg-amber-100 px-1.5 py-0.5 text-xs font-medium text-amber-900"
      data-testid="competitor-simulated-badge"
    >
      {t('simulated')}
    </span>
  );
}

/**
 * Why a real resolve or scan is unavailable, one sentence per missing key,
 * and where to fix it: the Owner gets a link to Settings, a Member is told
 * to ask the Owner (Settings is Owner-only).
 */
export function CredentialHint({ messages }: { messages: string[] }) {
  const { t } = useTranslation('competitors');
  const { current } = useWorkspace();
  if (!messages.length) return null;
  return (
    <div
      className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900"
      role="status"
      data-testid="competitor-credential-hint"
    >
      <InformationCircleIcon className="h-5 w-5 shrink-0" aria-hidden="true" />
      <div className="min-w-0 space-y-1">
        {messages.map((message) => (
          <p key={message}>{message}</p>
        ))}
        <p>{t('mock-only')}</p>
        {current.role === 'OWNER' ? (
          <Link
            href={`/w/${current.slug}/settings#${PROVIDER_CREDENTIALS_ANCHOR}`}
            className="font-medium underline"
            data-testid="competitor-settings-link"
          >
            {t('credential-owner-action')}
          </Link>
        ) : (
          <p className="text-amber-800">{t('credential-member-action')}</p>
        )}
      </div>
    </div>
  );
}
