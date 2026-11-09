import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'next-i18next';
import { useDomainCommand } from '@/hooks/domain/useDomainCommand';
import { newActionId } from '@/components/generation/shared/newActionId';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import { TechnicalDetails } from '@/components/workspace/MarketingTechnicalInfo';
import {
  actionReasonMessage,
  LocalizationProblem,
  LocalizationSection as Section,
  shortHash,
} from './LocalizationUi';

type Workspace = components['schemas']['LocalizationWorkspace'];
type Batch = components['schemas']['LocalizationBatchDetail'];
type Claim = components['schemas']['LocalizationSourceClaim'];
type Market = components['schemas']['LocalizationTruthMarket'];

type ClaimAction = 'replace' | 'remove' | 'preserve';

const ANALYSIS_RECIPE_HASH = 'localization-analysis-v1';

function actionEligibility(batch: Batch, action: string) {
  return batch.allowed_actions?.find(
    (candidate) => candidate.action === action
  );
}

function sameLocale(source: string, target: string): boolean {
  return source.trim().toLowerCase() === target.trim().toLowerCase();
}

function matchingFacts(market: Market, claim: Claim) {
  return (market.facts ?? []).filter(
    (fact) =>
      (claim.candidate_fact_types ?? []).includes(fact.fact_type) &&
      fact.scope_key === claim.scope_key
  );
}

function initialAction(market: Market, claim: Claim): ClaimAction {
  if (matchingFacts(market, claim).length > 0) return 'replace';
  if (claim.market_sensitive) return 'remove';
  return 'preserve';
}

