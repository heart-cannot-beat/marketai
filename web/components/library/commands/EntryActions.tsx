import { useId } from 'react';
import { useTranslation } from 'next-i18next';
import type { LibraryEntryType } from '@/lib/library/navigation';
import type { LibraryCommandKind } from '@/hooks/library/command-definitions';

export interface AllowedAction {
  command: LibraryCommandKind;
  allowed: boolean;
  reason_code: string | null;
}

type TFunction = ReturnType<typeof useTranslation>['t'];

/**
 * Why the server refused a Library command (`ActionEligibility.reason_code`,
 * given by `_entry_allowed_actions` and `_version_allowed_actions` in
 * `app/library/v1_router.py`), worded as `commands.denied.<code>` (#399). A
 * code without copy, from a server newer than this page, reads as a generic
 * sentence instead of the code.
 */
export function deniedReason(t: TFunction, code: string | null): string {
  const fallback = t('commands.denied-default');
  return code
    ? t(`commands.denied.${code}`, { defaultValue: fallback })
    : fallback;
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
  const reasonId = useId();

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

  const label = (cmd: LibraryCommandKind) =>
    cmd === 'create_entry' ? t('commands.create-entry') : t(`commands.${cmd}`);

  // The server's reasons under the toolbar, one line per `reason_code` naming
  // the buttons it holds back, so touch and keyboard users see them too.
  const reasons = new Map<string, string[]>();
  for (const cmd of [
    ...(actionMap.has('create_entry') ? (['create_entry'] as const) : []),
    ...visibleActions,
  ]) {
    const action = actionMap.get(cmd)!;
    if (action.allowed) continue;
    const code = action.reason_code ?? '';
    reasons.set(code, [...(reasons.get(code) ?? []), label(cmd)]);
  }
  const reasonCodes = Array.from(reasons.keys());
  const lineId = (code: string) => `${reasonId}-${reasonCodes.indexOf(code)}`;
  const describedBy = (action: AllowedAction) =>
    action.allowed ? undefined : lineId(action.reason_code ?? '');

  return (
    <div className="space-y-1">
      <div
        className="flex flex-wrap items-center gap-2"
        role="toolbar"
        aria-label={t('commands.entry-actions')}
      >
        {actionMap.has('create_entry') && (
          <ActionButton
            command="create_entry"
            action={actionMap.get('create_entry')!}
            label={label('create_entry')}
            onClick={onCreateEntry}
            variant="primary"
            describedBy={describedBy(actionMap.get('create_entry')!)}
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
              label={label(cmd)}
              onClick={handler}
              variant={
                cmd === 'publish'
                  ? 'primary'
                  : cmd === 'archive'
                    ? 'warning'
                    : 'default'
              }
              describedBy={describedBy(action)}
            />
          );
        })}
      </div>
      {reasons.size > 0 && (
        <ul className="space-y-0.5 text-xs text-gray-500 dark:text-gray-400">
          {Array.from(reasons, ([code, labels]) => (
            <li key={code} id={lineId(code)}>
              {t('commands.denied-line', {
                actions: labels.join(t('commands.action-separator')),
                reason: deniedReason(t, code || null),
              })}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

interface ActionButtonProps {
  command: string;
  action: AllowedAction;
  label: string;
  onClick: () => void;
  variant: 'primary' | 'warning' | 'default';
  /** The reason line under the toolbar, for a refused action. */
  describedBy?: string;
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
  describedBy,
}: ActionButtonProps) {
  const { t } = useTranslation('library');
  const denied = !action.allowed;

  // Not `disabled`: a refused action stays focusable and reads its reason.
  return (
    <button
      type="button"
      className={`btn btn-sm ${VARIANT_CLASSES[variant]} ${denied ? 'btn-disabled opacity-60' : ''}`}
      aria-disabled={denied || undefined}
      aria-describedby={describedBy}
      title={denied ? deniedReason(t, action.reason_code) : undefined}
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
