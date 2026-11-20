/**
 * @jest-environment jsdom
 */
import type { ReactNode } from 'react';
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { SWRConfig } from 'swr';
import { StartPage } from '@/components/ideas/StartPage';
import type { FirstBatchQuote } from '@/lib/ideas/first-batch-view';

/**
 * The start page (ADR 0018, design §5.1, #195): paste a link, choose where the
 * ads run, and start with one button that says the most it can cost. Without
 * an OpenAI key the Owner adds one in the same form; anyone else is told to
 * ask the Owner and cannot start.
 */
const mockPush = jest.fn().mockResolvedValue(true);
jest.mock('next/router', () => ({
  useRouter: () => ({
    asPath: '/w/acme/start',
    query: {},
    push: mockPush,
    replace: jest.fn(),
  }),
}));
let mockRole: 'OWNER' | 'MEMBER' = 'OWNER';
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: { slug: 'acme', workspaceId: 'ws_1', role: mockRole },
  }),
}));
const mockGateway = {
  getMarketingFirstBatchQuote: jest.fn(),
  startMarketingFirstBatch: jest.fn(),
  createCredential: jest.fn(),
  bindCredential: jest.fn(),
  pollOperation: jest.fn(),
  listMarketingFirstBatches: jest.fn(),
};
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));
const mockStartUpload = jest.fn();
jest.mock('@/hooks/domain/useUpload', () => ({
  useUpload: () => ({ startUpload: mockStartUpload }),
}));

const LINK = 'https://acmecharge.example/products/home';

function ok<T>(data: T, status = 200, kind = 'ok') {
  return { kind, status, data, meta: {} };
}

function problem(status: number, code: string, params?: object) {
  return {
    kind: 'problem' as const,
    status,
    problem: {
      type: 'about:blank',
      title: code,
      status,
      detail: code,
      instance: '/x',
      code,
      trace_id: 'tr_1',
      retryable: status === 503,
      field_errors: [],
      ...(params ? { params } : {}),
    },
    meta: {},
  };
}

function quote(extra: Partial<FirstBatchQuote> = {}): FirstBatchQuote {
  return {
    available: true,
    unavailable_reason: null,
    execution_mode: 'byok',
    maximum_cost: '0.8625',
    currency: 'USD',
    quote_hash: 'qh_1',
    image_provider: 'openai',
    image_model: 'image-model',
    steps: [],
    ...extra,
  };
}

const NO_KEY = quote({
  available: false,
  unavailable_reason: 'openai_key_required',
  maximum_cost: '0',
  quote_hash: 'qh_none',
});

function Fresh({ children }: { children: ReactNode }) {
  return (
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      {children}
    </SWRConfig>
  );
}

function renderPage() {
  return render(
    <Fresh>
      <StartPage />
    </Fresh>
  );
}

const paste = () =>
  screen.getByLabelText('Product link, description or images');

beforeEach(() => {
  jest.clearAllMocks();
  mockRole = 'OWNER';
  mockGateway.startMarketingFirstBatch.mockResolvedValue(
    ok({ id: 'mfb_1', state: 'reading' }, 201, 'created')
  );
  mockGateway.listMarketingFirstBatches.mockResolvedValue(ok({ items: [] }));
  mockPush.mockResolvedValue(true);
});

