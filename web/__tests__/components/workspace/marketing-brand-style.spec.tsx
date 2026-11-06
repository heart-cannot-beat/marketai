/** @jest-environment jsdom */
import { fireEvent, render, screen } from '@testing-library/react';
import {
  MarketingBrandStyle,
  styleBody,
  withStyleVariation,
} from '@/components/workspace/MarketingBrandStyle';

let mockKits: any;
jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: () => ({ data: mockKits }),
}));

function kit(id: string, version: number, palette = ['#0A3D62']) {
  return {
    id,
    product_id: 'prod_1',
    version,
    content_hash: String(version).repeat(64),
    created_at: '2026-10-24T00:00:00Z',
    logo_available: true,
    content: {
      logo: null,
      fonts: { primary: 'Inter', secondary: 'Source Serif' },
      palette,
      readability_rules: {
        min_text_height_ratio: 0.03,
        min_contrast_ratio: 4.5,
        safe_area_ratio: 0.05,
      },
      forbidden: ['Unverified UL certification'],
    },
  };
}

const plan: any = {
  strategy_language: 'en-US',
  changed: ['Buying proposition and expression'],
  held_constant: ['Same product and offer', 'Visual style'],
  style_variation: 'held',
  style_notes: 'Clean daylight product photography',
  brand_kit: null,
  groups: [
    {
      id: 'g1',
      name: 'Understand value',
      combination_ids: ['c1'],
      rationale: 'r',
      style: '',
    },
    {
      id: 'g2',
      name: 'Address concerns',
      combination_ids: ['c2'],
      rationale: 'r',
      style: '',
    },
  ],
};

beforeEach(() => {
  mockKits = {
    items: [
      {
        product_id: 'prod_1',
        product_name: 'Acme Charge adapter',
        current: kit('bk_2', 2, ['#FF0000']),
      },
    ],
  };
});

it('moves visual style between held and changed in either strategy language', () => {
  const tested = withStyleVariation(plan, 'tested');
  expect(tested.changed).toEqual([
    'Buying proposition and expression',
    'Visual style',
  ]);
  expect(tested.held_constant).toEqual(['Same product and offer']);
  const back = withStyleVariation(
    { ...tested, groups: tested.groups.map((g) => ({ ...g, style: 'x' })) },
    'held'
  );
  expect(back.held_constant).toEqual([
    'Same product and offer',
    'Visual style',
  ]);
  expect(back.changed).toEqual(['Buying proposition and expression']);
  expect(back.groups.every((g) => g.style === '')).toBe(true);
  const zh = withStyleVariation(
    {
      ...plan,
      strategy_language: 'zh-CN',
      held_constant: ['同一产品', '视觉风格'],
    },
    'tested'
  );
  expect(zh.changed).toContain('视觉风格');
  expect(zh.held_constant).toEqual(['同一产品']);
});

it('records the held default for plans drafted before the baseline', () => {
  const legacy = {
    ...plan,
    style_variation: undefined,
    style_notes: undefined,
    groups: plan.groups.map((g: any) => {
      const legacyGroup = { ...g };
      delete legacyGroup.style;
      return legacyGroup;
    }),
  };
  expect(styleBody(legacy)).toEqual({
    style_variation: 'held',
    style_notes: '',
    groups: plan.groups,
  });
});

