import { useState } from 'react';
import { useTranslation } from 'next-i18next';
import type { ResolvedVersion } from '@/hooks/library/useResolvedLibraryVersion';
import { VersionActions, type VersionAction } from '../commands/VersionActions';
import { WithdrawDialog } from '../commands/WithdrawDialog';
import { ForkDialog } from '../commands/ForkDialog';
import { CloneProductDialog } from '../commands/CloneProductDialog';

interface VersionDetailProps {
  version: ResolvedVersion;
  /** The entry's name, the default for a fork's or a clone's name. */
  entryDisplayName: string;
  /** Opens the entry a Fork or Clone Product created, on its Draft. */
  onEntryCreated: (entryId: string) => void;
}

const ELIGIBILITY_BADGES: Record<string, string> = {
  eligible: 'badge-success',
  warning: 'badge-warning',
  blocked: 'badge-error',
};

export function VersionDetail({
  version,
  entryDisplayName,
  onEntryCreated,
}: VersionDetailProps) {
  const { t } = useTranslation('library');
  const [dialog, setDialog] = useState<VersionAction | null>(null);
  const close = () => setDialog(null);

  return (
    <div className="border border-gray-200 dark:border-gray-700 rounded-lg p-4 space-y-3">
      <div className="flex items-center gap-3">
        <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
          {t('tabs.versions.version-label', { no: String(version.version_no) })}
        </span>
        <span
          className={`badge badge-sm ${version.state === 'effective' ? 'badge-success' : version.state === 'withdrawn' ? 'badge-error' : 'badge-warning'}`}
        >
          {t(`status.${version.state}`, { defaultValue: version.state })}
        </span>
        <span
          className={`badge badge-sm ${ELIGIBILITY_BADGES[version.eligibility] ?? 'badge-ghost'}`}
        >
          {t(`eligibility.${version.eligibility}`, {
            defaultValue: t('eligibility-default'),
          })}
        </span>
      </div>

      <VersionActions
        allowedActions={version.allowed_actions ?? []}
        onWithdraw={() => setDialog('withdraw')}
        onFork={() => setDialog('fork')}
        onCloneProduct={() => setDialog('clone_product')}
      />

      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
        {version.content_hash && (
          <>
            <dt className="text-gray-500 dark:text-gray-400">
              {t('tabs.versions.content-hash')}
            </dt>
            <dd className="text-gray-900 dark:text-gray-100 font-mono text-xs">
              {version.content_hash.slice(0, 16)}
            </dd>
          </>
        )}
        {version.source_version_id && (
          <>
            <dt className="text-gray-500 dark:text-gray-400">
              {t('tabs.versions.source')}
            </dt>
            <dd className="text-gray-900 dark:text-gray-100 font-mono text-xs">
              {version.source_version_id.slice(0, 8)}
            </dd>
          </>
        )}
        {version.published_at && (
          <>
            <dt className="text-gray-500 dark:text-gray-400">
              {t('tabs.versions.published-at')}
            </dt>
            <dd className="text-gray-900 dark:text-gray-100">
              {version.published_at}
            </dd>
          </>
        )}
        {version.withdraw_reason && (
          <>
            <dt className="text-gray-500 dark:text-gray-400">
              {t('tabs.versions.withdraw-reason')}
            </dt>
            <dd className="text-amber-600 dark:text-amber-400">
              {version.withdraw_reason}
            </dd>
          </>
        )}
      </dl>

      {dialog === 'withdraw' && (
        <WithdrawDialog
          versionId={version.id}
          open
          onClose={close}
          onWithdrawn={close}
        />
      )}
      {dialog === 'fork' && (
        <ForkDialog
          versionId={version.id}
          sourceDisplayName={entryDisplayName}
          open
          onClose={close}
          onForked={(entryId) => {
            close();
            onEntryCreated(entryId);
          }}
        />
      )}
      {dialog === 'clone_product' && (
        <CloneProductDialog
          versionId={version.id}
          sourceDisplayName={entryDisplayName}
          open
          onClose={close}
          onCloned={(productId) => {
            close();
            onEntryCreated(productId);
          }}
        />
      )}
    </div>
  );
}
