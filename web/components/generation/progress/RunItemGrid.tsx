import { useTranslation } from 'next-i18next';
import { RunItemTile, type RunItemTileItem } from './RunItemTile';

interface RunItemGridProps {
  items: RunItemTileItem[];
  selectedItemId: string | null;
  onSelectItem: (itemId: string) => void;
  pagination: { cursor: string | null; hasMore: boolean };
  onPageChange: (cursor: string | null) => void;
}

/**
 * Selectable grid of a Run's RunItems -- one `RunItemTile` per item: the
 * produced asset's thumbnail, ordinal, state badge and combination value
 * tags (the full detail renders in `RunItemDetail` once selected).
 * `pagination`/`onPageChange` thread straight through to
 * `useRunProgress().itemsPagination`/`.setItemPage` (Task 8).
 */
export function RunItemGrid({
  items,
  selectedItemId,
  onSelectItem,
  pagination,
  onPageChange,
}: RunItemGridProps) {
  const { t } = useTranslation('common');

  if (items.length === 0) {
    return (
      <p className="text-sm text-base-content/60">
        {t('generation-run-items-empty')}
      </p>
    );
  }

  return (
    <div>
      <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-2">
        {items.map((item) => (
          <RunItemTile
            key={item.id}
            item={item}
            selected={selectedItemId === item.id}
            onSelect={onSelectItem}
          />
        ))}
      </div>

      {pagination.hasMore && (
        <div className="mt-3 flex justify-center">
          <button
            type="button"
            className="btn btn-sm btn-ghost"
            onClick={() => onPageChange(pagination.cursor)}
          >
            {t('generation-list-load-more')}
          </button>
        </div>
      )}
    </div>
  );
}
