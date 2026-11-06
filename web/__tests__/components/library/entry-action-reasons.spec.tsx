/**
 * @jest-environment jsdom
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { I18nextProvider, getI18n } from 'react-i18next';
import { LibraryEntryCommands } from '@/components/library/commands/LibraryEntryCommands';
import type { LibraryEntryType } from '@/lib/library/navigation';
import enLibrary from '../../../locales/en/library.json';
import zhLibrary from '../../../locales/zh-CN/library.json';

/**
 * The entry toolbar says under its buttons why the server refused each one
 * (#399): one line per `reason_code`, naming the buttons it holds back, in
 * the real en and zh-CN copy. A refused button stays focusable and points
 * at its line.
 */
jest.mock('@/components/library/commands/PublishDialog', () => ({
  PublishDialog: () => <div data-testid="publish-dialog" />,
}));
jest.mock('@/components/library/commands/ArchiveDialog', () => ({
  ArchiveDialog: () => <div data-testid="archive-dialog" />,
}));
jest.mock('@/components/library/commands/BindReferenceDialog', () => ({
  BindReferenceDialog: () => <div data-testid="bind-reference-dialog" />,
}));
jest.mock('@/components/library/nested/CreateInternalEntryDialog', () => ({
  CreateInternalEntryDialog: () => null,
}));

const COPY = {
  en: {
    library: enLibrary,
    publish: 'Publish',
    archive: 'Archive',
    bind: 'Bind Reference',
    ownerRequired: 'Archive: Only the Workspace Owner can do this',
    archived: 'Publish, Archive, Bind Reference: This entry is archived',
    notBindable:
      'Bind Reference: Only Product and Person entries can bind references',
    unknown: "Publish: This action isn't available right now",
  },
  'zh-CN': {
    library: zhLibrary,
    publish: '发布',
    archive: '归档',
    bind: '绑定参考素材',
    ownerRequired: '归档：只有工作区 Owner 可以执行这个操作',
    archived: '发布、归档、绑定参考素材：这个条目已归档',
    notBindable: '绑定参考素材：只有产品和人物条目可以绑定参考素材',
    unknown: '发布：暂时不能执行这个操作',
  },
} as const;
type Locale = keyof typeof COPY;

type Eligibility = [boolean, string | null];

/** An entry's `allowed_actions` the way `_entry_allowed_actions` gives them. */
function serverActions(
  publish: Eligibility,
  archive: Eligibility,
  bind: Eligibility
) {
  return [
    { action: 'save_draft', allowed: publish[0], reason_code: publish[1] },
    { action: 'validate_draft', allowed: publish[0], reason_code: publish[1] },
    { action: 'publish', allowed: publish[0], reason_code: publish[1] },
    { action: 'archive', allowed: archive[0], reason_code: archive[1] },
    { action: 'bind_reference', allowed: bind[0], reason_code: bind[1] },
  ];
}

const ALLOWED: Eligibility = [true, null];

function renderIn(
  locale: Locale,
  entryType: LibraryEntryType,
  allowedActions: ReturnType<typeof serverActions>,
  lifecycle = 'active'
) {
  // A fork of the suite's instance (`jest.setup.js`), which has no `library`.
  const i18n = getI18n().cloneInstance({
    lng: locale,
    fallbackLng: false,
    forkResourceStore: true,
  });
  i18n.addResourceBundle(locale, 'library', COPY[locale].library);
  void i18n.changeLanguage(locale);
  return render(
    <I18nextProvider i18n={i18n}>
      <LibraryEntryCommands
        entryId="e1"
        entryType={entryType}
        lifecycle={lifecycle}
        draftRevision={1}
        allowedActions={allowedActions}
        onRefresh={jest.fn()}
        onEntryChange={jest.fn()}
      />
    </I18nextProvider>
  );
}

const reasonLines = () =>
  screen.queryAllByRole('listitem').map((line) => line.textContent);

