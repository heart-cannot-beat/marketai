/** @jest-environment jsdom */
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { SWRConfig } from 'swr';
import { MarketingExecution } from '@/components/workspace/MarketingExecution';
import type { MarketingRound } from '@/components/workspace/MarketingExecution';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import fixture from '../../fixtures/marketing-ai-qc.json';
import messages from '../../../locales/en/common.json';
jest.mock('next-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, unknown>) =>
      ((messages as Record<string, string>)[key] ?? key).replace(
        /\{\{(\w+)\}\}/g,
        (_: string, name: string) => String(options?.[name] ?? '')
      ),
  }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { workspaceId: 'ws', slug: 'alpha' } }),
}));
const gateway = {
  getMarketingWorkflow: jest.fn(),
  getMarketingAIQCQuote: jest.fn(),
  startMarketingAIQC: jest.fn(),
  confirmMarketingAIQC: jest.fn(),
  reviewMarketingCreative: jest.fn(),
  createGenerationAssetVersionDownloadIntent: jest.fn(),
};
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => gateway,
}));
const blobTransfer = {
  downloadBlob: jest.fn(async () => new Blob(['image'], { type: 'image/png' })),
};
jest.mock('@/lib/domain-client/upload-deps', () => ({
  useBlobTransferClient: () => blobTransfer,
}));
let current: MarketingRound;
const project =
  fixture.project as components['schemas']['MarketingProjectDetail'];
const ok = (data: unknown) => ({ kind: 'ok', data });
function show() {
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <MarketingExecution project={project} />
    </SWRConfig>
  );
}
// Per-image review lives in the board drawer.
async function openFirstImage() {
  fireEvent.click(
    await screen.findByRole('button', { name: '使用情境演示 · Execution 1' })
  );
  return screen.findByRole('dialog', { name: 'Image details' });
}
beforeEach(() => {
  jest.clearAllMocks();
  URL.createObjectURL = jest.fn(() => 'blob:marketing-review');
  URL.revokeObjectURL = jest.fn();
  Object.defineProperty(globalThis.crypto, 'randomUUID', {
    configurable: true,
    value: () => 'ai-check-action',
  });
  current = JSON.parse(JSON.stringify(fixture.row)) as MarketingRound;
  gateway.getMarketingWorkflow.mockImplementation(async () =>
    ok({
      project_id: project.id,
      current,
      rounds: [],
      next_action: 'review_creatives',
    })
  );
  gateway.getMarketingAIQCQuote.mockResolvedValue(
    ok({
      ...fixture.quote,
      available: false,
      unavailable_reason: 'nothing_to_check',
    })
  );
  gateway.createGenerationAssetVersionDownloadIntent.mockResolvedValue(
    ok({ download_grant: { url: 'http://localhost/private.png' } })
  );
  for (const method of [
    'startMarketingAIQC',
    'confirmMarketingAIQC',
    'reviewMarketingCreative',
  ] as const)
    gateway[method].mockImplementation(async () =>
      ok({ id: current.id, data: current })
    );
});

test('AI findings are prefilled and shared product uncertainty is confirmed once for the batch', async () => {
  show();
  const confirm = await screen.findByRole('button', {
    name: 'Confirm quality for 0 AI-reviewed creatives',
  });
  expect(confirm).toBeDisabled();
  expect(await screen.findAllByRole('img')).toHaveLength(5);
  await openFirstImage();
  expect(
    screen
      .getAllByLabelText(messages['marketing-ai-qc-check-proposition_clear'])[0]
      .closest('details')
  ).not.toHaveAttribute('open');
  fireEvent.click(screen.getByRole('button', { name: 'Close' }));
  fireEvent.click(
    screen.getByRole('checkbox', {
      name: /I have checked the product appearance across these 5 creatives/,
    })
  );
  fireEvent.click(
    screen.getByRole('button', {
      name: 'Confirm quality for 5 AI-reviewed creatives',
    })
  );
  await waitFor(() =>
    expect(gateway.confirmMarketingAIQC).toHaveBeenCalledTimes(1)
  );
  const params = gateway.confirmMarketingAIQC.mock.calls[0][0];
  // Each image is confirmed from both its single-image and its comparison result.
  expect(params.body.ai_qc_ids).toHaveLength(10);
  expect(params.body.ai_qc_ids).toEqual(
    expect.arrayContaining(
      current.assets.flatMap((a) => [a.ai_qc!.id, a.paired_ai_qc!.id])
    )
  );
  expect(params.body.resolved_checks).toEqual({ product_fidelity: 'pass' });
  expect(params.body.note).toMatch(/I checked product appearance/);
  expect(gateway.reviewMarketingCreative).not.toHaveBeenCalled();
});

