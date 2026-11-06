/**
 * Which product the legacy guided setup (`NewTestFlow`) is set up for, kept
 * out of the URL (front-flow contract §1 principle 2): a one-shot `new-test`
 * handoff (`lib/products/handoff.ts`), then remembered for this tab so a
 * refresh keeps the same product. Since the legacy flow was frozen (#337)
 * `/w/{slug}/tests/new` redirects to the marketing test, `NewTestFlow` is not
 * mounted and nothing writes either value; kept until that flow is deleted.
 */
import { PLAN_SCALES, type ThreeAnswers } from './plan-model';

function key(slug: string): string {
  return `tests:${slug}:new-product`;
}

export function rememberNewTestProduct(
  slug: string,
  productId: string | null
): void {
  try {
    if (productId) window.sessionStorage.setItem(key(slug), productId);
    else window.sessionStorage.removeItem(key(slug));
  } catch {
    // Convenience only.
  }
}

export function recallNewTestProduct(slug: string): string | null {
  try {
    return window.sessionStorage.getItem(key(slug));
  } catch {
    return null;
  }
}

interface NewTestSetup {
  productId: string;
  answers: ThreeAnswers;
}

/** Convenience only; submitting still creates a new governed server command. */
export function rememberNewTestSetup(
  slug: string,
  setup: NewTestSetup | null
): void {
  try {
    const storageKey = `tests:${slug}:new-setup`;
    if (setup) window.sessionStorage.setItem(storageKey, JSON.stringify(setup));
    else window.sessionStorage.removeItem(storageKey);
  } catch {
    // Storage can be unavailable.
  }
}

export function recallNewTestSetup(slug: string): NewTestSetup | null {
  try {
    const setup = JSON.parse(
      window.sessionStorage.getItem(`tests:${slug}:new-setup`) ?? 'null'
    );
    const answers = setup?.answers;
    if (
      typeof setup?.productId !== 'string' ||
      !setup.productId ||
      !answers ||
      !['explore', 'optimize'].includes(answers.goal) ||
      !PLAN_SCALES.includes(answers.scale) ||
      typeof answers.audienceText !== 'string' ||
      !(
        answers.audienceCardIndex === null ||
        (Number.isInteger(answers.audienceCardIndex) &&
          answers.audienceCardIndex >= 0)
      ) ||
      !Array.isArray(answers.seedAssetVersionIds) ||
      !answers.seedAssetVersionIds.every(
        (id: unknown) => typeof id === 'string'
      )
    ) {
      return null;
    }
    return setup as NewTestSetup;
  } catch {
    return null;
  }
}
