import fs from 'node:fs';
import path from 'node:path';
import { readNamespace } from './locale-files';

/**
 * Codes the interface words instead of printing (#286). Each row names an
 * enum in the domain contract, the key family that words it, and the
 * component that renders through that family. A code added to the enum
 * without en and zh-CN copy fails here before it can ship as a raw code.
 */
interface Row {
  schema: string;
  property: string;
  namespace: string;
  /** Key prefix; the code completes the key. */
  prefix: string;
  /** Keys under `prefix` that are not codes. */
  extra?: string[];
  /** Codes that are deliberately never worded under `prefix`. */
  unworded?: string[];
  /** A component that renders `prefix` + code. */
  renderedBy: string;
}

const ROWS: Row[] = [
  {
    schema: 'LocalizationBatchDetail',
    property: 'preparation_stage',
    namespace: 'localization',
    prefix: 'preparation.state-',
    renderedBy: 'components/localization/LocalizationPreparation.tsx',
  },
  {
    schema: 'LaunchProvisioningStepResponse',
    property: 'step_type',
    namespace: 'experiments',
    prefix: 'launch.step-',
    renderedBy: 'components/experiments/ExperimentsWorkspace.tsx',
  },
  {
    schema: 'RoundSnapshot',
    property: 'attribution_window',
    namespace: 'common',
    prefix: 'marketing-attribution-window-',
    renderedBy: 'components/workspace/MarketingExecution.tsx',
  },
  {
    schema: 'OperationDetailResponse',
    property: 'state',
    namespace: 'common',
    prefix: 'operation-status-',
    extra: ['polling-problem'],
    renderedBy: 'components/workspace/OperationStatus.tsx',
  },
  {
    schema: 'ProductLearnings',
    property: 'evidence_mode',
    namespace: 'learnings',
    prefix: 'evidence-notice-',
    // Real data carries no notice (`EVIDENCE_NOTICE` in learnings/service.py).
    unworded: ['production'],
    renderedBy: 'components/learnings/LearningsCard.tsx',
  },
  {
    // #303: which confirmed copy field the image's rendered text differs in.
    schema: 'RenderedTextDifference',
    property: 'field',
    namespace: 'common',
    prefix: 'marketing-ai-qc-copy-field-',
    renderedBy: 'components/workspace/MarketingAIQC.tsx',
  },
  // #274: the competitor panel's resolves, quotes and scans.
  {
    schema: 'CompetitorResolution',
    property: 'status',
    namespace: 'competitors',
    prefix: 'resolution-status-',
    renderedBy: 'components/competitors/CompetitorResolvePanel.tsx',
  },
  {
    schema: 'CompetitorQuoteLine',
    property: 'operation',
    namespace: 'competitors',
    prefix: 'resolve-line-',
    renderedBy: 'components/competitors/CompetitorResolvePanel.tsx',
  },
  {
    schema: 'CompetitorConfirmation',
    property: 'source',
    namespace: 'competitors',
    prefix: 'confirmation-',
    extra: ['unconfirmed', 'resolving'],
    renderedBy: 'components/competitors/CompetitorRow.tsx',
  },
  {
    schema: 'CompetitorPageProbe',
    property: 'status',
    namespace: 'competitors',
    prefix: 'candidate-probe-',
    renderedBy: 'components/competitors/CandidateCard.tsx',
  },
  {
    schema: 'CompetitorCandidate',
    property: 'excluded_reason',
    namespace: 'competitors',
    prefix: 'candidate-excluded-',
    renderedBy: 'components/competitors/CandidateCard.tsx',
  },
  {
    schema: 'CompetitorScanCredential',
    property: 'binding_scope',
    namespace: 'competitors',
    prefix: 'credential-missing-',
    renderedBy: 'components/competitors/ScanSetup.tsx',
  },
  {
    schema: 'CompetitorScanQuoteLine',
    property: 'operation',
    namespace: 'competitors',
    prefix: 'quote-line-',
    renderedBy: 'components/competitors/ScanQuote.tsx',
  },
  {
    schema: 'CompetitorScan',
    property: 'status',
    namespace: 'competitors',
    prefix: 'scan-status-',
    renderedBy: 'components/competitors/ScanProgress.tsx',
  },
  {
    schema: 'CompetitorScanStepState',
    property: 'step',
    namespace: 'competitors',
    prefix: 'step-',
    extra: [
      'status-pending',
      'status-running',
      'status-completed',
      'status-partial',
      'status-failed',
      'status-skipped',
      'not-implemented',
      'cluster-superseded',
    ],
    renderedBy: 'components/competitors/ScanProgress.tsx',
  },
  {
    schema: 'CompetitorScanStepState',
    property: 'status',
    namespace: 'competitors',
    prefix: 'step-status-',
    renderedBy: 'components/competitors/ScanProgress.tsx',
  },
  {
    schema: 'CompetitorScanTarget',
    property: 'status',
    namespace: 'competitors',
    prefix: 'target-status-',
    renderedBy: 'components/competitors/ScanProgress.tsx',
  },
  {
    schema: 'CompetitorScanReconciliation',
    property: 'status',
    namespace: 'competitors',
    prefix: 'reconcile-status-',
    renderedBy: 'components/competitors/ScanProgress.tsx',
  },
  {
    schema: 'CompetitorScanReconciliation',
    property: 'source',
    namespace: 'competitors',
    prefix: 'reconcile-source-',
    renderedBy: 'components/competitors/ScanProgress.tsx',
  },
  {
    schema: 'CompetitorScanBalanceCheck',
    property: 'outcome',
    namespace: 'competitors',
    prefix: 'reconcile-check-',
    renderedBy: 'components/competitors/ScanProgress.tsx',
  },
  {
    schema: 'CompetitorScanBalanceCheck',
    property: 'reason',
    namespace: 'competitors',
    prefix: 'reconcile-reason-',
    renderedBy: 'components/competitors/ScanProgress.tsx',
  },
  // #275: the competitor board, its judgments and the summary.
  {
    schema: 'CompetitorAdRelevance',
    property: 'relevance',
    namespace: 'competitors',
    prefix: 'card-relevance-',
    // An observation no step or member judged yet (the `unassessed` filter).
    extra: ['unassessed'],
    renderedBy: 'components/competitors/ObservationCard.tsx',
  },
  {
    schema: 'CompetitorAdRelevance',
    property: 'source',
    namespace: 'competitors',
    prefix: 'card-source-',
    renderedBy: 'components/competitors/ObservationCard.tsx',
  },
  {
    schema: 'CompetitorAdRelevance',
    property: 'rule',
    namespace: 'competitors',
    prefix: 'card-rule-',
    renderedBy: 'components/competitors/ObservationCard.tsx',
  },
  {
    schema: 'CompetitorAd',
    property: 'media_status',
    namespace: 'competitors',
    prefix: 'card-media-',
    // The thumbnail's own states: its signed link failed, or it is loading.
    extra: ['failed', 'loading'],
    renderedBy: 'components/competitors/AdThumbnail.tsx',
  },
  {
    schema: 'CompetitorScanRecluster',
    property: 'status',
    namespace: 'competitors',
    prefix: 'summary-recluster-status-',
    renderedBy: 'components/competitors/SnapshotSummary.tsx',
  },
  // #273: plan-review findings from the cited competitor snapshot; the other kinds are worded in `common`.
  ...['review-kind-', 'review-system-'].map((prefix) => ({
    schema: 'ReviewItem',
    property: 'kind',
    namespace: 'competitors',
    prefix,
    unworded: [
      'angle_overlap',
      'expression_duplicate',
      'evidence_gap',
      'budget_insufficient',
      'image_brief_stale',
    ],
    renderedBy: 'components/workspace/MarketingPlanReview.tsx',
  })),
  // #276: a recommended advertiser's judgment.
  {
    schema: 'CompetitorDiscovery',
    property: 'relevance',
    namespace: 'competitors',
    prefix: 'discovery-relevance-',
    renderedBy: 'components/competitors/CompetitorDiscovery.tsx',
  },
];

