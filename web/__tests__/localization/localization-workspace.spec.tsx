/**
 * @jest-environment jsdom
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import {
  LocalizationItemInspector,
  LocalizationWorkspace,
  NewBatchPanel,
  TruthPanel,
} from '@/components/localization/LocalizationWorkspace';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import { LocalizationPreparation } from '@/components/localization/LocalizationPreparation';

const commandExecutors: Record<string, jest.Mock> = {};

jest.mock('@/hooks/domain/useDomainCommand', () => ({
  useDomainCommand: (operation: string) => {
    commandExecutors[operation] ??= jest.fn();
    return {
      execute: commandExecutors[operation],
      isPending: false,
      problem: undefined,
    };
  },
}));

// The whole page reads its batch through these; only the batch is served.
const mockQueryData: Record<string, unknown> = {};

jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: (operation: string) => ({
    data: mockQueryData[operation],
    isLoading: false,
    problem: undefined,
    refresh: jest.fn(),
  }),
}));

jest.mock('@/hooks/domain/useOperation', () => ({
  useOperation: () => ({
    operation: undefined,
    isTerminal: false,
    problem: undefined,
  }),
}));

jest.mock('next/router', () => ({
  useRouter: () => ({
    query: { batch: 'batch-1' },
    pathname: '/w/[slug]/localization',
    replace: jest.fn(),
  }),
}));

type Batch = components['schemas']['LocalizationBatchDetail'];
type Item = components['schemas']['LocalizationItemDetail'];
type Workspace = components['schemas']['LocalizationWorkspace'];

function workspace(): Workspace {
  return {
    role: 'owner',
    eligible_sources: [
      {
        evidence_origin: 'legacy_experiment',
        asset_version_id: 'asset-source-v1',
        decision_id: 'decision-1',
        experiment_id: 'experiment-1',
        experiment_version_id: 'experiment-version-1',
        experiment_version_candidate_id: 'candidate-1',
        source_generation_mode: 'mock',
        experiment_evidence_mode: 'mock',
        product_version_id: 'product-v1',
        offer_version_id: 'offer-v1',
        source_market: 'US',
        placement: 'feed',
        source_market_snapshot: {
          market: 'US',
          placement: 'feed',
          locale: 'en-US',
          country: 'US',
          currency: 'USD',
        },
        source_context_problem: null,
        asset: { language: 'en-US' },
      },
    ],
    markets: [
      {
        market: 'DE',
        locale: 'de-DE',
        country: 'DE',
        currency: 'EUR',
        market_policy_version_id: 'market-policy-v1',
        requires_local_copy: true,
        base_tone: 'direct',
      },
    ],
    truth_resolution_policies: [
      {
        id: 'truth-policy-v1',
        entry_id: 'truth-policy-entry',
        entry_type: 'truth_resolution_policy',
        display_name: 'Default Truth policy',
        fact_policies: [
          { fact_type: 'availability', on_failure: 'skip' },
          { fact_type: 'price', on_failure: 'block' },
          { fact_type: 'product_claim', on_failure: 'degrade' },
        ],
      },
    ],
    data_sources: [
      {
        id: 'data-source-v1',
        entry_id: 'data-source-entry',
        entry_type: 'data_source',
        display_name: 'Controlled facts',
      },
    ],
  };
}

function batch(overrides: Partial<Batch> = {}): Batch {
  return {
    id: 'batch-1',
    phase: 'ready',
    outcome: 'pending',
    product_version_id: 'product-v1',
    offer_version_id: 'offer-v1',
    provider_execution_mode: 'mock',
    source_count: 1,
    market_count: 1,
    item_count: 0,
    revision: 3,
    created_at: '2026-09-09T00:00:00Z',
    updated_at: '2026-09-09T00:00:00Z',
    truth_data_mode: 'mock',
    max_attempts: 3,
    ...overrides,
  };
}

function item(overrides: Partial<Item> = {}): Item {
  return {
    id: 'item-1',
    batch_id: 'batch-1',
    state: 'retry_wait',
    target_market: 'DE',
    source_asset_version_id: 'asset-source-v1',
    decision_id: 'decision-1',
    truth_package_id: 'truth-1',
    truth_package_market_id: 'truth-market-1',
    market_policy_version_id: 'market-policy-v1',
    inventory_id: 'inventory-1',
    claim_decision_set_id: 'claims-1',
    copy_version_id: 'copy-1',
    attempt_count: 3,
    max_attempts: 3,
    revision: 4,
    lineage: {
      source_asset_version_id: 'asset-source-v1',
      decision_id: 'decision-1',
      offer_version_id: 'offer-v1',
      truth_package_id: 'truth-1',
      copy_version_id: 'copy-1',
      localized_asset_version_id: null,
    },
    allowed_actions: [
      { action: 'retry', allowed: false, reason_code: 'attempts_exhausted' },
      { action: 'resume', allowed: false, reason_code: 'item_not_paused' },
    ],
    ...overrides,
  };
}

/** A preparing batch whose one source has an analysed price claim for DE. */
function claimReviewBatch(overrides: Partial<Batch> = {}): Batch {
  return batch({
    phase: 'preparing',
    preparation_stage: 'claim_review',
    truth_package_id: 'truth-1',
    truth_freshness: 'current',
    sources: [
      {
        id: 'source-1',
        asset_version_id: 'asset-source-v1',
        decision_id: 'decision-1',
        experiment_id: 'experiment-1',
        experiment_version_id: 'experiment-version-1',
        source_generation_mode: 'mock',
        experiment_evidence_mode: 'mock',
        source_market_snapshot: {
          market: 'US',
          placement: 'feed',
          locale: 'en-US',
          country: 'US',
          currency: 'USD',
        },
        analysis_recipe_hash: 'localization-analysis-v1',
        inventory_id: 'inventory-1',
      },
    ],
    markets: [
      {
        market: 'DE',
        locale: 'de-DE',
        country: 'DE',
        currency: 'EUR',
        market_policy_version_id: 'market-policy-v1',
        readiness: 'ready',
        facts: [
          {
            id: 'fact-price-1',
            fact_type: 'price',
            scope_key: 'hero-price',
            value: { amount: '19.99', currency: 'EUR' },
            external_object_ref: 'mock:price',
            accepted_source_attempt_id: 'attempt-price-1',
            evidence_hash: 'fact-evidence',
            observed_at: '2026-09-09T00:00:00Z',
            valid_until: '2026-10-09T00:00:00Z',
          },
        ],
      },
    ],
    inventories: [
      {
        id: 'inventory-1',
        asset_version_id: 'asset-source-v1',
        analysis_recipe_hash: 'localization-analysis-v1',
        version_number: 1,
        provider: 'controlled-mock-vision',
        model: 'localization-analysis-v1',
        analysis_execution_mode: 'mock',
        evidence_hash: 'inventory-evidence',
        claims: [
          {
            claim_index: 0,
            claim_type: 'price_claim',
            candidate_fact_types: ['price'],
            market_sensitive: true,
            scope_key: 'hero-price',
            ocr_text: '$19.99',
            confidence: 0.99,
            risk: 'low',
            conflicting: false,
          },
        ],
      },
    ],
    claim_decision_sets: [],
    allowed_actions: [{ action: 'submit_claim_decisions', allowed: true }],
    ...overrides,
  });
}

