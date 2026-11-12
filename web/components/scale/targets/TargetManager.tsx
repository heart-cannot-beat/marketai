import { useEffect, useId, useRef, useState } from 'react';
import { useTranslation } from 'next-i18next';
import {
  useScaleTargets,
  useScaleTargetContexts,
  useCreateScaleTargetPublicationPreview,
  usePublishScaleTarget,
  useCreateScaleTargetWithdrawalPreview,
  useWithdrawScaleTarget,
  type ScaleTarget,
  type ScaleTargetContext,
  type ScaleTargetPublicationPreview,
  type ScaleTargetWithdrawalPreview,
} from '@/hooks/scale/useScaleTargets';
import { useScaleMetaMode, type ScaleMetaMode } from '@/lib/scale/meta-mode';
import { MetaModeSelect } from '@/components/scale/shared/MetaModeSelect';
import { LifecycleBadge } from '@/components/scale/shared/LifecycleBadge';
import { AllowedActionButton } from '@/components/scale/shared/AllowedActionButton';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import {
  errorMessage,
  fieldErrorMessage,
  fieldPath,
} from '@/lib/i18n/domain-errors';

const MONEY_FIELDS = [
  'target_cpa',
  'breakeven_roas',
  'minimum_economic_spend',
] as const;

type MoneyField = (typeof MONEY_FIELDS)[number];

type MoneyForm = Record<MoneyField, string>;

/** The refusals that are about the minimum economic spend against the
 * platform floor (#134): the field shows them as its own. */
const MINIMUM_SPEND_CODES = new Set([
  'minimum_economic_spend_required',
  'minimum_economic_spend_below_platform_floor',
]);

/** What the publish form says about the platform floor of the chosen
 * context (#134): the floor and when the minimum is required, or why there
 * is no floor to check against. The server decides; this only reads it. */
function PlatformFloorHint({
  context,
  id,
}: {
  context: ScaleTargetContext;
  id: string;
}) {
  const { t } = useTranslation('scale');
  const params = { currency: context.currency };
  if (context.platform_spend_floor !== null) {
    return (
      <div id={id} className="mt-3 text-xs text-base-content/70">
        <p>
          {t('targets.publish-dialog.platform-floor', {
            ...params,
            floor: context.platform_spend_floor,
          })}
        </p>
        <p>{t('targets.publish-dialog.minimum-required-when-roas-only')}</p>
      </div>
    );
  }
  const reason = context.platform_spend_floor_unavailable_reason;
  return (
    <p id={id} className="mt-3 text-xs text-warning">
      {reason === 'spend_floor_currency_unsupported'
        ? t('targets.publish-dialog.floor-currency-unsupported', params)
        : t('targets.publish-dialog.floor-no-eligible-policy', params)}
    </p>
  );
}

const EMPTY_MONEY_FORM: MoneyForm = MONEY_FIELDS.reduce(
  (acc, field) => ({ ...acc, [field]: '' }),
  {} as MoneyForm
);

interface PublishTargetDialogProps {
  open: boolean;
  metaMode: ScaleMetaMode;
  onClose: () => void;
  onPublished: () => void;
}

/** Publish flow (spec §10, §21): context selector -> enter target
 * CPA/ROAS/minimum spend -> preview -> confirm. A context that already has an
 * active target stays selectable: publishing replaces it, and the confirm
 * step says so (#398). A confirm the server refuses with `preview_expired`,
 * or with `preview_hash_mismatch` because the context changed, offers a new
 * preview in place (#382, #398); the user confirms again after seeing its
 * impact. */
