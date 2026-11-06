/**
 * @jest-environment jsdom
 */
import { useState } from 'react';
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { I18nextProvider, getI18n } from 'react-i18next';
import { TargetManager } from '@/components/scale/targets/TargetManager';
import enScale from '../../locales/en/scale.json';
import zhScale from '../../locales/zh-CN/scale.json';
import zhCommon from '../../locales/zh-CN/common.json';

const mockPreviewRun = jest.fn();
const mockPublishRun = jest.fn();
const mockWithdrawPreviewRun = jest.fn();
const mockWithdrawRun = jest.fn();
const mockRefresh = jest.fn();
let mockRole = 'OWNER';
let mockContexts: object[] = [];
let mockTargets: object[] = [];

/** A command hook that keeps `problem` and `isPending` the way
 * `useDomainCommand` does: cleared when a run starts, set by a refusal. */
function useMockCommand(run: jest.Mock) {
  const [problem, setProblem] = useState<object | undefined>();
  const [isPending, setIsPending] = useState(false);
  return {
    problem,
    isPending,
    reset: () => setProblem(undefined),
    run: async (params: object) => {
      setProblem(undefined);
      setIsPending(true);
      const outcome = await run(params);
      setIsPending(false);
      if (outcome.kind === 'problem') setProblem(outcome.problem);
      return outcome;
    },
  };
}

jest.mock('@/hooks/scale/useScaleTargets', () => ({
  useScaleTargets: () => ({
    items: mockTargets,
    hasMore: false,
    isLoading: false,
    isLoadingMore: false,
    problem: undefined,
    loadMore: jest.fn(),
    refresh: mockRefresh,
  }),
  useScaleTargetContexts: () => ({ contexts: mockContexts, isLoading: false }),
  useCreateScaleTargetPublicationPreview: () => useMockCommand(mockPreviewRun),
  usePublishScaleTarget: () => useMockCommand(mockPublishRun),
  useCreateScaleTargetWithdrawalPreview: () =>
    useMockCommand(mockWithdrawPreviewRun),
  useWithdrawScaleTarget: () => useMockCommand(mockWithdrawRun),
}));
jest.mock('@/lib/scale/meta-mode', () => ({
  ...jest.requireActual('@/lib/scale/meta-mode'),
  useScaleMetaMode: () => ({
    metaMode: 'customer_sandbox',
    setMetaMode: jest.fn(),
  }),
}));
jest.mock('@/components/scale/shared/MetaModeSelect', () => ({
  MetaModeSelect: () => <select data-testid="meta-mode-select" />,
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme', role: mockRole } }),
}));

const context = (n: number) => ({
  product_version_id: `pv_${n}`,
  offer_version_id: `ov_${n}`,
  market_code: n === 2 ? 'DE' : 'US',
  currency: n === 2 ? 'EUR' : 'USD',
  has_active_target: false,
});

/** The server echoes the amounts back; `Decimal("0")` is falsy, so a
 * typed "0" comes back as `null` (`app/scale/v1_router.py`). */
const publicationPreview = (id: string, n: number, affected: number) => ({
  id,
  ...context(n),
  target_cpa: '20.50',
  breakeven_roas: null,
  minimum_economic_spend: null,
  affected_enrollment_count: affected,
  policy_floor_check_passed: true,
  impact_hash: `hash_${id}`,
  expires_at: '2026-11-05T10:15:00Z',
  created_at: '2026-11-05T10:00:00Z',
});

const withdrawalPreview = (id: string, affected: number, threads: number) => ({
  id,
  target_version_id: 'stgt_1',
  reason: 'Margin changed',
  affected_enrollment_count: affected,
  dependent_advice_thread_count: threads,
  impact_hash: `hash_${id}`,
  expires_at: '2026-11-05T10:15:00Z',
  created_at: '2026-11-05T10:00:00Z',
});

const target = (withdraw: object) => ({
  id: 'stgt_1',
  ...context(1),
  target_cpa: '20.50',
  breakeven_roas: null,
  minimum_economic_spend: null,
  lifecycle_state: 'active',
  creator_id: 'user_owner',
  revision: 1,
  published_at: '2026-11-05T09:00:00Z',
  withdrawn_at: null,
  created_at: '2026-11-05T09:00:00Z',
  allowed_actions: [{ action: 'withdraw', requirements: [], ...withdraw }],
});

