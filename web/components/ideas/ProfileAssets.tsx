import { useId, useState, type ReactNode } from 'react';
import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { useIdeasCommands } from '@/hooks/ideas/useIdeasCommands';
import {
  useProductDirectory,
  type ProductSummary,
} from '@/hooks/products/useProductDirectory';
import { ReferenceThumbnail } from '@/components/library/references/ReferenceThumbnail';
import { ProductBrandKitSettings } from '@/components/products/ProductBrandKitSettings';
import type { IdeasDetail } from '@/lib/ideas/ideas-view';
import {
  adsWithPhoto,
  photoDecision,
  type IdeasPhoto,
} from '@/lib/ideas/photos';
import {
  PhotoFailureNote,
  PhotoStatusBadge,
  PhotoThumbnail,
  RejectPhotoQuestion,
  useCheckDeclaration,
  usePhotoFailure,
  usePhotoLabel,
  type PhotoFailure,
} from './PhotoParts';

/**
 * The photos and brand of a product's profile (ADR 0018, design §5.6, #202,
 * #229).
 *
 * The photos are the ones the product's reading found (`IdeasPhoto`), each
 * with its checking: they can be checked -- the declaration that it is a
 * real photo of this product the user may use in ads -- or rejected, which
 * makes the ads made with it need redoing. The check before downloading
 * checks the same photos the same way, from the same answer.
 *
 * Ideas no first batch opened have no photos of their own: their ads use the
 * product's published photos, listed read only. The photo editor
 * (`ProductReferenceSettings`) is never shown here: a product of the new flow
 * has no published product card for it to bind to, and once its checked
 * content is published the editor would publish over it.
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

export function ProfilePhotos({
  productId,
  projectId,
  detail,
}: {
  productId: string;
  projectId: string;
  detail: IdeasDetail;
}) {
  const photos = detail.photos ?? [];
  if (photos.length > 0)
    return (
      <CandidatePhotos projectId={projectId} detail={detail} photos={photos} />
    );
  return <PublishedPhotos productId={productId} />;
}

/** One question open at a time: check this photo, or reject it. */
type Asking = { kind: 'check' | 'reject'; photoId: string } | null;