function PublishTargetDialog({
  open,
  metaMode,
  onClose,
  onPublished,
}: PublishTargetDialogProps) {
  const { t } = useTranslation('scale');
  const {
    contexts,
    isLoading: contextsLoading,
    refresh: refreshContexts,
  } = useScaleTargetContexts(metaMode);
  const [contextIndex, setContextIndex] = useState<number | null>(null);
  const [form, setForm] = useState<MoneyForm>(EMPTY_MONEY_FORM);
  const [preview, setPreview] = useState<ScaleTargetPublicationPreview | null>(
    null
  );
  const previewCmd = useCreateScaleTargetPublicationPreview();
  const publishCmd = usePublishScaleTarget();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const fieldId = useId();

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

  if (!open) return null;

  const context = contextIndex !== null ? contexts[contextIndex] : undefined;

  /** The server's reasons for one amount (#381); the summary below the
   * fields still reads the first of them. A refusal of the minimum against
   * the platform floor (#134) is that field's too. */
  const fieldMessages = (field: MoneyField) => {
    const problem = previewCmd.problem;
    if (problem?.code === 'validation_error')
      return (problem.field_errors ?? [])
        .filter((error) => fieldPath(error.field) === field)
        .map((error) => fieldErrorMessage(t, error));
    if (
      field === 'minimum_economic_spend' &&
      problem?.code &&
      MINIMUM_SPEND_CODES.has(problem.code)
    )
      return [errorMessage(t, problem)];
    return [];
  };

  // #134: with a ROAS and no CPA, the minimum is required.
  const roasOnly =
    form.breakeven_roas.trim() !== '' && form.target_cpa.trim() === '';
  const floorHintId = `${fieldId}-platform-floor`;

  const reset = () => {
    setContextIndex(null);
    setForm(EMPTY_MONEY_FORM);
    setPreview(null);
    previewCmd.reset();
    publishCmd.reset();
  };

  const handleClose = () => {
    reset();
    onClose();
  };

  /** The amounts both previews send, trimmed (#381) so a renewed preview
   * reads the form exactly as the first one did. */
  const amounts = () => ({
    targetCpa: form.target_cpa.trim(),
    breakevenRoas: form.breakeven_roas.trim(),
    minimumEconomicSpend: form.minimum_economic_spend.trim(),
  });

  // Each preview refetches the context list too, so the replacement line
  // on the confirm step reads what the context has now, not what it had
  // when the dialog opened (#398).
  const handleCreatePreview = async () => {
    if (!context) return;
    void refreshContexts();
    const outcome = await previewCmd.run({
      metaMode,
      productVersionId: context.product_version_id,
      offerVersionId: context.offer_version_id,
      marketCode: context.market_code,
      currency: context.currency,
      ...amounts(),
    });
    if (outcome.kind === 'success') {
      setPreview(outcome.data as ScaleTargetPublicationPreview);
    }
  };

  const handleConfirm = async () => {
    if (!preview) return;
    const outcome = await publishCmd.run({
      metaMode,
      publicationPreviewId: preview.id,
    });
    if (outcome.kind === 'success') {
      reset();
      onPublished();
    }
  };

  // The context comes from the expired preview itself, not
  // `contexts[contextIndex]`: the list refetches on focus in no stable
  // order. The amounts are the user's own strings, never the echoed ones.
  const handleRefreshPreview = async () => {
    if (!preview) return;
    void refreshContexts();
    const outcome = await previewCmd.run({
      metaMode,
      productVersionId: preview.product_version_id,
      offerVersionId: preview.offer_version_id,
      marketCode: preview.market_code,
      currency: preview.currency,
      ...amounts(),
    });
    if (outcome.kind === 'success') {
      publishCmd.reset();
      setPreview(outcome.data as ScaleTargetPublicationPreview);
    }
  };

  const previewRenewable =
    publishCmd.problem?.code === 'preview_expired' ||
    publishCmd.problem?.code === 'preview_hash_mismatch';

  // Matched on the previewed context's fields, for the same reason as above.
  const replacesActiveTarget =
    preview !== null &&
    contexts.some(
      (c) =>
        c.product_version_id === preview.product_version_id &&
        c.offer_version_id === preview.offer_version_id &&
        c.market_code === preview.market_code &&
        c.currency === preview.currency &&
        c.has_active_target
    );

  return (
    <dialog
      ref={dialogRef}
      className="modal modal-open"
      aria-labelledby="publish-target-title"
      onClose={handleClose}
    >
      <div className="modal-box max-w-lg">
        <h3 id="publish-target-title" className="text-lg font-bold">
          {t('targets.publish-dialog.title')}
        </h3>

        {!preview && (
          <>
            {contextsLoading ? (
              <div className="mt-4 flex justify-center" aria-busy="true">
                <span className="loading loading-spinner loading-sm" />
              </div>
            ) : contexts.length === 0 ? (
              <p className="mt-4 text-sm text-base-content/60">
                {t('targets.publish-dialog.no-contexts')}
              </p>
            ) : (
              <>
                <label className="form-control mt-4">
                  <span className="label-text text-xs">
                    {t('targets.publish-dialog.context-label')}
                  </span>
                  <select
                    className="select select-bordered select-sm"
                    value={contextIndex ?? ''}
                    onChange={(e) =>
                      setContextIndex(
                        e.target.value === '' ? null : Number(e.target.value)
                      )
                    }
                  >
                    <option value="">
                      {t('targets.publish-dialog.context-placeholder')}
                    </option>
                    {contexts.map((c, i) => (
                      <option key={i} value={i}>
                        {c.product_version_id} / {c.offer_version_id} /{' '}
                        {c.market_code} ({c.currency})
                        {c.has_active_target
                          ? ` — ${t('targets.publish-dialog.has-active-target')}`
                          : ''}
                      </option>
                    ))}
                  </select>
                </label>

                <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
                  {MONEY_FIELDS.map((field) => {
                    const errors = fieldMessages(field);
                    const inputId = `${fieldId}-${field}`;
                    const isMinimum = field === 'minimum_economic_spend';
                    const describedBy = [
                      isMinimum && context ? floorHintId : null,
                      errors.length > 0 ? `${inputId}-errors` : null,
                    ]
                      .filter(Boolean)
                      .join(' ');
                    return (
                      <div key={field} className="form-control">
                        <label htmlFor={inputId} className="label-text text-xs">
                          {t(`targets.field.${field}`)}
                        </label>
                        <input
                          id={inputId}
                          type="text"
                          inputMode="decimal"
                          className={`input input-bordered input-sm${
                            errors.length > 0 ? ' input-error' : ''
                          }`}
                          aria-invalid={errors.length > 0 || undefined}
                          aria-required={(isMinimum && roasOnly) || undefined}
                          aria-describedby={describedBy || undefined}
                          value={form[field]}
                          onChange={(e) =>
                            setForm((prev) => ({
                              ...prev,
                              [field]: e.target.value,
                            }))
                          }
                        />
                        {errors.length > 0 && (
                          <p
                            id={`${inputId}-errors`}
                            className="mt-1 text-xs text-error"
                          >
                            {errors.join(' ')}
                          </p>
                        )}
                      </div>
                    );
                  })}
                </div>

                {context && (
                  <PlatformFloorHint context={context} id={floorHintId} />
                )}

                {previewCmd.problem && (
                  <p className="mt-2 text-sm text-error" role="alert">
                    {errorMessage(t, previewCmd.problem)}
                  </p>
                )}

                <div className="modal-action mt-4">
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    onClick={handleClose}
                  >
                    {t('targets.publish-dialog.cancel')}
                  </button>
                  <button
                    type="button"
                    className="btn btn-primary btn-sm"
                    disabled={!context || previewCmd.isPending}
                    onClick={handleCreatePreview}
                  >
                    {previewCmd.isPending
                      ? t('targets.publish-dialog.previewing')
                      : t('targets.publish-dialog.preview')}
                  </button>
                </div>
              </>
            )}
          </>
        )}

        {preview && (
          <>
            <dl className="mt-4 grid grid-cols-2 gap-2 text-sm">
              <div>
                <dt className="text-xs text-base-content/60">
                  {t('targets.publish-dialog.affected-enrollments')}
                </dt>
                <dd>{preview.affected_enrollment_count}</dd>
              </div>
              <div>
                <dt className="text-xs text-base-content/60">
                  {t('targets.publish-dialog.policy-floor-check')}
                </dt>
                <dd>
                  {preview.policy_floor_check_passed
                    ? t('targets.publish-dialog.policy-floor-passed')
                    : t('targets.publish-dialog.policy-floor-failed')}
                </dd>
              </div>
            </dl>

            {replacesActiveTarget && (
              <p className="mt-3 text-sm text-warning">
                {t('targets.publish-dialog.replaces-active-target')}
              </p>
            )}

            {publishCmd.problem && (
              <p className="mt-2 text-sm text-error" role="alert">
                {errorMessage(t, publishCmd.problem)}
              </p>
            )}
            {previewCmd.problem && (
              <p className="mt-2 text-sm text-error" role="alert">
                {errorMessage(t, previewCmd.problem)}
              </p>
            )}

            <div className="modal-action mt-4">
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={handleClose}
              >
                {t('targets.publish-dialog.cancel')}
              </button>
              {previewRenewable ? (
                <button
                  type="button"
                  className="btn btn-primary btn-sm"
                  disabled={previewCmd.isPending}
                  onClick={handleRefreshPreview}
                >
                  {previewCmd.isPending
                    ? t('targets.publish-dialog.previewing')
                    : t('targets.publish-dialog.refresh-preview')}
                </button>
              ) : (
                <button
                  type="button"
                  className="btn btn-primary btn-sm"
                  disabled={publishCmd.isPending}
                  onClick={handleConfirm}
                >
                  {publishCmd.isPending
                    ? t('targets.publish-dialog.publishing')
                    : t('targets.publish-dialog.confirm')}
                </button>
              )}
            </div>
          </>
        )}
      </div>
      <form method="dialog" className="modal-backdrop">
        <button type="button" onClick={handleClose}>
          {t('targets.publish-dialog.cancel')}
        </button>
      </form>
    </dialog>
  );
}

