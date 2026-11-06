import { useId, useState } from 'react';
import { useTranslation } from 'next-i18next';
import { formatDateTime } from '@/lib/i18n/format';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import { useMarketingImageSource } from './useMarketingImageSource';
import { CHECKERBOARD } from './ImageSelectionCanvas';
import { TechnicalDetails } from './MarketingTechnicalInfo';

type Asset = components['schemas']['WorkflowAsset'];
const INPUT =
  'w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm';

function orderVersions(assets: Asset[]) {
  // Scope is the server-authorized round. Missing ancestors remain identified
  // as external to this round; never fetch another version by guessing its ID.
  const byId = new Map(assets.map((a) => [a.asset_version_id, a]));
  const sorted = [...assets].sort(
    (a, b) =>
      (a.created_at || '').localeCompare(b.created_at || '') ||
      a.asset_version_id.localeCompare(b.asset_version_id)
  );
  const result: { asset: Asset; depth: number }[] = [];
  const visited = new Set<string>();
  const children = new Map<string, Asset[]>();
  for (const asset of sorted) {
    if (asset.parent_version_id) {
      const siblings = children.get(asset.parent_version_id) || [];
      siblings.push(asset);
      children.set(asset.parent_version_id, siblings);
    }
  }
  function visit(asset: Asset, depth: number) {
    const stack = [{ asset, depth }];
    while (stack.length) {
      const entry = stack.pop()!;
      if (visited.has(entry.asset.asset_version_id)) continue;
      visited.add(entry.asset.asset_version_id);
      result.push(entry);
      for (const child of [
        ...(children.get(entry.asset.asset_version_id) || []),
      ].reverse()) {
        stack.push({ asset: child, depth: entry.depth + 1 });
      }
    }
  }
  sorted
    .filter((a) => !a.parent_version_id || !byId.has(a.parent_version_id))
    .forEach((a) => visit(a, 0));
  sorted.forEach((a) => visit(a, 0));
  return result;
}

/** A version is recognised by its picture and number, never by its id (J-32). */
function VersionThumbnail({
  assetVersionId,
  alt,
  open,
}: {
  assetVersionId: string;
  alt: string;
  open: boolean;
}) {
  const image = useMarketingImageSource(assetVersionId, open);
  return (
    <div className="h-12 w-12 shrink-0 overflow-hidden rounded border border-gray-200 bg-gray-50">
      {image.imageUrl && (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={image.imageUrl}
          alt={alt}
          className="h-full w-full object-cover"
          onError={image.onImageError}
        />
      )}
    </div>
  );
}

