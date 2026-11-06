/**
 * The scan's market (`country`, ISO 3166-1 alpha-2) defaults to the plan's
 * market, which is free text. The server maps drafts to snapshots with the
 * deployment's alias table (spec §7.2); here a two-letter code or a common
 * name only preselects the field, and anything else leaves it to the user.
 */
export const MARKET_OPTIONS = [
  'US',
  'CA',
  'GB',
  'AU',
  'NZ',
  'IE',
  'DE',
  'FR',
  'IT',
  'ES',
  'NL',
  'SE',
  'PL',
  'JP',
  'KR',
  'SG',
  'MY',
  'PH',
  'MX',
  'BR',
  'IN',
  'AE',
  'SA',
] as const;

// Lower-cased, whitespace removed. Chinese names are written as escapes in
// plain strings: source files stay free of CJK
// (`__tests__/locales/source-no-cjk.spec.ts`), and object keys would be
// unescaped by Prettier.
const ALIASES = new Map<string, string>([
  ['usa', 'US'],
  ['unitedstates', 'US'],
  ['america', 'US'],
  ['northamerica', 'US'],
  ['\u7f8e\u56fd', 'US'],
  ['\u5317\u7f8e', 'US'],
  ['canada', 'CA'],
  ['\u52a0\u62ff\u5927', 'CA'],
  ['uk', 'GB'],
  ['unitedkingdom', 'GB'],
  ['britain', 'GB'],
  ['greatbritain', 'GB'],
  ['england', 'GB'],
  ['\u82f1\u56fd', 'GB'],
  ['australia', 'AU'],
  ['\u6fb3\u5927\u5229\u4e9a', 'AU'],
  ['\u6fb3\u6d32', 'AU'],
  ['germany', 'DE'],
  ['\u5fb7\u56fd', 'DE'],
  ['france', 'FR'],
  ['\u6cd5\u56fd', 'FR'],
  ['japan', 'JP'],
  ['\u65e5\u672c', 'JP'],
]);

/** The country a plan market names, or null when it cannot be told. */
export function countryFromMarket(market: string | null | undefined) {
  const text = (market ?? '').trim();
  if (/^[A-Za-z]{2}$/.test(text)) return text.toUpperCase();
  return ALIASES.get(text.toLowerCase().replace(/\s+/g, '')) ?? null;
}

/** "US" -> "United States", in the interface language. */
export function countryName(locale: string, code: string) {
  try {
    return new Intl.DisplayNames([locale], { type: 'region' }).of(code) ?? code;
  } catch {
    return code;
  }
}
