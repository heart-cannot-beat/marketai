import { useTranslation } from 'next-i18next';
import type { LibraryEntryType } from '@/lib/library/navigation';
import type { LibraryCommandKind } from '@/hooks/library/command-definitions';

export interface AllowedAction {
  command: LibraryCommandKind;
  allowed: boolean;
  reason_code: string | null;
}

interface EntryActionsProps {
  entryId: string;
  entryType: LibraryEntryType;
  lifecycle: string;
  allowedActions: AllowedAction[];
  onPublish: () => void;
  onArchive: () => void;
  onFork: () => void;
  onClone: () => void;
  onApplyTemplate: () => void;
  onCreateEntry: () => void;
  onBindReference: () => void;
}

const ENTRY_COMMANDS: LibraryCommandKind[] = [
  'publish',
  'archive',
  'fork',
  'clone',
  'apply_template',
  'bind_reference',
];

export function EntryActions({
  allowedActions,
  onPublish,
  onArchive,
  onFork,
  onClone,
  onApplyTemplate,
  onCreateEntry,
  onBindReference,
}: EntryActionsProps) {
  const { t } = useTranslation('library');

  const handlers: Record<string, () => void> = {
    publish: onPublish,
    archive: onArchive,
    fork: onFork,
    clone: onClone,
    apply_template: onApplyTemplate,
    bind_reference: onBindReference,
  };

  const actionMap = new Map(allowedActions.map((a) => [a.command, a]));

  const visibleActions = ENTRY_COMMANDS.filter((cmd) => actionMap.has(cmd));

  if (visibleActions.length === 0 && !actionMap.has('create_entry')) {
    return null;
  }

  return (
    <div
      className="flex flex-wrap items-center gap-2"
      role="toolbar"
      aria-label={t('commands.entry-actions')}
    >
      {actionMap.has('create_entry') && (
        <ActionButton
          command="create_entry"
          action={actionMap.get('create_entry')!}
          label={t('commands.create-entry')}
          onClick={onCreateEntry}
          variant="primary"
        />
      )}
      {visibleActions.map((cmd) => {
        const action = actionMap.get(cmd)!;
        const handler = handlers[cmd];
        return (
          <ActionButton
            key={cmd}
            command={cmd}
            action={action}
            label={t(`commands.${cmd}`)}
            onClick={handler}
            variant={
              cmd === 'publish'
                ? 'primary'
                : cmd === 'archive'
                  ? 'warning'
                  : 'default'
            }
          />
        );
      })}
    </div>
  );
}

interface ActionButtonProps {
  command: string;
  action: AllowedAction;
  label: string;
  onClick: () => void;
  variant: 'primary' | 'warning' | 'default';
}

const VARIANT_CLASSES: Record<string, string> = {
  primary: 'btn-primary',
  warning: 'btn-warning',
  default: 'btn-outline',
};

function ActionButton({
  command,
  action,
  label,
  onClick,
  variant,
}: ActionButtonProps) {
  const { t } = useTranslation('library');
  const denied = !action.allowed;

  return (
    <button
      type="button"
      className={`btn btn-sm ${VARIANT_CLASSES[variant]} ${denied ? 'btn-disabled opacity-60' : ''}`}
      disabled={denied}
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
        onClick();
      }}
      data-command={command}
    >
      {label}
    </button>
  );
}
