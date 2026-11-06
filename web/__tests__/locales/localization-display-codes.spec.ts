import fs from 'node:fs';
import path from 'node:path';
import { LEDGER_SIGN } from '@/components/localization/LocalizationWorkspace';
import { CJK, readNamespace } from './locale-files';

/**
 * The localization page words the codes it used to print (#384): why an
 * action is unavailable, an Item's next action, a Truth skip, QC checks,
 * provider calls and cost ledger entries. None of these fields has an enum in
 * the contract, so each family is read from where the server fixes its
 * values: the literals its producers write, a database CheckConstraint, or a
 * Python constant. A value added there without en and zh-CN copy fails here
 * instead of reaching the page as a raw code.
 */
const APP = path.resolve(__dirname, '../../../app');
const WEB = path.resolve(__dirname, '../..');
const read = (file: string) => fs.readFileSync(path.join(APP, file), 'utf8');
const queries = read('localization/queries.py');
const worker = read('localization/worker.py');
const batch = read('localization/batch.py');
const truth = read('localization/truth.py');
const factSources = read('localization/fact_sources.py');
const qc = read('localization/qc.py');
const libraryContracts = read('library/contracts.py');
const costModels = read('entitlements/cost_models.py');
const models = read('adapters/persistence/models.py');

const LITERAL = /^"([a-z_]+)"$/;
const literals = (text: string) =>
  Array.from(text.matchAll(/"([a-z_]+)"/g), (m) => m[1]);
const unique = (codes: string[]) => Array.from(new Set(codes)).sort();

/** The body of a top-level `def name(`, up to the next top-level statement. */
const functionBody = (source: string, name: string) => {
  const body = source.match(
    new RegExp(
      `^def ${name}\\([\\s\\S]*?(?=^def |^class |^# |^[A-Z_]+ = )`,
      'm'
    )
  )?.[0];
  if (!body) throw new Error(`${name} not found`);
  return body;
};

/** The quoted values of the `CheckConstraint("<column> IN (...)", name=...)`. */
const constraint = (source: string, column: string, name: string) => {
  const end = source.indexOf(`name="${name}"`);
  if (end < 0) throw new Error(`constraint ${name} not found`);
  const clause = source.slice(source.lastIndexOf('CheckConstraint(', end), end);
  // A compound expression would need its own reading.
  if (!new RegExp(`^CheckConstraint\\(\\s*"${column} IN \\(`).test(clause))
    throw new Error(`${name} is not "${column} IN (...)"`);
  return Array.from(clause.matchAll(/'([a-z_]+)'/g), (m) => m[1]);
};

/**
 * `_batch_actions` and `_item_actions` give each action
 * `"reason_code": None if <allowed> else <code>`; the item's retry picks one
 * of two codes with a nested conditional. A code is a literal right after
 * `else` (or its parenthesis) or right before `if`; any other literal is part
 * of a condition.
 */
const actionWrites = ['_batch_actions', '_item_actions'].flatMap((name) =>
  Array.from(
    functionBody(queries, name).matchAll(/"reason_code":\s*([\s\S]*?)\}/g),
    (m) => m[1].trim()
  )
);
const actionReasons = unique(
  actionWrites.flatMap((value) =>
    Array.from(
      value.matchAll(/\belse\s+\(?\s*"([a-z_]+)"|"([a-z_]+)"\s+if\b/g),
      (m) => m[1] ?? m[2]
    )
  )
);
const actionConditions = actionWrites
  .flatMap(literals)
  .filter((value) => !actionReasons.includes(value));

/** `next_action` as a keyword or attribute, a dict entry, or a subscript. */
const NEXT_ACTION_WRITE =
  /\bnext_action\s*=(?!=)\s*([^,\n)]+)|"next_action":\s*([^,\n}]+)|\["next_action"\]\s*=\s*([^\n]+)/g;
const nextActionNamed: string[] = [];
const nextActionIndirect: string[] = [];
for (const source of [worker, batch]) {
  for (const m of Array.from(source.matchAll(NEXT_ACTION_WRITE))) {
    const value = (m[1] ?? m[2] ?? m[3]).trim();
    const literal = value.match(LITERAL);
    if (literal) nextActionNamed.push(literal[1]);
    else nextActionIndirect.push(value);
  }
}

/**
 * The writes that pass a value on instead of naming one, each traced to where
 * its value comes from. A new form fails here instead of slipping by.
 */
const NEXT_ACTION_INDIRECT = [
  // Clears it: a requeued, resumed, succeeded or cancelled Item.
  'None',
  'None',
  'None',
  'None',
  'None',
  // `_ItemPlan` carries `_pair_initial_disposition`'s (read below).
  'combo.next_action',
  'disposition[4]',
  // `_transition_item` stores what its callers name.
  'next_action',
];

