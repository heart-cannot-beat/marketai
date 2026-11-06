import { useTranslation } from 'next-i18next';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';

interface ReferenceAssetEntry {
  id: string;
  display_name?: string;
}

interface ReferenceSelectorProps {
  referenceAssetIds: string[];
  onUpdate: (referenceAssetIds: string[]) => void;
  disabled?: boolean;
}

/**
 * Binds Library `reference_asset` entries (`entry_type: 'reference_asset'`,
 * per `components/library/entry-types/registry.tsx`) to the draft. A
 * first-delivery stub over entries, same caveat as `DimensionConfigurator` --
 * version-level `ReferenceAssetVersion` pinning is a follow-up. Selected ids
 * are written to `selectors.reference_asset_ids` by `DraftComposer`.
 */
export function ReferenceSelector({
  referenceAssetIds,
  onUpdate,
  disabled,
}: ReferenceSelectorProps) {
  const { t } = useTranslation('common');
  const { data, isLoading } = useDomainQuery('list_library_entries', {
    entry_type: 'reference_asset',
    limit: 100,
  } as any);

  const references =
    ((data as Record<string, unknown> | undefined)?.items as
      | ReferenceAssetEntry[]
      | undefined) ?? [];

  function toggle(id: string) {
    if (disabled) return;
    const next = referenceAssetIds.includes(id)
      ? referenceAssetIds.filter((existing) => existing !== id)
      : [...referenceAssetIds, id];
    onUpdate(next);
  }

  return (
    <div className="form-control">
      <span className="label-text font-medium">
        {t('generation-draft-references-label')}
      </span>
      {isLoading && (
        <span className="text-xs text-base-content/60">
          {t('generation-draft-references-loading')}
        </span>
      )}
      {!isLoading && references.length === 0 && (
        <span className="text-xs text-base-content/60">
          {t('generation-draft-references-empty')}
        </span>
      )}
      <div className="flex flex-col gap-1 mt-1">
        {references.map((reference) => (
          <label
            key={reference.id}
            className="label cursor-pointer justify-start gap-2"
          >
            <input
              type="checkbox"
              className="checkbox checkbox-sm"
              checked={referenceAssetIds.includes(reference.id)}
              disabled={disabled}
              onChange={() => toggle(reference.id)}
            />
            <span className="label-text">
              {reference.display_name ?? reference.id}
            </span>
          </label>
        ))}
      </div>
    </div>
  );
}
