import { useTranslation } from 'next-i18next';
import { useCallback, useState } from 'react';
import { Card } from '@/components/shared';
import { newActionId } from '@/components/generation/shared/newActionId';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import { formatDateTime } from '@/lib/i18n/format';
import { errorMessage, type DomainProblem } from '@/lib/i18n/domain-errors';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import {
  LIMIT_FIELDS,
  changeKind,
  draftFrom,
  formatLimit,
  isLimitValue,
  isSpendGuardrail,
  isSpendGuardrailPreview,
  refusedField,
  type GuardrailLimits,
  type LimitField,
  type SpendGuardrail,
  type SpendGuardrailPreview,
} from '@/lib/entitlements/spend-guardrail';
import { useWorkspace } from './WorkspaceProvider';

/**
 * Settings → Spending limits: the Workspace SpendGuardrail (#126).
 *
 * Every member reads the four limits; only the Owner changes them
 * (subscription-entitlement contract §12). A change always goes through the
 * Domain's preview and confirm, which records it and its audit event. Lowering
 * needs nothing more: saving previews and confirms in one step. Raising any
 * limit lets more be spent, so the Owner reviews the server's preview and
 * confirms it a second time (§11). The server refuses a limit above the
 * platform maximum with `spend_guardrail_limit_above_platform`, naming the
 * field and the maximum; that limit's input shows it. MFA is checked only at
 * login (ADR 0015), so nothing here asks for a code.
 */

/** Failure banners: `settings:spend-guardrail.<key>`. */
type FailureMessage = 'read-unavailable' | 'save-refused' | 'save-unresolved';

interface Failure {
  message: FailureMessage;
  problem?: ProblemDetails;
}

/** Refusals that mean the limits moved on: read them again. */
const STALE_CODES = new Set([
  'revision_conflict',
  'preview_expired',
  'preview_already_consumed',
  'spend_guardrail_preview_missing',
]);

const ABOVE_PLATFORM = 'spend_guardrail_limit_above_platform';

function GuardrailProblem({
  failure,
  testId,
}: {
  failure: Failure;
  testId: string;
}) {
  const { t } = useTranslation('settings');
  const reference = failure.problem
    ? [
        failure.problem.code,
        failure.problem.trace_id &&
          t('trace', { id: failure.problem.trace_id }),
      ]
        .filter(Boolean)
        .join(' · ')
    : '';
  return (
    <div
      className="alert alert-error items-start break-words text-sm"
      role="alert"
      data-testid={testId}
    >
      <div>
        <div className="font-semibold">
          {t(`spend-guardrail.${failure.message}`)}
        </div>
        {failure.problem && (
          <div className="mt-1 text-xs">{errorMessage(t, failure.problem)}</div>
        )}
        {reference && (
          <div className="mt-1 break-all text-xs opacity-75">{reference}</div>
        )}
      </div>
    </div>
  );
}

export function SpendGuardrailSection() {
  const { t, i18n } = useTranslation('settings');
  const { current } = useWorkspace();
  const isOwner = current.role === 'OWNER';
  const { data, problem, isLoading, refresh } = useDomainQuery(
    'get_spend_guardrail'
  );
  // The generated contract types the body `null`: read it as data.
  const body: unknown = data;
  const guardrail = isSpendGuardrail(body) ? body : null;
  const [editing, setEditing] = useState(false);
  const [saved, setSaved] = useState(false);

  const onSaved = useCallback(async () => {
    setEditing(false);
    setSaved(true);
    await refresh();
  }, [refresh]);

  return (
    <div data-testid="spend-guardrail-section">
      <Card>
        <Card.Body>
          <Card.Header>
            <Card.Title>{t('spend-guardrail.title')}</Card.Title>
            <Card.Description>
              {t('spend-guardrail.description')}
            </Card.Description>
          </Card.Header>

          {isLoading && !guardrail && (
            <p className="text-sm text-gray-500">
              {t('spend-guardrail.loading')}
            </p>
          )}

          {!isLoading && !guardrail && (
            <div className="flex flex-col items-start gap-2">
              <GuardrailProblem
                failure={{ message: 'read-unavailable', problem }}
                testId="spend-guardrail-read-problem"
              />
              <button
                type="button"
                className="btn btn-outline btn-sm"
                onClick={() => void refresh()}
              >
                {t('spend-guardrail.retry')}
              </button>
            </div>
          )}

          {guardrail && !editing && (
            <div className="flex flex-col gap-3">
              <dl
                className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-4 gap-y-2 text-sm"
                data-testid="spend-guardrail-limits"
              >
                {LIMIT_FIELDS.map((field) => (
                  <LimitRow
                    key={field}
                    label={t(`spend-guardrail.field-${field}`)}
                    value={formatLimit(
                      i18n.language,
                      guardrail[field],
                      guardrail.budget_currency
                    )}
                    testId={`spend-guardrail-value-${field}`}
                  />
                ))}
              </dl>
              {saved && (
                <p
                  className="text-sm text-success"
                  role="status"
                  data-testid="spend-guardrail-saved"
                >
                  {t('spend-guardrail.saved')}
                </p>
              )}
              {isOwner ? (
                <div>
                  <button
                    type="button"
                    className="btn btn-outline btn-sm"
                    data-testid="spend-guardrail-edit"
                    onClick={() => {
                      setSaved(false);
                      setEditing(true);
                    }}
                  >
                    {t('spend-guardrail.edit')}
                  </button>
                </div>
              ) : (
                <p
                  className="text-sm text-gray-500"
                  data-testid="spend-guardrail-owner-only"
                >
                  {t('spend-guardrail.owner-only')}
                </p>
              )}
            </div>
          )}

          {guardrail && editing && isOwner && (
            <GuardrailEditor
              guardrail={guardrail}
              refresh={refresh}
              onCancel={() => setEditing(false)}
              onSaved={onSaved}
            />
          )}
        </Card.Body>
      </Card>
    </div>
  );
}

