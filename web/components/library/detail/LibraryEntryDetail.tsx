import { useTranslation } from 'next-i18next';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import type { LibraryRouteState, LibraryTab } from '@/lib/library/route-codec';
import type { LibraryEntryType } from '@/lib/library/navigation';
import { getEntryTypeDefinition } from '@/components/library/entry-types/registry';
import { EntryHeader } from './EntryHeader';
import { ProblemState } from './ProblemState';
import { LibraryTabs } from './LibraryTabs';
import { CurrentTab } from '../tabs/CurrentTab';
import { DraftTab } from '../tabs/DraftTab';
import { VersionsTab } from '../tabs/VersionsTab';
import { EventsTab } from '../tabs/EventsTab';
import { ImpactTab } from '../tabs/ImpactTab';
import { BindingsTab } from '../tabs/BindingsTab';
import { ReferencesTab } from '../tabs/ReferencesTab';
import type { DraftReferenceBinding } from '../references/DraftReferenceBindings';
import { SchemaTab } from '../tabs/SchemaTab';
import { FactsTab } from '../tabs/FactsTab';
import { ResolutionTab } from '../tabs/ResolutionTab';
import { SettingsTab } from '../tabs/SettingsTab';
import { NestedResourcePanel } from '../nested/NestedResourcePanel';
import { LibraryEntryCommands } from '../commands/LibraryEntryCommands';

interface LibraryEntryDetailProps {
  route: LibraryRouteState;
  onEntryChange?: (entryId: string) => void;
  onTabChange: (tab: LibraryTab | null) => void;
  onVersionChange: (versionId: string | null) => void;
  onDismissResource: () => void;
}

interface EntryDetailData {
  id: string;
  entry_type: string;
  display_name: string;
  lifecycle: string;
  current_version_id?: string | null;
  draft?: {
    id: string;
    entry_id: string;
    revision: number;
    state: string;
    content_schema_version: number;
    content?: Record<string, unknown>;
    created_at?: string;
    updated_at?: string;
  } | null;
  revision: number;
  allowed_actions?: Array<{
    action: string;
    allowed: boolean;
    reason_code?: string | null;
  }>;
}

export function LibraryEntryDetail({
  route,
  onEntryChange,
  onTabChange,
  onVersionChange,
  onDismissResource,
}: LibraryEntryDetailProps) {
  const { t } = useTranslation('library');
  const entryId = route.entry;

  const { data, problem, isLoading, refresh } = useDomainQuery(
    'get_library_entry',
    entryId ? ({ entry_id: entryId } as any) : undefined,
    { enabled: !!entryId }
  );

  if (!entryId) {
    return (
      <div className="flex items-center justify-center h-full py-12 text-center">
        <p className="text-sm text-gray-500 dark:text-gray-400">
          {t('detail.no-selection')}
        </p>
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-12" aria-busy="true">
        <p className="text-sm text-gray-500 dark:text-gray-400">
          {t('detail.loading')}
        </p>
      </div>
    );
  }

  if (problem) {
    if (problem.status === 404) {
      return (
        <div
          className="flex flex-col items-center justify-center py-12 text-center"
          role="alert"
        >
          <p className="text-lg font-medium text-gray-900 dark:text-gray-100">
            {t('detail.not-found')}
          </p>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            {t('detail.not-found-description')}
          </p>
        </div>
      );
    }
    return <ProblemState onRetry={() => refresh()} />;
  }

  if (!data) return null;

  const entry = data as unknown as EntryDetailData;
  const definition = getEntryTypeDefinition(
    entry.entry_type as LibraryEntryType
  );
  const activeTab = route.tab ?? definition.defaultTab;

  const headerEntry = {
    id: entry.id,
    entry_type: entry.entry_type,
    display_name: entry.display_name,
    lifecycle: entry.lifecycle,
    draft_state: entry.draft?.state ?? null,
    current_version_number: null as number | null,
    current_version_state: null as string | null,
    eligibility: null as string | null,
    updated_at: '',
    created_at: '',
  };

  return (
    <div className="space-y-4">
      <EntryHeader entry={headerEntry} />

      <LibraryEntryCommands
        entryId={entry.id}
        entryType={entry.entry_type as LibraryEntryType}
        lifecycle={entry.lifecycle}
        draftRevision={entry.draft?.revision ?? null}
        allowedActions={entry.allowed_actions ?? []}
        onRefresh={refresh}
        onEntryChange={(entryId) => onEntryChange?.(entryId)}
      />

      <LibraryTabs
        definition={definition}
        activeTab={activeTab}
        onTabChange={onTabChange}
      >
        <ActiveTabContent
          tab={activeTab}
          entryId={entry.id}
          entryType={entry.entry_type as LibraryEntryType}
          currentVersionId={entry.current_version_id ?? null}
          draft={entry.draft ?? null}
          versionId={route.version}
          onGoToDraft={() => onTabChange('draft')}
          onSelectVersion={onVersionChange}
        />
      </LibraryTabs>

      {route.resource_type && route.resource && (
        <NestedResourcePanel
          rootEntryId={entryId}
          resourceType={route.resource_type}
          resourceId={route.resource}
          onDismiss={onDismissResource}
        />
      )}
    </div>
  );
}

