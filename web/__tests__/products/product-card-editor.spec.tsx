/**
 * @jest-environment jsdom
 */
import { fireEvent, render, screen, within } from '@testing-library/react';
import { ProductCardEditor } from '@/components/products/ProductCardEditor';
import type { IntakeCard } from '@/lib/products/intake-model';

jest.mock('@/hooks/products/useIntakeImageUrl', () => ({
  useIntakeImageUrl: () => ({ blobUrl: null, isLoading: false, failed: false }),
}));

const CARD: IntakeCard = {
  name: 'VoltNest 壁挂充电桩',
  description: '家用 11 kW 交流充电桩',
  specs: { 功率: '11 kW' },
  model_variants: [
    { label: '7 kW', specs: { 功率: '7 kW' } },
    { label: '11 kW', specs: { 功率: '11 kW' } },
  ],
  requires_variant_selection: true,
  selling_points: ['充电快三倍', 'App 预约'],
  audiences: [
    { who: '新车主', why: '家里有车位', pain: '公共桩排队' },
    { who: '别墅业主', why: '多车家庭', pain: '' },
  ],
  must_match: [
    {
      id: 'mm_1',
      type: 'color',
      description: '机身白色',
      severity: 'blocking',
      proposed_by: 'ai',
      confirmed: false,
    },
  ],
  images: [
    { image_id: 'img_a', rank: 1, source: 'product_page', selectable: true },
    { image_id: 'img_b', rank: 2, source: 'product_page', selectable: true },
    { image_id: 'img_c', rank: 3, source: 'product_page', selectable: false },
  ],
  default_image_ids: ['img_a', 'img_b'],
  dropped_commercial_specs: ['价格'],
};

function renderEditor(
  overrides: Partial<Parameters<typeof ProductCardEditor>[0]> = {}
) {
  const onConfirm = jest.fn();
  render(
    <ProductCardEditor
      intakeId="pi_1"
      card={CARD}
      executionMode="mock"
      confirming={false}
      confirmProblem={null}
      onConfirm={onConfirm}
      onStartOver={jest.fn()}
      {...overrides}
    />
  );
  return onConfirm;
}

