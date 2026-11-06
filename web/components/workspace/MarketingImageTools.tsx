import { useState } from 'react';
import { useTranslation } from 'next-i18next';
import useSWR from 'swr';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { useWorkspace } from './WorkspaceProvider';
import { useMarketingImageSource } from './useMarketingImageSource';
import { ImageSelectionCanvas, type ImageStroke } from './ImageSelectionCanvas';
import type { components } from '@/lib/domain-client/generated/domain-v1';

type Feedback = components['schemas']['PrepareFeedback'];
type Tool = NonNullable<Feedback['image_tool']>;
const INPUT =
  'w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm';

export function MarketingImageTools({
  assetVersionId,
  mode,
  busy,
  onPrepare,
}: {
  assetVersionId: string;
  mode: 'mock' | 'byok';
  busy: boolean;
  onPrepare: (body: Feedback) => void;
}) {
  const { t } = useTranslation('common');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<Tool['kind']>('upscale');
  const [scale, setScale] = useState<2 | 4>(2);
  const [providerId, setProviderId] = useState('');
  const [modelId, setModelId] = useState('');
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [selectionMode, setSelectionMode] = useState<'rectangle' | 'brush'>(
    'rectangle'
  );
  const [strokes, setStrokes] = useState<ImageStroke[]>([]);
  const [redo, setRedo] = useState<ImageStroke[]>([]);
  const [brushWidth, setBrushWidth] = useState(5);
  const [painting, setPainting] = useState(false);
  const brushLimit =
    strokes.length >= 128 ||
    strokes.reduce((sum, s) => sum + s.points.length, 0) >= 7680;
  const [region, setRegion] = useState({
    x: '0',
    y: '0',
    width: '25',
    height: '25',
  });
  const { imageUrl, imageFailed, preview, onImageError } =
    useMarketingImageSource(assetVersionId, open);
  const caps = useSWR(
    open ? ['marketing-generation-capabilities', current.workspaceId] : null,
    async () => {
      const result = await gateway.getGenerationCapabilities({
        workspaceSlug: current.slug,
      });
      if (!('data' in result) || !result.data)
        throw new Error('marketing-action-failed');
      return result.data;
    }
  );
  const options = (caps.data?.provider_options || [])
    .map((p) => ({
      ...p,
      models: p.models.filter(
        (m) =>
          m.available &&
          m.image_sizes.includes('2K') &&
          m.image_tools?.includes(kind)
      ),
    }))
    .filter((p) => p.available && p.execution_mode === mode && p.models.length);
  const provider = options.find((p) => p.id === providerId) || options[0];
  const model =
    provider?.models.find((m) => m.id === modelId) ||
    provider?.models.find((m) => m.id === provider.default_model) ||
    provider?.models[0];
  const numbers = Object.fromEntries(
    Object.entries(region).map(([k, v]) => [k, Number(v)])
  );
  const validRegion =
    Object.values(region).every((v) => v.trim() !== '') &&
    Object.values(numbers).every(Number.isFinite) &&
    numbers.x >= 0 &&
    numbers.y >= 0 &&
    numbers.width > 0 &&
    numbers.height > 0 &&
    numbers.x + numbers.width <= 100 &&
    numbers.y + numbers.height <= 100 &&
    Math.round(((numbers.x + numbers.width) * size.width) / 100) >
      Math.round((numbers.x * size.width) / 100) &&
    Math.round(((numbers.y + numbers.height) * size.height) / 100) >
      Math.round((numbers.y * size.height) / 100);
  const factor = kind === 'upscale' ? scale : 1;
  const validSelection =
    selectionMode === 'brush' ? strokes.length > 0 : validRegion;
  const validSize =
    size.width > 0 &&
    size.height > 0 &&
    size.width * size.height * factor * factor <= 25_000_000 &&
    Math.max(size.width, size.height) * factor <= 8192;
  return (
    <details
      open={open}
      onToggle={(e) => setOpen(e.currentTarget.open)}
      className="space-y-3 border-l border-gray-200 pl-4"
    >
      <summary className="cursor-pointer text-sm font-medium">
        {t('marketing-image-tools-title')}
      </summary>
      <p className="text-sm text-gray-600">{t('marketing-image-tools-note')}</p>
      <label className="block space-y-1 text-sm">
        <span>{t('marketing-image-tools-operation')}</span>
        <select
          className={INPUT}
          value={kind}
          onChange={(e) => setKind(e.target.value as Tool['kind'])}
        >
          {(['upscale', 'remove_background', 'erase'] as const).map((k) => (
            <option key={k} value={k}>
              {t(`marketing-image-tools-${k}`)}
            </option>
          ))}
        </select>
      </label>
      {imageUrl && (
        <ImageSelectionCanvas
          key={kind + selectionMode}
          src={imageUrl}
          mode={kind === 'erase' ? selectionMode : 'none'}
          region={{
            x: numbers.x / 100,
            y: numbers.y / 100,
            width: numbers.width / 100,
            height: numbers.height / 100,
          }}
          onRegion={(r) =>
            setRegion({
              x: String(r.x * 100),
              y: String(r.y * 100),
              width: String(r.width * 100),
              height: String(r.height * 100),
            })
          }
          onLoad={setSize}
          onError={onImageError}
          disabled={busy || (selectionMode === 'brush' && brushLimit)}
          strokes={strokes}
          brushWidth={brushWidth / 100}
          onPainting={setPainting}
          onStroke={(stroke) => {
            setStrokes([...strokes, stroke]);
            setRedo([]);
          }}
        />
      )}
      {(preview.error || imageFailed) && (
        <button
          type="button"
          className="text-sm underline"
          onClick={() => void preview.mutate().catch(() => undefined)}
        >
          {t('marketing-image-edit-reload')}
        </button>
      )}
      {kind === 'upscale' && (
        <>
          <p className="text-sm text-gray-600">
            {t('marketing-image-tools-upscale-note')}
          </p>
          <label className="block space-y-1 text-sm">
            <span>{t('marketing-image-tools-scale')}</span>
            <select
              className={INPUT}
              value={scale}
              onChange={(e) => setScale(Number(e.target.value) as 2 | 4)}
            >
              <option value={2}>
                {t('marketing-image-tools-scale-option', { value: 2 })}
              </option>
              <option value={4}>
                {t('marketing-image-tools-scale-option', { value: 4 })}
              </option>
            </select>
          </label>
        </>
      )}
      {kind === 'erase' && (
        <>
          <p className="text-sm text-gray-600">
            {t(
              selectionMode === 'brush'
                ? 'image-canvas-erase-note'
                : 'marketing-image-tools-erase-note'
            )}
          </p>
          <label className="block space-y-1 text-sm">
            <span>{t('image-canvas-selection-mode')}</span>
            <select
              className={INPUT}
              value={selectionMode}
              disabled={painting}
              onChange={(e) =>
                setSelectionMode(e.target.value as 'rectangle' | 'brush')
              }
            >
              <option value="rectangle">{t('image-canvas-rectangle')}</option>
              <option value="brush">{t('image-canvas-brush')}</option>
            </select>
          </label>
          {selectionMode === 'brush' ? (
            <div className="space-y-2">
              <label className="block text-sm">
                <span>
                  {t('image-canvas-brush-size', { size: brushWidth })}
                </span>
                <input
                  className="block w-full"
                  type="range"
                  min={1}
                  max={30}
                  value={brushWidth}
                  disabled={painting}
                  onChange={(e) => setBrushWidth(Number(e.target.value))}
                />
              </label>
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  className="rounded border px-3 py-1 text-sm disabled:opacity-40"
                  disabled={painting || !strokes.length}
                  onClick={() => {
                    setRedo([...redo, strokes[strokes.length - 1]]);
                    setStrokes(strokes.slice(0, -1));
                  }}
                >
                  {t('image-canvas-undo')}
                </button>
                <button
                  type="button"
                  className="rounded border px-3 py-1 text-sm disabled:opacity-40"
                  disabled={painting || !redo.length}
                  onClick={() => {
                    setStrokes([...strokes, redo[redo.length - 1]]);
                    setRedo(redo.slice(0, -1));
                  }}
                >
                  {t('image-canvas-redo')}
                </button>
                <button
                  type="button"
                  className="rounded border px-3 py-1 text-sm disabled:opacity-40"
                  disabled={painting || !strokes.length}
                  onClick={() => {
                    setStrokes([]);
                    setRedo([]);
                  }}
                >
                  {t('image-canvas-clear')}
                </button>
              </div>
              <p className="text-xs text-gray-600" role="status">
                {t(
                  brushLimit
                    ? 'image-canvas-limit'
                    : 'image-canvas-stroke-count',
                  { count: strokes.length }
                )}
              </p>
            </div>
          ) : (
            <div className="grid grid-cols-2 gap-3">
              {(['x', 'y', 'width', 'height'] as const).map((k) => (
                <label className="space-y-1 text-sm" key={k}>
                  <span>{t(`marketing-image-edit-${k}`)}</span>
                  <input
                    className={INPUT}
                    type="number"
                    min={0}
                    max={100}
                    step="1"
                    value={region[k]}
                    onChange={(e) =>
                      setRegion({ ...region, [k]: e.target.value })
                    }
                  />
                </label>
              ))}
            </div>
          )}
          {selectionMode === 'rectangle' && !validRegion && (
            <p role="alert">{t('marketing-image-edit-invalid')}</p>
          )}
        </>
      )}
      <p className="text-sm text-gray-600">
        {t('marketing-image-tools-review-note')}
      </p>
      <label className="block space-y-1 text-sm">
        <span>{t('marketing-feedback-provider')}</span>
        <select
          className={INPUT}
          value={provider?.id || ''}
          onChange={(e) => {
            setProviderId(e.target.value);
            setModelId('');
          }}
        >
          {options.map((p) => (
            <option key={p.id} value={p.id}>
              {p.label}
            </option>
          ))}
        </select>
      </label>
      <label className="block space-y-1 text-sm">
        <span>{t('generation-model')}</span>
        <select
          className={INPUT}
          value={model?.id || ''}
          onChange={(e) => setModelId(e.target.value)}
        >
          {provider?.models.map((m) => (
            <option key={m.id} value={m.id}>
              {m.id}
            </option>
          ))}
        </select>
      </label>
      {!provider && caps.data && (
        <p role="note">{t('marketing-image-tools-unavailable')}</p>
      )}
      {caps.error && <p role="alert">{t('marketing-action-failed')}</p>}
      {size.width > 0 && !validSize && (
        <p role="alert">{t('marketing-image-tools-size-limit')}</p>
      )}
      {validSize && (
        <p className="text-sm">
          {t('marketing-image-tools-output', {
            width: size.width * factor,
            height: size.height * factor,
          })}
        </p>
      )}
      <button
        type="button"
        className="rounded-lg border border-gray-300 px-4 py-2 text-sm disabled:opacity-40"
        disabled={
          busy ||
          painting ||
          !imageUrl ||
          imageFailed ||
          !!preview.error ||
          !provider ||
          !model ||
          !validSize ||
          (kind === 'erase' && !validSelection)
        }
        onClick={() => {
          if (!provider || !model) return;
          onPrepare({
            asset_version_id: assetVersionId,
            note: '',
            provider: provider.id as Feedback['provider'],
            model: model.id,
            image_tool: {
              algorithm: 'supplier-image-tools-v1',
              kind,
              source_width: size.width,
              source_height: size.height,
              ...(kind === 'upscale' ? { scale } : {}),
              ...(kind === 'erase'
                ? {
                    ...(selectionMode === 'brush'
                      ? { strokes }
                      : {
                          region: {
                            x: numbers.x / 100,
                            y: numbers.y / 100,
                            width: numbers.width / 100,
                            height: numbers.height / 100,
                          },
                        }),
                  }
                : {}),
            },
          });
        }}
      >
        {t('marketing-image-tools-prepare')}
      </button>
    </details>
  );
}
