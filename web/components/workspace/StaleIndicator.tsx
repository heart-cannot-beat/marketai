import { useCallback, useEffect, useReducer, useRef } from 'react';
import { useTranslation } from 'next-i18next';

interface StaleIndicatorProps {
  isStale: boolean;
  lastSuccessfulAt: number | null;
}

function formatAge(ms: number): string {
  const secs = Math.floor(ms / 1000);
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m`;
  return `${Math.floor(mins / 60)}h`;
}

/**
 * Client-cache age only — reflects how long ago `useDomainQuery` last saw a
 * successful response, not whether the underlying domain data itself
 * changed. Renders nothing while fresh. Announces the transition into
 * staleness once via `aria-live="polite"`, then falls silent (`aria-live`
 * flips to `"off"`) for the periodic age-text updates that follow so a
 * screen reader isn't re-announced every tick.
 *
 * The age string is derived straight from `Date.now()` during render
 * rather than cached in state that's only populated by an effect —
 * `useDomainQuery` callers are expected to keep this component mounted at
 * all times (it decides on its own whether to render), so the render that
 * first turns `isStale` on is a *re-render* of an already-mounted
 * instance, not a fresh mount. An effect-populated `useState('')` would
 * make that first `aria-live="polite"` render announce empty text before
 * the effect ever ran. A `useReducer` tick only forces the periodic
 * re-renders that keep the *displayed* age fresh every 10s; it plays no
 * part in computing the value itself.
 */
export function StaleIndicator({
  isStale,
  lastSuccessfulAt,
}: StaleIndicatorProps) {
  const { t } = useTranslation('common');
  const [, forceTick] = useReducer((c: number) => c + 1, 0);
  const announcedRef = useRef(false);

  useEffect(() => {
    if (!isStale || lastSuccessfulAt == null) {
      announcedRef.current = false;
      return;
    }
    const timer = setInterval(forceTick, 10_000);
    return () => clearInterval(timer);
  }, [isStale, lastSuccessfulAt]);

  // Stable ref-callback identity: React invokes it once on mount (not on
  // every re-render caused by the age ticking), so `announcedRef` flips to
  // `true` right after the first commit — the render that produced the
  // `aria-live="polite"` announcement (with the real age text already in
  // place) already happened by then.
  const markAnnounced = useCallback((node: HTMLSpanElement | null) => {
    if (node) announcedRef.current = true;
  }, []);

  if (!isStale || lastSuccessfulAt == null) return null;

  const age = formatAge(Date.now() - lastSuccessfulAt);

  return (
    <span
      className="text-xs text-gray-400"
      aria-live={announcedRef.current ? 'off' : 'polite'}
      ref={markAnnounced}
    >
      {t('workspace-loaded-ago', { age })}
    </span>
  );
}