test('single-creative edits retain AI provenance and require a human reason', async () => {
  current.assets = [current.assets[0]];
  show();
  await openFirstImage();
  fireEvent.click(
    await screen.findByText('Review flags or adjust AI suggestions')
  );
  const approval = screen.getByRole('button', {
    name: 'Confirm quality approval',
  });
  expect(approval).toBeDisabled();
  fireEvent.change(
    screen.getByRole('combobox', {
      name: messages['marketing-ai-qc-check-product_fidelity'],
    }),
    { target: { value: 'pass' } }
  );
  expect(approval).toBeDisabled();
  fireEvent.change(
    screen.getByLabelText('Why you changed or resolved the AI finding'),
    {
      target: {
        value: 'Compared the product appearance with the physical product.',
      },
    }
  );
  fireEvent.change(
    screen.getByLabelText('What you checked or what needs correction'),
    { target: { value: 'Checked the exact image.' } }
  );
  fireEvent.click(approval);
  await waitFor(() =>
    expect(gateway.reviewMarketingCreative).toHaveBeenCalled()
  );
  expect(gateway.reviewMarketingCreative.mock.calls[0][0].body).toMatchObject({
    ai_qc_id: current.assets[0].ai_qc!.id,
    checks: { product_fidelity: 'pass' },
    override_note: 'Compared the product appearance with the physical product.',
  });
});

test('each kind has its own cost confirmation and nothing paid starts merely by rendering', async () => {
  current.assets = current.assets.map((a) => ({
    ...a,
    ai_qc: null,
    paired_ai_qc: null,
  }));
  gateway.getMarketingAIQCQuote.mockImplementation(
    async ({ kind }: { kind: 'single' | 'paired' }) =>
      ok({ ...fixture.quote, kind, quote_hash: `${kind}-quote` })
  );
  show();
  const single = await screen.findByRole('button', {
    name: 'Confirm up to $0.50 and check 5 generated images',
  });
  expect(
    screen.getByRole('button', {
      name: 'Confirm up to $0.50 and run 5 comparison checks',
    })
  ).toBeInTheDocument();
  expect(gateway.startMarketingAIQC).not.toHaveBeenCalled();
  expect(
    gateway.getMarketingAIQCQuote.mock.calls.map((c) => c[0].kind).sort()
  ).toEqual(['paired', 'single']);
  fireEvent.click(single);
  await waitFor(() =>
    expect(gateway.startMarketingAIQC).toHaveBeenCalledTimes(1)
  );
  // Findings are written in the interface's strategy language when the plan has none (J-43).
  expect(gateway.startMarketingAIQC.mock.calls[0][0].body).toEqual({
    quote_hash: 'single-quote',
    kind: 'single',
    strategy_language: 'en-US',
  });
  expect(
    gateway.getMarketingAIQCQuote.mock.calls.map((c) => c[0].strategyLanguage)
  ).toEqual(['en-US', 'en-US']);
  await waitFor(() =>
    expect(
      screen.getByRole('button', {
        name: 'Confirm up to $0.50 and run 5 comparison checks',
      })
    ).toBeEnabled()
  );
  fireEvent.click(
    screen.getByRole('button', {
      name: 'Confirm up to $0.50 and run 5 comparison checks',
    })
  );
  await waitFor(() =>
    expect(gateway.startMarketingAIQC).toHaveBeenCalledTimes(2)
  );
  expect(gateway.startMarketingAIQC.mock.calls[1][0].body).toEqual({
    quote_hash: 'paired-quote',
    kind: 'paired',
    strategy_language: 'en-US',
  });
});

