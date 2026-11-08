import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {
  FINDINGS_DEFAULT_KEY,
  FINDINGS_KEY,
  FINDING_CODE_TEMPLATES,
} from '@/lib/library/validation-findings';
import { CJK, placeholdersOf, readNamespace } from './locale-files';
import {
  findingCodesOf,
  libraryFindingCodes,
  maskPython,
} from './library-finding-codes';

/**
 * Every Creative Library validation finding the server can give has en and
 * zh-CN copy in `library` (#20): `validation-findings.<code>` for a code
 * written out in full, and `validation-findings.<prefix>*` (with
 * `{{field}}` for the rest of the code) for one built by an f-string. The
 * codes are read from the `Finding(...)` calls under `app/library`
 * (`./library-finding-codes.ts`).
 *
 * When this fails because the server added a code, the failure lists each
 * code without copy and the key to add to both
 * `web/locales/{en,zh-CN}/library.json`. A code in a form the reader does
 * not understand fails "reads every finding code" with its `file:line`.
 */
const PREFIX = `${FINDINGS_KEY}.`;
const WEB = path.resolve(__dirname, '../..');

interface ServerCodes {
  codes: string[];
  templates: string[];
}

interface ExpectedKey {
  key: string;
  label: string;
  placeholders: string[];
}

/** The copy key of each server code, with the placeholders it must have. */
const expectedKeys = ({ codes, templates }: ServerCodes): ExpectedKey[] => [
  ...codes.map((code) => ({
    key: PREFIX + code,
    label: `"${code}"`,
    placeholders: [],
  })),
  ...templates.map((prefix) => ({
    key: `${PREFIX}${prefix}*`,
    label: `"${prefix}{…}" (f-string)`,
    placeholders: ['field'],
  })),
];

/** What is wrong with one language's finding copy, one line each. */
function copyProblems(
  locale: string,
  server: ServerCodes,
  keys: Map<string, unknown>
): string[] {
  const problems: string[] = [];
  const expected = expectedKeys(server);
  const checkLanguage = (key: string, value: unknown) => {
    if (typeof value !== 'string' || !value.trim()) {
      problems.push(`${locale}: ${key} is empty`);
    } else if (locale === 'en' && CJK.test(value)) {
      problems.push(`${locale}: ${key} is not English: ${value}`);
    } else if (locale !== 'en' && !CJK.test(value)) {
      problems.push(`${locale}: ${key} is not Chinese: ${value}`);
    }
  };
  for (const { key, label, placeholders } of expected) {
    if (!keys.has(key)) {
      problems.push(`${locale}: no copy for finding code ${label}: add ${key}`);
      continue;
    }
    const value = keys.get(key);
    checkLanguage(key, value);
    const found = placeholdersOf(value);
    if (found.join() !== placeholders.join()) {
      problems.push(
        `${locale}: ${key} has placeholders [${found}], expected [${placeholders}]`
      );
    }
  }
  const expectedNames = new Set(expected.map(({ key }) => key));
  for (const key of Array.from(keys.keys())) {
    if (key.startsWith(PREFIX) && !expectedNames.has(key)) {
      problems.push(
        `${locale}: ${key} is copy for a code the server never gives`
      );
    }
  }
  if (!keys.has(FINDINGS_DEFAULT_KEY)) {
    problems.push(
      `${locale}: no ${FINDINGS_DEFAULT_KEY} for a code without copy`
    );
  } else {
    checkLanguage(FINDINGS_DEFAULT_KEY, keys.get(FINDINGS_DEFAULT_KEY));
  }
  return problems;
}

const server = libraryFindingCodes();

