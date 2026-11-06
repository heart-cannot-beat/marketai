/**
 * @jest-environment jsdom
 */
import { fireEvent, render, screen, within } from '@testing-library/react';
import {
  ExclusionConfig,
  writeExclusionRules,
} from '@/components/generation/draft/ExclusionConfig';
import type { TemplateDimension } from '@/hooks/generation/useTemplateDimensions';

const DIMENSIONS: TemplateDimension[] = [
  {
    key: 'lib_t:angle',
    templateEntryId: 'lib_t',
    templateName: 'Grid',
    dimensionId: 'angle',
    name: 'Angle',
    values: [
      { id: 'lifestyle', name: 'Lifestyle' },
      { id: 'studio', name: 'Studio' },
    ],
  },
  {
    key: 'lib_t:tone',
    templateEntryId: 'lib_t',
    templateName: 'Grid',
    dimensionId: 'tone',
    name: 'Tone',
    values: [
      { id: 'warm', name: 'Warm' },
      { id: 'cool', name: 'Cool' },
    ],
  },
];

const dimensionSelect = () =>
  document.getElementById('exclusion-dimension') as HTMLSelectElement;
const valueSelect = () =>
  document.getElementById('exclusion-value') as HTMLSelectElement;
const reasonInput = () =>
  document.getElementById('exclusion-reason') as HTMLInputElement;

describe('ExclusionConfig (Issue #157: builds exclusions.rules the server matches)', () => {
  test('a rule is built from dimension/value pairs plus a reason', () => {
    const onUpdate = jest.fn();
    render(
      <ExclusionConfig
        exclusions={{}}
        dimensions={DIMENSIONS}
        onUpdate={onUpdate}
      />
    );

    fireEvent.change(dimensionSelect(), { target: { value: 'lib_t:angle' } });
    fireEvent.change(valueSelect(), { target: { value: 'studio' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add value' }));

    fireEvent.change(dimensionSelect(), { target: { value: 'lib_t:tone' } });
    fireEvent.change(valueSelect(), { target: { value: 'warm' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add value' }));

    const pending = screen.getByRole('list', {
      name: 'Values in the new rule',
    });
    expect(within(pending).getAllByRole('listitem')).toHaveLength(2);
    expect(pending).toHaveTextContent('Angle = Studio');
    expect(pending).toHaveTextContent('Tone = Warm');

    fireEvent.change(reasonInput(), { target: { value: 'clashes' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add exclusion' }));

    expect(onUpdate).toHaveBeenCalledTimes(1);
    expect(onUpdate).toHaveBeenCalledWith({
      rules: [
        {
          dimensions: { 'lib_t:angle': 'studio', 'lib_t:tone': 'warm' },
          reason: 'clashes',
        },
      ],
    });
  });

  test('adding a rule without a reason defaults it to "manual"', () => {
    const onUpdate = jest.fn();
    render(
      <ExclusionConfig
        exclusions={{}}
        dimensions={DIMENSIONS}
        onUpdate={onUpdate}
      />
    );
    fireEvent.change(dimensionSelect(), { target: { value: 'lib_t:tone' } });
    fireEvent.change(valueSelect(), { target: { value: 'cool' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add value' }));
    fireEvent.click(screen.getByRole('button', { name: 'Add exclusion' }));

    expect(onUpdate).toHaveBeenCalledWith({
      rules: [{ dimensions: { 'lib_t:tone': 'cool' }, reason: 'manual' }],
    });
  });

  test('the add button stays disabled until at least one pair is chosen', () => {
    render(
      <ExclusionConfig
        exclusions={{}}
        dimensions={DIMENSIONS}
        onUpdate={jest.fn()}
      />
    );
    expect(
      screen.getByRole('button', { name: 'Add exclusion' })
    ).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Add value' })).toBeDisabled();
  });

  test('existing rules are listed with labels and can be removed; legacy keys are dropped on write', () => {
    const onUpdate = jest.fn();
    render(
      <ExclusionConfig
        exclusions={{
          rules: [
            { dimensions: { 'lib_t:angle': 'lifestyle' }, reason: 'dull' },
            { dimensions: { 'lib_t:tone': 'cool' }, reason: 'off-brand' },
          ],
          manual: ['free text that never worked'],
          invalid: [{ rule: 'x', code: 'legacy_manual_exclusion' }],
        }}
        dimensions={DIMENSIONS}
        onUpdate={onUpdate}
      />
    );

    const list = screen.getByRole('list', { name: 'Exclusion rules' });
    expect(within(list).getAllByRole('listitem')).toHaveLength(2);
    expect(list).toHaveTextContent('Angle = Lifestyle');
    expect(list).toHaveTextContent('dull');

    fireEvent.click(
      screen.getByRole('button', { name: 'Remove Angle = Lifestyle' })
    );
    expect(onUpdate).toHaveBeenCalledWith({
      rules: [{ dimensions: { 'lib_t:tone': 'cool' }, reason: 'off-brand' }],
    });
  });

  test('server-flagged invalid rules are shown read-only with their code', () => {
    render(
      <ExclusionConfig
        exclusions={{
          invalid: [
            {
              rule: { dimensions: { 'ghost:dim': 'x' }, reason: 'typo' },
              code: 'unknown_dimension',
              detail:
                "dimension 'ghost:dim' is not part of this draft's selection",
            },
            { rule: 'A1 with B1', code: 'legacy_manual_exclusion' },
          ],
        }}
        dimensions={DIMENSIONS}
        onUpdate={jest.fn()}
      />
    );

    const note = screen.getByRole('note');
    expect(note).toHaveTextContent('Flagged invalid by the server');
    expect(note).toHaveTextContent('ghost:dim = x');
    expect(note).toHaveTextContent('(unknown_dimension)');
    expect(note).toHaveTextContent('A1 with B1');
    expect(note).toHaveTextContent('(legacy_manual_exclusion)');
    expect(screen.queryByRole('button', { name: /remove/i })).toBeNull();
  });

  test('without template dimensions the builder explains what to do first', () => {
    render(
      <ExclusionConfig exclusions={{}} dimensions={[]} onUpdate={jest.fn()} />
    );
    expect(
      screen.getByText(/select a dimension template first/i)
    ).toBeInTheDocument();
    expect(document.getElementById('exclusion-dimension')).toBeNull();
  });

  test('writeExclusionRules strips server-owned and legacy keys only', () => {
    expect(
      writeExclusionRules(
        { manual: ['x'], invalid: [{ rule: 'x', code: 'c' }], note: 'keep' },
        []
      )
    ).toEqual({ note: 'keep', rules: [] });
  });
});
