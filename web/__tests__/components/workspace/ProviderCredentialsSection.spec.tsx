/** @jest-environment jsdom */
import {
  render,
  screen,
  fireEvent,
  waitFor,
  within,
} from '@testing-library/react';

const mockRefreshList = jest.fn();
const mockRefreshCapabilities = jest.fn();
let mockWorkspace = { workspaceId: 'ws_test', slug: 'test-ws' };
const executeByOperation: Record<string, jest.Mock> = {
  create_credential: jest.fn(),
  rotate_credential: jest.fn(),
  bind_credential: jest.fn(),
  revoke_credential: jest.fn(),
};

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: mockWorkspace }),
}));

jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: jest.fn(),
}));

jest.mock('@/hooks/domain/useDomainCommand', () => ({
  useDomainCommand: jest.fn((operationId: string) => ({
    execute: executeByOperation[operationId],
    retry: jest.fn(),
    reset: jest.fn(),
    isPending: false,
    result: undefined,
    problem: undefined,
    reconciliation: undefined,
  })),
}));

jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => ({}),
}));

import {
  ProviderCredentialsSection,
  utf8ToBase64,
  type CredentialMetadata,
} from '@/components/workspace/ProviderCredentialsSection';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';

const mockUseDomainQuery = jest.mocked(useDomainQuery);

const OPENAI_KEY: CredentialMetadata = {
  credential_version_id: 'cv_openai_1',
  credential_key: 'openai_api_key',
  version_no: 1,
  provider: 'openai',
  display_label: 'Team key',
  state: 'active',
  value_sha256: 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6',
  created_at: '2026-10-01T00:00:00Z',
  rotated_at: null,
  revoked_at: null,
};

function queryState(data: unknown, refresh: jest.Mock, problem?: unknown) {
  return {
    data,
    problem,
    isLoading: false,
    isStale: false,
    lastSuccessfulAt: null,
    refresh,
  } as any;
}

/** Points `useDomainQuery` at the two reads the section makes. */
function arrangeReads({
  credentials = [] as CredentialMetadata[],
  readiness = {} as Record<string, unknown>,
  listProblem = undefined as unknown,
}: {
  credentials?: CredentialMetadata[];
  readiness?: Record<string, unknown>;
  listProblem?: unknown;
} = {}) {
  mockUseDomainQuery.mockImplementation(((operationId: string) => {
    if (operationId === 'list_credentials') {
      return queryState(
        listProblem ? undefined : credentials,
        mockRefreshList,
        listProblem
      );
    }
    if (operationId === 'get_generation_capabilities') {
      return queryState(
        {
          providers: ['mock', 'openai'],
          models: {},
          modes: ['mock'],
          credential_readiness: readiness,
        },
        mockRefreshCapabilities
      );
    }
    throw new Error(`unexpected read ${operationId}`);
  }) as any);
}

const success = (data: unknown) => ({
  kind: 'success',
  data,
  meta: { etag: null, retryAt: null, location: null },
});

const problem = (code: string, status = 409) => ({
  kind: 'problem',
  problem: {
    type: `urn:marketai:error:${code}`,
    title: 'Request failed',
    status,
    detail: `detail for ${code}`,
    instance: '',
    code,
    trace_id: 'trace-xyz',
    retryable: false,
    field_errors: [],
  },
  meta: { etag: null, retryAt: null, location: null },
});

/**
 * `fetch` double that records every request. MFA is checked only at login
 * (ADR 0015), so a credential command sends nothing besides the Domain
 * command itself (mocked above): no step-up grant, no MFA challenge.
 */
function arrangeFetch() {
  const calls: Array<{ url: string; body: any }> = [];
  const fetchMock = jest.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : {} });
    return { ok: false, status: 500, json: async () => ({}) } as Response;
  });
  Object.defineProperty(window, 'fetch', {
    configurable: true,
    writable: true,
    value: fetchMock,
  });
  return { fetchMock, calls };
}