/** The fifth element of each tuple `_pair_initial_disposition` returns. */
const disposition = functionBody(batch, '_pair_initial_disposition');
const dispositionActions = Array.from(
  disposition.matchAll(
    /return \(?\s*"[a-z]+",\s*[^,]+,\s*[^,]+,\s*[^,]+,\s*([^,\s)]+)/g
  ),
  (m) => m[1]
);
const nextActions = unique([
  ...nextActionNamed,
  ...dispositionActions.flatMap((value) => value.match(LITERAL)?.[1] ?? []),
]);

/**
 * A fact no source resolved is skipped under its frozen `on_failure` rule
 * as `on_failure_<mode>`; every other `skip_code=` is `None` or a copy.
 */
const skipWrites = Array.from(truth.matchAll(/\bskip_code=([^,\n)]+)/g), (m) =>
  m[1].trim()
);
const onFailureModes = literals(
  libraryContracts.match(
    /^ON_FAILURE_MODES = frozenset\(\{([^}]*)\}\)/m
  )?.[1] ?? ''
);
const onFailureLiteral = literals(
  truth.match(/\bon_failure: Literal\[([^\]]*)\]/)?.[1] ?? ''
);
const truthSkips = onFailureModes.map((mode) => `on_failure_${mode}`).sort();

/** An adapter's failure: `FactSourceFailure(..., code="x", ...)`. */
const truthFailureWrites = [factSources, truth].flatMap((source) =>
  Array.from(
    source.matchAll(/\bFactSourceFailure\([\s\S]*?\bcode=([^,\n)]+)/g),
    (m) => m[1].trim()
  )
);
const truthFailures = unique(
  truthFailureWrites.flatMap((value) => value.match(LITERAL)?.[1] ?? [])
);
const failureCodeWrites = Array.from(
  truth.matchAll(/\bfailure_code=([^,\n)]+)/g),
  (m) => m[1].trim()
);

/** `QCPlanSlot("<kind>", ...)` in `qc.py`; the Worker's placeholder checks. */
const qcKinds = unique([
  ...Array.from(qc.matchAll(/\bQCPlanSlot\("([a-z_]+)"/g), (m) => m[1]),
  ...Array.from(worker.matchAll(/\bcheck_kind="([a-z_]+)"/g), (m) => m[1]),
]);
const qcSeverities = literals(
  qc.match(/^SEVERITIES = \(([^)]*)\)/m)?.[1] ?? ''
);

/** Provider calls, their reservations and the ledger are billing tables. */
const callSteps = constraint(
  models,
  'provider_step_kind',
  'ck_lipc_provider_step_kind'
);
const dispatchStates = constraint(
  costModels,
  'dispatch_state',
  'dispatch_state_valid'
);
const chargeabilities = constraint(
  costModels,
  'chargeability',
  'chargeability'
);
const reservationStates = constraint(
  costModels,
  'state',
  'spend_reservation_state_valid'
);
const ledgerEntries = constraint(
  costModels,
  'entry_type',
  'provider_cost_ledger_entry_type_valid'
);
const ledgerDirections = constraint(
  costModels,
  'direction',
  'provider_cost_ledger_direction_valid'
);

interface Row {
  name: string;
  codes: string[];
  /** Key prefix in `localization`; the code completes the key. */
  prefix: string;
  /** Other fields word their codes under the same prefix. */
  shared?: boolean;
  /** The component that renders `prefix` + code. */
  renderedBy: string;
  /** What it renders through, when the prefix alone is not this field's. */
  renders?: string;
}

const WORKSPACE = 'components/localization/LocalizationWorkspace.tsx';
const ROWS: Row[] = [
  {
    name: 'action reasons',
    codes: actionReasons,
    prefix: 'action-reason.',
    renderedBy: 'components/localization/LocalizationUi.tsx',
  },
  {
    name: 'Item next actions',
    codes: nextActions,
    prefix: 'item.next-action.',
    renderedBy: WORKSPACE,
  },
  {
    name: 'Truth skips',
    codes: truthSkips,
    prefix: 'truth.skip.',
    renderedBy: WORKSPACE,
  },
  {
    name: 'QC check kinds',
    codes: qcKinds,
    prefix: 'item.qc-kind.',
    renderedBy: WORKSPACE,
  },
  {
    name: 'QC severities',
    codes: qcSeverities,
    prefix: 'item.qc-severity.',
    renderedBy: WORKSPACE,
  },
  {
    name: 'call steps',
    codes: callSteps,
    prefix: 'item.call-step.',
    renderedBy: WORKSPACE,
  },
  {
    // A call the query cannot find reads `unknown`, worded as `status.unknown`.
    name: 'call dispatch states',
    codes: dispatchStates,
    prefix: 'status.',
    shared: true,
    renderedBy: WORKSPACE,
    renders: '`status.${call.dispatch_state}`',
  },
  {
    name: 'call chargeability',
    codes: chargeabilities,
    prefix: 'item.call-charge.',
    renderedBy: WORKSPACE,
  },
  {
    name: 'reservation states',
    codes: reservationStates,
    prefix: 'item.reservation.',
    renderedBy: WORKSPACE,
  },
  {
    name: 'ledger entry types',
    codes: ledgerEntries,
    prefix: 'item.ledger-entry.',
    renderedBy: WORKSPACE,
  },
];

