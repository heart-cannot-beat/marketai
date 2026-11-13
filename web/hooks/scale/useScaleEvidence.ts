import { useCallback, useEffect, useRef, useState } from 'react';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import type { ScaleMetaMode } from '@/lib/scale/meta-mode';

export type EnrollmentEvidence =
  components['schemas']['EnrollmentEvidenceResponse'];
export type EpisodeSummary = components['schemas']['EpisodeResponse'];
export type BreakdownPage = components['schemas']['BreakdownResponse'];
export type BreakdownSegment =
  components['schemas']['BreakdownSegmentResponse'];
export type BreakdownType = 'placement' | 'country' | 'age_gender';

/**
 * Current evidence for an Enrollment's active Episode (spec §3.4 step 3/4,
 * §4.4 "Evidence condition" axis, #114): the evidence-readiness projection
 * (`readiness`, null before the first manifest) and the accepted Evaluation
 * (`maturity`, `health` and the rest, all null before the first Evaluation)
 * -- not a fresh evaluation: collecting, sealing and evaluating are
 * scheduler/Worker-driven, never a browser-initiated read.
 *
 * 404 `no_active_episode` (a draft or closed Enrollment) is a normal,
 * displayable state, so callers read `problem` rather than treating it as a
 * hard failure. `lib/scale/evidence-view` names the state the rest is in.
 */
export function useScaleEnrollmentEvidence(
  enrollmentId: string | undefined,
  metaMode: ScaleMetaMode
) {
  return useDomainQuery(
    'get_scale_enrollment_evidence',
    enrollmentId
      ? ({ enrollment_id: enrollmentId, meta_mode: metaMode } as any)
      : undefined,
    { enabled: !!enrollmentId }
  );
}

/** Episode phase/maturity detail (spec §4.4 "Episode phase and maturity"
 * axis) -- Episode carries no directly HTTP-mutable action of its own, it
 * only moves as a side effect of an Enrollment lifecycle command. */
export function useScaleEpisode(
  enrollmentId: string | undefined,
  episodeId: string | undefined | null,
  metaMode: ScaleMetaMode
) {
  return useDomainQuery(
    'get_scale_episode',
    enrollmentId && episodeId
      ? ({
          enrollment_id: enrollmentId,
          episode_id: episodeId,
          meta_mode: metaMode,
        } as any)
      : undefined,
    { enabled: !!enrollmentId && !!episodeId }
  );
}

export interface UseScaleBreakdownsResult {
  /** The most recently fetched page (snapshot/window/segments metadata) --
   * `segments` accumulates across `loadMore()` calls instead. */
  page: BreakdownPage | undefined;
  segments: BreakdownSegment[];
  problem: ProblemDetails | undefined;
  isLoading: boolean;
  isLoadingMore: boolean;
  hasMore: boolean;
  loadMore: () => void;
  refresh: () => void;
}

/**
 * Snapshot-pinned, paginated breakdown projection (spec §4.2, Issue #87
 * Task 4/7): `snapshotId` pins every page in this view to the same
 * immutable evidence snapshot (a fresh evaluation must not shift a segment
 * table the caller is mid-page through) and `breakdownType` selects one of
 * `placement`/`country`/`age_gender`. Both are required by the server --
 * this hook does not fetch until a `snapshotId` is known (normally the
 * active Episode's current accepted-evidence snapshot, from
 * `useScaleEnrollmentEvidence`).
 *
 * Segments accumulate locally across `loadMore()` (the server's `Page`
 * envelope is cursor-only, no total count) and reset whenever the
 * Enrollment/snapshot/breakdown-type scope changes.
 */
export function useScaleEnrollmentBreakdowns(
  enrollmentId: string | undefined,
  metaMode: ScaleMetaMode,
  snapshotId: string | undefined,
  breakdownType: BreakdownType
): UseScaleBreakdownsResult {
  const [cursor, setCursor] = useState<string | undefined>(undefined);
  const [segments, setSegments] = useState<BreakdownSegment[]>([]);
  const enabled = !!enrollmentId && !!snapshotId;
  const scopeKey = `${enrollmentId ?? ''}:${snapshotId ?? ''}:${breakdownType}`;
  const scopeRef = useRef(scopeKey);

  if (scopeRef.current !== scopeKey) {
    scopeRef.current = scopeKey;
    // Scope changed mid-render (new Enrollment/snapshot/tab) -- reset
    // accumulation synchronously so `segments` never briefly shows the
    // previous scope's data under the new scope's key.
    if (cursor !== undefined) setCursor(undefined);
    if (segments.length !== 0) setSegments([]);
  }

  const query = useDomainQuery(
    'get_scale_enrollment_breakdowns',
    enabled
      ? ({
          enrollment_id: enrollmentId,
          meta_mode: metaMode,
          snapshot_id: snapshotId,
          breakdown_type: breakdownType,
          cursor,
        } as any)
      : undefined,
    { enabled }
  );

  const page = query.data as BreakdownPage | undefined;

  useEffect(() => {
    if (!page) return;
    setSegments((prev) =>
      cursor ? [...prev, ...page.segments] : page.segments
    );
    // `cursor` intentionally excluded -- it only decides append-vs-replace
    // for this page's arrival, not a dependency to re-run on.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page]);

  const loadMore = useCallback(() => {
    if (page?.next_cursor) setCursor(page.next_cursor);
  }, [page]);

  const refresh = useCallback(() => {
    setCursor(undefined);
    setSegments([]);
    void query.refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return {
    page,
    segments,
    problem: query.problem,
    isLoading: query.isLoading && cursor === undefined,
    isLoadingMore: query.isLoading && cursor !== undefined,
    hasMore: !!page?.next_cursor,
    loadMore,
    refresh,
  };
}
