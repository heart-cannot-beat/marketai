/**
 * @jest-environment jsdom
 */
import { createElement } from 'react';
import {
  render,
  screen,
  fireEvent,
  act,
  waitFor,
} from '@testing-library/react';

let mockStartUpload: jest.Mock;
let mockPollOperation: jest.Mock;
let mockImportManualFacts: jest.Mock;
let mockInvalidate: jest.Mock;

jest.mock('next-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, opts?: Record<string, string>) => {
      if (opts) {
        let result = `library:${key}`;
        for (const [k, v] of Object.entries(opts)) {
          result = result.replace(`{{${k}}}`, String(v));
        }
        return result;
      }
      return `library:${key}`;
    },
  }),
}));

jest.mock('next/router', () => ({
  useRouter: () => ({
    query: { slug: 'acme' },
    asPath: '/w/acme/library',
    push: jest.fn(),
    replace: jest.fn(),
  }),
}));

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: {
      slug: 'acme',
      workspaceId: 'w1',
      role: 'OWNER',
      membershipId: 'm1',
      displayName: 'Acme',
    },
  }),
}));

jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => ({
    pollOperation: mockPollOperation,
    importLibraryManualFacts: mockImportManualFacts,
  }),
}));

jest.mock('@/hooks/domain/useUpload', () => ({
  useUpload: () => ({
    state: 'idle',
    hashProgress: 0,
    startUpload: mockStartUpload,
  }),
}));

jest.mock('@/hooks/library/useLibraryCache', () => ({
  useLibraryCache: () => ({
    invalidate: mockInvalidate,
  }),
}));

import { ConnectionCheckPanel } from '@/components/library/data-sources/ConnectionCheckPanel';
import { CredentialBindingPanel } from '@/components/library/data-sources/CredentialBindingPanel';
import { ManualFactImportPanel } from '@/components/library/data-sources/ManualFactImportPanel';
import { SettingsTab } from '@/components/library/tabs/SettingsTab';

if (typeof Blob.prototype.text !== 'function') {
  Blob.prototype.text = function () {
    return new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result as string);
      reader.onerror = () => reject(reader.error);
      reader.readAsText(this);
    });
  };
}

beforeEach(() => {
  mockStartUpload = jest.fn().mockResolvedValue({
    kind: 'accepted',
    uploadIntentId: 'upload-intent-1',
    operationRecordId: 'upload-operation-1',
  });
  mockPollOperation = jest.fn().mockResolvedValue({
    kind: 'ok',
    data: { state: 'succeeded', result: null },
  });
  mockImportManualFacts = jest.fn().mockResolvedValue({
    kind: 'accepted',
    data: {
      operation_id: 'import-operation-1',
      state: 'queued',
      command_type: 'manual_fact_import',
    },
  });
  mockInvalidate = jest.fn().mockResolvedValue(undefined);
});

// ─── ConnectionCheckPanel ─────────────────────────────────────────────

describe('ConnectionCheckPanel', () => {
  it('says the check is not available and offers no button', () => {
    render(createElement(ConnectionCheckPanel));

    expect(
      screen.getByText('library:data-source-ops.connection-unavailable')
    ).toBeTruthy();
    expect(screen.queryByRole('button')).toBeNull();
    expect(
      screen.queryByText('library:data-source-ops.check-connection')
    ).toBeNull();
    expect(
      screen.queryByText('library:data-source-ops.connection-success')
    ).toBeNull();
  });

  it('is all the data source settings tab shows', () => {
    const { container } = render(createElement(SettingsTab));

    expect(container.textContent).toBe(
      'library:data-source-ops.connection-unavailable'
    );
    expect(screen.queryByRole('button')).toBeNull();
  });
});

// ─── CredentialBindingPanel ───────────────────────────────────────────

