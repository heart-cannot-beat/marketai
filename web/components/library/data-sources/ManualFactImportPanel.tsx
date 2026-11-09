import { useTranslation } from 'next-i18next';
import { useCallback, useId, useMemo, useRef, useState } from 'react';
import { useLibraryEntries } from '@/hooks/library/useLibraryEntries';
import { useLibraryRoute } from '@/hooks/library/useLibraryRoute';
import { useManualFactImport } from '@/hooks/library/useManualFactImport';

/** One request lists them all: the route's page limit. */
const OFFER_PAGE_SIZE = 100;

interface ManualFactImportPanelProps {
  dataSourceEntryId: string;
}

/**
 * Imports a CSV or JSON facts file (#14). Each import creates a new Manual
 * Fact Set draft next to the data source; the draft is reviewed and
 * published there. Every outcome reads as a sentence, and an import whose
 * outcome is uncertain says how to check before importing again.
 *
 * Every fact row names its Offer (#54). A default Offer, chosen before the
 * file, is given to the rows that name none; only the Workspace's published
 * Offers are offered, the ones the import accepts.
 */
export function ManualFactImportPanel({
  dataSourceEntryId,
}: ManualFactImportPanelProps) {
  const { t } = useTranslation('library');
  const { setEntry } = useLibraryRoute();
  const { view, importFile, reset } = useManualFactImport(dataSourceEntryId);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [defaultOffer, setDefaultOffer] = useState('');
  const offerFieldId = useId();
  const offerList = useLibraryEntries('offer', {
    pageSize: OFFER_PAGE_SIZE,
  });
  const offers = useMemo(
    () => offerList.items.filter((offer) => offer.current_version_id),
    [offerList.items]
  );

  const handleFileChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (file) void importFile(file, defaultOffer || null);
    },
    [importFile, defaultOffer]
  );

  const startOver = useCallback(() => {
    reset();
    if (fileInputRef.current) fileInputRef.current.value = '';
  }, [reset]);

  if (view.step === 'done') {
    return (
      <div className="rounded-lg border border-green-200 bg-green-50 p-4 dark:border-green-800 dark:bg-green-900/20">
        <p className="text-sm text-green-700 dark:text-green-300">
          {t('data-source-ops.import-complete')}
        </p>
        <div className="mt-3 flex gap-2">
          {view.factSetId && (
            <button
              type="button"
              className="btn btn-primary btn-xs"
              // The draft is reviewed before it is published.
              onClick={() => setEntry(view.factSetId, 'draft')}
            >
              {t('data-source-ops.open-fact-set')}
            </button>
          )}
          <button
            type="button"
            className="btn btn-ghost btn-xs"
            onClick={startOver}
          >
            {t('data-source-ops.done')}
          </button>
        </div>
      </div>
    );
  }

  if (view.step === 'uncertain') {
    return (
      <div
        className="rounded-lg border border-amber-200 bg-amber-50 p-4 dark:border-amber-800 dark:bg-amber-900/20"
        role="status"
      >
        <p className="text-sm text-amber-700 dark:text-amber-300">
          {t('data-source-ops.import-uncertain')}
        </p>
        <button
          type="button"
          className="btn btn-ghost btn-xs mt-2"
          onClick={startOver}
        >
          {t('data-source-ops.done')}
        </button>
      </div>
    );
  }

  if (view.step === 'error') {
    return (
      <div
        className="rounded-lg border border-red-200 bg-red-50 p-4 dark:border-red-800 dark:bg-red-900/20"
        role="alert"
      >
        <p className="text-sm text-red-700 dark:text-red-300">{view.message}</p>
        <button
          type="button"
          className="btn btn-ghost btn-xs mt-2"
          onClick={startOver}
        >
          {t('data-source-ops.retry')}
        </button>
      </div>
    );
  }

  if (view.step !== 'idle') {
    return (
      <div
        className="rounded-lg border border-gray-200 p-4 dark:border-gray-700"
        aria-busy="true"
      >
        <p className="text-sm text-gray-500 dark:text-gray-400">
          {view.step === 'importing'
            ? t('data-source-ops.processing')
            : t('data-source-ops.importing')}
        </p>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-gray-200 p-4 dark:border-gray-700">
      <p className="text-sm text-gray-700 dark:text-gray-300">
        {t('data-source-ops.import-description')}
      </p>
      <div className="mt-3 flex max-w-md flex-col gap-1">
        <label
          htmlFor={offerFieldId}
          className="text-sm font-medium text-gray-700 dark:text-gray-300"
        >
          {t('data-source-ops.default-offer-label')}
        </label>
        <select
          id={offerFieldId}
          className="select select-bordered select-sm w-full"
          value={defaultOffer}
          onChange={(e) => setDefaultOffer(e.target.value)}
          aria-describedby={`${offerFieldId}-help`}
          disabled={offerList.isLoading}
        >
          <option value="">{t('data-source-ops.default-offer-none')}</option>
          {offers.map((offer) => (
            <option key={offer.id} value={offer.id}>
              {offer.display_name}
            </option>
          ))}
        </select>
        <p
          id={`${offerFieldId}-help`}
          className="text-xs text-gray-500 dark:text-gray-400"
        >
          {offerList.isLoading
            ? t('data-source-ops.default-offer-loading')
            : offerList.problem
              ? t('data-source-ops.default-offer-unavailable')
              : t('data-source-ops.default-offer-help')}
        </p>
      </div>
      <button
        type="button"
        className="btn btn-primary btn-sm mt-3"
        onClick={() => fileInputRef.current?.click()}
      >
        {t('data-source-ops.select-file')}
      </button>
      <input
        ref={fileInputRef}
        type="file"
        accept=".csv,.json"
        className="hidden"
        onChange={handleFileChange}
        aria-label={t('data-source-ops.file-input')}
      />
    </div>
  );
}
