import { useId, useState } from 'react';
import { useTranslation } from 'next-i18next';
import { useIdeasCommands } from '@/hooks/ideas/useIdeasCommands';
import { useIdeasPhotoUpload } from '@/hooks/ideas/useIdeasPhotoUpload';
import { useIdeasPhotoUrl } from '@/hooks/ideas/useIdeasPhotoUrl';
import { useProductDirectory } from '@/hooks/products/useProductDirectory';
import type { IdeasDetail } from '@/lib/ideas/ideas-view';
import { photoDecision, type IdeasLogo } from '@/lib/ideas/photos';
import { AddPhoto } from './AddPhoto';
import {
  PhotoFailureNote,
  PhotoStatusBadge,
  usePhotoFailure,
  type PhotoFailure,
} from './PhotoParts';

/**
 * The brand's logo on the product profile (#233, design §5.7: "参考照片、
 * Logo → 产品资料：产品照片、品牌").
 *
 * The user uploads the logo through the same upload gate as a photo; it is
 * listed here, never among the product photos, and never sent as the
 * product's look. Checking it declares that it is the brand's logo and the
 * user may use it in ads; it then joins the product's confirmed details as a
 * logo, which the brand baseline below can choose. Rejecting it asks first:
 * a baseline already saved with it keeps it until it is changed there.
 */

type Asking = { kind: 'check' | 'reject'; photoId: string } | null;

export function ProfileLogos({
  projectId,
  detail,
}: {
  projectId: string;
  detail: IdeasDetail;
}) {
  const { t } = useTranslation('ideas');
  const logos = detail.logos ?? [];
  const uploader = useIdeasPhotoUpload(projectId);
  const { verify, refresh } = useIdeasCommands(projectId, detail);
  const directory = useProductDirectory();
  const failureOf = usePhotoFailure();
  const ids = useId();
  const [asking, setAsking] = useState<Asking>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [failure, setFailure] = useState<{
    photoId: string;
    failure: PhotoFailure;
  } | null>(null);

  async function decide(logo: IdeasLogo, check: boolean) {
    if (busy) return;
    setBusy(logo.photo_id);
    setFailure(null);
    const outcome = await verify(
      check
        ? photoDecision([logo.photo_id], [])
        : photoDecision([], [logo.photo_id])
    );
    setBusy(null);
    const failed = failureOf(outcome);
    if (failed) {
      setFailure({ photoId: logo.photo_id, failure: failed });
      return;
    }
    setAsking(null);
    // A checked logo joins the product's confirmed details: the brand baseline
    // can choose it once the product list says so.
    void directory.refresh();
  }

  return (
    <div className="space-y-3" data-testid="profile-logos">
      {logos.length > 0 && (
        <ul role="list" className="grid gap-4 sm:grid-cols-2">
          {logos.map((logo, index) => {
            const name = t('profile-logo-name', { number: index + 1 });
            const nameId = `${ids}-${index}`;
            const open = asking?.photoId === logo.photo_id ? asking.kind : null;
            const failed =
              failure?.photoId === logo.photo_id ? failure.failure : null;
            const working = busy === logo.photo_id;
            return (
              <li
                key={logo.photo_id}
                className="space-y-3 rounded-xl border border-gray-200 bg-white p-3 dark:border-gray-700 dark:bg-gray-900"
                data-testid="profile-logo"
              >
                <LogoPicture
                  projectId={projectId}
                  photoId={logo.photo_id}
                  alt={name}
                />
                <div className="space-y-1">
                  <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                    <p id={nameId} className="text-sm font-semibold">
                      {name}
                    </p>
                    <PhotoStatusBadge status={logo.status} />
                  </div>
                  <p className="text-sm">
                    {t(`profile-logo-means-${logo.status}`)}
                  </p>
                </div>
                {open && (
                  <div
                    role="group"
                    aria-label={
                      open === 'check'
                        ? t('profile-logo-check')
                        : t('profile-logo-reject')
                    }
                    className="space-y-3 rounded-lg border border-gray-300 bg-gray-50 p-3 text-sm dark:border-gray-600 dark:bg-gray-950"
                  >
                    <p>
                      {open === 'check'
                        ? t('profile-logo-declaration')
                        : t('profile-logo-reject-question')}
                    </p>
                    <PhotoFailureNote
                      failure={failed}
                      onRefresh={() => void refresh()}
                    />
                    <div className="flex flex-wrap gap-2">
                      <button
                        type="button"
                        className="btn btn-primary btn-sm min-h-11"
                        disabled={working}
                        onClick={() => void decide(logo, open === 'check')}
                      >
                        {working
                          ? t('photo-saving')
                          : open === 'check'
                            ? t('profile-logo-check-confirm')
                            : t('profile-logo-reject-confirm')}
                      </button>
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm min-h-11"
                        disabled={working}
                        onClick={() => {
                          setFailure(null);
                          setAsking(null);
                        }}
                      >
                        {t('photo-cancel')}
                      </button>
                    </div>
                  </div>
                )}
                {!open && logo.status !== 'unavailable' && (
                  <div className="flex flex-wrap gap-2">
                    {logo.status !== 'verified' && (
                      <button
                        type="button"
                        className="btn btn-sm min-h-11"
                        disabled={busy !== null}
                        aria-describedby={nameId}
                        onClick={() => {
                          setFailure(null);
                          setAsking({ kind: 'check', photoId: logo.photo_id });
                        }}
                      >
                        {t('profile-logo-check')}
                      </button>
                    )}
                    {logo.status !== 'rejected' && (
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm min-h-11"
                        disabled={busy !== null}
                        aria-describedby={nameId}
                        onClick={() => {
                          setFailure(null);
                          setAsking({ kind: 'reject', photoId: logo.photo_id });
                        }}
                      >
                        {t('profile-logo-reject')}
                      </button>
                    )}
                  </div>
                )}
                {!open && (
                  <PhotoFailureNote
                    failure={failed}
                    onRefresh={() => void refresh()}
                  />
                )}
              </li>
            );
          })}
        </ul>
      )}
      <AddPhoto uploader={uploader} kind="logo" />
    </div>
  );
}

/** The logo itself, whole (not cropped), from its released copy. */
function LogoPicture({
  projectId,
  photoId,
  alt,
}: {
  projectId: string;
  photoId: string;
  alt: string;
}) {
  const { t } = useTranslation('ideas');
  const { blobUrl, failed } = useIdeasPhotoUrl(projectId, photoId);
  return (
    <div className="flex h-24 w-full max-w-60 items-center justify-center overflow-hidden rounded-lg border border-gray-200 bg-white p-2 dark:border-gray-700">
      {blobUrl ? (
        // A private blob URL; next/image cannot load it.
        // eslint-disable-next-line @next/next/no-img-element
        <img src={blobUrl} alt={alt} className="h-full w-full object-contain" />
      ) : (
        <span className="text-center text-xs text-gray-500 dark:text-gray-400">
          {failed ? t('photo-image-unavailable') : t('photo-image-loading')}
        </span>
      )}
    </div>
  );
}
