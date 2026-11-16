/**
 * @jest-environment jsdom
 */
import { fireEvent, render, screen } from '@testing-library/react';
import en from '../../../locales/en/library.json';
import zh from '../../../locales/zh-CN/library.json';
import { RightsScopeSelect } from '@/components/library/references/RightsScopeSelect';
import { RIGHTS_SCOPES } from '@/lib/library/rights-scope';

/** The real `library` copy of one locale, looked up by dotted key. */
const mockMessages: { current: unknown } = { current: en };
jest.mock('next-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, string>) => {
      const value = key
        .split('.')
        .reduce<unknown>(
          (node, part) => (node as Record<string, unknown> | undefined)?.[part],
          mockMessages.current
        );
      if (typeof value !== 'string') return key;
      return value.replace(
        /{{\s*(\w+)\s*}}/g,
        (_, name: string) => options?.[name] ?? ''
      );
    },
  }),
}));

const options = () =>
  screen
    .getAllByRole('option')
    .map((option) => [
      (option as HTMLOptionElement).value,
      option.textContent ?? '',
    ]);

beforeEach(() => {
  mockMessages.current = en;
});

/**
 * #182: the rights scope was a free-text box prefilled with
 * `owned_product_photo`, which read like an untranslated key. It is now a
 * choice of known scopes: each option shows a translated label and stores
 * its code, and a scope saved before the list is kept as it was saved.
 */
describe('RightsScopeSelect', () => {
  it('shows each scope by its English label and stores the code', () => {
    const onChange = jest.fn();
    render(
      <RightsScopeSelect value="owned_product_photo" onChange={onChange} />
    );
    const select = screen.getByLabelText('Rights scope');
    expect(select.tagName).toBe('SELECT');
    expect(select).toHaveValue('owned_product_photo');
    expect(select).toHaveDisplayValue('Our own product photo');
    expect(select).toHaveAccessibleDescription(
      en.references['rights-scope-hint']
    );
    expect(screen.queryByRole('textbox')).toBeNull();

    const labels = en.references['rights-scope-options'] as Record<
      string,
      string
    >;
    expect(options()).toEqual(
      RIGHTS_SCOPES.map((code) => [code, labels[code]])
    );
    for (const [code, label] of options()) {
      expect(label).not.toContain(code);
      expect(label).not.toMatch(/_/);
    }

    fireEvent.change(select, { target: { value: 'licensed_stock' } });
    expect(onChange).toHaveBeenCalledWith('licensed_stock');
  });

  it('labels every scope in Chinese on the Chinese interface', () => {
    mockMessages.current = zh;
    render(
      <RightsScopeSelect value="owned_product_photo" onChange={jest.fn()} />
    );
    const select = screen.getByLabelText('授权范围');
    expect(select).toHaveDisplayValue('自有产品照片');
    const labels = zh.references['rights-scope-options'] as Record<
      string,
      string
    >;
    expect(options()).toEqual(
      RIGHTS_SCOPES.map((code) => [code, labels[code]])
    );
  });

  it('keeps a scope saved before the list, shown as it was saved', () => {
    const onChange = jest.fn();
    const saved = 'Photos supplied by the Acme Charge factory';
    render(<RightsScopeSelect value={saved} onChange={onChange} />);
    const select = screen.getByLabelText('Rights scope');
    expect(select).toHaveValue(saved);
    expect(select).toHaveDisplayValue(`${saved} (saved earlier)`);
    expect(options()).toHaveLength(RIGHTS_SCOPES.length + 1);
    expect(options()[RIGHTS_SCOPES.length]).toEqual([
      saved,
      `${saved} (saved earlier)`,
    ]);

    fireEvent.change(select, { target: { value: 'supplier_provided' } });
    expect(onChange).toHaveBeenCalledWith('supplier_provided');
  });

  it('marks a scope saved earlier in Chinese too', () => {
    mockMessages.current = zh;
    render(<RightsScopeSelect value="经销商授权照片" onChange={jest.fn()} />);
    expect(screen.getByLabelText('授权范围')).toHaveDisplayValue(
      '经销商授权照片（之前保存的值）'
    );
  });
});
