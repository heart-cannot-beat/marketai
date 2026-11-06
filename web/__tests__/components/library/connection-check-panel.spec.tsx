/**
 * @jest-environment jsdom
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { I18nextProvider, getI18n } from 'react-i18next';
import { SWRConfig } from 'swr';
import type { ReactElement } from 'react';
import enLibrary from '../../../locales/en/library.json';
import common from '../../../locales/zh-CN/common.json';
import library from '../../../locales/zh-CN/library.json';

const mockGateway = {
  checkLibraryDataSourceConnection: jest.fn(),
  pollOperation: jest.fn(),
  getLibraryDataSourceConnectionCheck: jest.fn(),
};

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: { slug: 'acme', workspaceId: 'w1', role: 'OWNER' },
  }),
}));
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));

import { ConnectionCheckPanel } from '@/components/library/data-sources/ConnectionCheckPanel';
import { SettingsTab } from '@/components/library/tabs/SettingsTab';

/**
 * The settings panel starts a check, follows its Operation with
 * `useOperation` and reads the check its result names (#383). Only the
 * gateway is replaced: the hook, `useOperation`, `useDomainQuery` and the
 * copy are the real ones, and the responses have the shapes the API sends.
 */
const ENTRY = 'ds_1';
const OPERATION = 'op_1';
const CHECK_ID = 'audit_1';

const accepted = {
  kind: 'accepted',
  data: {
    operation_id: OPERATION,
    state: 'queued',
    command_type: 'connection_check',
  },
};

const operationIn = (
  state: string,
  result: { resource_type: string; resource_id: string } | null = null
) => ({
  kind: 'ok',
  data: {
    operation_id: OPERATION,
    command_type: 'connection_check',
    state,
    result,
    created_at: '2026-11-05T02:00:00+00:00',
    updated_at: '2026-11-05T02:00:01+00:00',
  },
  // `Retry-After: 0` keeps the real poll loop fast.
  meta: { etag: `"${state}"`, retryAt: 0, location: null },
});

const finished = operationIn('succeeded', {
  resource_type: 'data_source_connection_check',
  resource_id: CHECK_ID,
});

const checkOf = (overrides: Record<string, unknown> = {}) => ({
  kind: 'ok',
  data: {
    check_id: CHECK_ID,
    data_source_entry_id: ENTRY,
    health: 'healthy',
    reason_code: 'manual_source_no_connection',
    adapter_kind: 'manual',
    adapter_version: 'manual-v1',
    execution_mode: 'real',
    data_source_version_id: 'ver_1',
    credential_state: null,
    checked_at: '2026-11-05T02:00:01+00:00',
    ...overrides,
  },
  meta: { etag: null, retryAt: null, location: null },
});

function renderIn(language: 'en' | 'zh-CN', ui: ReactElement) {
  const i18n = getI18n().cloneInstance({
    lng: language,
    fallbackLng: false,
    forkResourceStore: true,
  });
  // The Jest setup loads `common` but not `library`.
  i18n.addResourceBundle('en', 'library', enLibrary);
  if (language === 'zh-CN') {
    i18n.addResourceBundle('zh-CN', 'common', common);
    i18n.addResourceBundle('zh-CN', 'library', library);
  }
  void i18n.changeLanguage(language);
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <I18nextProvider i18n={i18n}>{ui}</I18nextProvider>
    </SWRConfig>
  );
}

const panel = <ConnectionCheckPanel dataSourceEntryId={ENTRY} />;

/** No reason code, health, key or raw mode reaches the page. */
const expectNoRawCodes = (text: string) => {
  expect(text).not.toMatch(
    /data-source-ops|_source|not_published|not_enabled|not_active|config_invalid|\breal\b|\bmock\b|\bmanual\b/
  );
};

