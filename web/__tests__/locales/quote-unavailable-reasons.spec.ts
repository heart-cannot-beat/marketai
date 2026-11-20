import enCommon from '../../locales/en/common.json';
import zhCommon from '../../locales/zh-CN/common.json';
import enIdeas from '../../locales/en/ideas.json';
import zhIdeas from '../../locales/zh-CN/ideas.json';
import { QUOTE_REASON_COPY } from '@/lib/ideas/ideas-problems';

/**
 * Every reason a paid quote on the ads-first screens can give for "not now"
 * (the planner's and the checks' capability, the spend checks, the image
 * price) reads as a sentence, never as its code (#204: the ideas page showed
 * `planner_not_configured` itself). A reason no service raises as an error
 * has its own copy in `ideas`; the others read as their `error-<code>`.
 */
const QUOTE_UNAVAILABLE_REASONS = [
  // app/marketing/service.py planner_capability, provider_choice.choose
  'planner_not_configured',
  'credential_required',
  // app/marketing/ai_qc.py capability
  'qc_not_configured',
  // app/generation/selection.py authorize_paid, check_budget
  'generation_not_entitled',
  'budget_exceeded',
  'guardrail_required',
  'currency_unsupported',
  // app/generation/selection.py maximum_unit_cost, first_batch._image_unit
  'price_unavailable',
  'model_unavailable',
];

describe.each([
  ['en', enCommon, enIdeas],
  ['zh-CN', zhCommon, zhIdeas],
])('quote reasons in %s', (_locale, common, ideas) => {
  it.each(QUOTE_UNAVAILABLE_REASONS)('%s has its own sentence', (reason) => {
    const own = QUOTE_REASON_COPY[reason];
    const sentence = own
      ? (ideas as Record<string, string>)[own]
      : (common as Record<string, string>)[`error-${reason}`];
    expect(typeof sentence).toBe('string');
    expect(sentence.trim()).not.toBe('');
    expect(sentence).not.toContain(reason);
  });
});