describe.each([
  { provider: 'openai', credentialKey: 'openai_api_key' },
  { provider: 'gemini', credentialKey: 'gemini_api_key' },
  { provider: 'seedream', credentialKey: 'ark_api_key' },
])('ProviderCredentialsSection — $provider', ({ provider, credentialKey }) => {
  const KEY = {
    ...OPENAI_KEY,
    provider,
    credential_key: credentialKey,
    credential_version_id: `cv_${provider}_1`,
  };
  const nextVersionId = `cv_${provider}_2`;

  beforeEach(() => {
    mockWorkspace = { workspaceId: 'ws_test', slug: 'test-ws' };
    jest.clearAllMocks();
    for (const mock of Object.values(executeByOperation)) mock.mockReset();
    mockRefreshList.mockResolvedValue(undefined);
    mockRefreshCapabilities.mockResolvedValue(undefined);
  });

  test('utf8ToBase64 produces the standard base64 the Domain decodes', () => {
    expect(utf8ToBase64('sk-proj-abc')).toBe('c2stcHJvai1hYmM=');
    expect(utf8ToBase64('密钥')).toBe('5a+G6ZKl');
  });

  test('offers to add a key when the Workspace has none', () => {
    arrangeReads();
    render(<ProviderCredentialsSection />);
    expect(
      screen.getByTestId('provider-credentials-section')
    ).toBeInTheDocument();
    expect(
      screen.getByTestId(`add-${provider}-credential`)
    ).toBeInTheDocument();
    expect(
      screen.queryByTestId(`${provider}-credential-status`)
    ).not.toBeInTheDocument();
  });

  test('shows the list problem with a retry', () => {
    arrangeReads({
      listProblem: {
        type: 'urn:marketai:error:owner-role-required',
        title: 'Forbidden',
        status: 403,
        code: 'owner_role_required',
        trace_id: 'trace-1',
        retryable: false,
      },
    });
    render(<ProviderCredentialsSection />);
    expect(
      screen.getByTestId('provider-credentials-list-problem')
    ).toHaveTextContent('owner_role_required');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(mockRefreshList).toHaveBeenCalled();
  });

  test('opens an empty provider-specific secret field without reusing the login password', () => {
    arrangeReads();
    render(<ProviderCredentialsSection />);
    fireEvent.click(screen.getByTestId(`add-${provider}-credential`));
    const field = screen.getByTestId(`${provider}-credential-key`);
    expect(field).toHaveAttribute('type', 'password');
    expect(field).toHaveAttribute('name', `${provider}-provider-api-key`);
    expect(field).toHaveAttribute('autocomplete', 'new-password');
    expect(field).toHaveAttribute('data-1p-ignore');
    expect(field).toHaveAttribute('data-lpignore', 'true');
    expect(field).toHaveValue('');
    expect(screen.getByTestId(`save-${provider}-credential`)).toBeDisabled();
    expect(executeByOperation.create_credential).not.toHaveBeenCalled();
  });

  test('saving a key creates it, then binds the new version, without asking for a code', async () => {
    arrangeReads();
    const { calls } = arrangeFetch();
    executeByOperation.create_credential.mockResolvedValue(
      success({ ...KEY, credential_version_id: 'cv_new' })
    );
    executeByOperation.bind_credential.mockResolvedValue(
      success({ binding_id: 'cb_1' })
    );

    render(<ProviderCredentialsSection />);
    fireEvent.click(screen.getByTestId(`add-${provider}-credential`));
    fireEvent.change(screen.getByTestId(`${provider}-credential-label`), {
      target: { value: '  Team key ' },
    });
    fireEvent.change(screen.getByTestId(`${provider}-credential-key`), {
      target: { value: ' sk-proj-abc ' },
    });
    fireEvent.click(screen.getByTestId(`save-${provider}-credential`));

    await waitFor(() =>
      expect(executeByOperation.bind_credential).toHaveBeenCalledTimes(1)
    );

    expect(calls).toEqual([]);
    expect(screen.queryByTestId('mfa-challenge-code')).not.toBeInTheDocument();

    const createParams = executeByOperation.create_credential.mock.calls[0][0];
    expect(createParams.body).toEqual({
      credential_key: credentialKey,
      provider,
      plaintext_b64: utf8ToBase64('sk-proj-abc'),
      display_label: 'Team key',
    });
    expect(createParams).not.toHaveProperty('stepUpGrant');
    expect(createParams.actionInstanceId).toBeTruthy();

    const bindParams = executeByOperation.bind_credential.mock.calls[0][0];
    expect(bindParams.pathParams).toEqual({
      credential_version_id: 'cv_new',
      scope: 'generation',
    });
    expect(bindParams.body).toEqual({ provider });
    expect(bindParams).not.toHaveProperty('stepUpGrant');

    await waitFor(() => expect(mockRefreshList).toHaveBeenCalled());
    expect(mockRefreshCapabilities).toHaveBeenCalled();
    expect(
      screen.queryByTestId(`${provider}-credential-form`)
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId(`${provider}-credential-problem`)
    ).not.toBeInTheDocument();
  });

  test('a refused create shows the problem and sends nothing else', async () => {
    arrangeReads();
    arrangeFetch();
    executeByOperation.create_credential.mockResolvedValue(
      problem('credential_key_active')
    );

    render(<ProviderCredentialsSection />);
    fireEvent.click(screen.getByTestId(`add-${provider}-credential`));
    fireEvent.change(screen.getByTestId(`${provider}-credential-key`), {
      target: { value: 'sk-proj-abc' },
    });
    fireEvent.click(screen.getByTestId(`save-${provider}-credential`));

    await waitFor(() =>
      expect(
        screen.getByTestId(`${provider}-credential-problem`)
      ).toHaveTextContent('credential_key_active')
    );
    expect(
      screen.getByTestId(`${provider}-credential-problem`)
    ).toHaveTextContent('trace trace-xyz');
    expect(executeByOperation.bind_credential).not.toHaveBeenCalled();
    // The form stays open so the Owner can correct and retry.
    expect(
      screen.getByTestId(`${provider}-credential-form`)
    ).toBeInTheDocument();
  });

  test('a bound key is reported ready and can be removed', async () => {
    arrangeReads({
      credentials: [KEY],
      readiness: {
        [provider]: {
          has_active_credential: true,
          credential_version_id: KEY.credential_version_id,
        },
      },
    });
    const { calls } = arrangeFetch();
    executeByOperation.revoke_credential.mockResolvedValue(
      success({ ok: true })
    );

    render(<ProviderCredentialsSection />);
    const status = screen.getByTestId(`${provider}-credential-status`);
    expect(status).toHaveAttribute('data-bound', 'true');
    expect(status).toHaveTextContent('Key enabled for generation');
    expect(screen.getByText('Team key')).toBeInTheDocument();
    expect(screen.getByText('v1')).toBeInTheDocument();
    expect(screen.getByText('a1b2c3d4e5f6a7b8')).toBeInTheDocument();
    expect(
      screen.queryByTestId(`bind-${provider}-credential`)
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId(`add-${provider}-credential`)
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId(`remove-${provider}-credential`));
    await waitFor(() =>
      expect(executeByOperation.revoke_credential).toHaveBeenCalledTimes(1)
    );
    expect(calls).toEqual([]);
    const revokeParams = executeByOperation.revoke_credential.mock.calls[0][0];
    expect(revokeParams.pathParams).toEqual({
      credential_version_id: KEY.credential_version_id,
    });
    expect(revokeParams.body.reason).toBeTruthy();
    await waitFor(() => expect(mockRefreshCapabilities).toHaveBeenCalled());
  });

  test('a saved key the binding does not point at can be enabled in place', async () => {
    arrangeReads({
      credentials: [KEY],
      readiness: {
        [provider]: {
          has_active_credential: false,
          credential_version_id: null,
        },
      },
    });
    arrangeFetch();
    executeByOperation.bind_credential.mockResolvedValue(
      success({ binding_id: 'cb_1' })
    );

    render(<ProviderCredentialsSection />);
    expect(screen.getByTestId(`${provider}-credential-status`)).toHaveAttribute(
      'data-bound',
      'false'
    );
    fireEvent.click(screen.getByTestId(`bind-${provider}-credential`));
    await waitFor(() =>
      expect(executeByOperation.bind_credential).toHaveBeenCalledTimes(1)
    );
    expect(
      executeByOperation.bind_credential.mock.calls[0][0].pathParams
    ).toEqual({
      credential_version_id: KEY.credential_version_id,
      scope: 'generation',
    });
  });

  test('replacing a key rotates it and binds the new version', async () => {
    arrangeReads({
      credentials: [KEY],
      readiness: {
        [provider]: {
          has_active_credential: true,
          credential_version_id: KEY.credential_version_id,
        },
      },
    });
    const { calls } = arrangeFetch();
    executeByOperation.rotate_credential.mockResolvedValue(
      success({
        ...KEY,
        credential_version_id: nextVersionId,
        version_no: 2,
      })
    );
    executeByOperation.bind_credential.mockResolvedValue(
      success({ binding_id: 'cb_1' })
    );

    render(<ProviderCredentialsSection />);
    fireEvent.click(screen.getByTestId(`replace-${provider}-credential`));
    expect(
      screen.queryByTestId(`${provider}-credential-label`)
    ).not.toBeInTheDocument();
    fireEvent.change(screen.getByTestId(`${provider}-credential-key`), {
      target: { value: 'sk-proj-new' },
    });
    fireEvent.click(screen.getByTestId(`save-${provider}-credential`));

    await waitFor(() =>
      expect(executeByOperation.bind_credential).toHaveBeenCalledTimes(1)
    );
    expect(calls).toEqual([]);
    const rotateParams = executeByOperation.rotate_credential.mock.calls[0][0];
    expect(rotateParams.pathParams).toEqual({
      credential_key: credentialKey,
    });
    expect(rotateParams.body).toEqual({
      new_plaintext_b64: utf8ToBase64('sk-proj-new'),
    });
    expect(executeByOperation.create_credential).not.toHaveBeenCalled();
  });

  test('failed binding recovers from saved metadata without submitting the key again', async () => {
    arrangeReads();
    arrangeFetch();
    executeByOperation.create_credential.mockResolvedValue(success(KEY));
    executeByOperation.bind_credential
      .mockResolvedValueOnce(problem('credential_not_bindable'))
      .mockResolvedValueOnce(success({ binding_id: 'cb_new' }));
    const ui = render(<ProviderCredentialsSection />);
    fireEvent.click(screen.getByTestId(`add-${provider}-credential`));
    fireEvent.change(screen.getByTestId(`${provider}-credential-key`), {
      target: { value: 'fixture-only-key' },
    });
    fireEvent.click(screen.getByTestId(`save-${provider}-credential`));
    await waitFor(() =>
      expect(
        screen.getByTestId(`${provider}-credential-problem`)
      ).toHaveTextContent('credential_not_bindable')
    );
    expect(
      screen.queryByTestId(`${provider}-credential-key`)
    ).not.toBeInTheDocument();
    arrangeReads({ credentials: [KEY] });
    ui.rerender(<ProviderCredentialsSection />);
    fireEvent.click(screen.getByTestId(`bind-${provider}-credential`));
    await waitFor(() =>
      expect(executeByOperation.bind_credential).toHaveBeenCalledTimes(2)
    );
    expect(executeByOperation.create_credential).toHaveBeenCalledTimes(1);
    expect(executeByOperation.rotate_credential).not.toHaveBeenCalled();
  });

  test('ambiguous save clears the secret and blocks new mutations until status is reloaded', async () => {
    arrangeReads();
    arrangeFetch();
    executeByOperation.create_credential.mockRejectedValue(
      new Error('fixture connection lost')
    );
    render(<ProviderCredentialsSection />);
    fireEvent.click(screen.getByTestId(`add-${provider}-credential`));
    fireEvent.change(screen.getByTestId(`${provider}-credential-key`), {
      target: { value: 'fixture-only-key' },
    });
    fireEvent.click(screen.getByTestId(`save-${provider}-credential`));
    await waitFor(() =>
      expect(
        screen.getByTestId(`${provider}-credential-problem`)
      ).toHaveTextContent('Reload the status')
    );
    expect(
      screen.queryByTestId(`${provider}-credential-key`)
    ).not.toBeInTheDocument();
    expect(screen.getByTestId(`add-${provider}-credential`)).toBeDisabled();
    expect(
      within(screen.getByTestId(`${provider}-credential-section`)).getByRole(
        'button',
        { name: 'Reload status' }
      )
    ).toBeEnabled();
    expect(executeByOperation.bind_credential).not.toHaveBeenCalled();
    expect(executeByOperation.create_credential).toHaveBeenCalledTimes(1);
  });

  test('shows and changes the actually bound lineage even when a canonical key is also active', async () => {
    const bound = {
      ...KEY,
      credential_key: 'custom_key',
      credential_version_id: 'cv_bound',
      display_label: 'Actual bound key',
    };
    arrangeReads({
      credentials: [KEY, bound],
      readiness: {
        [provider]: {
          has_active_credential: true,
          credential_version_id: 'cv_bound',
        },
      },
    });
    arrangeFetch();
    executeByOperation.rotate_credential.mockResolvedValue(
      success({ ...bound, credential_version_id: 'cv_rotated' })
    );
    executeByOperation.bind_credential.mockResolvedValue(
      success({ binding_id: 'cb_new' })
    );
    render(<ProviderCredentialsSection />);
    expect(screen.getByTestId(`${provider}-credential-status`)).toHaveAttribute(
      'data-bound',
      'true'
    );
    expect(screen.getByText('Actual bound key')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId(`replace-${provider}-credential`));
    fireEvent.change(screen.getByTestId(`${provider}-credential-key`), {
      target: { value: 'fixture-only-key' },
    });
    fireEvent.click(screen.getByTestId(`save-${provider}-credential`));
    await waitFor(() =>
      expect(executeByOperation.bind_credential).toHaveBeenCalledTimes(1)
    );
    expect(
      executeByOperation.rotate_credential.mock.calls[0][0].pathParams
    ).toEqual({ credential_key: 'custom_key' });
  });

  test('cancelling clears only this provider form and never submits a credential', () => {
    arrangeReads();
    render(<ProviderCredentialsSection />);
    fireEvent.click(screen.getByTestId(`add-${provider}-credential`));
    fireEvent.change(screen.getByTestId(`${provider}-credential-key`), {
      target: { value: 'fixture-only-key' },
    });
    fireEvent.click(
      within(screen.getByTestId(`${provider}-credential-section`)).getByRole(
        'button',
        { name: 'Cancel' }
      )
    );
    fireEvent.click(screen.getByTestId(`add-${provider}-credential`));
    expect(screen.getByTestId(`${provider}-credential-key`)).toHaveValue('');
    expect(screen.getByTestId(`save-${provider}-credential`)).toBeDisabled();
    expect(executeByOperation.create_credential).not.toHaveBeenCalled();
  });

  test('provider forms keep their secrets and submitted provider identities separate', async () => {
    const other = { openai: 'gemini', gemini: 'seedream', seedream: 'openai' }[
      provider
    ];
    arrangeReads();
    arrangeFetch();
    executeByOperation.create_credential.mockResolvedValue(success(KEY));
    executeByOperation.bind_credential.mockResolvedValue(
      success({ binding_id: 'cb_1' })
    );
    render(<ProviderCredentialsSection />);
    fireEvent.click(screen.getByTestId(`add-${provider}-credential`));
    fireEvent.click(screen.getByTestId(`add-${other}-credential`));
    fireEvent.change(screen.getByTestId(`${provider}-credential-key`), {
      target: { value: 'fixture-first' },
    });
    fireEvent.change(screen.getByTestId(`${other}-credential-key`), {
      target: { value: 'fixture-second' },
    });
    fireEvent.click(screen.getByTestId(`save-${provider}-credential`));
    await waitFor(() =>
      expect(executeByOperation.bind_credential).toHaveBeenCalledTimes(1)
    );
    expect(
      executeByOperation.create_credential.mock.calls[0][0].body
    ).toMatchObject({
      provider,
      credential_key: credentialKey,
      plaintext_b64: utf8ToBase64('fixture-first'),
    });
    expect(screen.getByTestId(`${other}-credential-key`)).toHaveValue(
      'fixture-second'
    );
  });

  test('repeated form submission has one create and one bind intent', async () => {
    arrangeReads();
    const { calls } = arrangeFetch();
    executeByOperation.create_credential.mockResolvedValue(success(KEY));
    executeByOperation.bind_credential.mockResolvedValue(
      success({ binding_id: 'cb_1' })
    );
    render(<ProviderCredentialsSection />);
    fireEvent.click(screen.getByTestId(`add-${provider}-credential`));
    fireEvent.change(screen.getByTestId(`${provider}-credential-key`), {
      target: { value: 'fixture-only' },
    });
    const form = screen.getByTestId(`${provider}-credential-form`);
    fireEvent.submit(form);
    fireEvent.submit(form);
    await waitFor(() =>
      expect(executeByOperation.bind_credential).toHaveBeenCalledTimes(1)
    );
    expect(executeByOperation.create_credential).toHaveBeenCalledTimes(1);
    expect(calls).toEqual([]);
  });

  test('switching workspace discards the unsent secret and form', () => {
    arrangeReads();
    arrangeFetch();
    const ui = render(<ProviderCredentialsSection />);
    fireEvent.click(screen.getByTestId(`add-${provider}-credential`));
    fireEvent.change(screen.getByTestId(`${provider}-credential-key`), {
      target: { value: 'fixture-old-workspace' },
    });
    mockWorkspace = { workspaceId: 'ws_other', slug: 'other-ws' };
    ui.rerender(<ProviderCredentialsSection />);
    expect(
      screen.queryByTestId(`${provider}-credential-form`)
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId(`add-${provider}-credential`));
    expect(screen.getByTestId(`${provider}-credential-key`)).toHaveValue('');
    expect(executeByOperation.create_credential).not.toHaveBeenCalled();
  });
});

