import fs from 'node:fs';
import path from 'node:path';
import en from '../../locales/en/common.json';
import zhCN from '../../locales/zh-CN/common.json';
import { familyOf } from '../../lib/i18n/domain-errors';
import { CJK, placeholdersOf } from './locale-files';

/**
 * Every error code the interface can receive has `error-<code>` copy in both
 * languages (#253): the page renders the code, never the service's `detail`.
 *
 * Codes are read from source rather than listed by hand. A code counts when
 * it is a literal in one of the shapes the services raise it in:
 *
 *   XxxError("<code>", ...)     XxxProblem(409, "<code>", ...)
 *   XxxRefused("<code>", ...)   MetaModeNotPermitted("<code>", ...)
 *   XxxError(f"<code>:{id}")    _problem(400, "<code>", ...)  _coded(409, "<code>", ...)
 *   super().__init__("<code>", ...)
 *   code="<code>"  default_code = "<code>"  error_code=  failure_code=
 *   conflict_code="<code>"
 *   row.error_code = "<code>"   call.state, call.error_code = "failed", "<code>"
 *   data, known_error = None, "<code>"
 *   {"code": "<code>"}          error_summary="<code>: ..."
 *   x.error_code or "<code>"    getattr(exc, "code", "<code>")
 *   _X_CODES.get(key, "<code>")
 *   FAILURE_X = "<code>"
 *   self._send_503(send, "<code>", ...)
 *
 * An exception is any class under `app/` whose bases lead to a Python
 * exception (#363), so a new one is read without a name to add here; one
 * raised only inside the Worker is listed in `NOT_SHOWN_CLASSES`.
 *
 * Also read: the BFF's own refusals (`(res, 502, '<code>'`, `code: '<code>'`),
 * and the control plane's coded refusals (`code: '<code>'` in
 * `lib/entitlements`: the seat limit, #157);
 * the keys of the code tables -- CSV preflight issues (`ISSUE_PARAMS`) and
 * results-import refusals (`REASONS`), which the services refuse to send
 * unless declared there (#255), and outside images' failures
 * (`FAILURE_COPY`); the values of the tables a refusal is mapped through
 * (`_COORDINATOR_ERROR_CODES`); and the members of the code sets the identity
 * middleware answers from (#309: `_RETRYABLE_403_CODES`, whose
 * `*_sync_pending` codes are returned by a check, then sent).
 *
 * A code these shapes find that no page shows is listed in `NOT_SHOWN` with
 * its reason. A code built at run time (`f"openai_{status}"`) reads as its
 * family in `lib/i18n/domain-errors.ts`; a code passed only as a variable is
 * not a code the page can rely on -- raise it as a literal. Every code the
 * services' own tests expect is found here or listed in `NOT_SHOWN`, so a
 * shape this file cannot read fails here rather than on the page.
 */
const REPO = path.resolve(__dirname, '../../..');
const WEB = path.resolve(__dirname, '../..');

const CODE = String.raw`f?["']([a-z][a-z0-9_]*)(?=["':])`;

/** Python's own exceptions carry a code only as their whole message. */
const BUILTIN = String.raw`(?:Value|Type|Key|Runtime|Assertion|Lookup|Attribute|Index|OS)Error`;

/** A base defined outside `app/` that makes a class an exception. */
const EXCEPTION_BASE = /^(?:[A-Z]\w*Error|\w*Exception)$/;

/** Library exceptions the services raise with a code. */
const LIBRARY_EXCEPTIONS = ['HTTPException', 'PydanticCustomError'];

/**
 * Exceptions raised only inside the Worker. Their first string is a Job's
 * code (`JobFailed`, `JobRetry`, `JobObsolete`) or the check a Scale
 * snapshot failed (`InvalidScaleSnapshot`); no response or record a page
 * reads carries it.
 */
const NOT_SHOWN_CLASSES = [
  'JobFailed',
  'JobRetry',
  'JobObsolete',
  'InvalidScaleSnapshot',
];

