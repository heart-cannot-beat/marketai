import { useTranslation } from 'next-i18next';
import { useRef, useCallback, useState, useEffect } from 'react';
import { useLibraryReferenceUpload } from '@/hooks/library/useLibraryReferenceUpload';
import { ReferenceGateProgress } from './ReferenceGateProgress';

interface ReferenceUploadZoneProps {
  contextEntryId: string;
  ownerKind?: 'library_entry' | 'marketing_project';
  /** Whether the promoted reference is bound to `contextEntryId`'s Draft
   * (true for products and persons, false for reference groups). */
  bindToContext?: boolean;
  onComplete?: (referenceObjectId: string) => void;
  /** Product settings impose a tighter limit than the general Library. */
  maxFileBytes?: number;
}

const DEFAULT_RIGHTS_SCOPE = 'owned_product_photo';

function defaultValidTo(): string {
  const next = new Date();
  next.setFullYear(next.getFullYear() + 1);
  return next.toISOString().slice(0, 10);
}

export function ReferenceUploadZone({
  contextEntryId,
  ownerKind = 'library_entry',
  bindToContext = false,
  onComplete,
  maxFileBytes,
}: ReferenceUploadZoneProps) {
  const { t } = useTranslation('library');
  const { upload, reset, progress } = useLibraryReferenceUpload();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [isDragOver, setIsDragOver] = useState(false);
  const [fileError, setFileError] = useState<string | null>(null);
  const notified = useRef<string | null>(null);
  useEffect(() => {
    if (
      progress.phase === 'released' &&
      progress.referenceObjectId &&
      notified.current !== progress.referenceObjectId
    ) {
      notified.current = progress.referenceObjectId;
      onComplete?.(progress.referenceObjectId);
    }
  }, [progress.phase, progress.referenceObjectId, onComplete]);
  // The rights declaration (contract §7.2) travels with the upload and is
  // what the server publishes into the ReferenceAssetVersion -- the user
  // never writes it as raw JSON afterwards.
  const [rightsScope, setRightsScope] = useState(DEFAULT_RIGHTS_SCOPE);
  const [rightsValidTo, setRightsValidTo] = useState(defaultValidTo);
  const [rightsConfirmed, setRightsConfirmed] = useState(false);

  const rightsReady =
    rightsConfirmed &&
    rightsScope.trim().length > 0 &&
    /^\d{4}-\d{2}-\d{2}$/.test(rightsValidTo);

  const handleFile = useCallback(
    async (file: File) => {
      if (!rightsReady) return;
      if (maxFileBytes && file.size > maxFileBytes) {
        setFileError(
          t('references.file-too-large', { mb: maxFileBytes / 1024 / 1024 })
        );
        return;
      }
      if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) {
        setFileError(t('references.file-type-not-supported'));
        return;
      }
      setFileError(null);
      await upload(file, {
        contextEntryId,
        ownerKind,
        bindToContext,
        rights: { scope: rightsScope.trim(), valid_to: rightsValidTo },
      });
    },
    [
      upload,
      contextEntryId,
      ownerKind,
      bindToContext,
      rightsReady,
      rightsScope,
      rightsValidTo,
      maxFileBytes,
      t,
    ]
  );

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setIsDragOver(false);
      const file = e.dataTransfer.files[0];
      if (file) handleFile(file);
    },
    [handleFile]
  );

  const handleInputChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (file) handleFile(file);
    },
    [handleFile]
  );

  if (progress.phase === 'released') {
    return (
      <div className="rounded-lg border border-green-200 bg-green-50 p-4 dark:border-green-800 dark:bg-green-900/20">
        <p className="text-sm text-green-700 dark:text-green-300">
          {progress.boundEntryId
            ? t('references.upload-complete-bound')
            : t('references.upload-complete')}
        </p>
        <button
          type="button"
          className="btn btn-ghost btn-xs mt-2"
          onClick={reset}
        >
          {t('references.upload-another')}
        </button>
      </div>
    );
  }

  if (progress.phase !== 'idle') {
    return (
      <div className="rounded-lg border border-gray-200 p-4 dark:border-gray-700">
        <ReferenceGateProgress progress={progress} />
        {(progress.phase === 'failed' || progress.phase === 'rejected') && (
          <button
            type="button"
            className="btn btn-ghost btn-xs mt-3"
            onClick={reset}
          >
            {t('references.try-again')}
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="form-control w-full">
          <div className="label">
            <span className="label-text">{t('references.rights-scope')}</span>
          </div>
          <input
            type="text"
            className="input input-bordered input-sm w-full"
            value={rightsScope}
            onChange={(e) => setRightsScope(e.target.value)}
            autoComplete="off"
          />
        </label>
        <label className="form-control w-full">
          <div className="label">
            <span className="label-text">
              {t('references.rights-valid-to')}
            </span>
          </div>
          <input
            type="date"
            className="input input-bordered input-sm w-full"
            value={rightsValidTo}
            onChange={(e) => setRightsValidTo(e.target.value)}
          />
        </label>
      </div>
      <label className="flex items-start gap-2 text-sm text-gray-700 dark:text-gray-300">
        <input
          type="checkbox"
          className="checkbox checkbox-sm mt-0.5"
          checked={rightsConfirmed}
          onChange={(e) => setRightsConfirmed(e.target.checked)}
        />
        <span>{t('references.rights-confirm')}</span>
      </label>

      <div
        className={`rounded-lg border-2 border-dashed p-6 text-center transition-colors ${
          !rightsReady
            ? 'border-gray-200 opacity-60 dark:border-gray-700'
            : isDragOver
              ? 'border-blue-400 bg-blue-50 dark:border-blue-600 dark:bg-blue-900/20'
              : 'border-gray-300 dark:border-gray-600'
        }`}
        onDragOver={(e) => {
          e.preventDefault();
          if (rightsReady) setIsDragOver(true);
        }}
        onDragLeave={() => setIsDragOver(false)}
        onDrop={handleDrop}
        onPaste={(event) => {
          const file = Array.from(event.clipboardData.files).find((item) =>
            item.type.startsWith('image/')
          );
          if (file) {
            event.preventDefault();
            void handleFile(file);
          }
        }}
        tabIndex={0}
        role="region"
        aria-label={t('references.upload-zone')}
      >
        <p className="text-sm text-gray-600 dark:text-gray-400">
          {rightsReady
            ? t('references.drop-or-browse')
            : t('references.confirm-rights-first')}
        </p>
        <button
          type="button"
          className="btn btn-ghost btn-sm mt-2"
          disabled={!rightsReady}
          onClick={() => fileInputRef.current?.click()}
        >
          {t('references.browse-files')}
        </button>
        <input
          ref={fileInputRef}
          type="file"
          accept="image/png,image/jpeg,image/webp"
          className="hidden"
          onChange={handleInputChange}
          aria-label={t('references.file-input')}
        />
      </div>
      {fileError && (
        <p role="alert" className="text-sm text-error">
          {fileError}
        </p>
      )}
    </div>
  );
}