/**
 * The three generation entries keep their scope, copy and behaviour now that
 * the section also holds a market-data entry (#267). The strings are pinned
 * here rather than read from the locale file, so a copy change shows up.
 */
describe.each([
  {
    provider: 'openai',
    credentialKey: 'openai_api_key',
    add: 'Add OpenAI key',
  },
  {
    provider: 'gemini',
    credentialKey: 'gemini_api_key',
    add: 'Add Gemini key',
  },
  {
    provider: 'seedream',
    credentialKey: 'ark_api_key',
    add: 'Add Seedream key',
  },
])(
  'ProviderCredentialsSection — $provider is unchanged by the market-data entry',
  ({ provider, credentialKey, add }) => {
    const KEY = {
      ...OPENAI_KEY,
      provider,
      credential_key: credentialKey,
      credential_version_id: `cv_${provider}_1`,
    };

    beforeEach(() => {
      mockWorkspace = { workspaceId: 'ws_test', slug: 'test-ws' };
      jest.clearAllMocks();
      for (const mock of Object.values(executeByOperation)) mock.mockReset();
      mockRefreshList.mockResolvedValue(undefined);
      mockRefreshCapabilities.mockResolvedValue(undefined);
    });

    const section = () => screen.getByTestId(`${provider}-credential-section`);

    test('keeps the generation hint, add and save copy, and no data-vendor notice', () => {
      arrangeReads();
      render(<ProviderCredentialsSection />);
      expect(section()).toHaveTextContent(
        'Saving a key does not call the provider or verify that the key works. A configured model cost ceiling and budget are still required before generation.'
      );
      expect(
        screen.getByTestId(`add-${provider}-credential`)
      ).toHaveTextContent(add);
      expect(
        screen.queryByTestId(`${provider}-credential-terms`)
      ).not.toBeInTheDocument();
      fireEvent.click(screen.getByTestId(`add-${provider}-credential`));
      expect(
        screen.getByTestId(`save-${provider}-credential`)
      ).toHaveTextContent('Save and use for generation');
    });

    test('reads readiness from generation capabilities, not from the key bindings', () => {
      // A `market_data` binding on the key list does not make a generation
      // key ready; only `get_generation_capabilities` does.
      arrangeReads({
        credentials: [
          {
            ...KEY,
            bindings: [{ binding_scope: 'market_data', provider }],
          },
        ],
      });
      const ui = render(<ProviderCredentialsSection />);
      expect(
        screen.getByTestId(`${provider}-credential-status`)
      ).toHaveAttribute('data-bound', 'false');
      expect(section()).toHaveTextContent('Saved, not in use');
      expect(
        screen.getByTestId(`bind-${provider}-credential`)
      ).toHaveTextContent('Use for generation');

      arrangeReads({
        credentials: [KEY],
        readiness: {
          [provider]: {
            has_active_credential: true,
            credential_version_id: KEY.credential_version_id,
          },
        },
      });
      ui.rerender(<ProviderCredentialsSection />);
      expect(
        screen.getByTestId(`${provider}-credential-status`)
      ).toHaveAttribute('data-bound', 'true');
      expect(section()).toHaveTextContent('Key enabled for generation');
      expect(section()).not.toHaveTextContent('Last checked');
    });

    test('enabling binds the generation scope', async () => {
      arrangeReads({ credentials: [KEY] });
      arrangeFetch();
      executeByOperation.bind_credential.mockResolvedValue(
        success({ binding_id: 'cb_1' })
      );
      render(<ProviderCredentialsSection />);
      fireEvent.click(screen.getByTestId(`bind-${provider}-credential`));
      await waitFor(() =>
        expect(executeByOperation.bind_credential).toHaveBeenCalledTimes(1)
      );
      expect(executeByOperation.bind_credential.mock.calls[0][0]).toMatchObject(
        {
          pathParams: {
            credential_version_id: KEY.credential_version_id,
            scope: 'generation',
          },
          body: { provider },
        }
      );
    });
  }
);

