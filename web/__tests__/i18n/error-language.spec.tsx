/**
 * @jest-environment jsdom
 */
import { act, fireEvent, render, screen } from '@testing-library/react';
import { getI18n } from 'react-i18next';
import zhCommon from '../../locales/zh-CN/common.json';
import zhProducts from '../../locales/zh-CN/products.json';
import enLibrary from '../../locales/en/library.json';
import zhLibrary from '../../locales/zh-CN/library.json';
import { NewProductFlow } from '@/components/products/NewProductFlow';
import { SubmitConfirmation } from '@/components/generation/preview/SubmitConfirmation';
import { ReferenceGateProgress } from '@/components/library/references/ReferenceGateProgress';
import type { UploadProgress } from '@/hooks/library/useLibraryReferenceUpload';

/**
 * #253 acceptance: a Library upload failure, a generation over its spending
 * limit and an over-long product import each read in the interface's own
 * language, from the response `code` and `params`; the service's `detail`
 * (here deliberately in the other language) never reaches the page.
 */
const mockExecute = jest.fn();
let mockIntake: Record<string, unknown> = {};

jest.mock('@/hooks/domain/useDomainCommand', () => ({
  useDomainCommand: () => ({
    execute: mockExecute,
    retry: jest.fn(),
    reset: jest.fn(),
    result: undefined,
    problem: undefined,
    reconciliation: undefined,
    isPending: false,
  }),
}));
jest.mock('@/components/generation/shared/useGenerationNav', () => ({
  useGenerationNav: () => ({ navigateToRun: jest.fn() }),
}));
jest.mock('next/router', () => ({
  useRouter: () => ({ push: jest.fn(), query: { slug: 'acme' } }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme', role: 'OWNER' } }),
}));
jest.mock('@/hooks/products/useProductIntake', () => ({
  useProductIntake: () => mockIntake,
}));
jest.mock('@/hooks/products/useIntakeImageUrl', () => ({
  useIntakeImageUrl: () => ({ blobUrl: null, isLoading: false, failed: false }),
}));

const i18n = getI18n();
beforeAll(() => {
  i18n.addResourceBundle('zh-CN', 'common', zhCommon, true, true);
  i18n.addResourceBundle('zh-CN', 'products', zhProducts, true, true);
  i18n.addResourceBundle('en', 'library', enLibrary, true, true);
  i18n.addResourceBundle('zh-CN', 'library', zhLibrary, true, true);
});
afterAll(async () => {
  await act(async () => {
    await i18n.changeLanguage('en');
  });
});

const uploadFailed = (problem: UploadProgress['problem']): UploadProgress => ({
  phase: 'failed',
  fileName: 'charger.png',
  bytesTransferred: 0,
  totalBytes: 30 * 1024 * 1024,
  referenceObjectId: null,
  referenceVersionId: null,
  referenceEntryId: null,
  boundEntryId: null,
  failureCode: null,
  problem,
});

const CASES = [
  {
    lng: 'en',
    upload: 'The file is larger than 20 MB.',
    quota:
      "This generation would exceed the Workspace's spending limit or the platform's per-request cost limit.",
    tooLong:
      'Could not start reading the product: Use at most 20000 characters.',
    otherLanguage: /[㐀-鿿]/,
  },
  {
    lng: 'zh-CN',
    upload: '文件超过 20 MB。',
    quota: '这次生成会超出工作区的花费上限或平台单次请求的费用上限。',
    tooLong: '没能开始识别产品：最多填写 20000 个字符。',
    otherLanguage: /Upload exceeds|String should have|Request validation/,
  },
];

describe.each(CASES)('in $lng', (expected) => {
  beforeEach(async () => {
    mockExecute.mockReset();
    await act(async () => {
      await i18n.changeLanguage(expected.lng);
    });
  });

  it('a Library upload failure', () => {
    render(
      <ReferenceGateProgress
        progress={uploadFailed({
          code: 'file_too_large',
          detail: 'Upload exceeds 20971520 bytes',
          params: { limit_mb: 20 },
        })}
      />
    );
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent(expected.upload);
    expect(alert).not.toHaveTextContent('20971520');
  });

  it('a generation over its spending limit', async () => {
    mockExecute.mockResolvedValue({
      kind: 'problem',
      problem: {
        type: 'urn:marketai:error:budget-exceeded',
        title: 'budget_exceeded',
        status: 422,
        detail:
          expected.lng === 'en'
            ? '本次生成超过工作区预算上限或未结算成本上限。'
            : 'Upload exceeds the guardrail',
        instance: '',
        code: 'budget_exceeded',
        params: { limit: '5.00', amount: '7.50', currency: 'USD' },
        trace_id: 't1',
        retryable: false,
        field_errors: [],
      },
      meta: { etag: null, retryAt: null, location: null },
    });
    render(
      <SubmitConfirmation
        previewId="prev-1"
        previewHash="hash-1"
        draftRevision={1}
        validUntil={new Date(Date.now() + 600_000).toISOString()}
        onSubmitted={jest.fn()}
      />
    );
    await act(async () => {
      fireEvent.click(
        screen.getByRole('button', {
          name: i18n.t('generation-submit-run-submit'),
        })
      );
    });
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(expected.quota);
    expect(alert).not.toHaveTextContent(expected.otherLanguage);
  });

  it('a product import that is too long', () => {
    mockIntake = {
      phase: 'input',
      detail: null,
      uploadProgress: null,
      lastInput: null,
      submit: jest.fn(),
      confirm: jest.fn(),
      startOver: jest.fn(),
      resumePolling: jest.fn(),
      problem: {
        stage: 'intake',
        code: 'validation_error',
        problem: {
          code: 'validation_error',
          detail: 'Request validation failed',
          field_errors: [
            {
              field: 'body.text',
              code: 'string_too_long',
              message: 'String should have at most 20000 characters',
              params: { max_length: 20000 },
            },
          ],
        },
      },
    };
    render(<NewProductFlow />);
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent(expected.tooLong);
    expect(alert).not.toHaveTextContent('String should have');
  });
});
