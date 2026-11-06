import { useEffect, useMemo, useState } from 'react';
import { useRouter } from 'next/router';
import { useTranslation } from 'next-i18next';
import {
  ArrowPathIcon,
  BeakerIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  LockClosedIcon,
  PlusIcon,
} from '@heroicons/react/24/outline';
import { useDomainPaginatedQuery } from '@/hooks/domain/useDomainPaginatedQuery';
import {
  useExperimentsWorkspace,
  useAnalyzeDuplicates,
  useExperimentEvidence,
  useMetaLaunchPreview,
  useMetaLaunchProvisioning,
  type ExperimentDetail,
  type ExperimentProposal,
} from '@/hooks/experiments/useExperimentsWorkspace';
import { newActionId } from '@/components/generation/shared/newActionId';
import {
  DuplicateResolutionPanel,
  type GroupResolution,
} from '@/components/experiments/DuplicateResolutionPanel';
import { FrozenResolutionEvidence } from './FrozenResolutionEvidence';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import { errorMessage } from '@/lib/i18n/domain-errors';
import { formatDate, formatDateTime } from '@/lib/i18n/format';

type AssetVersionSummary = components['schemas']['AssetVersionSummary'];
type MetaLaunchPreview = components['schemas']['MultiCandidatePreviewResponse'];
type MetaLaunchConfirmation =
  components['schemas']['MultiCandidateConfirmationResponse'];
type MetaLaunchProvisioning =
  components['schemas']['LaunchProvisioningResponse'];

type MetaMode = 'customer_sandbox' | 'controlled_mock';

type TFunction = ReturnType<typeof useTranslation>['t'];

interface DraftContextFields {
  productVersionId: string;
  offerVersionId: string;
  market: string;
  currency: string;
  placement: string;
  dailyBudget: string;
  totalBudget: string;
  startDate: string;
  endDate: string;
}

const DEFAULT_CONTEXT: DraftContextFields = {
  productVersionId: '',
  offerVersionId: '',
  market: 'US',
  currency: 'USD',
  placement: 'feed',
  dailyBudget: '100',
  totalBudget: '3000',
  startDate: '',
  endDate: '',
};

function toDraftContext(fields: DraftContextFields) {
  return {
    market: fields.market,
    currency: fields.currency,
    placement: fields.placement,
    product_version_id: fields.productVersionId,
    offer_version_id: fields.offerVersionId,
    budget: {
      daily_amount: fields.dailyBudget,
      total_amount: fields.totalBudget,
    },
    schedule: {
      start_at: `${fields.startDate}T00:00:00+00:00`,
      end_at: `${fields.endDate}T23:59:59+00:00`,
      timezone: 'UTC',
    },
    audience: { schema_version: '1', content: { targeting: 'broad' } },
    attribution: { click_window_days: 7, view_window_days: 1 },
    statistical_plan: {
      primary_metric: 'ctr',
      ctr_threshold: '0.01',
      alpha: '0.05',
      bootstrap_samples: 2000,
    },
  };
}

function fieldsFromExperiment(
  experiment: ExperimentDetail
): DraftContextFields {
  const context = experiment.draft_context as Record<string, any>;
  const budget = (context.budget ?? {}) as Record<string, unknown>;
  const schedule = (context.schedule ?? {}) as Record<string, unknown>;
  return {
    productVersionId: String(context.product_version_id ?? ''),
    offerVersionId: String(context.offer_version_id ?? ''),
    market: String(context.market ?? 'US'),
    currency: String(context.currency ?? 'USD'),
    placement: String(context.placement ?? 'feed'),
    dailyBudget: String(budget.daily_amount ?? '100'),
    totalBudget: String(budget.total_amount ?? '3000'),
    startDate: String(schedule.start_at ?? '').slice(0, 10),
    endDate: String(schedule.end_at ?? '').slice(0, 10),
  };
}

function stateTone(state: string) {
  switch (state) {
    case 'running':
      return 'badge-info';
    case 'closed':
      return 'badge-success';
    case 'frozen':
      return 'badge-warning';
    default:
      return 'badge-ghost';
  }
}

const CLIENT_NETWORK_ERROR = 'client_network_error';

/**
 * A request whose outcome the browser never saw (dropped connection). It
 * never reached the server, so its advice is written here, in the interface
 * language, rather than looked up by a server code.
 */
function networkProblem(
  t: TFunction,
  detailKey: string,
  titleKey = 'problem.network-title'
): ProblemDetails {
  return {
    type: CLIENT_NETWORK_ERROR,
    title: t(titleKey),
    status: 0,
    detail: t(detailKey),
    instance: '',
    code: 'network_error',
    trace_id: '',
    retryable: true,
    field_errors: [],
  };
}

/**
 * A refusal in the interface language, by its code (#253); the server's
 * `detail` is never shown. A dropped connection (`networkProblem`) shows the
 * advice written in the browser instead of the Meta-specific
 * `error-network_error` copy.
 */
function ProblemMessage({ problem }: { problem?: ProblemDetails | null }) {
  // `errorMessage` reads the `common` error table itself.
  const { t } = useTranslation('experiments');
  if (!problem) return null;
  if (problem.type === CLIENT_NETWORK_ERROR) return <>{problem.detail}</>;
  return <>{errorMessage(t, problem)}</>;
}

function ContextFields({
  value,
  onChange,
  disabled = false,
}: {
  value: DraftContextFields;
  onChange: (value: DraftContextFields) => void;
  disabled?: boolean;
}) {
  const { t } = useTranslation('experiments');
  const set = (key: keyof DraftContextFields, fieldValue: string) =>
    onChange({ ...value, [key]: fieldValue });

  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
      <label className="form-control">
        <span className="label-text mb-1 text-xs font-medium">
          {t('context.product-version')}
        </span>
        <input
          className="input input-bordered input-sm"
          value={value.productVersionId}
          onChange={(event) => set('productVersionId', event.target.value)}
          disabled={disabled}
          required
        />
      </label>
      <label className="form-control">
        <span className="label-text mb-1 text-xs font-medium">
          {t('context.offer-version')}
        </span>
        <input
          className="input input-bordered input-sm"
          value={value.offerVersionId}
          onChange={(event) => set('offerVersionId', event.target.value)}
          disabled={disabled}
          required
        />
      </label>
      <label className="form-control">
        <span className="label-text mb-1 text-xs font-medium">
          {t('context.market')}
        </span>
        <input
          className="input input-bordered input-sm uppercase"
          value={value.market}
          maxLength={2}
          onChange={(event) => set('market', event.target.value.toUpperCase())}
          disabled={disabled}
          required
        />
      </label>
      <label className="form-control">
        <span className="label-text mb-1 text-xs font-medium">
          {t('context.currency')}
        </span>
        <input
          className="input input-bordered input-sm uppercase"
          value={value.currency}
          maxLength={3}
          onChange={(event) =>
            set('currency', event.target.value.toUpperCase())
          }
          disabled={disabled}
          required
        />
      </label>
      <label className="form-control">
        <span className="label-text mb-1 text-xs font-medium">
          {t('context.placement')}
        </span>
        <select
          className="select select-bordered select-sm"
          value={value.placement}
          onChange={(event) => set('placement', event.target.value)}
          disabled={disabled}
        >
          <option value="feed">{t('context.placement-feed')}</option>
          <option value="story">{t('context.placement-story')}</option>
          <option value="reel">{t('context.placement-reel')}</option>
        </select>
      </label>
      <label className="form-control">
        <span className="label-text mb-1 text-xs font-medium">
          {t('context.daily-budget')}
        </span>
        <input
          className="input input-bordered input-sm"
          type="number"
          min="0.01"
          step="0.01"
          value={value.dailyBudget}
          onChange={(event) => set('dailyBudget', event.target.value)}
          disabled={disabled}
          required
        />
      </label>
      <label className="form-control">
        <span className="label-text mb-1 text-xs font-medium">
          {t('context.total-budget')}
        </span>
        <input
          className="input input-bordered input-sm"
          type="number"
          min="0.01"
          step="0.01"
          value={value.totalBudget}
          onChange={(event) => set('totalBudget', event.target.value)}
          disabled={disabled}
          required
        />
      </label>
      <label className="form-control">
        <span className="label-text mb-1 text-xs font-medium">
          {t('context.start-date')}
        </span>
        <input
          className="input input-bordered input-sm"
          type="date"
          value={value.startDate}
          onChange={(event) => set('startDate', event.target.value)}
          disabled={disabled}
          required
        />
      </label>
      <label className="form-control">
        <span className="label-text mb-1 text-xs font-medium">
          {t('context.end-date')}
        </span>
        <input
          className="input input-bordered input-sm"
          type="date"
          value={value.endDate}
          onChange={(event) => set('endDate', event.target.value)}
          disabled={disabled}
          required
        />
      </label>
    </div>
  );
}

