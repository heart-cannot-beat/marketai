import { useTranslation } from 'next-i18next';
import { useAssetImageUrl } from '@/hooks/assets/useAssetImageUrl';
import {
  exportBlockReasons,
  exportBlockStatus,
  type ExportRefusal,
} from '@/lib/assets/export-block';

/** How many refused ads are drawn; each one loads its image. */
const SHOWN = 6;

interface ExportRefusedListProps {
  refusal: ExportRefusal;
  /** What was refused, in one sentence: the package, or its download. */
  heading: string;
}

/** One refused ad's own image: the page may always show it (`purpose=display`). */
function RefusedImage({ versionId }: { versionId: string }) {
  const { t } = useTranslation('assets');
  const { blobUrl, isLoading, problem } = useAssetImageUrl(versionId);
  return (
    <div className="relative h-12 w-12 shrink-0 overflow-hidden rounded bg-base-200">
      {blobUrl && (
        <img
          src={blobUrl}
          alt={t('export-refused-image')}
          className="h-full w-full object-cover"
        />
      )}
      {isLoading && (
        <span className="loading loading-spinner loading-xs absolute inset-0 m-auto" />
      )}
      {problem && <span className="sr-only">{t('image-unavailable')}</span>}
    </div>
  );
}

/**
 * The ads an export refused (`ads_not_exportable`, Issue #219): each as its
 * own image with what it is and why it may not leave, so nobody has to
 * match an id against the gallery.
 */
export function ExportRefusedList({
  refusal,
  heading,
}: ExportRefusedListProps) {
  const { t } = useTranslation('assets');
  const shown = refusal.ads.slice(0, SHOWN);
  const more = refusal.count - shown.length;

  return (
    <div
      role="alert"
      className="rounded-lg border border-warning bg-warning/10 p-3 text-sm flex flex-col gap-3"
      data-testid="export-refused"
    >
      <p className="font-medium">{heading}</p>
      <ul className="flex flex-col gap-2">
        {shown.map((ad) => (
          <li
            key={ad.asset_version_id}
            className="flex items-start gap-3"
            data-testid="export-refused-ad"
            data-status={ad.status}
          >
            <RefusedImage versionId={ad.asset_version_id} />
            <div>
              <p className="font-medium">{exportBlockStatus(t, ad)}</p>
              <p className="text-base-content/70">
                {exportBlockReasons(t, ad).join(
                  t('export-block-reasons-separator')
                )}
              </p>
            </div>
          </li>
        ))}
      </ul>
      {more > 0 && (
        <p className="text-base-content/70">
          {t('export-refused-more', { count: more })}
        </p>
      )}
      <p>{t('export-refused-hint')}</p>
    </div>
  );
}