describe('reading finding codes from app/library', () => {
  it('reads every finding code in app/library', () => {
    expect(server.unreadable).toEqual([]);
    expect(server.codes.length).toBeGreaterThan(150);
    expect(server.codes).toEqual(
      expect.arrayContaining([
        // One line.
        'binding_duplicate',
        // A call over several lines.
        'reference_file_evidence',
        'ds_execution_mode_mismatch',
        // A keyword `code=`.
        'brand_policy.invalid_claim_switches',
        // `problem.code` from `fact_value_problems()` (#26).
        'fact_value_missing',
        'fact_bool_invalid',
        'fact_days_invalid',
        'fact_days_range',
        'fact_text_required',
        'fact_amount_parse',
        // Literal codes added by #26.
        'fact_type_unknown',
        'trp_fact_type_unknown',
        // service.py, templates.py, prompt_policy.py.
        'dependency_warning',
        'constraint_cycle',
        'protected_phrase_restored',
      ])
    );
    expect(server.templates).toEqual(
      expect.arrayContaining([
        // `code=f"brand_policy.missing_{required}"` over several lines.
        'brand_policy.missing_',
        // contracts.py and service.py.
        'reference_evidence_',
        'reference_evidence_missing_',
      ])
    );
  });

  it('reads multi-line calls, keyword codes, f-string prefixes and fact value problems', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'finding-codes-'));
    try {
      fs.writeFileSync(
        path.join(root, 'fact_types.py'),
        [
          '@dataclass(frozen=True)',
          'class FactValueProblem:',
          '    """One field of one fact row."""',
          '',
          '    field: str',
          '    code: str',
          '    message: str',
          '',
          '',
          'def _problem(field, kind_code):',
          '    if kind_code:',
          '        return FactValueProblem(field, kind_code, "m")',
          '    if field:',
          '        return FactValueProblem(field=field, code="fact_keyword", message="m")',
          '    return FactValueProblem(field, "fact_positional", f"{field} 不对")',
          '',
        ].join('\n')
      );
      const sample = [
        'findings.append(Finding("one_line", "消息", "a"))',
        'findings.append(Finding(',
        '    "multi_line",',
        '    "一个没有闭合的括号 (",',
        '    f"{path}.x",',
        '))',
        'findings.append(Finding(',
        '    code=f"prefix.missing_{required}",',
        '    message=f"Required field \'{required}\' missing",',
        '    path=required,',
        '))',
        '# findings.append(Finding("in_comment", "m"))',
        'text = "Finding(\\"in_string\\", \\"m\\")"',
        "findings.append(Finding(message='m', code='keyword_last'))",
        'for problem in fact_value_problems(fact_type, fact):',
        '    findings.append(Finding(problem.code, problem.message, f"{p}.{problem.field}"))',
        'findings.append(ValidationFinding("not_a_finding", "m"))',
        'findings.append(Finding(code_var, "m"))',
        'findings.append(Finding(f"{kind}_middle_{x}", "m"))',
        'findings.append(Finding("a" "b", "m"))',
        'findings.append(Finding())',
        '',
      ].join('\n');
      const read = findingCodesOf({ 'sample.py': sample }, root);
      expect(read).toEqual({
        codes: [
          'fact_keyword',
          'fact_positional',
          'keyword_last',
          'multi_line',
          'one_line',
        ],
        templates: ['prefix.missing_'],
        unreadable: [
          'fact_types.py:12: kind_code',
          'sample.py:18: code_var',
          'sample.py:19: f"{kind}_middle_{x}"',
          'sample.py:20: "a" "b"',
          'sample.py:21: (no code)',
        ],
      });

      // `problem.code` is only read where it comes from fact_value_problems().
      expect(
        findingCodesOf(
          { 'other.py': 'out.append(Finding(problem.code, "m"))\n' },
          root
        ).unreadable
      ).toEqual(['other.py:1: problem.code']);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  it('masks string bodies and comments without moving any line', () => {
    const source = 'a = f"x(#)"  # Finding(\nb = buf"\'"\n';
    const masked = maskPython(source);
    expect(masked).toHaveLength(source.length);
    expect(masked.split('\n')).toHaveLength(source.split('\n').length);
    expect(masked).not.toContain('Finding(');
    expect(masked.startsWith('a = f"    "')).toBe(true);
  });

  it('gives codes that are locale keys under validation-findings', () => {
    const KEY = /^[a-z0-9_]+(?:\.[a-z0-9_]+)*$/;
    expect(server.codes.filter((code) => !KEY.test(code))).toEqual([]);
    expect(
      server.templates.filter((prefix) => !KEY.test(`${prefix}x`))
    ).toEqual([]);
    // A code may not also be the group another code's copy nests under.
    expect(
      server.codes.filter((code) =>
        [...server.codes, ...server.templates].some((other) =>
          other.startsWith(`${code}.`)
        )
      )
    ).toEqual([]);
  });

  it('keeps FINDING_CODE_TEMPLATES (lib/library/validation-findings.ts) equal to the server f-strings', () => {
    expect([...FINDING_CODE_TEMPLATES].sort()).toEqual(server.templates);
  });
});

