import { useId, type ReactNode } from 'react';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import {
  useProductDirectory,
  type ProductSummary,
} from '@/hooks/products/useProductDirectory';
import { ReferenceThumbnail } from '@/components/library/references/ReferenceThumbnail';
import { ProductBrandKitSettings } from '@/components/products/ProductBrandKitSettings';

/**
 * The photos and brand of a product's profile (ADR 0018, design §5.6, #202).
 *
 * Photos are shown, not managed: a product of the new flow has no published
 * product card for the photo editor (`ProductReferenceSettings`) to bind to,
 * and once its checked facts are published the editor would publish over
 * them. Adding and checking photos here comes with #199; until then this
 * lists the photos ads may use today, the product's published ones.
 *
 * The brand is the product's brand baseline (`ProductBrandKitSettings`),
 * which belongs to the product itself and works for both flows.
 */

function Section({
  title,
  intro,
  children,
  testId,
}: {
  title: string;
  intro: string;
  children: ReactNode;
  testId: string;
}) {
  const heading = useId();
  return (
    <section
      aria-labelledby={heading}
      className="space-y-3"
      data-testid={testId}
    >
      <div className="space-y-1">
        <h2 id={heading} className="text-lg font-semibold">
          {title}
        </h2>
        <p className="text-sm text-gray-600 dark:text-gray-400">{intro}</p>
      </div>
      {children}
    </section>
  );
}

const ROLE_COPY: Record<string, string> = {
  primary: 'profile-photo-role-primary',
  detail: 'profile-photo-role-detail',
  logo: 'profile-photo-role-logo',
  certification_mark: 'profile-photo-role-certification',
};

export function ProfilePhotos({ productId }: { productId: string }) {
  const { t } = useTranslation('ideas');
  const directory = useProductDirectory();
  const summary = directory.find(productId);
  // Only a product with a published card has photos the ads can use.
  const published = Boolean(summary?.entry_id && summary.ready);
  const query = useDomainQuery(
    'get_product_references',
    { product_id: productId },
    { enabled: published }
  );
  const items = published ? (query.data?.items ?? []) : [];
  const failed =
    published &&
    !!query.problem &&
    query.problem.code !== 'product_reference_profile_required';

  return (
    <Section
      title={t('profile-photos-title')}
      intro={t('profile-photos-intro')}
      testId="profile-photos"
    >
      {failed ? (
        <div role="alert" className="space-y-2 text-sm">
          <p>{t('profile-photos-problem')}</p>
          <button
            type="button"
            className="btn btn-sm min-h-11"
            onClick={() => void query.refresh()}
          >
            {t('profile-retry')}
          </button>
        </div>
      ) : items.length === 0 ? (
        <p className="text-sm">{t('profile-photos-none')}</p>
      ) : (
        <ul role="list" className="grid grid-cols-2 gap-3 sm:grid-cols-3">
          {items.map((item) => {
            const checked = item.identity_status === 'verified_physical';
            const role = t(ROLE_COPY[item.role] ?? 'profile-photo-role-detail');
            return (
              <li key={item.item_id} className="space-y-1">
                <div className="aspect-square overflow-hidden rounded-lg border border-gray-200 dark:border-gray-700">
                  <ReferenceThumbnail
                    versionId={item.reference_version_id}
                    alt={role}
                  />
                </div>
                <p className="text-xs font-semibold">
                  {item.model_variant
                    ? t('profile-photo-role-model', {
                        role,
                        model: item.model_variant,
                      })
                    : role}
                </p>
                <span
                  className={classNames(
                    'inline-block rounded-full px-2 py-px text-xs font-semibold',
                    checked
                      ? 'bg-emerald-100 text-emerald-900'
                      : 'bg-amber-100 text-amber-900'
                  )}
                >
                  {checked
                    ? t('profile-photo-checked')
                    : t('profile-photo-unchecked')}
                </span>
                {!item.available && (
                  <p className="text-xs text-amber-800 dark:text-amber-300">
                    {t('profile-photo-unavailable')}
                  </p>
                )}
              </li>
            );
          })}
        </ul>
      )}
      <p className="rounded-lg border border-gray-200 p-3 text-sm text-gray-700 dark:border-gray-700 dark:text-gray-300">
        {t('profile-photos-later')}
      </p>
    </Section>
  );
}

export function ProfileBrand({ productId }: { productId: string }) {
  const { t } = useTranslation('ideas');
  const directory = useProductDirectory();
  // The brand baseline belongs to the product; a product the list does not
  // carry yet still has one.
  const summary: ProductSummary = directory.find(productId) ?? {
    product_id: productId,
    entry_id: null,
    ready: false,
  };
  return (
    <Section
      title={t('profile-brand-title')}
      intro={t('profile-brand-intro')}
      testId="profile-brand"
    >
      <div className="rounded-xl border border-gray-200 px-4 pb-4 dark:border-gray-700 [&>details]:mt-0 [&>details]:border-t-0">
        <ProductBrandKitSettings product={summary} />
      </div>
      <p className="text-xs text-gray-600 dark:text-gray-400">
        {t('profile-brand-logo-later')}
      </p>
    </Section>
  );
}
