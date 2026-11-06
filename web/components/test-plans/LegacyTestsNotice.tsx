import Link from 'next/link';
import { useTranslation } from 'next-i18next';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';

/**
 * The legacy test flow is frozen (#337): its pages only show what was
 * drafted and generated, and new tests start in the marketing test
 * (`/w/{slug}/experiments`).
 */
export function LegacyTestsNotice() {
  const { t } = useTranslation('tests');
  const { current } = useWorkspace();
  return (
    <div
      className="alert alert-info text-sm"
      role="note"
      data-testid="legacy-tests-notice"
    >
      <span>
        {t('legacy-notice')}{' '}
        <Link
          href={`/w/${current.slug}/experiments`}
          className="link font-medium"
        >
          {t('legacy-notice-link')}
        </Link>
      </span>
    </div>
  );
}