function NewExperimentPanel({
  assets,
  assetsLoading,
  problem,
  onCancel,
  onCreate,
  isPending,
}: {
  assets: AssetVersionSummary[];
  assetsLoading: boolean;
  problem?: ProblemDetails;
  onCancel: () => void;
  onCreate: (input: {
    name: string;
    productId: string;
    context: DraftContextFields;
    assetIds: string[];
  }) => Promise<void>;
  isPending: boolean;
}) {
  const { t } = useTranslation('experiments');
  const [name, setName] = useState('');
  const [context, setContext] = useState(DEFAULT_CONTEXT);
  const [selected, setSelected] = useState<string[]>([]);
  const selectedAssets = assets.filter((asset) => selected.includes(asset.id));
  const productIds = new Set(selectedAssets.map((asset) => asset.product_id));
  const selectionValid = selected.length >= 2 && productIds.size === 1;

  const toggleAsset = (asset: AssetVersionSummary) => {
    setSelected((current) =>
      current.includes(asset.id)
        ? current.filter((id) => id !== asset.id)
        : [...current, asset.id]
    );
  };

  return (
    <section
      className="rounded-box border border-base-300 bg-base-100 p-5"
      aria-label={t('new.aria')}
    >
      <div className="mb-5 flex items-start justify-between gap-4">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-primary">
            {t('new.eyebrow')}
          </p>
          <h2 className="mt-1 text-xl font-semibold">{t('new.title')}</h2>
          <p className="mt-1 max-w-2xl text-sm text-base-content/65">
            {t('new.intro')}
          </p>
        </div>
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={onCancel}
        >
          {t('new.cancel')}
        </button>
      </div>

      <form
        className="space-y-6"
        onSubmit={(event) => {
          event.preventDefault();
          if (!selectionValid) return;
          void onCreate({
            name,
            productId: selectedAssets[0].product_id,
            context,
            assetIds: selected,
          });
        }}
      >
        <label className="form-control max-w-xl">
          <span className="label-text mb-1 text-xs font-medium">
            {t('new.name')}
          </span>
          <input
            className="input input-bordered"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder={t('new.name-placeholder')}
            required
          />
        </label>

        <div>
          <div className="mb-2 flex items-center justify-between gap-4">
            <h3 className="font-semibold">{t('new.candidates')}</h3>
            <span className="text-xs text-base-content/60">
              {t('new.selected', { count: selected.length })}
            </span>
          </div>
          {assetsLoading ? (
            <div
              className="flex min-h-24 items-center justify-center"
              aria-busy="true"
            >
              <span className="loading loading-spinner loading-sm" />
            </div>
          ) : assets.length === 0 ? (
            <div className="rounded-box border border-dashed border-base-300 p-5 text-sm text-base-content/65">
              {t('new.no-assets')}
            </div>
          ) : (
            <div className="grid max-h-64 grid-cols-1 gap-2 overflow-y-auto sm:grid-cols-2">
              {assets.map((asset) => {
                const checked = selected.includes(asset.id);
                const otherProductSelected =
                  productIds.size === 1 && !productIds.has(asset.product_id);
                return (
                  <label
                    key={asset.id}
                    className={`flex cursor-pointer items-start gap-3 rounded-box border p-3 ${checked ? 'border-primary bg-primary/5' : 'border-base-300'} ${otherProductSelected ? 'opacity-45' : ''}`}
                  >
                    <input
                      type="checkbox"
                      className="checkbox checkbox-primary checkbox-sm mt-0.5"
                      checked={checked}
                      disabled={otherProductSelected && !checked}
                      onChange={() => toggleAsset(asset)}
                    />
                    <span className="min-w-0">
                      <span className="block truncate text-sm font-medium">
                        {asset.id}
                      </span>
                      <span className="block truncate text-xs text-base-content/55">
                        {asset.product_id} · {asset.aspect_ratio} ·{' '}
                        {asset.provider}
                      </span>
                    </span>
                  </label>
                );
              })}
            </div>
          )}
          {selected.length > 0 && !selectionValid && (
            <p className="mt-2 text-xs text-warning" role="status">
              {t('new.invalid-selection')}
            </p>
          )}
        </div>

        <div>
          <h3 className="mb-3 font-semibold">{t('new.context')}</h3>
          <ContextFields value={context} onChange={setContext} />
        </div>

        {problem && (
          <div className="alert alert-error text-sm" role="alert">
            <ExclamationTriangleIcon className="h-5 w-5" />
            <span>
              <ProblemMessage problem={problem} />
            </span>
          </div>
        )}

        <div className="flex justify-end">
          <button
            type="submit"
            className="btn btn-primary"
            disabled={!selectionValid || isPending}
          >
            {isPending && (
              <span className="loading loading-spinner loading-xs" />
            )}
            {t('new.create')}
          </button>
        </div>
      </form>
    </section>
  );
}

function ProposalCard({
  experiment,
  proposal,
  onConfirm,
  pending,
  problem,
}: {
  experiment: ExperimentDetail;
  proposal: ExperimentProposal;
  onConfirm: () => Promise<void>;
  pending: boolean;
  problem?: ProblemDetails;
}) {
  const { t } = useTranslation('experiments');
  const [acknowledged, setAcknowledged] = useState(false);
  const confirmed = experiment.decisions?.some(
    (decision) =>
      decision.proposal_id === proposal.id && Boolean(decision.confirmation_id)
  );
  const decisiveEntries = (proposal.proposed_entries ?? []).filter(
    (entry) => String(entry.verdict ?? 'inconclusive') !== 'inconclusive'
  );
  const inconclusive = decisiveEntries.length === 0;

  return (
    <article
      className="rounded-box border border-base-300 bg-base-100 p-4"
      data-proposal-id={proposal.id}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.14em] text-info">
            {t('proposal.eyebrow')}
          </p>
          <h4 className="mt-1 font-semibold">{t('proposal.title')}</h4>
        </div>
        <span
          className={`badge ${confirmed ? 'badge-success' : 'badge-warning'}`}
        >
          {confirmed ? t('proposal.confirmed') : t('proposal.not-confirmed')}
        </span>
      </div>

      <div className="mt-4 space-y-2">
        {(proposal.proposed_entries ?? []).map((entry, index) => {
          const verdict = String(entry.verdict ?? 'inconclusive');
          return (
            <div
              key={`${String(entry.entry_id)}-${index}`}
              className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-base-200/60 px-3 py-2 text-sm"
            >
              <span className="font-mono text-xs">
                {String(entry.entry_id ?? t('proposal.entry-candidate'))}
              </span>
              <span>
                {t('proposal.entry', {
                  verdict: t(`verdict.${verdict}`, { defaultValue: verdict }),
                  metric: String(entry.metric ?? t('proposal.metric')),
                  estimate: String(entry.estimate ?? ''),
                })}
              </span>
            </div>
          );
        })}
      </div>

      <details className="mt-3 text-xs text-base-content/65">
        <summary className="cursor-pointer font-medium">
          {t('proposal.snapshot')}
        </summary>
        <pre className="mt-2 max-h-48 overflow-auto rounded-lg bg-base-200 p-3 whitespace-pre-wrap">
          {JSON.stringify(proposal.evidence ?? {}, null, 2)}
        </pre>
      </details>

      {!confirmed && proposal.state === 'created' && (
        <div className="mt-4 border-t border-base-300 pt-4">
          {inconclusive && (
            <div className="alert alert-warning py-2 text-xs" role="status">
              <ExclamationTriangleIcon className="h-4 w-4" />
              <span>{t('proposal.inconclusive')}</span>
            </div>
          )}
          <label className="flex cursor-pointer items-start gap-3 text-sm">
            <input
              type="checkbox"
              className="checkbox checkbox-primary checkbox-sm mt-0.5"
              checked={acknowledged}
              disabled={inconclusive}
              onChange={(event) => setAcknowledged(event.target.checked)}
            />
            <span>{t('proposal.acknowledge')}</span>
          </label>
          {problem && (
            <p className="mt-2 text-sm text-error" role="alert">
              <ProblemMessage problem={problem} />
            </p>
          )}
          <button
            type="button"
            className="btn btn-primary btn-sm mt-3"
            disabled={inconclusive || !acknowledged || pending}
            onClick={() => void onConfirm()}
          >
            {pending && <span className="loading loading-spinner loading-xs" />}
            {inconclusive ? t('proposal.no-winner') : t('proposal.confirm')}
          </button>
        </div>
      )}
    </article>
  );
}