test('reads every action reason queries.py gives', () => {
  // One `"reason_code":` per action: five on a batch, two on an Item.
  expect(actionWrites).toHaveLength(7);
  expect(actionWrites.every((value) => value.startsWith('None if '))).toBe(
    true
  );
  expect(actionConditions).toEqual(['draft']);
  expect(actionReasons).toEqual(
    expect.arrayContaining([
      'batch_not_ready',
      'claim_review_not_ready',
      'attempts_exhausted',
      'item_not_retry_wait',
      'item_not_paused',
    ])
  );
});

test('reads every next action the server writes', () => {
  expect(nextActionIndirect.sort()).toEqual(NEXT_ACTION_INDIRECT);
  expect(dispositionActions).toHaveLength(
    disposition.match(/\breturn\b/g)!.length
  );
  expect(dispositionActions.sort()).toEqual([
    '"create_new_batch"',
    'None',
    'None',
    'None',
  ]);
  expect(nextActions).toEqual(
    expect.arrayContaining(['retry_item', 'resume_item', 'reconcile'])
  );
});

test('reads every Truth skip and failure the server writes', () => {
  expect(skipWrites.filter((value) => value !== 'None').sort()).toEqual([
    'attempt.skip_code',
    'f"on_failure_{on_failure}"',
  ]);
  expect(onFailureModes.sort()).toEqual(onFailureLiteral.sort());
  expect(truthSkips).toEqual(expect.arrayContaining(['on_failure_skip']));
  // Every adapter failure names its code.
  expect(truthFailureWrites).toHaveLength(
    (factSources + truth).match(/\bFactSourceFailure\(/g)!.length
  );
  expect(truthFailureWrites.every((value) => LITERAL.test(value))).toBe(true);
  expect(failureCodeWrites.filter((value) => value !== 'None').sort()).toEqual([
    'attempt.failure_code',
    'failure.code',
  ]);
  expect(truthFailures).toEqual(
    expect.arrayContaining(['no_manual_fact_set', 'adapter_not_enabled'])
  );
});

test('reads every QC check kind and the billing constraints', () => {
  expect(qcKinds).toEqual(
    expect.arrayContaining(['placeholder_count', 'locked_fact', 'ocr_check'])
  );
  expect(qcSeverities).toEqual(['blocking', 'warning']);
  expect(callSteps).toContain('localization_edit');
  expect(dispatchStates).toContain('prepared');
  expect(chargeabilities).toContain('free_mock');
  expect(reservationStates).toContain('partially_settled');
  expect(ledgerEntries).toContain('reported_cost');
});

describe.each(['en', 'zh-CN'])('%s', (locale) => {
  const keys = readNamespace(locale, 'localization');

  it.each(ROWS.map((row) => [row.name, row] as const))(
    'words every one of the %s',
    (_name, row) => {
      expect(row.codes.filter((code) => !keys.has(row.prefix + code))).toEqual(
        []
      );
      if (locale === 'zh-CN')
        expect(
          row.codes.filter(
            (code) => !CJK.test(String(keys.get(row.prefix + code)))
          )
        ).toEqual([]);
    }
  );

  it.each(
    ROWS.filter((row) => !row.shared).map((row) => [row.name, row] as const)
  )('has no copy for other %s', (_name, row) => {
    const extra = Array.from(keys.keys())
      .filter((key) => key.startsWith(row.prefix))
      .map((key) => key.slice(row.prefix.length))
      .filter((code) => !row.codes.includes(code));
    expect(extra).toEqual([]);
  });

  it('has error copy for every Truth source failure', () => {
    // The panel reads an adapter's failure as its `error-<code>` copy.
    const common = readNamespace(locale, 'common');
    expect(
      truthFailures.filter((code) => !common.has(`error-${code}`))
    ).toEqual([]);
  });
});

test('signs exactly the ledger directions the constraint allows', () => {
  expect(Object.keys(LEDGER_SIGN).sort()).toEqual(ledgerDirections.sort());
});

test.each(ROWS.map((row) => [row.name, row] as const))(
  'the %s are rendered through their key family',
  (_name, row) => {
    const source = fs.readFileSync(path.join(WEB, row.renderedBy), 'utf8');
    expect(source).toContain(row.renders ?? `\`${row.prefix}\${`);
  }
);

test('both localization pages word action reasons', () => {
  for (const file of [
    WORKSPACE,
    'components/localization/LocalizationPreparation.tsx',
  ]) {
    const source = fs.readFileSync(path.join(WEB, file), 'utf8');
    expect(source).toContain('actionReasonMessage(t, ');
    // No action shows its bare code as a fallback.
    expect(source).not.toMatch(/reason_code\s*(\|\||\?\?)/);
  }
});
