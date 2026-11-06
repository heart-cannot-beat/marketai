import Link from 'next/link';
import { useTranslation } from 'next-i18next';
import { InformationCircleIcon } from '@heroicons/react/24/outline';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useGenerationDefaults } from '@/hooks/test-plans/useGenerationDefaults';

/** Where Settings renders the provider-credential section. */
export const PROVIDER_CREDENTIALS_ANCHOR = 'provider-credentials';

/**
 * The front flow's top banner (contract §4): shown while the Workspace
 * generates in Mock (no OpenAI key bound) or when its defaults ask for
 * OpenAI without a usable key. The Owner gets a link to Settings → provider
 * credentials; a Member is told to ask the Owner (Settings is Owner-only).
 */
export function GenerationDefaultsBanner() {
  const { t } = useTranslation('tests');
  const { current } = useWorkspace();
  const { defaults } = useGenerationDefaults();
  const warnings = defaults?.warnings ?? [];
  const missingKey = warnings.includes('byok_credential_missing');
  const mock = warnings.includes('mock_mode');
  if (!missingKey && !mock) return null;
  const isOwner = current.role === 'OWNER';

  return (
    <div
      className={`alert ${missingKey ? 'alert-warning' : 'alert-info'} items-start text-sm`}
      role="status"
      data-testid="generation-defaults-banner"
      data-warning={missingKey ? 'byok_credential_missing' : 'mock_mode'}
    >
      <InformationCircleIcon className="h-5 w-5 shrink-0" aria-hidden="true" />
      <div className="flex flex-1 flex-wrap items-center gap-x-3 gap-y-1">
        <span>{missingKey ? t('banner-byok-missing') : t('banner-mock')}</span>
        {isOwner ? (
          <Link
            href={`/w/${current.slug}/settings#${PROVIDER_CREDENTIALS_ANCHOR}`}
            className="link font-medium"
          >
            {t('banner-owner-action')}
          </Link>
        ) : (
          <span className="text-base-content/70">
            {t('banner-member-action')}
          </span>
        )}
      </div>
    </div>
  );
}
