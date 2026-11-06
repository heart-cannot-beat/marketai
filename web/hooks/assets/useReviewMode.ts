import { useCallback, useEffect, useRef, useState } from 'react';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import type { AssetFilters } from '@/hooks/assets/useAssetsUrlState';
import {
  isActionAllowed,
  type ActionEligibility,
} from '@/hooks/assets/useAssetActions';

type ReviewValue = 'approved' | 'needs_changes' | 'rejected';

export interface ReviewableVersion {
  id: string;
  review_head_id: string | null;
  effective_review_value: string | null;
  allowed_actions: ActionEligibility[];
}

export interface ReviewStats {
  total: number;
  reviewed: number;
  skipped: number;
  approved: number;
  needsChanges: number;
  rejected: number;
}

interface ReviewQueueSnapshot {
  version: 1;
  orderedVersionIds: string[];
  currentIndex: number;
  stats: ReviewStats;
  reviewHeadsByVersion: Record<string, string>;
  reviewValuesByVersion: Record<string, ReviewValue>;
}

interface UseReviewModeOptions {
  storageKey: string;
  isQueueLoading: boolean;
}

export interface UseReviewModeResult {
  currentIndex: number;
  currentVersionId: string | undefined;
  isComplete: boolean;
  isInitializing: boolean;
  stats: ReviewStats;
  submitReview: (
    value: ReviewValue,
    currentVersion: ReviewableVersion
  ) => Promise<void>;
  skip: () => void;
  goBack: () => void;
  clearQueue: () => void;
  isSubmitting: boolean;
}

function emptyStats(total: number): ReviewStats {
  return {
    total,
    reviewed: 0,
    skipped: 0,
    approved: 0,
    needsChanges: 0,
    rejected: 0,
  };
}

function readReviewQueueSnapshot(
  storageKey: string
): ReviewQueueSnapshot | undefined {
  if (typeof window === 'undefined') return undefined;

  try {
    const raw = sessionStorage.getItem(storageKey);
    if (!raw) return undefined;
    const parsed = JSON.parse(raw) as Partial<ReviewQueueSnapshot>;
    if (
      parsed.version !== 1 ||
      !Array.isArray(parsed.orderedVersionIds) ||
      !parsed.orderedVersionIds.every((id) => typeof id === 'string') ||
      typeof parsed.currentIndex !== 'number' ||
      !parsed.stats
    ) {
      return undefined;
    }
    return {
      version: 1,
      orderedVersionIds: parsed.orderedVersionIds,
      currentIndex: Math.min(
        Math.max(0, parsed.currentIndex),
        parsed.orderedVersionIds.length
      ),
      stats: {
        ...emptyStats(parsed.orderedVersionIds.length),
        ...parsed.stats,
        total: parsed.orderedVersionIds.length,
      },
      reviewHeadsByVersion:
        parsed.reviewHeadsByVersion &&
        typeof parsed.reviewHeadsByVersion === 'object' &&
        Object.values(parsed.reviewHeadsByVersion).every(
          (reviewId) => typeof reviewId === 'string'
        )
          ? (parsed.reviewHeadsByVersion as Record<string, string>)
          : {},
      reviewValuesByVersion:
        parsed.reviewValuesByVersion &&
        typeof parsed.reviewValuesByVersion === 'object' &&
        Object.values(parsed.reviewValuesByVersion).every(
          (value) =>
            value === 'approved' ||
            value === 'needs_changes' ||
            value === 'rejected'
        )
          ? (parsed.reviewValuesByVersion as Record<string, ReviewValue>)
          : {},
    };
  } catch {
    return undefined;
  }
}

function writeReviewQueueSnapshot(
  storageKey: string,
  snapshot: ReviewQueueSnapshot
): void {
  if (typeof window === 'undefined') return;
  try {
    sessionStorage.setItem(storageKey, JSON.stringify(snapshot));
  } catch {
    // Storage is only a recovery cache; the live review session still works.
  }
}

/** Creates the immutable selected-version queue before the review overlay
 * enters the URL, closing the refresh gap between the click and first render. */
export function seedReviewQueueSnapshot(
  storageKey: string,
  orderedVersionIds: string[]
): void {
  writeReviewQueueSnapshot(storageKey, {
    version: 1,
    orderedVersionIds: [...orderedVersionIds],
    currentIndex: 0,
    stats: emptyStats(orderedVersionIds.length),
    reviewHeadsByVersion: {},
    reviewValuesByVersion: {},
  });
}

/** Stable, Workspace- and filter-scoped storage key. */
export function reviewQueueStorageKey(
  workspaceSlug: string,
  filters: AssetFilters
): string {
  const scope = Object.entries(filters)
    .filter((entry): entry is [string, string] => entry[1] !== undefined)
    .sort(([left], [right]) => left.localeCompare(right));
  let hash = 2166136261;
  for (const char of JSON.stringify(scope)) {
    hash ^= char.charCodeAt(0);
    hash = Math.imul(hash, 16777619);
  }
  return `asset-review-queue:${workspaceSlug}:${(hash >>> 0).toString(36)}`;
}