interface WithdrawTargetDialogProps {
  target: ScaleTarget;
  metaMode: ScaleMetaMode;
  onClose: () => void;
  onWithdrawn: () => void;
}

/** Withdraw flow (spec §10, §21): reason -> preview -> confirm. Only
 * rendered for a Target `allowed_actions` already marks
 * `withdraw` on (the caller gates the trigger button). An expired preview
 * is renewed in place, like publishing (#382). */
function WithdrawTargetDialog({
  target,
  metaMode,
  onClose,
  onWithdrawn,
}: WithdrawTargetDialogProps) {
  const { t } = useTranslation('scale');
  const [reason, setReason] = useState('');
  const [preview, setPreview] = useState<ScaleTargetWithdrawalPreview | null>(
    null
  );
  const previewCmd = useCreateScaleTargetWithdrawalPreview();
  const withdrawCmd = useWithdrawScaleTarget();
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (!dialog.open && typeof dialog.showModal === 'function') {
      try {
        dialog.showModal();
      } catch {
        // jsdom
      }
    }
  }, []);

  const reset = () => {
    setReason('');
    setPreview(null);
    previewCmd.reset();
    withdrawCmd.reset();
  };

  const handleClose = () => {
    reset();
    onClose();
  };

  const handleCreatePreview = async () => {
    const outcome = await previewCmd.run({
      targetId: target.id,
      metaMode,
      reason,
    });
    if (outcome.kind === 'success') {
      setPreview(outcome.data as ScaleTargetWithdrawalPreview);
    }
  };

  const handleConfirm = async () => {
    if (!preview) return;
    const outcome = await withdrawCmd.run({
      targetId: target.id,
      metaMode,
      withdrawalPreviewId: preview.id,
    });
    if (outcome.kind === 'success') {
      reset();
      onWithdrawn();
    }
  };

  const handleRefreshPreview = async () => {
    const outcome = await previewCmd.run({
      targetId: target.id,
      metaMode,
      reason,
    });
    if (outcome.kind === 'success') {
      withdrawCmd.reset();
      setPreview(outcome.data as ScaleTargetWithdrawalPreview);
    }
  };

  const previewExpired = withdrawCmd.problem?.code === 'preview_expired';

  return (
    <dialog
      ref={dialogRef}
      className="modal modal-open"
      aria-labelledby="withdraw-target-title"
      onClose={handleClose}
    >
      <div className="modal-box max-w-md">
        <h3 id="withdraw-target-title" className="text-lg font-bold">
          {t('targets.withdraw-dialog.title')}
        </h3>

        {!preview && (
          <>
            <label className="form-control mt-4">
              <span className="label-text text-xs">
                {t('targets.withdraw-dialog.reason-label')}
              </span>
              <textarea
                className="textarea textarea-bordered textarea-sm"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
            </label>

            {previewCmd.problem && (
              <p className="mt-2 text-sm text-error" role="alert">
                {errorMessage(t, previewCmd.problem)}
              </p>
            )}

            <div className="modal-action mt-4">
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={handleClose}
              >
                {t('targets.withdraw-dialog.cancel')}
              </button>
              <button
                type="button"
                className="btn btn-error btn-sm"
                disabled={!reason.trim() || previewCmd.isPending}
                onClick={handleCreatePreview}
              >
                {previewCmd.isPending
                  ? t('targets.withdraw-dialog.previewing')
                  : t('targets.withdraw-dialog.preview')}
              </button>
            </div>
          </>
        )}

        {preview && (
          <>
            <dl className="mt-4 grid grid-cols-2 gap-2 text-sm">
              <div>
                <dt className="text-xs text-base-content/60">
                  {t('targets.withdraw-dialog.affected-enrollments')}
                </dt>
                <dd>{preview.affected_enrollment_count}</dd>
              </div>
              <div>
                <dt className="text-xs text-base-content/60">
                  {t('targets.withdraw-dialog.dependent-advice-threads')}
                </dt>
                <dd>{preview.dependent_advice_thread_count}</dd>
              </div>
            </dl>

            {withdrawCmd.problem && (
              <p className="mt-2 text-sm text-error" role="alert">
                {errorMessage(t, withdrawCmd.problem)}
              </p>
            )}
            {previewCmd.problem && (
              <p className="mt-2 text-sm text-error" role="alert">
                {errorMessage(t, previewCmd.problem)}
              </p>
            )}

            <div className="modal-action mt-4">
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={handleClose}
              >
                {t('targets.withdraw-dialog.cancel')}
              </button>
              {previewExpired ? (
                <button
                  type="button"
                  className="btn btn-error btn-sm"
                  disabled={previewCmd.isPending}
                  onClick={handleRefreshPreview}
                >
                  {previewCmd.isPending
                    ? t('targets.withdraw-dialog.previewing')
                    : t('targets.withdraw-dialog.refresh-preview')}
                </button>
              ) : (
                <button
                  type="button"
                  className="btn btn-error btn-sm"
                  disabled={withdrawCmd.isPending}
                  onClick={handleConfirm}
                >
                  {withdrawCmd.isPending
                    ? t('targets.withdraw-dialog.withdrawing')
                    : t('targets.withdraw-dialog.confirm')}
                </button>
              )}
            </div>
          </>
        )}
      </div>
      <form method="dialog" className="modal-backdrop">
        <button type="button" onClick={handleClose}>
          {t('targets.withdraw-dialog.cancel')}
        </button>
      </form>
    </dialog>
  );
}