beforeEach(() => {
  jest.clearAllMocks();
  sessionStorage.clear();
  mockGateway.checkLibraryDataSourceConnection.mockResolvedValue(accepted);
  mockGateway.pollOperation
    .mockResolvedValueOnce(operationIn('queued'))
    .mockResolvedValueOnce(operationIn('running'))
    .mockResolvedValue(finished);
  mockGateway.getLibraryDataSourceConnectionCheck.mockResolvedValue(checkOf());
});

describe('ConnectionCheckPanel', () => {
  it('follows the check to its conclusion and words it', async () => {
    const { container } = renderIn('en', panel);
    expect(
      screen.getByText(/does not contact the provider/)
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Check connection' }));
    expect(await screen.findByText('Checking…')).toBeInTheDocument();

    expect(await screen.findByText('Check passed')).toBeInTheDocument();
    expect(
      screen.getByText(
        'Manual data sources need no connection: their facts come from imports.'
      )
    ).toBeInTheDocument();
    expect(screen.getByText('Manual import')).toBeInTheDocument();
    expect(screen.getByTestId('execution-mode-badge')).toHaveAttribute(
      'data-mode',
      'real'
    );
    expect(screen.getByRole('button', { name: 'Check again' })).toBeEnabled();

    // The client asks for no mode: the version's is the one checked.
    expect(mockGateway.checkLibraryDataSourceConnection).toHaveBeenCalledWith(
      expect.objectContaining({ dataSourceEntryId: ENTRY, body: {} })
    );
    expect(mockGateway.pollOperation).toHaveBeenCalledTimes(3);
    expect(
      mockGateway.getLibraryDataSourceConnectionCheck
    ).toHaveBeenCalledWith({
      workspaceSlug: 'acme',
      dataSourceEntryId: ENTRY,
      checkId: CHECK_ID,
    });
    expectNoRawCodes(container.textContent ?? '');
  });

  it('marks a Controlled Mock result as Mock, in Chinese', async () => {
    mockGateway.getLibraryDataSourceConnectionCheck.mockResolvedValue(
      checkOf({
        reason_code: 'controlled_mock_source',
        adapter_kind: 'mock',
        adapter_version: 'mock-v1',
        execution_mode: 'mock',
      })
    );
    const { container } = renderIn('zh-CN', panel);
    fireEvent.click(screen.getByRole('button', { name: '检查连接' }));

    expect(await screen.findByText('检查通过')).toBeInTheDocument();
    expect(
      screen.getByText('这是受控 Mock 数据源，不连接真实服务商。')
    ).toBeInTheDocument();
    const badge = screen.getByTestId('execution-mode-badge');
    expect(badge).toHaveAttribute('data-mode', 'controlled-mock');
    expect(badge).toHaveTextContent('示例（受控 Mock）');
    expect(screen.getByText('检查时间')).toBeInTheDocument();
    const text = (container.textContent ?? '').replace(/受控 Mock/g, '');
    expectNoRawCodes(text);
  });

  it.each([
    [
      'failed',
      'adapter_not_enabled',
      { adapter_kind: 'shopify', adapter_version: null },
      'Check did not pass',
      'There is no connector for this kind of data source yet.',
    ],
    [
      'failed',
      'data_source_not_published',
      {
        adapter_kind: null,
        adapter_version: null,
        execution_mode: null,
        data_source_version_id: null,
      },
      'Check did not pass',
      'Publish this data source first, then check it.',
    ],
    [
      'revoked',
      'credential_not_active',
      { credential_state: 'revoked' },
      'Credential no longer valid',
      'The bound credential version was rotated or revoked. Bind a valid credential.',
    ],
  ])(
    'words a %s check (%s)',
    async (health, reason, overrides, title, sentence) => {
      mockGateway.getLibraryDataSourceConnectionCheck.mockResolvedValue(
        checkOf({ health, reason_code: reason, ...overrides })
      );
      const { container } = renderIn('en', panel);
      fireEvent.click(screen.getByRole('button', { name: 'Check connection' }));

      expect(await screen.findByText(title)).toBeInTheDocument();
      expect(screen.getByText(sentence)).toBeInTheDocument();
      expectNoRawCodes(container.textContent ?? '');
    }
  );

  it('says the check did not finish when its Operation failed', async () => {
    mockGateway.pollOperation
      .mockReset()
      .mockResolvedValue(operationIn('failed'));
    renderIn('en', panel);
    fireEvent.click(screen.getByRole('button', { name: 'Check connection' }));

    expect(
      await screen.findByText('The connection check did not finish.')
    ).toBeInTheDocument();
    expect(
      mockGateway.getLibraryDataSourceConnectionCheck
    ).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeEnabled();
  });

  it('words a refused check by its code', async () => {
    mockGateway.checkLibraryDataSourceConnection.mockResolvedValue({
      kind: 'problem',
      problem: {
        type: 'about:blank',
        title: 'Conflict',
        status: 409,
        code: 'idempotency_key_reused',
        trace_id: 'trace_1',
        retryable: false,
      },
    });
    renderIn('en', panel);
    fireEvent.click(screen.getByRole('button', { name: 'Check connection' }));

    expect(
      await screen.findByText(
        'This request was already used for a different action. Start again.'
      )
    ).toBeInTheDocument();
    expect(mockGateway.pollOperation).not.toHaveBeenCalled();
  });

  it('says an unconfirmed start in a sentence, not an English error', async () => {
    mockGateway.checkLibraryDataSourceConnection.mockResolvedValue({
      kind: 'reconciliation_required',
      actionInstanceId: 'connection-check:1',
      staleSince: Date.now(),
    });
    renderIn('zh-CN', panel);
    fireEvent.click(screen.getByRole('button', { name: '检查连接' }));

    expect(
      await screen.findByText('无法确认检查已经开始，请再检查一次。')
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '重试' })).toBeEnabled();
  });

  it('stops at an uncertain Operation', async () => {
    mockGateway.pollOperation
      .mockReset()
      .mockResolvedValue(operationIn('uncertain'));
    renderIn('en', panel);
    fireEvent.click(screen.getByRole('button', { name: 'Check connection' }));

    expect(
      await screen.findByText(
        'The check outcome is uncertain. Check again later.'
      )
    ).toBeInTheDocument();
    const polls = mockGateway.pollOperation.mock.calls.length;
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(mockGateway.pollOperation).toHaveBeenCalledTimes(polls);
  });

  it('a read that fails is worded too', async () => {
    mockGateway.getLibraryDataSourceConnectionCheck.mockResolvedValue({
      kind: 'problem',
      problem: {
        type: 'about:blank',
        title: 'Not Found',
        status: 404,
        code: 'connection_check_not_found',
        trace_id: 'trace_1',
        retryable: false,
      },
    });
    renderIn('en', panel);
    fireEvent.click(screen.getByRole('button', { name: 'Check connection' }));

    expect(
      await screen.findByText('That connection check was not found.')
    ).toBeInTheDocument();
  });

  it('checks again from a result', async () => {
    renderIn('en', panel);
    fireEvent.click(screen.getByRole('button', { name: 'Check connection' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Check again' }));

    await waitFor(() =>
      expect(
        mockGateway.checkLibraryDataSourceConnection
      ).toHaveBeenCalledTimes(2)
    );
    const [first, second] =
      mockGateway.checkLibraryDataSourceConnection.mock.calls;
    expect(first[0].actionInstanceId).not.toEqual(second[0].actionInstanceId);
  });
});

describe('SettingsTab', () => {
  it('checks the entry it shows', async () => {
    renderIn('en', <SettingsTab entryId={ENTRY} />);
    fireEvent.click(screen.getByRole('button', { name: 'Check connection' }));

    expect(await screen.findByText('Check passed')).toBeInTheDocument();
    expect(mockGateway.checkLibraryDataSourceConnection).toHaveBeenCalledWith(
      expect.objectContaining({
        workspaceSlug: 'acme',
        dataSourceEntryId: ENTRY,
      })
    );
  });
});