export function useReviewMode(
  initialQueueIds: string[],
  options: UseReviewModeOptions
): UseReviewModeResult {
  const command = useDomainCommand('create_generation_asset_review');
  const mutexRef = useRef(false);
  const hydratedStorageKeyRef = useRef<string | null>(null);
  const [queueIds, setQueueIds] = useState<string[]>(() => [
    ...initialQueueIds,
  ]);
  const [currentIndex, setCurrentIndex] = useState(0);
  const [stats, setStats] = useState<ReviewStats>(() =>
    emptyStats(initialQueueIds.length)
  );
  const [reviewHeadsByVersion, setReviewHeadsByVersion] = useState<
    Record<string, string>
  >({});
  const [reviewValuesByVersion, setReviewValuesByVersion] = useState<
    Record<string, ReviewValue>
  >({});
  // Server and first client render must stay identical. sessionStorage is
  // intentionally read only after hydration; otherwise a refreshed queue
  // renders progress client-side over the server's loading placeholder.
  const [isInitializing, setIsInitializing] = useState(true);
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    hydratedStorageKeyRef.current = null;
    const snapshot = readReviewQueueSnapshot(options.storageKey);
    if (!snapshot) {
      setIsInitializing(true);
      return;
    }
    setQueueIds(snapshot.orderedVersionIds);
    setCurrentIndex(snapshot.currentIndex);
    setStats(snapshot.stats);
    setReviewHeadsByVersion(snapshot.reviewHeadsByVersion);
    setReviewValuesByVersion(snapshot.reviewValuesByVersion);
    hydratedStorageKeyRef.current = options.storageKey;
    setIsInitializing(false);
  }, [options.storageKey]);

  useEffect(() => {
    if (hydratedStorageKeyRef.current !== null || options.isQueueLoading) {
      return;
    }
    const ids = [...initialQueueIds];
    setQueueIds(ids);
    setCurrentIndex(0);
    setStats(emptyStats(ids.length));
    setReviewHeadsByVersion({});
    setReviewValuesByVersion({});
    hydratedStorageKeyRef.current = options.storageKey;
    setIsInitializing(false);
  }, [initialQueueIds, options.isQueueLoading, options.storageKey]);

  useEffect(() => {
    if (
      isInitializing ||
      hydratedStorageKeyRef.current !== options.storageKey
    ) {
      return;
    }
    writeReviewQueueSnapshot(options.storageKey, {
      version: 1,
      orderedVersionIds: queueIds,
      currentIndex,
      stats,
      reviewHeadsByVersion,
      reviewValuesByVersion,
    });
  }, [
    options.storageKey,
    queueIds,
    currentIndex,
    stats,
    reviewHeadsByVersion,
    reviewValuesByVersion,
    isInitializing,
  ]);

  const currentVersionId = queueIds[currentIndex];
  const isComplete = !isInitializing && currentIndex >= queueIds.length;

  const submitReview = useCallback(
    async (value: ReviewValue, currentVersion: ReviewableVersion) => {
      if (
        mutexRef.current ||
        !currentVersionId ||
        currentVersion.id !== currentVersionId ||
        !isActionAllowed(currentVersion.allowed_actions, 'review')
      ) {
        return;
      }
      mutexRef.current = true;
      setIsSubmitting(true);

      try {
        const body: Record<string, unknown> = { review_value: value };
        const reviewHead =
          reviewHeadsByVersion[currentVersion.id] ??
          currentVersion.review_head_id;
        if (reviewHead) {
          body.supersedes_review_id = reviewHead;
        }

        const actionInstanceId = `review-mode:${currentVersion.id}:${Date.now()}`;
        const outcome = await command.execute({
          pathParams: { version_id: currentVersion.id },
          body: body as any,
          actionInstanceId,
        });

        if (outcome.kind === 'success') {
          const previousValue = reviewValuesByVersion[currentVersion.id];
          const createdReviewId = (outcome.data as { id?: unknown }).id;
          if (typeof createdReviewId === 'string') {
            setReviewHeadsByVersion((previous) => ({
              ...previous,
              [currentVersion.id]: createdReviewId,
            }));
          }
          setReviewValuesByVersion((previous) => ({
            ...previous,
            [currentVersion.id]: value,
          }));
          setStats((previous) => ({
            ...previous,
            reviewed: previous.reviewed + (previousValue ? 0 : 1),
            approved:
              previous.approved +
              (value === 'approved' ? 1 : 0) -
              (previousValue === 'approved' ? 1 : 0),
            needsChanges:
              previous.needsChanges +
              (value === 'needs_changes' ? 1 : 0) -
              (previousValue === 'needs_changes' ? 1 : 0),
            rejected:
              previous.rejected +
              (value === 'rejected' ? 1 : 0) -
              (previousValue === 'rejected' ? 1 : 0),
          }));
          setCurrentIndex((previous) => previous + 1);
        }
      } finally {
        mutexRef.current = false;
        setIsSubmitting(false);
      }
    },
    [currentVersionId, command, reviewHeadsByVersion, reviewValuesByVersion]
  );

  const skip = useCallback(() => {
    if (mutexRef.current) return;
    setStats((previous) => ({ ...previous, skipped: previous.skipped + 1 }));
    setCurrentIndex((previous) => Math.min(queueIds.length, previous + 1));
  }, [queueIds.length]);

  const goBack = useCallback(() => {
    if (mutexRef.current) return;
    setCurrentIndex((previous) => Math.max(0, previous - 1));
  }, []);

  const clearQueue = useCallback(() => {
    if (typeof window !== 'undefined') {
      try {
        sessionStorage.removeItem(options.storageKey);
      } catch {
        // Best-effort cleanup only.
      }
    }
  }, [options.storageKey]);

  return {
    currentIndex,
    currentVersionId,
    isComplete,
    isInitializing,
    stats,
    submitReview,
    skip,
    goBack,
    clearQueue,
    isSubmitting,
  };
}
