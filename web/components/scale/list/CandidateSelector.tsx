import { useEffect, useRef, useState, useMemo } from 'react';
import { useTranslation } from 'next-i18next';
import { useScaleEnrollmentCandidates } from '@/hooks/scale/useScaleCandidates';
import {
  useCreateScaleEnrollmentCandidate,
  useCreateScaleEnrollment,
} from '@/hooks/scale';
import type { ScaleMetaMode } from '@/lib/scale/meta-mode';
import { errorMessage } from '@/lib/i18n/domain-errors';
import {
  ObservedAdPicker,
  type PickedDeliveryTarget,
} from './ObservedAdPicker';

interface CandidateSelectorProps {
  open: boolean;
  metaMode: ScaleMetaMode;
  onClose: () => void;
  onCreated: (enrollmentId: string) => void;
  preferredDecisionId?: string;
}

/** One advisory discovery row (spec §4.4) -- a confirmed Winner Decision
 * that currently passes the lighter identity/eligibility checks and has no
 * live Enrollment yet. Never persisted, never accepted back as input to a
 * command directly (`app.scale.candidates.CandidateDiscoveryItem`); the
 * caller must still name the exact `(decision_id, delivery_target_id)` pair
 * to `create_scale_enrollment_candidate`, which re-validates from scratch.
 * `list_scale_enrollment_candidates` has no declared `response_model`
 * server-side, so the generated `DomainOperationMap` types this body as
 * `null` -- the actual shape is `{items: CandidateDiscoveryItem[]}`. */
interface CandidateDiscoveryItem {
  decision_id: string;
  evidence_origin?: string;
  delivery_targets?: { id: string; label: string; source_type: string }[];
  delivery_unavailable_reason?: string | null;
  confirmation_id: string | null;
  experiment_version_id: string | null;
  candidate_id: string | null;
  asset_version_id: string | null;
}
interface CandidateDiscoveryResponse {
  items: CandidateDiscoveryItem[];
}

/**
 * New-Enrollment candidate selection (spec §4.1/§4.4, Issue #86 Task 15).
 *
 * Server discovery supplies eligible decisions. For the selected Winner the
 * user picks the observed ad it runs in, and the server makes the
 * DeliveryTarget from recorded evidence (#129); marketing sources also list
 * the observed targets already compatible with their exact conditions.
 * Creating a candidate and then a draft enrollment revalidates the pair
 * independently on the server.
 */
