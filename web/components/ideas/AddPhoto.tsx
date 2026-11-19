import { useEffect, useId, useRef, useState } from 'react';
import { useTranslation } from 'next-i18next';
import { useRouter } from 'next/router';
import type { UseIdeasPhotoUploadResult } from '@/hooks/ideas/useIdeasPhotoUpload';
import { errorMessage } from '@/lib/i18n/domain-errors';
import type { PhotoRole } from '@/lib/ideas/photos';

/**
 * Add the user's own product photo (#233, design §5.2: "加一张产品照片，图里的
 * 产品会更像"). The user says what it shows and picks the file; the page
 * uploads it through the reference upload gate and adds it to the product,
 * where it is listed with the other photos as not checked yet. A rejected
 * image says why, in the gate's own words.
 *
 * The ideas page links here (`#add-photo`) when some ads were made without a
 * product photo; arriving that way brings this part into view.
 */

export const ADD_PHOTO_ANCHOR = 'add-photo';

const ROLES: PhotoRole[] = ['main', 'angle', 'detail'];
const ACCEPT = 'image/png,image/jpeg,image/webp';

export function AddPhoto({
  uploader,
  hasMain,
}: {
  /**
   * The upload, held by the photo area itself: the first photo turns the
   * list of published photos into the list of candidates, and what this
   * part says about the upload must survive that.
   */
  uploader: UseIdeasPhotoUploadResult;
  /** The product already has a usable photo of the whole product. */
  hasMain: boolean;
}) {
  const { t } = useTranslation('ideas');
  const { state, upload, reset } = uploader;
  const [role, setRole] = useState<PhotoRole>(hasMain ? 'detail' : 'main');
  const title = useId();
  const roleName = useId();
  const input = useRef<HTMLInputElement>(null);
  const area = useRef<HTMLDivElement>(null);
  const router = useRouter();
  const working = ['uploading', 'checking', 'adding'].includes(state.phase);

  // Arriving from the ideas page's link brings this part into view. A link
  // inside the app lands before the route change has finished settling the
  // page, so it is tried once that has finished too.
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const bring = () => {
      if (window.location.hash === `#${ADD_PHOTO_ANCHOR}`)
        area.current?.scrollIntoView?.({ block: 'center' });
    };
    const timer = window.setTimeout(bring, 0);
    router.events?.on('routeChangeComplete', bring);
    return () => {
      window.clearTimeout(timer);
      router.events?.off('routeChangeComplete', bring);
    };
  }, [router.events]);

  function chosen(files: FileList | null) {
    const file = files?.[0];
    if (input.current) input.current.value = '';
    if (!file) return;
    void upload(file, role);
  }

  const status =
    state.phase === 'uploading'
      ? t('profile-photo-add-uploading', { file: state.fileName })
      : state.phase === 'checking'
        ? t('profile-photo-add-checking')
        : state.phase === 'adding'
          ? t('profile-photo-add-adding')
          : state.phase === 'added'
            ? t('profile-photo-add-added')
            : '';

  return (
    <div
      ref={area}
      id={ADD_PHOTO_ANCHOR}
      role="group"
      aria-labelledby={title}
      className="space-y-3 rounded-xl border border-dashed border-gray-300 p-4 dark:border-gray-600"
      data-testid="profile-photo-add"
    >
      <div className="space-y-1">
        <h3 id={title} className="text-base font-semibold">
          {t('profile-photo-add-title')}
        </h3>
        <p className="text-sm text-gray-600 dark:text-gray-400">
          {t('profile-photo-add-intro')}
        </p>
      </div>
      <fieldset className="space-y-2" disabled={working}>
        <legend id={roleName} className="text-sm font-medium">
          {t('profile-photo-add-role')}
        </legend>
        <div className="flex flex-wrap gap-x-4 gap-y-2">
          {ROLES.map((value) => (
            <label
              key={value}
              className="flex min-h-11 items-center gap-2 text-sm"
            >
              <input
                type="radio"
                name={roleName}
                className="radio radio-sm"
                value={value}
                checked={role === value}
                onChange={() => setRole(value)}
              />
              {t(`profile-photo-add-role-${value}`)}
            </label>
          ))}
        </div>
      </fieldset>
      {state.phase === 'rejected' || state.phase === 'failed' ? (
        <div role="alert" className="space-y-2 text-sm">
          <p className="text-red-800 dark:text-red-300">
            {state.phase === 'rejected'
              ? !state.failureCode ||
                state.failureCode === 'reference_gate_rejected'
                ? t('profile-photo-add-rejected-gate')
                : t('profile-photo-add-rejected', {
                    reason: errorMessage(t, { code: state.failureCode }),
                  })
              : state.trouble
                ? t(`profile-photo-add-${state.trouble}`)
                : t('profile-photo-add-failed', {
                    reason: errorMessage(t, state.problem),
                  })}
          </p>
          <button
            type="button"
            className="btn btn-sm min-h-11"
            onClick={() => {
              reset();
              input.current?.click();
            }}
          >
            {t('profile-photo-add-another')}
          </button>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-3">
          <label
            className={`btn btn-sm min-h-11 ${working ? 'btn-disabled' : 'btn-primary'}`}
            aria-disabled={working}
          >
            {t('profile-photo-add-choose')}
            <input
              ref={input}
              type="file"
              accept={ACCEPT}
              className="sr-only"
              disabled={working}
              data-testid="profile-photo-add-file"
              onChange={(event) => chosen(event.target.files)}
            />
          </label>
          <p aria-live="polite" className="min-w-0 flex-1 basis-48 text-sm">
            {status}
          </p>
        </div>
      )}
    </div>
  );
}
