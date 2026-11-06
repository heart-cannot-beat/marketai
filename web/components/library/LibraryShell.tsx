import { useTranslation } from 'next-i18next';
import { useLibraryRoute } from '@/hooks/library/useLibraryRoute';
import { LibraryCategoryNav } from './LibraryCategoryNav';
import { LibraryListView } from './list/LibraryListView';
import { LibraryEntryDetail } from './detail/LibraryEntryDetail';

export function LibraryShell() {
  const { t } = useTranslation('library');
  const { state, setCategory, setEntry, setTab, setVersion, setResource } =
    useLibraryRoute();

  return (
    <div data-testid="library-shell">
      <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100 mb-4">
        {t('title')}
      </h1>

      {/* Mobile/medium category selector — hidden on lg+ */}
      <div className="lg:hidden mb-3">
        <LibraryCategoryNav
          activeCategory={state.cat}
          onCategoryChange={setCategory}
        />
      </div>

      {/* Three-pane desktop; two-pane medium; drill-in mobile */}
      <div className="flex gap-4 min-h-[calc(100vh-14rem)]">
        {/* Category nav sidebar — lg+ only */}
        <aside className="hidden lg:block w-44 shrink-0">
          <LibraryCategoryNav
            activeCategory={state.cat}
            onCategoryChange={setCategory}
          />
        </aside>

        {/* Entry list — hidden on mobile when an entry is selected */}
        <div
          className={`w-full md:w-72 md:shrink-0 border-r border-gray-200 dark:border-gray-700 overflow-hidden ${
            state.entry ? 'hidden md:block' : ''
          }`}
        >
          <LibraryListView
            category={state.cat}
            selectedEntryId={state.entry}
            onSelectEntry={(id) => setEntry(id)}
          />
        </div>

        {/* Detail pane — hidden on mobile when no entry is selected */}
        <div
          className={`flex-1 min-w-0 ${!state.entry ? 'hidden md:block' : ''}`}
        >
          <LibraryEntryDetail
            route={state}
            onEntryChange={(id) => setEntry(id)}
            onTabChange={setTab}
            onVersionChange={setVersion}
            onDismissResource={() => setResource(null, null)}
          />
        </div>
      </div>
    </div>
  );
}