/** An active Target published before the floor check (#134) that misses
 * it. It stays in force, and its evaluations use the platform floor; the
 * Owner may publish a replacement at or above the floor. The server decides
 * which Targets these are (`below_platform_floor`). */
function BelowPlatformFloor({
  target,
  isOwner,
}: {
  target: ScaleTarget;
  isOwner: boolean;
}) {
  const { t } = useTranslation('scale');
  const params = {
    floor: target.platform_spend_floor ?? '',
    currency: target.currency,
  };
  return (
    <div className="mt-1 max-w-xs" data-testid="below-platform-floor">
      <span className="badge badge-warning badge-sm whitespace-normal">
        {t('targets.below-platform-floor.label')}
      </span>
      <p className="mt-1 text-xs text-base-content/70">
        {isOwner
          ? t('targets.below-platform-floor.republish-owner', params)
          : t('targets.below-platform-floor.republish-member', params)}
      </p>
    </div>
  );
}

/**
 * Target management (spec §10, §21, Issue #87 Tasks 2/6/7). Every action is
 * server-gated: the "New target" trigger reads `list_scale_target_contexts`
 * (never lets the caller free-type a product/offer/market/currency
 * combination), and each row's "Withdraw" button only renders when that
 * Target's own `allowed_actions` already lists `withdraw` (spec §4.4:
 * "server-driven allowed_actions ... never inferred client-side") --
 * mirroring `EnrollmentDetail`'s `AllowedActionButton` gating. "New target"
 * belongs to no aggregate, so it alone reads the Workspace role: a Member
 * sees a line saying only the Owner publishes or withdraws (#382).
 */