/**
 * Constructors whose first string names a resource or a limit, not a code
 * (`StaleRevisionError("batch", ...)` answers `stale_revision`).
 */
const NOT_CODE_FIRST =
  /^(?:StaleRevisionError|GenerationResourceNotFoundError|GuardrailExceededError)\(/;

const filesUnder = (directory: string, extensions: string[]): string[] =>
  fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(directory, entry.name);
    if (entry.isDirectory())
      return entry.name === '__pycache__' || entry.name === 'generated'
        ? []
        : filesUnder(full, extensions);
    return extensions.some((extension) => entry.name.endsWith(extension))
      ? [full]
      : [];
  });

const PYTHON_FILES = filesUnder(path.join(REPO, 'app'), ['.py']);

/** Every class under `app/` whose bases lead to an exception. */
const exceptionClasses = () => {
  const bases = new Map<string, string[]>();
  for (const file of PYTHON_FILES)
    for (const match of Array.from(
      fs
        .readFileSync(file, 'utf8')
        .matchAll(/^[ \t]*class\s+(\w+)\s*\(([^)]*)\)\s*:/gm)
    ))
      bases.set(match[1], [
        ...(bases.get(match[1]) ?? []),
        ...match[2].split(',').map((base) => base.trim().split('.').pop()!),
      ]);
  const classes = new Set<string>();
  const isException = (base: string) =>
    classes.has(base) || (!bases.has(base) && EXCEPTION_BASE.test(base));
  for (let grew = true; grew; ) {
    grew = false;
    for (const [name, of] of Array.from(bases))
      if (!classes.has(name) && of.some(isException)) {
        classes.add(name);
        grew = true;
      }
  }
  return classes;
};

const RAISERS = Array.from(exceptionClasses())
  .concat(LIBRARY_EXCEPTIONS)
  .filter((name) => !NOT_SHOWN_CLASSES.includes(name))
  .sort();

const PYTHON_PATTERNS = [
  new RegExp(
    String.raw`\b(?:${RAISERS.join('|')}|_problem|_request_problem|_coded)\(\s*(?:request\s*,\s*)?(?:\d{3}\s*,\s*)?` +
      CODE,
    'g'
  ),
  new RegExp(String.raw`\b${BUILTIN}\(\s*["']([a-z][a-z0-9_]*)["']`, 'g'),
  // An exception's own code: `super().__init__("<code>", export_id)` (#363).
  new RegExp(String.raw`\bsuper\(\)\.__init__\(\s*` + CODE, 'g'),
  new RegExp(
    String.raw`(?<![\w.])(?:default_|error_|failure_|conflict_)?code\s*=\s*` +
      CODE,
    'g'
  ),
  // A record's own code: `row.error_code = "<code>"`, and the second value of
  // `call.state, call.error_code = "failed", "<code>"`.
  new RegExp(
    String.raw`^\s*[\w.]+\.(?:error_|failure_)?code\s*=\s*` + CODE,
    'gm'
  ),
  new RegExp(
    String.raw`\.(?:error|failure)_code\s*=\s*["'][a-z_]+["'],\s*` + CODE,
    'g'
  ),
  // A Worker's known failure, written to its call later (#363).
  new RegExp(String.raw`\bknown_error\s*=\s*(?:\w+\s*,\s*)?` + CODE, 'g'),
  new RegExp(String.raw`["'](?:error_|failure_)?code["']\s*:\s*` + CODE, 'g'),
  new RegExp(String.raw`\berror_summary\s*=\s*` + CODE, 'g'),
  // A fallback: `code = outcome.rejection_code or "<code>"`, and anywhere
  // `failed.code or "<code>"`, `getattr(exc, "code", "<code>")` and
  // `_X_CODES.get(type(exc), "<code>")` (#363).
  new RegExp(
    String.raw`(?<![\w.])(?:error_|failure_)?code\s*=\s*[\w.]+\s+or\s+` + CODE,
    'g'
  ),
  new RegExp(String.raw`(?<!\w)(?:error_|failure_)?code\s+or\s+` + CODE, 'g'),
  new RegExp(
    String.raw`\bgetattr\(\s*[\w.]+\s*,\s*["'](?:error_|failure_)?code["']\s*,\s*` +
      CODE,
    'g'
  ),
  new RegExp(String.raw`\b\w*_CODES\.get\([^,\n]+,\s*` + CODE, 'g'),
  new RegExp(String.raw`^\s*FAILURE_[A-Z_]+\s*=\s*` + CODE, 'gm'),
  // The identity middleware writes its own responses (#309).
  new RegExp(String.raw`\b_send_\d{3}\(\s*send\s*,\s*` + CODE, 'g'),
];