const problem = (code: string, status: number) => ({
  kind: 'problem',
  problem: {
    type: `urn:marketai:error:${code.replace(/_/g, '-')}`,
    title: code,
    status,
    code,
    detail: code,
    trace_id: 'trace_382',
    retryable: false,
    field_errors: [],
  },
});

const success = (data: object) => ({ kind: 'success', data });

/** What FastAPI answers for a CPA of 0 and a ROAS too wide for its column. */
const refusal = {
  type: 'urn:marketai:error:validation-error',
  title: 'validation_error',
  status: 422,
  detail: 'Request validation failed',
  instance: '/target-publication-previews',
  code: 'validation_error',
  trace_id: 'trace-381',
  retryable: false,
  field_errors: [
    {
      field: 'body.target_cpa',
      code: 'greater_than',
      message: 'Input should be greater than 0',
      params: { gt: 0 },
    },
    {
      field: 'body.breakeven_roas',
      code: 'decimal_whole_digits',
      message: 'Decimal input should have no more than 6 digits',
      params: { whole_digits: 6 },
    },
  ],
};

const COPY = {
  en: {
    scale: enScale,
    newTarget: 'New target',
    ownerOnly: 'Only the Workspace Owner can publish or withdraw targets.',
    denied: 'Only the Workspace Owner can do this.',
    withdraw: 'Withdraw',
    withdrawTitle: 'Withdraw target',
    reason: 'Reason',
    context: 'Context',
    cpa: 'Target CPA',
    roas: 'Breakeven ROAS',
    spend: 'Minimum economic spend',
    preview: 'Create preview',
    confirmPublish: 'Confirm publish',
    confirmWithdraw: 'Confirm withdrawal',
    refresh: 'Create a new preview',
    expired: 'The preview expired. Create a new one.',
    affected: 'Affected enrollments',
    cpaReason: 'Enter a number greater than 0.',
    roasReason: 'This number has too many digits before the decimal point.',
  },
  'zh-CN': {
    scale: zhScale,
    newTarget: '新建目标',
    ownerOnly: '只有工作区 Owner 可以发布或撤回目标。',
    denied: '只有工作区 Owner 可以执行这个操作。',
    withdraw: '撤回',
    withdrawTitle: '撤回目标',
    reason: '原因',
    context: '上下文',
    cpa: '目标 CPA',
    roas: '盈亏平衡 ROAS',
    spend: '最低经济花费',
    preview: '创建预览',
    confirmPublish: '确认发布',
    confirmWithdraw: '确认撤回',
    refresh: '重新生成预览',
    expired: '预览已过期，请重新生成。',
    affected: '受影响的监控',
    cpaReason: '请填写大于 0 的数字。',
    roasReason: '整数部分位数太多。',
  },
} as const;
type Locale = keyof typeof COPY;

function renderIn(locale: Locale) {
  // A fork of the suite's instance (`jest.setup.js`), which has no `scale`
  // and only the en `common` that `errorMessage` reads.
  const i18n = getI18n().cloneInstance({
    lng: locale,
    fallbackLng: false,
    forkResourceStore: true,
  });
  i18n.addResourceBundle(locale, 'scale', COPY[locale].scale);
  if (locale === 'zh-CN') i18n.addResourceBundle(locale, 'common', zhCommon);
  void i18n.changeLanguage(locale);
  return render(
    <I18nextProvider i18n={i18n}>
      <TargetManager />
    </I18nextProvider>
  );
}

/** The value a list-item `<dd>` shows under its `<dt>` label. */
function shown(dialog: HTMLElement, label: string) {
  return within(dialog).getByText(label).nextElementSibling?.textContent;
}

async function previewPublication(copy: (typeof COPY)[Locale]) {
  fireEvent.click(screen.getByRole('button', { name: copy.newTarget }));
  const dialog = screen.getByRole('dialog');
  fireEvent.change(within(dialog).getByRole('combobox'), {
    target: { value: '1' },
  });
  // Padded, so both previews have to trim the same way (#381).
  fireEvent.change(within(dialog).getByLabelText(copy.cpa), {
    target: { value: ' 20.50\t' },
  });
  fireEvent.change(within(dialog).getByLabelText(copy.spend), {
    target: { value: '0\n' },
  });
  fireEvent.click(within(dialog).getByRole('button', { name: copy.preview }));
  await within(dialog).findByRole('button', { name: copy.confirmPublish });
  return dialog;
}

