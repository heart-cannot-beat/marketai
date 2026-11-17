/** Standard base64 of the UTF-8 bytes — what a credential's `plaintext_b64`
 * carries. No `TextEncoder` so the same code runs under jsdom. Shared by
 * Settings and the start page's key row (#195). */
export function utf8ToBase64(value: string): string {
  const bytes = encodeURIComponent(value).replace(
    /%([0-9A-F]{2})/g,
    (_match, hex: string) => String.fromCharCode(parseInt(hex, 16))
  );
  return btoa(bytes);
}
