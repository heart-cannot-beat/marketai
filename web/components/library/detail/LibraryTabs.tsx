import { useTranslation } from 'next-i18next';
import type { LibraryTab } from '@/lib/library/route-codec';
import type { EntryTypeDefinition } from '@/lib/library/entry-type-definition';

interface LibraryTabsProps {
  definition: EntryTypeDefinition;
  activeTab: LibraryTab;
  onTabChange: (tab: LibraryTab) => void;
  children?: React.ReactNode;
}

/**
 * Tabs whose `library:tabs.<tab>` locale entry is a key *group* (the panel's
 * own strings live under it) carry their label at `tabs.<tab>.label`;
 * resolving the group key itself returns an object, which i18next renders
 * as "key 'tabs.draft (en)' returned an object instead of string." in the
 * tab strip.
 */
const GROUPED_TABS: ReadonlySet<LibraryTab> = new Set<LibraryTab>([
  'draft',
  'versions',
  'events',
  'impact',
  'bindings',
]);

export function tabLabelKey(tab: LibraryTab): string {
  return GROUPED_TABS.has(tab) ? `tabs.${tab}.label` : `tabs.${tab}`;
}

export function LibraryTabs({
  definition,
  activeTab,
  onTabChange,
  children,
}: LibraryTabsProps) {
  const { t } = useTranslation('library');

  return (
    <div>
      <div
        role="tablist"
        aria-label={t('tabs.label')}
        className="flex gap-1 border-b border-gray-200 dark:border-gray-700 mb-4 overflow-x-auto"
      >
        {definition.supportedTabs.map((tab) => (
          <button
            key={tab}
            type="button"
            role="tab"
            aria-selected={tab === activeTab}
            aria-controls={`tabpanel-${tab}`}
            onClick={() => onTabChange(tab)}
            className={`px-3 py-2 text-sm whitespace-nowrap border-b-2 transition-colors ${
              tab === activeTab
                ? 'border-blue-500 text-blue-600 dark:text-blue-400 font-medium'
                : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 hover:border-gray-300'
            }`}
          >
            {t(tabLabelKey(tab))}
          </button>
        ))}
      </div>
      <div
        role="tabpanel"
        id={`tabpanel-${activeTab}`}
        aria-label={t(tabLabelKey(activeTab))}
      >
        {children}
      </div>
    </div>
  );
}
