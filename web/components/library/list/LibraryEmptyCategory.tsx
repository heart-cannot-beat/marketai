import { useTranslation } from 'next-i18next';
import type { LibraryCategory } from '@/lib/library/navigation';

interface LibraryEmptyCategoryProps {
  category: LibraryCategory;
}

export function LibraryEmptyCategory({ category }: LibraryEmptyCategoryProps) {
  const { t } = useTranslation('library');

  return (
    <div className="flex flex-col items-center justify-center py-12 text-center">
      <p className="text-sm font-medium text-gray-900 dark:text-gray-100">
        {t('list.empty', { category: t(`category.${category}`) })}
      </p>
      <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
        {t('list.empty-description')}
      </p>
    </div>
  );
}
