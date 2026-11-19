import { useId, useRef, useState } from 'react';
import { useTranslation } from 'next-i18next';
import { useArrivalScroll } from '@/hooks/ideas/useArrivalScroll';
import type { UseIdeasPhotoUploadResult } from '@/hooks/ideas/useIdeasPhotoUpload';
import { errorMessage } from '@/lib/i18n/domain-errors';
import type { PhotoRole, UploadRole } from '@/lib/ideas/photos';

/**
 * Add the user's own product photo (#233, design §5.2: "加一张产品照片，图里的
 * 产品会更像"). The user says what it shows and picks the file; the page
 * uploads it through the reference upload gate and adds it to the product,
 * where it is listed with the other photos as not checked yet. A rejected
 * image says why, in the gate's own words.
 *
 * The ideas page links here (`#add-photo`) when some ads were made without a
 * product photo; arriving that way brings this part into view.
 *
 * `kind = "logo"` adds the brand's logo instead (#233): no choice of what it
 * shows, its own words, and it is listed with the brand, never as a photo.
 */

export const ADD_PHOTO_ANCHOR = 'add-photo';

const ROLES: PhotoRole[] = ['main', 'angle', 'detail'];
const ACCEPT = 'image/png,image/jpeg,image/webp';

export function AddPhoto({
  uploader,
  hasMain = false,
  kind = 'photo',
}: {
  /**
   * The upload, held by the photo area itself: the first photo turns the
   * list of published photos into the list of candidates, and what this
   * part says about the upload must survive that.
   */
  uploader: UseIdeasPhotoUploadResult;
  /** The product already has a usable photo of the whole product. */
  hasMain?: boolean;
  kind?: 'photo' | 'logo';
}) {
  const { t } = useTranslation('ideas');
  const { state, upload, reset } = uploader;
  const logo = kind === 'logo';
  const [role, setRole] = useState<PhotoRole>(hasMain ? 'detail' : 'main');

  const copy = logo ? 'profile-logo-add' : 'profile-photo-add';
  const title = useId();
  const roleName = useId();
  const input = useRef<HTMLInputElement>(null);
  // Arriving from the ideas page's link brings this part into view.
  const area = useArrivalScroll<HTMLDivElement>(ADD_PHOTO_ANCHOR, !logo);
  const working = ['uploading', 'checking', 'adding'].includes(state.phase);

  function chosen(files: FileList | null) {
    const file = files?.[0];
    if (input.current) input.current.value = '';
    if (!file) return;
    const as: UploadRole = logo ? 'logo' : role;
    void upload(file, as);
  }

  const status =
    state.phase === 'uploading'
      ? t('profile-photo-add-uploading', { file: state.fileName })
      : state.phase === 'checking'
        ? t('profile-photo-add-checking')
        : state.phase === 'adding'
          ? t('profile-photo-add-adding')
          : state.phase === 'added'
            ? t(`${copy}-added`)
            : '';

  return (
    <div
      ref={area}
      id={logo ? undefined : ADD_PHOTO_ANCHOR}
      role="group"
      aria-labelledby={title}
      className="space-y-3 rounded-xl border border-dashed border-gray-300 p-4 dark:border-gray-600"
      data-testid={`${copy}`}
    >
      <div className="space-y-1">
        <h3 id={title} className="text-base font-semibold">
          {t(`${copy}-title`)}
        </h3>
        <p className="text-sm text-gray-600 dark:text-gray-400">
          {t(`${copy}-intro`)}
        </p>
      </div>
      {!logo && (
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
      )}
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
            {t(`${copy}-choose`)}
            <input
              ref={input}
              type="file"
              accept={ACCEPT}
              className="sr-only"
              disabled={working}
              data-testid={`${copy}-file`}
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