it('chooses an exact brand version and flags a newer one without moving the plan', () => {
  const onChange = jest.fn();
  const frozen = kit('bk_1', 1);
  const { rerender } = render(
    <MarketingBrandStyle
      plan={plan}
      frozen={null}
      onChange={onChange}
      fieldMessages={() => []}
      disabled={false}
    />
  );
  fireEvent.change(screen.getByLabelText('Brand baseline'), {
    target: { value: 'bk_2' },
  });
  expect(onChange).toHaveBeenCalledWith(
    expect.objectContaining({
      brand_kit: {
        id: 'bk_2',
        product_id: 'prod_1',
        version: 2,
        content_hash: '2'.repeat(64),
      },
    })
  );
  const referenced = {
    ...plan,
    brand_kit: {
      id: 'bk_1',
      product_id: 'prod_1',
      version: 1,
      content_hash: '1'.repeat(64),
    },
  };
  rerender(
    <MarketingBrandStyle
      plan={referenced}
      frozen={frozen as any}
      onChange={onChange}
      fieldMessages={() => []}
      disabled={false}
    />
  );
  expect(screen.getByText('#0A3D62')).toBeInTheDocument();
  expect(screen.getByText('Fonts: Inter / Source Serif')).toBeInTheDocument();
  expect(
    screen.getByText('Never show or claim: Unverified UL certification')
  ).toBeInTheDocument();
  expect(
    screen.getByText(/Version 2 of this product's brand baseline is newer/)
  ).toBeInTheDocument();
});

it('asks for each group style only when style is tested and shows server field errors', () => {
  const onChange = jest.fn();
  const tested = withStyleVariation(plan, 'tested');
  render(
    <MarketingBrandStyle
      plan={tested}
      frozen={null}
      onChange={onChange}
      fieldMessages={(path) =>
        path === 'plan.groups.1.style' ? ['Describe each group style.'] : []
      }
      disabled={false}
    />
  );
  expect(
    screen.getByLabelText(
      'Style for Understand value (English, sent to the image model)'
    )
  ).toBeInTheDocument();
  const second = screen.getByLabelText(
    'Style for Address concerns (English, sent to the image model)'
  );
  expect(second).toHaveAttribute('aria-invalid', 'true');
  expect(screen.getByText('Describe each group style.')).toBeInTheDocument();
  fireEvent.click(
    screen.getByLabelText('Held constant: every group uses one shared style')
  );
  expect(onChange.mock.calls[0][0].held_constant).toContain('Visual style');
});

it('offers the style fix after a refusal and names the hidden group styles it clears (#354)', () => {
  const onChange = jest.fn();
  // A live draft: style held, but reworded in the held list and written per group.
  const drafted = {
    ...plan,
    changed: ['Angles', 'Visual style per group'],
    held_constant: ['Same product and offer', 'style variation held'],
    groups: plan.groups.map((g: any) => ({ ...g, style: 'held' })),
  };
  const { rerender } = render(
    <MarketingBrandStyle
      plan={drafted}
      frozen={null}
      onChange={onChange}
      fieldMessages={() => []}
      errorCodes={['group_style_not_held', 'group_style_not_held']}
      disabled={false}
    />
  );
  expect(
    screen.getByText(/do not match "Held constant" yet/)
  ).toBeInTheDocument();
  expect(
    screen.getByText('These group-specific styles will be cleared:')
  ).toBeInTheDocument();
  expect(screen.getByText('Understand value: held')).toHaveAttribute(
    'data-validation-field',
    'plan.groups.0.style'
  );
  expect(screen.getByText('Address concerns: held')).toHaveAttribute(
    'data-validation-field',
    'plan.groups.1.style'
  );
  fireEvent.click(
    screen.getByRole('button', { name: 'Fix for the current style choice' })
  );
  const fixed = onChange.mock.calls[0][0];
  expect(fixed.changed).toEqual(['Angles']);
  expect(fixed.held_constant).toEqual([
    'Same product and offer',
    'style variation held',
    'Visual style',
  ]);
  expect(fixed.groups.map((g: any) => g.style)).toEqual(['', '']);
  expect(fixed.style_variation).toBe('held');

  // Unrelated refusals and a refused tested plan without group styles to clear.
  rerender(
    <MarketingBrandStyle
      plan={drafted}
      frozen={null}
      onChange={onChange}
      fieldMessages={() => []}
      errorCodes={['group_style_required', 'expressions_not_distinct']}
      disabled={false}
    />
  );
  expect(
    screen.queryByRole('button', { name: 'Fix for the current style choice' })
  ).not.toBeInTheDocument();
});

it('fixes a tested plan without touching its group styles', () => {
  const onChange = jest.fn();
  const tested = {
    ...plan,
    style_variation: 'tested',
    groups: plan.groups.map((g: any, i: number) => ({
      ...g,
      style: i ? '' : 'Bright lifestyle photo',
    })),
  };
  render(
    <MarketingBrandStyle
      plan={tested}
      frozen={null}
      onChange={onChange}
      fieldMessages={() => []}
      errorCodes={['style_variation_undeclared']}
      disabled={false}
    />
  );
  expect(screen.getByText(/do not match "Tested" yet/)).toBeInTheDocument();
  expect(
    screen.queryByText('These group-specific styles will be cleared:')
  ).not.toBeInTheDocument();
  fireEvent.click(
    screen.getByRole('button', { name: 'Fix for the current style choice' })
  );
  const fixed = onChange.mock.calls[0][0];
  expect(fixed.changed).toEqual([
    'Buying proposition and expression',
    'Visual style',
  ]);
  expect(fixed.held_constant).toEqual(['Same product and offer']);
  expect(fixed.groups.map((g: any) => g.style)).toEqual([
    'Bright lifestyle photo',
    '',
  ]);
});