describe('the copy check', () => {
  const sample: ServerCodes = {
    codes: ['fact_currency', 'brand_policy.invalid_claim_switches'],
    templates: ['reference_evidence_'],
  };
  const english = () =>
    new Map<string, unknown>([
      [`${PREFIX}fact_currency`, 'Use a three-letter currency code.'],
      [
        `${PREFIX}brand_policy.invalid_claim_switches`,
        'Claim switches must be an object.',
      ],
      [`${PREFIX}reference_evidence_*`, 'Record the file {{field}}.'],
      [FINDINGS_DEFAULT_KEY, 'This value did not pass validation.'],
    ]);

  it('passes complete copy', () => {
    expect(copyProblems('en', sample, english())).toEqual([]);
  });

  it('fails a server code without copy and names the key to add', () => {
    expect(
      copyProblems(
        'en',
        {
          codes: [...sample.codes, 'fact_new_rule'],
          templates: [...sample.templates, 'reference_new_'],
        },
        english()
      )
    ).toEqual([
      'en: no copy for finding code "fact_new_rule": add validation-findings.fact_new_rule',
      'en: no copy for finding code "reference_new_{…}" (f-string): add validation-findings.reference_new_*',
    ]);
  });

  it('fails copy for a code the server never gives', () => {
    const keys = english().set(`${PREFIX}fact_gone`, 'Gone.');
    expect(copyProblems('en', sample, keys)).toEqual([
      'en: validation-findings.fact_gone is copy for a code the server never gives',
    ]);
  });

  it('fails Chinese in en, English in zh-CN and a template without {{field}}', () => {
    const en = english()
      .set(`${PREFIX}fact_currency`, '币种必须是 3 位代码。')
      .set(`${PREFIX}reference_evidence_*`, 'Record the file evidence.');
    expect(copyProblems('en', sample, en)).toEqual([
      'en: validation-findings.fact_currency is not English: 币种必须是 3 位代码。',
      'en: validation-findings.reference_evidence_* has placeholders [], expected [field]',
    ]);
    const zh = new Map<string, unknown>([
      [`${PREFIX}fact_currency`, '币种必须是 3 位大写代码。'],
      [`${PREFIX}brand_policy.invalid_claim_switches`, 'claim_switches'],
      [`${PREFIX}reference_evidence_*`, '缺少文件的 {{field}} 证据。'],
      [FINDINGS_DEFAULT_KEY, 'This value did not pass validation.'],
    ]);
    expect(copyProblems('zh-CN', sample, zh)).toEqual([
      'zh-CN: validation-findings.brand_policy.invalid_claim_switches is not Chinese: claim_switches',
      'zh-CN: validation-findings-default is not Chinese: This value did not pass validation.',
    ]);
  });
});

describe.each(['en', 'zh-CN'])('%s library copy', (locale) => {
  it('words every finding code the server gives, and no other', () => {
    expect(
      copyProblems(locale, server, readNamespace(locale, 'library'))
    ).toEqual([]);
  });
});

test('the draft panel and the publish dialog word findings through FindingText', () => {
  const read = (file: string) =>
    fs.readFileSync(path.join(WEB, 'components/library', file), 'utf8');
  for (const file of [
    'draft/ValidationPanel.tsx',
    'commands/PublishDialog.tsx',
    'commands/ArchiveDialog.tsx',
    'commands/WithdrawDialog.tsx',
  ]) {
    const source = read(file);
    expect([file, source.includes('<FindingText')]).toEqual([file, true]);
    // The server's own sentence is never rendered directly.
    expect([file, /\.message\b/.test(source)]).toEqual([file, false]);
  }
  const findingTextSource = read('FindingText.tsx');
  expect(findingTextSource).toContain(
    'findingText(t, i18n?.language, finding)'
  );
});