describe('ProviderCredentialsSection — ScrapeCreators (market data)', () => {
  const SCAN_KEY: CredentialMetadata = {
    ...OPENAI_KEY,
    provider: 'scrapecreators',
    credential_key: 'scrapecreators_api_key',
    credential_version_id: 'cv_scan_1',
    display_label: 'Scan key',
    bindings: [],
    last_check: {
      outcome: 'valid',
      credit_count: 24987,
      checked_at: '2026-10-28T00:00:00Z',
    },
  };
  const BOUND = {
    ...SCAN_KEY,
    bindings: [{ binding_scope: 'market_data', provider: 'scrapecreators' }],
  };

  beforeEach(() => {
    mockWorkspace = { workspaceId: 'ws_test', slug: 'test-ws' };
    jest.clearAllMocks();
    for (const mock of Object.values(executeByOperation)) mock.mockReset();
    mockRefreshList.mockResolvedValue(undefined);
    mockRefreshCapabilities.mockResolvedValue(undefined);
  });

  const section = () => screen.getByTestId('scrapecreators-credential-section');

  test('stands beside the image providers with its own hint and the vendor-terms notice', () => {
    arrangeReads();
    render(<ProviderCredentialsSection />);
    for (const provider of ['openai', 'gemini', 'seedream', 'scrapecreators'])
      expect(
        screen.getByTestId(`add-${provider}-credential`)
      ).toBeInTheDocument();
    expect(section()).toHaveTextContent('ScrapeCreators (competitor ad data)');
    expect(section()).toHaveTextContent(
      'Used for competitor ad scans, not for generation.'
    );
    expect(section()).not.toHaveTextContent('does not call the provider');
    expect(
      screen.getByTestId('scrapecreators-credential-terms')
    ).toHaveTextContent(
      'Your use of ScrapeCreators is governed by your agreement with ScrapeCreators'
    );
    fireEvent.click(screen.getByTestId('add-scrapecreators-credential'));
    expect(
      screen.getByTestId('save-scrapecreators-credential')
    ).toHaveTextContent('Check and save key');
    expect(screen.getByTestId('scrapecreators-credential-key')).toHaveAttribute(
      'type',
      'password'
    );
  });

  test('saving creates the key, then binds it to market_data', async () => {
    arrangeReads();
    const { calls } = arrangeFetch();
    executeByOperation.create_credential.mockResolvedValue(success(SCAN_KEY));
    executeByOperation.bind_credential.mockResolvedValue(
      success({ binding_id: 'cb_scan' })
    );
    render(<ProviderCredentialsSection />);
    fireEvent.click(screen.getByTestId('add-scrapecreators-credential'));
    fireEvent.change(screen.getByTestId('scrapecreators-credential-key'), {
      target: { value: ' fixture-scan-key ' },
    });
    fireEvent.click(screen.getByTestId('save-scrapecreators-credential'));
    await waitFor(() =>
      expect(executeByOperation.bind_credential).toHaveBeenCalledTimes(1)
    );
    expect(executeByOperation.create_credential.mock.calls[0][0].body).toEqual({
      credential_key: 'scrapecreators_api_key',
      provider: 'scrapecreators',
      plaintext_b64: utf8ToBase64('fixture-scan-key'),
      display_label: null,
    });
    expect(executeByOperation.bind_credential.mock.calls[0][0]).toMatchObject({
      pathParams: { credential_version_id: 'cv_scan_1', scope: 'market_data' },
      body: { provider: 'scrapecreators' },
    });
    expect(calls).toEqual([]);
  });

  test('an invalid or empty key reads as invalid or no balance and never as connected', async () => {
    arrangeReads();
    arrangeFetch();
    executeByOperation.create_credential.mockResolvedValue(
      problem('market_data_credential_invalid', 422)
    );
    render(<ProviderCredentialsSection />);
    fireEvent.click(screen.getByTestId('add-scrapecreators-credential'));
    fireEvent.change(screen.getByTestId('scrapecreators-credential-key'), {
      target: { value: 'fixture-empty-key' },
    });
    fireEvent.click(screen.getByTestId('save-scrapecreators-credential'));
    await waitFor(() =>
      expect(
        screen.getByTestId('scrapecreators-credential-problem')
      ).toHaveTextContent(
        'The key is invalid or has no credits left, so it was not saved.'
      )
    );
    expect(executeByOperation.bind_credential).not.toHaveBeenCalled();
    expect(section()).not.toHaveTextContent('Connected');
    expect(
      screen.queryByTestId('scrapecreators-credential-status')
    ).not.toBeInTheDocument();
    // The form stays open to paste another key.
    expect(
      screen.getByTestId('scrapecreators-credential-form')
    ).toBeInTheDocument();
  });

  test('a check that could not run says so', async () => {
    arrangeReads();
    arrangeFetch();
    executeByOperation.create_credential.mockResolvedValue(
      problem('market_data_credential_check_unavailable', 503)
    );
    render(<ProviderCredentialsSection />);
    fireEvent.click(screen.getByTestId('add-scrapecreators-credential'));
    fireEvent.change(screen.getByTestId('scrapecreators-credential-key'), {
      target: { value: 'fixture-scan-key' },
    });
    fireEvent.click(screen.getByTestId('save-scrapecreators-credential'));
    await waitFor(() =>
      expect(
        screen.getByTestId('scrapecreators-credential-problem')
      ).toHaveTextContent('could not be checked with the provider right now')
    );
    expect(executeByOperation.bind_credential).not.toHaveBeenCalled();
  });

  test('a bound key with credits at its last check is connected', () => {
    arrangeReads({ credentials: [BOUND] });
    render(<ProviderCredentialsSection />);
    const status = screen.getByTestId('scrapecreators-credential-status');
    expect(status).toHaveAttribute('data-bound', 'true');
    expect(status).toHaveAttribute('data-connected', 'true');
    expect(status).toHaveTextContent(
      'Connected · 24,987 credits at last check'
    );
    expect(section()).toHaveTextContent('Last checked');
    expect(
      screen.queryByTestId('scrapecreators-credential-terms')
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId('bind-scrapecreators-credential')
    ).not.toBeInTheDocument();
  });

  test('a bound key without a successful check is not shown as connected', () => {
    for (const lastCheck of [
      null,
      {
        outcome: 'invalid_or_no_balance',
        credit_count: 0,
        checked_at: '2026-10-28T00:00:00Z',
      },
    ]) {
      arrangeReads({ credentials: [{ ...BOUND, last_check: lastCheck }] });
      const ui = render(<ProviderCredentialsSection />);
      const status = screen.getByTestId('scrapecreators-credential-status');
      expect(status).toHaveAttribute('data-connected', 'false');
      expect(status).toHaveTextContent('Saved · not verified');
      expect(section()).not.toHaveTextContent('Connected');
      expect(section()).toHaveTextContent('Replace it to check it again.');
      ui.unmount();
    }
  });

  test('a saved key that is not bound is enabled for competitor scans', async () => {
    arrangeReads({ credentials: [SCAN_KEY] });
    arrangeFetch();
    executeByOperation.bind_credential.mockResolvedValue(
      success({ binding_id: 'cb_scan' })
    );
    render(<ProviderCredentialsSection />);
    expect(
      screen.getByTestId('scrapecreators-credential-status')
    ).toHaveAttribute('data-connected', 'false');
    expect(section()).toHaveTextContent('Saved, not in use');
    const bind = screen.getByTestId('bind-scrapecreators-credential');
    expect(bind).toHaveTextContent('Use for competitor scans');
    fireEvent.click(bind);
    await waitFor(() =>
      expect(executeByOperation.bind_credential).toHaveBeenCalledTimes(1)
    );
    expect(
      executeByOperation.bind_credential.mock.calls[0][0].pathParams
    ).toEqual({ credential_version_id: 'cv_scan_1', scope: 'market_data' });
  });

  test('a saved key that could not be enabled offers its own retry copy', async () => {
    arrangeReads();
    arrangeFetch();
    // A success body without a version id: saved, but nothing to bind.
    executeByOperation.create_credential.mockResolvedValue(success(null));
    render(<ProviderCredentialsSection />);
    fireEvent.click(screen.getByTestId('add-scrapecreators-credential'));
    fireEvent.change(screen.getByTestId('scrapecreators-credential-key'), {
      target: { value: 'fixture-scan-key' },
    });
    fireEvent.click(screen.getByTestId('save-scrapecreators-credential'));
    await waitFor(() =>
      expect(
        screen.getByTestId('scrapecreators-credential-problem')
      ).toHaveTextContent('could not be enabled for competitor scans')
    );
    expect(
      screen.getByTestId('scrapecreators-credential-problem')
    ).not.toHaveTextContent('for generation');
  });

  test('replacing the key rotates it and binds the new version to market_data', async () => {
    arrangeReads({ credentials: [BOUND] });
    arrangeFetch();
    executeByOperation.rotate_credential.mockResolvedValue(
      success({
        ...SCAN_KEY,
        credential_version_id: 'cv_scan_2',
        version_no: 2,
      })
    );
    executeByOperation.bind_credential.mockResolvedValue(
      success({ binding_id: 'cb_scan' })
    );
    render(<ProviderCredentialsSection />);
    fireEvent.click(screen.getByTestId('replace-scrapecreators-credential'));
    fireEvent.change(screen.getByTestId('scrapecreators-credential-key'), {
      target: { value: 'fixture-topped-up-key' },
    });
    fireEvent.click(screen.getByTestId('save-scrapecreators-credential'));
    await waitFor(() =>
      expect(executeByOperation.bind_credential).toHaveBeenCalledTimes(1)
    );
    expect(
      executeByOperation.rotate_credential.mock.calls[0][0].pathParams
    ).toEqual({ credential_key: 'scrapecreators_api_key' });
    expect(executeByOperation.bind_credential.mock.calls[0][0]).toMatchObject({
      pathParams: { credential_version_id: 'cv_scan_2', scope: 'market_data' },
    });
  });

  test('its status does not wait on generation capabilities', () => {
    mockUseDomainQuery.mockImplementation(((operationId: string) =>
      operationId === 'list_credentials'
        ? queryState([BOUND], mockRefreshList)
        : queryState(undefined, mockRefreshCapabilities, {
            code: 'service_unavailable',
            status: 503,
          })) as any);
    render(<ProviderCredentialsSection />);
    expect(
      screen.getByTestId('scrapecreators-credential-status')
    ).toHaveAttribute('data-connected', 'true');
  });
});