export function CandidateSelector({
  open,
  metaMode,
  onClose,
  onCreated,
  preferredDecisionId,
}: CandidateSelectorProps) {
  const { t } = useTranslation('scale');
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [selected, setSelected] = useState<CandidateDiscoveryItem | null>(null);
  const [deliveryTargetId, setDeliveryTargetId] = useState('');
  const [pickedTarget, setPickedTarget] = useState<PickedDeliveryTarget | null>(
    null
  );

  const {
    data,
    problem: candidatesProblem,
    isLoading,
  } = useScaleEnrollmentCandidates(metaMode);
  const items = useMemo(
    () => (data as CandidateDiscoveryResponse | undefined)?.items ?? [],
    [data]
  );

  useEffect(() => {
    if (open && !selected && preferredDecisionId) {
      const preferred = items.find(
        (item) => item.decision_id === preferredDecisionId
      );
      if (preferred) setSelected(preferred);
    }
  }, [open, selected, preferredDecisionId, items]);

  const createCandidateCmd = useCreateScaleEnrollmentCandidate();
  const createEnrollmentCmd = useCreateScaleEnrollment();
  const isPending =
    createCandidateCmd.isPending || createEnrollmentCmd.isPending;
  const problem = createCandidateCmd.problem ?? createEnrollmentCmd.problem;

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open && typeof dialog.showModal === 'function') {
      try {
        dialog.showModal();
      } catch {
        // jsdom
      }
    }
  }, [open]);

  useEffect(() => {
    if (!open) {
      setSelected(null);
      setDeliveryTargetId('');
      setPickedTarget(null);
      createCandidateCmd.reset();
      createEnrollmentCmd.reset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  if (!open) return null;

  const canSubmit = !!selected && deliveryTargetId.trim() !== '';

  const handleSubmit = async () => {
    if (!selected) return;
    const candidateOutcome = await createCandidateCmd.run({
      metaMode,
      winnerDecisionId: selected.decision_id,
      deliveryTargetId: deliveryTargetId.trim(),
    });
    if (candidateOutcome.kind !== 'success') return;

    const enrollmentOutcome = await createEnrollmentCmd.run({
      metaMode,
      enrollmentCandidateId: candidateOutcome.data.id,
    });
    if (enrollmentOutcome.kind === 'success') {
      onCreated(enrollmentOutcome.data.id);
    }
  };

  return (
    <dialog
      ref={dialogRef}
      className="modal modal-open"
      aria-labelledby="candidate-selector-title"
      onClose={onClose}
    >
      <div className="modal-box max-w-lg">
        <h3 id="candidate-selector-title" className="text-lg font-bold">
          {t('candidate-selector.title')}
        </h3>

        <p className="mt-2 text-sm text-base-content/70">
          {t('candidate-selector.description')}
        </p>

        {candidatesProblem && (
          <div className="mt-3 text-sm text-error" role="alert">
            {errorMessage(t, candidatesProblem)}
          </div>
        )}

        {isLoading && (
          <div className="mt-4 flex justify-center" aria-busy="true">
            <span className="loading loading-spinner loading-sm" />
          </div>
        )}

        {!isLoading && items.length === 0 && !candidatesProblem && (
          <div className="mt-4 text-sm text-base-content/70">
            {t('candidate-selector.empty')}
          </div>
        )}

        <ul className="mt-4 flex max-h-64 flex-col gap-2 overflow-y-auto">
          {items.map((item) => (
            <li key={item.decision_id}>
              <button
                type="button"
                className={`btn btn-sm w-full justify-start normal-case ${
                  selected?.decision_id === item.decision_id
                    ? 'btn-primary'
                    : 'btn-outline'
                }`}
                onClick={() => {
                  setSelected(item);
                  setDeliveryTargetId('');
                  setPickedTarget(null);
                }}
              >
                <span className="flex flex-col items-start text-left">
                  <span className="text-xs">
                    {item.evidence_origin === 'marketing_confirmed_combination'
                      ? t('candidate-selector.marketing-origin')
                      : item.decision_id}
                  </span>
                  {item.asset_version_id && (
                    <span className="text-xs opacity-70">
                      {t('candidate-selector.asset-version', {
                        id: item.asset_version_id,
                      })}
                    </span>
                  )}
                </span>
              </button>
            </li>
          ))}
        </ul>

        {selected && (
          <ObservedAdPicker
            metaMode={metaMode}
            winnerDecisionId={selected.decision_id}
            onPicked={(target) => {
              setPickedTarget(target);
              setDeliveryTargetId(target.id);
            }}
          />
        )}

        {selected?.evidence_origin === 'marketing_confirmed_combination' ? (
          <div className="mt-4 space-y-3 text-sm">
            <p>{t('candidate-selector.marketing-boundary')}</p>
            {selected.delivery_targets?.length || pickedTarget ? (
              <label className="block">
                {t('candidate-selector.match-delivery')}
                <select
                  className="select select-bordered mt-1 w-full min-w-0"
                  value={deliveryTargetId}
                  onChange={(e) => setDeliveryTargetId(e.target.value)}
                >
                  <option value="">
                    {t('candidate-selector.choose-delivery')}
                  </option>
                  {(selected.delivery_targets ?? []).map((target) => (
                    <option key={target.id} value={target.id}>
                      {target.label}
                    </option>
                  ))}
                  {pickedTarget &&
                    !selected.delivery_targets?.some(
                      (target) => target.id === pickedTarget.id
                    ) && (
                      <option value={pickedTarget.id}>
                        {t('candidate-selector.observed-ad-option', {
                          market: pickedTarget.market_code,
                          placement: pickedTarget.placement,
                          ad: pickedTarget.ad_remote_id,
                        })}
                      </option>
                    )}
                </select>
              </label>
            ) : (
              <p>{t('candidate-selector.delivery-not-ready')}</p>
            )}
          </div>
        ) : (
          selected && (
            <label className="form-control mt-4">
              <span className="label-text text-xs">
                {t('candidate-selector.field.delivery_target_id')}
              </span>
              <input
                type="text"
                className="input input-bordered input-sm"
                value={deliveryTargetId}
                onChange={(e) => setDeliveryTargetId(e.target.value)}
              />
            </label>
          )
        )}

        {problem && (
          <div className="mt-3 text-sm text-error" role="alert">
            {errorMessage(t, problem)}
          </div>
        )}

        <div className="modal-action mt-6">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={onClose}
          >
            {t('candidate-selector.cancel')}
          </button>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={!canSubmit || isPending}
            onClick={handleSubmit}
          >
            {isPending
              ? t('candidate-selector.creating')
              : t('candidate-selector.create')}
          </button>
        </div>
      </div>
      <form method="dialog" className="modal-backdrop">
        <button type="submit">{t('candidate-selector.cancel')}</button>
      </form>
    </dialog>
  );
}