describe('ProductCardEditor', () => {
  it('shows the AI draft, the mock badge and the dropped commercial facts', () => {
    renderEditor();
    expect(screen.getByLabelText('Product name')).toHaveValue(
      'VoltNest 壁挂充电桩'
    );
    expect(screen.getByTestId('intake-mock-badge')).toHaveTextContent(
      'Example result, no OpenAI key bound'
    );
    expect(
      screen.getByText(
        /Prices, discounts and other commercial details were left out of the product card \(1 items\)/
      )
    ).toBeInTheDocument();
    expect(
      screen.getByText('Did not pass the image checks')
    ).toBeInTheDocument();
    expect(screen.getByText('AI suggestion')).toBeInTheDocument();
  });

  it('keeps confirm unavailable until a variant is chosen', () => {
    const onConfirm = renderEditor();
    const confirm = screen.getByRole('button', { name: 'Confirm product' });
    expect(confirm).toBeDisabled();
    expect(confirm).toHaveAccessibleDescription(
      'Choose a variant above first.'
    );
    fireEvent.click(confirm);
    expect(onConfirm).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('radio', { name: '7 kW' }));
    expect(
      screen.getByRole('button', { name: 'Confirm product' })
    ).toBeEnabled();
    expect(
      screen.queryByText('Choose a variant above first.')
    ).not.toBeInTheDocument();
  });

  it('puts "Choose a variant" first on the card', () => {
    renderEditor();
    const headings = screen
      .getAllByRole('heading', { level: 3 })
      .map((heading) => heading.textContent);
    expect(headings[0]).toBe('Choose a variant');
    expect(headings[1]).toBe('Product images');
  });

  it('turns every edit into the confirm payload', () => {
    const onConfirm = renderEditor();

    fireEvent.click(screen.getByRole('radio', { name: '11 kW' }));
    fireEvent.change(screen.getByLabelText('Product name'), {
      target: { value: 'VoltNest Pro' },
    });
    // Specs: edit, add and remove rows.
    fireEvent.change(screen.getByLabelText('Value 1'), {
      target: { value: '11 千瓦' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add a spec' }));
    fireEvent.change(screen.getByLabelText('Spec 2'), {
      target: { value: '防护等级' },
    });
    fireEvent.change(screen.getByLabelText('Value 2'), {
      target: { value: 'IP65' },
    });
    // Selling points: remove one, add one.
    fireEvent.click(
      screen.getByRole('button', { name: 'Remove selling point 2' })
    );
    fireEvent.click(
      screen.getByRole('button', { name: 'Add a selling point' })
    );
    fireEvent.change(screen.getByLabelText('Selling point 2'), {
      target: { value: '静音运行' },
    });
    // Audiences: make the second primary, edit it, add a third.
    fireEvent.click(
      screen.getByRole('radio', {
        name: 'Main customer group (Customer group 2)',
      })
    );
    fireEvent.change(screen.getByLabelText('Pain point (Customer group 2)'), {
      target: { value: '车多桩少' },
    });
    fireEvent.click(
      screen.getByRole('button', { name: 'Add a customer group' })
    );
    fireEvent.change(screen.getByLabelText('Who (Customer group 3)'), {
      target: { value: '网约车司机' },
    });
    // Must-match: soften the AI item, add one of our own.
    const firstItem = screen.getAllByTestId('must-match-item')[0];
    fireEvent.click(
      within(firstItem).getByRole('radio', { name: 'Match where possible' })
    );
    fireEvent.click(screen.getByRole('button', { name: 'Add a requirement' }));
    fireEvent.change(screen.getByLabelText('Type 2'), {
      target: { value: 'logo' },
    });
    fireEvent.change(screen.getByLabelText('Requirement 2'), {
      target: { value: 'Logo 在正面居中' },
    });
    // Images: second image becomes the main one.
    fireEvent.click(
      screen.getByRole('radio', {
        name: 'Set as main image (Candidate image 2)',
      })
    );

    fireEvent.click(screen.getByRole('button', { name: 'Confirm product' }));

    expect(onConfirm).toHaveBeenCalledWith({
      card: {
        name: 'VoltNest Pro',
        description: '家用 11 kW 交流充电桩',
        specs: { 功率: '11 千瓦', 防护等级: 'IP65' },
        selling_points: ['充电快三倍', '静音运行'],
        audiences: [
          { who: '新车主', why: '家里有车位', pain: '公共桩排队' },
          { who: '别墅业主', why: '多车家庭', pain: '车多桩少' },
          { who: '网约车司机', why: '', pain: '' },
        ],
        primary_audience_index: 1,
        must_match: [
          {
            id: 'mm_1',
            type: 'color',
            description: '机身白色',
            severity: 'warning',
          },
          {
            type: 'logo',
            description: 'Logo 在正面居中',
            severity: 'blocking',
          },
        ],
      },
      selected_variant: '11 kW',
      image_ids: ['img_b', 'img_a'],
    });
  });

  it('can drop an image and every must-match item', () => {
    const onConfirm = renderEditor();
    fireEvent.click(screen.getByRole('radio', { name: '7 kW' }));
    fireEvent.click(
      screen.getByRole('checkbox', {
        name: 'Use this image (Candidate image 1)',
      })
    );
    fireEvent.click(
      screen.getByRole('button', { name: 'Remove requirement 1' })
    );
    fireEvent.click(
      screen.getByRole('button', { name: 'Remove customer group 1' })
    );
    fireEvent.click(screen.getByRole('button', { name: 'Confirm product' }));

    const body = onConfirm.mock.calls[0][0];
    expect(body.image_ids).toEqual(['img_b']);
    expect(body.card.must_match).toEqual([]);
    expect(body.card.audiences).toEqual([
      { who: '别墅业主', why: '多车家庭', pain: '' },
    ]);
    expect(body.card.primary_audience_index).toBe(0);
  });

  it('shows a refused confirm and the saving state', () => {
    renderEditor({ confirming: true, confirmProblem: 'Choose a variant.' });
    expect(screen.getByRole('alert')).toHaveTextContent('Choose a variant.');
    expect(screen.getByRole('button', { name: 'Saving' })).toBeDisabled();
  });
});

/** Issue #192: a page that sells one model in three colours. */
const COLOUR_CARD: IntakeCard = {
  name: 'Grizzl-E Classic',
  description: '家用 40 A 充电桩',
  specs: { 外壳: '铝合金外壳', 功率: '9.6 kW' },
  model_variants: [
    {
      label: 'Classic Black',
      specs: { 外壳: '哑光黑铝合金外壳' },
      appearance: [
        {
          id: 'mm_black',
          type: 'color',
          description: '外壳为哑光黑色，正面蓝色爪印 logo',
          severity: 'blocking',
          proposed_by: 'ai',
          confirmed: false,
        },
      ],
      replaces_must_match_ids: ['mm_colours'],
      image_ids: ['img_2'],
      default_image_ids: ['img_1', 'img_2'],
    },
    {
      label: 'Avalanche White',
      specs: { 外壳: '亮面白铝合金外壳' },
      appearance: [
        {
          id: 'mm_white',
          type: 'color',
          description: '外壳为亮面白色，正面灰色爪印 logo',
          severity: 'blocking',
          proposed_by: 'ai',
          confirmed: false,
        },
      ],
      replaces_must_match_ids: ['mm_colours'],
      image_ids: ['img_3'],
      default_image_ids: ['img_1', 'img_3'],
    },
    {
      label: 'Camo',
      specs: {},
      appearance: [],
      replaces_must_match_ids: [],
      image_ids: ['img_4'],
      default_image_ids: ['img_1', 'img_4'],
    },
  ],
  requires_variant_selection: true,
  selling_points: ['一晚充满'],
  audiences: [{ who: '新车主', why: '家里有车位', pain: '' }],
  must_match: [
    {
      id: 'mm_colours',
      type: 'color',
      description: '可选配色：Classic Black、Avalanche White、Camo',
      severity: 'blocking',
      proposed_by: 'ai',
      confirmed: false,
    },
    {
      id: 'mm_logo',
      type: 'logo',
      description: '正面居中的爪印 logo',
      severity: 'blocking',
      proposed_by: 'ai',
      confirmed: false,
    },
  ],
  images: [
    {
      image_id: 'img_1',
      rank: 1,
      source: 'product_page',
      selectable: true,
      variant_label: null,
    },
    {
      image_id: 'img_2',
      rank: 2,
      source: 'product_page',
      selectable: true,
      variant_label: 'Classic Black',
    },
    {
      image_id: 'img_3',
      rank: 3,
      source: 'product_page',
      selectable: true,
      variant_label: 'Avalanche White',
    },
    {
      image_id: 'img_4',
      rank: 4,
      source: 'product_page',
      selectable: true,
      variant_label: 'Camo',
    },
  ],
  default_image_ids: ['img_1', 'img_2', 'img_3', 'img_4'],
};

function checked(index: number): boolean {
  return (
    screen.getByRole('checkbox', {
      name: `Use this image (Candidate image ${index})`,
    }) as HTMLInputElement
  ).checked;
}

function mustMatchTexts(): string[] {
  return screen
    .getAllByTestId('must-match-item')
    .map(
      (item) => (within(item).getByRole('textbox') as HTMLInputElement).value
    );
}

describe('ProductCardEditor — choosing a colour edition', () => {
  it('shows each edition with what differs and tags images by edition', () => {
    renderEditor({ card: COLOUR_CARD });
    const choices = screen.getAllByTestId('variant-choice');
    expect(choices).toHaveLength(3);
    expect(choices[0]).toHaveTextContent('Classic Black');
    expect(choices[0]).toHaveTextContent(
      '外壳: 哑光黑铝合金外壳; 外壳为哑光黑色，正面蓝色爪印 logo'
    );
    expect(
      screen
        .getAllByTestId('intake-image-variant')
        .map((tag) => tag.textContent)
    ).toEqual(['Shared', 'Classic Black', 'Avalanche White', 'Camo']);
    expect(
      screen.getByRole('button', { name: 'Confirm product' })
    ).toBeDisabled();
  });

  it('preselects the edition’s images and rewrites the colour item per edition', () => {
    const onConfirm = renderEditor({ card: COLOUR_CARD });

    fireEvent.click(screen.getByRole('radio', { name: 'Avalanche White' }));
    expect([1, 2, 3, 4].map(checked)).toEqual([true, false, true, false]);
    expect(mustMatchTexts()).toEqual([
      '正面居中的爪印 logo',
      '外壳为亮面白色，正面灰色爪印 logo',
    ]);
    expect(screen.getByLabelText('Value 1')).toHaveValue('亮面白铝合金外壳');

    // Switching editions swaps images, colour item and specs again.
    fireEvent.click(screen.getByRole('radio', { name: 'Classic Black' }));
    expect([1, 2, 3, 4].map(checked)).toEqual([true, true, false, false]);
    expect(mustMatchTexts()).toEqual([
      '正面居中的爪印 logo',
      '外壳为哑光黑色，正面蓝色爪印 logo',
    ]);
    expect(screen.getByLabelText('Value 1')).toHaveValue('哑光黑铝合金外壳');

    // An edition without its own colour gets the shared colour line back.
    fireEvent.click(screen.getByRole('radio', { name: 'Camo' }));
    expect([1, 2, 3, 4].map(checked)).toEqual([true, false, false, true]);
    expect(mustMatchTexts()).toEqual([
      '可选配色：Classic Black、Avalanche White、Camo',
      '正面居中的爪印 logo',
    ]);
    expect(screen.getByLabelText('Value 1')).toHaveValue('铝合金外壳');

    // Back to white; another edition's image can still be ticked back on.
    fireEvent.click(screen.getByRole('radio', { name: 'Avalanche White' }));
    fireEvent.click(
      screen.getByRole('checkbox', {
        name: 'Use this image (Candidate image 4)',
      })
    );
    fireEvent.click(screen.getByRole('button', { name: 'Confirm product' }));

    expect(onConfirm).toHaveBeenCalledTimes(1);
    const body = onConfirm.mock.calls[0][0];
    expect(body.selected_variant).toBe('Avalanche White');
    expect(body.image_ids).toEqual(['img_1', 'img_3', 'img_4']);
    expect(body.card.specs).toEqual({
      外壳: '亮面白铝合金外壳',
      功率: '9.6 kW',
    });
    expect(body.card.must_match).toEqual([
      {
        id: 'mm_logo',
        type: 'logo',
        description: '正面居中的爪印 logo',
        severity: 'blocking',
      },
      {
        id: 'mm_white',
        type: 'color',
        description: '外壳为亮面白色，正面灰色爪印 logo',
        severity: 'blocking',
      },
    ]);
  });
});
