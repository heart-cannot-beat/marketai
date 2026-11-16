import { useWorkspace } from './WorkspaceProvider';
import { useOptionalNavigationBlocker } from './NavigationBlocker';
import { useTranslation } from 'next-i18next';

/** `id` keeps the label wired when the phone menu renders a second copy. */
export function WorkspaceSelector({
  id = 'workspace-selector',
}: { id?: string } = {}) {
  const { t } = useTranslation('common');
  const { current, available, pendingSlug, switchWorkspace } = useWorkspace();
  const blocker = useOptionalNavigationBlocker();

  const handleChange = (slug: string) => {
    if (blocker && blocker.isBlocked()) {
      const confirmed = window.confirm(
        'You have unsaved changes. Discard them and switch workspaces?'
      );
      if (!confirmed) return;
    }
    switchWorkspace(slug);
  };

  return (
    <div className="px-2 py-3">
      <label htmlFor={id} className="sr-only">
        {t('workspace-switch')}
      </label>
      <select
        id={id}
        value={pendingSlug ?? current.slug}
        onChange={(e) => handleChange(e.target.value)}
        disabled={!!pendingSlug}
        className="select select-bordered select-sm w-full"
      >
        {available.map((w) => (
          <option key={w.workspaceId} value={w.slug}>
            {w.displayName}
          </option>
        ))}
      </select>
    </div>
  );
}
