import {
  acceptImages,
  buildConfirmRequest,
  buildIntakeRequest,
  cardDraftFrom,
  intakePhase,
  linkHost,
  nextPollDelay,
  needsVariantChoice,
  offersSupplement,
  POLL_INITIAL_MS,
  POLL_MAX_MS,
  selectVariant,
  splitPaste,
  stepStates,
  suggestedAction,
  validateCard,
  variantDifferences,
  type IntakeCard,
} from '@/lib/products/intake-model';

function file(name: string, type: string, size = 10): File {
  const blob = new File(['x'], name, { type });
  Object.defineProperty(blob, 'size', { value: size });
  return blob;
}

describe('splitPaste', () => {
  it('treats a lone link as the url only', () => {
    expect(splitPaste('  https://shop.example/p/wallbox  ')).toEqual({
      url: 'https://shop.example/p/wallbox',
      text: null,
    });
  });

  it('treats plain text as text only', () => {
    expect(splitPaste('一款 11kW 家用充电桩')).toEqual({
      url: null,
      text: '一款 11kW 家用充电桩',
    });
  });

  it('keeps both when a link comes with a description', () => {
    const raw = '看这个：https://shop.example/p/wallbox，白色款，送安装';
    expect(splitPaste(raw)).toEqual({
      url: 'https://shop.example/p/wallbox',
      text: raw,
    });
  });

  it('drops trailing punctuation from the link', () => {
    expect(splitPaste('https://shop.example/p.').url).toBe(
      'https://shop.example/p'
    );
  });

  it('is empty for whitespace', () => {
    expect(splitPaste('   ')).toEqual({ url: null, text: null });
  });

  it('shows only the host of a link', () => {
    expect(linkHost('https://shop.example/p/wallbox?ref=1')).toBe(
      'shop.example'
    );
    expect(linkHost('not a url')).toBeNull();
  });
});

describe('acceptImages', () => {
  it('accepts png, jpeg and webp up to 10 MB and 12 images', () => {
    const { accepted, rejected } = acceptImages(10, [
      file('a.png', 'image/png'),
      file('b.gif', 'image/gif'),
      file('c.jpg', 'image/jpeg', 11 * 1024 * 1024),
      file('d.webp', 'image/webp'),
      file('e.png', 'image/png'),
    ]);
    expect(accepted.map((f) => f.name)).toEqual(['a.png', 'd.webp']);
    expect(rejected.map((r) => [r.file.name, r.reason])).toEqual([
      ['b.gif', 'type'],
      ['c.jpg', 'size'],
      ['e.png', 'count'],
    ]);
  });
});

describe('buildIntakeRequest', () => {
  it('sends any combination of link, text and uploads', () => {
    expect(buildIntakeRequest({ url: null, text: '介绍' }, [])).toEqual({
      text: '介绍',
    });
    expect(
      buildIntakeRequest({ url: 'https://a.example/p', text: null }, ['ui_1'])
    ).toEqual({ url: 'https://a.example/p', upload_intent_ids: ['ui_1'] });
    expect(
      buildIntakeRequest({ url: 'https://a.example/p', text: '白色' }, [
        'ui_1',
        'ui_2',
      ])
    ).toEqual({
      url: 'https://a.example/p',
      text: '白色',
      upload_intent_ids: ['ui_1', 'ui_2'],
    });
  });

  it('is null when nothing was given', () => {
    expect(buildIntakeRequest({ url: null, text: null }, [])).toBeNull();
  });
});

describe('intake status', () => {
  it('maps statuses to phases', () => {
    expect(intakePhase('queued')).toBe('working');
    expect(intakePhase('preparing_images')).toBe('working');
    expect(intakePhase('ready')).toBe('ready');
    expect(intakePhase('confirmed')).toBe('confirmed');
    expect(intakePhase('fetch_failed')).toBe('failed');
    expect(intakePhase('extraction_failed')).toBe('failed');
  });

  it('marks the active step and skips fetching without a link', () => {
    expect(
      stepStates({ status: 'extracting', input: { url: 'https://a/p' } })
    ).toEqual({ fetch: 'done', extract: 'active', images: 'pending' });
    expect(stepStates({ status: 'extracting', input: { url: null } })).toEqual({
      fetch: 'skipped',
      extract: 'active',
      images: 'pending',
    });
    expect(stepStates({ status: 'ready', input: { url: null } })).toEqual({
      fetch: 'skipped',
      extract: 'done',
      images: 'done',
    });
  });

  it('marks the step a failure happened in', () => {
    expect(
      stepStates({
        status: 'fetch_failed',
        input: { url: 'https://a/p' },
        failure: {
          code: 'access_denied',
          step: 'fetch',
          message: 'x',
          suggested_action: 'paste_text_or_screenshot',
          retryable: false,
        },
      })
    ).toEqual({ fetch: 'failed', extract: 'pending', images: 'pending' });
  });

  it('backs off from 1 s to at most 5 s', () => {
    const delays: number[] = [];
    let delay: number | null = null;
    for (let i = 0; i < 8; i++) {
      delay = nextPollDelay(delay);
      delays.push(delay);
    }
    expect(delays[0]).toBe(POLL_INITIAL_MS);
    expect(delays[1]).toBe(1500);
    expect(Math.max(...delays)).toBe(POLL_MAX_MS);
    expect(delays[delays.length - 1]).toBe(POLL_MAX_MS);
  });
});