test('a result written in another language is labelled and shown as recorded, not translated', async () => {
  current.assets = [current.assets[0]];
  // The evidence, not the summary: a result that still needs confirmation shows the server's verdict line.
  const recorded = current.assets[0].ai_qc!.result!.findings.find(
    (f) => f.check_id === 'proposition_clear'
  )!.visual_evidence;
  current.assets[0].ai_qc!.language = 'zh-CN';
  current.assets[0].paired_ai_qc!.language = 'en-US';
  show();
  const drawer = await openFirstImage();
  const single = within(drawer).getByRole('region', {
    name: 'Single-image check',
  });
  const paired = within(drawer).getByRole('region', {
    name: 'Comparison check',
  });
  expect(within(single).getByRole('note')).toHaveTextContent(
    'Written in Chinese (Simplified) and shown as recorded, without translation.'
  );
  expect(single).toHaveTextContent(recorded);
  expect(within(paired).queryByRole('note')).not.toBeInTheDocument();
});

test('comparison checks name the images they still wait for instead of passing', async () => {
  current.assets = current.assets.map((a) => ({ ...a, paired_ai_qc: null }));
  gateway.getMarketingAIQCQuote.mockImplementation(
    async ({ kind }: { kind: 'single' | 'paired' }) =>
      ok(
        kind === 'single'
          ? {
              ...fixture.quote,
              kind,
              available: false,
              quantity: 0,
              unavailable_reason: 'nothing_to_check',
            }
          : {
              ...fixture.quote,
              kind,
              available: false,
              quantity: 0,
              asset_version_ids: [],
              unavailable_reason: 'paired_qc_waiting',
              waiting: [
                {
                  asset_version_id: current.assets[0].asset_version_id,
                  awaiting_combination_ids: ['c2_2', 'c2_3'],
                },
              ],
            }
      )
  );
  show();
  expect(
    await screen.findByText(
      'Comparison checks start once every image they compare with has been generated.'
    )
  ).toBeInTheDocument();
  expect(
    screen.getByText(/1 images wait for comparison images of:/)
  ).toHaveTextContent(
    '减少购买顾虑 · 产品细节说明; 减少购买顾虑 · 适用边界解读'
  );
  expect(
    screen.queryByRole('button', { name: /comparison checks/ })
  ).not.toBeInTheDocument();
  // Without a current comparison result no image is offered for batch approval.
  expect(
    screen.queryByRole('button', { name: /Confirm quality for/ })
  ).not.toBeInTheDocument();
});

test('single and paired results show apart, with passing items folded and doubts open', async () => {
  current.assets = [current.assets[0]];
  current.assets[0].paired_ai_qc!.result!.findings[0].status = 'fail';
  current.assets[0].paired_ai_qc!.result!.findings[0].rationale =
    'It looks like the Q&A expression, not the planned demonstration.';
  show();
  const drawer = await openFirstImage();
  const single = within(drawer).getByRole('region', {
    name: 'Single-image check',
  });
  const paired = within(drawer).getByRole('region', {
    name: 'Comparison check',
  });
  expect(single).toHaveTextContent(
    messages['marketing-ai-qc-check-product_fidelity']
  );
  expect(
    within(single)
      .getByText(/checks passed/)
      .closest('details')
  ).not.toHaveAttribute('open');
  expect(paired).toHaveTextContent(
    'It looks like the Q&A expression, not the planned demonstration.'
  );
  expect(paired).toHaveTextContent('Compared with');
  fireEvent.click(screen.getByText('Review flags or adjust AI suggestions'));
  fireEvent.change(
    screen.getByRole('combobox', {
      name: messages['marketing-ai-qc-check-product_fidelity'],
    }),
    { target: { value: 'pass' } }
  );
  fireEvent.change(
    screen.getAllByRole('combobox', {
      name: messages['marketing-ai-qc-check-belongs_to_intended_combination'],
    })[0],
    { target: { value: 'pass' } }
  );
  fireEvent.change(
    screen.getByLabelText('Why you changed or resolved the AI finding'),
    { target: { value: 'The demonstration step is the planned one.' } }
  );
  fireEvent.change(
    screen.getByLabelText('What you checked or what needs correction'),
    { target: { value: 'Checked the exact image.' } }
  );
  fireEvent.click(
    screen.getByRole('button', { name: 'Confirm quality approval' })
  );
  await waitFor(() =>
    expect(gateway.reviewMarketingCreative).toHaveBeenCalled()
  );
  expect(gateway.reviewMarketingCreative.mock.calls[0][0].body).toMatchObject({
    ai_qc_id: current.assets[0].ai_qc!.id,
    paired_ai_qc_id: current.assets[0].paired_ai_qc!.id,
    compared_asset_version_ids:
      current.assets[0].paired_ai_qc!.compared_asset_version_ids,
  });
});

