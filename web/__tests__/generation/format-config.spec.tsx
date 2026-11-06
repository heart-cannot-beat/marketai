/**
 * @jest-environment jsdom
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { FormatConfig } from '@/components/generation/draft/FormatConfig';

jest.mock('next-i18next', () => ({
  useTranslation: (ns: string) => ({
    t: (key: string) => `${ns}:${key}`,
  }),
}));

const resolutionInput = () =>
  document.getElementById('format-resolution') as HTMLInputElement;
const variantInput = () =>
  document.getElementById('format-variant-count') as HTMLInputElement;

describe('FormatConfig', () => {
  test('typing does not save on every keystroke; Enter saves once', () => {
    const onUpdate = jest.fn();
    render(
      <FormatConfig format={{ aspect_ratio: '1:1' }} onUpdate={onUpdate} />
    );

    const input = resolutionInput();
    fireEvent.focus(input);
    for (const partial of ['1', '10', '102', '1024', '1024x1024']) {
      fireEvent.change(input, { target: { value: partial } });
    }
    expect(onUpdate).not.toHaveBeenCalled();
    expect(input.value).toBe('1024x1024');

    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onUpdate).toHaveBeenCalledTimes(1);
    expect(onUpdate).toHaveBeenCalledWith({
      aspect_ratio: '1:1',
      resolution: '1024x1024',
    });
  });

  test('blur saves the edited value, and only when it changed', () => {
    const onUpdate = jest.fn();
    render(
      <FormatConfig
        format={{ aspect_ratio: '1:1', resolution: '1024x1024' }}
        onUpdate={onUpdate}
      />
    );

    const input = resolutionInput();
    fireEvent.focus(input);
    fireEvent.blur(input);
    expect(onUpdate).not.toHaveBeenCalled();

    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: ' 1536x1024 ' } });
    fireEvent.blur(input);
    expect(onUpdate).toHaveBeenCalledWith({
      aspect_ratio: '1:1',
      resolution: '1536x1024',
    });
  });

  test('a saved value arriving mid-edit does not overwrite what is being typed', () => {
    const onUpdate = jest.fn();
    const { rerender } = render(
      <FormatConfig format={{ resolution: '' }} onUpdate={onUpdate} />
    );

    const input = resolutionInput();
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: '1024x' } });
    // e.g. another field's save refreshing the draft while this one is focused
    rerender(<FormatConfig format={{ resolution: '0' }} onUpdate={onUpdate} />);
    expect(input.value).toBe('1024x');

    fireEvent.change(input, { target: { value: '1024x1024' } });
    fireEvent.blur(input);
    expect(onUpdate).toHaveBeenLastCalledWith({ resolution: '1024x1024' });

    // Not editing: the saved value is shown.
    rerender(
      <FormatConfig format={{ resolution: '1024x1536' }} onUpdate={onUpdate} />
    );
    expect(input.value).toBe('1024x1536');
  });

  test('variant count commits a positive integer, and clears on empty', () => {
    const onUpdate = jest.fn();
    render(
      <FormatConfig
        format={{ variants_per_combination: 2 }}
        onUpdate={onUpdate}
      />
    );

    const input = variantInput();
    expect(input.value).toBe('2');
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: '4' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onUpdate).toHaveBeenLastCalledWith({ variants_per_combination: 4 });

    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: '' } });
    fireEvent.blur(input);
    expect(onUpdate).toHaveBeenLastCalledWith({
      variants_per_combination: undefined,
    });
  });

  test('variant count is clamped to the model limit and the limit is shown', () => {
    const onUpdate = jest.fn();
    render(
      <FormatConfig
        format={{ variants_per_combination: 2 }}
        onUpdate={onUpdate}
        maxVariantsPerCombination={4}
      />
    );

    const input = variantInput();
    expect(input).toHaveAttribute('max', '4');
    expect(
      screen.getByText('common:generation-draft-format-variant-count-max')
    ).toBeInTheDocument();

    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: '9' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onUpdate).toHaveBeenLastCalledWith({ variants_per_combination: 4 });

    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: '3' } });
    fireEvent.blur(input);
    expect(onUpdate).toHaveBeenLastCalledWith({ variants_per_combination: 3 });
  });

  test('without a known limit the input carries no max and nothing is clamped', () => {
    const onUpdate = jest.fn();
    render(<FormatConfig format={{}} onUpdate={onUpdate} />);
    const input = variantInput();
    expect(input).not.toHaveAttribute('max');
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: '9' } });
    fireEvent.blur(input);
    expect(onUpdate).toHaveBeenLastCalledWith({ variants_per_combination: 9 });
  });

  test('aspect ratio still saves on change', () => {
    const onUpdate = jest.fn();
    render(<FormatConfig format={{}} onUpdate={onUpdate} />);
    fireEvent.change(screen.getByRole('combobox'), {
      target: { value: '16:9' },
    });
    expect(onUpdate).toHaveBeenCalledWith({ aspect_ratio: '16:9' });
  });
});