describe('failure guidance', () => {
  it('uses the suggested action, falling back on retryable', () => {
    expect(
      suggestedAction({
        code: 'timeout',
        message: '',
        suggested_action: 'check_link',
        retryable: false,
      })
    ).toBe('check_link');
    expect(
      suggestedAction({
        code: 'x',
        message: '',
        suggested_action: 'something_new',
        retryable: true,
      })
    ).toBe('retry');
    expect(suggestedAction(null)).toBe('paste_text_or_screenshot');
  });

  it('offers the inline supplement box only where adding content helps', () => {
    expect(offersSupplement('paste_text_or_screenshot')).toBe(true);
    expect(offersSupplement('add_more_detail')).toBe(true);
    expect(offersSupplement('check_link')).toBe(true);
    expect(offersSupplement('retry')).toBe(false);
    expect(offersSupplement('bind_openai_key')).toBe(false);
  });
});

const CARD: IntakeCard = {
  name: 'VoltNest 壁挂充电桩',
  description: '家用 11 kW 交流充电桩',
  specs: { 功率: '11 kW', 防护等级: 'IP65' },
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
};

describe('card draft → confirm payload', () => {
  it('starts from the AI draft with the first default image as main', () => {
    const draft = cardDraftFrom(CARD);
    expect(draft.name).toBe('VoltNest 壁挂充电桩');
    expect(draft.specs).toEqual([
      { key: '功率', value: '11 kW' },
      { key: '防护等级', value: 'IP65' },
    ]);
    expect(draft.includedImageIds).toEqual(['img_a', 'img_b']);
    expect(draft.mainImageId).toBe('img_a');
    expect(draft.selectedVariant).toBeNull();
    expect(draft.mustMatch[0]).toMatchObject({
      id: 'mm_1',
      proposedByAi: true,
    });
  });

  it('requires a variant when the page sells several', () => {
    const draft = cardDraftFrom(CARD);
    expect(validateCard(draft, CARD)).toContain('variant_required');
    expect(validateCard({ ...draft, selectedVariant: '11 kW' }, CARD)).toEqual(
      []
    );
  });

  it('sends every edited field, the main image first and the chosen variant', () => {
    const draft = {
      ...cardDraftFrom(CARD),
      name: '  VoltNest Pro ',
      specs: [
        { key: '功率', value: '11 kW' },
        { key: '', value: '空的行不发送' },
      ],
      sellingPoints: ['充电快三倍', '  ', '静音'],
      primaryAudience: 1,
      mustMatch: [
        ...cardDraftFrom(CARD).mustMatch,
        {
          id: null,
          type: 'logo' as const,
          description: 'Logo 在正面',
          severity: 'warning' as const,
          proposedByAi: false,
        },
      ],
      mainImageId: 'img_b',
      selectedVariant: '11 kW',
    };
    expect(buildConfirmRequest(draft, CARD)).toEqual({
      card: {
        name: 'VoltNest Pro',
        description: '家用 11 kW 交流充电桩',
        specs: { 功率: '11 kW' },
        selling_points: ['充电快三倍', '静音'],
        audiences: [
          { who: '新车主', why: '家里有车位', pain: '公共桩排队' },
          { who: '别墅业主', why: '多车家庭', pain: '' },
        ],
        primary_audience_index: 1,
        must_match: [
          {
            id: 'mm_1',
            type: 'color',
            description: '机身白色',
            severity: 'blocking',
          },
          { type: 'logo', description: 'Logo 在正面', severity: 'warning' },
        ],
      },
      selected_variant: '11 kW',
      image_ids: ['img_b', 'img_a'],
    });
  });

  it('never sends an image the gate did not release', () => {
    const draft = {
      ...cardDraftFrom(CARD),
      includedImageIds: ['img_c', 'img_a'],
      mainImageId: 'img_c',
      selectedVariant: '7 kW',
    };
    expect(buildConfirmRequest(draft, CARD).image_ids).toEqual(['img_a']);
  });

  it('flags empty names, descriptions, audiences and must-match items', () => {
    const draft = {
      ...cardDraftFrom(CARD),
      name: ' ',
      description: '',
      audiences: [{ who: '', why: '', pain: '' }],
      mustMatch: [
        {
          id: null,
          type: 'other' as const,
          description: '',
          severity: 'blocking' as const,
          proposedByAi: false,
        },
      ],
      selectedVariant: '7 kW',
    };
    expect(validateCard(draft, CARD)).toEqual([
      'name_required',
      'description_required',
      'audience_who_required',
      'must_match_description_required',
    ]);
  });
});

