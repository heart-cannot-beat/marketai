import { useTranslation } from 'next-i18next';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { GuardedLink } from '@/components/workspace/GuardedLink';

/**
 * The bodies of a product's views until their own slices fill them (ADR
 * 0018, step-1 plan): step 3 the results. The ad ideas (#196) and the
 * product profile (#202) have their own components (`components/ideas`),
 * and so does the test history (#258, `components/tests/TestHistory`). The
 * placeholder points at the page that holds the same thing today.
 */

function Placeholder({
  text,
  href,
  label,
  testId,
}: {
  text: string;
  href: string;
  label: string;
  testId: string;
}) {
  return (
    <div
      className="space-y-3 rounded-xl border border-dashed border-base-300 p-5"
      data-testid={testId}
    >
      <p className="max-w-prose text-sm text-base-content/80">{text}</p>
      <GuardedLink href={href} className="btn btn-sm">
        {label}
      </GuardedLink>
    </div>
  );
}

export function ProductResultsPlaceholder() {
  const { t } = useTranslation('common');
  const { current } = useWorkspace();
  return (
    <Placeholder
      text={t('product-results-placeholder')}
      href={`/w/${current.slug}/experiments`}
      label={t('product-open-earlier-tests')}
      testId="product-results-placeholder"
    />
  );
}
