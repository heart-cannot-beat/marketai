import { useTranslation } from 'next-i18next';

interface UnsupportedSchemaStateProps {
  schemaVersion: number;
  entryType: string;
}

export function UnsupportedSchemaState({
  schemaVersion,
  entryType,
}: UnsupportedSchemaStateProps) {
  const { t } = useTranslation('library');

  return (
    <div
      className="rounded-lg border border-amber-300 bg-amber-50 p-6 dark:border-amber-700 dark:bg-amber-950"
      role="status"
    >
      <p className="text-sm font-medium text-amber-800 dark:text-amber-200">
        {t('draft.unsupported-schema')}
      </p>
      <p className="mt-1 text-sm text-amber-700 dark:text-amber-300">
        {t('draft.unsupported-schema-description', {
          version: String(schemaVersion),
          type: entryType,
        })}
      </p>
    </div>
  );
}
