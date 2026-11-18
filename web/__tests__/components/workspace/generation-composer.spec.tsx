/** @jest-environment jsdom */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { GenerationComposer } from '@/components/workspace/GenerationComposer';
import { MainActionProvider } from '@/components/workspace/marketing-focus';
import { readFileSync } from 'node:fs';
import path from 'node:path';
const messages: Record<string, string> = JSON.parse(
  readFileSync(path.join(process.cwd(), 'locales/en/common.json'), 'utf8')
);

const refresh = jest.fn().mockResolvedValue(undefined);
const replace = jest.fn().mockResolvedValue(true);
const gateway = {
  createGenerationDraft: jest.fn(),
  updateGenerationDraft: jest.fn(),
  createGenerationPreview: jest.fn(),
  submitGenerationRun: jest.fn(),
  getGenerationDraft: jest.fn(),
  getGenerationPreview: jest.fn(),
  createGenerationAssetVersionDownloadIntent: jest.fn(),
};
let query: Record<string, string> = {};
let runItems: unknown[] = [];
let runAssets: unknown[] = [];
const model = (id: string, available = true) => ({
  id,
  available,
  image_sizes: ['1K', '2K'],
  maximum_costs: { '1K': '0.30', '2K': '0.50' },
  unavailable_reason: available ? null : 'credential_required',
});
const provider = (id: string, label: string, available = true) => ({
  id,
  label,
  available,
  credential_ready: available,
  execution_mode: id === 'mock' ? 'mock' : 'byok',
  default_model: `${id}-model`,
  models: [model(`${id}-model`, available), model(`${id}-other`, available)],
  aspect_ratios: ['1:1', '4:5', '9:16', '16:9'],
  unavailable_reason: available ? null : 'credential_required',
});
let providers = [
  provider('gemini', 'Gemini'),
  provider('openai', 'OpenAI'),
  provider('seedream', 'Seedream'),
  provider('mock', 'Controlled Mock'),
];
const saved = {
  id: 'gd_saved',
  product_id: 'product_1',
  provider: 'gemini',
  model: 'gemini-model',
  execution_mode: 'byok',
  revision: 1,
  state: 'editing',
  selectors: { prompt: 'Product on a desk' },
  format_config: { aspect_ratio: '1:1', image_size: '1K', variants: 1 },
  allowed_actions: [],
};
const frozen = {
  purpose: 'initial_generation',
  id: 'preview_1',
  draft_id: 'gd_saved',
  draft_revision: 1,
  preview_hash: 'hash_1',
  recipe_input_hash: 'recipe_1',
  execution_mode: 'byok',
  maximum_cost: { amount: '0.30', currency: 'USD' },
  selection_maximum_cost: '0.30',
  currency: 'USD',
  quantity: 1,
  provider: 'gemini',
  model: 'gemini-model',
  valid_until: new Date(Date.now() + 600000).toISOString(),
  allowed_actions: [{ action: 'submit', allowed: true, reason_code: null }],
};

jest.mock('next/router', () => ({
  useRouter: () => ({ query, pathname: '/w/[slug]/generate', replace }),
}));
jest.mock('next-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, options?: { defaultValue?: string }) =>
      (messages as Record<string, string>)[key] ?? options?.defaultValue ?? key,
  }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: { slug: 'alpha', workspaceId: 'ws_alpha', role: 'MEMBER' },
  }),
}));
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => gateway,
}));
jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: (operation: string) => ({
    data:
      operation === 'get_generation_capabilities'
        ? {
            provider_options: providers,
            products: [{ id: 'product_1', name: 'Test product' }],
          }
        : operation === 'list_generation_runs'
          ? { items: runItems, next_cursor: null }
          : operation === 'list_generation_assets'
            ? { items: runAssets, next_cursor: null }
            : { items: [], next_cursor: null },
    problem: undefined,
    isLoading: false,
    refresh,
  }),
}));

