/**
 * @jest-environment jsdom
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useRouter } from 'next/router';
import { useDomainPaginatedQuery } from '@/hooks/domain/useDomainPaginatedQuery';
import {
  useAnalyzeDuplicates,
  useExperimentEvidence,
  useExperimentsWorkspace,
  useMetaLaunchPreview,
  useMetaLaunchProvisioning,
} from '@/hooks/experiments/useExperimentsWorkspace';
import { ExperimentsWorkspace } from '@/components/experiments';

jest.mock('next/router', () => ({ useRouter: jest.fn() }));
jest.mock('@/hooks/domain/useDomainPaginatedQuery', () => ({
  useDomainPaginatedQuery: jest.fn(),
}));
jest.mock('@/hooks/experiments/useExperimentsWorkspace', () => ({
  useExperimentsWorkspace: jest.fn(),
  useAnalyzeDuplicates: jest.fn(),
  useExperimentEvidence: jest.fn(),
  useMetaLaunchPreview: jest.fn(),
  useMetaLaunchProvisioning: jest.fn(),
}));

const okMeta = { etag: null, retryAt: null, location: null };

function command(result?: unknown) {
  return {
    execute: jest.fn().mockResolvedValue({
      kind: 'success',
      data: result ?? {},
      meta: okMeta,
    }),
    retry: jest.fn(),
    reset: jest.fn(),
    result: undefined,
    problem: undefined,
    reconciliation: undefined,
    isPending: false,
  };
}

function query(data?: unknown) {
  return {
    data,
    problem: undefined,
    isLoading: false,
    isStale: false,
    lastSuccessfulAt: Date.now(),
    refresh: jest.fn().mockResolvedValue(undefined),
  };
}

function baseWorkspace(detailData?: Record<string, unknown>) {
  return {
    list: {
      items: detailData
        ? [
            {
              id: detailData.id,
              name: detailData.name,
              state: detailData.state,
              revision: detailData.revision,
              created_at: detailData.created_at,
            },
          ]
        : [],
      firstPage: undefined,
      hasMore: false,
      isLoading: false,
      isLoadingMore: false,
      isRefreshing: false,
      problem: undefined,
      loadMore: jest.fn(),
      refresh: jest.fn().mockResolvedValue(undefined),
    },
    detail: {
      data: detailData,
      problem: undefined,
      isLoading: false,
      isStale: false,
      lastSuccessfulAt: Date.now(),
      refresh: jest.fn().mockResolvedValue(undefined),
    },
    commands: {
      create: command(),
      addEntry: command(),
      updateContext: command(),
      freeze: command(),
      bindCandidate: command(),
      start: command(),
      judge: command(),
      confirmProposal: command({
        confirmation_id: 'confirmation-1',
        decisions: [],
      }),
      createLaunchPreview: command(),
      confirmLaunchPreview: command(),
      executeLaunchPreview: command(),
      recoverLaunchOperation: command(),
    },
  };
}

const mockRouter = {
  query: { slug: 'acme', experiment: 'exp-1' } as Record<string, string>,
  pathname: '/w/[slug]/experiments',
  replace: jest.fn().mockResolvedValue(true),
};

beforeEach(() => {
  jest.clearAllMocks();
  (useRouter as jest.Mock).mockReturnValue(mockRouter);
  (useDomainPaginatedQuery as jest.Mock).mockReturnValue({
    items: [],
    isLoading: false,
    problem: undefined,
  });
  (useExperimentEvidence as jest.Mock).mockReturnValue(
    query({ head_sequence: 0, items: [] })
  );
  (useMetaLaunchPreview as jest.Mock).mockReturnValue(query());
  (useMetaLaunchProvisioning as jest.Mock).mockReturnValue(query());
  (useAnalyzeDuplicates as jest.Mock).mockReturnValue(query());
});

describe('ExperimentsWorkspace', () => {
  it('restores the selected experiment from the URL and requires explicit proposal confirmation', async () => {
    const workspace = baseWorkspace({
      id: 'exp-1',
      product_id: 'product-1',
      name: 'US feed test',
      state: 'running',
      revision: 5,
      record_schema_version: 2,
      created_at: '2026-09-09T00:00:00Z',
      draft_context: {
        product_version_id: 'pv-1',
        offer_version_id: 'ov-1',
        market: 'US',
        currency: 'USD',
        placement: 'feed',
        budget: { daily_amount: '100', total_amount: '3000' },
        schedule: {
          start_at: '2026-09-10T00:00:00Z',
          end_at: '2026-09-20T00:00:00Z',
        },
      },
      entries: [],
      candidates: [],
      judgements: [{ id: 'judgement-1' }],
      proposals: [
        {
          id: 'proposal-1',
          judgement_id: 'judgement-1',
          proposal_hash: 'hash-1',
          state: 'created',
          sequence: 1,
          proposed_entries: [
            {
              entry_id: 'candidate-1',
              verdict: 'winner',
              metric: 'ctr',
              estimate: '0.12',
            },
          ],
          evidence: { eligible: true },
          created_at: '2026-09-09T01:00:00Z',
        },
      ],
      decisions: [],
    });
    (useExperimentsWorkspace as jest.Mock).mockReturnValue(workspace);

    render(<ExperimentsWorkspace />);

    expect(useExperimentsWorkspace).toHaveBeenCalledWith('exp-1');
    expect(screen.getAllByText('US feed test')).toHaveLength(2);
    expect(screen.getByText('Not confirmed')).toBeInTheDocument();
    expect(screen.getByText(/Proposed winner/)).toBeInTheDocument();
    expect(screen.queryByText(/Confirmed winner/i)).not.toBeInTheDocument();

    const confirmButton = screen.getByRole('button', {
      name: 'Confirm proposal',
    });
    expect(confirmButton).toBeDisabled();
    fireEvent.click(
      screen.getByLabelText(/I reviewed the evidence and understand/i)
    );
    fireEvent.click(confirmButton);

    await waitFor(() => {
      expect(workspace.commands.confirmProposal.execute).toHaveBeenCalledWith(
        expect.objectContaining({
          pathParams: {
            experiment_id: 'exp-1',
            proposal_id: 'proposal-1',
          },
          body: { expected_proposal_sequence: 1 },
        })
      );
      expect(workspace.detail.refresh).toHaveBeenCalled();
    });
  });

  it('shows a refusal by its code and a dropped connection by its own advice, never the raw detail', async () => {
    mockRouter.query = { slug: 'acme', experiment: 'exp-run' };
    const workspace = baseWorkspace({
      id: 'exp-run',
      product_id: 'product-1',
      name: 'Running test',
      state: 'running',
      revision: 7,
      record_schema_version: 2,
      created_at: '2026-09-09T00:00:00Z',
      draft_context: {
        product_version_id: 'pv-1',
        offer_version_id: 'ov-1',
        market: 'US',
        currency: 'USD',
        placement: 'feed',
        budget: { daily_amount: '100', total_amount: '3000' },
        schedule: {
          start_at: '2026-09-10T00:00:00Z',
          end_at: '2026-09-20T00:00:00Z',
        },
      },
      entries: [],
      candidates: [],
      judgements: [],
      proposals: [],
      decisions: [],
    });
    workspace.commands.judge.execute
      .mockResolvedValueOnce({
        kind: 'problem',
        problem: {
          status: 409,
          code: 'revision_conflict',
          detail: 'raw backend detail',
        },
      })
      .mockRejectedValueOnce(new TypeError('Failed to fetch'));
    (useExperimentsWorkspace as jest.Mock).mockReturnValue(workspace);
    render(<ExperimentsWorkspace />);

    fireEvent.click(screen.getByRole('button', { name: /Run judgement/ }));
    expect(
      await screen.findByText(
        'This changed while you were working. Refresh and try again.'
      )
    ).toBeInTheDocument();
    expect(screen.queryByText(/raw backend detail/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Run judgement/ }));
    expect(
      await screen.findByText(
        'The outcome is uncertain. Refresh before retrying this action.'
      )
    ).toBeInTheDocument();
    // The browser never reached the server, so not the Meta-specific copy.
    expect(
      screen.queryByText(/Meta could not be reached/)
    ).not.toBeInTheDocument();
  });

  it('creates a draft from same-product approved assets and advances revisions sequentially', async () => {
    mockRouter.query = { slug: 'acme' };
    const workspace = baseWorkspace();
    workspace.commands.create.execute.mockResolvedValue({
      kind: 'success',
      data: { id: 'exp-new', revision: 1 },
      meta: okMeta,
    });
    workspace.commands.addEntry.execute
      .mockResolvedValueOnce({
        kind: 'success',
        data: { revision: 2 },
        meta: okMeta,
      })
      .mockResolvedValueOnce({
        kind: 'success',
        data: { revision: 3 },
        meta: okMeta,
      });
    (useExperimentsWorkspace as jest.Mock).mockReturnValue(workspace);
    (useDomainPaginatedQuery as jest.Mock).mockReturnValue({
      items: [
        {
          id: 'asset-version-1',
          asset_id: 'asset-1',
          product_id: 'product-1',
          run_id: 'run-1',
          provider: 'mock',
          model: 'mock',
          status: 'ready',
          aspect_ratio: '1:1',
          origin_kind: 'generated',
          byte_size: 100,
        },
        {
          id: 'asset-version-2',
          asset_id: 'asset-2',
          product_id: 'product-1',
          run_id: 'run-1',
          provider: 'mock',
          model: 'mock',
          status: 'ready',
          aspect_ratio: '1:1',
          origin_kind: 'generated',
          byte_size: 100,
        },
      ],
      isLoading: false,
      problem: undefined,
    });

    render(<ExperimentsWorkspace />);
    fireEvent.click(screen.getByRole('button', { name: /New experiment/i }));

    fireEvent.change(screen.getByLabelText('Experiment name'), {
      target: { value: 'New creative test' },
    });
    fireEvent.change(screen.getByLabelText('Product version ID'), {
      target: { value: 'pv-1' },
    });
    fireEvent.change(screen.getByLabelText('Offer version ID'), {
      target: { value: 'ov-1' },
    });
    fireEvent.change(screen.getByLabelText('Start date'), {
      target: { value: '2026-09-10' },
    });
    fireEvent.change(screen.getByLabelText('End date'), {
      target: { value: '2026-09-20' },
    });
    screen
      .getAllByRole('checkbox')
      .forEach((checkbox) => fireEvent.click(checkbox));
    fireEvent.click(screen.getByRole('button', { name: 'Create draft' }));

    await waitFor(() => {
      expect(workspace.commands.addEntry.execute).toHaveBeenNthCalledWith(
        1,
        expect.objectContaining({
          pathParams: { experiment_id: 'exp-new' },
          body: { asset_version_id: 'asset-version-1', expected_revision: 1 },
        })
      );
      expect(workspace.commands.addEntry.execute).toHaveBeenNthCalledWith(
        2,
        expect.objectContaining({
          pathParams: { experiment_id: 'exp-new' },
          body: { asset_version_id: 'asset-version-2', expected_revision: 2 },
        })
      );
      expect(mockRouter.replace).toHaveBeenCalledWith(
        {
          pathname: '/w/[slug]/experiments',
          query: { slug: 'acme', experiment: 'exp-new' },
        },
        undefined,
        { shallow: true }
      );
    });
  });

  it('shows a refused entry by its duplicate-asset copy, never a generic failure (#340)', async () => {
    mockRouter.query = { slug: 'acme' };
    const workspace = baseWorkspace();
    workspace.commands.create.execute.mockResolvedValue({
      kind: 'success',
      data: { id: 'exp-new', revision: 1 },
      meta: okMeta,
    });
    workspace.commands.addEntry.execute
      .mockResolvedValueOnce({
        kind: 'success',
        data: { revision: 2 },
        meta: okMeta,
      })
      .mockResolvedValueOnce({
        kind: 'problem',
        problem: {
          status: 409,
          code: 'experiment_asset_duplicate',
          detail: 'raw backend detail',
        },
        meta: okMeta,
      });
    (useExperimentsWorkspace as jest.Mock).mockReturnValue(workspace);
    const approved = (id: string, assetId: string) => ({
      id,
      asset_id: assetId,
      product_id: 'product-1',
      run_id: 'run-1',
      provider: 'mock',
      model: 'mock',
      status: 'ready',
      aspect_ratio: '1:1',
      origin_kind: 'generated',
      byte_size: 100,
    });
    (useDomainPaginatedQuery as jest.Mock).mockReturnValue({
      items: [
        approved('asset-version-1', 'asset-1'),
        approved('asset-version-2', 'asset-2'),
      ],
      isLoading: false,
      problem: undefined,
    });

    render(<ExperimentsWorkspace />);
    fireEvent.click(screen.getByRole('button', { name: /New experiment/i }));
    fireEvent.change(screen.getByLabelText('Experiment name'), {
      target: { value: 'New creative test' },
    });
    fireEvent.change(screen.getByLabelText('Product version ID'), {
      target: { value: 'pv-1' },
    });
    fireEvent.change(screen.getByLabelText('Offer version ID'), {
      target: { value: 'ov-1' },
    });
    fireEvent.change(screen.getByLabelText('Start date'), {
      target: { value: '2026-09-10' },
    });
    fireEvent.change(screen.getByLabelText('End date'), {
      target: { value: '2026-09-20' },
    });
    screen
      .getAllByRole('checkbox')
      .forEach((checkbox) => fireEvent.click(checkbox));
    fireEvent.click(screen.getByRole('button', { name: 'Create draft' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The same asset version appears twice in this experiment.'
    );
    expect(screen.queryByText(/raw backend detail/)).not.toBeInTheDocument();
    expect(workspace.commands.addEntry.execute).toHaveBeenCalledTimes(2);
  });

  it('shows the server-computed Sandbox cost before launch confirmation', async () => {
    mockRouter.query = { slug: 'acme', experiment: 'exp-frozen' };
    const workspace = baseWorkspace({
      id: 'exp-frozen',
      product_id: 'product-1',
      name: 'Frozen test',
      state: 'frozen',
      revision: 4,
      record_schema_version: 2,
      version_id: 'experiment-version-1',
      created_at: '2026-09-09T00:00:00Z',
      draft_context: {
        product_version_id: 'pv-1',
        offer_version_id: 'ov-1',
        market: 'US',
        currency: 'USD',
        placement: 'feed',
        budget: { daily_amount: '5', total_amount: '100' },
        schedule: {
          start_at: '2026-09-10T00:00:00Z',
          end_at: '2026-09-20T00:00:00Z',
        },
      },
      entries: [],
      candidates: [
        {
          id: 'candidate-1',
          position: 0,
          asset_version_id: 'asset-version-1',
          candidate_hash: 'hash-1',
          execution_mode: 'sandbox',
        },
        {
          id: 'candidate-2',
          position: 1,
          asset_version_id: 'asset-version-2',
          candidate_hash: 'hash-2',
          execution_mode: 'sandbox',
        },
      ],
      judgements: [],
      proposals: [],
      decisions: [],
    });
    const preview = {
      id: 'launch-preview-1',
      workspace_id: 'workspace-1',
      meta_mode: 'customer_sandbox',
      meta_connection_id: 'connection-1',
      meta_binding_id: 'binding-evidence-1',
      experiment_id: 'exp-frozen',
      experiment_version_id: 'experiment-version-1',
      candidate_count: 2,
      campaign_config: { initial_status: 'PAUSED' },
      daily_budget_minor: 500,
      total_daily_cost_minor: 1000,
      currency: 'USD',
      preview_hash: 'a'.repeat(64),
      state: 'draft',
      expires_at: '2099-09-09T02:00:00Z',
      created_at: '2026-09-09T01:00:00Z',
      candidates: [
        {
          id: 'launch-candidate-1',
          candidate_index: 0,
          experiment_version_candidate_id: 'candidate-1',
          asset_version_id: 'asset-version-1',
          creative_config: { headline: 'Exact A' },
        },
        {
          id: 'launch-candidate-2',
          candidate_index: 1,
          experiment_version_candidate_id: 'candidate-2',
          asset_version_id: 'asset-version-2',
          creative_config: { headline: 'Exact B' },
        },
      ],
    };
    workspace.commands.createLaunchPreview.execute.mockResolvedValue({
      kind: 'success',
      data: preview,
      meta: okMeta,
    });
    workspace.commands.confirmLaunchPreview.execute.mockResolvedValue({
      kind: 'success',
      data: { ...preview, state: 'confirmed' },
      meta: okMeta,
    });
    workspace.commands.executeLaunchPreview.execute.mockResolvedValue({
      kind: 'success',
      data: {
        id: 'provisioning-1',
        operation_id: 'operation-1',
        provisioning_state: 'provisioned',
      },
      meta: okMeta,
    });
    (useExperimentsWorkspace as jest.Mock).mockReturnValue(workspace);

    render(<ExperimentsWorkspace />);
    fireEvent.change(screen.getByLabelText('Sandbox Meta connection ID'), {
      target: { value: 'connection-1' },
    });
    fireEvent.change(screen.getByLabelText('Sandbox Meta ad account ID'), {
      target: { value: 'act_1' },
    });
    fireEvent.click(
      screen.getByRole('button', { name: 'Create Sandbox preview' })
    );

    expect(await screen.findByText('USD 10.00')).toBeInTheDocument();
    expect(screen.getByText('Exact candidate manifest')).toBeInTheDocument();
    expect(screen.getByText('candidate-1')).toBeInTheDocument();
    expect(screen.getByText('{"headline":"Exact A"}')).toBeInTheDocument();
    const confirm = screen.getByRole('button', {
      name: 'Confirm Sandbox preview',
    });
    expect(confirm).toBeDisabled();
    fireEvent.click(screen.getByLabelText(/I reviewed every exact candidate/i));
    fireEvent.click(confirm);

    await waitFor(() => {
      expect(
        workspace.commands.confirmLaunchPreview.execute
      ).toHaveBeenCalledWith(
        expect.objectContaining({
          pathParams: { preview_id: 'launch-preview-1' },
          body: {
            meta_mode: 'customer_sandbox',
            preview_hash: 'a'.repeat(64),
          },
        })
      );
      expect(
        screen.getByText(
          'Preview confirmed. No object was created by preview confirmation.'
        )
      ).toBeInTheDocument();
    });

    const execute = screen.getByRole('button', {
      name: 'Execute PAUSED Sandbox launch',
    });
    expect(execute).toBeDisabled();
    fireEvent.click(screen.getByLabelText(/Execute this exact 2-candidate/i));
    fireEvent.click(execute);

    await waitFor(() => {
      expect(
        workspace.commands.executeLaunchPreview.execute
      ).toHaveBeenCalledWith(
        expect.objectContaining({
          pathParams: { preview_id: 'launch-preview-1' },
          body: { meta_mode: 'customer_sandbox' },
        })
      );
      expect(mockRouter.replace).toHaveBeenCalledWith(
        expect.objectContaining({
          query: expect.objectContaining({
            experiment: 'exp-frozen',
            launchPreview: 'launch-preview-1',
            launchProvisioning: 'provisioning-1',
          }),
        }),
        undefined,
        { shallow: true }
      );
    });
  });

  it('blocks launch preview when frozen candidate execution modes are mixed', () => {
    mockRouter.query = { slug: 'acme', experiment: 'exp-mixed' };
    const workspace = baseWorkspace({
      id: 'exp-mixed',
      product_id: 'product-1',
      name: 'Mixed mode test',
      state: 'frozen',
      revision: 4,
      record_schema_version: 2,
      version_id: 'experiment-version-mixed',
      created_at: '2026-09-09T00:00:00Z',
      draft_context: {
        currency: 'USD',
        budget: { daily_amount: '5', total_amount: '100' },
        schedule: {},
      },
      entries: [],
      candidates: [
        {
          id: 'candidate-sandbox',
          position: 0,
          asset_version_id: 'asset-sandbox',
          candidate_hash: 'hash-sandbox',
          execution_mode: 'sandbox',
        },
        {
          id: 'candidate-mock',
          position: 1,
          asset_version_id: 'asset-mock',
          candidate_hash: 'hash-mock',
          execution_mode: 'mock',
        },
      ],
      judgements: [],
      proposals: [],
      decisions: [],
    });
    (useExperimentsWorkspace as jest.Mock).mockReturnValue(workspace);

    render(<ExperimentsWorkspace />);

    expect(
      screen.getByText(/missing or mixed execution modes/i)
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Create Sandbox preview' })
    ).toBeDisabled();
  });

  it('restores a PAUSED provisioning and parity evidence from URL-backed ids', () => {
    mockRouter.query = {
      slug: 'acme',
      experiment: 'exp-frozen',
      launchPreview: 'launch-preview-1',
      launchProvisioning: 'provisioning-1',
    };
    const workspace = baseWorkspace({
      id: 'exp-frozen',
      product_id: 'product-1',
      name: 'Frozen test',
      state: 'frozen',
      revision: 4,
      record_schema_version: 2,
      version_id: 'experiment-version-1',
      created_at: '2026-09-09T00:00:00Z',
      draft_context: {
        currency: 'USD',
        budget: { daily_amount: '5', total_amount: '100' },
        schedule: {},
      },
      entries: [],
      candidates: [],
      judgements: [],
      proposals: [],
      decisions: [],
    });
    const preview = {
      id: 'launch-preview-1',
      workspace_id: 'workspace-1',
      meta_mode: 'customer_sandbox',
      meta_connection_id: 'connection-1',
      meta_binding_id: 'binding-1',
      experiment_id: 'exp-frozen',
      experiment_version_id: 'experiment-version-1',
      candidate_count: 2,
      campaign_config: { initial_status: 'PAUSED' },
      daily_budget_minor: 500,
      total_daily_cost_minor: 1000,
      currency: 'USD',
      preview_hash: 'b'.repeat(64),
      state: 'confirmed',
      confirmed_at: '2026-09-09T01:00:00Z',
      expires_at: '2099-09-09T02:00:00Z',
      created_at: '2026-09-09T00:30:00Z',
      candidates: [],
    };
    (useExperimentsWorkspace as jest.Mock).mockReturnValue(workspace);
    (useMetaLaunchPreview as jest.Mock).mockReturnValue(query(preview));
    (useMetaLaunchProvisioning as jest.Mock).mockReturnValue(
      query({
        id: 'provisioning-1',
        operation_id: 'operation-1',
        operation_state: 'succeeded',
        provisioning_state: 'provisioned',
        activation_state: 'paused',
        fencing_token: 1,
        steps: [{ step_type: 'create_campaign', state: 'applied' }],
        parity_check: {
          passed: true,
          all_candidates_present: true,
          budget_parity_verified: true,
          config_parity_verified: true,
          status_parity_verified: true,
        },
      })
    );

    render(<ExperimentsWorkspace />);

    expect(useMetaLaunchPreview).toHaveBeenCalledWith(
      'launch-preview-1',
      'customer_sandbox'
    );
    expect(useMetaLaunchProvisioning).toHaveBeenCalledWith(
      'provisioning-1',
      'customer_sandbox'
    );
    expect(screen.getByText('provisioned')).toBeInTheDocument();
    expect(screen.getByText('PAUSED')).toBeInTheDocument();
    expect(screen.getByText(/Parity check: passed/)).toBeInTheDocument();
    // The step type reads as copy, never as its code (#286).
    expect(screen.getByText('Create campaign')).toBeInTheDocument();
    expect(screen.queryByText('create_campaign')).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Reconcile uncertain operation' })
    ).not.toBeInTheDocument();
  });

  it('offers recovery only for an uncertain durable operation', async () => {
    mockRouter.query = {
      slug: 'acme',
      experiment: 'exp-frozen',
      launchPreview: 'launch-preview-1',
      launchProvisioning: 'provisioning-1',
    };
    const workspace = baseWorkspace({
      id: 'exp-frozen',
      product_id: 'product-1',
      name: 'Frozen test',
      state: 'frozen',
      revision: 4,
      record_schema_version: 2,
      version_id: 'experiment-version-1',
      created_at: '2026-09-09T00:00:00Z',
      draft_context: {
        currency: 'USD',
        budget: { daily_amount: '5', total_amount: '100' },
        schedule: {},
      },
      entries: [],
      candidates: [],
      judgements: [],
      proposals: [],
      decisions: [],
    });
    const preview = {
      id: 'launch-preview-1',
      workspace_id: 'workspace-1',
      meta_mode: 'customer_sandbox',
      meta_connection_id: 'connection-1',
      meta_binding_id: 'binding-1',
      experiment_id: 'exp-frozen',
      experiment_version_id: 'experiment-version-1',
      candidate_count: 2,
      campaign_config: { initial_status: 'PAUSED' },
      daily_budget_minor: 500,
      total_daily_cost_minor: 1000,
      currency: 'USD',
      preview_hash: 'b'.repeat(64),
      state: 'confirmed',
      confirmed_at: '2026-09-09T01:00:00Z',
      expires_at: '2099-09-09T02:00:00Z',
      created_at: '2026-09-09T00:30:00Z',
      candidates: [],
    };
    const provisioningQuery = query({
      id: 'provisioning-1',
      operation_id: 'operation-1',
      operation_state: 'uncertain',
      provisioning_state: 'partially_provisioned',
      activation_state: 'paused',
      fencing_token: 1,
      steps: [{ step_type: 'create_campaign', state: 'intent_committed' }],
      parity_check: null,
    });
    (useExperimentsWorkspace as jest.Mock).mockReturnValue(workspace);
    (useMetaLaunchPreview as jest.Mock).mockReturnValue(query(preview));
    (useMetaLaunchProvisioning as jest.Mock).mockReturnValue(provisioningQuery);

    render(<ExperimentsWorkspace />);
    fireEvent.click(
      screen.getByRole('button', { name: 'Reconcile uncertain operation' })
    );

    await waitFor(() => {
      expect(
        workspace.commands.recoverLaunchOperation.execute
      ).toHaveBeenCalledWith(
        expect.objectContaining({
          pathParams: { operation_id: 'operation-1' },
          body: { meta_mode: 'customer_sandbox' },
        })
      );
      expect(provisioningQuery.refresh).toHaveBeenCalled();
    });
  });

  it('requires every duplicate group resolved before freezing, then submits the plan', async () => {
    const workspace = baseWorkspace({
      id: 'exp-1',
      product_id: 'product-1',
      name: 'Duplicate concept draft',
      state: 'draft',
      revision: 3,
      record_schema_version: 2,
      created_at: '2026-09-09T00:00:00Z',
      draft_context: {
        product_version_id: 'pv-1',
        offer_version_id: 'ov-1',
        market: 'US',
        currency: 'USD',
        placement: 'feed',
        budget: { daily_amount: '100', total_amount: '3000' },
        schedule: {
          start_at: '2026-09-10T00:00:00Z',
          end_at: '2026-09-20T00:00:00Z',
        },
      },
      entries: [
        { id: 'entry-1', asset_version_id: 'asset-version-1' },
        { id: 'entry-2', asset_version_id: 'asset-version-2' },
      ],
      candidates: [],
      judgements: [],
      proposals: [],
      decisions: [],
    });
    (useExperimentsWorkspace as jest.Mock).mockReturnValue(workspace);
    (useAnalyzeDuplicates as jest.Mock).mockReturnValue(
      query({
        experiment_id: 'exp-1',
        analyzed_revision: 3,
        candidate_set_hash: 'candidate-set-hash-1',
        has_duplicates: true,
        duplicate_groups: [
          {
            duplicate_group_hash: 'group-1',
            dimension_key: 'headline',
            concept_key_id: 'concept-1',
            concept_key_name: 'Discount CTA',
            members: [
              {
                source_entry_id: 'entry-1',
                source_candidate_fingerprint: 'fp-1',
                asset_version_id: 'asset-version-1',
                display_label: 'Candidate 1',
              },
              {
                source_entry_id: 'entry-2',
                source_candidate_fingerprint: 'fp-2',
                asset_version_id: 'asset-version-2',
                display_label: 'Candidate 2',
              },
            ],
            allowed_actions: [
              'allow_controlled_duplicate',
              'exclude_source_candidate',
            ],
            remap_targets: [],
          },
        ],
      })
    );

    render(<ExperimentsWorkspace />);

    expect(
      screen.getByText('Duplicate Concept Resolution')
    ).toBeInTheDocument();
    const freezeButton = screen.getByRole('button', {
      name: /Freeze experiment/i,
    });
    expect(freezeButton).toBeDisabled();

    fireEvent.click(
      screen.getByLabelText(
        'I reviewed the candidates, budget, schedule, and immutable context.'
      )
    );
    expect(freezeButton).toBeDisabled();

    fireEvent.click(
      screen.getByRole('radio', { name: 'Allow (controlled duplicate)' })
    );
    expect(freezeButton).toBeDisabled();

    fireEvent.change(screen.getByLabelText('Reason (required)'), {
      target: {
        value: 'Same concept intentionally duplicated for a hold-out.',
      },
    });
    expect(freezeButton).toBeEnabled();

    fireEvent.click(freezeButton);

    await waitFor(() => {
      expect(workspace.commands.freeze.execute).toHaveBeenCalledWith(
        expect.objectContaining({
          pathParams: { experiment_id: 'exp-1' },
          body: {
            expected_revision: 3,
            analyzed_candidate_set_hash: 'candidate-set-hash-1',
            concept_resolution_plan: [
              {
                duplicate_group_hash: 'group-1',
                action: 'allow_controlled_duplicate',
                reason: 'Same concept intentionally duplicated for a hold-out.',
                source_candidate_fingerprints: ['fp-1', 'fp-2'],
                source_candidate_fingerprint: undefined,
                remap_concept_key_id: undefined,
              },
            ],
          },
        })
      );
      expect(workspace.detail.refresh).toHaveBeenCalled();
    });
  });

  it('resets the resolution plan and refetches on a stale-analysis freeze rejection', async () => {
    const workspace = baseWorkspace({
      id: 'exp-1',
      product_id: 'product-1',
      name: 'Duplicate concept draft',
      state: 'draft',
      revision: 3,
      record_schema_version: 2,
      created_at: '2026-09-09T00:00:00Z',
      draft_context: {
        product_version_id: 'pv-1',
        offer_version_id: 'ov-1',
        market: 'US',
        currency: 'USD',
        placement: 'feed',
        budget: { daily_amount: '100', total_amount: '3000' },
        schedule: {
          start_at: '2026-09-10T00:00:00Z',
          end_at: '2026-09-20T00:00:00Z',
        },
      },
      entries: [
        { id: 'entry-1', asset_version_id: 'asset-version-1' },
        { id: 'entry-2', asset_version_id: 'asset-version-2' },
      ],
      candidates: [],
      judgements: [],
      proposals: [],
      decisions: [],
    });
    const staleProblem = {
      type: 'about:blank',
      title: 'Concept resolution stale',
      status: 409,
      detail: 'The analyzed candidate set changed since this plan was built.',
      instance: '',
      code: 'concept_resolution_stale',
      trace_id: 'trace-1',
      retryable: true,
      field_errors: [],
    };
    workspace.commands.freeze.execute.mockResolvedValue({
      kind: 'problem',
      problem: staleProblem,
      meta: okMeta,
    });
    (useExperimentsWorkspace as jest.Mock).mockReturnValue(workspace);
    const analysisQuery = query({
      experiment_id: 'exp-1',
      analyzed_revision: 3,
      candidate_set_hash: 'candidate-set-hash-1',
      has_duplicates: false,
      duplicate_groups: [],
    });
    (useAnalyzeDuplicates as jest.Mock).mockReturnValue(analysisQuery);

    render(<ExperimentsWorkspace />);

    fireEvent.click(
      screen.getByLabelText(
        'I reviewed the candidates, budget, schedule, and immutable context.'
      )
    );
    fireEvent.click(screen.getByRole('button', { name: /Freeze experiment/i }));

    await waitFor(() => {
      expect(workspace.commands.freeze.execute).toHaveBeenCalled();
      expect(workspace.detail.refresh).toHaveBeenCalled();
      expect(analysisQuery.refresh).toHaveBeenCalled();
    });
  });
});
