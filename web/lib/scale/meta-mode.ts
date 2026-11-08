import { useCallback, useMemo } from 'react';
import { useRouter } from 'next/router';

/**
 * `app.scale.types.MetaMode` (Python) mirrored client-side. Every Scale
 * read/command carries an explicit `meta_mode` (spec: a Workspace may
 * operate connections in more than one Meta mode; `request_tenant_context()`
 * never infers it). Scale never performs Meta writes regardless of mode --
 * this only selects which mode's already-synced evidence to read.
 */
export const SCALE_META_MODES = [
  'customer_sandbox',
  'controlled_mock',
  'internal_validation',
] as const;

export type ScaleMetaMode = (typeof SCALE_META_MODES)[number];

export const DEFAULT_SCALE_META_MODE: ScaleMetaMode = 'customer_sandbox';

export function isScaleMetaMode(value: unknown): value is ScaleMetaMode {
  return (
    typeof value === 'string' &&
    (SCALE_META_MODES as readonly string[]).includes(value)
  );
}

/**
 * The `scale` key that words a Meta mode read from the server, the same
 * `meta-mode.*` copy `MetaModeSelect` offers. A mode this build does not
 * know reads as the generic `meta-mode.unknown`, never as its code (#58).
 */
export function metaModeLabelKey(mode: string): string {
  return isScaleMetaMode(mode) ? `meta-mode.${mode}` : 'meta-mode.unknown';
}

/**
 * `meta_mode` is exclusively a URL path segment for every Scale route (spec
 * §3.10-§3.11, Issue #86 Task 6) -- never a query parameter or request-body
 * field. This mirrors `app.scale.v1_router`'s `mode_router`, mounted at
 * `/{meta_mode}` under `/api/v1/scale`.
 */
export function scaleBasePath(metaMode: ScaleMetaMode): string {
  return `/api/v1/scale/${metaMode}`;
}

/**
 * Reads/writes the `meta_mode` the current Scale page operates against,
 * persisted in the URL query string (`?meta_mode=...`) so it survives
 * navigation between the list, detail, evidence, breakdowns and targets
 * pages, and is shareable/bookmarkable like any other Scale view state.
 */
export function useScaleMetaMode(): {
  metaMode: ScaleMetaMode;
  setMetaMode: (mode: ScaleMetaMode) => void;
} {
  const router = useRouter();
  const raw = router.query.meta_mode;
  const metaMode = isScaleMetaMode(raw) ? raw : DEFAULT_SCALE_META_MODE;

  const setMetaMode = useCallback(
    (mode: ScaleMetaMode) => {
      router.replace(
        { query: { ...router.query, meta_mode: mode } },
        undefined,
        { shallow: true }
      );
    },
    [router]
  );

  return useMemo(() => ({ metaMode, setMetaMode }), [metaMode, setMetaMode]);
}