function CandidatePhotos({
  projectId,
  detail,
  photos,
}: {
  projectId: string;
  detail: IdeasDetail;
  photos: IdeasPhoto[];
}) {
  const { t } = useTranslation('ideas');
  const { verify, refresh } = useIdeasCommands(projectId, detail);
  const directory = useProductDirectory();
  const failureOf = usePhotoFailure();
  const declaration = useCheckDeclaration()(detail.understanding?.product_name);
  const labelOf = usePhotoLabel();
  const ids = useId();
  const [asking, setAsking] = useState<Asking>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [failure, setFailure] = useState<{
    photoId: string;
    failure: PhotoFailure;
  } | null>(null);

  async function decide(photo: IdeasPhoto, check: boolean) {
    if (busy) return;
    setBusy(photo.photo_id);
    setFailure(null);
    const outcome = await verify(
      check
        ? photoDecision([photo.photo_id], [])
        : photoDecision([], [photo.photo_id])
    );
    setBusy(null);
    const failed = failureOf(outcome);
    if (failed) {
      setFailure({ photoId: photo.photo_id, failure: failed });
      return;
    }
    setAsking(null);
    // A checked photo becomes part of the product's confirmed details.
    void directory.refresh();
  }

  const failureAt = (photoId: string) =>
    failure?.photoId === photoId ? failure.failure : null;

  return (
    <Section
      title={t('profile-photos-title')}
      intro={t('profile-photos-intro')}
      testId="profile-photos"
    >
      <p className="text-sm text-gray-600 dark:text-gray-400">
        {t('profile-photos-expiry')}
      </p>
      <ul role="list" className="grid gap-4 sm:grid-cols-2">
        {photos.map((photo, index) => {
          const label = labelOf(photo);
          const name = t('profile-photo-name', {
            number: index + 1,
            role: label.role,
          });
          const nameId = `${ids}-${index}`;
          const used = adsWithPhoto(detail, photo.photo_id).length;
          const open = asking?.photoId === photo.photo_id ? asking.kind : null;
          const working = busy === photo.photo_id;
          return (
            <li
              key={photo.photo_id}
              className="space-y-3 rounded-xl border border-gray-200 bg-white p-3 dark:border-gray-700 dark:bg-gray-900"
              data-testid="profile-photo"
            >
              <PhotoThumbnail photo={photo} alt={name} size="lg" />
              <div className="space-y-1">
                <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                  <p id={nameId} className="text-sm font-semibold">
                    {name}
                  </p>
                  <PhotoStatusBadge status={photo.status} />
                </div>
                <p className="text-xs text-gray-600 dark:text-gray-400">
                  {label.source}
                </p>
                <p className="text-xs text-gray-600 dark:text-gray-400">
                  {used > 0
                    ? t('profile-photo-used', { count: used })
                    : t('profile-photo-unused')}
                </p>
                <p className="text-sm">
                  {t(`profile-photo-means-${photo.status}`)}
                </p>
              </div>
              {open === 'check' && (
                <CheckPhotoQuestion
                  declaration={declaration}
                  used={used}
                  status={photo.status}
                  busy={working}
                  failure={failureAt(photo.photo_id)}
                  onRefresh={() => void refresh()}
                  onCheck={() => void decide(photo, true)}
                  onCancel={() => {
                    setFailure(null);
                    setAsking(null);
                  }}
                />
              )}
              {open === 'reject' && (
                <RejectPhotoQuestion
                  used={used}
                  busy={working}
                  failure={failureAt(photo.photo_id)}
                  onRefresh={() => void refresh()}
                  onReject={() => void decide(photo, false)}
                  onCancel={() => {
                    setFailure(null);
                    setAsking(null);
                  }}
                />
              )}
              {open === null && photo.status !== 'unavailable' && (
                <div className="flex flex-wrap gap-2">
                  {photo.status !== 'verified' && (
                    <button
                      type="button"
                      className="btn btn-sm min-h-11"
                      disabled={busy !== null}
                      aria-describedby={nameId}
                      onClick={() => {
                        setFailure(null);
                        setAsking({ kind: 'check', photoId: photo.photo_id });
                      }}
                    >
                      {photo.status === 'rejected'
                        ? t('profile-photo-check-again')
                        : t('profile-photo-check')}
                    </button>
                  )}
                  {photo.status !== 'rejected' && (
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm min-h-11"
                      disabled={busy !== null}
                      aria-describedby={nameId}
                      onClick={() => {
                        setFailure(null);
                        setAsking({ kind: 'reject', photoId: photo.photo_id });
                      }}
                    >
                      {t('photo-reject-open')}
                    </button>
                  )}
                </div>
              )}
              {open === null && (
                <PhotoFailureNote
                  failure={failureAt(photo.photo_id)}
                  onRefresh={() => void refresh()}
                />
              )}
            </li>
          );
        })}
      </ul>
      <p className="text-xs text-gray-600 dark:text-gray-400">
        {t('profile-photos-upload-later')}
      </p>
    </Section>
  );
}

/**
 * Asked before a photo is checked (#199, #229): the declaration in full --
 * a real photo of this product, one the user may use in ads -- and what
 * checking it does for the ads made with it.
 */
function CheckPhotoQuestion({
  declaration,
  used,
  status,
  busy,
  failure,
  onRefresh,
  onCheck,
  onCancel,
}: {
  declaration: string;
  used: number;
  status: IdeasPhoto['status'];
  busy: boolean;
  failure: PhotoFailure | null;
  onRefresh: () => void;
  onCheck: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation('ideas');
  const title = useId();
  return (
    <div
      role="group"
      aria-labelledby={title}
      className="space-y-3 rounded-lg border border-gray-300 bg-gray-50 p-3 text-sm dark:border-gray-600 dark:bg-gray-950"
    >
      <p id={title} className="font-semibold">
        {t('profile-photo-check-title')}
      </p>
      <p>{declaration}</p>
      {used > 0 && (
        <p>
          {status === 'rejected'
            ? t('profile-photo-check-ads-rejected', { count: used })
            : t('profile-photo-check-ads', { count: used })}
        </p>
      )}
      <PhotoFailureNote failure={failure} onRefresh={onRefresh} />
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          className="btn btn-primary btn-sm min-h-11"
          disabled={busy}
          onClick={onCheck}
        >
          {busy ? t('photo-saving') : t('profile-photo-check-confirm')}
        </button>
        <button
          type="button"
          className="btn btn-ghost btn-sm min-h-11"
          disabled={busy}
          onClick={onCancel}
        >
          {t('photo-cancel')}
        </button>
      </div>
    </div>
  );
}

/**
 * Ideas no first batch opened (#199): their ads use the product's published
 * photos, shown read only; or none, when the product has none.
 */
function PublishedPhotos({ productId }: { productId: string }) {
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
      intro={
        items.length > 0
          ? t('profile-photos-published-intro')
          : t('profile-photos-plain-intro')
      }
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
      <p className="text-xs text-gray-600 dark:text-gray-400">
        {t('profile-photos-upload-later')}
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
