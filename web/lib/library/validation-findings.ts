import type { useTranslation } from 'next-i18next';

type Translate = ReturnType<typeof useTranslation>['t'];

/**
 * One Creative Library validation finding as the server's
 * `Finding.as_dict()` writes it (`app/library/contracts.py`), without its
 * severity. `path` locates it in the draft content (`facts[3].currency`) and
 * is empty for a finding about the draft as a whole. `message` is the
 * server's own sentence, written in Chinese.
 */
export interface LibraryFinding {
  code: string;
  message: string;
  path: string;
}

/**
 * `library` copy of a finding: `validation-findings.<code>`. A code with a
 * dot (`brand_policy.invalid_claim_switches`) nests one level down.
 */
export const FINDINGS_KEY = 'validation-findings';

/** The sentence for a code without copy (en only; see `findingText`). */
export const FINDINGS_DEFAULT_KEY = 'validation-findings-default';

/**
 * Fixed prefixes of the codes the server builds with an f-string
 * (`f"reference_evidence_missing_{field}"`). Each has one sentence,
 * `validation-findings.<prefix>*`, given the rest of the code as
 * `{{field}}`. `__tests__/locales/library-validation-findings.spec.ts`
 * keeps this list equal to the server's f-strings.
 */
export const FINDING_CODE_TEMPLATES: readonly string[] = [
  'brand_policy.missing_',
  'reference_derivative_',
  'reference_derivative_mismatch_',
  'reference_derivative_missing_',
  'reference_evidence_',
  'reference_evidence_mismatch_',
  'reference_evidence_missing_',
];

const CODE = /^[a-z0-9_]+(?:\.[a-z0-9_]+)*$/;
const MISSING = '\u0000missing';

const lookup = (t: Translate, key: string, field?: string): string | null => {
  const value: unknown = t(key, {
    defaultValue: MISSING,
    // A key that names a group of codes (`brand_policy`) is not copy.
    returnObjects: true,
    // `replace` keeps the value apart from i18next's own options.
    replace: field === undefined ? {} : { field },
  });
  return typeof value === 'string' && value !== MISSING ? value : null;
};

/** The longest template prefix `code` extends, if any. */
const templateOf = (code: string): string | undefined =>
  FINDING_CODE_TEMPLATES.filter(
    (prefix) => code.length > prefix.length && code.startsWith(prefix)
  ).sort((a, b) => b.length - a.length)[0];

/**
 * The interface's sentence for one finding: its code's copy, else its
 * f-string template's. A code with neither (one the server built at run time
 * that `FINDING_CODE_TEMPLATES` does not know) reads as the server's own
 * sentence in zh-CN and as a general sentence in any other language, so an
 * en page never shows Chinese. The finding's `path` is shown next to it by
 * the caller (`components/library/FindingText.tsx`).
 */
export function findingText(
  t: Translate,
  language: string | undefined,
  finding: LibraryFinding
): string {
  const { code } = finding;
  if (CODE.test(code)) {
    const exact = lookup(t, `${FINDINGS_KEY}.${code}`);
    if (exact !== null) return exact;
    const template = templateOf(code);
    if (template !== undefined) {
      const worded = lookup(
        t,
        `${FINDINGS_KEY}.${template}*`,
        code.slice(template.length)
      );
      if (worded !== null) return worded;
    }
  }
  if (language === 'zh-CN' && finding.message.trim()) return finding.message;
  return t(FINDINGS_DEFAULT_KEY);
}

/** A finding of a command-impact projection, read field by field. */
export function toLibraryFinding(value: unknown): LibraryFinding {
  // Only a code: no sentence of the server's and no location.
  if (typeof value === 'string') return { code: value, message: '', path: '' };
  const record =
    value && typeof value === 'object'
      ? (value as Record<string, unknown>)
      : {};
  const text = (field: unknown) => (typeof field === 'string' ? field : '');
  return {
    code: text(record.code),
    message: text(record.message),
    path: text(record.path),
  };
}