function ExperimentDetailPanel({
  experiment,
  workspace,
  launchPreviewId,
  launchProvisioningId,
  onLaunchRouteChange,
}: {
  experiment: ExperimentDetail;
  workspace: ReturnType<typeof useExperimentsWorkspace>;
  launchPreviewId: string | null;
  launchProvisioningId: string | null;
  onLaunchRouteChange: (values: {
    previewId?: string | null;
    provisioningId?: string | null;
  }) => void;
}) {
  const { t, i18n } = useTranslation('experiments');
  const [context, setContext] = useState(() =>
    fieldsFromExperiment(experiment)
  );
  const [metaMode, setMetaMode] = useState<MetaMode>('customer_sandbox');
  const [freezeAcknowledged, setFreezeAcknowledged] = useState(false);
  const [bindingDrafts, setBindingDrafts] = useState<
    Record<string, { adAccountId: string; adId: string }>
  >({});
  const [launchConnectionId, setLaunchConnectionId] = useState('');
  const [launchAdAccountId, setLaunchAdAccountId] = useState('');
  const [launchDailyBudgetMinor, setLaunchDailyBudgetMinor] = useState(() =>
    String(Math.max(1, Math.round(Number(context.dailyBudget || 0) * 100)))
  );
  const [localLaunchPreview, setLocalLaunchPreview] =
    useState<MetaLaunchPreview>();
  const [launchAcknowledged, setLaunchAcknowledged] = useState(false);
  const [executeAcknowledged, setExecuteAcknowledged] = useState(false);
  const [launchConfirmation, setLaunchConfirmation] = useState<{
    token: string;
    expiresAt: string;
  }>();
  const [localProblem, setLocalProblem] = useState<ProblemDetails>();
  const evidence = useExperimentEvidence(experiment.id);
  const persistedPreview = useMetaLaunchPreview(launchPreviewId, metaMode);
  const provisioning = useMetaLaunchProvisioning(
    launchProvisioningId,
    metaMode
  );
  const launchPreview = persistedPreview.data ?? localLaunchPreview;
  const provisioningData = provisioning.data as
    | MetaLaunchProvisioning
    | undefined;

  const entries = experiment.entries ?? [];
  const candidates = experiment.candidates ?? [];

  const analysis = useAnalyzeDuplicates(
    experiment.state === 'draft' && entries.length >= 2 ? experiment.id : null
  );

  const [resolutions, setResolutions] = useState<Map<string, GroupResolution>>(
    new Map()
  );

  const analysisStateKey = analysis.data
    ? `${experiment.id}-${analysis.data.analyzed_revision}-${analysis.data.candidate_set_hash}`
    : '';

  // Reset resolutions and the freeze acknowledgement whenever the analysis
  // snapshot changes (a new revision or a re-run analysis) so a stale plan
  // can never be submitted against a candidate set it wasn't computed for.
  useEffect(() => {
    setResolutions(new Map());
    setFreezeAcknowledged(false);
  }, [analysisStateKey]);
  const candidateExecutionModes = new Set(
    candidates.map((candidate) => candidate.execution_mode).filter(Boolean)
  );
  const launchCandidateModesValid =
    candidates.length >= 2 &&
    candidateExecutionModes.size === 1 &&
    candidates.every((candidate) => Boolean(candidate.execution_mode));
  const allBound =
    candidates.length >= 2 &&
    candidates.every((candidate) => candidate.binding);
  const start = new Date(context.startDate);
  const end = new Date(context.endDate);
  const durationDays =
    Number.isFinite(start.getTime()) && Number.isFinite(end.getTime())
      ? Math.max(
          1,
          Math.ceil((end.getTime() - start.getTime()) / 86_400_000) + 1
        )
      : 0;
  const dailyImpact = entries.length * Number(context.dailyBudget || 0);
  const plannedImpact = dailyImpact * durationDays;
  const previewExpired = launchPreview
    ? new Date(launchPreview.expires_at).getTime() <= Date.now()
    : false;
  const previewMatchesExperiment = Boolean(
    launchPreview &&
    launchPreview.experiment_id === experiment.id &&
    launchPreview.experiment_version_id === experiment.version_id &&
    launchPreview.meta_mode === metaMode
  );
  const confirmationExpired = launchConfirmation
    ? new Date(launchConfirmation.expiresAt).getTime() <= Date.now()
    : true;

  useEffect(() => {
    if (
      !launchProvisioningId ||
      !provisioningData ||
      (!['pending', 'provisioning'].includes(
        provisioningData.provisioning_state
      ) &&
        provisioningData.operation_state !== 'uncertain')
    ) {
      return undefined;
    }
    const interval = window.setInterval(() => {
      void provisioning.refresh();
    }, 2_000);
    return () => window.clearInterval(interval);
  }, [
    launchProvisioningId,
    provisioning,
    provisioningData,
    provisioningData?.provisioning_state,
  ]);

  const refresh = async () => {
    await Promise.all([workspace.detail.refresh(), workspace.list.refresh()]);
  };

  const run = async (
    operation: () => Promise<{ kind: string; problem?: ProblemDetails }>
  ) => {
    setLocalProblem(undefined);
    try {
      const outcome = await operation();
      if (outcome.kind === 'problem') {
        setLocalProblem(outcome.problem);
        return false;
      }
      if (outcome.kind === 'success') await refresh();
      return outcome.kind === 'success';
    } catch {
      setLocalProblem(networkProblem(t, 'problem.network-detail'));
      return false;
    }
  };

  const saveContext = () =>
    run(() =>
      workspace.commands.updateContext.execute({
        actionInstanceId: newActionId(),
        pathParams: { experiment_id: experiment.id },
        body: {
          draft_context: toDraftContext(context),
          expected_revision: experiment.revision,
        },
      })
    );

  const allGroupsResolved =
    !analysis.data?.has_duplicates ||
    (analysis.data.duplicate_groups ?? []).every((g) => {
      const r = resolutions.get(g.duplicate_group_hash);
      if (!r?.action || !r?.reason?.trim()) return false;
      if (
        (r.action === 'exclude_source_candidate' ||
          r.action === 'remap_before_freeze') &&
        !r.affectedCandidateFingerprint
      )
        return false;
      if (r.action === 'remap_before_freeze' && !r.remapTargetId) return false;
      return true;
    });

  const freeze = async () => {
    if (!analysis.data) return;
    const plan = analysis.data.has_duplicates
      ? (analysis.data.duplicate_groups ?? []).map((g) => {
          const r = resolutions.get(g.duplicate_group_hash)!;
          return {
            duplicate_group_hash: g.duplicate_group_hash,
            action: r.action as
              | 'allow_controlled_duplicate'
              | 'exclude_source_candidate'
              | 'remap_before_freeze',
            reason: r.reason,
            source_candidate_fingerprints: (g.members ?? [])
              .map((m) => m.source_candidate_fingerprint)
              .sort(),
            source_candidate_fingerprint: r.affectedCandidateFingerprint,
            remap_concept_key_id: r.remapTargetId,
          };
        })
      : [];

    setLocalProblem(undefined);
    let outcome: { kind: string; problem?: ProblemDetails };
    try {
      outcome = await workspace.commands.freeze.execute({
        actionInstanceId: newActionId(),
        pathParams: { experiment_id: experiment.id },
        body: {
          expected_revision: experiment.revision,
          analyzed_candidate_set_hash: analysis.data!.candidate_set_hash,
          concept_resolution_plan: plan,
        },
      });
    } catch {
      setLocalProblem(networkProblem(t, 'problem.network-detail'));
      return;
    }

    if (outcome.kind === 'problem') setLocalProblem(outcome.problem);
    if (outcome.kind === 'success') await refresh();

    // Use the outcome returned directly from execute() rather than the
    // command's `problem` field, which is a stale closure captured at
    // render time and would not reflect the result of this submission.
    if (outcome.kind === 'problem') {
      const code = outcome.problem?.code;
      if (
        code === 'revision_conflict' ||
        code === 'concept_resolution_stale' ||
        code === 'concept_unresolved'
      ) {
        // The draft moved (or the analyzed candidate set went stale)
        // underneath this submission — refetch both the experiment and the
        // duplicate analysis and force the operator to re-resolve against
        // the current snapshot rather than resubmit the old plan.
        await Promise.all([workspace.detail.refresh(), analysis.refresh()]);
        setResolutions(new Map());
        setFreezeAcknowledged(false);
      }
    }
  };

  const bind = (candidateId: string) => {
    const binding = bindingDrafts[candidateId];
    if (!binding?.adAccountId || !binding.adId) return Promise.resolve(false);
    return run(() =>
      workspace.commands.bindCandidate.execute({
        actionInstanceId: newActionId(),
        pathParams: { experiment_id: experiment.id, candidate_id: candidateId },
        body: {
          ad_account_id: binding.adAccountId,
          ad_id: binding.adId,
          source: 'manual_read_only',
          expected_revision: experiment.revision,
        },
      })
    );
  };

  const createLaunchPreview = async () => {
    if (!experiment.version_id) return;
    setLocalProblem(undefined);
    try {
      const outcome = await workspace.commands.createLaunchPreview.execute({
        actionInstanceId: newActionId(),
        body: {
          experiment_id: experiment.id,
          experiment_version_id: experiment.version_id,
          connection_id: launchConnectionId,
          meta_mode: metaMode,
          ad_account_id: launchAdAccountId,
          candidates: candidates.map((candidate) => ({
            experiment_version_candidate_id: candidate.id,
            asset_version_id: candidate.asset_version_id,
            creative_config: candidate.marketing_vars ?? {},
          })),
          daily_budget_minor: Number(launchDailyBudgetMinor),
          currency: context.currency,
          campaign_config: {
            objective: 'OUTCOME_TRAFFIC',
            initial_status: 'PAUSED',
          },
        },
      });
      if (outcome.kind === 'problem') setLocalProblem(outcome.problem);
      if (outcome.kind === 'success') {
        setLocalLaunchPreview(outcome.data);
        setLaunchConfirmation(undefined);
        setLaunchAcknowledged(false);
        setExecuteAcknowledged(false);
        onLaunchRouteChange({
          previewId: outcome.data.id,
          provisioningId: null,
        });
      }
    } catch {
      setLocalProblem(networkProblem(t, 'problem.preview-uncertain'));
    }
  };

  const confirmLaunchPreview = async () => {
    if (!launchPreview) return;
    setLocalProblem(undefined);
    try {
      const outcome = await workspace.commands.confirmLaunchPreview.execute({
        actionInstanceId: newActionId(),
        pathParams: { preview_id: launchPreview.id },
        body: {
          meta_mode: metaMode,
          preview_hash: launchPreview.preview_hash,
        },
      });
      if (outcome.kind === 'problem') setLocalProblem(outcome.problem);
      if (outcome.kind === 'success') {
        const confirmed = outcome.data as MetaLaunchConfirmation;
        setLocalLaunchPreview(confirmed);
        setLaunchConfirmation({
          token: confirmed.confirmation_token,
          expiresAt: confirmed.confirmation_expires_at,
        });
        await persistedPreview.refresh();
      }
    } catch {
      setLocalProblem(networkProblem(t, 'problem.confirm-uncertain'));
    }
  };

  const executeLaunchPreview = async () => {
    if (
      !launchPreview ||
      launchPreview.state !== 'confirmed' ||
      !launchConfirmation ||
      confirmationExpired
    )
      return;
    setLocalProblem(undefined);
    try {
      const outcome = await workspace.commands.executeLaunchPreview.execute({
        actionInstanceId: newActionId(),
        pathParams: { preview_id: launchPreview.id },
        body: {
          meta_mode: metaMode,
          confirmation_token: launchConfirmation.token,
        },
      });
      if (outcome.kind === 'problem') setLocalProblem(outcome.problem);
      if (outcome.kind === 'success') {
        onLaunchRouteChange({
          previewId: launchPreview.id,
          provisioningId: outcome.data.id,
        });
      }
    } catch {
      setLocalProblem(
        networkProblem(
          t,
          'problem.provisioning-detail',
          'problem.provisioning-title'
        )
      );
    }
  };

  const recoverLaunch = async () => {
    if (!provisioningData) return;
    const outcome = await workspace.commands.recoverLaunchOperation.execute({
      actionInstanceId: newActionId(),
      pathParams: { operation_id: provisioningData.operation_id },
      body: { meta_mode: metaMode },
    });
    if (outcome.kind === 'problem') setLocalProblem(outcome.problem);
    if (outcome.kind === 'success') await provisioning.refresh();
  };

  const sharedProblem =
    localProblem ??
    workspace.commands.updateContext.problem ??
    workspace.commands.freeze.problem ??
    workspace.commands.bindCandidate.problem ??
    workspace.commands.start.problem ??
    workspace.commands.judge.problem ??
    workspace.commands.createLaunchPreview.problem ??
    workspace.commands.confirmLaunchPreview.problem ??
    workspace.commands.executeLaunchPreview.problem ??
    workspace.commands.recoverLaunchOperation.problem ??
    persistedPreview.problem ??
    provisioning.problem ??
    analysis.problem;

  return (
    <section className="min-w-0 space-y-5" aria-label={t('detail.aria')}>
      <div className="rounded-box border border-base-300 bg-base-100 p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <div className="flex items-center gap-2">
              <span className={`badge badge-sm ${stateTone(experiment.state)}`}>
                {t(`status.${experiment.state}`, {
                  defaultValue: experiment.state,
                })}
              </span>
              <span className="text-xs text-base-content/50">
                {t('detail.revision', { revision: experiment.revision })}
              </span>
            </div>
            <h2 className="mt-2 text-2xl font-semibold">{experiment.name}</h2>
            <p className="mt-1 font-mono text-xs text-base-content/50">
              {experiment.id}
            </p>
          </div>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => void workspace.detail.refresh()}
            disabled={workspace.detail.isLoading}
          >
            <ArrowPathIcon className="h-4 w-4" /> {t('detail.refresh')}
          </button>
        </div>
      </div>

      {sharedProblem && (
        <div className="alert alert-error text-sm" role="alert">
          <ExclamationTriangleIcon className="h-5 w-5" />
          <span>
            <ProblemMessage problem={sharedProblem} />
          </span>
        </div>
      )}

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1.4fr)_minmax(19rem,0.6fr)]">
        <div className="space-y-5">
          <section className="rounded-box border border-base-300 bg-base-100 p-5">
            <div className="mb-4 flex items-center justify-between gap-3">
              <div>
                <h3 className="font-semibold">{t('detail.context-title')}</h3>
                <p className="text-xs text-base-content/55">
                  {experiment.state === 'draft'
                    ? t('detail.context-editable')
                    : t('detail.context-frozen')}
                </p>
              </div>
              {experiment.state !== 'draft' && (
                <LockClosedIcon className="h-5 w-5 text-base-content/45" />
              )}
            </div>
            <ContextFields
              value={context}
              onChange={setContext}
              disabled={experiment.state !== 'draft'}
            />
            {experiment.state === 'draft' && (
              <button
                type="button"
                className="btn btn-outline btn-sm mt-4"
                onClick={() => void saveContext()}
                disabled={workspace.commands.updateContext.isPending}
              >
                {t('detail.save-context')}
              </button>
            )}
            {experiment.frozen_snapshot_hash && (
              <p className="mt-4 break-all font-mono text-[11px] text-base-content/50">
                {t('detail.snapshot', {
                  hash: experiment.frozen_snapshot_hash,
                })}
              </p>
            )}
            {experiment.frozen_context && (
              <div className="mt-4 rounded-lg border border-base-300 bg-base-200/40 p-3 text-xs">
                <p className="font-semibold">{t('detail.manifest')}</p>
                <dl className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
                  <div>
                    <dt className="text-base-content/50">
                      {t('detail.exact-versions')}
                    </dt>
                    <dd className="font-mono">
                      {experiment.frozen_context.product_version_id} ·{' '}
                      {experiment.frozen_context.offer_version_id}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-base-content/50">
                      {t('detail.budget')}
                    </dt>
                    <dd>
                      {t('detail.budget-value', {
                        currency: experiment.frozen_context.budget_currency,
                        daily: experiment.frozen_context.daily_budget_amount,
                        cap: experiment.frozen_context.total_budget_cap,
                      })}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-base-content/50">
                      {t('detail.attribution')}
                    </dt>
                    <dd>
                      {t('detail.attribution-value', {
                        click:
                          experiment.frozen_context
                            .attribution_click_window_days,
                        view: experiment.frozen_context
                          .attribution_view_window_days,
                      })}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-base-content/50">
                      {t('detail.schedule')}
                    </dt>
                    <dd>
                      {t('detail.schedule-value', {
                        start: formatDate(
                          i18n.language,
                          experiment.frozen_context.schedule_start_at
                        ),
                        end: formatDate(
                          i18n.language,
                          experiment.frozen_context.schedule_end_at
                        ),
                        timezone: experiment.frozen_context.schedule_timezone,
                      })}
                    </dd>
                  </div>
                </dl>
              </div>
            )}
          </section>

          <section className="rounded-box border border-base-300 bg-base-100 p-5">
            <div className="flex items-center justify-between gap-3">
              <div>
                <h3 className="font-semibold">
                  {t('detail.candidates-title')}
                </h3>
                <p className="text-xs text-base-content/55">
                  {t('detail.candidates-intro')}
                </p>
              </div>
              <span className="badge badge-ghost">
                {experiment.state === 'draft'
                  ? entries.length
                  : candidates.length}
              </span>
            </div>

            <div className="mt-4 space-y-2">
              {experiment.state === 'draft'
                ? entries.map((entry, index) => (
                    <div
                      key={entry.id}
                      className="flex items-center justify-between rounded-lg bg-base-200/60 px-3 py-2 text-sm"
                    >
                      <span>
                        {t('detail.candidate', { number: index + 1 })}
                      </span>
                      <span className="font-mono text-xs">
                        {entry.asset_version_id}
                      </span>
                    </div>
                  ))
                : candidates.map((candidate, index) => {
                    const draft = bindingDrafts[candidate.id] ?? {
                      adAccountId: '',
                      adId: '',
                    };
                    return (
                      <div
                        key={candidate.id}
                        className="rounded-lg border border-base-300 p-3"
                        role="group"
                        aria-label={t('detail.candidate', {
                          number: index + 1,
                        })}
                      >
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <div>
                            <span className="text-sm font-medium">
                              {t('detail.candidate', { number: index + 1 })}
                            </span>
                            <span className="ml-2 font-mono text-xs text-base-content/55">
                              {candidate.asset_version_id}
                            </span>
                            <span className="ml-2 badge badge-ghost badge-xs">
                              {candidate.execution_mode ??
                                t('detail.mode-unavailable')}
                            </span>
                          </div>
                          <span
                            className={`badge badge-sm ${candidate.binding ? 'badge-success' : 'badge-ghost'}`}
                          >
                            {candidate.binding
                              ? t('detail.bound')
                              : t('detail.binding-required')}
                          </span>
                        </div>
                        {candidate.binding ? (
                          <p className="mt-2 text-xs text-base-content/60">
                            {t('detail.binding-line', {
                              account: candidate.binding.ad_account_id,
                              ad: candidate.binding.ad_id,
                            })}
                          </p>
                        ) : experiment.state === 'frozen' ? (
                          <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-[1fr_1fr_auto]">
                            <input
                              className="input input-bordered input-sm min-w-0 w-full"
                              aria-label={t('detail.ad-account-for', {
                                number: index + 1,
                              })}
                              placeholder="act_..."
                              value={draft.adAccountId}
                              onChange={(event) =>
                                setBindingDrafts((current) => ({
                                  ...current,
                                  [candidate.id]: {
                                    ...draft,
                                    adAccountId: event.target.value,
                                  },
                                }))
                              }
                            />
                            <input
                              className="input input-bordered input-sm min-w-0 w-full"
                              aria-label={t('detail.ad-id-for', {
                                number: index + 1,
                              })}
                              placeholder="ad_..."
                              value={draft.adId}
                              onChange={(event) =>
                                setBindingDrafts((current) => ({
                                  ...current,
                                  [candidate.id]: {
                                    ...draft,
                                    adId: event.target.value,
                                  },
                                }))
                              }
                            />
                            <button
                              type="button"
                              className="btn btn-outline btn-sm"
                              disabled={
                                !draft.adAccountId ||
                                !draft.adId ||
                                workspace.commands.bindCandidate.isPending
                              }
                              onClick={() => void bind(candidate.id)}
                            >
                              {t('detail.bind')}
                            </button>
                          </div>
                        ) : null}
                      </div>
                    );
                  })}
            </div>
          </section>

          {experiment.concept_resolution_evidence && (
            <section className="rounded-box border border-base-300 bg-base-100 p-5">
              <FrozenResolutionEvidence
                evidence={experiment.concept_resolution_evidence}
              />
            </section>
          )}

          {analysis.data?.has_duplicates && (
            <section className="rounded-box border border-base-300 bg-base-100 p-5">
              <DuplicateResolutionPanel
                groups={analysis.data.duplicate_groups ?? []}
                onResolutionsChange={setResolutions}
                stateKey={analysisStateKey}
              />
            </section>
          )}

          {experiment.state !== 'draft' && (
            <section className="rounded-box border border-base-300 bg-base-100 p-5">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <h3 className="font-semibold">
                    {t('detail.timeline-title')}
                  </h3>
                  <p className="text-xs text-base-content/55">
                    {t('detail.timeline-intro')}
                  </p>
                </div>
                <button
                  type="button"
                  className="btn btn-ghost btn-xs"
                  onClick={() => void evidence.refresh()}
                >
                  {t('detail.refresh')}
                </button>
              </div>
              {evidence.isLoading && !evidence.data ? (
                <div className="mt-3 skeleton h-16 w-full" aria-busy="true" />
              ) : (evidence.data?.items?.length ?? 0) === 0 ? (
                <p className="mt-3 text-sm text-base-content/55">
                  {t('detail.no-events')}
                </p>
              ) : (
                <ol className="mt-3 space-y-2">
                  {evidence.data?.items?.map((event) => (
                    <li
                      key={event.id}
                      className="rounded-lg bg-base-200/50 px-3 py-2 text-xs"
                    >
                      <div className="flex justify-between gap-3">
                        <span className="font-medium">{event.event_type}</span>
                        <span className="text-base-content/45">
                          #{event.timeline_sequence}
                        </span>
                      </div>
                      <p className="mt-1 break-all font-mono text-[10px] text-base-content/50">
                        {event.source_resource_type}:{event.source_resource_id}
                      </p>
                    </li>
                  ))}
                </ol>
              )}
            </section>
          )}

          {(experiment.proposals?.length ?? 0) > 0 && (
            <section className="space-y-3">
              <div>
                <h3 className="font-semibold">{t('detail.proposals-title')}</h3>
                <p className="text-xs text-base-content/55">
                  {t('detail.proposals-intro')}
                </p>
              </div>
              {experiment.proposals?.map((proposal) => (
                <ProposalCard
                  key={proposal.id}
                  experiment={experiment}
                  proposal={proposal}
                  pending={workspace.commands.confirmProposal.isPending}
                  problem={workspace.commands.confirmProposal.problem}
                  onConfirm={() =>
                    run(() =>
                      workspace.commands.confirmProposal.execute({
                        actionInstanceId: newActionId(),
                        pathParams: {
                          experiment_id: experiment.id,
                          proposal_id: proposal.id,
                        },
                        body: { expected_proposal_sequence: proposal.sequence },
                      })
                    ).then(() => undefined)
                  }
                />
              ))}
            </section>
          )}

          {(experiment.decisions?.length ?? 0) > 0 && (
            <section className="rounded-box border border-success/30 bg-success/5 p-5">
              <div className="flex items-center gap-2">
                <CheckCircleIcon className="h-5 w-5 text-success" />
                <h3 className="font-semibold">{t('detail.decisions-title')}</h3>
              </div>
              <div className="mt-3 space-y-2">
                {experiment.decisions?.map((decision) => (
                  <div
                    key={decision.id}
                    className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-base-100 px-3 py-2 text-sm"
                  >
                    <span className="font-mono text-xs">
                      {decision.candidate_id}
                    </span>
                    <span className="font-semibold">
                      {decision.verdict === 'winner' ||
                      decision.verdict === 'win'
                        ? t('detail.confirmed-winner')
                        : t('detail.confirmed-verdict', {
                            verdict: t(`verdict.${decision.verdict}`, {
                              defaultValue: decision.verdict,
                            }),
                          })}
                    </span>
                  </div>
                ))}
              </div>
            </section>
          )}
        </div>

        <aside className="space-y-4">
          <section className="rounded-box border border-base-300 bg-base-100 p-4">
            <p className="text-xs font-semibold uppercase tracking-[0.14em] text-base-content/50">
              {t('detail.next-action')}
            </p>
            {experiment.state === 'draft' && (
              <div className="mt-3">
                <p className="text-sm font-medium">
                  {t('detail.freeze-title')}
                </p>
                <p className="mt-1 text-xs text-base-content/60">
                  {t('detail.freeze-impact', {
                    count: entries.length,
                    currency: context.currency,
                    daily: context.dailyBudget,
                    days: durationDays || '?',
                    total: Number.isFinite(plannedImpact)
                      ? plannedImpact.toFixed(2)
                      : '0.00',
                  })}
                </p>
                <label className="mt-3 flex cursor-pointer items-start gap-2 text-xs">
                  <input
                    type="checkbox"
                    className="checkbox checkbox-warning checkbox-sm"
                    checked={freezeAcknowledged}
                    onChange={(event) =>
                      setFreezeAcknowledged(event.target.checked)
                    }
                  />
                  <span>{t('detail.freeze-ack')}</span>
                </label>
                <button
                  type="button"
                  className="btn btn-warning btn-sm mt-3 w-full"
                  disabled={
                    !freezeAcknowledged ||
                    entries.length < 2 ||
                    workspace.commands.freeze.isPending ||
                    !analysis.data ||
                    analysis.data.analyzed_revision !== experiment.revision ||
                    !allGroupsResolved
                  }
                  onClick={() => void freeze()}
                >
                  <LockClosedIcon className="h-4 w-4" /> {t('detail.freeze')}
                </button>
              </div>
            )}
            {experiment.state === 'frozen' && (
              <div className="mt-3">
                <p className="text-sm font-medium">
                  {t('detail.attach-title')}
                </p>
                <p className="mt-1 text-xs text-base-content/60">
                  {t('detail.attach-intro')}
                </p>
                <button
                  type="button"
                  className="btn btn-primary btn-sm mt-3 w-full"
                  disabled={!allBound || workspace.commands.start.isPending}
                  onClick={() =>
                    void run(() =>
                      workspace.commands.start.execute({
                        actionInstanceId: newActionId(),
                        pathParams: { experiment_id: experiment.id },
                        body: { expected_revision: experiment.revision },
                      })
                    )
                  }
                >
                  {t('detail.start')}
                </button>
              </div>
            )}
            {experiment.state === 'running' && (
              <div className="mt-3">
                <p className="text-sm font-medium">
                  {t('detail.evaluate-title')}
                </p>
                <p className="mt-1 text-xs text-base-content/60">
                  {t('detail.evaluate-intro')}
                </p>
                <button
                  type="button"
                  className="btn btn-primary btn-sm mt-3 w-full"
                  disabled={workspace.commands.judge.isPending}
                  onClick={() =>
                    void run(() =>
                      workspace.commands.judge.execute({
                        actionInstanceId: newActionId(),
                        pathParams: { experiment_id: experiment.id },
                        body: { expected_revision: experiment.revision },
                      })
                    )
                  }
                >
                  <BeakerIcon className="h-4 w-4" /> {t('detail.judge')}
                </button>
              </div>
            )}
            {experiment.state === 'closed' && (
              <div className="mt-3 flex items-start gap-2 text-sm text-success">
                <CheckCircleIcon className="mt-0.5 h-5 w-5 shrink-0" />
                <span>{t('detail.closed')}</span>
              </div>
            )}
          </section>

          <section className="rounded-box border border-base-300 bg-base-100 p-4 text-xs">
            <h3 className="font-semibold text-sm">
              {t('detail.status-title')}
            </h3>
            <dl className="mt-3 space-y-2">
              <div className="flex justify-between gap-3">
                <dt className="text-base-content/55">
                  {t('detail.judgements')}
                </dt>
                <dd>{experiment.judgements?.length ?? 0}</dd>
              </div>
              <div className="flex justify-between gap-3">
                <dt className="text-base-content/55">
                  {t('detail.open-proposals')}
                </dt>
                <dd>
                  {experiment.proposals?.filter(
                    (item) => item.state === 'created'
                  ).length ?? 0}
                </dd>
              </div>
              <div className="flex justify-between gap-3">
                <dt className="text-base-content/55">
                  {t('detail.confirmed-decisions')}
                </dt>
                <dd>{experiment.decisions?.length ?? 0}</dd>
              </div>
            </dl>
          </section>

          <section className="rounded-box border border-base-300 bg-base-100 p-4">
            <div className="space-y-1">
              <label className="text-sm font-medium" htmlFor="meta-mode-select">
                {t('detail.evidence-mode')}
              </label>
              <select
                id="meta-mode-select"
                className="w-full rounded border px-2 py-1 text-sm"
                value={metaMode}
                onChange={(event) =>
                  setMetaMode(event.target.value as MetaMode)
                }
              >
                <option value="customer_sandbox">
                  {t('detail.mode-sandbox')}
                </option>
                <option value="controlled_mock">{t('detail.mode-mock')}</option>
              </select>
            </div>
          </section>

          {(experiment.state === 'frozen' ||
            launchPreviewId ||
            launchProvisioningId) && (
            <section className="rounded-box border border-base-300 bg-base-100 p-4">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <p className="text-xs font-semibold uppercase tracking-[0.14em] text-secondary">
                    {t('launch.eyebrow', { mode: metaMode })}
                  </p>
                  <h3 className="mt-1 text-sm font-semibold">
                    {t('launch.title')}
                  </h3>
                </div>
                <span className="badge badge-outline badge-sm">
                  {t('launch.fake-adapter')}
                </span>
              </div>

              {persistedPreview.isLoading && !launchPreview ? (
                <div className="mt-4 skeleton h-24 w-full" aria-busy="true" />
              ) : !launchPreview ? (
                <div className="mt-4 space-y-2">
                  {!launchCandidateModesValid && (
                    <div
                      className="alert alert-error py-2 text-xs"
                      role="alert"
                    >
                      <ExclamationTriangleIcon className="h-4 w-4" />
                      <span>{t('launch.mixed-modes')}</span>
                    </div>
                  )}
                  <input
                    className="input input-bordered input-sm w-full"
                    aria-label={t('launch.connection-aria')}
                    placeholder={t('launch.connection-placeholder')}
                    value={launchConnectionId}
                    onChange={(event) =>
                      setLaunchConnectionId(event.target.value)
                    }
                  />
                  <input
                    className="input input-bordered input-sm w-full"
                    aria-label={t('launch.ad-account-aria')}
                    placeholder="act_..."
                    value={launchAdAccountId}
                    onChange={(event) =>
                      setLaunchAdAccountId(event.target.value)
                    }
                  />
                  <label className="form-control">
                    <span className="label-text mb-1 text-xs">
                      {t('launch.daily-budget-minor')}
                    </span>
                    <input
                      className="input input-bordered input-sm w-full"
                      type="number"
                      min="1"
                      value={launchDailyBudgetMinor}
                      onChange={(event) =>
                        setLaunchDailyBudgetMinor(event.target.value)
                      }
                    />
                  </label>
                  <p className="text-xs text-base-content/55">
                    {t('launch.preview-note', { count: candidates.length })}
                  </p>
                  <button
                    type="button"
                    className="btn btn-secondary btn-sm w-full"
                    disabled={
                      !experiment.version_id ||
                      !launchCandidateModesValid ||
                      !launchConnectionId ||
                      !launchAdAccountId ||
                      Number(launchDailyBudgetMinor) < 1 ||
                      workspace.commands.createLaunchPreview.isPending
                    }
                    onClick={() => void createLaunchPreview()}
                  >
                    {t('launch.create-preview')}
                  </button>
                </div>
              ) : (
                <div className="mt-4">
                  {(!previewMatchesExperiment || previewExpired) && (
                    <div className="alert alert-warning mb-3 py-2 text-xs">
                      <ExclamationTriangleIcon className="h-4 w-4" />
                      <span>
                        {previewExpired
                          ? t('launch.expired')
                          : t('launch.mismatch')}
                      </span>
                    </div>
                  )}
                  <div className="rounded-lg bg-base-200/60 p-3 text-xs">
                    <div className="flex items-center justify-between gap-3">
                      <span>{t('launch.preview-state')}</span>
                      <span
                        className={`badge badge-sm ${launchPreview.state === 'confirmed' ? 'badge-success' : 'badge-warning'}`}
                      >
                        {previewExpired
                          ? t('status.expired')
                          : t(`status.${launchPreview.state}`, {
                              defaultValue: launchPreview.state,
                            })}
                      </span>
                    </div>
                    <dl className="mt-3 space-y-2">
                      <div className="flex justify-between gap-3">
                        <dt className="text-base-content/55">
                          {t('launch.mode')}
                        </dt>
                        <dd className="font-mono">{launchPreview.meta_mode}</dd>
                      </div>
                      <div className="flex justify-between gap-3">
                        <dt className="text-base-content/55">
                          {t('launch.candidates')}
                        </dt>
                        <dd>{launchPreview.candidate_count}</dd>
                      </div>
                      <div className="flex justify-between gap-3">
                        <dt className="text-base-content/55">
                          {t('launch.per-candidate-budget')}
                        </dt>
                        <dd>
                          {launchPreview.currency}{' '}
                          {(launchPreview.daily_budget_minor / 100).toFixed(2)}
                        </dd>
                      </div>
                      <div className="flex justify-between gap-3">
                        <dt className="text-base-content/55">
                          {t('launch.total-daily-impact')}
                        </dt>
                        <dd className="font-semibold">
                          {launchPreview.currency}{' '}
                          {(launchPreview.total_daily_cost_minor / 100).toFixed(
                            2
                          )}
                        </dd>
                      </div>
                      <div className="flex justify-between gap-3">
                        <dt className="text-base-content/55">
                          {t('launch.initial-status')}
                        </dt>
                        <dd className="font-semibold">PAUSED</dd>
                      </div>
                      <div className="flex justify-between gap-3">
                        <dt className="text-base-content/55">
                          {t('launch.binding-evidence')}
                        </dt>
                        <dd className="max-w-36 truncate font-mono">
                          {launchPreview.meta_binding_id}
                        </dd>
                      </div>
                    </dl>
                    <div className="mt-3 border-t border-base-300 pt-3">
                      <p className="font-semibold">{t('launch.manifest')}</p>
                      <ol className="mt-2 space-y-2">
                        {(launchPreview.candidates ?? []).map((candidate) => (
                          <li
                            key={candidate.id}
                            className="rounded border border-base-300 bg-base-100 p-2"
                          >
                            <div className="flex justify-between gap-2">
                              <span>
                                {t('launch.candidate', {
                                  number: candidate.candidate_index + 1,
                                })}
                              </span>
                              <span className="font-mono">
                                {candidate.asset_version_id}
                              </span>
                            </div>
                            <p className="mt-1 break-all font-mono text-[10px] text-base-content/50">
                              {candidate.experiment_version_candidate_id}
                            </p>
                            <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-all text-[10px] text-base-content/60">
                              {JSON.stringify(candidate.creative_config)}
                            </pre>
                          </li>
                        ))}
                      </ol>
                      <p className="mt-2 font-semibold">
                        {t('launch.campaign-config')}
                      </p>
                      <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-all rounded border border-base-300 bg-base-100 p-2 text-[10px] text-base-content/60">
                        {JSON.stringify(launchPreview.campaign_config)}
                      </pre>
                    </div>
                    <p className="mt-3 break-all font-mono text-[10px] text-base-content/45">
                      {t('launch.hash', { hash: launchPreview.preview_hash })}
                    </p>
                    <p className="mt-1 text-[10px] text-base-content/45">
                      {t('launch.expires', {
                        time: formatDateTime(
                          i18n.language,
                          launchPreview.expires_at
                        ),
                      })}
                    </p>
                  </div>

                  {launchPreview.state === 'draft' ? (
                    <>
                      <label className="mt-3 flex cursor-pointer items-start gap-2 text-xs">
                        <input
                          type="checkbox"
                          className="checkbox checkbox-secondary checkbox-sm"
                          checked={launchAcknowledged}
                          disabled={previewExpired || !previewMatchesExperiment}
                          onChange={(event) =>
                            setLaunchAcknowledged(event.target.checked)
                          }
                        />
                        <span>{t('launch.review-ack')}</span>
                      </label>
                      <button
                        type="button"
                        className="btn btn-secondary btn-sm mt-3 w-full"
                        disabled={
                          !launchAcknowledged ||
                          previewExpired ||
                          !previewMatchesExperiment ||
                          workspace.commands.confirmLaunchPreview.isPending
                        }
                        onClick={() => void confirmLaunchPreview()}
                      >
                        {t('launch.confirm')}
                      </button>
                    </>
                  ) : !launchProvisioningId ? (
                    <div className="mt-3">
                      <div className="flex items-start gap-2 text-xs text-success">
                        <CheckCircleIcon className="h-4 w-4 shrink-0" />
                        <span>{t('launch.confirmed')}</span>
                      </div>
                      {!launchConfirmation || confirmationExpired ? (
                        <p className="mt-3 rounded-box bg-warning/10 p-3 text-xs text-warning-content">
                          {t('launch.confirmation-expired')}
                        </p>
                      ) : null}
                      <label className="mt-3 flex cursor-pointer items-start gap-2 text-xs">
                        <input
                          type="checkbox"
                          className="checkbox checkbox-error checkbox-sm"
                          checked={executeAcknowledged}
                          onChange={(event) =>
                            setExecuteAcknowledged(event.target.checked)
                          }
                        />
                        <span>
                          {t('launch.execute-ack', {
                            candidates: launchPreview.candidate_count,
                            currency: launchPreview.currency,
                            amount: (
                              launchPreview.total_daily_cost_minor / 100
                            ).toFixed(2),
                            mode: metaMode,
                          })}
                        </span>
                      </label>
                      <button
                        type="button"
                        className="btn btn-error btn-sm mt-3 w-full"
                        disabled={
                          !executeAcknowledged ||
                          !launchConfirmation ||
                          confirmationExpired ||
                          previewExpired ||
                          !previewMatchesExperiment ||
                          workspace.commands.executeLaunchPreview.isPending
                        }
                        onClick={() => void executeLaunchPreview()}
                      >
                        {t('launch.execute')}
                      </button>
                    </div>
                  ) : null}

                  {(previewExpired ||
                    !previewMatchesExperiment ||
                    (launchPreview.state === 'confirmed' &&
                      (!launchConfirmation || confirmationExpired))) &&
                    !launchProvisioningId && (
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm mt-3 w-full"
                        onClick={() => {
                          setLocalLaunchPreview(undefined);
                          setLaunchConfirmation(undefined);
                          onLaunchRouteChange({
                            previewId: null,
                            provisioningId: null,
                          });
                        }}
                      >
                        {t('launch.fresh-preview')}
                      </button>
                    )}

                  {launchProvisioningId && (
                    <div className="mt-4 border-t border-base-300 pt-4">
                      <div className="flex items-center justify-between gap-2">
                        <p className="text-sm font-semibold">
                          {t('launch.provisioning-title')}
                        </p>
                        <button
                          type="button"
                          className="btn btn-ghost btn-xs"
                          onClick={() => void provisioning.refresh()}
                        >
                          {t('launch.refresh')}
                        </button>
                      </div>
                      {provisioning.isLoading && !provisioningData ? (
                        <div
                          className="mt-3 skeleton h-20 w-full"
                          aria-busy="true"
                        />
                      ) : provisioningData ? (
                        <div className="mt-3 text-xs">
                          <div className="flex items-center justify-between gap-2">
                            <span className="text-base-content/55">
                              {t('launch.provisioning')}
                            </span>
                            <span
                              className={`badge badge-sm ${provisioningData.provisioning_state === 'provisioned' ? 'badge-success' : provisioningData.provisioning_state === 'failed' || provisioningData.provisioning_state === 'partially_provisioned' ? 'badge-error' : 'badge-info'}`}
                              data-provisioning-state={
                                provisioningData.provisioning_state
                              }
                            >
                              {t(
                                `status.${provisioningData.provisioning_state}`,
                                {
                                  defaultValue:
                                    provisioningData.provisioning_state,
                                }
                              )}
                            </span>
                          </div>
                          <div className="mt-2 flex justify-between gap-2">
                            <span className="text-base-content/55">
                              {t('launch.activation')}
                            </span>
                            <strong className="uppercase">
                              {t(
                                `status.${provisioningData.activation_state}`,
                                {
                                  defaultValue:
                                    provisioningData.activation_state,
                                }
                              )}
                            </strong>
                          </div>
                          <ol className="mt-3 space-y-1">
                            {provisioningData.steps?.map((step, index) => (
                              <li
                                key={`${step.step_type}-${index}`}
                                className="flex justify-between gap-2 rounded bg-base-200/60 px-2 py-1"
                              >
                                <span>
                                  {t(`launch.step-${step.step_type}`)}
                                </span>
                                <span>
                                  {t(`status.${step.state}`, {
                                    defaultValue: step.state,
                                  })}
                                </span>
                              </li>
                            ))}
                          </ol>
                          {provisioningData.parity_check && (
                            <div
                              className={`mt-3 rounded-lg p-2 ${provisioningData.parity_check.passed ? 'bg-success/10 text-success' : 'bg-error/10 text-error'}`}
                            >
                              {t('launch.parity', {
                                result: provisioningData.parity_check.passed
                                  ? t('launch.parity-passed')
                                  : t('launch.parity-failed'),
                                verified: provisioningData.parity_check
                                  .status_parity_verified
                                  ? t('launch.verified')
                                  : t('launch.not-verified'),
                              })}
                            </div>
                          )}
                          {provisioningData.operation_state === 'uncertain' && (
                            <button
                              type="button"
                              className="btn btn-outline btn-sm mt-3 w-full"
                              disabled={
                                workspace.commands.recoverLaunchOperation
                                  .isPending
                              }
                              onClick={() => void recoverLaunch()}
                            >
                              {t('launch.reconcile')}
                            </button>
                          )}
                          <p className="mt-3 break-all font-mono text-[10px] text-base-content/45">
                            {t('launch.operation', {
                              id: provisioningData.operation_id,
                            })}
                          </p>
                        </div>
                      ) : null}
                    </div>
                  )}
                </div>
              )}
            </section>
          )}
        </aside>
      </div>
    </section>
  );
}