export function LocalizationPreparation({
  workspace,
  batch,
  onRefresh,
}: {
  workspace: Workspace;
  batch: Batch;
  onRefresh: () => Promise<void>;
}) {
  const { t } = useTranslation('localization');
  const startCommand = useDomainCommand('start_localization_preparation');
  const decisionCommand = useDomainCommand(
    'submit_localization_claim_decisions'
  );
  const policies = useMemo(
    () => workspace.truth_resolution_policies ?? [],
    [workspace.truth_resolution_policies]
  );
  const [policyVersionId, setPolicyVersionId] = useState('');
  const [selectedPair, setSelectedPair] = useState('');
  const [claimActions, setClaimActions] = useState<Record<string, ClaimAction>>(
    {}
  );

  useEffect(() => {
    if (!policyVersionId && policies[0]) setPolicyVersionId(policies[0].id);
  }, [policies, policyVersionId]);

  const inventories = batch.inventories ?? [];
  const analysisValidationFindings = Array.isArray(
    batch.frozen_recipe?.analysis_validation_findings
  )
    ? batch.frozen_recipe.analysis_validation_findings.filter(
        (finding): finding is string => typeof finding === 'string'
      )
    : [];
  const completedPairs = new Set(
    (batch.claim_decision_sets ?? []).map(
      (decisionSet) =>
        `${decisionSet.source_asset_version_id}:${decisionSet.target_market}`
    )
  );
  const unresolvedPairs = (batch.sources ?? []).flatMap((source) =>
    (batch.markets ?? [])
      .filter(
        (market) =>
          !completedPairs.has(`${source.asset_version_id}:${market.market}`)
      )
      .map((market) => ({
        key: `${source.asset_version_id}:${market.market}`,
        source,
        market,
        inventory: inventories.find(
          (candidate) =>
            candidate.id === source.inventory_id &&
            candidate.analysis_recipe_hash === source.analysis_recipe_hash
        ),
      }))
  );

  useEffect(() => {
    if (!unresolvedPairs.some((pair) => pair.key === selectedPair)) {
      setSelectedPair(unresolvedPairs[0]?.key ?? '');
    }
  }, [selectedPair, unresolvedPairs]);

  const activePair = unresolvedPairs.find((pair) => pair.key === selectedPair);
  const startAction = actionEligibility(batch, 'start_preparation');
  const decisionsAction = actionEligibility(batch, 'submit_claim_decisions');
  const targetContextsReady = (batch.markets ?? []).every(
    (market) =>
      typeof market.locale === 'string' &&
      market.locale.length > 0 &&
      typeof market.country === 'string' &&
      market.country.length > 0 &&
      typeof market.currency === 'string' &&
      market.currency.length > 0
  );
  const configurationReady = Boolean(policyVersionId && targetContextsReady);

  async function startPreparation() {
    if (!configurationReady) return;
    const result = await startCommand.execute({
      pathParams: { batch_id: batch.id },
      actionInstanceId: newActionId(),
      body: {
        truth_resolution_policy_version_id: policyVersionId,
        analysis_recipe_hash: ANALYSIS_RECIPE_HASH,
        analysis_provider: 'controlled-mock-vision',
        analysis_model: 'localization-analysis-v1',
        analysis_adapter_version: 'mock-v1',
      },
    });
    if (result.kind === 'success') await onRefresh();
  }

  function actionFor(claim: Claim): ClaimAction {
    if (!activePair) return 'preserve';
    return (
      claimActions[`${activePair.key}:${claim.claim_index}`] ??
      initialAction(activePair.market, claim)
    );
  }

  async function submitDecisions() {
    if (!activePair?.inventory) return;
    const claims = activePair.inventory.claims ?? [];
    if (claims.length === 0) return;
    const sourceLocale = activePair.source.source_market_snapshot?.locale;
    const targetLocale = activePair.market.locale;
    if (!sourceLocale || !targetLocale) return;
    const decisions = claims.map((claim) => {
      const action = actionFor(claim);
      const replacement = matchingFacts(activePair.market, claim)[0];
      return {
        claim_index: claim.claim_index,
        action,
        replacement_fact_id: action === 'replace' ? replacement?.id : undefined,
        removal_strategy:
          action === 'remove'
            ? {
                kind: 'mask',
                region: claim.region ?? { scope_key: claim.scope_key },
              }
            : undefined,
        rationale: `Explicit ${action} decision in Localization workspace`,
      };
    });
    const result = await decisionCommand.execute({
      pathParams: { batch_id: batch.id },
      actionInstanceId: newActionId(),
      ifMatch: batch.revision,
      body: {
        asset_version_id: activePair.source.asset_version_id,
        target_market: activePair.market.market,
        analysis_recipe_hash: activePair.inventory.analysis_recipe_hash,
        decisions,
        copy_policy_snapshot: {
          market_policy_version_id: activePair.market.market_policy_version_id,
          locked_fact_source: batch.truth_package_id,
        },
        source_locale: sourceLocale,
        target_locale: targetLocale,
        translation_recipe: sameLocale(sourceLocale, targetLocale)
          ? undefined
          : {
              execution_mode: 'mock',
              provider: 'controlled-mock-translation',
              model: 'localization-copy-v1',
              adapter_version: 'mock-v1',
            },
        requested_evidence_tier: 'mock',
      },
    });
    if (result.kind === 'success') await onRefresh();
  }

  return (
    <Section
      title={t('preparation.title')}
      subtitle={t('preparation.subtitle')}
    >
      {batch.phase === 'draft' ? (
        <div className="space-y-3">
          {!configurationReady ? (
            <div className="alert alert-warning text-sm">
              {t('preparation.not-ready')}
            </div>
          ) : null}
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            <label className="form-control">
              <span className="label-text mb-1 text-xs">
                {t('preparation.policy-version')}
              </span>
              <select
                className="select select-bordered select-sm"
                aria-label={t('preparation.policy-version')}
                value={policyVersionId}
                onChange={(event) => setPolicyVersionId(event.target.value)}
              >
                <option value="">{t('preparation.select-version')}</option>
                {policies.map((policy) => (
                  <option key={policy.id} value={policy.id}>
                    {policy.display_name} · {policy.id}
                  </option>
                ))}
              </select>
              <span className="label-text-alt mt-1 text-xs opacity-70">
                {t('preparation.sources-from-policy')}
              </span>
            </label>
          </div>
          <button
            className="btn btn-primary btn-sm"
            disabled={
              !startAction?.allowed ||
              !configurationReady ||
              startCommand.isPending
            }
            onClick={startPreparation}
          >
            {t('preparation.start')}
          </button>
          {!startAction?.allowed ? (
            <span className="ml-2 text-xs opacity-60">
              {t('preparation.unavailable', {
                reason: startAction?.reason_code
                  ? actionReasonMessage(t, startAction.reason_code)
                  : t('preparation.server-policy'),
              })}
            </span>
          ) : null}
          <LocalizationProblem
            problem={startCommand.problem}
            className="mt-3"
          />
        </div>
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap gap-2 text-xs">
            <span className="badge badge-outline">
              {t('preparation.stage', {
                stage: batch.preparation_stage
                  ? t(`preparation.state-${batch.preparation_stage}`)
                  : t('status.pending'),
              })}
            </span>
            <span className="badge badge-outline">
              {t('preparation.inventories', { count: inventories.length })}
            </span>
            <span className="badge badge-outline">
              {t('preparation.decision-sets', {
                count: batch.claim_decision_sets?.length ?? 0,
              })}
            </span>
          </div>
          {analysisValidationFindings.length > 0 ? (
            <div className="alert alert-error text-sm">
              {t('preparation.validation-findings', {
                findings: analysisValidationFindings.join(t('list-separator')),
              })}
            </div>
          ) : null}
          {inventories.length > 0 ? (
            <div className="space-y-1 break-words text-xs opacity-70">
              {inventories.map((inventory) => (
                <div key={inventory.id}>
                  {t('preparation.inventory-line', {
                    id: inventory.id,
                    recipe: inventory.analysis_recipe_hash,
                    version: inventory.version_number,
                  })}
                </div>
              ))}
            </div>
          ) : null}
          {unresolvedPairs.length === 0 ? (
            <div className="alert alert-success text-sm">
              {t('preparation.all-resolved')}
            </div>
          ) : (
            <>
              <label className="form-control">
                <span className="label-text mb-1 text-xs">
                  {t('preparation.pair')}
                </span>
                <select
                  className="select select-bordered select-sm"
                  aria-label={t('preparation.pair')}
                  value={selectedPair}
                  onChange={(event) => setSelectedPair(event.target.value)}
                >
                  {unresolvedPairs.map((pair) => (
                    <option key={pair.key} value={pair.key}>
                      {pair.source.asset_version_id} → {pair.market.market}
                    </option>
                  ))}
                </select>
              </label>
              {!activePair?.inventory ? (
                <div className="alert alert-info text-sm">
                  {t('preparation.analysis-pending')}
                </div>
              ) : (
                <div className="space-y-2">
                  <div className="break-words text-xs opacity-70">
                    {t('preparation.inventory-evidence', {
                      id: activePair.inventory.id,
                      evidence: shortHash(activePair.inventory.evidence_hash),
                    })}
                  </div>
                  <TechnicalDetails
                    className="text-xs"
                    facts={[
                      [t('preparation.inventory-id'), activePair.inventory.id],
                      [
                        t('preparation.evidence-hash'),
                        activePair.inventory.evidence_hash,
                      ],
                    ]}
                  />
                  {(activePair.inventory.claims ?? []).map((claim) => {
                    const facts = matchingFacts(activePair.market, claim);
                    const key = `${activePair.key}:${claim.claim_index}`;
                    return (
                      <div
                        key={key}
                        className="rounded-lg border border-base-300 p-3 text-sm"
                      >
                        <div className="flex flex-wrap items-start justify-between gap-2">
                          <div>
                            <strong>{claim.claim_type}</strong> ·{' '}
                            {claim.scope_key}
                            <div className="text-xs opacity-70">
                              {claim.ocr_text ?? t('preparation.no-ocr')}
                            </div>
                          </div>
                          <div className="text-xs">
                            {t('preparation.claim-meta', {
                              confidence: claim.confidence ?? t('unknown'),
                              risk: claim.risk ?? t('unknown'),
                            })}
                            {claim.conflicting
                              ? ` · ${t('preparation.conflicting')}`
                              : ''}
                          </div>
                        </div>
                        <label className="form-control mt-2 max-w-xs">
                          <span className="label-text text-xs">
                            {t('preparation.decision')}
                          </span>
                          <select
                            className="select select-bordered select-xs"
                            aria-label={t('preparation.decision-for-claim', {
                              index: claim.claim_index,
                            })}
                            value={actionFor(claim)}
                            onChange={(event) =>
                              setClaimActions((current) => ({
                                ...current,
                                [key]: event.target.value as ClaimAction,
                              }))
                            }
                          >
                            {facts.length > 0 ? (
                              <option value="replace">
                                {t('preparation.replace')}
                              </option>
                            ) : null}
                            <option value="remove">
                              {t('preparation.remove')}
                            </option>
                            {!claim.market_sensitive ? (
                              <option value="preserve">
                                {t('preparation.preserve')}
                              </option>
                            ) : null}
                          </select>
                        </label>
                        {actionFor(claim) === 'replace' ? (
                          <div className="mt-2 rounded bg-base-200 p-2 text-xs">
                            {t('preparation.locked-fact', {
                              fact:
                                facts[0]?.id ?? t('preparation.missing-fact'),
                              value: facts[0]
                                ? JSON.stringify(facts[0].value)
                                : t('preparation.value-unavailable'),
                            })}
                          </div>
                        ) : null}
                      </div>
                    );
                  })}
                  <button
                    className="btn btn-primary btn-sm"
                    disabled={
                      !decisionsAction?.allowed ||
                      (activePair.inventory.claims ?? []).length === 0 ||
                      !activePair.source.source_market_snapshot?.locale ||
                      !activePair.market.locale ||
                      decisionCommand.isPending
                    }
                    onClick={submitDecisions}
                  >
                    {t('preparation.submit')}
                  </button>
                  {!decisionsAction?.allowed ? (
                    <span className="ml-2 text-xs opacity-60">
                      {t('preparation.unavailable', {
                        reason: decisionsAction?.reason_code
                          ? actionReasonMessage(t, decisionsAction.reason_code)
                          : t('preparation.server-policy'),
                      })}
                    </span>
                  ) : null}
                </div>
              )}
            </>
          )}
          <LocalizationProblem
            problem={decisionCommand.problem}
            className="mt-3"
          />
        </div>
      )}
    </Section>
  );
}
