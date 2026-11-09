/**
 * @jest-environment jsdom
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { I18nextProvider, getI18n } from 'react-i18next';
import { VersionsTab } from '@/components/library/tabs/VersionsTab';
import enLibrary from '../../../locales/en/library.json';
import zhLibrary from '../../../locales/zh-CN/library.json';
import enCommon from '../../../locales/en/common.json';
import zhCommon from '../../../locales/zh-CN/common.json';
import fixtures from '../../fixtures/library-versions.json';

/**
 * The version toolbar on the Versions tab (#25), driven by what FastAPI
 * really answers (`fixtures/library-versions.json`, written by
 * `tests/test_library_version_web_fixtures.py`), in the real en and zh-CN
 * copy: an Owner withdraws after reading the impact analysis, a Member sees
 * Withdraw held back with its reason, Fork and Clone Product open the entry
 * they create, a Clone Product refused for asking to copy Offers tells the
 * reader to create them separately (ADR 0016), and another Workspace's
 * version id reads as a sentence.
 */
let mockRun: jest.Mock;
let mockPreview: jest.Mock;
let mockQuery: { data?: unknown; problem?: unknown };
let mockCloneProblem: unknown;

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: {
      slug: 'acme',
      workspaceId: 'w1',
      role: 'OWNER',
      membershipId: 'm1',
      displayName: 'Acme Charge',
    },
  }),
}));

jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: (operationId: string) => {
    if (operationId !== 'get_library_version')
      throw new Error(`unexpected query ${operationId}`);
    return { ...mockQuery, isLoading: false, refresh: jest.fn() };
  },
}));

jest.mock('@/hooks/library/useLibraryVersions', () => ({
  useLibraryVersions: () => ({
    items: [
      {
        id: 'libver_01',
        entry_id: 'lib_01',
        entry_type: 'product',
        version_no: 1,
        state: 'effective',
        eligibility: 'eligible',
        content_schema_version: 1,
        lifecycle_revision: 1,
      },
    ],
    hasMore: false,
    isLoading: false,
    isLoadingMore: false,
    problem: undefined,
    loadMore: jest.fn(),
    refresh: jest.fn(),
  }),
}));

jest.mock('@/hooks/library/useLibraryCommand', () => ({
  useLibraryCommand: (kind: string) => ({
    run: (params: unknown) => mockRun(kind, params),
    retry: jest.fn(),
    reset: jest.fn(),
    dismiss: jest.fn(),
    isPending: false,
    problem: kind === 'clone' ? mockCloneProblem : undefined,
    reconciliation: undefined,
  }),
}));

jest.mock('@/hooks/library/useLibraryCache', () => ({
  useLibraryCache: () => ({
    invalidate: jest.fn().mockResolvedValue(undefined),
  }),
}));

jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => ({
    previewLibraryWithdrawImpact: (...args: unknown[]) => mockPreview(...args),
  }),
}));

const COPY = {
  en: {
    library: enLibrary,
    common: enCommon,
    withdraw: 'Withdraw',
    fork: 'Fork',
    clone: 'Clone Product',
    usable: 'Usable',
    blocked: 'Blocked',
    withdrawn: 'Withdrawn',
    ownerOnly: 'Withdraw: Only the Workspace Owner can do this',
    productsOnly: 'Clone Product: Only Product entries can be cloned',
    allWithdrawn:
      'Withdraw, Fork, Clone Product: This version has been withdrawn',
    impactTitle: 'Impact',
    clearsCurrent:
      "This is the entry's effective version. After withdrawal the entry has no effective version until a new one is published.",
    dependents:
      'Published versions that depend on this one: 1. Newly blocked from new use: 1.',
    offerBlocked: 'Offer v1: will be blocked from new use',
    reasonLabel: 'Reason for withdrawal',
    confirmWithdraw: 'Withdraw',
    forkName: 'Acme Charge 65W (fork)',
    confirmFork: 'Fork',
    confirmClone: 'Clone',
    neverCopied:
      'Offers, prices, market mappings and landing pages are never copied. Create them for the new product separately.',
    cloneRefused:
      'Offers, prices, market mappings and landing pages are never copied. Create them for the new product separately.',
    notFound: "This version doesn't exist, or you don't have access to it.",
  },
  'zh-CN': {
    library: zhLibrary,
    common: zhCommon,
    withdraw: '撤回',
    fork: '分叉',
    clone: '克隆产品',
    usable: '可用',
    blocked: '已阻止',
    withdrawn: '已撤回',
    ownerOnly: '撤回：只有工作区 Owner 可以执行这个操作',
    productsOnly: '克隆产品：只有产品条目可以克隆',
    allWithdrawn: '撤回、分叉、克隆产品：这个版本已撤回',
    impactTitle: '影响分析',
    clearsCurrent:
      '这是条目当前生效的版本。撤回后，在发布新版本之前，这个条目没有生效版本。',
    dependents: '依赖这个版本的已发布版本：1 个，其中 1 个将被阻止新的使用。',
    offerBlocked: '销售方案 v1：将被阻止新的使用',
    reasonLabel: '撤回原因',
    confirmWithdraw: '撤回',
    forkName: 'Acme Charge 65W（分叉）',
    confirmFork: '分叉',
    confirmClone: '克隆',
    neverCopied:
      '销售方案、价格、市场来源映射和落地页永远不会被复制，请为新产品另行创建。',
    cloneRefused:
      '销售方案、价格、市场来源映射和落地页不会被复制，请为新产品另行创建。',
    notFound: '找不到这个版本，或你没有访问权限。',
  },
} as const;
type Locale = keyof typeof COPY;