test('approving with only the single-image result asks for the override reason', async () => {
  current.assets = [{ ...current.assets[0], paired_ai_qc: null }];
  show();
  const drawer = await openFirstImage();
  expect(drawer).toHaveTextContent('No current comparison check yet.');
  expect(drawer).toHaveTextContent(
    'Only one kind of check has a current result.'
  );
  fireEvent.click(screen.getByText('Review flags or adjust AI suggestions'));
  // The comparison checks have no AI answer, so they wait for a human decision.
  // Named by their copy: the fixture's comparison_faithful criterion is older (#308).
  for (const name of [
    messages['marketing-ai-qc-check-product_fidelity'],
    messages['marketing-ai-qc-check-belongs_to_intended_combination'],
    messages['marketing-ai-qc-check-comparison_faithful'],
  ])
    fireEvent.change(screen.getAllByRole('combobox', { name })[0], {
      target: { value: 'pass' },
    });
  const approval = screen.getByRole('button', {
    name: 'Confirm quality approval',
  });
  expect(approval).toBeDisabled();
  fireEvent.change(
    screen.getByLabelText('Why you changed or resolved the AI finding'),
    { target: { value: 'Checked the comparison by hand.' } }
  );
  fireEvent.change(
    screen.getByLabelText('What you checked or what needs correction'),
    { target: { value: 'Checked the exact image.' } }
  );
  fireEvent.click(approval);
  await waitFor(() =>
    expect(gateway.reviewMarketingCreative).toHaveBeenCalled()
  );
  expect(
    gateway.reviewMarketingCreative.mock.calls[0][0].body.paired_ai_qc_id
  ).toBeNull();
});

test('a call from before the split shows once and is confirmed with one id', async () => {
  current.assets = current.assets.map((a) => {
    const combined = {
      ...a.ai_qc!,
      kind: 'combined' as const,
      compared_asset_version_ids: a.paired_ai_qc!.compared_asset_version_ids,
    };
    return { ...a, ai_qc: combined, paired_ai_qc: combined };
  });
  show();
  fireEvent.click(
    await screen.findByRole('checkbox', {
      name: /I have checked the product appearance across these 5 creatives/,
    })
  );
  fireEvent.click(
    screen.getByRole('button', {
      name: 'Confirm quality for 5 AI-reviewed creatives',
    })
  );
  await waitFor(() => expect(gateway.confirmMarketingAIQC).toHaveBeenCalled());
  expect(gateway.confirmMarketingAIQC.mock.calls[0][0].body.ai_qc_ids).toEqual(
    current.assets.map((a) => a.ai_qc!.id)
  );
  const drawer = await openFirstImage();
  expect(
    within(drawer).getAllByRole('region', { name: /check$/ })
  ).toHaveLength(1);
  expect(
    within(drawer).getByRole('region', { name: 'Earlier combined check' })
  ).toBeInTheDocument();
});

test('AI failures stay out of batch approval', async () => {
  current.assets[0].ai_qc!.result!.findings.find(
    (f) => f.check_id === 'proposition_clear'
  )!.status = 'fail';
  show();
  fireEvent.click(
    await screen.findByRole('checkbox', {
      name: /I have checked the product appearance across these 4 creatives/,
    })
  );
  fireEvent.click(
    screen.getByRole('button', {
      name: 'Confirm quality for 4 AI-reviewed creatives',
    })
  );
  await waitFor(() => expect(gateway.confirmMarketingAIQC).toHaveBeenCalled());
  expect(
    gateway.confirmMarketingAIQC.mock.calls[0][0].body.ai_qc_ids
  ).not.toContain(current.assets[0].ai_qc!.id);
});

test('private images use a local blob URL accepted by the production CSP and revoke it on unmount', async () => {
  current.assets = [current.assets[0]];
  const view = show();
  const image = (await screen.findAllByRole('img'))[0];
  expect(image).toHaveAttribute('src', 'blob:marketing-review');
  expect(blobTransfer.downloadBlob).toHaveBeenCalledWith(
    expect.objectContaining({ url: 'http://localhost/private.png' }),
    expect.objectContaining({ signal: expect.anything() })
  );
  view.unmount();
  expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:marketing-review');
});

