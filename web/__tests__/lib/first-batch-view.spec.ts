import {
  adsMade,
  costLabel,
  isRunning,
  progressSteps,
  quoteState,
  readFromImagesOnly,
  stopView,
  type FirstBatch,
  type FirstBatchQuote,
} from '@/lib/ideas/first-batch-view';

/**
 * What the start page and the reading page show of the first batch (ADR 0018,
 * design §5.1, §5.2, #195): the step it is at, why it stopped and what
 * continuing takes, and what the start button can say.
 */
function batch(extra: Partial<FirstBatch> = {}): FirstBatch {
  return {
    id: 'mfb_secret',
    operation_id: 'op_secret',
    state: 'reading',
    needs_attention: false,
    attention: null,
    resume: null,
    intake_id: 'pi_secret',
    product_id: null,
    project_id: null,
    market: 'US',
    ad_language: 'en-US',
    strategy_language: 'en-US',
    execution_mode: 'byok',
    selected_variant: null,
    confirmed_max_cost: '0.8625',
    reserved_total: '0.0125',
    currency: 'USD',
    image_provider: 'openai',
    image_model: 'image-model',
    ads_planned: null,
    created_at: '2026-11-17T08:00:00+00:00',
    updated_at: '2026-11-17T08:00:00+00:00',
    ...extra,
  };
}

function quote(extra: Partial<FirstBatchQuote> = {}): FirstBatchQuote {
  return {
    available: true,
    unavailable_reason: null,
    execution_mode: 'byok',
    maximum_cost: '0.8625',
    currency: 'USD',
    quote_hash: 'qh_1',
    image_provider: 'openai',
    image_model: 'image-model',
    steps: [],
    ...extra,
  };
}

describe('progressSteps', () => {
  const states = (b: FirstBatch) => progressSteps(b).map((s) => s.state);

  it('follows the batch from reading to making the ads', () => {
    expect(states(batch({ state: 'reading' }))).toEqual([
      'active',
      'pending',
      'pending',
      'pending',
    ]);
    expect(states(batch({ state: 'understanding' }))).toEqual([
      'done',
      'active',
      'pending',
      'pending',
    ]);
    expect(states(batch({ state: 'ideas' }))).toEqual([
      'done',
      'done',
      'active',
      'pending',
    ]);
    expect(states(batch({ state: 'generating' }))).toEqual([
      'done',
      'done',
      'done',
      'active',
    ]);
    expect(states(batch({ state: 'done' }))).toEqual([
      'done',
      'done',
      'done',
      'done',
    ]);
    expect(progressSteps(batch()).map((s) => s.key)).toEqual([
      'reading',
      'understanding',
      'ideas',
      'images',
    ]);
  });

  it('marks the step it stopped at, keeping what was done before it', () => {
    expect(
      states(
        batch({
          state: 'needs_attention',
          attention: {
            reason: 'too_few_claims',
            step: 'ideas',
            resumable: true,
          },
        })
      )
    ).toEqual(['done', 'done', 'stopped', 'pending']);
    expect(
      states(
        batch({
          state: 'failed',
          attention: {
            reason: 'page_unreadable',
            step: 'reading',
            resumable: false,
          },
        })
      )
    ).toEqual(['stopped', 'pending', 'pending', 'pending']);
  });
});

/** S9 (#197) adds a checking state and a checks step; today's generated
 * contract does not list them yet. */
const CHECKING = 'checking' as unknown as FirstBatch['state'];
const CHECKS = 'checks' as unknown as NonNullable<
  FirstBatch['attention']
>['step'];

describe('progressSteps once the ads are checked (S9, #197)', () => {
  const states = (b: FirstBatch) => progressSteps(b).map((s) => s.state);

  it('counts checking as every step done: the ads are made', () => {
    expect(states(batch({ state: CHECKING }))).toEqual([
      'done',
      'done',
      'done',
      'done',
    ]);
    expect(adsMade(batch({ state: CHECKING }))).toBe(true);
    expect(adsMade(batch({ state: 'done' }))).toBe(true);
    expect(adsMade(batch({ state: 'generating' }))).toBe(false);
    expect(isRunning(batch({ state: CHECKING }))).toBe(false);
  });

  it('adds the check as a fifth step only when it stopped there', () => {
    const steps = progressSteps(
      batch({
        state: 'needs_attention',
        attention: { reason: 'step_failed', step: CHECKS, resumable: true },
      })
    );
    expect(steps.map((s) => s.key)).toEqual([
      'reading',
      'understanding',
      'ideas',
      'images',
      'checks',
    ]);
    expect(steps.map((s) => s.state)).toEqual([
      'done',
      'done',
      'done',
      'done',
      'stopped',
    ]);
    expect(progressSteps(batch({ state: 'generating' }))).toHaveLength(4);
  });
});