interface ActiveTabContentProps {
  tab: LibraryTab;
  entryId: string;
  entryType: LibraryEntryType;
  currentVersionId: string | null;
  draft: EntryDetailData['draft'];
  versionId: string | null;
  onGoToDraft: () => void;
  onSelectVersion: (versionId: string | null) => void;
}

function ActiveTabContent({
  tab,
  entryId,
  entryType,
  currentVersionId,
  draft,
  versionId,
  onGoToDraft,
  onSelectVersion,
}: ActiveTabContentProps) {
  const { t } = useTranslation('library');

  switch (tab) {
    case 'overview':
      return (
        <CurrentTab
          entryId={entryId}
          currentVersionId={currentVersionId}
          onGoToDraft={onGoToDraft}
        />
      );
    case 'draft':
      return (
        <DraftTab
          entryId={entryId}
          entryType={entryType}
          draft={draft ?? null}
        />
      );
    case 'versions':
      return (
        <VersionsTab
          entryId={entryId}
          selectedVersionId={versionId}
          onSelectVersion={onSelectVersion}
        />
      );
    case 'events':
      return <EventsTab entryId={entryId} />;
    case 'impact':
      return (
        <ImpactTab
          entryId={entryId}
          versionId={versionId}
          currentVersionId={currentVersionId}
        />
      );
    case 'bindings':
      return (
        <BindingsTab
          entryId={entryId}
          versionId={versionId}
          currentVersionId={currentVersionId}
        />
      );
    case 'references': {
      const draftBindings = draft?.content?.reference_bindings;
      return (
        <ReferencesTab
          entryId={entryId}
          entryType={entryType}
          versionId={versionId}
          currentVersionId={currentVersionId}
          draftBindings={
            Array.isArray(draftBindings)
              ? (draftBindings as DraftReferenceBinding[])
              : []
          }
        />
      );
    }
    case 'schema':
      return (
        <SchemaTab
          entryId={entryId}
          entryType={entryType}
          versionId={versionId}
          currentVersionId={currentVersionId}
        />
      );
    case 'facts':
      return (
        <FactsTab
          entryId={entryId}
          entryType={entryType}
          currentVersionId={currentVersionId}
          draftContent={draft?.content ?? null}
        />
      );
    case 'resolution':
      return <ResolutionTab />;
    case 'settings':
      return <SettingsTab entryId={entryId} />;
    default:
      return (
        <div className="flex items-center justify-center py-12 text-center">
          <p className="text-sm text-gray-500 dark:text-gray-400">
            {t('tabs.placeholder', { tab })}
          </p>
        </div>
      );
  }
}
