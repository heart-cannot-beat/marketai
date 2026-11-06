import { useRef, useState } from 'react';
import type { PointerEvent } from 'react';
import { useTranslation } from 'next-i18next';
import type { components } from '@/lib/domain-client/generated/domain-v1';

export type ImageRegion = {
  x: number;
  y: number;
  width: number;
  height: number;
};
export type ImageStroke = NonNullable<
  components['schemas']['ImageToolSpec']['strokes']
>[number];
type Point = { x: number; y: number };
type Gesture = {
  id: number;
  start: Point;
  original: ImageRegion;
  handle: string;
  stroke: ImageStroke;
};
export const CHECKERBOARD = {
  backgroundColor: '#fff',
  backgroundImage:
    'conic-gradient(#e5e7eb 25%, transparent 0 50%, #e5e7eb 0 75%, transparent 0)',
  backgroundSize: '20px 20px',
};
const clamp = (n: number, min = 0, max = 1) => Math.max(min, Math.min(max, n));
const rounded = (n: number) => Math.round(n * 1e6) / 1e6;

/** All coordinates refer to the oriented original, never the CSS-sized preview. */
export function ImageSelectionCanvas({
  src,
  mode,
  region,
  onRegion,
  strokes = [],
  onStroke,
  brushWidth = 0.05,
  disabled = false,
  onPainting,
  onLoad,
  onError,
}: {
  src: string;
  mode: 'none' | 'rectangle' | 'brush';
  region: ImageRegion;
  onRegion: (region: ImageRegion) => void;
  strokes?: ImageStroke[];
  onStroke?: (stroke: ImageStroke) => void;
  brushWidth?: number;
  disabled?: boolean;
  onPainting?: (painting: boolean) => void;
  onLoad?: (size: { width: number; height: number }) => void;
  onError?: () => void;
}) {
  const { t } = useTranslation('common');
  const gesture = useRef<Gesture | null>(null);
  const [draft, setDraft] = useState<ImageStroke | null>(null);
  const [size, setSize] = useState({ width: 1, height: 1 });
  const [loaded, setLoaded] = useState(false);
  const valid =
    Object.values(region).every(Number.isFinite) &&
    region.width > 0 &&
    region.height > 0 &&
    region.x >= 0 &&
    region.y >= 0 &&
    region.x + region.width <= 1 &&
    region.y + region.height <= 1;
  function point(e: PointerEvent<HTMLDivElement>): Point {
    const bounds = e.currentTarget.getBoundingClientRect();
    return {
      x: rounded(clamp((e.clientX - bounds.left) / bounds.width)),
      y: rounded(clamp((e.clientY - bounds.top) / bounds.height)),
    };
  }
  function move(e: PointerEvent<HTMLDivElement>) {
    const g = gesture.current;
    if (!g || g.id !== e.pointerId) return;
    const p = point(e);
    if (mode === 'brush') {
      const last = g.stroke.points[g.stroke.points.length - 1];
      if (g.stroke.points.length < 512 && (last.x !== p.x || last.y !== p.y)) {
        g.stroke = { ...g.stroke, points: [...g.stroke.points, p] };
        setDraft(g.stroke);
      }
      return;
    }
    const r = g.original;
    if (g.handle === 'move') {
      onRegion({
        ...r,
        x: rounded(clamp(r.x + p.x - g.start.x, 0, 1 - r.width)),
        y: rounded(clamp(r.y + p.y - g.start.y, 0, 1 - r.height)),
      });
      return;
    }
    const anchor =
      g.handle === 'draw'
        ? g.start
        : {
            x: g.handle.includes('w') ? r.x + r.width : r.x,
            y: g.handle.includes('n') ? r.y + r.height : r.y,
          };
    const width = rounded(
      Math.min(1, Math.max(1 / size.width, Math.abs(p.x - anchor.x)))
    );
    const height = rounded(
      Math.min(1, Math.max(1 / size.height, Math.abs(p.y - anchor.y)))
    );
    onRegion({
      x: rounded(Math.min(Math.min(anchor.x, p.x), 1 - width)),
      y: rounded(Math.min(Math.min(anchor.y, p.y), 1 - height)),
      width,
      height,
    });
  }
  function end(e: PointerEvent<HTMLDivElement>, cancel = false) {
    const g = gesture.current;
    if (!g || g.id !== e.pointerId) return;
    if (!cancel) move(e);
    if (cancel && mode === 'rectangle') onRegion(g.original);
    if (!cancel && mode === 'brush') onStroke?.(g.stroke);
    gesture.current = null;
    setDraft(null);
    onPainting?.(false);
    if (e.currentTarget.hasPointerCapture?.(e.pointerId))
      e.currentTarget.releasePointerCapture(e.pointerId);
  }
  return (
    <div className="max-w-full">
      <div
        role="group"
        aria-label={t(
          mode === 'brush' ? 'image-canvas-brush' : 'image-canvas-selection'
        )}
        className="relative inline-block max-w-full align-top"
        style={{
          ...CHECKERBOARD,
          touchAction: mode === 'none' ? 'auto' : 'none',
          cursor: mode === 'none' ? 'default' : 'crosshair',
        }}
        onPointerDown={(e) => {
          if (
            disabled ||
            !loaded ||
            mode === 'none' ||
            gesture.current ||
            (e.button !== 0 && e.pointerType !== 'touch')
          )
            return;
          e.preventDefault();
          const p = point(e);
          gesture.current = {
            id: e.pointerId,
            start: p,
            original: region,
            handle: (e.target as Element).getAttribute('data-handle') || 'draw',
            stroke: { width: brushWidth, points: [p] },
          };
          e.currentTarget.setPointerCapture?.(e.pointerId);
          onPainting?.(true);
          if (mode === 'brush') setDraft(gesture.current.stroke);
        }}
        onPointerMove={move}
        onPointerUp={(e) => end(e)}
        onPointerCancel={(e) => end(e, true)}
        onLostPointerCapture={(e) => end(e, true)}
      >
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={src}
          alt={t('marketing-image-edit-original')}
          draggable={false}
          className="block max-h-96 max-w-full select-none"
          onError={onError}
          onLoad={(e) => {
            const dimensions = {
              width: e.currentTarget.naturalWidth,
              height: e.currentTarget.naturalHeight,
            };
            setSize(dimensions);
            setLoaded(true);
            onLoad?.(dimensions);
          }}
        />
        {mode === 'rectangle' && valid && (
          <div
            data-handle="move"
            aria-label={t('marketing-image-tools-selection')}
            className="pointer-events-none absolute border-2 border-white bg-red-500/20 outline outline-1 outline-black"
            style={{
              left: `${region.x * 100}%`,
              top: `${region.y * 100}%`,
              width: `${region.width * 100}%`,
              height: `${region.height * 100}%`,
              cursor: 'move',
            }}
          >
            <span
              data-handle="move"
              aria-hidden="true"
              className="pointer-events-auto absolute left-1/2 top-1/2 flex h-8 w-8 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded border border-gray-900 bg-white/90 text-gray-900"
              style={{ cursor: 'move' }}
            >
              <svg
                width="20"
                height="20"
                viewBox="0 0 20 20"
                fill="none"
                stroke="currentColor"
                className="pointer-events-none"
              >
                <path d="M10 2v16M2 10h16M7 5l3-3 3 3M7 15l3 3 3-3M5 7l-3 3 3 3M15 7l3 3-3 3" />
              </svg>
            </span>
            {(['nw', 'ne', 'sw', 'se'] as const).map((handle) => (
              <span
                key={handle}
                data-handle={handle}
                className="pointer-events-auto absolute h-5 w-5 rounded-sm border border-gray-900 bg-white"
                style={{
                  left: handle.includes('w') ? '-8px' : 'calc(100% - 8px)',
                  top: handle.includes('n') ? '-8px' : 'calc(100% - 8px)',
                  cursor: `${handle}-resize`,
                }}
              />
            ))}
          </div>
        )}
        {mode === 'brush' && (
          <svg
            aria-hidden="true"
            className="pointer-events-none absolute inset-0 h-full w-full"
            viewBox={`0 0 ${size.width} ${size.height}`}
          >
            <g
              opacity=".45"
              fill="#ef4444"
              stroke="#ef4444"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              {[...strokes, ...(draft ? [draft] : [])].map((stroke, i) => {
                const width = Math.max(
                  1,
                  Math.round(stroke.width * Math.min(size.width, size.height))
                );
                const points = stroke.points.map(
                  (p) =>
                    `${Math.round(p.x * (size.width - 1))},${Math.round(p.y * (size.height - 1))}`
                );
                return stroke.points.length === 1 ? (
                  <circle
                    key={i}
                    cx={Math.round(stroke.points[0].x * (size.width - 1))}
                    cy={Math.round(stroke.points[0].y * (size.height - 1))}
                    r={width / 2}
                    stroke="none"
                  />
                ) : (
                  <polyline
                    key={i}
                    points={points.join(' ')}
                    fill="none"
                    strokeWidth={width}
                  />
                );
              })}
            </g>
          </svg>
        )}
      </div>
      {mode !== 'none' && (
        <p className="mt-2 text-xs text-gray-600">
          {t(
            mode === 'brush'
              ? 'image-canvas-brush-help'
              : 'image-canvas-drag-help'
          )}
        </p>
      )}
    </div>
  );
}
