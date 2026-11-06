/**
 * One-shot, per-tab handoffs between product pages, kept out of the URL so
 * no internal id ever shows up where the user can see it (front-flow
 * contract §1 principle 2):
 *
 * - `highlight`: the product just confirmed, highlighted once on the
 *   product home;
 * - `new-draft`: the product the Generate workbench's New Draft preselects;
 * - `new-test`: the product the legacy guided test setup (`NewTestFlow`,
 *   Issue #165) opens for; nothing writes it since `/w/{slug}/tests/new`
 *   redirects to the marketing test (#337);
 * - `marketing-test`: the product a product card's "Start test" starts a
 *   marketing test from (`/w/{slug}/experiments`, J-32 #230).
 *
 * Storage can be unavailable (private mode, blocked site data); every read
 * and write is guarded and a missing value only loses the convenience.
 */

type HandoffKind = 'highlight' | 'new-draft' | 'new-test' | 'marketing-test';

function key(kind: HandoffKind, slug: string): string {
  return `products:${slug}:${kind}`;
}

export function stashProductHandoff(
  kind: HandoffKind,
  slug: string,
  productId: string
): void {
  try {
    window.sessionStorage.setItem(key(kind, slug), productId);
  } catch {
    // Convenience only.
  }
}

/** Reads and clears the handoff (it applies once). */
export function takeProductHandoff(
  kind: HandoffKind,
  slug: string
): string | null {
  try {
    const value = window.sessionStorage.getItem(key(kind, slug));
    if (value !== null) window.sessionStorage.removeItem(key(kind, slug));
    return value;
  } catch {
    return null;
  }
}

/** The in-flight intake of this tab, so a refresh resumes the same card. */
export function rememberIntake(slug: string, intakeId: string | null): void {
  try {
    const storageKey = `products:${slug}:intake`;
    if (intakeId) window.sessionStorage.setItem(storageKey, intakeId);
    else window.sessionStorage.removeItem(storageKey);
  } catch {
    // Convenience only.
  }
}

export function recallIntake(slug: string): string | null {
  try {
    return window.sessionStorage.getItem(`products:${slug}:intake`);
  } catch {
    return null;
  }
}