describe('stopView', () => {
  const resume = {
    maximum_cost: '0.95',
    additional_cost: '0.0875',
    currency: 'USD' as const,
    quote_hash: 'rq_1',
    requires: [] as ('selected_variant' | 'additional_text')[],
  };

  it('is nothing while the batch runs or after it is done', () => {
    for (const state of [
      'reading',
      'understanding',
      'ideas',
      'generating',
      'done',
    ] as const)
      expect(stopView(batch({ state }))).toBeNull();
  });

  it('asks to choose a model, continuing with the chosen one', () => {
    const view = stopView(
      batch({
        state: 'needs_attention',
        attention: {
          reason: 'several_models',
          step: 'reading',
          variants: ['7 kW', '11 kW'],
          resumable: true,
        },
        resume: { ...resume, requires: ['selected_variant'] },
      })
    );
    expect(view).toEqual({
      kind: 'choose_model',
      reason: 'several_models',
      step: 'reading',
      variants: ['7 kW', '11 kW'],
      failedAds: 0,
      resume: { ...resume, requires: ['selected_variant'] },
    });
  });

  it('starts again when the page could not be read; the batch has ended', () => {
    expect(
      stopView(
        batch({
          state: 'failed',
          attention: {
            reason: 'page_unreadable',
            step: 'reading',
            resumable: false,
          },
        })
      )
    ).toMatchObject({ kind: 'start_again', reason: 'page_unreadable' });
    expect(
      stopView(
        batch({
          state: 'failed',
          attention: {
            reason: 'reading_failed',
            step: 'reading',
            resumable: false,
          },
        })
      )
    ).toMatchObject({ kind: 'start_again', reason: 'reading_failed' });
    // Any other ended batch: also start again, never a continue button.
    expect(
      stopView(
        batch({
          state: 'failed',
          attention: {
            reason: 'step_failed',
            step: 'understanding',
            code: 'ideas_unavailable',
            resumable: false,
          },
        })
      )
    ).toMatchObject({ kind: 'start_again', resume: null });
  });

  it('asks for more material when the product cannot carry two claims', () => {
    expect(
      stopView(
        batch({
          state: 'needs_attention',
          attention: {
            reason: 'too_few_claims',
            step: 'ideas',
            resumable: true,
          },
          resume: { ...resume, requires: ['additional_text'] },
        })
      )
    ).toMatchObject({ kind: 'add_material', reason: 'too_few_claims' });
  });

  it('continues after a refusal, a failed step or the confirmed maximum', () => {
    for (const reason of [
      'over_budget',
      'ideas_refused',
      'step_failed',
    ] as const)
      expect(
        stopView(
          batch({
            state: 'needs_attention',
            attention: { reason, step: 'ideas', resumable: true },
            resume,
          })
        )
      ).toMatchObject({ kind: 'continue', reason, resume });
  });

  it('counts the ads that did not come out', () => {
    expect(
      stopView(
        batch({
          state: 'needs_attention',
          attention: {
            reason: 'step_failed',
            step: 'images',
            combination_ids: ['c2', 'c5'],
            resumable: true,
          },
          resume,
        })
      )
    ).toMatchObject({ kind: 'continue', step: 'images', failedAds: 2 });
  });

  it('never offers to continue an uncertain call until it is reconciled', () => {
    const waiting = stopView(
      batch({
        state: 'needs_attention',
        attention: { reason: 'uncertain', step: 'images', resumable: false },
        resume: null,
      })
    );
    expect(waiting).toMatchObject({ kind: 'uncertain', resume: null });
    const reconciled = stopView(
      batch({
        state: 'needs_attention',
        attention: { reason: 'uncertain', step: 'images', resumable: true },
        resume,
      })
    );
    expect(reconciled).toMatchObject({ kind: 'uncertain', resume });
  });
});

describe('quoteState', () => {
  it('is ready with the maximum and the hash the start confirms', () => {
    expect(quoteState(quote())).toEqual({
      kind: 'ready',
      maximum: '0.8625',
      quoteHash: 'qh_1',
    });
  });

  it('asks for an OpenAI key when the workspace has none', () => {
    expect(
      quoteState(
        quote({
          available: false,
          unavailable_reason: 'openai_key_required',
          maximum_cost: '0',
        })
      )
    ).toEqual({ kind: 'key_required' });
  });

  it('says why starting is not possible otherwise', () => {
    expect(
      quoteState(
        quote({
          available: false,
          unavailable_reason: 'price_unavailable',
          maximum_cost: '0',
        })
      )
    ).toEqual({ kind: 'unavailable', reason: 'price_unavailable' });
    expect(
      quoteState(
        quote({ available: false, unavailable_reason: null, maximum_cost: '0' })
      )
    ).toEqual({ kind: 'unavailable', reason: 'unknown' });
    expect(quoteState(undefined)).toEqual({ kind: 'loading' });
  });
});

describe('costLabel', () => {
  it('rounds a maximum up to the cent, so the button never says less', () => {
    expect(costLabel('en', '0.8625')).toBe('$0.87');
    expect(costLabel('en', '0.8')).toBe('$0.80');
    expect(costLabel('en', '1.2')).toBe('$1.20');
    expect(costLabel('en', '0.0001')).toBe('$0.01');
    expect(costLabel('zh-CN', '2.5')).toBe('US$2.50');
  });
});

describe('isRunning', () => {
  it('is true while the batch still has a step to finish', () => {
    expect(isRunning(batch({ state: 'reading' }))).toBe(true);
    expect(isRunning(batch({ state: 'generating' }))).toBe(true);
    expect(isRunning(batch({ state: 'done' }))).toBe(false);
    expect(isRunning(batch({ state: 'needs_attention' }))).toBe(false);
    expect(isRunning(batch({ state: 'failed' }))).toBe(false);
    expect(isRunning(undefined)).toBe(false);
  });
});

describe('readFromImagesOnly', () => {
  it('is true only when images were uploaded with neither a link nor text', () => {
    expect(
      readFromImagesOnly({ url: null, text_chars: 0, upload_count: 2 })
    ).toBe(true);
    expect(
      readFromImagesOnly({
        url: 'https://acmecharge.example/products/home',
        text_chars: 0,
        upload_count: 2,
      })
    ).toBe(false);
    expect(
      readFromImagesOnly({ url: null, text_chars: 40, upload_count: 2 })
    ).toBe(false);
    expect(
      readFromImagesOnly({ url: null, text_chars: 0, upload_count: 0 })
    ).toBe(false);
    expect(readFromImagesOnly(undefined)).toBe(false);
  });
});
