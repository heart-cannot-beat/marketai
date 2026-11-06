import { useTranslation } from 'next-i18next';

interface LineageEntry {
  version_id: string;
  intent?: string | null;
}

interface VersionHistoryProps {
  parent: LineageEntry | null;
  childVersions: LineageEntry[];
  onNavigate: (versionId: string) => void;
}

const INTENT_LABEL_KEYS: Record<string, string> = {
  same_recipe: 'intent-same-recipe',
  new_seed: 'intent-new-seed',
  edit_prompt: 'intent-edit-prompt',
  quality_feedback: 'intent-quality-feedback',
};

/**
 * Parent / derived versions of an asset. Versions are named by position
 * (上一版 / 衍生版 N), never by their `av_` id (front-flow contract §1
 * principle 2); the id stays in the `title` tooltip for debugging.
 */
export function VersionHistory({
  parent,
  childVersions,
  onNavigate,
}: VersionHistoryProps) {
  const { t } = useTranslation('assets');
  if (!parent && childVersions.length === 0) return null;

  // An intent the domain adds later shows no badge rather than its raw value.
  const intentBadge = (intent: string | null | undefined) =>
    intent && INTENT_LABEL_KEYS[intent] ? (
      <span className="badge badge-xs badge-ghost">
        {t(INTENT_LABEL_KEYS[intent])}
      </span>
    ) : null;

  return (
    <div className="flex flex-col gap-2">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-base-content/50">
        {t('lineage-heading')}
      </h3>

      {parent && (
        <button
          type="button"
          className="flex items-center gap-1.5 text-sm text-primary hover:underline"
          onClick={() => onNavigate(parent.version_id)}
          title={parent.version_id}
          data-testid="lineage-parent"
        >
          <span className="text-base-content/40">&larr;</span>
          <span>{t('parent-version')}</span>
          {intentBadge(parent.intent)}
        </button>
      )}

      {childVersions.length > 0 && (
        <div className="flex flex-col gap-1 pl-4 border-l-2 border-base-300">
          {childVersions.map((child, index) => (
            <button
              key={child.version_id}
              type="button"
              className="flex items-center gap-1.5 text-sm text-primary hover:underline"
              onClick={() => onNavigate(child.version_id)}
              title={child.version_id}
              data-testid="lineage-child"
            >
              <span>{t('child-version', { index: index + 1 })}</span>
              {intentBadge(child.intent)}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