describe('StartPage (#195)', () => {
  it('with a key: no key row, and the button carries the maximum; clicking starts with that quote', async () => {
    mockGateway.getMarketingFirstBatchQuote.mockResolvedValue(ok(quote()));
    renderPage();

    const start = await screen.findByRole('button', {
      name: 'Start · up to $0.87',
    });
    expect(screen.queryByTestId('start-key-row')).toBeNull();
    expect(screen.queryByLabelText('Your OpenAI API key')).toBeNull();
    expect(start).toBeDisabled(); // nothing pasted yet

    fireEvent.change(paste(), { target: { value: LINK } });
    fireEvent.change(screen.getByLabelText('Ads run in'), {
      target: { value: 'GB' },
    });
    fireEvent.click(start);

    await waitFor(() =>
      expect(mockPush).toHaveBeenCalledWith('/w/acme/start/mfb_1')
    );
    expect(mockGateway.startMarketingFirstBatch).toHaveBeenCalledTimes(1);
    const call = mockGateway.startMarketingFirstBatch.mock.calls[0][0];
    expect(call.workspaceSlug).toBe('acme');
    expect(call.actionInstanceId).toEqual(expect.any(String));
    expect(call.body).toEqual({
      url: LINK,
      text: null,
      upload_intent_ids: [],
      market: 'GB',
      ad_language: 'en-US',
      strategy_language: 'en-US',
      execution_mode: 'byok',
      quote_hash: 'qh_1',
    });
    expect(mockGateway.createCredential).not.toHaveBeenCalled();
  });

  it('without a key, the Owner gets a key row; saving checks it with OpenAI, switches it on, then the button shows the maximum', async () => {
    mockGateway.getMarketingFirstBatchQuote
      .mockResolvedValueOnce(ok(NO_KEY))
      .mockResolvedValue(ok(quote({ quote_hash: 'qh_after_key' })));
    mockGateway.createCredential.mockResolvedValue(
      ok({ credential_version_id: 'cv_1' })
    );
    mockGateway.bindCredential.mockResolvedValue(ok(null));
    renderPage();

    const row = await screen.findByTestId('start-key-row');
    expect(
      within(row).getByRole('link', { name: 'How do I get one?' })
    ).toHaveAttribute('href', 'https://platform.openai.com/api-keys');
    const save = screen.getByRole('button', { name: 'Save key' });
    fireEvent.change(paste(), { target: { value: LINK } });
    expect(save).toBeDisabled(); // no key typed yet
    fireEvent.change(screen.getByLabelText('Your OpenAI API key'), {
      target: { value: 'sk-test-fixture' },
    });
    fireEvent.click(save);

    expect(
      await screen.findByText('Key saved. OpenAI confirmed that it works.')
    ).toBeInTheDocument();
    expect(mockGateway.createCredential).toHaveBeenCalledWith(
      expect.objectContaining({
        workspaceSlug: 'acme',
        body: {
          credential_key: 'openai_api_key',
          provider: 'openai',
          plaintext_b64: btoa('sk-test-fixture'),
          display_label: null,
          verify: true,
        },
      })
    );
    expect(mockGateway.bindCredential).toHaveBeenCalledWith(
      expect.objectContaining({
        credentialVersionId: 'cv_1',
        scope: 'generation',
        body: { provider: 'openai' },
      })
    );
    // The maximum was not on the button when it was clicked, so nothing
    // started: the button now says it, and the next click confirms it.
    const start = await screen.findByRole('button', {
      name: 'Start · up to $0.87',
    });
    expect(mockGateway.startMarketingFirstBatch).not.toHaveBeenCalled();
    expect(screen.queryByTestId('start-key-row')).toBeNull();
    fireEvent.click(start);
    await waitFor(() =>
      expect(mockGateway.startMarketingFirstBatch).toHaveBeenCalledWith(
        expect.objectContaining({
          body: expect.objectContaining({ quote_hash: 'qh_after_key' }),
        })
      )
    );
  });

  it('starts straight after saving the key when the button already carried a maximum that covers the new quote', async () => {
    mockGateway.getMarketingFirstBatchQuote
      .mockResolvedValueOnce(ok({ ...NO_KEY, maximum_cost: '0.90' }))
      .mockResolvedValue(ok(quote({ quote_hash: 'qh_after_key' })));
    mockGateway.createCredential.mockResolvedValue(
      ok({ credential_version_id: 'cv_1' })
    );
    mockGateway.bindCredential.mockResolvedValue(ok(null));
    renderPage();

    const save = await screen.findByRole('button', {
      name: 'Save key and start · up to $0.90',
    });
    fireEvent.change(paste(), { target: { value: LINK } });
    fireEvent.change(screen.getByLabelText('Your OpenAI API key'), {
      target: { value: 'sk-test-fixture' },
    });
    fireEvent.click(save);

    await waitFor(() =>
      expect(mockPush).toHaveBeenCalledWith('/w/acme/start/mfb_1')
    );
    expect(
      mockGateway.startMarketingFirstBatch.mock.calls[0][0].body.quote_hash
    ).toBe('qh_after_key');
  });

  it('without a key, someone who is not the Owner sees no key field, is told to ask the Owner, and cannot start', async () => {
    mockRole = 'MEMBER';
    mockGateway.getMarketingFirstBatchQuote.mockResolvedValue(ok(NO_KEY));
    renderPage();

    expect(
      await screen.findByText(
        'Ask the workspace Owner to add an OpenAI API key first. Only the Owner can add keys.'
      )
    ).toBeInTheDocument();
    expect(screen.queryByTestId('start-key-row')).toBeNull();
    expect(screen.queryByLabelText('Your OpenAI API key')).toBeNull();
    fireEvent.change(paste(), { target: { value: LINK } });
    const start = screen.getByRole('button', { name: 'Start' });
    expect(start).toBeDisabled();
    fireEvent.submit(start.closest('form')!);
    expect(mockGateway.startMarketingFirstBatch).not.toHaveBeenCalled();
    expect(mockGateway.createCredential).not.toHaveBeenCalled();
  });

  it.each([
    [
      problem(422, 'provider_key_rejected', { reason: 'key_invalid' }),
      'OpenAI says this key is not valid, so it was not saved. Check that you copied all of it.',
    ],
    [
      problem(422, 'provider_key_rejected', { reason: 'quota_exhausted' }),
      "This key's OpenAI account has no credit left or has reached its spending limit, so the key was not saved.",
    ],
    [
      problem(503, 'provider_key_check_unavailable'),
      "We couldn't reach OpenAI to check the key, so it was not saved. Try again in a moment.",
    ],
    [
      problem(422, 'provider_key_check_unsupported'),
      "This key can't be checked before saving, so it was not saved. Paste an OpenAI key.",
    ],
  ])(
    'a refused key says why and does not start (%#)',
    async (refusal, message) => {
      mockGateway.getMarketingFirstBatchQuote.mockResolvedValue(ok(NO_KEY));
      mockGateway.createCredential.mockResolvedValue(refusal);
      renderPage();

      const save = await screen.findByRole('button', { name: 'Save key' });
      fireEvent.change(paste(), { target: { value: LINK } });
      fireEvent.change(screen.getByLabelText('Your OpenAI API key'), {
        target: { value: 'sk-test-fixture' },
      });
      fireEvent.click(save);

      expect(await screen.findByRole('alert')).toHaveTextContent(message);
      expect(mockGateway.bindCredential).not.toHaveBeenCalled();
      expect(mockGateway.startMarketingFirstBatch).not.toHaveBeenCalled();
      // Still the key row, so the key can be fixed and saved again.
      expect(screen.getByTestId('start-key-row')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Save key' })).toBeEnabled();
    }
  );

  it('keeps the button busy until the reading page has opened (#204)', async () => {
    mockGateway.getMarketingFirstBatchQuote.mockResolvedValue(ok(quote()));
    // The page change takes a moment; the button must not come back meanwhile.
    let opened: (value: boolean) => void = () => undefined;
    mockPush.mockReturnValue(new Promise<boolean>((done) => (opened = done)));
    renderPage();
    const start = await screen.findByRole('button', {
      name: 'Start · up to $0.87',
    });
    fireEvent.change(paste(), { target: { value: LINK } });
    fireEvent.click(start);
    await waitFor(() =>
      expect(mockPush).toHaveBeenCalledWith('/w/acme/start/mfb_1')
    );
    expect(screen.getByRole('button', { name: /Starting…/ })).toBeDisabled();
    opened(true);
  });

  it('comes back when the reading page did not open (#204)', async () => {
    mockGateway.getMarketingFirstBatchQuote.mockResolvedValue(ok(quote()));
    mockPush.mockResolvedValue(false);
    renderPage();
    const start = await screen.findByRole('button', {
      name: 'Start · up to $0.87',
    });
    fireEvent.change(paste(), { target: { value: LINK } });
    fireEvent.click(start);
    expect(
      await screen.findByRole('button', { name: 'Start · up to $0.87' })
    ).toBeEnabled();
  });

  it('a server error while saving the key clears it, since it may have been stored (#204)', async () => {
    mockGateway.getMarketingFirstBatchQuote.mockResolvedValue(ok(NO_KEY));
    mockGateway.createCredential.mockResolvedValue(
      problem(500, 'internal_error')
    );
    renderPage();
    const save = await screen.findByRole('button', { name: 'Save key' });
    fireEvent.change(paste(), { target: { value: LINK } });
    fireEvent.change(screen.getByLabelText('Your OpenAI API key'), {
      target: { value: 'sk-test-fixture' },
    });
    fireEvent.click(save);
    expect(await screen.findByRole('alert')).toHaveTextContent(
      "We couldn't confirm whether the key was saved. Refresh the page to check."
    );
    expect(screen.getByLabelText('Your OpenAI API key')).toHaveValue('');
    expect(mockGateway.bindCredential).not.toHaveBeenCalled();
  });

  it('over the spending limit: the Owner gets the way to the limits (#204)', async () => {
    mockGateway.getMarketingFirstBatchQuote.mockResolvedValue(
      ok(
        quote({
          available: false,
          unavailable_reason: 'budget_exceeded',
          maximum_cost: '0',
        })
      )
    );
    renderPage();
    expect(
      await screen.findByText(
        "This would go over the workspace's spending limit. The Owner can raise it in Settings."
      )
    ).toBeInTheDocument();
    expect(
      screen.getByRole('link', { name: 'Change the limits' })
    ).toHaveAttribute('href', '/w/acme/settings#spend-guardrail');
  });

  it('a Member is told about the spending limit but gets no link to change it (#204)', async () => {
    mockRole = 'MEMBER';
    mockGateway.getMarketingFirstBatchQuote.mockResolvedValue(
      ok(
        quote({
          available: false,
          unavailable_reason: 'budget_exceeded',
          maximum_cost: '0',
        })
      )
    );
    renderPage();
    await screen.findByText(
      "This would go over the workspace's spending limit. The Owner can raise it in Settings."
    );
    expect(
      screen.queryByRole('link', { name: 'Change the limits' })
    ).toBeNull();
  });

  it('lists a reading that was left before its product existed, with the way back (#204)', async () => {
    mockGateway.getMarketingFirstBatchQuote.mockResolvedValue(ok(quote()));
    mockGateway.listMarketingFirstBatches.mockResolvedValue(
      ok({
        items: [
          {
            id: 'mfb_9',
            state: 'needs_attention',
            needs_attention: true,
            reason: 'several_models',
            source_url: LINK,
            created_at: '2026-11-17T08:00:00+00:00',
            updated_at: '2026-11-17T08:01:00+00:00',
          },
        ],
      })
    );
    renderPage();
    const left = await screen.findByTestId('unfinished-readings');
    expect(left).toHaveTextContent('acmecharge.example/products/home');
    expect(left).toHaveTextContent(
      'Stopped and waiting for you before the product could be made.'
    );
    expect(
      within(left).getByRole('link', { name: 'Sort it out' })
    ).toHaveAttribute('href', '/w/acme/start/mfb_9');
    expect(document.body.textContent).not.toMatch(/mfb_/);
  });

  it('says the site is not set up when checking the ads is not configured (S9, #197)', async () => {
    mockGateway.getMarketingFirstBatchQuote.mockResolvedValue(
      ok(
        quote({
          available: false,
          unavailable_reason: 'qc_not_configured',
          maximum_cost: '0',
        })
      )
    );
    renderPage();
    expect(
      await screen.findByText(
        "Making ads isn't fully set up on this site yet, so you can't start. Contact the administrator."
      )
    ).toBeInTheDocument();
  });

  it('says plainly why starting is not possible when there is no price, and keeps the button off', async () => {
    mockGateway.getMarketingFirstBatchQuote.mockResolvedValue(
      ok(
        quote({
          available: false,
          unavailable_reason: 'price_unavailable',
          maximum_cost: '0',
        })
      )
    );
    renderPage();

    expect(
      await screen.findByText(
        "The cost of making ads isn't set up on this site yet, so you can't start. Contact the administrator."
      )
    ).toBeInTheDocument();
    fireEvent.change(paste(), { target: { value: LINK } });
    expect(screen.getByRole('button', { name: 'Start' })).toBeDisabled();
    expect(screen.queryByTestId('start-key-row')).toBeNull();
  });

  it('asks for the quote again and says so when starting is refused as unavailable', async () => {
    mockGateway.getMarketingFirstBatchQuote.mockResolvedValue(ok(quote()));
    mockGateway.startMarketingFirstBatch.mockResolvedValue(
      problem(409, 'first_batch_unavailable', { reason: 'price_unavailable' })
    );
    renderPage();

    const start = await screen.findByRole('button', {
      name: 'Start · up to $0.87',
    });
    fireEvent.change(paste(), { target: { value: LINK } });
    fireEvent.click(start);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      "You can't start right now. The page now says why."
    );
    expect(mockPush).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(mockGateway.getMarketingFirstBatchQuote).toHaveBeenCalledTimes(2)
    );
  });

  it('a start that gets no answer says so, and the next click replays the same start with the same uploads', async () => {
    mockGateway.getMarketingFirstBatchQuote.mockResolvedValue(ok(quote()));
    mockStartUpload.mockResolvedValue({
      kind: 'accepted',
      uploadIntentId: 'ui_1',
      operationRecordId: 'op_upload',
    });
    mockGateway.pollOperation.mockResolvedValue(ok({ state: 'succeeded' }));
    mockGateway.startMarketingFirstBatch
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(
        ok({ id: 'mfb_1', state: 'reading' }, 201, 'created')
      );
    const { container } = renderPage();

    const start = await screen.findByRole('button', {
      name: 'Start · up to $0.87',
    });
    const file = new File(['png'], 'charger.png', { type: 'image/png' });
    fireEvent.change(container.querySelector('input[type="file"]')!, {
      target: { files: [file] },
    });
    fireEvent.click(start);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      "We couldn't confirm whether it started. Click again; it won't start twice."
    );
    const again = screen.getByRole('button', { name: 'Start · up to $0.87' });
    expect(again).toBeEnabled();
    fireEvent.click(again);

    await waitFor(() =>
      expect(mockPush).toHaveBeenCalledWith('/w/acme/start/mfb_1')
    );
    const [first, second] = mockGateway.startMarketingFirstBatch.mock.calls;
    expect(second[0].actionInstanceId).toBe(first[0].actionInstanceId);
    expect(second[0].body).toEqual(first[0].body);
    // The images went up once; the replay sends the same upload ids.
    expect(mockStartUpload).toHaveBeenCalledTimes(1);
  });

  it('uploads dropped images first and starts with them', async () => {
    mockGateway.getMarketingFirstBatchQuote.mockResolvedValue(ok(quote()));
    mockStartUpload.mockResolvedValue({
      kind: 'accepted',
      uploadIntentId: 'ui_1',
      operationRecordId: 'op_upload',
    });
    mockGateway.pollOperation.mockResolvedValue(ok({ state: 'succeeded' }));
    const { container } = renderPage();

    const start = await screen.findByRole('button', {
      name: 'Start · up to $0.87',
    });
    const file = new File(['png'], 'charger.png', { type: 'image/png' });
    const input = container.querySelector('input[type="file"]')!;
    fireEvent.change(input, { target: { files: [file] } });
    fireEvent.click(start);

    await waitFor(() => expect(mockPush).toHaveBeenCalled());
    expect(mockStartUpload).toHaveBeenCalledWith(
      expect.objectContaining({ file, ownerKind: 'product_intake' })
    );
    expect(
      mockGateway.startMarketingFirstBatch.mock.calls[0][0].body
    ).toMatchObject({ url: null, text: null, upload_intent_ids: ['ui_1'] });
  });

  it('shows six made-up Acme Charge ads below, each marked as an example', async () => {
    mockGateway.getMarketingFirstBatchQuote.mockResolvedValue(ok(quote()));
    renderPage();
    await screen.findByRole('button', { name: 'Start · up to $0.87' });

    const examples = screen.getByTestId('start-examples');
    expect(
      within(examples).getByRole('heading', {
        name: 'Example: 3 claims and 6 ads for Acme Charge',
      })
    ).toBeInTheDocument();
    const images = within(examples).getAllByRole('img');
    expect(images).toHaveLength(6);
    for (const image of images)
      expect(image.getAttribute('src')).toMatch(/^\/example-ad-[1-6]\.svg$/);
    expect(within(examples).getAllByText('Example')).toHaveLength(6);
    expect(
      within(examples).getByText('Plug in at night and forget about it')
    ).toBeInTheDocument();
  });
});