describe('Localization workspace evidence views', () => {
  beforeEach(() => {
    for (const executor of Object.values(commandExecutors))
      executor.mockReset();
    for (const operation of Object.keys(mockQueryData))
      delete mockQueryData[operation];
  });

  it('starts persisted Truth and analysis with exact policy, source, offer, and markets', async () => {
    commandExecutors.start_localization_preparation = jest
      .fn()
      .mockResolvedValue({
        kind: 'success',
        data: { operation_id: 'op-prepare' },
      });
    const onRefresh = jest.fn().mockResolvedValue(undefined);
    render(
      <LocalizationPreparation
        workspace={workspace()}
        batch={batch({
          phase: 'draft',
          source_market_snapshot: { market: 'US', locale: 'en-US' },
          sources: [
            {
              id: 'source-1',
              asset_version_id: 'asset-source-v1',
              decision_id: 'decision-1',
              experiment_id: 'experiment-1',
              experiment_version_id: 'experiment-version-1',
              source_generation_mode: 'mock',
              experiment_evidence_mode: 'mock',
              source_market_snapshot: {
                market: 'US',
                placement: 'feed',
                locale: 'en-US',
                country: 'US',
                currency: 'USD',
              },
              reusable_inventories: [
                {
                  id: 'inventory-reusable-v1',
                  analysis_recipe_hash: 'localization-analysis-v1',
                  version_number: 1,
                  claim_types: [
                    'bundle_price',
                    'certification_claim',
                    'discount',
                    'headline',
                    'shipping',
                  ],
                },
              ],
            },
          ],
          markets: [
            {
              market: 'DE',
              locale: 'de-DE',
              country: 'DE',
              currency: 'EUR',
              market_policy_version_id: 'market-policy-v1',
              readiness: 'pending',
            },
          ],
          allowed_actions: [{ action: 'start_preparation', allowed: true }],
        })}
        onRefresh={onRefresh}
      />
    );

    fireEvent.click(
      screen.getByRole('button', { name: /resolve truth and analyze sources/i })
    );

    await waitFor(() =>
      expect(commandExecutors.start_localization_preparation).toHaveBeenCalled()
    );
    const request =
      commandExecutors.start_localization_preparation.mock.calls[0][0];
    expect(request.pathParams).toEqual({ batch_id: 'batch-1' });
    expect(request.body).toMatchObject({
      truth_resolution_policy_version_id: 'truth-policy-v1',
      data_source_version_ids: ['data-source-v1'],
    });
    expect(request.body).not.toHaveProperty('truth_command');
    expect(onRefresh).toHaveBeenCalled();
  });

  it('submits one complete immutable ClaimDecisionSet for an exact source-market pair', async () => {
    commandExecutors.submit_localization_claim_decisions = jest
      .fn()
      .mockResolvedValue({
        kind: 'success',
        data: { operation_id: 'op-copy' },
      });
    render(
      <LocalizationPreparation
        workspace={workspace()}
        batch={claimReviewBatch()}
        onRefresh={jest.fn().mockResolvedValue(undefined)}
      />
    );

    fireEvent.click(
      screen.getByRole('button', { name: /submit immutable claim decisions/i })
    );

    await waitFor(() =>
      expect(
        commandExecutors.submit_localization_claim_decisions
      ).toHaveBeenCalled()
    );
    const request =
      commandExecutors.submit_localization_claim_decisions.mock.calls[0][0];
    expect(request.body).toMatchObject({
      asset_version_id: 'asset-source-v1',
      target_market: 'DE',
      analysis_recipe_hash: 'localization-analysis-v1',
      decisions: [
        {
          claim_index: 0,
          action: 'replace',
          replacement_fact_id: 'fact-price-1',
        },
      ],
      source_locale: 'en-US',
      target_locale: 'de-DE',
      requested_evidence_tier: 'mock',
    });
    expect(request.body.translation_recipe).toMatchObject({
      execution_mode: 'mock',
      provider: 'controlled-mock-translation',
    });
  });

  it('words why claim decisions cannot be submitted yet', () => {
    render(
      <LocalizationPreparation
        workspace={workspace()}
        batch={claimReviewBatch({
          preparation_stage: 'analysis',
          allowed_actions: [
            {
              action: 'submit_claim_decisions',
              allowed: false,
              reason_code: 'claim_review_not_ready',
            },
          ],
        })}
        onRefresh={jest.fn().mockResolvedValue(undefined)}
      />
    );

    expect(
      screen.getByText(
        'Unavailable: claim review has not opened, or source analysis is still running'
      )
    ).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/claim_review_not_ready/);
  });

  it('words why a batch cannot be confirmed', () => {
    mockQueryData.get_localization_batch = batch({
      phase: 'closed',
      allowed_actions: [
        { action: 'confirm', allowed: false, reason_code: 'batch_not_ready' },
        { action: 'cancel', allowed: false, reason_code: 'batch_terminal' },
      ],
    });
    render(<LocalizationWorkspace />);

    expect(
      screen.getByText(
        'Confirm unavailable: the batch is not ready to confirm (Truth must be current and every localized copy approved)'
      )
    ).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(
      /batch_not_ready|batch_terminal/
    );
  });

  it('words the command timeline, source modes and copy origins', () => {
    const command = (id: string, command_kind: string, operation_id = '') => ({
      id,
      command_kind,
      operation_id: operation_id || null,
      actor_id: 'user-1',
      created_at: '2026-09-09T00:00:00Z',
    });
    const copy = (id: string, target_market: string, copy_origin: string) => ({
      id,
      source_asset_version_id: 'asset-source-v1',
      target_market,
      state: 'approved',
      copy_origin,
      content: { headline: 'Hallo' },
      evidence_hash: `evidence-${id}`,
    });
    mockQueryData.get_localization_batch = batch({
      phase: 'preparing',
      commands: [
        command('command-1', 'create_draft'),
        command('command-2', 'start_preparation', 'op_prepare'),
      ],
      sources: [
        {
          ...claimReviewBatch().sources![0],
          source_generation_mode: 'mock',
          experiment_evidence_mode: 'sandbox',
        },
      ],
      copies: [
        copy('copy-de', 'DE', 'translated'),
        copy('copy-at', 'AT', 'source_locale_projection'),
      ],
    });
    render(<LocalizationWorkspace />);

    expect(screen.getByText(/^Batch created · user-1 ·/)).toBeInTheDocument();
    // The latest Operation's command also heads the Operation status.
    expect(screen.getAllByText(/^Preparation started ·/)).toHaveLength(2);
    expect(
      screen.getByText('generation mock · evidence sandbox')
    ).toBeInTheDocument();
    expect(screen.getByText('Origin: translated · asset')).toBeInTheDocument();
    expect(
      screen.getByText(
        'Origin: same language as the source (not translated) · asset'
      )
    ).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(
      /create_draft|start_preparation|source_locale_projection/
    );
  });

  it.each([
    ['to a Member', 'member', true],
    ['not to an Owner', 'owner', false],
    ['not while the workspace loads', undefined, false],
  ])('shows the Member role note %s', (_who, role, told) => {
    if (role)
      mockQueryData.get_localization_workspace = {
        ...workspace(),
        role,
      };
    render(<LocalizationWorkspace />);

    const note =
      'You are a Member of this Workspace. Commands remain subject to server authorization; unavailable actions stay visible with their reason.';
    if (told) expect(screen.getByText(note)).toBeInTheDocument();
    else expect(screen.queryByText(note)).toBeNull();
    expect(document.body.textContent).not.toMatch(/Role:/);
  });

  it('requires translation when locales share a language but differ by region', async () => {
    commandExecutors.submit_localization_claim_decisions = jest
      .fn()
      .mockResolvedValue({ kind: 'success', data: {} });
    render(
      <LocalizationPreparation
        workspace={workspace()}
        batch={batch({
          phase: 'preparing',
          preparation_stage: 'claim_review',
          truth_package_id: 'truth-1',
          sources: [
            {
              id: 'source-1',
              asset_version_id: 'asset-source-v1',
              decision_id: 'decision-1',
              experiment_id: 'experiment-1',
              experiment_version_id: 'experiment-version-1',
              source_generation_mode: 'mock',
              experiment_evidence_mode: 'mock',
              source_market_snapshot: {
                market: 'US',
                placement: 'feed',
                locale: 'en-US',
                country: 'US',
                currency: 'USD',
              },
              analysis_recipe_hash: 'localization-analysis-v1',
              inventory_id: 'inventory-1',
            },
          ],
          markets: [
            {
              market: 'GB',
              locale: 'en-GB',
              country: 'GB',
              currency: 'GBP',
              market_policy_version_id: 'market-policy-gb-v1',
              readiness: 'ready',
              facts: [],
            },
          ],
          inventories: [
            {
              id: 'inventory-1',
              asset_version_id: 'asset-source-v1',
              analysis_recipe_hash: 'localization-analysis-v1',
              version_number: 1,
              provider: 'controlled-mock-vision',
              model: 'localization-analysis-v1',
              analysis_execution_mode: 'mock',
              evidence_hash: 'inventory-evidence',
              claims: [
                {
                  claim_index: 0,
                  claim_type: 'headline',
                  candidate_fact_types: [],
                  market_sensitive: false,
                  scope_key: 'headline',
                  risk: 'low',
                  conflicting: false,
                },
              ],
            },
          ],
          claim_decision_sets: [],
          allowed_actions: [
            { action: 'submit_claim_decisions', allowed: true },
          ],
        })}
        onRefresh={jest.fn().mockResolvedValue(undefined)}
      />
    );

    fireEvent.click(
      screen.getByRole('button', { name: /submit immutable claim decisions/i })
    );
    await waitFor(() =>
      expect(
        commandExecutors.submit_localization_claim_decisions
      ).toHaveBeenCalled()
    );
    const request =
      commandExecutors.submit_localization_claim_decisions.mock.calls[0][0];
    expect(request.body.source_locale).toBe('en-US');
    expect(request.body.target_locale).toBe('en-GB');
    expect(request.body.translation_recipe).toMatchObject({
      provider: 'controlled-mock-translation',
    });
  });

  type ContextProblem = NonNullable<
    NonNullable<Workspace['eligible_sources']>[number]['source_context_problem']
  >;

  it.each<[ContextProblem, string]>([
    [
      { code: 'missing_source_context', missing_fields: ['locale'] },
      'The selected source is missing its saved locale. Complete the source context before creating a batch; no target Market Policy or browser default is used.',
    ],
    [
      { code: 'invalid_source_context', invalid_fields: ['currency'] },
      'The selected source has an invalid currency. Fix the source context before creating a batch.',
    ],
    [
      {
        code: 'conflicting_source_context',
        conflicting_fields: ['country', 'currency'],
      },
      "The selected source's saved contexts disagree on country, currency. Make them match before creating a batch.",
    ],
  ])(
    'blocks batch creation and words the source context problem %#',
    (problem, sentence) => {
      const incomplete = workspace();
      incomplete.eligible_sources![0].source_market_snapshot = null;
      incomplete.eligible_sources![0].source_context_problem = problem;
      render(
        <NewBatchPanel
          workspace={incomplete}
          onCreated={jest.fn().mockResolvedValue(undefined)}
        />
      );

      expect(screen.getByRole('alert')).toHaveTextContent(sentence);
      expect(screen.getByRole('alert').textContent).not.toMatch(
        /_source_context|_fields/
      );
      expect(
        screen.getByRole('button', { name: /create controlled mock batch/i })
      ).toBeDisabled();
    }
  );

  it('keeps degraded Truth, missing facts, and source failures visible', () => {
    render(
      <TruthPanel
        batch={batch({
          truth_status: 'degraded',
          truth_freshness: 'fresh',
          truth_package_id: 'truth-1',
          markets: [
            {
              market: 'DE',
              market_policy_version_id: 'market-policy-v1',
              locale: 'de-DE',
              currency: 'EUR',
              readiness: 'degraded',
              readiness_detail: {
                reason: 'fact_missing_permitted',
                missing: ['availability'],
              },
              facts: [],
              source_attempts: [
                {
                  id: 'attempt-1',
                  fact_type: 'availability',
                  adapter_kind: 'manual',
                  data_source_version_id: 'source-v1',
                  outcome: 'failed',
                  failure_code: 'fact_type_not_in_manual_set',
                },
              ],
            },
          ],
        })}
      />
    );

    expect(
      screen.getByText(/degraded or missing facts remain visible/i)
    ).toBeInTheDocument();
    // The missing fact types stay listed as they are.
    expect(
      screen.getByText(
        'Some facts could not be resolved; the market continues without them. Missing: availability.'
      )
    ).toBeInTheDocument();
    expect(screen.getByText(/no persisted facts/i)).toBeInTheDocument();
    expect(
      screen.getByText(
        'availability via manual facts: The manual fact set has no value for this fact.'
      )
    ).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(
      /fact_missing_permitted|Missing\/degraded detail/
    );
  });

  it('words why each market Truth is not ready', () => {
    const market = (
      name: string,
      readiness: string,
      readiness_detail: Record<string, unknown>,
      source_attempts: NonNullable<
        Batch['markets']
      >[number]['source_attempts'] = []
    ) => ({
      market: name,
      market_policy_version_id: `market-policy-${name}`,
      locale: 'de-DE',
      currency: 'EUR',
      readiness,
      readiness_detail,
      facts: [],
      source_attempts,
    });
    render(
      <TruthPanel
        batch={batch({
          truth_status: 'degraded',
          truth_freshness: 'current',
          truth_package_id: 'truth-1',
          markets: [
            // A draft batch: Truth is resolved when preparation starts.
            market('DE', 'pending', {}),
            market('FR', 'failed', {
              reason: 'price_unresolved',
              missing: ['discount', 'price'],
            }),
            market('AU', 'unavailable', { reason: 'skipped_unavailable' }, [
              {
                id: 'attempt-au',
                fact_type: 'discount',
                adapter_kind: 'controlled_mock',
                data_source_version_id: 'source-v1',
                outcome: 'failed',
                failure_code: 'fact_type_not_in_mock_scenario',
              },
              {
                id: 'attempt-au-shop',
                fact_type: 'price',
                adapter_kind: 'shopify',
                data_source_version_id: 'source-v2',
                outcome: 'failed',
                failure_code: 'adapter_not_enabled',
              },
            ]),
            // A package without this market keeps its JSON.
            market('IT', 'failed', { missing: ['truth_package_market'] }),
            // An `on_failure=block` fact stops the market.
            market('NL', 'failed', {
              reason: 'fact_missing_blocked',
              blocked: ['delivery_time'],
              missing: ['delivery_time', 'discount'],
            }),
          ],
        })}
      />
    );

    expect(
      screen.getByText(
        'Truth has not been resolved yet. It is resolved when preparation starts.'
      )
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        'The price could not be resolved. Missing: discount, price.'
      )
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        'Truth says the product is not available in this market.'
      )
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        'discount via Controlled Mock: The mock scenario has no value for this fact.'
      )
    ).toBeInTheDocument();
    expect(
      screen.getByText('price via Shopify: This fact source is not set up.')
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        'Missing/degraded detail: {"missing":["truth_package_market"]}'
      )
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        'A fact the policy requires could not be resolved (policy: block), so this market does not run. Blocking: delivery_time. Missing: delivery_time, discount.'
      )
    ).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(
      /\{\}|price_unresolved|skipped_unavailable|fact_missing_blocked|controlled_mock/
    );
  });

  it('never reports an accepted source attempt as a failure', () => {
    render(
      <TruthPanel
        batch={batch({
          truth_status: 'ready',
          truth_freshness: 'fresh',
          truth_package_id: 'truth-ok',
          markets: [
            {
              market: 'DE',
              market_policy_version_id: 'market-policy-v1',
              locale: 'de-DE',
              currency: 'EUR',
              readiness: 'ready',
              readiness_detail: {},
              facts: [
                {
                  id: 'fact-1',
                  fact_type: 'price',
                  scope_key: 'sku-1',
                  value: '19.99',
                  external_object_ref: 'manual://price',
                  accepted_source_attempt_id: 'attempt-ok',
                  evidence_hash: 'evidence-1',
                  observed_at: '2026-09-09T00:00:00Z',
                  valid_until: '2027-09-09T00:00:00Z',
                },
              ],
              source_attempts: [
                {
                  id: 'attempt-ok',
                  fact_type: 'price',
                  adapter_kind: 'manual',
                  data_source_version_id: 'source-v1',
                  outcome: 'accepted',
                  failure_code: null,
                },
              ],
            },
          ],
        })}
      />
    );

    // `accepted` is the fact landing, not a failure. The panel used to test
    // `outcome !== 'resolved'` — a value the backend never emits — so every
    // accepted attempt was listed under "Source failures / skips".
    expect(screen.queryByText(/source failures \/ skips/i)).toBeNull();
    expect(screen.queryByText(/accepted/i)).toBeNull();
  });

  it('separates accepted attempts from the failed and skipped ones', () => {
    render(
      <TruthPanel
        batch={batch({
          truth_status: 'degraded',
          truth_freshness: 'fresh',
          truth_package_id: 'truth-mixed',
          markets: [
            {
              market: 'FR',
              market_policy_version_id: 'market-policy-v2',
              locale: 'fr-FR',
              currency: 'EUR',
              readiness: 'degraded',
              readiness_detail: {
                reason: 'fact_missing_permitted',
                missing: ['availability', 'shipping'],
              },
              facts: [],
              source_attempts: [
                {
                  id: 'attempt-accepted',
                  fact_type: 'price',
                  adapter_kind: 'manual',
                  data_source_version_id: 'source-v1',
                  outcome: 'accepted',
                  failure_code: null,
                },
                {
                  id: 'attempt-failed',
                  fact_type: 'availability',
                  adapter_kind: 'manual',
                  data_source_version_id: 'source-v1',
                  outcome: 'failed',
                  failure_code: 'fact_type_not_in_manual_set',
                },
                {
                  // What no source resolved is skipped under its frozen
                  // `on_failure` rule (`truth.py`).
                  id: 'attempt-skipped',
                  fact_type: 'shipping',
                  adapter_kind: 'policy',
                  data_source_version_id: 'source-v1',
                  outcome: 'skipped',
                  skip_code: 'on_failure_skip',
                },
              ],
            },
          ],
        })}
      />
    );

    expect(screen.getByText(/source failures \/ skips/i)).toBeInTheDocument();
    expect(
      screen.getByText(
        'availability via manual facts: The manual fact set has no value for this fact.'
      )
    ).toBeInTheDocument();
    // A skip comes from the fact's `on_failure` rule, not from a source.
    expect(
      screen.getByText(
        'shipping via fallback rule: no source resolved it (policy: skip)'
      )
    ).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(
      /fact_type_not_in_manual_set|on_failure_skip|via policy/
    );
    // The accepted price attempt contributes no failure row.
    expect(screen.queryByText(/price via manual/i)).toBeNull();
  });

  it('shows exhausted retry, isolated output, blocking QC, cost, and lineage together', () => {
    render(
      <LocalizationItemInspector
        item={item({
          // The last attempt's blocking check failed: `_handle_attempt_failure`
          // keeps the failing slot as the detail.
          state: 'failed',
          reason_code: 'qc_exhausted',
          reason_detail: 'headline',
          next_action: 'create_new_batch',
          attempts: [
            {
              id: 'attempt-3',
              attempt_number: 3,
              state: 'failed',
              provider: 'mock-edit',
              model: 'edit-v1',
              created_at: '2026-09-09T00:00:00Z',
              output: {
                id: 'output-3',
                content_sha256: 'abc123',
                byte_size: 1024,
                mime_type: 'image/png',
                isolated: true,
              },
              publication: null,
              provider_calls: [
                {
                  step: 'localization_edit',
                  dispatch_state: 'succeeded',
                  chargeability: 'charged',
                  provider: 'mock-edit',
                  model: 'edit-v1',
                  maximum_cost: {
                    amount: '0.75',
                    currency: 'USD',
                    state: 'settled',
                  },
                },
              ],
              qc: [
                {
                  id: 'qc-1',
                  check_kind: 'locked_fact',
                  check_slot_key: 'locked_fact:0',
                  severity: 'blocking',
                  result: 'fail',
                  expected: '19.99 EUR',
                  observed: '24.99 EUR',
                  rule_version: 'qc-rule-v1',
                },
              ],
            },
          ],
          cost: {
            totals: [{ amount: '0.61', currency: 'USD' }],
            ledger: [
              {
                entry_type: 'reported_cost',
                direction: 'increase',
                amount: '0.61',
                currency: 'USD',
                provider: 'mock-edit',
                model: 'edit-v1',
                created_at: '2026-09-09T00:00:00Z',
              },
            ],
          },
          lineage: {
            source_asset_version_id: 'asset-source-v1',
            decision_id: 'decision-1',
            offer_version_id: 'offer-v1',
            truth_package_id: 'truth-1',
            copy_version_id: 'copy-1',
            localized_asset_version_id: 'asset-localized-v1',
          },
        })}
        onRetry={jest.fn()}
        onResume={jest.fn()}
        isPending={false}
      />
    );

    expect(screen.getByRole('button', { name: /retry item/i })).toBeDisabled();
    expect(
      screen.getByText(
        'The last allowed attempt failed. Create a new batch to try again.'
      )
    ).toBeInTheDocument();
    // The code and its detail are folded away under Technical details.
    const technical = screen.getByText('Technical details').closest('details')!;
    expect(technical).not.toHaveAttribute('open');
    expect(technical).toContainElement(screen.getByText('qc_exhausted'));
    expect(technical).toHaveTextContent('qc_exhausted headline');
    expect(
      screen.getByText('Retry unavailable: all allowed attempts have been used')
    ).toBeInTheDocument();
    expect(screen.getByText('Create a new batch')).toBeInTheDocument();
    expect(screen.getByText(/isolated \(not adopted\)/i)).toBeInTheDocument();
    expect(
      screen.getByText(/locked-copy \/ blocking qc failure/i)
    ).toBeInTheDocument();
    // The slot key stays: it names which locked fact failed.
    expect(
      screen.getByText(
        'Locked fact value (locked_fact:0): expected 19.99 EUR, observed 24.99 EUR'
      )
    ).toBeInTheDocument();
    expect(
      screen.getByRole('cell', { name: 'Locked fact value' })
    ).toBeInTheDocument();
    expect(screen.getByRole('cell', { name: 'Blocking' })).toBeInTheDocument();
    expect(
      screen.getByText(
        'image edit: mock-edit/edit-v1 · succeeded · charged · max 0.75 USD (settled)'
      )
    ).toBeInTheDocument();
    expect(
      screen.getByText('Reported cost: +0.61 USD · mock-edit/edit-v1')
    ).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(
      /attempts_exhausted|create_new_batch|localization_edit|reported_cost|increase/
    );
    expect(screen.getByText('asset-localized-v1')).toBeInTheDocument();
  });

  it.each([
    [
      'dispatch_uncertain',
      'uncertain',
      'call left uncertain; requires reconciliation',
      'Whether the external call finished is unknown. It needs reconciling.',
    ],
    [
      // A provider's own code, passed on by the call coordinator.
      'provider_error',
      'retry_wait',
      'provider_error',
      'The AI provider returned an error and this call did not complete. Try again later.',
    ],
    [
      // A failed edit call that carried no code reads as its family.
      'edit_dispatch_failed',
      'retry_wait',
      'edit_dispatch_failed',
      'The external call failed.',
    ],
  ])(
    'reads the dispatch outcome %s as its error copy',
    (code, state, detail, sentence) => {
      render(
        <LocalizationItemInspector
          item={item({
            state,
            reason_code: code,
            reason_detail: detail,
            attempts: [],
          })}
          onRetry={jest.fn()}
          onResume={jest.fn()}
          isPending={false}
        />
      );

      expect(screen.getByText(sentence)).toBeInTheDocument();
      expect(
        screen.getByText('Technical details').closest('details')
      ).toContainElement(screen.getByText(code, { selector: 'code' }));
    }
  );

  it('reads a failed call by its error copy, not its code', () => {
    render(
      <LocalizationItemInspector
        item={item({
          state: 'uncertain',
          reason_code: 'dispatch_uncertain',
          reason_detail: 'call left uncertain; requires reconciliation',
          attempts: [
            {
              id: 'attempt-1',
              attempt_number: 1,
              state: 'uncertain',
              provider: 'mock-edit',
              model: 'edit-v1',
              created_at: '2026-09-09T00:00:00Z',
              output: null,
              publication: null,
              provider_calls: [
                {
                  step: 'localization_edit',
                  dispatch_state: 'uncertain',
                  chargeability: 'charged',
                  provider: 'mock-edit',
                  model: 'edit-v1',
                  error_code: 'materialize_failed',
                  maximum_cost: {
                    amount: '0.75',
                    currency: 'USD',
                    state: 'reserved',
                  },
                },
              ],
              qc: [],
            },
          ],
        })}
        onRetry={jest.fn()}
        onResume={jest.fn()}
        isPending={false}
      />
    );

    expect(
      screen.getByText(/error: The AI result could not be saved\./)
    ).not.toHaveTextContent('materialize_failed');
  });

  it('never hides the zero-cost state', () => {
    render(
      <LocalizationItemInspector
        item={item({ attempts: [], cost: { totals: [], ledger: [] } })}
        onRetry={jest.fn()}
        onResume={jest.fn()}
        isPending={false}
      />
    );

    expect(
      screen.getByText(/no chargeable provider cost recorded/i)
    ).toBeInTheDocument();
  });

  it.each([
    [
      // What every published Item shows today: no command reviews a binding.
      'pending_review',
      'current',
      false,
      'Active binding: Localized version · review: pending review · Truth: current · not ready for a new experiment yet',
    ],
    [
      'approved',
      'current',
      true,
      'Active binding: Localized version · review: approved · Truth: current · ready for a new experiment',
    ],
    [
      'needs_changes',
      'current',
      false,
      'Active binding: Localized version · review: needs changes · Truth: current · not ready for a new experiment yet',
    ],
  ])(
    'words an active binding under review %s',
    (review_state, truth_freshness, ready_for_experiment, sentence) => {
      render(
        <LocalizationItemInspector
          item={item({
            binding: {
              id: 'binding-1',
              kind: 'localized',
              bound_asset_version_id: 'asset-localized-v1',
              review_state,
              truth_freshness,
              ready_for_experiment,
            },
          })}
          onRetry={jest.fn()}
          onResume={jest.fn()}
          isPending={false}
        />
      );

      expect(screen.getByText(sentence)).toBeInTheDocument();
      expect(document.body.textContent).not.toMatch(
        /localized ·|_review|needs_changes|true|false/
      );
    }
  );

  it('blocks a binding whose Truth is out of date from a new experiment', () => {
    render(
      <LocalizationItemInspector
        item={item({
          binding: {
            id: 'binding-1',
            kind: 'localized',
            bound_asset_version_id: 'asset-localized-v1',
            review_state: 'pending_review',
            truth_freshness: 'stale',
            ready_for_experiment: false,
          },
        })}
        onRetry={jest.fn()}
        onResume={jest.fn()}
        isPending={false}
      />
    );

    expect(
      screen.getByText(
        'Active binding: Localized version · review: pending review · Truth: stale · not ready for a new experiment yet'
      )
    ).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent(
      /cannot be used in a new experiment until it is localized again/i
    );
    expect(screen.getByRole('alert').textContent).not.toMatch(
      /stale_truth|MarketAssetBinding/
    );
  });

  it('makes stale Truth a blocking state without hiding historical facts', () => {
    render(
      <TruthPanel
        batch={batch({
          truth_status: 'full',
          truth_freshness: 'expired',
          truth_package_id: 'truth-old',
          markets: [],
        })}
      />
    );

    expect(screen.getByRole('alert')).toHaveTextContent(
      /stale truth blocks execution/i
    );
    expect(screen.getByText('truth-old')).toBeInTheDocument();
  });

  type Attempt = NonNullable<Item['attempts']>[number];
  type QcCheck = NonNullable<Attempt['qc']>[number];

  function isolatedAttempt(qc: QcCheck[]): Attempt {
    return {
      id: 'attempt-isolated',
      attempt_number: 2,
      state: 'completed',
      provider: 'mock-edit',
      model: 'edit-v1',
      created_at: '2026-09-09T00:00:00Z',
      output: {
        id: 'output-isolated',
        content_sha256: 'sha-isolated',
        byte_size: 2048,
        mime_type: 'image/png',
        isolated: true,
      },
      publication: null,
      qc,
      provider_calls: [],
    };
  }

  it('offers isolated-output recovery only while the output is unpublished', () => {
    const { rerender } = render(
      <LocalizationItemInspector
        item={item({ attempts: [isolatedAttempt([])] })}
        onRetry={jest.fn()}
        onResume={jest.fn()}
        onAuthorizeIsolatedQc={jest.fn()}
        onAdoptIsolatedOutput={jest.fn()}
        isPending={false}
      />
    );
    expect(
      screen.getByTestId('authorize-isolated-qc-attempt-isolated')
    ).toBeInTheDocument();

    // Once published, the recovery commands are gone: there is nothing
    // left to authorize and nothing left to adopt.
    const published = isolatedAttempt([]);
    published.publication = {
      id: 'pub-1',
      publication_kind: 'normal',
      asset_version_id: 'asset-localized-v1',
    } as never;
    rerender(
      <LocalizationItemInspector
        item={item({ attempts: [published] })}
        onRetry={jest.fn()}
        onResume={jest.fn()}
        onAuthorizeIsolatedQc={jest.fn()}
        onAdoptIsolatedOutput={jest.fn()}
        isPending={false}
      />
    );
    expect(
      screen.queryByTestId('authorize-isolated-qc-attempt-isolated')
    ).toBeNull();
  });

  it('blocks adoption until every frozen check has passed', () => {
    const onAdopt = jest.fn();
    render(
      <LocalizationItemInspector
        item={item({
          attempts: [
            isolatedAttempt([
              {
                id: 'qc-passed',
                check_kind: 'placeholder_count',
                check_slot_key: 'placeholder_count',
                severity: 'blocking',
                result: 'pass',
                expected: null,
                observed: null,
                rule_version: 'v1',
              },
              {
                id: 'qc-pending',
                check_kind: 'ocr_check',
                check_slot_key: 'ocr_check',
                severity: 'blocking',
                result: 'pending',
                expected: null,
                observed: null,
                rule_version: 'v1',
              },
            ]),
          ],
        })}
        onRetry={jest.fn()}
        onResume={jest.fn()}
        onAuthorizeIsolatedQc={jest.fn()}
        onAdoptIsolatedOutput={onAdopt}
        isPending={false}
      />
    );

    expect(
      screen.getByTestId('adopt-isolated-output-attempt-isolated')
    ).toBeDisabled();
    expect(screen.getByText(/adoption blocked/i)).toBeInTheDocument();
    // Authorizing the missing check is still available -- that is the
    // command that makes adoption reachable.
    expect(
      screen.getByTestId('authorize-isolated-qc-attempt-isolated')
    ).toBeEnabled();
  });

  it('sends the attempt id when authorizing and adopting', () => {
    const onAuthorize = jest.fn();
    const onAdopt = jest.fn();
    render(
      <LocalizationItemInspector
        item={item({
          attempts: [
            isolatedAttempt([
              {
                id: 'qc-passed',
                check_kind: 'ocr_check',
                check_slot_key: 'ocr_check',
                severity: 'blocking',
                result: 'pass',
                expected: null,
                observed: null,
                rule_version: 'v1',
              },
            ]),
          ],
        })}
        onRetry={jest.fn()}
        onResume={jest.fn()}
        onAuthorizeIsolatedQc={onAuthorize}
        onAdoptIsolatedOutput={onAdopt}
        isPending={false}
      />
    );

    fireEvent.click(
      screen.getByTestId('authorize-isolated-qc-attempt-isolated')
    );
    expect(onAuthorize).toHaveBeenCalledWith('attempt-isolated');

    fireEvent.click(
      screen.getByTestId('adopt-isolated-output-attempt-isolated')
    );
    expect(onAdopt).toHaveBeenCalledWith('attempt-isolated');
  });

  it('never offers to publish an output a blocking check failed', () => {
    render(
      <LocalizationItemInspector
        item={item({
          attempts: [
            isolatedAttempt([
              {
                id: 'qc-failed',
                check_kind: 'locked_fact',
                check_slot_key: 'locked_fact:0',
                severity: 'blocking',
                result: 'fail',
                expected: null,
                observed: null,
                rule_version: 'v1',
              },
            ]),
          ],
        })}
        onRetry={jest.fn()}
        onResume={jest.fn()}
        onAuthorizeIsolatedQc={jest.fn()}
        onAdoptIsolatedOutput={jest.fn()}
        isPending={false}
      />
    );

    expect(
      screen.queryByTestId('adopt-isolated-output-attempt-isolated')
    ).toBeNull();
    expect(
      screen.queryByTestId('authorize-isolated-qc-attempt-isolated')
    ).toBeNull();
    expect(screen.getByText(/can never be adopted/i)).toBeInTheDocument();
  });
});