async function previewWithdrawal(copy: (typeof COPY)[Locale]) {
  fireEvent.click(screen.getByRole('button', { name: copy.withdraw }));
  const dialog = screen.getByRole('dialog');
  fireEvent.change(within(dialog).getByLabelText(copy.reason), {
    target: { value: 'Margin changed' },
  });
  fireEvent.click(within(dialog).getByRole('button', { name: copy.preview }));
  await within(dialog).findByRole('button', { name: copy.confirmWithdraw });
  return dialog;
}

beforeEach(() => {
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', {
    configurable: true,
    value: function (this: HTMLDialogElement) {
      this.setAttribute('open', '');
    },
  });
  jest.resetAllMocks();
  mockRole = 'OWNER';
  mockContexts = [context(0), context(1)];
  mockTargets = [];
});

describe('a publication preview that expired before confirming', () => {
  test.each(['en', 'zh-CN'] as const)(
    'is renewed in place and confirmed again in %s',
    async (locale) => {
      const copy = COPY[locale];
      mockPreviewRun
        .mockResolvedValueOnce(success(publicationPreview('stpp_1', 1, 3)))
        .mockResolvedValueOnce(success(publicationPreview('stpp_2', 1, 5)));
      mockPublishRun
        .mockResolvedValueOnce(problem('preview_expired', 409))
        .mockResolvedValueOnce(success(target({ allowed: true })));
      renderIn(locale);

      const dialog = await previewPublication(copy);
      expect(shown(dialog, copy.affected)).toBe('3');

      fireEvent.click(
        within(dialog).getByRole('button', { name: copy.confirmPublish })
      );
      expect(await within(dialog).findByRole('alert')).toHaveTextContent(
        copy.expired
      );
      expect(
        within(dialog).queryByRole('button', { name: copy.confirmPublish })
      ).toBeNull();

      fireEvent.click(
        within(dialog).getByRole('button', { name: copy.refresh })
      );
      await within(dialog).findByRole('button', { name: copy.confirmPublish });
      expect(mockPreviewRun).toHaveBeenCalledTimes(2);
      expect(mockPreviewRun.mock.calls[1][0]).toEqual(
        mockPreviewRun.mock.calls[0][0]
      );
      expect(mockPreviewRun.mock.calls[1][0]).toEqual({
        metaMode: 'customer_sandbox',
        productVersionId: 'pv_1',
        offerVersionId: 'ov_1',
        marketCode: 'US',
        currency: 'USD',
        targetCpa: '20.50',
        breakevenRoas: '',
        minimumEconomicSpend: '0',
      });
      expect(shown(dialog, copy.affected)).toBe('5');
      expect(within(dialog).queryByRole('alert')).toBeNull();
      // Nothing is confirmed until the user confirms the new preview.
      expect(mockPublishRun).toHaveBeenCalledTimes(1);

      fireEvent.click(
        within(dialog).getByRole('button', { name: copy.confirmPublish })
      );
      await screen.findByRole('button', { name: copy.newTarget });
      expect(mockPublishRun.mock.calls.map(([p]) => p)).toEqual([
        { metaMode: 'customer_sandbox', publicationPreviewId: 'stpp_1' },
        { metaMode: 'customer_sandbox', publicationPreviewId: 'stpp_2' },
      ]);
      expect(mockRefresh).toHaveBeenCalled();
      expect(document.body).not.toHaveTextContent('preview_expired');
    }
  );

  test('keeps the previewed context after the context list reorders', async () => {
    const copy = COPY.en;
    mockPreviewRun
      .mockResolvedValueOnce(success(publicationPreview('stpp_1', 1, 3)))
      .mockResolvedValueOnce(success(publicationPreview('stpp_2', 1, 4)));
    mockPublishRun.mockResolvedValueOnce(problem('preview_expired', 409));
    renderIn('en');

    const dialog = await previewPublication(copy);
    // A refetch on focus returns the list in another order: index 1 is
    // now a different context.
    mockContexts = [context(1), context(2)];
    fireEvent.click(
      within(dialog).getByRole('button', { name: copy.confirmPublish })
    );
    fireEvent.click(
      await within(dialog).findByRole('button', { name: copy.refresh })
    );
    await within(dialog).findByRole('button', { name: copy.confirmPublish });

    expect(mockPreviewRun.mock.calls[1][0]).toMatchObject({
      productVersionId: 'pv_1',
      offerVersionId: 'ov_1',
      marketCode: 'US',
      currency: 'USD',
    });
  });

  // A refused amount reads as the summary sentence here: the fields stay
  // behind the confirm step.
  test.each([
    ['owner_required', problem('owner_required', 403), COPY.en.denied],
    [
      'validation_error',
      { kind: 'problem', problem: refusal },
      COPY.en.cpaReason,
    ],
  ])('shows why a new preview was refused: %s', async (_, refused, reason) => {
    const copy = COPY.en;
    mockPreviewRun
      .mockResolvedValueOnce(success(publicationPreview('stpp_1', 1, 3)))
      .mockResolvedValueOnce(refused);
    mockPublishRun.mockResolvedValueOnce(problem('preview_expired', 409));
    renderIn('en');

    const dialog = await previewPublication(copy);
    fireEvent.click(
      within(dialog).getByRole('button', { name: copy.confirmPublish })
    );
    fireEvent.click(
      await within(dialog).findByRole('button', { name: copy.refresh })
    );

    expect(await within(dialog).findByText(reason)).toHaveAttribute(
      'role',
      'alert'
    );
    expect(
      within(dialog).queryByRole('button', { name: copy.confirmPublish })
    ).toBeNull();
    expect(within(dialog).queryByLabelText(copy.cpa)).toBeNull();
    expect(dialog).not.toHaveTextContent(
      /owner_required|validation_error|greater_than|body\./
    );
  });
});