export function ExperimentsWorkspace() {
  const { t } = useTranslation('experiments');
  const router = useRouter();
  const selectedId =
    typeof router.query.experiment === 'string'
      ? router.query.experiment
      : null;
  const launchPreviewId =
    typeof router.query.launchPreview === 'string'
      ? router.query.launchPreview
      : null;
  const launchProvisioningId =
    typeof router.query.launchProvisioning === 'string'
      ? router.query.launchProvisioning
      : null;
  const workspace = useExperimentsWorkspace(selectedId);
  const [creating, setCreating] = useState(false);
  const [createProblem, setCreateProblem] = useState<ProblemDetails>();

  const approvedAssets = useDomainPaginatedQuery<
    'list_generation_assets',
    AssetVersionSummary
  >(
    'list_generation_assets',
    { review_value: 'approved' },
    { enabled: creating, pageSize: 100 }
  );

  const selectExperiment = (experimentId: string | null) => {
    const query = { ...router.query };
    if (experimentId) query.experiment = experimentId;
    else delete query.experiment;
    delete query.launchPreview;
    delete query.launchProvisioning;
    void router.replace({ pathname: router.pathname, query }, undefined, {
      shallow: true,
    });
  };

  const updateLaunchRoute = (values: {
    previewId?: string | null;
    provisioningId?: string | null;
  }) => {
    const query = { ...router.query };
    if (values.previewId === null) delete query.launchPreview;
    else if (values.previewId) query.launchPreview = values.previewId;
    if (values.provisioningId === null) delete query.launchProvisioning;
    else if (values.provisioningId)
      query.launchProvisioning = values.provisioningId;
    void router.replace({ pathname: router.pathname, query }, undefined, {
      shallow: true,
    });
  };

  const createExperiment = async (input: {
    name: string;
    productId: string;
    context: DraftContextFields;
    assetIds: string[];
  }) => {
    setCreateProblem(undefined);
    try {
      const created = await workspace.commands.create.execute({
        actionInstanceId: newActionId(),
        body: {
          product_id: input.productId,
          name: input.name,
          draft_context: toDraftContext(input.context),
        },
      });
      if (created.kind === 'problem') {
        setCreateProblem(created.problem);
        return;
      }
      if (created.kind !== 'success') return;

      let revision = created.data.revision;
      for (const assetId of input.assetIds) {
        const added = await workspace.commands.addEntry.execute({
          actionInstanceId: newActionId(),
          pathParams: { experiment_id: created.data.id },
          body: { asset_version_id: assetId, expected_revision: revision },
        });
        if (added.kind !== 'success') {
          if (added.kind === 'problem') setCreateProblem(added.problem);
          selectExperiment(created.data.id);
          await workspace.list.refresh();
          return;
        }
        revision = added.data.revision;
      }

      setCreating(false);
      selectExperiment(created.data.id);
      await workspace.list.refresh();
    } catch {
      setCreateProblem(networkProblem(t, 'problem.create-partial'));
    }
  };

  const listProblem = workspace.list.problem;
  const selectedExperiment = workspace.detail.data;
  const listItems = useMemo(() => workspace.list.items, [workspace.list.items]);

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-primary">
            {t('list.eyebrow')}
          </p>
          <h1 className="mt-1 text-2xl font-bold outline-none">
            {t('list.title')}
          </h1>
          <p className="mt-1 text-sm text-base-content/60">{t('list.intro')}</p>
        </div>
        <button
          type="button"
          className="btn btn-primary btn-sm"
          onClick={() => setCreating(true)}
        >
          <PlusIcon className="h-4 w-4" /> {t('list.new')}
        </button>
      </header>

      {creating ? (
        <NewExperimentPanel
          assets={approvedAssets.items}
          assetsLoading={approvedAssets.isLoading}
          problem={createProblem ?? approvedAssets.problem}
          isPending={
            workspace.commands.create.isPending ||
            workspace.commands.addEntry.isPending
          }
          onCancel={() => setCreating(false)}
          onCreate={createExperiment}
        />
      ) : (
        <div className="grid grid-cols-1 gap-5 lg:grid-cols-[17rem_minmax(0,1fr)]">
          <aside className="rounded-box h-fit border border-base-300 bg-base-100 p-3">
            <div className="flex items-center justify-between px-2 py-1">
              <h2 className="text-sm font-semibold">{t('list.all')}</h2>
              <button
                type="button"
                className="btn btn-ghost btn-xs"
                aria-label={t('list.refresh-aria')}
                onClick={() => void workspace.list.refresh()}
              >
                <ArrowPathIcon className="h-4 w-4" />
              </button>
            </div>

            {workspace.list.isLoading && listItems.length === 0 && (
              <div className="space-y-2 px-2 py-3" aria-busy="true">
                <div className="skeleton h-14 w-full" />
                <div className="skeleton h-14 w-full" />
              </div>
            )}
            {listProblem && listItems.length === 0 && (
              <div className="p-2 text-sm text-error" role="alert">
                <p>
                  <ProblemMessage problem={listProblem} />
                </p>
                <button
                  type="button"
                  className="btn btn-ghost btn-xs mt-2"
                  onClick={() => void workspace.list.refresh()}
                >
                  {t('list.retry')}
                </button>
              </div>
            )}
            {!workspace.list.isLoading &&
              !listProblem &&
              listItems.length === 0 && (
                <div className="p-4 text-center">
                  <BeakerIcon className="mx-auto h-7 w-7 text-base-content/30" />
                  <p className="mt-2 text-sm font-medium">
                    {t('list.empty-title')}
                  </p>
                  <p className="mt-1 text-xs text-base-content/55">
                    {t('list.empty-intro')}
                  </p>
                </div>
              )}
            <nav className="mt-2 space-y-1" aria-label={t('list.nav-aria')}>
              {listItems.map((experiment) => (
                <button
                  key={experiment.id}
                  type="button"
                  className={`w-full rounded-lg px-3 py-2 text-left ${selectedId === experiment.id ? 'bg-primary/10 text-primary' : 'hover:bg-base-200'}`}
                  onClick={() => selectExperiment(experiment.id)}
                >
                  <span className="block truncate text-sm font-medium">
                    {experiment.name}
                  </span>
                  <span className="mt-1 flex items-center justify-between gap-2 text-xs text-base-content/55">
                    <span>
                      {t(`status.${experiment.state}`, {
                        defaultValue: experiment.state,
                      })}
                    </span>
                    <span>
                      {t('list.revision-short', {
                        revision: experiment.revision,
                      })}
                    </span>
                  </span>
                </button>
              ))}
            </nav>
          </aside>

          <main className="min-w-0">
            {!selectedId && (
              <div className="rounded-box flex min-h-72 items-center justify-center border border-dashed border-base-300 p-8 text-center">
                <div>
                  <BeakerIcon className="mx-auto h-9 w-9 text-base-content/25" />
                  <h2 className="mt-3 font-semibold">
                    {t('list.select-title')}
                  </h2>
                  <p className="mt-1 text-sm text-base-content/55">
                    {t('list.select-intro')}
                  </p>
                </div>
              </div>
            )}
            {selectedId &&
              workspace.detail.isLoading &&
              !selectedExperiment && (
                <div className="space-y-4" aria-busy="true">
                  <div className="skeleton h-28 w-full" />
                  <div className="skeleton h-64 w-full" />
                </div>
              )}
            {selectedId && workspace.detail.problem && !selectedExperiment && (
              <div className="alert alert-error" role="alert">
                <ExclamationTriangleIcon className="h-5 w-5" />
                <span>
                  <ProblemMessage problem={workspace.detail.problem} />
                </span>
                <button
                  type="button"
                  className="btn btn-sm"
                  onClick={() => void workspace.detail.refresh()}
                >
                  {t('list.retry')}
                </button>
              </div>
            )}
            {selectedExperiment && (
              <ExperimentDetailPanel
                key={`${selectedExperiment.id}:${selectedExperiment.revision}`}
                experiment={selectedExperiment}
                workspace={workspace}
                launchPreviewId={launchPreviewId}
                launchProvisioningId={launchProvisioningId}
                onLaunchRouteChange={updateLaunchRoute}
              />
            )}
          </main>
        </div>
      )}
    </div>
  );
}
