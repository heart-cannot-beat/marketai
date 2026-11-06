import { useTranslation } from 'next-i18next';

interface OfferVersionViewerProps {
  version: Record<string, unknown>;
}

export function OfferVersionViewer({ version }: OfferVersionViewerProps) {
  const { t } = useTranslation('library');

  return (
    <dl className="space-y-3">
      <div>
        <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('fields.offer.display-name')}
        </dt>
        <dd className="mt-1 text-sm text-gray-900 dark:text-gray-100">
          {(version.display_name as string) || t('fields.version.no-value')}
        </dd>
      </div>

      <div>
        <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('fields.offer.item-semantics')}
        </dt>
        <dd className="mt-1 text-sm text-gray-900 dark:text-gray-100">
          {(version.item_semantics as string) || t('fields.version.no-value')}
        </dd>
      </div>

      <div>
        <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('fields.offer.market-semantics')}
        </dt>
        <dd className="mt-1 text-sm text-gray-900 dark:text-gray-100">
          {(version.market_semantics as string) || t('fields.version.no-value')}
        </dd>
      </div>

      <div>
        <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('fields.offer.claim-semantics')}
        </dt>
        <dd className="mt-1 text-sm text-gray-900 dark:text-gray-100">
          {(version.claim_semantics as string) || t('fields.version.no-value')}
        </dd>
      </div>

      <div>
        <dt className="text-xs font-medium text-gray-500 dark:text-gray-400">
          {t('fields.offer.date-semantics')}
        </dt>
        <dd className="mt-1 text-sm text-gray-900 dark:text-gray-100">
          {(version.date_semantics as string) || t('fields.version.no-value')}
        </dd>
      </div>
    </dl>
  );
}