test('bulk AI confirmation excludes a creative with a saved human correction', async () => {
  const corrected = current.assets[0];
  corrected.quality = {
    asset_version_id: corrected.asset_version_id,
    source: 'human_semantic_review',
    required_checks: corrected.required_checks,
    recipe_hash: 'test-recipe',
    plan_hash: current.snapshot.plan_hash,
    override_note: '',
    quality: 'needs_changes',
    checks: { product_fidelity: 'fail' },
    note: 'The connector geometry differs from the real product.',
  };
  show();
  await openFirstImage();
  expect(
    await screen.findByText('Changes requested · original AI report retained')
  ).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Close' }));
  fireEvent.click(
    screen.getByRole('checkbox', {
      name: /I have checked the product appearance across these 4 creatives/,
    })
  );
  fireEvent.click(
    screen.getByRole('button', {
      name: 'Confirm quality for 4 AI-reviewed creatives',
    })
  );
  await waitFor(() =>
    expect(gateway.confirmMarketingAIQC).toHaveBeenCalledTimes(1)
  );
  expect(
    gateway.confirmMarketingAIQC.mock.calls[0][0].body.ai_qc_ids
  ).not.toContain(corrected.ai_qc!.id);
});

test('continuing an earlier version opens its drawer with the editing tools expanded', async () => {
  show();
  const history = (
    await screen.findByText('Version history and comparison')
  ).closest('details')!;
  history.open = true;
  fireEvent(history, new Event('toggle'));
  fireEvent.click(
    within(history).getAllByRole('button', {
      name: 'Continue editing this version',
    })[0]
  );
  const drawer = await screen.findByRole('dialog', { name: 'Image details' });
  expect(
    within(drawer).getByText('More actions').closest('details')
  ).toHaveAttribute('open');
  expect(
    within(drawer).getByText('Crop and rotate').closest('details')
  ).toHaveAttribute('open');
});

type Finding = NonNullable<
  NonNullable<MarketingRound['assets'][number]['ai_qc']>['result']
>['findings'][number];
const VOLT_COPY =
  'Charge your EV at Halcyon Superchargers with the Acme Volt Plus, built with a safety interlock.';
const VORTES =
  'Charge your EV at Halcyon Superchargers with the Acme Vols Plus, built with a safety inerlock.';

// The J-50 image (#294): the model passed everything, the server's comparison failed the copy.
function misspeltCopy() {
  current.assets = [current.assets[0]];
  const result = current.assets[0].ai_qc!.result!;
  result.summary =
    'Overall quality check passed; all text is in English with no spelling errors.';
  result.quality_suggestion = 'needs_changes';
  result.rendered_text = {
    headline: current.assets[0].headline,
    body_copy: VORTES,
  };
  const copy = result.findings.find((f) => f.check_id === 'copy_consistent')!;
  Object.assign(copy, {
    status: 'fail',
    model_status: 'pass',
    gate_reason: 'rendered_text_mismatch',
    rationale: 'All copy matches the confirmed text.',
    suggested_fix:
      'Render the confirmed copy exactly; correct: “Vols” → “Volt”, “inerlock” → “interlock”',
    rendered_text_diff: [
      {
        field: 'body_copy',
        expected: VOLT_COPY,
        rendered: VORTES,
        changes: [
          { expected: 'Volt', rendered: 'Vols' },
          { expected: 'interlock', rendered: 'inerlock' },
        ],
      },
    ],
  } satisfies Partial<Finding>);
  return result;
}

test('a rendered copy that differs fails with its diff, and a summary that contradicts the findings is not the verdict', async () => {
  const result = misspeltCopy();
  show();
  const drawer = await openFirstImage();
  const single = within(drawer).getByRole('region', {
    name: 'Single-image check',
  });
  expect(single).not.toHaveTextContent(result.summary);
  expect(single).toHaveTextContent('Failed: 1 · To confirm: 1 · Passed: 6');
  expect(single).toHaveTextContent(
    'The text in the image differs from the confirmed copy.'
  );
  expect(single).not.toHaveTextContent('All copy matches the confirmed text.');
  const body = single.querySelector('[data-copy-field="body_copy"]')!;
  expect(body).toHaveTextContent('Body text');
  expect(body).toHaveTextContent(`Confirmed: ${VOLT_COPY}`);
  expect(body).toHaveTextContent(`In the image: ${VORTES}`);
  expect(
    within(body as HTMLElement)
      .getAllByRole('listitem')
      .map((li) => li.textContent)
  ).toEqual([
    '“Vols” should read “Volt”',
    '“inerlock” should read “interlock”',
  ]);
  // A summary that agrees with every finding still reads as the verdict.
  const paired = within(drawer).getByRole('region', {
    name: 'Comparison check',
  });
  expect(paired).toHaveTextContent(
    current.assets[0].paired_ai_qc!.result!.summary
  );
  // The review note never starts from the contradicting summary.
  fireEvent.click(screen.getByText('Review flags or adjust AI suggestions'));
  expect(
    screen.getByLabelText('What you checked or what needs correction')
  ).toHaveValue('');
  expect(
    screen.getAllByText('“Vols” should read “Volt”').length
  ).toBeGreaterThan(1);
});