const WEB = path.resolve(__dirname, '../..');
const CONTRACT = path.resolve(
  WEB,
  '../contracts/openapi/marketai-domain-v1.json'
);

type Property = { enum?: string[]; anyOf?: Property[] };
const schemas: Record<string, { properties?: Record<string, Property> }> =
  JSON.parse(fs.readFileSync(CONTRACT, 'utf8')).components.schemas;

/** The enum of `schema.property`, also inside an `anyOf` with null. */
const enumOf = (schema: string, property: string): string[] => {
  const node = schemas[schema]?.properties?.[property];
  const values =
    node?.enum ?? node?.anyOf?.flatMap((option) => option.enum ?? []) ?? [];
  return [...values].sort();
};

describe.each(ROWS)('$namespace:$prefix<$property>', (row) => {
  const codes = enumOf(row.schema, row.property);

  it(`reads the codes from ${row.schema}.${row.property}`, () => {
    expect(codes.length).toBeGreaterThan(1);
  });

  it.each(['en', 'zh-CN'])('%s words every code', (locale) => {
    const keys = readNamespace(locale, row.namespace);
    const missing = codes.filter(
      (code) => !keys.has(row.prefix + code) && !row.unworded?.includes(code)
    );
    expect(missing).toEqual([]);
  });

  it('has no copy for codes the contract no longer has', () => {
    const extra = new Set(row.extra ?? []);
    const stale = Array.from(readNamespace('en', row.namespace).keys())
      .filter((key) => key.startsWith(row.prefix))
      .map((key) => key.slice(row.prefix.length))
      .filter((code) => !extra.has(code))
      .filter((code) => !codes.includes(code) || row.unworded?.includes(code));
    expect(stale).toEqual([]);
  });

  it(`is rendered through the key family by ${row.renderedBy}`, () => {
    const source = fs.readFileSync(path.join(WEB, row.renderedBy), 'utf8');
    expect(source).toContain(`\`${row.prefix}\${`);
  });
});