/** Issue #192: two colour editions, one image each plus a general one. */
const COLOURS: IntakeCard = {
  name: 'Grizzl-E',
  description: '充电桩',
  specs: { 外壳: '铝合金' },
  model_variants: [
    {
      label: '黑',
      specs: { 外壳: '黑色铝合金' },
      appearance: [
        {
          id: 'mm_black',
          type: 'color',
          description: '哑光黑外壳',
          proposed_by: 'ai',
        },
      ],
      replaces_must_match_ids: ['mm_colours'],
      image_ids: ['img_2'],
      default_image_ids: ['img_1', 'img_2'],
    },
    {
      label: '白',
      specs: { 涂层: '亮面' },
      appearance: [
        {
          id: 'mm_white',
          type: 'color',
          description: '亮面白外壳',
          proposed_by: 'ai',
        },
      ],
      replaces_must_match_ids: ['mm_colours'],
      image_ids: ['img_3'],
      // No default list: the model works it out from the image ids.
    },
  ],
  requires_variant_selection: true,
  must_match: [
    {
      id: 'mm_colours',
      type: 'color',
      description: '黑、白两色',
      proposed_by: 'ai',
    },
  ],
  images: [
    { image_id: 'img_1', rank: 1, source: 'upload', selectable: true },
    { image_id: 'img_2', rank: 2, source: 'upload', selectable: true },
    { image_id: 'img_3', rank: 3, source: 'upload', selectable: true },
  ],
  default_image_ids: ['img_1', 'img_2', 'img_3'],
};

describe('variant selection', () => {
  it('waits for a choice and then swaps images, specs and the colour item', () => {
    const start = cardDraftFrom(COLOURS);
    expect(start.selectedVariant).toBeNull();
    expect(needsVariantChoice(start, COLOURS)).toBe(true);

    const black = selectVariant(start, COLOURS, '黑');
    expect(needsVariantChoice(black, COLOURS)).toBe(false);
    expect(black.includedImageIds).toEqual(['img_1', 'img_2']);
    expect(black.specs).toEqual([{ key: '外壳', value: '黑色铝合金' }]);
    expect(black.mustMatch.map((item) => item.id)).toEqual(['mm_black']);
    expect(black.mustMatch[0].proposedByAi).toBe(true);

    const white = selectVariant(black, COLOURS, '白');
    expect(white.includedImageIds).toEqual(['img_1', 'img_3']);
    expect(white.specs).toEqual([
      { key: '外壳', value: '铝合金' },
      { key: '涂层', value: '亮面' },
    ]);
    expect(white.mustMatch.map((item) => item.id)).toEqual(['mm_white']);
    expect(selectVariant(white, COLOURS, '白')).toBe(white);
    expect(selectVariant(white, COLOURS, '不存在')).toBe(white);
  });

  it('keeps what the user added across switches', () => {
    const black = selectVariant(cardDraftFrom(COLOURS), COLOURS, '黑');
    const edited = {
      ...black,
      specs: [...black.specs, { key: '质保', value: '五年' }],
      mustMatch: [
        ...black.mustMatch,
        {
          id: null,
          type: 'text' as const,
          description: '侧面印有型号',
          severity: 'warning' as const,
          proposedByAi: false,
        },
      ],
    };
    const white = selectVariant(edited, COLOURS, '白');
    expect(white.specs).toContainEqual({ key: '质保', value: '五年' });
    expect(white.mustMatch.map((item) => item.description)).toEqual([
      '侧面印有型号',
      '亮面白外壳',
    ]);
  });

  it('applies a single variant without asking', () => {
    const single: IntakeCard = {
      ...COLOURS,
      model_variants: [COLOURS.model_variants![0]],
      requires_variant_selection: false,
    };
    const draft = cardDraftFrom(single);
    expect(draft.selectedVariant).toBe('黑');
    expect(needsVariantChoice(draft, single)).toBe(false);
    expect(draft.mustMatch.map((item) => item.id)).toEqual(['mm_black']);
    expect(buildConfirmRequest(draft, single).card?.specs).toEqual({
      外壳: '黑色铝合金',
    });
  });

  it('describes what each variant changes', () => {
    expect(variantDifferences(COLOURS.model_variants![0])).toEqual([
      '外壳: 黑色铝合金',
      '哑光黑外壳',
    ]);
  });

  it('writes each spec with the formatter it is given', () => {
    expect(
      variantDifferences(
        COLOURS.model_variants![0],
        (key, value) => `${key} = ${value}`
      )
    ).toEqual(['外壳 = 黑色铝合金', '哑光黑外壳']);
  });
});