function renderIn(locale: Locale, onEntryCreated = jest.fn()) {
  // A fork of the suite's instance (`jest.setup.js`), which has no `library`.
  const i18n = getI18n().cloneInstance({
    lng: locale,
    fallbackLng: false,
    forkResourceStore: true,
  });
  i18n.addResourceBundle(locale, 'library', COPY[locale].library);
  i18n.addResourceBundle(locale, 'common', COPY[locale].common);
  void i18n.changeLanguage(locale);
  const view = render(
    <I18nextProvider i18n={i18n}>
      <VersionsTab
        entryId={
          (mockQuery.data as { entry_id?: string } | undefined)?.entry_id ??
          'lib_01'
        }
        entryDisplayName="Acme Charge 65W"
        selectedVersionId={
          (mockQuery.data as { id?: string } | undefined)?.id ?? 'libver_01'
        }
        onSelectVersion={jest.fn()}
        onEntryCreated={onEntryCreated}
      />
    </I18nextProvider>
  );
  return { ...view, onEntryCreated };
}

/** No server code, key name or server diagnostic in the text or a tooltip. */
function expectNoRawCodes(container: HTMLElement) {
  const raw = /\b[a-z]+_[a-z_]+\b|commands\.|eligibility\.|创意库/;
  expect(container.textContent).not.toMatch(raw);
  for (const element of Array.from(container.querySelectorAll('[title]')))
    expect(element.getAttribute('title')).not.toMatch(raw);
}

const button = (name: string) =>
  screen.getByRole('button', { name, hidden: true });
/** The reason lines under the version toolbar. */
const reasonLines = () =>
  Array.from(
    screen.getByRole('toolbar').parentElement!.querySelectorAll('li'),
    (line) => line.textContent
  );

beforeEach(() => {
  mockRun = jest.fn().mockResolvedValue({
    kind: 'success',
    data: { id: 'lib_new' },
    meta: { etag: null, retryAt: null, location: null },
  });
  mockPreview = jest.fn().mockResolvedValue({
    kind: 'success',
    data: fixtures.owner_withdraw_impact,
  });
  mockQuery = { data: fixtures.owner_effective_product };
  mockCloneProblem = undefined;
});

