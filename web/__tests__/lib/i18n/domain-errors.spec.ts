import { getI18n } from 'react-i18next';
import en from '../../../locales/en/common.json';
import zhCN from '../../../locales/zh-CN/common.json';
import {
  apiErrorMessage,
  DomainProblemError,
  errorCodeMessage,
  errorMessage,
  errorMessageOf,
  fieldErrorMessage,
  onMissingErrorCode,
} from '@/lib/i18n/domain-errors';

/**
 * #253: the page renders a refusal by its code in the interface language,
 * interpolating `params`; the service's `detail` never reaches it.
 */
// The Jest setup's instance (en: common, products, ...) plus zh-CN copy.
const instance = getI18n();
instance.addResourceBundle('zh-CN', 'common', zhCN, true, true);
const tIn = (lng: string, ns = 'common') =>
  instance.getFixedT(lng, ns) as never;

const missing: string[] = [];
beforeEach(() => {
  missing.length = 0;
  onMissingErrorCode((code) => missing.push(code));
});
afterAll(() =>
  onMissingErrorCode((code) => {
    throw new Error(`No "error-${code}" copy in locales/en/common.json`);
  })
);

describe('errorMessage', () => {
  const quota = {
    code: 'budget_exceeded',
    detail: '本次生成超过工作区预算上限或未结算成本上限。',
    params: { limit: '5.00', amount: '7.50', currency: 'USD' },
  };

  it('renders the code in each interface language, never the detail', () => {
    const english = errorMessage(tIn('en'), quota);
    const chinese = errorMessage(tIn('zh-CN'), quota);
    expect(english).toBe(en['error-budget_exceeded']);
    expect(chinese).toBe(zhCN['error-budget_exceeded']);
    expect(english).not.toContain(quota.detail);
    expect(chinese).not.toBe(quota.detail);
  });

  it('interpolates params in both languages', () => {
    const tooLarge = {
      code: 'file_too_large',
      detail: 'Upload exceeds 20971520 bytes',
      params: { limit_mb: 20 },
    };
    expect(errorMessage(tIn('en'), tooLarge)).toBe(
      'The file is larger than 20 MB.'
    );
    expect(errorMessage(tIn('zh-CN'), tooLarge)).toBe('文件超过 20 MB。');
  });

  it('never shows a placeholder a record did not fill', () => {
    expect(errorMessage(tIn('en'), { code: 'file_too_large' })).toBe(
      'The file is larger than … MB.'
    );
  });

  it('reads a failed validation as its first field reason, with the limit', () => {
    const tooLong = {
      code: 'validation_error',
      detail: 'Request validation failed',
      field_errors: [
        {
          field: 'body.source_text',
          code: 'string_too_long',
          message: 'String should have at most 16000 characters',
          params: { max_length: 16000 },
        },
      ],
    };
    expect(errorMessage(tIn('en'), tooLong)).toBe(
      'Use at most 16000 characters.'
    );
    expect(errorMessage(tIn('zh-CN'), tooLong)).toBe('最多填写 16000 个字符。');
  });

  it('finds the copy from a component bound to another namespace', () => {
    expect(errorMessage(tIn('en', 'products'), quota)).toBe(
      en['error-budget_exceeded']
    );
  });

  it('keeps params apart from i18next options', () => {
    // A param named like an option (`count`) must not pick a plural form.
    expect(
      errorCodeMessage(tIn('en'), 'text_too_long', { limit: 3, count: 2 })
    ).toBe('The description can be at most 3 characters.');
  });

  it('falls back to the code itself, never the detail, and reports it', () => {
    expect(
      errorMessage(tIn('en'), {
        code: 'brand_new_code',
        detail: '服务端原话',
      })
    ).toBe('brand_new_code');
    expect(missing).toEqual(['brand_new_code']);
  });

  it('reads run-time codes as their family', () => {
    expect(errorCodeMessage(tIn('en'), 'openai_502')).toBe(
      en['error-provider_http_error']
    );
    expect(errorCodeMessage(tIn('zh-CN'), 'meta_190_463')).toBe(
      zhCN['error-meta_api_error']
    );
    expect(missing).toEqual([]);
  });

  it('has a sentence for no problem and no code', () => {
    expect(errorMessage(tIn('en'), null)).toBe(en['error-unknown']);
    expect(errorMessage(tIn('en'), { detail: 'Internal' })).toBe(
      en['error-unknown']
    );
  });
});

describe('errorMessageOf', () => {
  it('reads a thrown problem by its code', () => {
    const error = new DomainProblemError({
      code: 'text_too_long',
      params: { limit: 20000 },
    });
    expect(errorMessageOf(tIn('zh-CN'), error)).toBe('介绍最多 20000 个字符。');
  });

  it('translates a page key and hides any other message', () => {
    expect(errorMessageOf(tIn('en'), new Error('error-unknown'))).toBe(
      en['error-unknown']
    );
    expect(errorMessageOf(tIn('en'), new Error('数据库未就绪'))).toBe(
      en['error-unknown']
    );
    expect(errorMessageOf(tIn('en'), 'boom')).toBe(en['error-unknown']);
  });
});

describe('fieldErrorMessage', () => {
  it('uses the field code, then the request code', () => {
    expect(
      fieldErrorMessage(tIn('en'), { field: 'body.name', code: 'missing' })
    ).toBe(en['error-missing']);
    expect(fieldErrorMessage(tIn('en'), { field: 'body.name' })).toBe(
      en['error-validation_error']
    );
  });
});

describe('apiErrorMessage', () => {
  it('renders a coded control-plane refusal in each language with its limit', () => {
    const refusal = {
      message: 'All seats in this Workspace are taken.',
      code: 'seat_limit_reached',
      params: { limit: 5 },
    };
    expect(apiErrorMessage(tIn('en'), refusal)).toBe(
      'All 5 seats in this Workspace are taken. Owners, Members and pending invitations each hold a seat; revoke an invitation or remove a member to free one.'
    );
    expect(apiErrorMessage(tIn('zh-CN'), refusal)).toBe(
      '这个工作区的 5 个席位已全部占用。Owner、Member 和待接受的邀请各占一个席位；撤销一张邀请或移除一名成员后再试。'
    );
    expect(
      apiErrorMessage(tIn('zh-CN'), {
        message: 'This Workspace is over its seat limit.',
        code: 'seat_limit_exceeded',
        params: { limit: 3 },
      })
    ).toContain('3 个席位');
    expect(missing).toEqual([]);
  });

  it("keeps an uncoded refusal's own message", () => {
    expect(
      apiErrorMessage(tIn('zh-CN'), { message: 'Invitation not found.' })
    ).toBe('Invitation not found.');
    expect(apiErrorMessage(tIn('en'), undefined)).toBe(en['error-unknown']);
  });
});