export function MarketingVersionHistory({
  assets,
  busy,
  editable,
  onEdit,
}: {
  assets: Asset[];
  busy: boolean;
  editable: boolean;
  onEdit: (id: string) => void;
}) {
  const { t, i18n } = useTranslation('common');
  const [open, setOpen] = useState(false);
  const [beforeId, setBeforeId] = useState('');
  const [afterId, setAfterId] = useState('');
  const ordered = orderVersions(assets);
  const latest = [...assets]
    .sort(
      (a, b) =>
        (a.created_at || '').localeCompare(b.created_at || '') ||
        a.asset_version_id.localeCompare(b.asset_version_id)
    )
    .at(-1);
  const after = assets.find((a) => a.asset_version_id === afterId) || latest;
  const before =
    assets.find((a) => a.asset_version_id === beforeId) ||
    assets.find((a) => a.asset_version_id === after?.parent_version_id) ||
    after;
  const labels = new Map(
    ordered.map(({ asset }, i) => [
      asset.asset_version_id,
      t('image-history-version', { number: i + 1 }),
    ])
  );
  const source = useMarketingImageSource(
    before?.asset_version_id || '',
    open && !!before
  );
  const result = useMarketingImageSource(
    after?.asset_version_id || '',
    open && !!after
  );
  const [split, setSplit] = useState(50);
  const [zoom, setZoom] = useState(1);
  const [frame, setFrame] = useState({ id: '', width: 1, height: 1 });
  const id = useId();
  if (!assets.length) return null;
  return (
    <details
      open={open}
      onToggle={(e) => setOpen(e.currentTarget.open)}
      className="space-y-4 rounded-lg border border-gray-200 p-4"
    >
      <summary className="cursor-pointer text-sm font-medium">
        {t('image-history-title')}
      </summary>
      <p className="text-sm text-gray-600">{t('image-history-note')}</p>
      <ol
        aria-label={t('image-history-title')}
        className="max-h-80 space-y-3 overflow-auto"
      >
        {ordered.map(({ asset, depth }) => (
          <li
            key={asset.asset_version_id}
            style={{ marginLeft: `${Math.min(depth, 4) * 12}px` }}
            className="space-y-1 border-l-2 border-gray-200 pl-3 text-sm"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex min-w-0 items-center gap-2">
                <VersionThumbnail
                  assetVersionId={asset.asset_version_id}
                  alt={labels.get(asset.asset_version_id) ?? ''}
                  open={open}
                />
                <p className="font-medium">
                  {labels.get(asset.asset_version_id)} ·{' '}
                  {t(
                    `image-history-operation-${asset.edit_operation || 'generated'}`
                  )}
                </p>
              </div>
              <span>
                {t(
                  asset.ready
                    ? 'image-history-approved'
                    : asset.quality?.quality === 'approved'
                      ? 'image-history-recheck'
                      : asset.quality?.quality === 'rejected'
                        ? 'image-history-rejected'
                        : asset.quality?.quality === 'needs_changes'
                          ? 'image-history-needs-changes'
                          : 'image-history-pending'
                )}
              </span>
            </div>
            <p>
              {asset.parent_version_id
                ? labels.get(asset.parent_version_id)
                  ? t('image-history-source', {
                      version: labels.get(asset.parent_version_id),
                    })
                  : t('image-history-source-outside')
                : t('image-history-original')}
            </p>
            {asset.created_at && (
              <time
                dateTime={asset.created_at}
                className="block text-xs text-gray-500"
              >
                {formatDateTime(i18n.language, asset.created_at)}
              </time>
            )}
            <div className="flex flex-wrap gap-3">
              <button
                type="button"
                className="text-sm underline"
                onClick={() => {
                  setAfterId(asset.asset_version_id);
                  setBeforeId(
                    asset.parent_version_id &&
                      assets.some(
                        (a) => a.asset_version_id === asset.parent_version_id
                      )
                      ? asset.parent_version_id
                      : asset.asset_version_id
                  );
                  setZoom(1);
                }}
              >
                {t('image-history-compare')}
              </button>
              {editable && (
                <button
                  type="button"
                  className="text-sm underline disabled:opacity-40"
                  disabled={busy}
                  onClick={() => onEdit(asset.asset_version_id)}
                >
                  {t('image-history-continue')}
                </button>
              )}
            </div>
            <TechnicalDetails
              className="text-xs"
              facts={[
                [t('marketing-board-version'), asset.asset_version_id],
                [t('marketing-board-parent-version'), asset.parent_version_id],
              ]}
            />
          </li>
        ))}
      </ol>
      <div className="grid gap-3 sm:grid-cols-2">
        <label htmlFor={id + '-before'} className="space-y-1 text-sm">
          <span>{t('image-compare-before')}</span>
          <select
            id={id + '-before'}
            className={INPUT}
            value={before?.asset_version_id || ''}
            onChange={(e) => setBeforeId(e.target.value)}
          >
            {ordered.map(({ asset }) => (
              <option
                key={asset.asset_version_id}
                value={asset.asset_version_id}
              >
                {labels.get(asset.asset_version_id)} ·{' '}
                {t(
                  `image-history-operation-${asset.edit_operation || 'generated'}`
                )}
              </option>
            ))}
          </select>
        </label>
        <label htmlFor={id + '-after'} className="space-y-1 text-sm">
          <span>{t('image-compare-after')}</span>
          <select
            id={id + '-after'}
            className={INPUT}
            value={after?.asset_version_id || ''}
            onChange={(e) => {
              setAfterId(e.target.value);
              setZoom(1);
            }}
          >
            {ordered.map(({ asset }) => (
              <option
                key={asset.asset_version_id}
                value={asset.asset_version_id}
              >
                {labels.get(asset.asset_version_id)} ·{' '}
                {t(
                  `image-history-operation-${asset.edit_operation || 'generated'}`
                )}
              </option>
            ))}
          </select>
        </label>
      </div>
      <p className="text-xs text-gray-600">{t('image-compare-fit-note')}</p>
      {source.preview.error ||
      result.preview.error ||
      source.imageFailed ||
      result.imageFailed ? (
        <button
          type="button"
          className="text-sm underline"
          onClick={() => {
            void source.preview.mutate().catch(() => undefined);
            void result.preview.mutate().catch(() => undefined);
          }}
        >
          {t('marketing-image-edit-reload')}
        </button>
      ) : (
        (!source.imageUrl || !result.imageUrl) && (
          <p role="status">{t('image-compare-loading')}</p>
        )
      )}
      {source.imageUrl && result.imageUrl && (
        <>
          <div
            role="region"
            tabIndex={0}
            aria-label={t('image-compare-view')}
            className="max-h-[70vh] w-full overflow-auto rounded border border-gray-200"
            style={CHECKERBOARD}
          >
            <div
              className="relative"
              style={{
                width: `${zoom * 100}%`,
                aspectRatio:
                  frame.id === after?.asset_version_id
                    ? `${frame.width} / ${frame.height}`
                    : '1 / 1',
              }}
            >
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={result.imageUrl}
                alt={t('image-compare-result')}
                className="absolute inset-0 h-full w-full object-contain"
                draggable={false}
                onError={result.onImageError}
                onLoad={(e) =>
                  setFrame({
                    id: after?.asset_version_id || '',
                    width: e.currentTarget.naturalWidth,
                    height: e.currentTarget.naturalHeight,
                  })
                }
              />
              <div
                className="absolute inset-0"
                style={{
                  ...CHECKERBOARD,
                  clipPath: `inset(0 ${100 - split}% 0 0)`,
                }}
              >
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={source.imageUrl}
                  alt={t('image-compare-source')}
                  className="h-full w-full object-contain"
                  draggable={false}
                  onError={source.onImageError}
                />
              </div>
              <div
                aria-hidden="true"
                className="pointer-events-none absolute bottom-0 top-0 w-0 border-l-2 border-gray-900"
                style={{ left: `${split}%` }}
              />
            </div>
          </div>
          <label htmlFor={id + '-split'} className="block text-sm">
            <span>{t('image-compare-slider', { value: split })}</span>
            <input
              id={id + '-split'}
              className="block w-full"
              type="range"
              min={0}
              max={100}
              value={split}
              onChange={(e) => setSplit(Number(e.target.value))}
            />
          </label>
          <label htmlFor={id + '-zoom'} className="block text-sm">
            <span>{t('image-compare-zoom', { value: zoom * 100 })}</span>
            <input
              id={id + '-zoom'}
              className="block w-full"
              type="range"
              min={1}
              max={4}
              step={0.25}
              value={zoom}
              onChange={(e) => setZoom(Number(e.target.value))}
            />
          </label>
        </>
      )}
    </details>
  );
}
