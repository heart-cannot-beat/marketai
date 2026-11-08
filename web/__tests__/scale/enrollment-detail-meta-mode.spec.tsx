/**
 * @jest-environment jsdom
 */
import { render, screen } from '@testing-library/react';
import { I18nextProvider, getI18n } from 'react-i18next';
import { EnrollmentDetail } from '@/components/scale/detail/EnrollmentDetail';
import { SCALE_META_MODES } from '@/lib/scale/meta-mode';
import { CJK } from '../locales/locale-files';
import en from '../../locales/en/scale.json';
import zhCN from '../../locales/zh-CN/scale.json';

/**
 * The Enrollment page words its Meta mode with the copy the mode selector
 * uses, never the raw `meta_mode` (#58).
 */
const mockUseScaleEnrollment = jest.fn();
jest.mock('@/hooks/scale/useScaleEnrollments', () => ({
  useScaleEnrollment: (...args: unknown[]) => mockUseScaleEnrollment(...args),
}));

const mockCommand = () => ({
  run: jest.fn(),
  reset: jest.fn(),
  isPending: false,
  problem: undefined,
  result: undefined,
});
jest.mock('@/hooks/scale/useScaleEnrollmentCommands', () => ({
  useCreateScaleActivationPreview: () => mockCommand(),
  usePauseScaleEnrollment: () => mockCommand(),
  useResumeScaleEnrollment: () => mockCommand(),
  useCloseScaleEnrollment: () => mockCommand(),
}));
jest.mock('@/hooks/scale/useScaleExport', () => ({
  useCreateScaleExport: () => mockCommand(),
  useCreateScaleExportDownloadIntent: () => mockCommand(),
}));
jest.mock('@/hooks/domain/useOperation', () => ({
  useOperation: () => ({
    operation: null,
    isTerminal: false,
    isPartialSuccess: false,
    problem: undefined,
  }),
}));
jest.mock('@/hooks/scale/useScaleNav', () => ({
  useScaleNav: () => ({
    buildListUrl: () => '/w/ws/scale',
    buildEvidenceUrl: (id: string) => `/w/ws/scale/${id}/evidence`,
    buildBreakdownsUrl: (id: string) => `/w/ws/scale/${id}/breakdowns`,
  }),
}));
// Panels with reads of their own; this test is about the summary only.
jest.mock('@/components/scale/detail/EpisodePanel', () => ({
  EpisodePanel: () => null,
}));
jest.mock('@/components/scale/detail/BreakdownTable', () => ({
  BreakdownTable: () => null,
}));
jest.mock('@/components/scale/advice/AdviceThreadList', () => ({
  AdviceThreadList: () => null,
}));
jest.mock('@/components/scale/detail/ActivateEnrollmentDialog', () => ({
  ActivateEnrollmentDialog: () => null,
}));
jest.mock('@/components/scale/detail/LifecycleReasonDialog', () => ({
  LifecycleReasonDialog: () => null,
}));

const enrollment = (metaMode: string) => ({
  id: 'enr-1',
  market_code: 'US',
  meta_mode: metaMode,
  lifecycle: 'active',
  revision: 3,
  current_episode_id: null,
  allowed_actions: [],
  updated_at: '2026-09-08T02:00:00Z',
});

function renderIn(locale: string, bundle: object, metaMode: string) {
  mockUseScaleEnrollment.mockReturnValue({
    data: enrollment(metaMode),
    problem: undefined,
    isLoading: false,
    refresh: jest.fn(),
  });
  // A fork of the suite's instance (`jest.setup.js`), which has no `scale`.
  const i18n = getI18n().cloneInstance({
    lng: locale,
    fallbackLng: false,
    forkResourceStore: true,
  });
  i18n.addResourceBundle(locale, 'scale', bundle);
  void i18n.changeLanguage(locale);
  return render(
    <I18nextProvider i18n={i18n}>
      <EnrollmentDetail enrollmentId="enr-1" metaMode="customer_sandbox" />
    </I18nextProvider>
  );
}

/** The `<dd>` next to the "Meta mode" term. */
const metaModeValue = (term: string) =>
  screen.getByText(term, { selector: 'dt' }).nextElementSibling as HTMLElement;

const LOCALES = [
  {
    locale: 'en',
    bundle: en,
    term: 'Meta mode',
    copy: {
      customer_sandbox: 'Customer sandbox',
      controlled_mock: 'Controlled mock',
      internal_validation: 'Internal validation',
    } as Record<string, string>,
    unknown: 'Unknown Meta mode',
    language: (text: string) => expect(text).not.toMatch(CJK),
  },
  {
    locale: 'zh-CN',
    bundle: zhCN,
    term: 'Meta 模式',
    copy: {
      customer_sandbox: '客户 Sandbox',
      controlled_mock: '受控 Mock',
      internal_validation: '内部只读验证',
    } as Record<string, string>,
    unknown: '未知的 Meta 模式',
    language: (text: string) => expect(text).toMatch(CJK),
  },
];

describe.each(LOCALES)(
  '$locale',
  ({ locale, bundle, term, copy, unknown, language }) => {
    it('has copy for every Meta mode', () => {
      expect(Object.keys(copy).sort()).toEqual([...SCALE_META_MODES].sort());
    });

    it.each([...SCALE_META_MODES])('words %s', (mode) => {
      renderIn(locale, bundle, mode);

      const value = metaModeValue(term);
      expect(value).toHaveTextContent(new RegExp(`^${copy[mode]}$`));
      language(value.textContent ?? '');
      expect(document.body.textContent).not.toContain(mode);
    });

    it('words a mode it does not know with the generic sentence', () => {
      renderIn(locale, bundle, 'future_mode');

      const value = metaModeValue(term);
      expect(value).toHaveTextContent(new RegExp(`^${unknown}$`));
      language(value.textContent ?? '');
      expect(document.body.textContent).not.toContain('future_mode');
    });
  }
);
