import { useTranslation } from 'next-i18next';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';

interface DimensionTemplateEntry {
  id: string;
  display_name?: string;
}

interface DimensionConfiguratorProps {
  dimensionTemplateIds: string[];
  onUpdate: (dimensionTemplateIds: string[]) => void;
  disabled?: boolean;
}

/**
 * Lets the draft's selectors reference one or more Library `dimension_template`
 * entries (`entry_type: 'dimension_template'`, per
 * `components/library/entry-types/registry.tsx`). This is a first-delivery
 * stub over entries rather than specific `DimensionTemplateVersion`s -- there
 * is no read operation that lists versions across every entry of a type in
 * one call (`list_library_entry_versions` takes a single `entry_id`), so
 * version-level pinning is left to a follow-up task. Selected ids are
 * written to `selectors.dimension_template_ids` by the caller (`DraftComposer`).
 */
export function DimensionConfigurator({
  dimensionTemplateIds,
  onUpdate,
  disabled,
}: DimensionConfiguratorProps) {
  const { t } = useTranslation('common');
  const { data, isLoading } = useDomainQuery('list_library_entries', {
    entry_type: 'dimension_template',
    limit: 100,
  } as any);

  const templates =
    ((data as Record<string, unknown> | undefined)?.items as
      | DimensionTemplateEntry[]
      | undefined) ?? [];

  function toggle(id: string) {
    if (disabled) return;
    const next = dimensionTemplateIds.includes(id)
      ? dimensionTemplateIds.filter((existing) => existing !== id)
      : [...dimensionTemplateIds, id];
    onUpdate(next);
  }

  return (
    <div className="form-control">
      <span className="label-text font-medium">
        {t('generation-draft-dimensions-label')}
      </span>
      {isLoading && (
        <span className="text-xs text-base-content/60">
          {t('generation-draft-dimensions-loading')}
        </span>
      )}
      {!isLoading && templates.length === 0 && (
        <span className="text-xs text-base-content/60">
          {t('generation-draft-dimensions-empty')}
        </span>
      )}
      <div className="flex flex-col gap-1 mt-1">
        {templates.map((template) => (
          <label
            key={template.id}
            className="label cursor-pointer justify-start gap-2"
          >
            <input
              type="checkbox"
              className="checkbox checkbox-sm"
              checked={dimensionTemplateIds.includes(template.id)}
              disabled={disabled}
              onChange={() => toggle(template.id)}
            />
            <span className="label-text">
              {template.display_name ?? template.id}
            </span>
          </label>
        ))}
      </div>
    </div>
  );
}