const WEB_PATTERNS = [
  /\(\s*(?:res,\s*)?\d{3},\s*'([a-z][a-z0-9_]*)'/g,
  /\bsuper\(\s*'([a-z][a-z0-9_]*)'/g,
  /\bcode:\s*'([a-z][a-z0-9_]*)'/g,
];
const WEB_DIRECTORIES = [
  'pages/api/domain',
  'lib/domain-client',
  'lib/delegated-identity',
  'lib/entitlements',
  'hooks/domain',
];

/** Codes the services' own tests expect a response or a record to carry. */
const TEST_PATTERNS = [
  /\[["'](?:error_|failure_)?code["']\]\s*(?:==|,)\s*["']([a-z][a-z0-9_]*)["']/g,
  /\.(?:error_|failure_)?code\s*==\s*["']([a-z][a-z0-9_]*)["']/g,
  /\bassert(?:Equal|In)\([^,\n]*\.(?:error_|failure_)?code\s*,\s*["']([a-z][a-z0-9_]*)["']/g,
];

/** Pydantic's own field error types a form can show beside a field. */
const FIELD_CODES = [
  'missing',
  'extra_forbidden',
  'value_error',
  'assertion_error',
  'literal_error',
  'enum',
  'string_type',
  'string_too_short',
  'string_too_long',
  'string_pattern_mismatch',
  'too_short',
  'too_long',
  'int_type',
  'int_parsing',
  'float_parsing',
  'decimal_parsing',
  'decimal_max_digits',
  'decimal_max_places',
  'bool_parsing',
  'greater_than',
  'greater_than_equal',
  'less_than',
  'less_than_equal',
  'list_type',
  'dict_type',
  'model_type',
  'url_parsing',
  'date_parsing',
  'datetime_parsing',
  'json_invalid',
];

/** Codes a conditional expression picks (`"x" if blocked else "y"`). */
const CONDITIONAL_CODES = [
  'credential_rejected',
  'no_image_returned',
  'provider_rejected',
];

/** Families in `domain-errors.ts`, and the fallback for no code at all. */
const FAMILY_CODES = [
  'provider_http_error',
  'meta_api_error',
  'dispatch_failed',
  'dispatch_uncertain',
  'unknown',
];

/**
 * Codes the services raise or test for that no page shows as
 * `error-<code>`, and why (#363). A code here has no copy.
 */
const NOT_SHOWN: Record<string, string> = {
  // Read by these shapes, but written only where no page reads a code.
  competitor_relevance_budget:
    "a scan step's and its call's code; the scan shows each step's state, and does not end with it",
  competitor_tag_budget:
    "a scan step's and its call's code; the scan shows each step's state, and does not end with it",
  competitor_discovery_budget:
    "a scan step's and its call's code; the scan shows each step's state, and does not end with it",
  outcome_unknown:
    "a reconciled scan call's code (`scan_reconcile.py`); no page reads the call ledger",
  ai_qc_completed:
    "an AI check's Operation reason; an Operation's reason is not shown",
  planning_completed:
    "the planner's Operation reason; an Operation's reason is not shown",
  error:
    '`_STATUS_CODES.get(status, "error")` in `library/api.py`: every HTTPException there has a dict detail or a known status',
  draft_creation_conflict:
    'raised by `create_draft_enrollment_from_request`, the pre-#86 path no route calls',
  withdrawal_conflict:
    'raised by `handle_upstream_withdrawal`, which nothing in `app/` calls',
  no_delivery_closure_conflict:
    "raised in the evaluation Worker's no-delivery closure, never over HTTP",
  // Expected by the services' tests, but never sent where a page reads it.
  content_too_large:
    'the development-only blob transfer process (`worker/local_blob_transfer.py`); the upload hook reads only the status',
  content_too_small:
    'the development-only blob transfer process (`worker/local_blob_transfer.py`); the upload hook reads only the status',
  content_constraint_mismatch:
    'the development-only blob transfer process (`worker/local_blob_transfer.py`); the upload hook reads only the status',
  checksum_mismatch:
    'an upload verification reason, answered as `upload_intent_rejected`; also the development-only blob transfer process',
  unsupported_country:
    'a ScrapeCreators failure, mapped to a `market_data_*` code and sent only as its `provider_code` param',
  invalid_response:
    'a ScrapeCreators failure, mapped to a `market_data_*` code and sent only as its `provider_code` param',
  constraint_cycle:
    "a Creative Library finding; the panel shows the finding's message, publishing only confirms by code",
  unsatisfiable:
    "a Creative Library finding; the panel shows the finding's message, publishing only confirms by code",
  protected_phrase_restored:
    "a Creative Library finding; the panel shows the finding's message, publishing only confirms by code",
  ds_execution_mode:
    "a Creative Library finding; the panel shows the finding's message, publishing only confirms by code",
  ds_execution_mode_mismatch:
    "a Creative Library finding; the panel shows the finding's message, and the data source form its own prompt (#396)",
  algorithm_version_unavailable:
    'an InvalidScaleSnapshot check, raised only inside the evaluation Worker',
  negative_metric:
    'an InvalidScaleSnapshot check, raised only inside the evaluation Worker',
  invalid_prior_evaluation:
    'an InvalidScaleSnapshot check, raised only inside the evaluation Worker',
  invalid_freshness_reference:
    'an InvalidScaleSnapshot check, raised only inside the evaluation Worker',
  invalid_policy:
    'an InvalidScaleSnapshot check, raised only inside the evaluation Worker',
  test_error: "a test's own made-up code (`tests/test_coordinator.py`)",
  some_code: "a test's own made-up code (`tests/test_delegated_identity.py`)",
  usage_metric_unknown:
    'a Usage fact the metric registry refuses (`UsageFactError`, #8): a programming error that rolls back its provider-call settlement, never sent to a page',
  usage_unit_mismatch:
    'a Usage fact the metric registry refuses (`UsageFactError`, #8): a programming error that rolls back its provider-call settlement, never sent to a page',
  usage_quantity_invalid:
    'a Usage fact the metric registry refuses (`UsageFactError`, #8): a programming error that rolls back its provider-call settlement, never sent to a page',
  usage_quantity_negative:
    'a Usage fact the metric registry refuses (`UsageFactError`, #8): a programming error that rolls back its provider-call settlement, never sent to a page',
  usage_execution_mode_unknown:
    'a Usage fact the metric registry refuses (`UsageFactError`, #8): a programming error that rolls back its provider-call settlement, never sent to a page',
  usage_semantic_key_invalid:
    'a Usage fact the metric registry refuses (`UsageFactError`, #8): a programming error that rolls back its provider-call settlement, never sent to a page',
  usage_occurred_at_naive:
    'a Usage fact the metric registry refuses (`UsageFactError`, #8): a programming error that rolls back its provider-call settlement, never sent to a page',
  usage_workspace_mismatch:
    'a Usage fact the metric registry refuses (`UsageFactError`, #8): a programming error that rolls back its provider-call settlement, never sent to a page',
  usage_source_invalid:
    'a Usage fact for an AssetVersion Generation did not produce (`UsageFactError`, #8 S2): a programming error that rolls back the version it was written with, never sent to a page',
  usage_execution_mode_mismatch:
    'a Usage fact in a bucket its metric does not record (`UsageFactError`, #148): a programming error that rolls back the transaction it was written in, never sent to a page',
};

const codesIn = (
  source: string,
  patterns: RegExp[],
  skip: (match: RegExpMatchArray) => boolean = () => false
) =>
  patterns.flatMap((pattern) =>
    Array.from(source.matchAll(pattern))
      .filter((match) => !skip(match))
      .map((match) => match[1])
  );

const collect = (
  files: string[],
  patterns: RegExp[],
  skip?: (match: RegExpMatchArray) => boolean
) => {
  const codes = new Map<string, string>();
  for (const file of files)
    for (const code of codesIn(fs.readFileSync(file, 'utf8'), patterns, skip))
      if (!codes.has(code)) codes.set(code, path.relative(REPO, file));
  return codes;
};

const pythonCodes = (source: string) =>
  codesIn(source, PYTHON_PATTERNS, (m) => NOT_CODE_FIRST.test(m[0]));

/** Row-level codes declared as the keys of a dict literal (#255). */
const CODE_TABLES = [
  ['app/marketing/metrics.py', 'ISSUE_PARAMS'],
  ['app/learnings/parsing.py', 'REASONS'],
  ['app/external_creatives/service.py', 'FAILURE_COPY'],
] as const;

/** Codes declared as the values of a dict literal a refusal maps through. */
const VALUE_TABLES = [
  ['app/providers/llm_chat.py', '_COORDINATOR_ERROR_CODES'],
] as const;

const tableBlock = (file: string, name: string) => {
  const source = fs.readFileSync(path.join(REPO, file), 'utf8');
  const start = source.search(new RegExp(`^${name}\\b[^=]*= \\{`, 'm'));
  if (start < 0) throw new Error(`${name} not found in ${file}`);
  return source.slice(start, source.indexOf('\n}\n', start));
};

const tableCodes = () => {
  const codes = new Map<string, string>();
  const read = (
    tables: ReadonlyArray<readonly [string, string]>,
    pattern: RegExp
  ) => {
    for (const [file, name] of tables)
      for (const match of Array.from(tableBlock(file, name).matchAll(pattern)))
        if (!codes.has(match[1])) codes.set(match[1], file);
  };
  read(CODE_TABLES, /^\s+"([a-z][a-z0-9_]*)":/gm);
  read(VALUE_TABLES, /:\s*"([a-z][a-z0-9_]*)",?$/gm);
  return codes;
};

/** Codes declared as the members of a set literal (#309). */
const CODE_SETS = [
  ['app/platform/delegated_identity.py', '_RETRYABLE_403_CODES'],
] as const;

const setCodes = () => {
  const codes = new Map<string, string>();
  for (const [file, name] of CODE_SETS) {
    const source = fs.readFileSync(path.join(REPO, file), 'utf8');
    const start = source.search(new RegExp(`^${name}\\b[^=]*=`, 'm'));
    if (start < 0) throw new Error(`${name} not found in ${file}`);
    const open = source.indexOf('{', start);
    const block = source.slice(open, source.indexOf('}', open));
    for (const match of Array.from(block.matchAll(/"([a-z][a-z0-9_]*)"/g)))
      if (!codes.has(match[1])) codes.set(match[1], file);
  }
  return codes;
};

const serverCodes = () => {
  const codes = collect(PYTHON_FILES, PYTHON_PATTERNS, (m) =>
    NOT_CODE_FIRST.test(m[0])
  );
  for (const [code, file] of Array.from(tableCodes()).concat(
    Array.from(setCodes())
  ))
    if (!codes.has(code)) codes.set(code, file);
  return codes;
};

const webCodes = () =>
  collect(
    WEB_DIRECTORIES.flatMap((directory) =>
      filesUnder(path.join(WEB, directory), ['.ts', '.tsx'])
    ),
    WEB_PATTERNS
  );

const testCodes = () =>
  collect(filesUnder(path.join(REPO, 'tests'), ['.py']), TEST_PATTERNS);

const allCodes = () => {
  const codes = new Map(
    Array.from(serverCodes()).concat(Array.from(webCodes()))
  );
  for (const code of [...FIELD_CODES, ...CONDITIONAL_CODES, ...FAMILY_CODES])
    codes.set(code, 'web/__tests__/locales/error-codes.spec.ts');
  return codes;
};

/** A code the page shows by its own copy, not as its family or not at all. */
const shownByItself = (code: string) => !(code in NOT_SHOWN) && !familyOf(code);

const messages = (locale: Record<string, string>, code: string) =>
  locale[`error-${code}`];

describe('error-<code> copy', () => {
  const codes = allCodes();
  const shown = Array.from(codes).filter(([code]) => shownByItself(code));

  it('finds the codes the services raise', () => {
    const server = serverCodes();
    expect(server.size).toBeGreaterThan(500);
    // One shape each, so a pattern that stops matching is noticed.
    for (const code of [
      'marketing_project_not_found', // MarketingError("<code>", ...)
      'text_too_long', // IntakeProblem(422, "<code>", ...)
      'meta_mode_not_permitted', // MetaModeNotPermitted("<code>", ...): no Error suffix
      'export_not_found', // super().__init__("<code>", ...)
      'library_display_name_required', // code="<code>"
      'library_invalid', // default_code = "<code>"
      'competitor_cluster_budget', // class XxxBudgetError: code = "<code>"
      'pause_conflict', // conflict_code="<code>"
      'entry_type_mismatch', // {"code": "<code>"}
      'candidate_ineligible', // f"<code>:{id}"
      'missing_clicks_column', // ParseError("<code>", ...), once f"missing_{x}_column"
      'qc_blocking_failure', // error_summary="<code>"
      'drafting_failed', // code = failed.code or "<code>"
      'provider_error', // f"{result.error_code or '<code>'}: ..."
      'dispatch_refused', // _COORDINATOR_ERROR_CODES.get(type(exc), "<code>")
      'lease_expired', // row.error_code = "<code>"
      'planner_output_invalid', // call.state, call.error_code = "failed", "<code>"
      'qc_provider_rejected', // data, known_error = None, "<code>"
      'reference_gate_rejected', // FAILURE_X = "<code>"
      'marketing_groups_too_few', // PydanticCustomError("<code>", ...)
      'outside_round_dates', // ISSUE_PARAMS = {"<code>": (...)}
      'unknown_code', // REASONS = {"<code>": "..."}
      'image_undecodable', // FAILURE_COPY = {"<code>": (...)}
      'job_not_found', // _COORDINATOR_ERROR_CODES = {...: "<code>"}
      'projection_sync_unavailable', // self._send_503(send, "<code>", ...)
      'entitlement_sync_pending', // _RETRYABLE_403_CODES = frozenset({"<code>"})
    ])
      expect(server.has(code)).toBe(true);
    // No real code is found by this shape alone.
    expect(pythonCodes('code = getattr(exc, "code", "x_budget")')).toEqual([
      'x_budget',
    ]);
    expect(server.has('batch')).toBe(false);
    // InvalidScaleSnapshot("<code>"), raised only inside the Worker.
    expect(server.has('snapshot_not_canonical')).toBe(false);
    expect(webCodes().has('domain_unavailable')).toBe(true);
  });

  it('every code has English copy', () => {
    const problems = shown
      .filter(([code]) => {
        const message = messages(en as Record<string, string>, code);
        return typeof message !== 'string' || !message || CJK.test(message);
      })
      .map(([code, file]) => `${code} (${file})`);
    expect(problems).toEqual([]);
  });

  it('every code has Simplified Chinese copy', () => {
    const problems = shown
      .filter(([code]) => {
        const message = messages(zhCN as Record<string, string>, code);
        return typeof message !== 'string' || !CJK.test(message);
      })
      .map(([code, file]) => `${code} (${file})`);
    expect(problems).toEqual([]);
  });

  it('no copy is left for a code nothing raises', () => {
    const stale = Object.keys(en).filter(
      (key) => /^error-[a-z]/.test(key) && !codes.has(key.slice(6))
    );
    expect(stale).toEqual([]);
  });

  it('every code the services test for is found or not shown', () => {
    // A code raised in a shape this file cannot read is still expected by a
    // test; it fails here instead of reaching the page as a raw code.
    const missed = Array.from(testCodes())
      .filter(([code]) => !codes.has(code) && shownByItself(code))
      .map(([code, file]) => `${code} (${file})`);
    expect(missed).toEqual([]);
  });

  it('every code not shown is raised or tested for, and has no copy', () => {
    const tested = testCodes();
    const dead = Object.keys(NOT_SHOWN).filter(
      (code) => !codes.has(code) && !tested.has(code)
    );
    expect(dead).toEqual([]);
    const copied = Object.keys(NOT_SHOWN).filter(
      (code) => `error-${code}` in en || `error-${code}` in zhCN
    );
    expect(copied).toEqual([]);
  });

  it('copy interpolates only params the code is sent with', () => {
    // A placeholder the service does not send would show as "{{limit}}".
    const withPlaceholders = Object.entries(en as Record<string, string>)
      .filter(([key, value]) => /^error-[a-z]/.test(key) && /{{/.test(value))
      .map(([key, value]) => `${key}: ${placeholdersOf(value).join(',')}`)
      .sort();
    expect(withPlaceholders).toEqual([...PARAMETERISED].sort());
  });
});

/**
 * Copy with placeholders, and the params every raise site of that code
 * sends (checked by the services' own tests). Add a line here together with
 * the params.
 */
const PARAMETERISED = [
  'error-ambiguous_column_mapping: columns',
  'error-column_currency_mismatch: expected,found',
  'error-feedback_note_language_mismatch: ad_language',
  'error-mapped_column_missing: column',
  'error-market_data_price_operation_missing: operations',
  'error-market_data_provider_error: provider_code',
  // #134: app.scale.targets sends the Target's currency, and the floor
  // when it has one.
  'error-minimum_economic_spend_required: currency,floor',
  'error-minimum_economic_spend_below_platform_floor: currency,floor',
  'error-spend_floor_currency_unsupported: currency',
  'error-competitor_name_too_long: limit',
  'error-competitor_resolution_expired: hours',
  'error-outside_round_dates: end,start',
  'error-reporting_context_mismatch: attribution_window,currency,timezone',
  'error-row_attribution_mismatch: expected,found',
  'error-row_currency_mismatch: expected,found',
  // #157: lib/entitlements/seats.ts sends the Workspace's seat limit.
  'error-seat_limit_exceeded: limit',
  'error-seat_limit_reached: limit',
  'error-too_many_rows: limit',
  'error-content_length_exceeds_policy: limit_mb',
  'error-file_too_large: limit_mb',
  'error-learnings_text_too_long: limit',
  'error-library_rights_scope_too_long: limit',
  'error-source_invalid: limit',
  'error-text_too_long: limit',
  'error-too_many_images: limit',
  'error-string_too_long: max_length',
  'error-string_too_short: min_length',
  'error-too_long: max_length',
  'error-too_short: min_length',
  'error-greater_than: gt',
  'error-greater_than_equal: ge',
  'error-less_than: lt',
  'error-less_than_equal: le',
];
