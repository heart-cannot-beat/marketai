/**
 * @jest-environment jsdom
 */
import { fireEvent, render, screen } from '@testing-library/react';
import {
  CombinationStrategy,
  readCombinationStrategy,
  writeCombinationStrategy,
} from '@/components/generation/draft/CombinationStrategy';

const capInput = () =>
  document.getElementById('sample-cap-input') as HTMLInputElement;

describe('CombinationStrategy (Issue #157: writes the shape the server reads)', () => {
  test('switching to coverage writes { kind: "coverage", cap } with the default cap', () => {
    const onUpdate = jest.fn();
    render(
      <CombinationStrategy strategy={{ kind: 'full' }} onUpdate={onUpdate} />
    );

    fireEvent.click(screen.getByRole('button', { name: 'Coverage sample' }));
    expect(onUpdate).toHaveBeenCalledWith({ kind: 'coverage', cap: 12 });
  });

  test('switching back to full drops the cap', () => {
    const onUpdate = jest.fn();
    render(
      <CombinationStrategy
        strategy={{ kind: 'coverage', cap: 8 }}
        onUpdate={onUpdate}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: 'Full' }));
    expect(onUpdate).toHaveBeenCalledWith({ kind: 'full' });
  });

  test('the cap commits on Enter, not per keystroke', () => {
    const onUpdate = jest.fn();
    render(
      <CombinationStrategy
        strategy={{ kind: 'coverage', cap: 10 }}
        onUpdate={onUpdate}
      />
    );

    const input = capInput();
    expect(input.value).toBe('10');
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: '1' } });
    fireEvent.change(input, { target: { value: '12' } });
    expect(onUpdate).not.toHaveBeenCalled();

    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onUpdate).toHaveBeenCalledTimes(1);
    expect(onUpdate).toHaveBeenCalledWith({ kind: 'coverage', cap: 12 });
  });

  test('legacy { mode: "sampled", sample_cap } is displayed as coverage and rewritten on change', () => {
    const onUpdate = jest.fn();
    render(
      <CombinationStrategy
        strategy={{ mode: 'sampled', sample_cap: 7, planner_version: 'v1' }}
        onUpdate={onUpdate}
      />
    );

    expect(
      screen.getByRole('button', { name: 'Coverage sample' })
    ).toHaveAttribute('aria-pressed', 'true');
    expect(capInput().value).toBe('7');
    expect(screen.getByRole('note')).toHaveTextContent(/old strategy format/i);

    const input = capInput();
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: '9' } });
    fireEvent.blur(input);
    // Legacy keys are gone, unrelated keys survive.
    expect(onUpdate).toHaveBeenCalledWith({
      kind: 'coverage',
      cap: 9,
      planner_version: 'v1',
    });
  });

  test('an unknown kind renders as full without throwing', () => {
    render(
      <CombinationStrategy strategy={{ kind: 'weird' }} onUpdate={jest.fn()} />
    );
    expect(screen.getByRole('button', { name: 'Full' })).toHaveAttribute(
      'aria-pressed',
      'true'
    );
  });
});

describe('readCombinationStrategy / writeCombinationStrategy', () => {
  test('reads canonical, legacy and empty shapes', () => {
    expect(readCombinationStrategy({})).toEqual({
      kind: 'full',
      cap: null,
      legacy: false,
    });
    expect(readCombinationStrategy({ kind: 'coverage', cap: 5 })).toEqual({
      kind: 'coverage',
      cap: 5,
      legacy: false,
    });
    expect(readCombinationStrategy({ mode: 'full', sample_cap: 3 })).toEqual({
      kind: 'full',
      cap: null,
      legacy: true,
    });
  });

  test('writes the canonical shape and keeps foreign keys', () => {
    expect(
      writeCombinationStrategy(
        { mode: 'sampled', sample_cap: 4, batch_draft_ids: ['gd_1'] },
        'coverage',
        null
      )
    ).toEqual({ kind: 'coverage', cap: 12, batch_draft_ids: ['gd_1'] });
    expect(
      writeCombinationStrategy({ kind: 'coverage', cap: 4 }, 'full', null)
    ).toEqual({ kind: 'full' });
  });
});
