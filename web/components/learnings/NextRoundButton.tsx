import { useState } from 'react';
import { useRouter } from 'next/router';
import { useTranslation } from 'next-i18next';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { newActionId } from '@/components/generation/shared/newActionId';
import { errorMessage } from '@/lib/i18n/domain-errors';

export interface NextRoundButtonProps {
  productId: string;
  /** The learnings snapshot the next round is drafted from. */
  snapshotId: string | null | undefined;
  /** Whether the snapshot holds any usable conclusion. */
  canSeed: boolean;
  winners: number;
  losers: number;
  /** Called with the new plan once it is admitted; without it the button
   * opens the plan at `/w/{slug}/tests/{planId}`. */
  onCreated?: (planId: string) => void;
  className?: string;
}

/**
 * 「按结论生成下一轮」: a new test plan for the product with goal
 * `optimize`, seeded from the snapshot's winners and losers
 * (`from_learnings_snapshot_id`). The drafter keeps winners, skips losers
 * and keeps one exploratory direction.
 */
export function NextRoundButton({
  productId,
  snapshotId,
  canSeed,
  winners,
  losers,
  onCreated,
  className,
}: NextRoundButtonProps) {
  const { t } = useTranslation('learnings');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const router = useRouter();
  const [state, setState] = useState<'idle' | 'working' | 'created' | 'failed'>(
    'idle'
  );
  const [message, setMessage] = useState('');
  const disabled = !canSeed || !snapshotId || state === 'working';

  const start = async () => {
    if (!snapshotId) return;
    setState('working');
    const result = await gateway.createTestPlan({
      workspaceSlug: current.slug,
      actionInstanceId: newActionId(),
      body: {
        product_id: productId,
        goal: 'optimize',
        scale: 'sample',
        from_learnings_snapshot_id: snapshotId,
      },
    });
    if (result.kind === 'accepted') {
      setState('created');
      if (onCreated) onCreated(result.data.plan_id);
      // Straight to the new plan: the three questions are already answered
      // (goal optimize from these learnings, the default audience, samples
      // first); the plan page shows the AI drafting and lets the user edit.
      else
        void router.push(
          `/w/${current.slug}/tests/${encodeURIComponent(result.data.plan_id)}`
        );
      return;
    }
    setState('failed');
    setMessage(
      result.kind === 'problem' ? errorMessage(t, result.problem) : ''
    );
  };

  return (
    <div className={className}>
      <button
        type="button"
        className="btn btn-primary btn-sm"
        onClick={start}
        disabled={disabled}
        title={canSeed ? t('next-round-hint') : t('next-round-disabled')}
      >
        {state === 'working' && (
          <span
            className="loading loading-spinner loading-xs"
            aria-hidden="true"
          />
        )}
        {state === 'working' ? t('next-round-working') : t('next-round')}
      </button>
      {!canSeed && (
        <p className="mt-1 text-xs text-base-content/60">
          {t('next-round-disabled')}
        </p>
      )}
      {state === 'created' && (
        <p className="mt-1 text-sm text-success" role="status">
          {t('next-round-created', { winners, losers })}
        </p>
      )}
      {state === 'failed' && (
        <p className="mt-1 text-sm text-error" role="alert">
          {t('next-round-failed', { message: message || t('problem-unknown') })}
        </p>
      )}
    </div>
  );
}
