import { useEffect, useState } from 'react';
import { ImageSelectionCanvas } from './ImageSelectionCanvas';
import { useTranslation } from 'next-i18next';
import { useMarketingImageSource } from './useMarketingImageSource';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import { errorCodeMessage } from '@/lib/i18n/domain-errors';

type Edit = components['schemas']['CreateImageEdit'];
export type ImageEditProgress = {
  id: string;
  operation_id: string;
  source_asset_version_id: string;
  result_asset_version_id: string | null;
  state: 'queued' | 'succeeded' | 'unchanged' | 'failed';
  error_code: string | null;
};
type Progress = ImageEditProgress;
const INPUT =
  'w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm';

export function MarketingImageEdit({
  assetVersionId,
  busy,
  edits,
  onCreate,
  selected = 0,
}: {
  assetVersionId: string;
  busy: boolean;
  edits: Progress[];
  onCreate: (body: Edit) => void;
  selected?: number;
}) {
  const { t } = useTranslation('common');
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (selected) setOpen(true);
  }, [selected]);
  const [painting, setPainting] = useState(false);
  const [cropEnabled, setCropEnabled] = useState(false);
  const [crop, setCrop] = useState({
    x: '0',
    y: '0',
    width: '100',
    height: '100',
  });
  const [rotation, setRotation] = useState<Edit['rotation']>(0);
  const values = Object.fromEntries(
    Object.entries(crop).map(([k, v]) => [k, Number(v)])
  );
  const valid =
    Object.values(crop).every((v) => v.trim() !== '') &&
    Object.values(values).every(Number.isFinite) &&
    values.x >= 0 &&
    values.y >= 0 &&
    values.width > 0 &&
    values.height > 0 &&
    values.x + values.width <= 100 &&
    values.y + values.height <= 100;
  const { imageUrl, imageFailed, preview, onImageError } =
    useMarketingImageSource(assetVersionId, open);
  return (
    <details
      open={open}
      onToggle={(e) => setOpen(e.currentTarget.open)}
      className="space-y-3 border-l border-gray-200 pl-4"
    >
      <summary className="cursor-pointer text-sm font-medium">
        {t('marketing-image-edit-title')}
      </summary>
      <p className="text-sm text-gray-600">{t('marketing-image-edit-note')}</p>
      {imageUrl && (
        <ImageSelectionCanvas
          src={imageUrl}
          mode={cropEnabled ? 'rectangle' : 'none'}
          region={{
            x: values.x / 100,
            y: values.y / 100,
            width: values.width / 100,
            height: values.height / 100,
          }}
          disabled={busy}
          onPainting={setPainting}
          onError={onImageError}
          onRegion={(r) =>
            setCrop({
              x: String(r.x * 100),
              y: String(r.y * 100),
              width: String(r.width * 100),
              height: String(r.height * 100),
            })
          }
        />
      )}
      {(preview.error || imageFailed) && (
        <button
          type="button"
          onClick={() => void preview.mutate().catch(() => undefined)}
          className="text-sm underline"
        >
          {t('marketing-image-edit-reload')}
        </button>
      )}
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={cropEnabled}
          onChange={(e) => setCropEnabled(e.target.checked)}
        />
        {t('marketing-image-edit-crop')}
      </label>
      {cropEnabled && (
        <div className="grid grid-cols-2 gap-3">
          {(['x', 'y', 'width', 'height'] as const).map((key) => (
            <label className="space-y-1 text-sm" key={key}>
              <span>{t(`marketing-image-edit-${key}`)}</span>
              <input
                className={INPUT}
                type="number"
                min={key === 'width' || key === 'height' ? 1 : 0}
                max={100}
                step="1"
                value={crop[key]}
                onChange={(e) => setCrop({ ...crop, [key]: e.target.value })}
              />
            </label>
          ))}
        </div>
      )}
      <label className="block space-y-1 text-sm">
        <span>{t('marketing-image-edit-rotation')}</span>
        <select
          className={INPUT}
          value={rotation}
          onChange={(e) =>
            setRotation(Number(e.target.value) as Edit['rotation'])
          }
        >
          {[0, 90, 180, 270].map((value) => (
            <option key={value} value={value}>
              {t('marketing-image-edit-degrees', { value })}
            </option>
          ))}
        </select>
      </label>
      {cropEnabled && !valid && (
        <p role="alert" className="text-sm">
          {t('marketing-image-edit-invalid')}
        </p>
      )}
      <button
        type="button"
        className="rounded-lg border border-gray-300 px-4 py-2 text-sm disabled:opacity-40"
        disabled={
          busy ||
          painting ||
          (cropEnabled && !valid) ||
          (!cropEnabled && !rotation) ||
          edits.some((e) => e.state === 'queued')
        }
        onClick={() =>
          onCreate({
            asset_version_id: assetVersionId,
            rotation,
            crop: cropEnabled
              ? {
                  x: values.x / 100,
                  y: values.y / 100,
                  width: values.width / 100,
                  height: values.height / 100,
                }
              : null,
          })
        }
      >
        {t('marketing-image-edit-create')}
      </button>
      {edits.map((edit) => (
        <p key={edit.id} role="status" className="text-sm">
          {t(`marketing-image-edit-state-${edit.state}`)}
          {edit.state === 'failed' && edit.error_code && (
            <span className="block">
              {errorCodeMessage(t, edit.error_code)}
            </span>
          )}
        </p>
      ))}
    </details>
  );
}