describe.each(Object.keys(COPY) as Locale[])('%s', (locale) => {
  const copy = COPY[locale];

  it('lets an Owner withdraw after reading the impact analysis', async () => {
    const { container, unmount } = renderIn(locale);

    expect(screen.getByText(copy.usable)).toBeInTheDocument();
    expect(reasonLines()).toEqual([]);
    expectNoRawCodes(container);

    fireEvent.click(button(copy.withdraw));
    await waitFor(() => expect(mockPreview).toHaveBeenCalledTimes(1));
    expect(mockPreview.mock.calls[0][0]).toMatchObject({
      versionId: 'libver_01',
    });

    const impact = await screen.findByTestId('withdraw-impact');
    expect(impact).toHaveTextContent(copy.impactTitle);
    expect(impact).toHaveTextContent(copy.clearsCurrent);
    expect(impact).toHaveTextContent(copy.dependents);
    expect(impact).toHaveTextContent(copy.offerBlocked);
    expectNoRawCodes(container);

    const confirm = screen
      .getAllByRole('button', { name: copy.confirmWithdraw, hidden: true })
      .find((b) => !b.hasAttribute('data-command'))!;
    expect(confirm).toBeDisabled();
    fireEvent.change(
      screen.getByRole('textbox', { name: copy.reasonLabel, hidden: true }),
      { target: { value: 'Recalled for legal review' } }
    );
    expect(confirm).not.toBeDisabled();
    fireEvent.click(confirm);

    await waitFor(() => expect(mockRun).toHaveBeenCalledTimes(1));
    expect(mockRun).toHaveBeenCalledWith('withdraw', {
      version_id: 'libver_01',
      body: {
        reason: 'Recalled for legal review',
        impact_id: fixtures.owner_withdraw_impact.impact_id,
        impact_hash: fixtures.owner_withdraw_impact.impact_hash,
      },
    });
    await waitFor(() =>
      expect(screen.queryByTestId('withdraw-impact')).toBeNull()
    );

    // The withdrawn version, as the server reads it back.
    unmount();
    mockQuery = { data: fixtures.owner_withdrawn_product };
    renderIn(locale);
    expect(screen.getAllByText(copy.withdrawn).length).toBeGreaterThan(0);
    expect(screen.getByText(copy.blocked)).toBeInTheDocument();
    expect(reasonLines()).toEqual([copy.allWithdrawn]);
    for (const name of [copy.withdraw, copy.fork, copy.clone])
      expect(button(name)).toHaveAttribute('aria-disabled', 'true');
  });

  it('shows a Member Withdraw held back with its reason', () => {
    mockQuery = { data: fixtures.member_effective_product };
    const { container } = renderIn(locale);

    const withdraw = button(copy.withdraw);
    expect(withdraw).toHaveAttribute('aria-disabled', 'true');
    expect(withdraw).not.toBeDisabled();
    expect(withdraw).toHaveAccessibleDescription(copy.ownerOnly);
    expect(reasonLines()).toEqual([copy.ownerOnly]);
    fireEvent.click(withdraw);
    expect(mockPreview).not.toHaveBeenCalled();
    expect(screen.queryByTestId('withdraw-impact')).toBeNull();

    expect(button(copy.fork)).not.toHaveAttribute('aria-disabled');
    expect(button(copy.clone)).not.toHaveAttribute('aria-disabled');
    expectNoRawCodes(container);
  });

  it('holds back Clone Product on a version that is not a Product', () => {
    mockQuery = { data: fixtures.member_effective_person };
    const { container } = renderIn(locale);

    expect(button(copy.clone)).toHaveAttribute('aria-disabled', 'true');
    expect(reasonLines()).toEqual([copy.ownerOnly, copy.productsOnly]);
    expectNoRawCodes(container);
  });

  it('opens the forked entry on its Draft', async () => {
    const { onEntryCreated } = renderIn(locale);
    clickInToolbar(copy.fork);

    const name = screen.getByRole('textbox', { hidden: true });
    expect(name).toHaveValue(copy.forkName);
    fireEvent.click(
      screen
        .getAllByRole('button', { name: copy.confirmFork, hidden: true })
        .find((b) => !b.hasAttribute('data-command'))!
    );

    await waitFor(() => expect(onEntryCreated).toHaveBeenCalledWith('lib_new'));
    expect(mockRun).toHaveBeenCalledWith('fork', {
      version_id: 'libver_01',
      body: { display_name: copy.forkName },
    });
  });

  it('clones a Product without Offers and opens the new product', async () => {
    const { onEntryCreated } = renderIn(locale);
    clickInToolbar(copy.clone);

    expect(screen.getByText(copy.neverCopied)).toBeInTheDocument();
    fireEvent.click(button(copy.confirmClone));

    await waitFor(() => expect(onEntryCreated).toHaveBeenCalledWith('lib_new'));
    const [kind, params] = mockRun.mock.calls[0];
    expect(kind).toBe('clone');
    expect(params.body.include_optional).toEqual([]);
    expect(Object.keys(params.body).sort()).toEqual([
      'display_name',
      'include_optional',
    ]);
  });

  it('words a Clone Product refused for copying Offers as create them separately', () => {
    mockCloneProblem = fixtures.clone_with_offers_refused;
    renderIn(locale);
    clickInToolbar(copy.clone);

    const dialog = screen.getByRole('dialog', { hidden: true });
    expect(screen.getByRole('alert', { hidden: true })).toHaveTextContent(
      copy.cloneRefused
    );
    expect(dialog.textContent).not.toContain(
      fixtures.clone_with_offers_refused.detail
    );
    expectNoRawCodes(dialog);
  });

  it("words another Workspace's version id as not found", () => {
    mockQuery = { problem: fixtures.other_workspace_not_found };
    const { container } = renderIn(locale);

    expect(screen.getByRole('alert')).toHaveTextContent(copy.notFound);
    expect(screen.queryByRole('toolbar')).toBeNull();
    expect(container.textContent).not.toContain(
      fixtures.other_workspace_not_found.detail
    );
    expectNoRawCodes(container);
  });
});

/** Clicks the version toolbar's button named `name`. */
function clickInToolbar(name: string) {
  const target = Array.from(
    screen.getByRole('toolbar').querySelectorAll('button')
  ).find((b) => b.textContent === name);
  if (!target) throw new Error(`no ${name} in the toolbar`);
  fireEvent.click(target);
}
