import { useMemo, useState } from 'react';
import { useTranslation } from 'next-i18next';
import type { LibraryCommandKind } from '@/hooks/library/command-definitions';
import {
  LIBRARY_ENTRY_TYPES,
  getPlacement,
  isInternal,
  isTopLevel,
  type InternalEntryType,
  type LibraryEntryType,
} from '@/lib/library/navigation';
import { EntryActions, type AllowedAction } from './EntryActions';
import { PublishDialog } from './PublishDialog';
import { ArchiveDialog } from './ArchiveDialog';
import { BindReferenceDialog } from './BindReferenceDialog';
import { CreateInternalEntryDialog } from '../nested/CreateInternalEntryDialog';

interface ServerAllowedAction {
  action: string;
  allowed: boolean;
  reason_code?: string | null;
}

interface LibraryEntryCommandsProps {
  entryId: string;
  entryType: LibraryEntryType;
  lifecycle: string;
  draftRevision: number | null;
  allowedActions: ServerAllowedAction[];
  onRefresh: () => void | Promise<void>;
  onEntryChange: (entryId: string) => void;
}

type DialogKind = 'publish' | 'archive' | 'bind_reference' | null;

const ENTRY_ACTIONS = new Set<LibraryCommandKind>([
  'publish',
  'archive',
  'bind_reference',
]);

export function LibraryEntryCommands({
  entryId,
  entryType,
  lifecycle,
  draftRevision,
  allowedActions,
  onRefresh,
  onEntryChange,
}: LibraryEntryCommandsProps) {
  const { t } = useTranslation('library');
  const [dialog, setDialog] = useState<DialogKind>(null);
  const [internalType, setInternalType] = useState<InternalEntryType | null>(
    null
  );

  const actions = useMemo<AllowedAction[]>(
    () =>
      allowedActions.flatMap((action) =>
        ENTRY_ACTIONS.has(action.action as LibraryCommandKind)
          ? [
              {
                command: action.action as LibraryCommandKind,
                allowed: action.allowed,
                reason_code: action.reason_code ?? null,
              },
            ]
          : []
      ),
    [allowedActions]
  );

  const childTypes = useMemo<InternalEntryType[]>(() => {
    if (!isTopLevel(entryType)) return [];
    return LIBRARY_ENTRY_TYPES.filter(
      (candidate): candidate is InternalEntryType => {
        if (!isInternal(candidate)) return false;
        const placement = getPlacement(candidate);
        return (
          placement.kind === 'internal' &&
          placement.parentKinds.includes(entryType)
        );
      }
    );
  }, [entryType]);

  const finish = () => {
    setDialog(null);
    void onRefresh();
  };

  return (
    <div className="space-y-2">
      <EntryActions
        entryId={entryId}
        entryType={entryType}
        lifecycle={lifecycle}
        allowedActions={actions}
        onPublish={() => setDialog('publish')}
        onArchive={() => setDialog('archive')}
        onFork={() => undefined}
        onClone={() => undefined}
        onApplyTemplate={() => undefined}
        onCreateEntry={() => undefined}
        onBindReference={() => setDialog('bind_reference')}
      />

      {childTypes.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          {childTypes.map((childType) => (
            <button
              key={childType}
              type="button"
              className="btn btn-sm btn-outline"
              onClick={() => setInternalType(childType)}
            >
              {t('contextual-create.new', {
                type: t(`entry-type.${childType}`),
              })}
            </button>
          ))}
        </div>
      )}

      {/* Non-blocking warnings are acknowledged inside the dialog itself
          (per impact projection), so nothing is pre-acknowledged here. */}
      {dialog === 'publish' && (
        <PublishDialog
          entryId={entryId}
          open
          onClose={() => setDialog(null)}
          onPublished={finish}
        />
      )}
      {dialog === 'archive' && (
        <ArchiveDialog
          entryId={entryId}
          open
          onClose={() => setDialog(null)}
          onArchived={finish}
        />
      )}
      {dialog === 'bind_reference' && draftRevision !== null && (
        <BindReferenceDialog
          entryId={entryId}
          draftEtag={`"${draftRevision}"`}
          open
          onClose={() => setDialog(null)}
          onBound={finish}
        />
      )}
      {internalType && (
        <CreateInternalEntryDialog
          entryType={internalType}
          parentEntryId={entryId}
          parentEntryType={entryType}
          rootEntryId={entryId}
          open
          onClose={() => setInternalType(null)}
          onCreated={(createdEntryId) => {
            setInternalType(null);
            onEntryChange(createdEntryId);
          }}
        />
      )}
    </div>
  );
}
