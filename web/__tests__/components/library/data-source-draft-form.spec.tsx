/**
 * @jest-environment jsdom
 */
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { I18nextProvider, getI18n } from 'react-i18next';
import { createElement, type ReactNode } from 'react';
import enLibrary from '../../../locales/en/library.json';
import zhCommon from '../../../locales/zh-CN/common.json';
import zhLibrary from '../../../locales/zh-CN/library.json';
import { CJK } from '../../locales/locale-files';

const mockSaveRun = jest.fn<Promise<unknown>, [unknown]>(async () => ({
  kind: 'success',
  data: { revision: 2 },
  meta: { etag: 'etag-2' },
}));

jest.mock('@/hooks/library/useLibraryCommand', () => ({
  useLibraryCommand: (kind: string) => ({
    run: kind === 'save_draft' ? mockSaveRun : jest.fn(),
    reset: jest.fn(),
    retry: jest.fn(),
    dismiss: jest.fn(),
    isPending: false,
    problem: undefined,
    reconciliation: undefined,
  }),
}));
jest.mock('@/hooks/library/useLibraryCache', () => ({
  useLibraryCache: () => ({ invalidate: jest.fn(async () => {}) }),
}));
jest.mock('@/components/workspace/NavigationBlocker', () => ({
  useNavigationBlocker: () => ({
    register: () => () => {},
    navigate: jest.fn(),
    isBlocked: () => false,
  }),
  NavigationBlockerProvider: ({ children }: { children: ReactNode }) =>
    createElement('div', null, children),
}));

import { DraftEditor } from '@/components/library/draft/DraftEditor';
import { useDraftSession } from '@/hooks/library/useDraftSession';

/**
 * The data source Draft tab (#396): the form offers the source type only and
 * writes the execution mode the server pairs with it, so a draft made in the
 * interface always publishes. Real `DraftEditor` and `useDraftSession`; only
 * the command layer is stubbed.
 */
function DataSourceDraftTab({ content }: { content: Record<string, unknown> }) {
  const session = useDraftSession('e1', 'data_source', {
    id: 'd1',
    entry_id: 'e1',
    revision: 1,
    state: 'editing',
    content_schema_version: 1,
    content,
    etag: 'etag-1',
  });
  return (
    <DraftEditor
      entryId="e1"
      entryType="data_source"
      schemaVersion={1}
      session={session}
    />
  );
}

function renderIn(language: 'en' | 'zh-CN', content: Record<string, unknown>) {
  const i18n = getI18n().cloneInstance({
    lng: language,
    fallbackLng: false,
    forkResourceStore: true,
  });
  i18n.addResourceBundle('en', 'library', enLibrary);
  if (language === 'zh-CN') {
    i18n.addResourceBundle('zh-CN', 'common', zhCommon);
    i18n.addResourceBundle('zh-CN', 'library', zhLibrary);
  }
  void i18n.changeLanguage(language);
  return render(
    <I18nextProvider i18n={i18n}>
      <DataSourceDraftTab content={content} />
    </I18nextProvider>
  );
}

const savedContent = () =>
  (mockSaveRun.mock.calls.at(-1)?.[0] as { body: { content: unknown } }).body
    .content;

beforeEach(() => mockSaveRun.mockClear());

