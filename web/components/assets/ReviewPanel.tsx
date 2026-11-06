import { useCallback, useState } from 'react';
import { useTranslation } from 'next-i18next';
import { useSWRConfig } from 'swr';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import {
  matchesOperation,
  matchesWorkspace,
} from '@/lib/domain-client/query-keys';
import type { DomainQueryKey } from '@/lib/domain-client/query-keys';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { errorMessage } from '@/lib/i18n/domain-errors';

interface ReviewPanelProps {
  versionId: string;
  reviewHeadId: string | null;
  effectiveReview: string | null;
  onReviewSubmitted?: (value: string) => void;
}

type ReviewValue = 'approved' | 'needs_changes' | 'rejected' | 'cleared';

/** The A / C / X keys stay as letters (they match review mode's keyboard
 * shortcuts); the Chinese verdict sits beside each (Issue #191). */
const REVIEW_BUTTONS: Array<{
  value: Exclude<ReviewValue, 'cleared'>;
  key: string;
  labelKey: string;
  testId: string;
  active: string;
  idle: string;
}> = [
  {
    value: 'approved',
    key: 'A',
    labelKey: 'review-approve',
    testId: 'review-approve',
    active: 'btn-success',
    idle: 'btn-outline btn-success',
  },
  {
    value: 'needs_changes',
    key: 'C',
    labelKey: 'review-needs-changes',
    testId: 'review-needs-changes',
    active: 'btn-warning',
    idle: 'btn-outline btn-warning',
  },
  {
    value: 'rejected',
    key: 'X',
    labelKey: 'review-reject',
    testId: 'review-reject',
    active: 'btn-error',
    idle: 'btn-outline btn-error',
  },
];

export function ReviewPanel({
  versionId,
  reviewHeadId,
  effectiveReview,
  onReviewSubmitted,
}: ReviewPanelProps) {
  const { t } = useTranslation('assets');
  const command = useDomainCommand('create_generation_asset_review');
  const [submitting, setSubmitting] = useState<ReviewValue | null>(null);
  const { mutate: globalMutate } = useSWRConfig();
  const { current } = useWorkspace();

  const submitReview = useCallback(
    async (value: ReviewValue) => {
      setSubmitting(value);
      const body: Record<string, unknown> = {
        review_value: value,
      };
      if (reviewHeadId) {
        body.supersedes_review_id = reviewHeadId;
      }

      const actionInstanceId = `review:${versionId}:${Date.now()}`;
      const outcome = await command.execute({
        pathParams: { version_id: versionId },
        body: body as any,
        actionInstanceId,
      });
      setSubmitting(null);

      if (outcome.kind === 'success') {
        // The gallery grid and this asset's own lightbox/sidebar detail
        // both read `effective_review_value` -- revalidate both caches so
        // the review just submitted here is reflected without a manual
        // refresh (spec §14 row 10, "Append-only effective review").
        globalMutate(
          (key: unknown) => {
            if (!Array.isArray(key) || key[0] !== 'domain') return false;
            const dk = key as unknown as DomainQueryKey;
            if (!matchesWorkspace(dk, current.slug)) return false;
            return (
              matchesOperation(dk, 'list_generation_assets') ||
              matchesOperation(dk, 'get_generation_asset_version')
            );
          },
          undefined,
          { revalidate: true }
        );

        if (onReviewSubmitted) {
          onReviewSubmitted(value);
        }
      }
    },
    [
      versionId,
      reviewHeadId,
      command,
      onReviewSubmitted,
      globalMutate,
      current.slug,
    ]
  );

  return (
    <div className="flex flex-col gap-2">
      <div
        className="flex items-center gap-1.5"
        role="group"
        aria-label={t('review-actions-label')}
      >
        {REVIEW_BUTTONS.map((button) => (
          <button
            key={button.value}
            type="button"
            className={`btn btn-sm flex-1 gap-1 ${effectiveReview === button.value ? button.active : button.idle}`}
            onClick={() => submitReview(button.value)}
            disabled={submitting !== null}
            aria-label={t(button.labelKey)}
            aria-keyshortcuts={button.key}
            data-testid={button.testId}
          >
            {submitting === button.value ? (
              <span className="loading loading-spinner loading-xs" />
            ) : (
              <>
                <kbd className="font-mono font-semibold">{button.key}</kbd>
                <span>{t(button.labelKey)}</span>
              </>
            )}
          </button>
        ))}
      </div>

      {effectiveReview && (
        <button
          type="button"
          className="btn btn-xs btn-ghost"
          onClick={() => submitReview('cleared')}
          disabled={submitting !== null}
          aria-label={t('review-clear')}
          data-testid="review-clear"
        >
          {submitting === 'cleared' ? (
            <span className="loading loading-spinner loading-xs" />
          ) : (
            t('review-clear')
          )}
        </button>
      )}

      {command.problem && (
        <div role="alert" className="text-xs text-error">
          {errorMessage(t, command.problem)}
        </div>
      )}

      <div aria-live="assertive" className="sr-only">
        {submitting === null && command.result ? t('review-submitted') : ''}
      </div>
    </div>
  );
}
