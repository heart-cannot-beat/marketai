import type { LibraryTab } from './route-codec';
import type { LibraryEntryType } from './navigation';

export interface EntryTypeDefinition {
  entryType: LibraryEntryType;
  labelKey: string;
  iconKey: string;
  supportedTabs: readonly LibraryTab[];
  defaultTab: LibraryTab;
  hasDraft: boolean;
  hasVersions: boolean;
}