function LimitRow({
  label,
  value,
  testId,
}: {
  label: string;
  value: string;
  testId: string;
}) {
  return (
    <>
      <dt className="text-gray-500">{label}</dt>
      <dd className="text-right font-medium tabular-nums" data-testid={testId}>
        {value}
      </dd>
    </>
  );
}

interface FieldRefusal {
  field: LimitField;
  limit: string | null;
}

function GuardrailEditor({
  guardrail,
  refresh,
  onCancel,
  onSaved,
}: {
  guardrail: SpendGuardrail;
  refresh: () => Promise<void>;
  onCancel: () => void;
  onSaved: () => Promise<void>;
}) {
  const { t, i18n } = useTranslation('settings');
  const previewCommand = useDomainCommand('preview_spend_guardrail');
  const confirmCommand = useDomainCommand('confirm_spend_guardrail');
  const [draft, setDraft] = useState<GuardrailLimits>(() =>
    draftFrom(guardrail)
  );
  const [raise, setRaise] = useState<SpendGuardrailPreview | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [refusal, setRefusal] = useState<FieldRefusal | null>(null);
  const [busy, setBusy] = useState(false);
  const kind = changeKind(guardrail, draft);
  const currency = guardrail.budget_currency;

  /** A refused command: the banner, the refused input, and a re-read when the limits moved on. */
  const refused = useCallback(
    async (problem: ProblemDetails) => {
      setFailure({ message: 'save-refused', problem });
      if (problem.code === ABOVE_PLATFORM) {
        const params = (problem as DomainProblem).params;
        const field = refusedField(params);
        const limit = params?.limit;
        if (field) {
          setRefusal({
            field,
            limit: typeof limit === 'string' ? limit : null,
          });
        }
      }
      if (STALE_CODES.has(problem.code)) {
        setRaise(null);
        await refresh();
      }
    },
    [refresh]
  );

  const unresolved = useCallback(async () => {
    setRaise(null);
    setFailure({ message: 'save-unresolved' });
    await refresh();
  }, [refresh]);

  const confirm = useCallback(
    async (preview: SpendGuardrailPreview) => {
      const outcome = await confirmCommand
        .execute({
          actionInstanceId: newActionId(),
          body: {
            preview_token: preview.preview_token,
            expected_revision: preview.current_revision,
          },
        })
        .catch(() => null);
      if (outcome === null || outcome.kind === 'reconciliation') {
        await unresolved();
        return;
      }
      if (outcome.kind === 'problem') {
        await refused(outcome.problem);
        return;
      }
      await onSaved();
    },
    [confirmCommand, onSaved, refused, unresolved]
  );

  const save = useCallback(async () => {
    if (kind !== 'lower' && kind !== 'raise') return;
    setFailure(null);
    setRefusal(null);
    setBusy(true);
    try {
      const outcome = await previewCommand
        .execute({
          actionInstanceId: newActionId(),
          body: {
            per_operation_limit: draft.per_operation_limit.trim(),
            rolling_24h_limit: draft.rolling_24h_limit.trim(),
            period_limit: draft.period_limit.trim(),
            outstanding_exposure_limit: draft.outstanding_exposure_limit.trim(),
            expected_revision: guardrail.revision,
          },
        })
        .catch(() => null);
      if (outcome === null || outcome.kind === 'reconciliation') {
        await unresolved();
        return;
      }
      if (outcome.kind === 'problem') {
        await refused(outcome.problem);
        return;
      }
      const previewed: unknown = outcome.data;
      if (!isSpendGuardrailPreview(previewed)) {
        await unresolved();
        return;
      }
      // The server's preview decides: limits it reads as raised wait for
      // the Owner's second confirmation, anything else confirms now.
      const preview = previewed;
      if (changeKind(preview.old_limits, preview.proposed_limits) === 'raise') {
        setRaise(preview);
        return;
      }
      await confirm(preview);
    } finally {
      setBusy(false);
    }
  }, [
    kind,
    previewCommand,
    draft,
    guardrail.revision,
    refused,
    unresolved,
    confirm,
  ]);

  const confirmRaise = useCallback(async () => {
    if (!raise) return;
    setFailure(null);
    setBusy(true);
    try {
      await confirm(raise);
    } finally {
      setBusy(false);
    }
  }, [raise, confirm]);

  if (raise) {
    return (
      <div
        className="flex flex-col gap-3"
        data-testid="spend-guardrail-raise-review"
      >
        <div>
          <p className="font-medium">{t('spend-guardrail.raise-title')}</p>
          <p className="text-sm text-gray-500">
            {t('spend-guardrail.raise-description')}
          </p>
        </div>
        <table className="table table-sm">
          <thead>
            <tr>
              <th>{t('spend-guardrail.column-limit')}</th>
              <th className="text-right">
                {t('spend-guardrail.column-current')}
              </th>
              <th className="text-right">{t('spend-guardrail.column-new')}</th>
            </tr>
          </thead>
          <tbody>
            {LIMIT_FIELDS.map((field) => (
              <tr key={field} data-testid={`spend-guardrail-change-${field}`}>
                <td>{t(`spend-guardrail.field-${field}`)}</td>
                <td className="text-right tabular-nums">
                  {formatLimit(
                    i18n.language,
                    raise.old_limits[field],
                    currency
                  )}
                </td>
                <td className="text-right font-medium tabular-nums">
                  {formatLimit(
                    i18n.language,
                    raise.proposed_limits[field],
                    currency
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {raise.expires_at && (
          <p className="text-xs text-gray-500">
            {t('spend-guardrail.raise-expires', {
              time: formatDateTime(i18n.language, raise.expires_at),
            })}
          </p>
        )}
        {failure && (
          <GuardrailProblem
            failure={failure}
            testId="spend-guardrail-problem"
          />
        )}
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            className="btn btn-primary btn-sm"
            data-testid="spend-guardrail-confirm-raise"
            disabled={busy}
            onClick={() => void confirmRaise()}
          >
            {busy
              ? t('spend-guardrail.saving')
              : t('spend-guardrail.confirm-raise')}
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={busy}
            onClick={() => {
              setFailure(null);
              setRaise(null);
            }}
          >
            {t('spend-guardrail.back')}
          </button>
        </div>
      </div>
    );
  }

  return (
    <form
      className="flex flex-col gap-3"
      data-testid="spend-guardrail-form"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      {LIMIT_FIELDS.map((field) => {
        const id = `spend-guardrail-${field}`;
        const invalid = !isLimitValue(draft[field]);
        const platformLimit =
          refusal?.field === field
            ? t('spend-guardrail.above-platform', {
                limit: refusal.limit
                  ? formatLimit(i18n.language, refusal.limit, currency)
                  : '',
              })
            : null;
        const hint = invalid
          ? t('spend-guardrail.invalid-value')
          : platformLimit;
        return (
          <div key={field} className="flex flex-col gap-1">
            <label className="text-sm font-medium" htmlFor={id}>
              {t('spend-guardrail.input-label', {
                limit: t(`spend-guardrail.field-${field}`),
                currency,
              })}
            </label>
            <input
              id={id}
              name={field}
              inputMode="decimal"
              autoComplete="off"
              className="input input-bordered input-sm w-full max-w-xs"
              value={draft[field]}
              aria-invalid={invalid || platformLimit !== null}
              aria-describedby={hint ? `${id}-hint` : undefined}
              onChange={(event) => {
                const value = event.target.value;
                setDraft((previous) => ({ ...previous, [field]: value }));
                if (refusal?.field === field) setRefusal(null);
              }}
            />
            {hint && (
              <p
                id={`${id}-hint`}
                className="text-xs text-error"
                data-testid={`${id}-hint`}
              >
                {hint}
              </p>
            )}
          </div>
        );
      })}

      {(kind === 'lower' || kind === 'raise') && (
        <p className="text-sm text-gray-500" data-testid="spend-guardrail-kind">
          {t(`spend-guardrail.kind-${kind}`)}
        </p>
      )}
      {failure && (
        <GuardrailProblem failure={failure} testId="spend-guardrail-problem" />
      )}
      <div className="flex flex-wrap gap-2">
        <button
          type="submit"
          className="btn btn-primary btn-sm"
          data-testid="spend-guardrail-save"
          disabled={busy || (kind !== 'lower' && kind !== 'raise')}
        >
          {busy
            ? t('spend-guardrail.saving')
            : kind === 'raise'
              ? t('spend-guardrail.review-raise')
              : t('spend-guardrail.save')}
        </button>
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          disabled={busy}
          onClick={onCancel}
        >
          {t('spend-guardrail.cancel')}
        </button>
      </div>
    </form>
  );
}