beforeEach(() => {
  sessionStorage.clear();
  jest.clearAllMocks();
  query = {};
  runItems = [];
  runAssets = [];
  providers = [
    provider('gemini', 'Gemini'),
    provider('openai', 'OpenAI'),
    provider('seedream', 'Seedream'),
    provider('mock', 'Controlled Mock'),
  ];
  gateway.createGenerationDraft.mockResolvedValue({
    kind: 'created',
    data: saved,
  });
  gateway.createGenerationPreview.mockResolvedValue({
    kind: 'created',
    data: frozen,
  });
  gateway.submitGenerationRun.mockResolvedValue({
    kind: 'accepted',
    data: { operation_id: 'op_1', generation_run_id: 'run_1' },
  });
  gateway.getGenerationDraft.mockResolvedValue({ kind: 'ok', data: saved });
});

test('a reload checks the server preview before repeating an interrupted submission', async () => {
  sessionStorage.setItem(
    'generation-submission:ws_alpha',
    JSON.stringify({
      action: 'same-action',
      previewId: 'preview_1',
      previewHash: 'hash_1',
      revision: 1,
    })
  );
  gateway.getGenerationPreview.mockResolvedValue({
    kind: 'ok',
    data: { ...frozen, submitted_run_id: 'run_already_created' },
  });
  const onSubmitted = jest.fn();
  render(<GenerationComposer onSubmitted={onSubmitted} />);
  await waitFor(() => expect(gateway.getGenerationPreview).toHaveBeenCalled());
  await waitFor(() =>
    expect(sessionStorage.getItem('generation-submission:ws_alpha')).toBeNull()
  );
  expect(gateway.submitGenerationRun).not.toHaveBeenCalled();
  await waitFor(() => expect(onSubmitted).toHaveBeenCalledTimes(1));
});

function fillBrief() {
  fireEvent.change(screen.getByLabelText('Creative brief'), {
    target: { value: 'Product on a desk' },
  });
}

test('switching providers resets the model and submits the selected provider', async () => {
  render(<GenerationComposer />);
  fireEvent.click(screen.getByRole('radio', { name: /Seedream/ }));
  expect(screen.getByLabelText('Model version')).toHaveValue('seedream-model');
  fillBrief();
  fireEvent.click(screen.getByRole('button', { name: 'Save draft' }));
  await waitFor(() => expect(gateway.createGenerationDraft).toHaveBeenCalled());
  expect(gateway.createGenerationDraft.mock.calls[0][0].body).toMatchObject({
    provider: 'seedream',
    model: 'seedream-model',
    execution_mode: 'byok',
  });
});

test('an unavailable provider explains setup and cannot start a generation', () => {
  providers[1] = provider('openai', 'OpenAI', false);
  render(<GenerationComposer />);
  fireEvent.click(screen.getByRole('radio', { name: /OpenAI/ }));
  fillBrief();
  expect(
    screen.getByRole('button', { name: 'Preview and review cost' })
  ).toBeDisabled();
  expect(
    screen.getAllByText(/Workspace API key required/).length
  ).toBeGreaterThan(0);
  expect(gateway.submitGenerationRun).not.toHaveBeenCalled();
});

test('preview and confirmation forward the saved revision and preview hash', async () => {
  const onSubmitted = jest.fn();
  render(<GenerationComposer onSubmitted={onSubmitted} />);
  fillBrief();
  fireEvent.click(
    screen.getByRole('button', { name: 'Preview and review cost' })
  );
  const confirm = await screen.findByRole('button', {
    name: 'Confirm and generate',
  });
  expect(screen.getByText('USD 0.30')).toBeInTheDocument();
  expect(gateway.createGenerationPreview.mock.calls[0][0]).toMatchObject({
    draftId: 'gd_saved',
    ifMatch: 1,
  });
  fireEvent.click(confirm);
  await waitFor(() =>
    expect(gateway.submitGenerationRun).toHaveBeenCalledTimes(1)
  );
  expect(gateway.submitGenerationRun.mock.calls[0][0]).toMatchObject({
    ifMatch: 1,
    body: { preview_id: 'preview_1', preview_hash: 'hash_1' },
  });
  await waitFor(() => expect(onSubmitted).toHaveBeenCalledTimes(1));
});

test('changing the recipe discards the old confirmation', async () => {
  render(<GenerationComposer />);
  fillBrief();
  fireEvent.click(
    screen.getByRole('button', { name: 'Preview and review cost' })
  );
  await screen.findByRole('button', { name: 'Confirm and generate' });
  fireEvent.change(screen.getByLabelText('Aspect ratio'), {
    target: { value: '4:5' },
  });
  expect(
    screen.queryByRole('button', { name: 'Confirm and generate' })
  ).not.toBeInTheDocument();
});