export function TargetManager() {
  const { t } = useTranslation('scale');
  const { current } = useWorkspace();
  const isOwner = current.role === 'OWNER';
  const { metaMode, setMetaMode } = useScaleMetaMode();
  const [publishOpen, setPublishOpen] = useState(false);
  const [withdrawTarget, setWithdrawTarget] = useState<ScaleTarget | null>(
    null
  );
  const {
    items: targets,
    hasMore,
    isLoading,
    isLoadingMore,
    problem,
    loadMore,
    refresh,
  } = useScaleTargets(metaMode);

  return (
    <>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold outline-none">
          {t('targets.title')}
        </h1>
        <div className="flex items-center gap-2">
          <MetaModeSelect value={metaMode} onChange={setMetaMode} />
          {isOwner ? (
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={() => setPublishOpen(true)}
            >
              {t('targets.new-target')}
            </button>
          ) : (
            <p className="text-sm text-base-content/60">
              {t('targets.owner-only')}
            </p>
          )}
        </div>
      </div>

      {isLoading ? (
        <div className="mt-8 flex justify-center" aria-busy="true">
          <span className="loading loading-spinner" />
        </div>
      ) : problem && targets.length === 0 ? (
        <div className="alert alert-error mt-4" role="alert">
          <span>{t('targets.error')}</span>
          <button type="button" className="btn btn-sm" onClick={refresh}>
            {t('targets.retry')}
          </button>
        </div>
      ) : targets.length === 0 ? (
        <p className="mt-6 text-sm text-base-content/60">
          {t('targets.empty')}
        </p>
      ) : (
        <div className="mt-4 overflow-x-auto">
          <table className="table table-sm">
            <thead>
              <tr>
                <th>{t('targets.column.context')}</th>
                <th>{t('targets.column.currency')}</th>
                <th>{t('targets.column.target-cpa')}</th>
                <th>{t('targets.column.breakeven-roas')}</th>
                <th>{t('targets.column.minimum-economic-spend')}</th>
                <th>{t('targets.column.lifecycle')}</th>
                <th>{t('targets.column.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {targets.map((target) => {
                const actionMap = new Map(
                  target.allowed_actions.map((a) => [a.action, a])
                );
                return (
                  <tr key={target.id}>
                    <td>
                      <div className="text-xs">
                        {target.product_version_id} / {target.offer_version_id}{' '}
                        / {target.market_code}
                      </div>
                    </td>
                    <td>{target.currency}</td>
                    <td>{target.target_cpa ?? '—'}</td>
                    <td>{target.breakeven_roas ?? '—'}</td>
                    <td>
                      <div>{target.minimum_economic_spend ?? '—'}</div>
                      {target.below_platform_floor && (
                        <BelowPlatformFloor target={target} isOwner={isOwner} />
                      )}
                    </td>
                    <td>
                      <LifecycleBadge
                        scope="target"
                        value={target.lifecycle_state}
                      />
                    </td>
                    <td>
                      <AllowedActionButton
                        action={actionMap.get('withdraw')}
                        label={t('targets.withdraw')}
                        onClick={() => setWithdrawTarget(target)}
                        variant="error"
                      />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>

          <div className="mt-3 text-center">
            {hasMore && (
              <button
                type="button"
                className="btn btn-sm btn-ghost w-full"
                onClick={loadMore}
                disabled={isLoadingMore}
              >
                {isLoadingMore ? t('targets.loading') : t('targets.load-more')}
              </button>
            )}
          </div>
        </div>
      )}

      <PublishTargetDialog
        open={publishOpen}
        metaMode={metaMode}
        onClose={() => setPublishOpen(false)}
        onPublished={() => {
          setPublishOpen(false);
          void refresh();
        }}
      />

      {withdrawTarget && (
        <WithdrawTargetDialog
          target={withdrawTarget}
          metaMode={metaMode}
          onClose={() => setWithdrawTarget(null)}
          onWithdrawn={() => {
            setWithdrawTarget(null);
            void refresh();
          }}
        />
      )}
    </>
  );
}
