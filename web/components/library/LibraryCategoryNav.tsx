import { useTranslation } from 'next-i18next';
import {
  LIBRARY_CATEGORIES,
  type LibraryCategory,
} from '@/lib/library/navigation';

interface LibraryCategoryNavProps {
  activeCategory: LibraryCategory;
  onCategoryChange: (category: LibraryCategory) => void;
}

export function LibraryCategoryNav({
  activeCategory,
  onCategoryChange,
}: LibraryCategoryNavProps) {
  const { t } = useTranslation('library');

  return (
    <nav aria-label={t('title')}>
      <ul role="list" className="flex flex-col gap-0.5">
        {LIBRARY_CATEGORIES.map((cat) => (
          <li key={cat}>
            <button
              type="button"
              onClick={() => onCategoryChange(cat)}
              aria-current={cat === activeCategory ? 'page' : undefined}
              className={`w-full text-left px-3 py-2 rounded-md text-sm transition-colors ${
                cat === activeCategory
                  ? 'bg-gray-100 dark:bg-gray-800 font-medium text-gray-900 dark:text-gray-100'
                  : 'text-gray-600 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800 hover:text-gray-900 dark:hover:text-gray-100'
              }`}
            >
              {t(`category.${cat}`)}
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}