test('a lost preview response retries the same intent without creating another draft', async () => {
  gateway.createGenerationPreview
    .mockRejectedValueOnce(new Error('network'))
    .mockResolvedValueOnce({ kind: 'created', data: frozen });
  render(<GenerationComposer />);
  fillBrief();
  fireEvent.click(
    screen.getByRole('button', { name: 'Preview and review cost' })
  );
  fireEvent.click(
    await screen.findByRole('button', { name: 'Retry the same request' })
  );
  await screen.findByRole('button', { name: 'Confirm and generate' });
  expect(gateway.createGenerationDraft).toHaveBeenCalledTimes(1);
  expect(gateway.createGenerationPreview).toHaveBeenCalledTimes(2);
  expect(
    gateway.createGenerationPreview.mock.calls[0][0].actionInstanceId
  ).toBe(gateway.createGenerationPreview.mock.calls[1][0].actionInstanceId);
});

test('reloading a saved draft restores its provider and model', async () => {
  query = { draft: 'gd_saved' };
  gateway.getGenerationDraft.mockResolvedValue({
    kind: 'ok',
    data: { ...saved, provider: 'seedream', model: 'seedream-other' },
  });
  render(<GenerationComposer />);
  await waitFor(() =>
    expect(screen.getByRole('radio', { name: /Seedream/ })).toBeChecked()
  );
  expect(screen.getByLabelText('Model version')).toHaveValue('seedream-other');
  expect(screen.getByLabelText('Creative brief')).toHaveValue(
    'Product on a desk'
  );
});

test('embedded preview and submission keep the parent marketing test route', async () => {
  query = { test: 'mp_parent' };
  gateway.updateGenerationDraft.mockResolvedValue({
    kind: 'ok',
    data: { ...saved, revision: 2 },
  });
  gateway.createGenerationPreview.mockResolvedValue({
    kind: 'created',
    data: { ...frozen, draft_revision: 2 },
  });
  render(<GenerationComposer embeddedDraftId="gd_saved" />);
  await waitFor(() =>
    expect(screen.getByLabelText('Creative brief')).toHaveValue(
      'Product on a desk'
    )
  );
  fireEvent.click(
    screen.getByRole('button', { name: 'Preview and review cost' })
  );
  const confirm = await screen.findByRole('button', {
    name: 'Confirm and generate',
  });
  expect(replace).not.toHaveBeenCalled();
  fireEvent.click(confirm);
  await waitFor(() =>
    expect(gateway.submitGenerationRun).toHaveBeenCalledTimes(1)
  );
  expect(replace).not.toHaveBeenCalled();
  expect(query.test).toBe('mp_parent');
  expect(screen.getByLabelText('Saved drafts')).toBeDisabled();
});

// J-32: inside a test page the composer is one step of the page.
const filled = () =>
  screen
    .getAllByRole('button')
    .filter((button) => button.className.includes('bg-gray-900'))
    .map((button) => button.textContent);

test('inside a test page model settings, the full prompt and workspace history are folded', async () => {
  runItems = [
    {
      id: 'gr_4b1d2c3e9f',
      provider: 'gemini',
      model: 'gemini-model',
      execution_mode: 'byok',
      status: 'succeeded',
      done: 1,
      total: 1,
    },
  ];
  gateway.getGenerationDraft.mockResolvedValue({ kind: 'ok', data: saved });
  gateway.updateGenerationDraft.mockResolvedValue({
    kind: 'ok',
    data: { ...saved, revision: 2 },
  });
  gateway.createGenerationPreview.mockResolvedValue({
    kind: 'created',
    data: { ...frozen, draft_revision: 2 },
  });
  render(
    <MainActionProvider value="generate">
      <GenerationComposer embeddedDraftId="gd_saved" />
    </MainActionProvider>
  );
  await waitFor(() =>
    expect(screen.getByLabelText('Creative brief')).toHaveValue(
      'Product on a desk'
    )
  );
  const settings = screen
    .getByText(messages['generation-settings-and-prompt'])
    .closest('details');
  expect(settings).not.toHaveAttribute('open');
  expect(settings).toContainElement(screen.getByLabelText('Creative brief'));
  const history = screen
    .getByText(messages['generation-runs-workspace'])
    .closest('details');
  expect(history).not.toHaveAttribute('open');
  // The run is named by provider and model; its id waits in technical details.
  const run = screen.getByText('gemini · gemini-model').closest('button')!;
  expect(run).not.toHaveTextContent('gr_4b1d2c3e9f');
  // One filled button at a time: preview first, then the paid confirmation.
  expect(filled()).toEqual([messages['generation-preview']]);
  fireEvent.click(
    screen.getByRole('button', { name: messages['generation-preview'] })
  );
  await screen.findByRole('button', {
    name: messages['generation-confirm-submit'],
  });
  expect(filled()).toEqual([messages['generation-confirm-submit']]);
});

