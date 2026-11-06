import { useTranslation } from 'next-i18next';
import type { AllowedAction } from './EntryActions';

interface VersionActionsProps {
  versionId: string;
  allowedActions: AllowedAction[];
  onWithdraw: () => void;
  onFork: () => void;
  onClone: () => void;
  onDownload: () => void;
}

const VERSION_COMMANDS = [
  'withdraw',
  'fork',
  'clone',
  'create_reference_download',
] as const;

export function VersionActions({
  allowedActions,
  onWithdraw,
  onFork,
  onClone,
  onDownload,
}: VersionActionsProps) {
  const { t } = useTranslation('library');

  const handlers: Record<string, () => void> = {
    withdraw: onWithdraw,
    fork: onFork,
    clone: onClone,
    create_reference_download: onDownload,
  };

  const actionMap = new Map(allowedActions.map((a) => [a.command, a]));
  const visible = VERSION_COMMANDS.filter((cmd) => actionMap.has(cmd));

  if (visible.length === 0) return null;

  return (
    <div
      className="flex flex-wrap items-center gap-2"
      role="toolbar"
      aria-label={t('commands.version-actions')}
    >
      {visible.map((cmd) => {
        const action = actionMap.get(cmd)!;
        const denied = !action.allowed;

        return (
          <button
            key={cmd}
            type="button"
            className={`btn btn-sm btn-outline ${denied ? 'btn-disabled opacity-60' : ''} ${
              cmd === 'withdraw' ? 'btn-warning' : ''
            }`}
            aria-disabled={denied || undefined}
            title={
              denied && action.reason_code
                ? t(`commands.denied.${action.reason_code}`, {
                    defaultValue: action.reason_code,
                  })
                : undefined
            }
            onClick={(e) => {
              if (denied) {
                e.preventDefault();
                return;
              }
              handlers[cmd]();
            }}
            data-command={cmd}
          >
            {t(`commands.${cmd}`)}
          </button>
        );
      })}
    </div>
  );
}