test('an already-consumed publication preview is not renewed', async () => {
  const copy = COPY.en;
  mockPreviewRun.mockResolvedValueOnce(
    success(publicationPreview('stpp_1', 1, 3))
  );
  mockPublishRun.mockResolvedValueOnce(
    problem('preview_already_consumed', 409)
  );
  renderIn('en');

  const dialog = await previewPublication(copy);
  fireEvent.click(
    within(dialog).getByRole('button', { name: copy.confirmPublish })
  );

  expect(await within(dialog).findByRole('alert')).toHaveTextContent(
    'This preview was already used.'
  );
  expect(
    within(dialog).queryByRole('button', { name: copy.refresh })
  ).toBeNull();
  expect(mockPreviewRun).toHaveBeenCalledTimes(1);
});

test.each(['en', 'zh-CN'] as const)(
  'an expired withdrawal preview is renewed in place in %s',
  async (locale) => {
    const copy = COPY[locale];
    mockTargets = [target({ allowed: true, reason_code: null })];
    mockWithdrawPreviewRun
      .mockResolvedValueOnce(success(withdrawalPreview('stwp_1', 2, 1)))
      .mockResolvedValueOnce(success(withdrawalPreview('stwp_2', 4, 1)));
    mockWithdrawRun
      .mockResolvedValueOnce(problem('preview_expired', 409))
      .mockResolvedValueOnce(
        success({ ...target({ allowed: false }), lifecycle_state: 'withdrawn' })
      );
    renderIn(locale);

    const dialog = await previewWithdrawal(copy);
    expect(shown(dialog, copy.affected)).toBe('2');
    fireEvent.click(
      within(dialog).getByRole('button', { name: copy.confirmWithdraw })
    );
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      copy.expired
    );
    expect(
      within(dialog).queryByRole('button', { name: copy.confirmWithdraw })
    ).toBeNull();

    fireEvent.click(within(dialog).getByRole('button', { name: copy.refresh }));
    await within(dialog).findByRole('button', { name: copy.confirmWithdraw });
    expect(mockWithdrawPreviewRun.mock.calls.map(([p]) => p)).toEqual([
      {
        targetId: 'stgt_1',
        metaMode: 'customer_sandbox',
        reason: 'Margin changed',
      },
      {
        targetId: 'stgt_1',
        metaMode: 'customer_sandbox',
        reason: 'Margin changed',
      },
    ]);
    expect(shown(dialog, copy.affected)).toBe('4');
    expect(within(dialog).queryByRole('alert')).toBeNull();
    expect(mockWithdrawRun).toHaveBeenCalledTimes(1);

    fireEvent.click(
      within(dialog).getByRole('button', { name: copy.confirmWithdraw })
    );
    await screen.findByRole('button', { name: copy.withdraw });
    expect(mockWithdrawRun.mock.calls[1][0]).toEqual({
      targetId: 'stgt_1',
      metaMode: 'customer_sandbox',
      withdrawalPreviewId: 'stwp_2',
    });
    expect(mockRefresh).toHaveBeenCalled();
  }
);