test('a copy the check could not read is a doubt, not a pass', async () => {
  current.assets = [current.assets[0]];
  const copy = current.assets[0].ai_qc!.result!.findings.find(
    (f) => f.check_id === 'copy_consistent'
  )!;
  Object.assign(copy, {
    status: 'unknown',
    gate_reason: 'rendered_text_missing',
    rendered_text_diff: [
      {
        field: 'headline',
        expected: current.assets[0].headline,
        rendered: '',
        changes: [],
      },
    ],
  } satisfies Partial<Finding>);
  show();
  const drawer = await openFirstImage();
  const single = within(drawer).getByRole('region', {
    name: 'Single-image check',
  });
  expect(single).toHaveTextContent(
    'The check could not read all of the headline and body in the image'
  );
  expect(single).toHaveTextContent('In the image: could not be read');
  expect(single).toHaveTextContent('Failed: 0 · To confirm: 2 · Passed: 6');
});

test('a refused QC output is explained, with its excerpt and hash, before checking again (#295)', async () => {
  current.assets = [current.assets[0]];
  const excerpt =
    '{"rendered_text": {"headline": "x"}, "summary": "ok", "findings": [{"visual_evidence": "xxxx';
  current.assets[0].ai_qc = {
    ...current.assets[0].ai_qc!,
    state: 'failed',
    result: null,
    current: false,
    error_code: 'qc_output_invalid',
    error_detail: {
      reason: 'qc_output_schema_invalid',
      field: 'findings.0.visual_evidence',
      problem: 'string_too_long',
      excerpt,
      sha256: 'a'.repeat(64),
      length: 5321,
    },
  };
  gateway.getMarketingAIQCQuote.mockImplementation(
    async ({ kind }: { kind: 'single' | 'paired' }) =>
      ok(
        kind === 'single'
          ? { ...fixture.quote, kind, quantity: 1, maximum_cost: '0.10' }
          : {
              ...fixture.quote,
              kind,
              available: false,
              unavailable_reason: 'nothing_to_check',
            }
      )
  );
  show();
  const retry = await screen.findByRole('button', {
    name: 'Confirm up to $0.10 and check 1 generated images',
  });
  const explained = screen
    .getAllByRole('alert')
    .find((a) => /The last check of 1 images? failed:/.test(a.textContent!))!;
  expect(explained).toHaveTextContent(
    "The AI check's answer did not match the required format."
  );
  expect(
    explained.compareDocumentPosition(retry) & Node.DOCUMENT_POSITION_FOLLOWING
  ).toBeTruthy();
  const drawer = await openFirstImage();
  const single = within(drawer).getByRole('region', {
    name: 'Single-image check',
  });
  expect(single).toHaveTextContent('AI could not complete this check.');
  expect(single).toHaveTextContent(
    "The AI check's answer did not match the required format."
  );
  expect(single).toHaveTextContent(
    'Field: findings.0.visual_evidence · Problem: string_too_long'
  );
  expect(within(single).getByText(excerpt).tagName).toBe('PRE');
  expect(single).toHaveTextContent(
    `First ${excerpt.length} of 5321 characters · SHA-256 ${'a'.repeat(64)}`
  );
});

test('a round with no images yet says so instead of claiming they were checked (#300)', async () => {
  current.assets = [];
  show();
  expect(
    await screen.findAllByText(
      'No images in this round yet. Checks run once an image is generated.'
    )
  ).toHaveLength(2);
  expect(
    screen.queryByText(/already been checked or approved/)
  ).not.toBeInTheDocument();
  expect(gateway.getMarketingAIQCQuote).not.toHaveBeenCalled();
});
