import {
  DomainProblemError,
  errorCodeMessage,
  type DomainProblem,
} from '@/lib/i18n/domain-errors';
import { useEffect, useId, useState } from 'react';
import { useTranslation } from 'next-i18next';
import useSWR from 'swr';
import { MarketingCallRecovery } from './MarketingCallRecovery';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { useBlobTransferClient } from '@/lib/domain-client/upload-deps';
import type { DownloadGrant } from '@/lib/domain-client/blob-transfer';
import { useWorkspace } from './WorkspaceProvider';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import { strategyLanguageFor } from '@/lib/i18n/locales';
import type { MarketingRound } from './MarketingExecution';
import { useEmphasis } from './marketing-focus';

export type AIQCCall = components['schemas']['WorkflowAIQCCall'];
export type MarketingQCAsset = components['schemas']['WorkflowAsset'];
type QCKind = 'single' | 'paired';
type Quote = components['schemas']['AIQCQuote'];
type Finding = NonNullable<AIQCCall['result']>['findings'][number];
type CopyDifference = NonNullable<Finding['rendered_text_diff']>[number];
type ErrorDetail = NonNullable<AIQCCall['error_detail']>;
const KINDS: QCKind[] = ['single', 'paired'];
// The standard checks: `CHECKS` in app/marketing/workflow.py plus the server's
// own `file_valid`. __tests__/locales/qc-check-ids.spec.ts keeps them equal.
export const STANDARD_CHECKS: ReadonlySet<string> = new Set([
  'product_fidelity',
  'facts_supported',
  'proposition_clear',
  'expression_realized',
  'comparison_faithful',
  'copy_consistent',
  'language_correct',
  'brand_style',
  'text_readable',
  'belongs_to_intended_combination',
  'expressions_distinct',
  'file_valid',
]);
const INPUT =
  'w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm';
const BUTTON =
  'rounded-lg bg-gray-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40';
const SECONDARY =
  'rounded-lg border border-gray-300 px-4 py-2 text-sm disabled:opacity-40';
