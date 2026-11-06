/**
 * Full page navigation. A separate module so tests can observe it: jsdom's
 * `window.location` cannot be stubbed in place.
 */
export function assignLocation(url: string): void {
  window.location.assign(url);
}

/** Like `assignLocation`, without leaving the current page in history. */
export function replaceLocation(url: string): void {
  window.location.replace(url);
}