test('while the test page waits on another step the composer stays secondary', async () => {
  gateway.getGenerationDraft.mockResolvedValue({ kind: 'ok', data: saved });
  render(
    <MainActionProvider value="ai_qc">
      <GenerationComposer embeddedDraftId="gd_saved" />
    </MainActionProvider>
  );
  await waitFor(() =>
    expect(screen.getByLabelText('Creative brief')).toHaveValue(
      'Product on a desk'
    )
  );
  expect(filled()).toEqual([]);
});

// Issue #219: a draft of ads-first ideas does not leave through the workbench.
describe('saving a run image', () => {
  const run = {
    id: 'gr_run',
    provider: 'openai',
    model: 'openai-model',
    execution_mode: 'byok',
    status: 'succeeded',
    done: 2,
    total: 2,
  };
  const image = (id: string, export_block: unknown = null) => ({
    id,
    provider: 'openai',
    model: 'openai-model',
    export_block,
  });
  const open = () => {
    render(<GenerationComposer />);
    fireEvent.click(
      screen.getByText('openai · openai-model').closest('button')!
    );
    return screen.getAllByRole('button', {
      name: messages['generation-download'],
    });
  };

  test('any image is saved with a link asked for saving; a draft ad is not offered', async () => {
    runItems = [run];
    runAssets = [
      image('av_plain'),
      image('av_draft', {
        status: 'draft',
        reasons: [{ code: 'facts_unverified', fact_ids: ['f1'] }],
      }),
      image('av_stale', {
        status: 'needs_redo',
        reasons: [{ code: 'facts_changed', fact_ids: ['f1'] }],
      }),
    ];
    gateway.createGenerationAssetVersionDownloadIntent.mockResolvedValue({
      kind: 'created',
      data: { download_grant: { url: 'https://storage.example/object' } },
    });
    const [plain, draft, stale] = open();
    expect(plain).toBeEnabled();
    expect(draft).toBeDisabled();
    expect(stale).toBeDisabled();
    expect(
      screen
        .getAllByTestId('generation-download-blocked')
        .map((note) => note.textContent)
    ).toEqual([
      messages['generation-download-draft'],
      messages['generation-download-needs-redo'],
    ]);

    fireEvent.click(plain);
    await waitFor(() =>
      expect(
        gateway.createGenerationAssetVersionDownloadIntent
      ).toHaveBeenCalledWith(
        expect.objectContaining({ versionId: 'av_plain', purpose: 'download' })
      )
    );
    fireEvent.click(draft);
    expect(
      gateway.createGenerationAssetVersionDownloadIntent
    ).toHaveBeenCalledTimes(1);
  });

  test('a refusal the list did not foresee is said in plain words', async () => {
    runItems = [run];
    runAssets = [image('av_was_exportable')];
    gateway.createGenerationAssetVersionDownloadIntent.mockResolvedValue({
      kind: 'problem',
      problem: { code: 'ads_not_exportable' },
    });
    const [button] = open();
    fireEvent.click(button);
    expect(await screen.findByRole('alert')).toHaveTextContent(
      messages['error-ads_not_exportable']
    );
  });

  test('any other failure keeps its own message', async () => {
    runItems = [run];
    runAssets = [image('av_plain')];
    gateway.createGenerationAssetVersionDownloadIntent.mockResolvedValue({
      kind: 'problem',
      problem: { code: 'asset_blob_unavailable' },
    });
    const [button] = open();
    fireEvent.click(button);
    expect(await screen.findByRole('alert')).toHaveTextContent(
      messages['generation-download-failed']
    );
  });
});