function valueOf(result: unknown): unknown {
  const r = result as {
    kind?: string;
    data?: unknown;
    problem?: DomainProblem;
  };
  if (r?.kind === 'problem') throw new DomainProblemError(r.problem);
  if (!r || r.data === undefined)
    throw new Error('marketing-action-needs-reconciliation');
  return r.data;
}
// A call from before the split (`combined`) answers both kinds, so both point at it.
function callOf(asset: MarketingQCAsset, kind: QCKind) {
  return (kind === 'single' ? asset.ai_qc : asset.paired_ai_qc) || null;
}
function currentCall(asset: MarketingQCAsset, kind: QCKind) {
  const call = callOf(asset, kind);
  return call?.state === 'succeeded' && call.current && call.result
    ? call
    : null;
}
function uniqueCalls(calls: (AIQCCall | null)[]) {
  return calls.filter(
    (call, i): call is AIQCCall =>
      !!call && calls.findIndex((c) => c?.id === call.id) === i
  );
}
// Why a check failed, worded from its code: the refused output's reason when
// the server kept one (#295), else the call's own error code.
function failureCode(call: AIQCCall) {
  return call.error_detail?.reason || call.error_code;
}
// The model's summary is its own words; it reads as the verdict only when every
// finding passed (#294). Otherwise the server's counts stand in for it.
function verdictOf(call: AIQCCall) {
  return call.result!.quality_suggestion === 'ready_to_confirm';
}
function CopyDiff({ diff }: { diff: CopyDifference[] }) {
  const { t } = useTranslation('common');
  return (
    <dl className="space-y-2">
      {diff.map((d) => (
        <div key={d.field} data-copy-field={d.field} className="space-y-0.5">
          <dt className="font-medium">
            {t(`marketing-ai-qc-copy-field-${d.field}`)}
          </dt>
          <dd>{t('marketing-ai-qc-copy-confirmed', { text: d.expected })}</dd>
          <dd>
            {d.rendered
              ? t('marketing-ai-qc-copy-rendered', { text: d.rendered })
              : t('marketing-ai-qc-copy-not-read')}
          </dd>
          {!!d.changes?.length && (
            <dd>
              <ul className="list-disc pl-5 text-red-700">
                {d.changes.map((c, i) => (
                  <li key={i}>
                    {t(
                      c.rendered && c.expected
                        ? 'marketing-ai-qc-copy-change'
                        : c.expected
                          ? 'marketing-ai-qc-copy-missing-word'
                          : 'marketing-ai-qc-copy-extra-word',
                      { expected: c.expected, rendered: c.rendered }
                    )}
                  </li>
                ))}
              </ul>
            </dd>
          )}
        </div>
      ))}
    </dl>
  );
}
function FailureDetail({ detail }: { detail: ErrorDetail }) {
  const { t } = useTranslation('common');
  return (
    <details className="text-xs text-gray-600">
      <summary className="cursor-pointer">
        {t('marketing-ai-qc-failure-output')}
      </summary>
      <div className="mt-2 space-y-1">
        {detail.field && (
          <p>
            {t('marketing-ai-qc-failure-field', { field: detail.field })}
            {detail.problem &&
              ` · ${t('marketing-ai-qc-failure-problem', {
                problem: detail.problem,
              })}`}
          </p>
        )}
        <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-all rounded bg-gray-100 p-2">
          {detail.excerpt}
        </pre>
        <p className="break-all">
          {t('marketing-ai-qc-failure-excerpt-note', {
            shown: detail.excerpt.length,
            length: detail.length,
            sha256: detail.sha256,
          })}
        </p>
      </div>
    </details>
  );
}
// A combination outside the frozen plan is named generically, never by id (J-32).
function combinationName(row: MarketingRound, id: string, unnamed: string) {
  const combo = row.snapshot.plan.combinations.find((c) => c.id === id);
  const angle = row.snapshot.plan.angles.find((a) => a.id === combo?.angle_id);
  const expression = angle?.expressions.find(
    (e) => e.id === combo?.expression_id
  );
  return angle && expression
    ? `${angle.proposition} · ${expression.name}`
    : unnamed;
}
function Field({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  const id = useId();
  return (
    <label htmlFor={id} className="block space-y-1 text-sm">
      <span>{label}</span>
      <textarea
        id={id}
        className={INPUT}
        rows={2}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  );
}

function QCKindSection({
  row,
  kind,
  busy,
  onRun,
  onResolved,
}: {
  row: MarketingRound;
  kind: QCKind;
  busy: boolean;
  onRun: (action: (id: string) => Promise<unknown>) => Promise<void>;
  onResolved?: () => void;
}) {
  const { t, i18n } = useTranslation('common');
  const emphasis = useEmphasis();
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  // Findings are written in the plan's strategy language; a plan without one uses the interface's (J-43).
  const language = strategyLanguageFor(i18n?.language);
  const calls = row.assets.map((a) => callOf(a, kind));
  // Nothing is generated yet: there is nothing to quote, check or approve (#300).
  const empty = row.assets.length === 0;
  const stateKey = row.assets
    .map(
      (a, i) =>
        `${a.asset_version_id}:${calls[i]?.id}:${calls[i]?.state}:${a.ready}`
    )
    .join('|');
  // Each kind has its own quote, reservation and settlement.
  const quote = useSWR<Quote>(
    empty
      ? null
      : [
          'marketing-ai-qc-quote',
          kind,
          language,
          current.workspaceId,
          row.id,
          row.revision,
          stateKey,
        ],
    async () =>
      valueOf(
        await gateway.getMarketingAIQCQuote({
          workspaceSlug: current.slug,
          roundId: row.id,
          kind,
          strategyLanguage: language,
        })
      ) as Quote
  );
  const pending = calls.filter(
    (c) => c && ['queued', 'running'].includes(c.state)
  ).length;
  const uncertain = uniqueCalls(calls).filter((c) => c.state === 'uncertain');
  // Failures the next check would retry: not an approved image or one sent back for changes.
  const failed = uniqueCalls(
    row.assets
      .filter(
        (a) =>
          !a.ready &&
          !['needs_changes', 'rejected'].includes(a.quality?.quality || '')
      )
      .map((a) => callOf(a, kind))
  ).filter((c) => c.state === 'failed');
  const waiting = quote.data?.waiting || [];
  return (
    <section className="space-y-2 rounded-lg border border-gray-200 bg-white p-3">
      <div className="space-y-1">
        <h5 className="text-sm font-semibold">
          {t(`marketing-ai-qc-kind-${kind}`)}
        </h5>
        <p className="text-xs text-gray-600">
          {t(`marketing-ai-qc-kind-${kind}-description`)}
        </p>
      </div>
      {empty && (
        <p className="text-sm text-gray-600" role="status">
          {t('marketing-ai-qc-no-images')}
        </p>
      )}
      {quote.error && (
        <p role="alert">{t('marketing-ai-qc-quote-load-failed')}</p>
      )}
      {failed.length > 0 && (
        <div role="alert" className="text-sm text-amber-800">
          <p>
            {t('marketing-ai-qc-failed-before-retry', {
              count: failed.length,
            })}
          </p>
          <ul className="list-disc pl-5">
            {Array.from(
              new Set(failed.map((c) => errorCodeMessage(t, failureCode(c))))
            ).map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </div>
      )}
      {uncertain.map((call) => (
        <MarketingCallRecovery
          key={call.id}
          call={call}
          kind="ai_qc"
          onResolved={() => {
            void quote.mutate();
            onResolved?.();
          }}
        />
      ))}
      {quote.data?.available && (
        <>
          <p className="text-sm">
            {t('marketing-ai-qc-cost', {
              count: quote.data.quantity,
              amount: quote.data.maximum_cost,
              provider: quote.data.provider,
              model: quote.data.model,
            })}
          </p>
          <button
            className={emphasis(
              // Before #229 one `ai_qc` step ran the single-image check.
              kind === 'single' ? ['ai_qc', 'single_qc'] : 'paired_qc',
              BUTTON,
              SECONDARY
            )}
            disabled={busy}
            onClick={() =>
              void onRun((actionInstanceId) =>
                gateway.startMarketingAIQC({
                  workspaceSlug: current.slug,
                  roundId: row.id,
                  expectedRevision: row.revision,
                  actionInstanceId,
                  body: {
                    quote_hash: quote.data!.quote_hash,
                    kind,
                    strategy_language: language,
                  },
                })
              )
            }
          >
            {row.execution_mode === 'mock'
              ? t(`marketing-ai-qc-${kind}-run-simulation`, {
                  count: quote.data.quantity,
                })
              : t(`marketing-ai-qc-${kind}-start`, {
                  count: quote.data.quantity,
                  amount: quote.data.maximum_cost,
                })}
          </button>
        </>
      )}
      {quote.data && !quote.data.available && (
        <p className="text-sm text-gray-600">
          {t(`marketing-ai-qc-unavailable-${quote.data.unavailable_reason}`)}
        </p>
      )}
      {waiting.length > 0 && (
        <p className="text-sm text-gray-600" role="status">
          {t('marketing-ai-qc-paired-waiting', {
            count: waiting.length,
            names: Array.from(
              new Set(waiting.flatMap((w) => w.awaiting_combination_ids))
            )
              .map((id) =>
                combinationName(
                  row,
                  id,
                  t('marketing-ai-qc-unnamed-combination')
                )
              )
              .join(t('list-separator')),
          })}
        </p>
      )}
      {pending > 0 && (
        <p role="status" className="text-sm">
          {t('marketing-ai-qc-pending', { count: pending })}
        </p>
      )}
      {uncertain.length > 0 && (
        <p role="alert" className="text-sm">
          {t('marketing-ai-qc-uncertain', { count: uncertain.length })}
        </p>
      )}
    </section>
  );
}

export function MarketingAIQCPanel({
  row,
  busy,
  onRun,
  onResolved,
  only,
}: {
  row: MarketingRound;
  busy: boolean;
  onRun: (action: (id: string) => Promise<unknown>) => Promise<void>;
  onResolved?: () => void;
  // A board drawer shows only the check its image needs next.
  only?: QCKind;
}) {
  const { t } = useTranslation('common');
  const emphasis = useEmphasis();
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const [appearanceAck, setAppearanceAck] = useState('');
  // Bulk approval needs both a current single-image and a current paired
  // result per image: a single-image pass never stands in for the comparison.
  const eligible = row.assets.filter(
    (a) =>
      !a.ready &&
      !['needs_changes', 'rejected'].includes(a.quality?.quality || '') &&
      KINDS.every((kind) =>
        currentCall(a, kind)?.result!.findings.every(
          (f) =>
            f.status === 'pass' ||
            (f.check_id === 'product_fidelity' && f.status === 'unknown')
        )
      )
  );
  const appearance = eligible.filter((a) =>
    currentCall(a, 'single')!.result!.findings.some(
      (f) => f.check_id === 'product_fidelity' && f.status === 'unknown'
    )
  );
  const appearanceKey = appearance
    .map((a) => a.ai_qc!.id)
    .sort()
    .join('|');
  const appearanceConfirmed =
    !!appearanceKey && appearanceAck === appearanceKey;
  const confirmable = eligible.filter(
    (a) => appearanceConfirmed || !appearance.includes(a)
  );
  const kinds = only ? [only] : KINDS;
  return (
    <div className="space-y-4 rounded-lg border border-gray-200 bg-gray-50 p-4">
      <div className="space-y-1">
        <h4 className="font-semibold">{t('marketing-ai-qc-title')}</h4>
        <p className="text-sm text-gray-600">
          {t('marketing-ai-qc-description')}
        </p>
      </div>
      {kinds.map((kind) => (
        <QCKindSection
          key={kind}
          row={row}
          kind={kind}
          busy={busy}
          onRun={onRun}
          onResolved={onResolved}
        />
      ))}
      {!only && appearance.length > 0 && (
        <label className="flex items-start gap-2 text-sm">
          <input
            className="mt-1"
            type="checkbox"
            checked={appearanceConfirmed}
            onChange={(e) =>
              setAppearanceAck(e.target.checked ? appearanceKey : '')
            }
          />
          <span>
            {t('marketing-ai-qc-confirm-appearance', {
              count: appearance.length,
            })}
          </span>
        </label>
      )}
      {!only && eligible.length > 0 && (
        <button
          className={emphasis('review', BUTTON, SECONDARY)}
          disabled={busy || confirmable.length === 0}
          onClick={() =>
            void onRun((actionInstanceId) =>
              gateway.confirmMarketingAIQC({
                workspaceSlug: current.slug,
                roundId: row.id,
                expectedRevision: row.revision,
                actionInstanceId,
                body: {
                  ai_qc_ids: confirmable.flatMap((a) =>
                    uniqueCalls(KINDS.map((kind) => currentCall(a, kind))).map(
                      (c) => c.id
                    )
                  ),
                  resolved_checks:
                    appearanceConfirmed && appearance.length
                      ? { product_fidelity: 'pass' }
                      : {},
                  note: appearanceConfirmed
                    ? t('marketing-ai-qc-appearance-note')
                    : '',
                },
              })
            )
          }
        >
          {t('marketing-ai-qc-confirm-batch', { count: confirmable.length })}
        </button>
      )}
      <p className="text-xs text-gray-500">
        {t('marketing-ai-qc-quality-only')}
      </p>
    </div>
  );
}

export function MarketingCreativeReview({
  asset,
  row,
  busy,
  onReview,
}: {
  asset: MarketingQCAsset;
  row: MarketingRound;
  busy: boolean;
  onReview: (body: components['schemas']['ReviewCreative']) => Promise<void>;
}) {
  const { t, i18n } = useTranslation('common');
  const interfaceLanguage = strategyLanguageFor(i18n?.language);
  const emphasis = useEmphasis();
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const singleCall = currentCall(asset, 'single');
  const pairedCall = currentCall(asset, 'paired');
  const reports = uniqueCalls([singleCall, pairedCall]);
  const ai = reports.length > 0;
  const findings = reports.flatMap((c) => c.result!.findings);
  const proposed: Record<string, string> = Object.fromEntries(
    findings.map((f) => [f.check_id, f.status])
  );
  // Approving with only one kind's current result is a human override of the other.
  const missingKind = ai && !(singleCall && pairedCall);
  const [checks, setChecks] = useState<
    Record<string, 'pass' | 'fail' | 'unknown'>
  >(
    () =>
      Object.fromEntries(
        Object.keys(asset.required_checks).map((key) => [
          key,
          asset.quality?.checks[key] || proposed[key] || 'unknown',
        ])
      ) as Record<string, 'pass' | 'fail' | 'unknown'>
  );
  const [note, setNote] = useState(
    asset.quality?.note ||
      (reports.length > 0 && reports.every(verdictOf)
        ? reports[0].result!.summary
        : '')
  );
  const [overrideNote, setOverrideNote] = useState(
    asset.quality?.override_note || ''
  );
  const [peers, setPeers] = useState<string[]>(
    pairedCall?.compared_asset_version_ids || []
  );
  const [loadImage, setLoadImage] = useState(false);
  const [imageFailed, setImageFailed] = useState(false);
  const [imageUrl, setImageUrl] = useState<string | null>(null);
  const blobClient = useBlobTransferClient();
  const image = useSWR<{ download_grant: DownloadGrant }>(
    ai || asset.ready || loadImage
      ? ['marketing-review-image', current.workspaceId, asset.asset_version_id]
      : null,
    async () =>
      valueOf(
        await gateway.createGenerationAssetVersionDownloadIntent({
          workspaceSlug: current.slug,
          versionId: asset.asset_version_id,
          actionInstanceId: crypto.randomUUID(),
        })
      ) as { download_grant: DownloadGrant },
    { revalidateOnFocus: false }
  );
  useEffect(() => {
    setImageUrl(null);
    if (!image.data) return;
    const controller = new AbortController();
    let localUrl: string | null = null;
    blobClient
      .downloadBlob(image.data.download_grant, { signal: controller.signal })
      .then((blob) => {
        if (controller.signal.aborted) return;
        localUrl = URL.createObjectURL(blob);
        setImageFailed(false);
        setImageUrl(localUrl);
      })
      .catch(() => {
        if (!controller.signal.aborted) setImageFailed(true);
      });
    return () => {
      controller.abort();
      if (localUrl) URL.revokeObjectURL(localUrl);
    };
  }, [image.data, blobClient]);
  const overridden =
    ai &&
    Object.keys(checks).some(
      (key) => key in proposed && checks[key] !== proposed[key]
    );
  const combo = row.snapshot.plan.combinations.find(
    (c) => c.id === asset.combination_id
  );
  const angle = row.snapshot.plan.angles.find((a) => a.id === combo?.angle_id);
  const expression = angle?.expressions.find(
    (e) => e.id === combo?.expression_id
  );
  const group = row.snapshot.plan.groups.find((g) =>
    g.combination_ids.includes(asset.combination_id)
  );
  // Checks without a current AI answer (a kind not run yet) need a human decision too.
  const flagged = Object.keys(asset.required_checks).filter(
    (key) => proposed[key] !== 'pass'
  );
  const allPass = Object.values(checks).every((value) => value === 'pass');
  const actions = (
    <div className="flex flex-wrap gap-2">
      {(['approved', 'needs_changes', 'rejected'] as const).map((quality) => (
        <button
          key={quality}
          className={
            quality === 'approved'
              ? emphasis('review', BUTTON, SECONDARY)
              : SECONDARY
          }
          disabled={
            busy ||
            !note.trim() ||
            (quality === 'approved' && !allPass) ||
            ((overridden || (quality === 'approved' && missingKind)) &&
              !overrideNote.trim())
          }
          onClick={() =>
            void onReview({
              asset_version_id: asset.asset_version_id,
              quality,
              checks,
              note,
              compared_asset_version_ids: peers,
              ai_qc_id: singleCall?.id || null,
              paired_ai_qc_id:
                pairedCall && pairedCall.id !== singleCall?.id
                  ? pairedCall.id
                  : null,
              override_note: overrideNote,
            })
          }
        >
          {t(
            ai
              ? `marketing-ai-review-${quality}`
              : `marketing-review-${quality}`
          )}
        </button>
      ))}
    </div>
  );
  // A standard check is named in the interface language by its id; its
  // `required_checks` text is the English criterion the AI judged against,
  // kept as evidence (#308). `intent_*` and any other id show the round's own
  // words, or the id when the round has none.
  const label = (key: string) =>
    STANDARD_CHECKS.has(key)
      ? t(`marketing-ai-qc-check-${key}`)
      : (asset.required_checks[key] ?? key);
  function flag(finding: Finding) {
    return (
      <div
        key={finding.check_id}
        className="space-y-1 border-l-2 border-amber-300 pl-3"
      >
        <p className="font-medium">{label(finding.check_id)}</p>
        <p>
          {finding.gate_reason
            ? t(`marketing-ai-qc-gate-${finding.gate_reason}`)
            : finding.rationale}
        </p>
        {finding.rendered_text_diff?.length ? (
          <CopyDiff diff={finding.rendered_text_diff} />
        ) : (
          finding.suggested_fix && (
            <p>{t('marketing-ai-qc-fix', { fix: finding.suggested_fix })}</p>
          )
        )}
      </div>
    );
  }
  // One section per check kind: doubts stay open, passing items are folded.
  function kindReport(kind: QCKind | 'combined', call: AIQCCall | null) {
    const latest =
      kind === 'combined' ? asset.ai_qc : callOf(asset, kind as QCKind);
    const doubts = (call?.result?.findings || []).filter(
      (f) => f.status !== 'pass'
    );
    const passing = (call?.result?.findings || []).filter(
      (f) => f.status === 'pass'
    );
    return (
      <section
        key={kind}
        aria-label={t(`marketing-ai-qc-kind-${kind}`)}
        className="space-y-2 rounded-lg border border-gray-200 p-3"
      >
        <h5 className="font-medium">{t(`marketing-ai-qc-kind-${kind}`)}</h5>
        {!call && (
          <p className="text-gray-600" role="status">
            {latest && latest.state !== 'succeeded'
              ? t(`marketing-ai-qc-state-${latest.state}`)
              : latest
                ? t('marketing-ai-qc-stale')
                : t(`marketing-ai-qc-${kind}-missing`)}
          </p>
        )}
        {!call && latest?.state === 'failed' && (
          <>
            <p className="text-amber-800">
              {errorCodeMessage(t, failureCode(latest))}
            </p>
            {latest.error_detail && (
              <FailureDetail detail={latest.error_detail} />
            )}
          </>
        )}
        {call && (
          <>
            {/* Evidence is shown as recorded, never translated (J-43). */}
            {call.language && call.language !== interfaceLanguage && (
              <p role="note" className="text-xs text-gray-500">
                {t(`marketing-ai-output-language-${call.language}`)}
              </p>
            )}
            <p>
              {verdictOf(call)
                ? call.result!.summary
                : t('marketing-ai-qc-verdict', {
                    failed: call.result!.findings.filter(
                      (f) => f.status === 'fail'
                    ).length,
                    unknown: call.result!.findings.filter(
                      (f) => f.status === 'unknown'
                    ).length,
                    passed: passing.length,
                  })}
            </p>
            {kind !== 'paired' &&
              !!call.product_reference_version_ids?.length && (
                <p>
                  {t('marketing-qc-product-photo-count', {
                    count: call.product_reference_version_ids.length,
                  })}
                </p>
              )}
            {kind !== 'single' && (
              <p className="text-gray-600">
                {t('marketing-ai-qc-compared-count', {
                  count: call.compared_asset_version_ids.length,
                })}
              </p>
            )}
            {!asset.ready && doubts.map(flag)}
            {passing.length > 0 && (
              <details>
                <summary className="cursor-pointer">
                  {t('marketing-ai-qc-passing-count', {
                    count: passing.length,
                  })}
                </summary>
                <ul className="mt-2 space-y-1 text-gray-600">
                  {passing.map((f) => (
                    <li key={f.check_id}>
                      <span className="font-medium text-gray-900">
                        {label(f.check_id)}
                      </span>
                      : {f.visual_evidence}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </>
        )}
      </section>
    );
  }
  function fields(keys: string[]) {
    return keys.map((key) => {
      const finding = findings.find((f) => f.check_id === key);
      return (
        <div key={key} className="space-y-1 border-l-2 border-gray-200 pl-3">
          <label className="flex flex-wrap items-center justify-between gap-2 text-sm">
            <span className="min-w-0 flex-1 font-medium">{label(key)}</span>
            <select
              aria-label={label(key)}
              className="rounded border border-gray-300 p-2"
              value={checks[key]}
              onChange={(e) =>
                setChecks({
                  ...checks,
                  [key]: e.target.value as 'pass' | 'fail' | 'unknown',
                })
              }
            >
              <option value="unknown">{t('marketing-check-unknown')}</option>
              <option value="pass">{t('marketing-check-pass')}</option>
              <option value="fail">{t('marketing-check-fail')}</option>
            </select>
          </label>
          {finding && (
            <div className="space-y-1 text-sm text-gray-600">
              <p>{finding.visual_evidence}</p>
              <p>{finding.rationale}</p>
              {finding.gate_reason && (
                <p>{t(`marketing-ai-qc-gate-${finding.gate_reason}`)}</p>
              )}
              {finding.rendered_text_diff?.length ? (
                <CopyDiff diff={finding.rendered_text_diff} />
              ) : (
                finding.suggested_fix && (
                  <p className="text-gray-900">
                    {t('marketing-ai-qc-fix', { fix: finding.suggested_fix })}
                  </p>
                )
              )}
            </div>
          )}
        </div>
      );
    });
  }
  return (
    <article className="space-y-3 border-b border-gray-200 pb-5">
      <h4 className="font-medium">
        {angle?.proposition} · {expression?.name}
      </h4>
      <p className="text-xs text-gray-500">
        {asset.quality?.quality === 'needs_changes'
          ? t('marketing-quality-changes-requested')
          : asset.ready
            ? t('marketing-quality-approved')
            : t('marketing-review-needed')}
      </p>
      {imageUrl && !imageFailed ? (
        <>
          {/* The private transfer stays direct; local blob URLs satisfy the production CSP. */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={imageUrl}
            alt={t('marketing-review-image-alt', { headline: asset.headline })}
            loading="lazy"
            className="max-h-72 max-w-full rounded-lg object-contain"
            onError={() => setImageFailed(true)}
          />
        </>
      ) : (
        <button
          className={SECONDARY}
          onClick={() => {
            setLoadImage(true);
            setImageFailed(false);
            if (image.data || image.error) void image.mutate();
          }}
        >
          {t(
            imageFailed || image.error
              ? 'marketing-ai-qc-reload-image'
              : 'marketing-view-exact-image'
          )}
        </button>
      )}
      <p className="font-medium">{asset.headline}</p>
      <p className="whitespace-pre-wrap text-sm">{asset.body_copy}</p>
      {(ai || asset.ai_qc || asset.paired_ai_qc) && (
        <div className="space-y-2 text-sm">
          {ai && (
            <p className="font-medium">
              {asset.ready
                ? t('marketing-ai-qc-confirmed')
                : flagged.length
                  ? t('marketing-ai-qc-flag-count', { count: flagged.length })
                  : t('marketing-ai-qc-ready-to-confirm')}
            </p>
          )}
          {reports.some((c) => c.result!.source === 'controlled_mock') && (
            <p className="text-amber-800">
              {t('marketing-ai-qc-simulation-note')}
            </p>
          )}
          {singleCall && singleCall.id === pairedCall?.id
            ? kindReport('combined', singleCall)
            : KINDS.map((kind) =>
                kindReport(kind, kind === 'single' ? singleCall : pairedCall)
              )}
          {missingKind && !asset.ready && (
            <p className="text-amber-800">
              {t('marketing-ai-qc-kind-missing-override')}
            </p>
          )}
          {ai && !asset.ready && actions}
        </div>
      )}
      {!ai && !asset.ai_qc && !asset.paired_ai_qc && (
        <p className="text-sm text-gray-600">
          {t('marketing-ai-qc-run-first')}
        </p>
      )}
      <details>
        <summary className="cursor-pointer text-sm font-medium">
          {t(
            ai
              ? 'marketing-ai-qc-adjust-suggestions'
              : 'marketing-ai-qc-manual-fallback'
          )}
        </summary>
        <div className="mt-3 space-y-4">
          <div className="rounded-lg bg-gray-50 p-3 text-sm">
            <p>
              {t('marketing-changed')}: {row.snapshot.plan.changed.join(' · ')}
            </p>
            <p>
              {t('marketing-held')}:{' '}
              {row.snapshot.plan.held_constant.join(' · ')}
            </p>
            <p>
              {t('marketing-allowed')}:{' '}
              {(row.snapshot.plan.allowed_changes || []).join(' · ')}
            </p>
          </div>
          {fields(ai ? flagged : Object.keys(asset.required_checks))}
          {ai && (
            <details>
              <summary className="cursor-pointer text-sm">
                {t('marketing-ai-qc-view-passing')}
              </summary>
              <div className="mt-3 space-y-3">
                {fields(
                  Object.keys(asset.required_checks).filter(
                    (key) => !flagged.includes(key)
                  )
                )}
              </div>
            </details>
          )}
          {row.snapshot.plan.comparison_kind === 'specific_factor' &&
            !pairedCall && (
              <fieldset className="space-y-2">
                <legend className="text-sm font-medium">
                  {t('marketing-paired-review')}
                </legend>
                {row.assets
                  .filter(
                    (a) =>
                      a.asset_version_id !== asset.asset_version_id &&
                      !group?.combination_ids.includes(a.combination_id)
                  )
                  .map((peer) => (
                    <label
                      key={peer.asset_version_id}
                      className="flex items-center gap-2 text-sm"
                    >
                      <input
                        type="checkbox"
                        checked={peers.includes(peer.asset_version_id)}
                        onChange={(e) =>
                          setPeers(
                            e.target.checked
                              ? [...peers, peer.asset_version_id]
                              : peers.filter((p) => p !== peer.asset_version_id)
                          )
                        }
                      />
                      {peer.headline}
                    </label>
                  ))}
              </fieldset>
            )}
          {ai && (
            <p className="text-xs text-gray-500">
              {t('marketing-ai-qc-provenance', {
                provider: reports[0]?.provider,
                model: reports[0]?.model,
                count: peers.length,
              })}
            </p>
          )}
          <Field
            label={t('marketing-review-note')}
            value={note}
            onChange={setNote}
          />
          {(overridden || missingKind) && (
            <Field
              label={t('marketing-ai-qc-override-note')}
              value={overrideNote}
              onChange={setOverrideNote}
            />
          )}
          {(!ai || asset.ready) && actions}
        </div>
      </details>
    </article>
  );
}
