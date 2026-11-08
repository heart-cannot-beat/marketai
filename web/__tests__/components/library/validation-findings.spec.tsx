/**
 * @jest-environment jsdom
 */
import { render, screen, within } from '@testing-library/react';
import { I18nextProvider, getI18n } from 'react-i18next';
import type { ReactElement } from 'react';
import enLibrary from '../../../locales/en/library.json';
import zhLibrary from '../../../locales/zh-CN/library.json';
import { CJK } from '../../locales/locale-files';
import { libraryFindingCodes } from '../../locales/library-finding-codes';

const mockUseLibraryCommandImpact = jest.fn();

jest.mock('@/hooks/library/useLibraryCommand', () => ({
  useLibraryCommand: () => ({
    run: jest.fn(),
    retry: jest.fn(),
    reset: jest.fn(),
    dismiss: jest.fn(),
    isPending: false,
    problem: undefined,
    reconciliation: undefined,
  }),
}));
jest.mock('@/hooks/library/useLibraryCache', () => ({
  useLibraryCache: () => ({ invalidate: jest.fn() }),
}));
jest.mock('@/hooks/library/useLibraryCommandImpact', () => ({
  useLibraryCommandImpact: () => mockUseLibraryCommandImpact(),
}));

import { ValidationPanel } from '@/components/library/draft/ValidationPanel';
import { PublishDialog } from '@/components/library/commands/PublishDialog';
import type { ValidationFinding } from '@/hooks/library/useDraftSession';
import {
  findingText,
  type LibraryFinding,
} from '@/lib/library/validation-findings';

/**
 * Creative Library validation findings read as `library` copy on the draft
 * page and in the publish dialog (#20): an en page shows no Chinese, a zh-CN
 * page shows Chinese, and a code without copy reads as a general sentence
 * plus its path in en and as the server's own sentence in zh-CN.
 */
type Language = 'en' | 'zh-CN';

const SERVER_SENTENCE = '服务端的中文原文';

function i18nFor(language: Language) {
  const i18n = getI18n().cloneInstance({
    lng: language,
    fallbackLng: false,
    forkResourceStore: true,
  });
  i18n.addResourceBundle('en', 'library', enLibrary);
  i18n.addResourceBundle('zh-CN', 'library', zhLibrary);
  void i18n.changeLanguage(language);
  return i18n;
}

function renderIn(language: Language, ui: ReactElement) {
  return render(
    <I18nextProvider i18n={i18nFor(language)}>{ui}</I18nextProvider>
  );
}

const textOf = (language: Language, finding: LibraryFinding) =>
  findingText(
    i18nFor(language).getFixedT(language, 'library') as never,
    language,
    finding
  );

const finding = (code: string, path = '', message = SERVER_SENTENCE) => ({
  code,
  message,
  path,
});

const blocking = (code: string, path = 'p'): ValidationFinding => ({
  ...finding(code, path),
  severity: 'blocking',
});

const panel = (
  findings: ValidationFinding[],
  warnings: ValidationFinding[] = []
) => (
  <ValidationPanel
    validation={{
      blocking: findings,
      warnings,
      revision: 1,
      contentHash: 'h1',
      acknowledged: new Set(),
    }}
    onAcknowledge={jest.fn()}
  />
);

/** Every code the server gives, with each template filled in. */
const server = libraryFindingCodes();
const everyCode = [
  ...server.codes,
  ...server.templates.map((prefix) => `${prefix}width`),
];

describe('findingText', () => {
  it('reads a code by its copy in each language', () => {
    expect(textOf('en', finding('fact_currency'))).toBe(
      enLibrary['validation-findings'].fact_currency
    );
    expect(textOf('zh-CN', finding('fact_currency'))).toBe(
      zhLibrary['validation-findings'].fact_currency
    );
  });

  it('reads a code with a dot from its nested copy', () => {
    expect(textOf('en', finding('brand_policy.invalid_claim_switches'))).toBe(
      enLibrary['validation-findings'].brand_policy.invalid_claim_switches
    );
  });

  it('fills an f-string code into the longest template it extends', () => {
    const en = textOf('en', finding('reference_evidence_missing_width'));
    expect(en).toBe(
      enLibrary['validation-findings']['reference_evidence_missing_*'].replace(
        '{{field}}',
        'width'
      )
    );
    expect(en).toContain('width');
    expect(textOf('zh-CN', finding('brand_policy.missing_style_guide'))).toBe(
      zhLibrary['validation-findings'].brand_policy['missing_*'].replace(
        '{{field}}',
        'style_guide'
      )
    );
  });

  it('prefers a code of its own over a template it happens to extend', () => {
    expect(textOf('en', finding('reference_derivative_evidence'))).toBe(
      enLibrary['validation-findings'].reference_derivative_evidence
    );
  });

  describe('a code without copy', () => {
    it('reads as a general sentence in en, never the Chinese sentence', () => {
      expect(textOf('en', finding('fact_future_rule'))).toBe(
        enLibrary['validation-findings-default']
      );
    });

    it("reads as the server's own sentence in zh-CN", () => {
      expect(textOf('zh-CN', finding('fact_future_rule'))).toBe(
        SERVER_SENTENCE
      );
    });

    it('reads as the general sentence in zh-CN when the server gave none', () => {
      expect(textOf('zh-CN', finding('fact_future_rule', '', ' '))).toBe(
        zhLibrary['validation-findings-default']
      );
    });

    it('covers a group name, a bare prefix and a code that is no key', () => {
      for (const code of [
        'brand_policy',
        'reference_evidence_',
        'library:draft.save',
        '',
      ]) {
        expect([code, textOf('en', finding(code))]).toEqual([
          code,
          enLibrary['validation-findings-default'],
        ]);
      }
    });
  });
});

