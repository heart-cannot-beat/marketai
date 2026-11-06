import { getI18n } from 'react-i18next';
import en from '../../../locales/en/common.json';
import zhCN from '../../../locales/zh-CN/common.json';
import { metricsIssueText } from '@/components/workspace/marketing-issues';

/** The real catalogues, so the wording under test is what ships. */
const translator = async (lng: 'en' | 'zh-CN') => {
  // A separate instance of the one jest.setup.js boots, holding both catalogues.
  const i18n = getI18n().createInstance();
  await i18n.init({
    lng,
    fallbackLng: false,
    resources: { en: { common: en }, 'zh-CN': { common: zhCN } },
    defaultNS: 'common',
    interpolation: { escapeValue: false },
    showSupportNotice: false,
  });
  return i18n.t.bind(i18n) as unknown as Parameters<typeof metricsIssueText>[0];
};

describe('metricsIssueText (#255)', () => {
  it('words a code with its params in the reader’s language', async () => {
    const issue = {
      row: 9,
      field: 'date',
      code: 'outside_round_dates',
      params: { start: '2025-01-01', end: '2025-01-07' },
    };
    expect(metricsIssueText(await translator('en'), issue)).toBe(
      'Row 9 · Date: The date is outside this round (2025-01-01 to 2025-01-07).'
    );
    expect(metricsIssueText(await translator('zh-CN'), issue)).toBe(
      '第 9 行 · 日期：日期不在本轮范围内（2025-01-01 至 2025-01-07）。'
    );
  });

  it('reads an issue stored before params without showing the template', async () => {
    const issue = { row: 9, field: 'date', code: 'outside_round_dates' };
    expect(metricsIssueText(await translator('en'), issue)).toBe(
      'Row 9 · Date: The date is outside this round (… to …).'
    );
  });

  it('names a file-level issue without a row and joins list params', async () => {
    const t = await translator('zh-CN');
    expect(
      metricsIssueText(t, {
        row: 0,
        field: 'context',
        code: 'reporting_context_mismatch',
        params: {
          currency: 'USD',
          timezone: 'UTC',
          attribution_window: '7d_click_1d_view',
        },
      })
    ).toBe(
      '报表设置：币种、时区或归因设置与冻结的这一轮不一致（USD、UTC、7d_click_1d_view）。'
    );
    expect(
      metricsIssueText(await translator('en'), {
        row: 1,
        field: 'spend',
        code: 'ambiguous_column_mapping',
        params: { columns: ['Amount spent', 'spend'] },
      })
    ).toBe(
      'Row 1 · Amount spent: Several columns could supply this metric (Amount spent, spend). Choose the exact source column.'
    );
  });
});