describe('data source Draft tab in en', () => {
  it('offers the source types, with Shopify shown but not selectable', () => {
    renderIn('en', {});

    const select = screen.getByLabelText('Source type');
    expect(
      within(select)
        .getAllByRole('option')
        .map((option) => [
          option.textContent,
          (option as HTMLOptionElement).disabled,
        ])
    ).toEqual([
      ['Choose a source type', true],
      ['Manual import', false],
      ['Controlled Mock (example)', false],
      ['Shopify (no connector yet)', true],
    ]);
    // One choice only: the execution mode follows the source type.
    expect(screen.getAllByRole('combobox')).toHaveLength(1);
    expect(screen.getByText(/Set by the source type/)).toBeInTheDocument();
    expect(
      screen.queryByTestId('execution-mode-badge')
    ).not.toBeInTheDocument();
  });

  it('saves a manual source as real, both fields together', async () => {
    renderIn('en', { display_name: 'Old name', config: {} });

    fireEvent.change(screen.getByLabelText('Source type'), {
      target: { value: 'manual' },
    });
    expect(screen.getByTestId('execution-mode-badge')).toHaveTextContent(
      'Real'
    );
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    });

    // `display_name` is the entry's name, not a form field: an existing key
    // rides along untouched, and the form never adds one.
    expect(savedContent()).toEqual({
      display_name: 'Old name',
      adapter_kind: 'manual',
      execution_mode: 'real',
      config: {},
    });
  });

  it('saves a new controlled Mock source as mock, and adds no name key', async () => {
    renderIn('en', {});

    fireEvent.change(screen.getByLabelText('Source type'), {
      target: { value: 'mock' },
    });
    expect(screen.getByTestId('execution-mode-badge')).toHaveTextContent(
      'Example (controlled Mock)'
    );
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    });

    expect(savedContent()).toEqual({
      adapter_kind: 'mock',
      execution_mode: 'mock',
      config: {},
    });
  });

  it('asks to choose again for a stored source type it does not offer, without editing the draft', () => {
    renderIn('en', { adapter_kind: 'Shopify', execution_mode: 'scheduled' });

    expect(
      (screen.getByLabelText('Source type') as HTMLSelectElement).value
    ).toBe('');
    expect(
      screen.getByText(/saved source type is not one of the options/)
    ).toBeInTheDocument();
    expect(
      screen.queryByTestId('execution-mode-badge')
    ).not.toBeInTheDocument();
    // Opening the draft does not rewrite it: nothing to save, nothing unsaved.
    expect(screen.queryByText('Unsaved changes')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
  });

  it('asks to choose again when the stored mode does not match the source type', () => {
    renderIn('en', { adapter_kind: 'manual', execution_mode: 'mock' });

    expect(
      (screen.getByLabelText('Source type') as HTMLSelectElement).value
    ).toBe('manual');
    expect(
      screen.getByText(/execution mode does not match the source type/)
    ).toBeInTheDocument();
    expect(
      screen.queryByTestId('execution-mode-badge')
    ).not.toBeInTheDocument();
    expect(screen.queryByText('Unsaved changes')).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Source type'), {
      target: { value: 'manual' },
    });
    expect(
      screen.queryByText(/execution mode does not match/)
    ).not.toBeInTheDocument();
    expect(screen.getByTestId('execution-mode-badge')).toHaveTextContent(
      'Real'
    );
  });

  it('shows no translation keys', () => {
    const { container } = renderIn('en', {
      adapter_kind: 'manual',
      execution_mode: 'mock',
    });
    expect(container.textContent).not.toMatch(/fields\.data-source|adapter-/);
    expect(container.textContent).not.toMatch(CJK);
  });
});

describe('data source Draft tab in zh-CN', () => {
  it('reads in Chinese and marks a controlled Mock source', () => {
    const { container } = renderIn('zh-CN', {});

    const select = screen.getByLabelText('来源类型');
    expect(
      within(select)
        .getAllByRole('option')
        .map((option) => option.textContent)
    ).toEqual([
      '请选择来源类型',
      '人工导入',
      '受控 Mock（示例）',
      'Shopify（还没有连接器）',
    ]);
    expect(screen.getByText('执行方式')).toBeInTheDocument();

    fireEvent.change(select, { target: { value: 'mock' } });
    expect(screen.getByTestId('execution-mode-badge')).toHaveTextContent(
      '示例（受控 Mock）'
    );
    expect(container.textContent).not.toMatch(
      /fields\.data-source|adapter-|Source type|Manual import|Real\b/
    );
  });

  it('asks in Chinese to choose again for a source type it does not offer', () => {
    renderIn('zh-CN', { adapter_kind: 'Shopify', execution_mode: 'scheduled' });
    expect(
      screen.getByText(/来源类型不在可选范围内，请重新选择来源类型/)
    ).toBeInTheDocument();
  });

  it('asks in Chinese to choose again for a mode that does not match', () => {
    renderIn('zh-CN', { adapter_kind: 'mock', execution_mode: 'real' });
    expect(
      screen.getByText(/执行方式与来源类型不一致，请重新选择来源类型/)
    ).toBeInTheDocument();
  });
});