describe('draft validation panel', () => {
  it('shows every server code without Chinese in en', () => {
    const { container } = renderIn(
      'en',
      panel(everyCode.map((code) => blocking(code)))
    );
    expect(screen.getAllByRole('listitem')).toHaveLength(everyCode.length);
    expect(container.textContent).not.toMatch(CJK);
    expect(container.textContent).not.toContain(
      enLibrary['validation-findings-default']
    );
  });

  it('shows every server code in its Chinese copy in zh-CN', () => {
    renderIn('zh-CN', panel(everyCode.map((code) => blocking(code))));
    const items = screen.getAllByRole('listitem');
    expect(items).toHaveLength(everyCode.length);
    expect(
      items
        .filter(
          (item) =>
            !CJK.test(item.textContent ?? '') ||
            item.textContent?.includes(SERVER_SENTENCE)
        )
        .map((item) => item.textContent)
    ).toEqual([]);
  });

  it('shows copy and path for blocking findings and warnings', () => {
    renderIn(
      'en',
      panel(
        [blocking('fact_currency', 'facts[3].currency')],
        [
          {
            ...finding('dependency_warning', 'reference_bindings[0]'),
            severity: 'warning',
          },
        ]
      )
    );
    const [first, second] = screen.getAllByRole('listitem');
    expect(first).toHaveTextContent(
      `facts[3].currency: ${enLibrary['validation-findings'].fact_currency}`
    );
    expect(second).toHaveTextContent(
      `reference_bindings[0]: ${enLibrary['validation-findings'].dependency_warning}`
    );
  });

  it('shows a code without copy as a general sentence and its path in en', () => {
    const { container } = renderIn(
      'en',
      panel([blocking('fact_future_rule', 'facts[2].amount')])
    );
    expect(screen.getByRole('listitem')).toHaveTextContent(
      `facts[2].amount: ${enLibrary['validation-findings-default']}`
    );
    expect(container.textContent).not.toMatch(CJK);
  });

  it("shows a code without copy as the server's sentence and its path in zh-CN", () => {
    renderIn('zh-CN', panel([blocking('fact_future_rule', 'facts[2].amount')]));
    expect(screen.getByRole('listitem')).toHaveTextContent(
      `facts[2].amount: ${SERVER_SENTENCE}`
    );
  });
});

describe('publish dialog', () => {
  const impact = (
    blockingFindings: LibraryFinding[],
    warnings: LibraryFinding[]
  ) => ({
    impact: {
      impactId: 'imp-1',
      impactHash: 'h1',
      commandKind: 'publish',
      targetId: 'e1',
      targetRevision: 1,
      contentHash: 'ch1',
      expiry: 9999,
      affectedSummaries: [],
      blocking: blockingFindings,
      warnings,
    },
    isLoading: false,
    problem: undefined,
    isStale: false,
    fetch: jest.fn(),
    clear: jest.fn(),
    confirmed: false,
    confirm: jest.fn(),
    resetConfirmation: jest.fn(),
  });

  const dialog = () => (
    <PublishDialog
      entryId="e1"
      open
      onClose={jest.fn()}
      onPublished={jest.fn()}
    />
  );

  beforeAll(() => {
    // jsdom has no modal <dialog>; an open one is enough here.
    HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
      this.setAttribute('open', '');
    };
  });

  it('shows every server code without Chinese in en', () => {
    mockUseLibraryCommandImpact.mockReturnValue(
      impact(
        everyCode.map((code) => finding(code, 'p')),
        [finding('dependency_warning', 'reference_bindings[0]')]
      )
    );
    const { container } = renderIn('en', dialog());
    expect(container.textContent).not.toMatch(CJK);
    const blocked = within(
      screen.getByText(enLibrary.commands['publish-blocked']).parentElement!
    );
    expect(blocked.getAllByRole('listitem')).toHaveLength(everyCode.length);
    const warned = within(
      screen.getByText(enLibrary.commands['publish-warnings']).parentElement!
    );
    expect(warned.getByRole('listitem')).toHaveTextContent(
      `reference_bindings[0]: ${enLibrary['validation-findings'].dependency_warning}`
    );
  });

  it('shows Chinese copy in zh-CN', () => {
    mockUseLibraryCommandImpact.mockReturnValue(
      impact([finding('fact_currency', 'facts[0].currency')], [])
    );
    renderIn('zh-CN', dialog());
    expect(screen.getByRole('listitem')).toHaveTextContent(
      `facts[0].currency: ${zhLibrary['validation-findings'].fact_currency}`
    );
  });

  it('shows a code without copy as a general sentence and its path in en', () => {
    mockUseLibraryCommandImpact.mockReturnValue(
      impact(
        [finding('fact_future_rule', 'facts[1].amount')],
        [finding('future_warning', '')]
      )
    );
    const { container } = renderIn('en', dialog());
    const [blockingItem, warningItem] = screen.getAllByRole('listitem');
    expect(blockingItem).toHaveTextContent(
      `facts[1].amount: ${enLibrary['validation-findings-default']}`
    );
    expect(warningItem).toHaveTextContent(
      enLibrary['validation-findings-default']
    );
    expect(container.textContent).not.toMatch(CJK);
  });

  it("shows a code without copy as the server's sentence in zh-CN", () => {
    mockUseLibraryCommandImpact.mockReturnValue(
      impact([finding('fact_future_rule', 'facts[1].amount')], [])
    );
    renderIn('zh-CN', dialog());
    expect(screen.getByRole('listitem')).toHaveTextContent(
      `facts[1].amount: ${SERVER_SENTENCE}`
    );
  });
});
