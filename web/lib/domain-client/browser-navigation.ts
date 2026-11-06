/**
 * Thin seam around browser navigation for `useDownload` (spec §7.12).
 *
 * `window.location` is an unforgeable, non-configurable browser property
 * (per the HTML spec, and enforced by jsdom) — it cannot be deleted,
 * replaced, or have `assign` stubbed from a test. Routing the call through
 * this one-line module lets a test `jest.mock()` it the same way it already
 * mocks `gateway-provider` and `upload-deps`, without jsdom attempting a
 * real (and always-failing, in a test DOM) cross-origin navigation.
 */
export function navigateTo(url: string): void {
  window.location.assign(url);
}