describe('the Workspace role', () => {
  test.each(['en', 'zh-CN'] as const)(
    'a Member gets no publish trigger and a denied withdraw in %s',
    (locale) => {
      const copy = COPY[locale];
      mockRole = 'MEMBER';
      mockTargets = [target({ allowed: false, reason_code: 'owner_required' })];
      renderIn(locale);

      expect(screen.queryByRole('button', { name: copy.newTarget })).toBeNull();
      expect(screen.getByText(copy.ownerOnly)).toBeInTheDocument();

      const withdraw = screen.getByRole('button', { name: copy.withdraw });
      expect(withdraw).toHaveAttribute('aria-disabled', 'true');
      expect(withdraw).toHaveAttribute('title', copy.denied);
      fireEvent.click(withdraw);
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(mockWithdrawPreviewRun).not.toHaveBeenCalled();

      expect(document.body).not.toHaveTextContent('owner_required');
      expect(document.body).not.toHaveTextContent('preview_expired');
    }
  );

  test.each(['en', 'zh-CN'] as const)(
    'an Owner gets the publish trigger and no Owner-only line in %s',
    (locale) => {
      const copy = COPY[locale];
      mockTargets = [target({ allowed: true, reason_code: null })];
      renderIn(locale);

      expect(
        screen.getByRole('button', { name: copy.newTarget })
      ).toBeInTheDocument();
      expect(screen.queryByText(copy.ownerOnly)).toBeNull();
      expect(
        screen.getByRole('button', { name: copy.withdraw })
      ).not.toHaveAttribute('aria-disabled');
    }
  );
});

describe('amounts the server refuses (#381)', () => {
  test.each(['en', 'zh-CN'] as const)(
    'shows each refused amount under its own field in %s',
    async (locale) => {
      const copy = COPY[locale];
      mockPreviewRun.mockResolvedValueOnce({
        kind: 'problem',
        problem: refusal,
      });
      renderIn(locale);

      fireEvent.click(screen.getByRole('button', { name: copy.newTarget }));
      fireEvent.change(screen.getByLabelText(copy.context), {
        target: { value: '0' },
      });
      const cpa = screen.getByLabelText(copy.cpa);
      const roas = screen.getByLabelText(copy.roas);
      const spend = screen.getByLabelText(copy.spend);
      fireEvent.change(cpa, { target: { value: ' 0 ' } });
      fireEvent.change(roas, { target: { value: '\t1234567\n' } });
      fireEvent.click(screen.getByRole('button', { name: copy.preview }));

      await waitFor(() => expect(roas).toHaveAttribute('aria-invalid', 'true'));
      expect(mockPreviewRun).toHaveBeenCalledWith(
        expect.objectContaining({
          targetCpa: '0',
          breakevenRoas: '1234567',
          minimumEconomicSpend: '',
        })
      );
      expect(roas).toHaveAccessibleDescription(copy.roasReason);
      expect(cpa).toHaveAttribute('aria-invalid', 'true');
      expect(cpa).toHaveAccessibleDescription(copy.cpaReason);
      expect(spend).not.toHaveAttribute('aria-invalid');
      expect(spend).not.toHaveAccessibleDescription();
      // The summary below the fields still reads the first reason.
      expect(screen.getByRole('alert')).toHaveTextContent(copy.cpaReason);
      expect(document.body.textContent).not.toMatch(
        /greater_than|decimal_whole_digits|body\./
      );
    }
  );

  test('a refusal that is not about an amount marks no field', async () => {
    const copy = COPY.en;
    mockPreviewRun.mockResolvedValueOnce(problem('owner_required', 403));
    renderIn('en');

    fireEvent.click(screen.getByRole('button', { name: copy.newTarget }));
    fireEvent.change(screen.getByLabelText(copy.context), {
      target: { value: '0' },
    });
    fireEvent.change(screen.getByLabelText(copy.cpa), {
      target: { value: '20' },
    });
    fireEvent.click(screen.getByRole('button', { name: copy.preview }));

    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
    for (const label of [copy.cpa, copy.roas, copy.spend]) {
      expect(screen.getByLabelText(label)).not.toHaveAttribute('aria-invalid');
    }
  });
});
