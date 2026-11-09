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

/** The server's verdict on one toolbar button. */
export interface ToolbarEligibility {
  allowed: boolean;
  reason_code: string | null;
}

export interface ToolbarButton {
  /** The server's action code, kept on the button as `data-command`. */
  command: string;
  label: string;
  action: ToolbarEligibility;
  onClick: () => void;
  variant: 'primary' | 'warning' | 'default';
}

interface ActionToolbarProps {
  label: string;
  buttons: ToolbarButton[];
}

/**
 * A Library toolbar: one button per action the server listed, and under it
 * the server's reasons, one line per `reason_code` naming the buttons it
 * holds back, so touch and keyboard users see them too.
 */
export function ActionToolbar({ label, buttons }: ActionToolbarProps) {
  const { t } = useTranslation('library');
  const reasonId = useId();

  const reasons = new Map<string, string[]>();
  for (const button of buttons) {
    if (button.action.allowed) continue;
    const code = button.action.reason_code ?? '';
    reasons.set(code, [...(reasons.get(code) ?? []), button.label]);
  }
  const reasonCodes = Array.from(reasons.keys());
  const lineId = (code: string) => `${reasonId}-${reasonCodes.indexOf(code)}`;

  return (
    <div className="space-y-1">
      <div
        className="flex flex-wrap items-center gap-2"
        role="toolbar"
        aria-label={label}
      >
        {buttons.map((button) => (
          <ActionButton
            key={button.command}
            command={button.command}
            action={button.action}
            label={button.label}
            onClick={button.onClick}
            variant={button.variant}
            describedBy={
              button.action.allowed
                ? undefined
                : lineId(button.action.reason_code ?? '')
            }
          />
        ))}
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

interface EntryActionsProps {
  entryId: string;
  entryType: LibraryEntryType;
  lifecycle: string;
  allowedActions: AllowedAction[];
  onPublish: () => void;
  onArchive: () => void;
  onCreateEntry: () => void;
  onBindReference: () => void;
}

/**
 * The entry-level commands `_entry_allowed_actions` can list. Fork and Clone
 * Product act on a published version and live on the version toolbar
 * (`VersionActions`).
 */
export const ENTRY_COMMANDS: LibraryCommandKind[] = [
  'publish',
  'archive',
  'bind_reference',
];

export function EntryActions({
  allowedActions,
  onPublish,
  onArchive,
  onCreateEntry,
  onBindReference,
}: EntryActionsProps) {
  const { t } = useTranslation('library');

  const handlers: Record<string, () => void> = {
    publish: onPublish,
    archive: onArchive,
    bind_reference: onBindReference,
  };

  const actionMap = new Map(allowedActions.map((a) => [a.command, a]));

  const visibleActions = ENTRY_COMMANDS.filter((cmd) => actionMap.has(cmd));

  if (visibleActions.length === 0 && !actionMap.has('create_entry')) {
    return null;
  }

  const buttons: ToolbarButton[] = [
    ...(actionMap.has('create_entry')
      ? [
          {
            command: 'create_entry',
            label: t('commands.create-entry'),
            action: actionMap.get('create_entry')!,
            onClick: onCreateEntry,
            variant: 'primary' as const,
          },
        ]
      : []),
    ...visibleActions.map((cmd) => ({
      command: cmd,
      label: t(`commands.${cmd}`),
      action: actionMap.get(cmd)!,
      onClick: handlers[cmd],
      variant:
        cmd === 'publish'
          ? ('primary' as const)
          : cmd === 'archive'
            ? ('warning' as const)
            : ('default' as const),
    })),
  ];

  return (
    <ActionToolbar label={t('commands.entry-actions')} buttons={buttons} />
  );
}

interface ActionButtonProps {
  command: string;
  action: ToolbarEligibility;
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