describe('CredentialBindingPanel', () => {
  const creds = [
    {
      id: 'c1',
      provider: 'google_ads',
      createdAt: '2025-01-01',
      expiresAt: null,
      status: 'active',
    },
    {
      id: 'c2',
      provider: 'meta',
      createdAt: '2025-01-01',
      expiresAt: '2025-12-31',
      status: 'expiring',
    },
  ];

  it('shows no-credentials message when empty', () => {
    render(
      createElement(CredentialBindingPanel, {
        credentials: [],
        onBind: jest.fn(),
      })
    );
    expect(
      screen.getByText('library:data-source-ops.no-credentials')
    ).toBeTruthy();
  });

  it('renders credential list with providers', () => {
    render(
      createElement(CredentialBindingPanel, {
        credentials: creds,
        onBind: jest.fn(),
      })
    );
    expect(screen.getByText('google_ads')).toBeTruthy();
    expect(screen.getByText('meta')).toBeTruthy();
    expect(
      screen.getAllByText('library:data-source-ops.credential-status')
    ).toHaveLength(2);
  });

  it('binds on click without asking for an authenticator code (ADR 0015)', async () => {
    const onBind = jest.fn().mockResolvedValue(undefined);
    render(
      createElement(CredentialBindingPanel, {
        credentials: [creds[0]],
        onBind,
      })
    );

    await act(async () => {
      fireEvent.click(screen.getByText('library:data-source-ops.bind'));
    });

    expect(onBind).toHaveBeenCalledWith('c1');
    expect(screen.queryByTestId('mfa-challenge-code')).toBeNull();
  });

  it('shows error when onBind throws', async () => {
    const onBind = jest.fn().mockRejectedValue(new Error('Bind failed'));
    render(
      createElement(CredentialBindingPanel, {
        credentials: [creds[0]],
        onBind,
      })
    );

    await act(async () => {
      fireEvent.click(screen.getByText('library:data-source-ops.bind'));
    });

    expect(screen.getByText('Bind failed')).toBeTruthy();
  });
});

// ─── ManualFactImportPanel ────────────────────────────────────────────

describe('ManualFactImportPanel', () => {
  const defaultProps = { dataSourceEntryId: 'ds-1' };

  it('renders idle state with description and select-file button', () => {
    render(createElement(ManualFactImportPanel, defaultProps));
    expect(
      screen.getByText('library:data-source-ops.import-description')
    ).toBeTruthy();
    expect(
      screen.getByText('library:data-source-ops.select-file')
    ).toBeTruthy();
  });

  it('has a hidden file input with csv/json accept', () => {
    render(createElement(ManualFactImportPanel, defaultProps));
    const input = screen.getByLabelText(
      'library:data-source-ops.file-input'
    ) as HTMLInputElement;
    expect(input.type).toBe('file');
    expect(input.accept).toBe('.csv,.json');
    expect(input.className).toContain('hidden');
  });

  it('processes file and shows complete state on success', async () => {
    render(createElement(ManualFactImportPanel, defaultProps));

    const input = screen.getByLabelText(
      'library:data-source-ops.file-input'
    ) as HTMLInputElement;
    const file = new File(['key,value\nk1,v1'], 'facts.csv', {
      type: 'text/csv',
    });

    await act(async () => {
      fireEvent.change(input, { target: { files: [file] } });
    });

    await waitFor(() => {
      expect(
        screen.getByText('library:data-source-ops.import-complete')
      ).toBeTruthy();
    });
    expect(mockInvalidate).toHaveBeenCalledWith({
      resources: ['entryList'],
    });
  });

  it('does not fabricate row errors absent from the operation contract', async () => {
    render(createElement(ManualFactImportPanel, defaultProps));
    const input = screen.getByLabelText(
      'library:data-source-ops.file-input'
    ) as HTMLInputElement;
    const file = new File(['data'], 'facts.csv', { type: 'text/csv' });

    await act(async () => {
      fireEvent.change(input, { target: { files: [file] } });
    });

    await waitFor(() =>
      expect(
        screen.getByText('library:data-source-ops.import-complete')
      ).toBeTruthy()
    );
    expect(
      screen.queryByText('library:data-source-ops.import-row-errors')
    ).toBeNull();
  });

  it('shows error state on gateway failure', async () => {
    mockImportManualFacts.mockResolvedValue({
      kind: 'problem',
      problem: { code: 'request_invalid', detail: 'Invalid format' },
    });

    render(createElement(ManualFactImportPanel, defaultProps));
    const input = screen.getByLabelText(
      'library:data-source-ops.file-input'
    ) as HTMLInputElement;
    const file = new File(['bad'], 'facts.csv', { type: 'text/csv' });

    await act(async () => {
      fireEvent.change(input, { target: { files: [file] } });
    });

    await waitFor(() => {
      expect(screen.getByText('library:error-request_invalid')).toBeTruthy();
    });
    expect(screen.getByText('library:data-source-ops.retry')).toBeTruthy();
  });

  it('resets on retry click after error', async () => {
    mockImportManualFacts.mockRejectedValue(new Error('Network error'));

    render(createElement(ManualFactImportPanel, defaultProps));
    const input = screen.getByLabelText(
      'library:data-source-ops.file-input'
    ) as HTMLInputElement;
    const file = new File(['data'], 'facts.csv', { type: 'text/csv' });

    await act(async () => {
      fireEvent.change(input, { target: { files: [file] } });
    });

    await waitFor(() => {
      expect(screen.getByText('library:data-source-ops.retry')).toBeTruthy();
    });

    fireEvent.click(screen.getByText('library:data-source-ops.retry'));
    expect(
      screen.getByText('library:data-source-ops.import-description')
    ).toBeTruthy();
  });
});