/** No server code or key name in the text or a tooltip. */
function expectNoRawCodes(container: HTMLElement) {
  const raw = /\b[a-z]+_[a-z_]+\b|commands\./;
  expect(container.textContent).not.toMatch(raw);
  for (const element of Array.from(container.querySelectorAll('[title]')))
    expect(element.getAttribute('title')).not.toMatch(raw);
}

describe.each(Object.keys(COPY) as Locale[])('%s', (locale) => {
  const copy = COPY[locale];
  const button = (name: string) => screen.getByRole('button', { name });

  it('tells a Member why Archive is unavailable', () => {
    const { container } = renderIn(
      locale,
      'product',
      serverActions(ALLOWED, [false, 'owner_required'], ALLOWED)
    );

    expect(reasonLines()).toEqual([copy.ownerRequired]);
    const archive = button(copy.archive);
    expect(archive).toHaveAttribute('aria-disabled', 'true');
    expect(archive).not.toBeDisabled();
    archive.focus();
    expect(archive).toHaveFocus();
    expect(archive).toHaveAccessibleDescription(copy.ownerRequired);
    fireEvent.click(archive);
    expect(screen.queryByTestId('archive-dialog')).toBeNull();

    const publish = button(copy.publish);
    expect(publish).not.toHaveAttribute('aria-disabled');
    expect(publish).not.toHaveAttribute('aria-describedby');
    fireEvent.click(publish);
    expect(screen.getByTestId('publish-dialog')).toBeInTheDocument();
    expectNoRawCodes(container);
  });

  it('gives an archived entry one line for its three buttons', () => {
    const archived: Eligibility = [false, 'entry_archived'];
    const { container } = renderIn(
      locale,
      'product',
      serverActions(archived, archived, archived),
      'archived'
    );

    expect(reasonLines()).toEqual([copy.archived]);
    for (const name of [copy.publish, copy.archive, copy.bind]) {
      expect(button(name)).toHaveAttribute('aria-disabled', 'true');
      expect(button(name)).toHaveAccessibleDescription(copy.archived);
      fireEvent.click(button(name));
    }
    expect(screen.queryByTestId('publish-dialog')).toBeNull();
    expect(screen.queryByTestId('archive-dialog')).toBeNull();
    expect(screen.queryByTestId('bind-reference-dialog')).toBeNull();
    expectNoRawCodes(container);
  });

  it('keeps Bind Reference on an Offer and says only Products and People bind', () => {
    const { container } = renderIn(
      locale,
      'offer',
      serverActions(
        ALLOWED,
        [false, 'owner_required'],
        [false, 'entry_type_not_bindable']
      )
    );

    expect(reasonLines()).toEqual([copy.ownerRequired, copy.notBindable]);
    const bind = button(copy.bind);
    expect(bind).toHaveAttribute('aria-disabled', 'true');
    expect(bind).toHaveAccessibleDescription(copy.notBindable);
    fireEvent.click(bind);
    expect(screen.queryByTestId('bind-reference-dialog')).toBeNull();
    expectNoRawCodes(container);
  });

  it('shows an Owner no reasons when every action is allowed', () => {
    const { container } = renderIn(
      locale,
      'product',
      serverActions(ALLOWED, ALLOWED, ALLOWED)
    );

    expect(reasonLines()).toEqual([]);
    for (const name of [copy.publish, copy.archive, copy.bind]) {
      expect(button(name)).not.toHaveAttribute('aria-disabled');
      expect(button(name)).not.toHaveAttribute('aria-describedby');
    }
    expectNoRawCodes(container);
  });

  it('words a reason without copy as a generic sentence', () => {
    const { container } = renderIn(
      locale,
      'product',
      serverActions([false, 'future_reason'], ALLOWED, ALLOWED)
    );

    expect(reasonLines()).toEqual([copy.unknown]);
    expect(button(copy.publish)).toHaveAccessibleDescription(copy.unknown);
    expectNoRawCodes(container);
  });
});
