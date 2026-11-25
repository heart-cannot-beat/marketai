import type { components } from '@/lib/domain-client/generated/domain-v1';
import type { TestRound } from '@/lib/tests/take-to-test';

/**
 * What the "test in progress" screen (design §5.7–§5.9, §5.11, #257) reads
 * and how it turns the user's choices into commands. Every number it shows --
 * spend, orders, cost per order, orders still missing, the counted and
 * left-out rows, the dates, the result -- comes from the server; nothing
 * here computes one.
 */

export type TestProgress = components['schemas']['MarketingTestProgress'];
export type TestComparison =
  components['schemas']['MarketingTestProgressComparison'];
export type TestResult = components['schemas']['MarketingTestResult'];
export type TestSchedule = components['schemas']['MarketingTestRoundSchedule'];
export type TestReport = components['schemas']['MarketingTestReport'];
export type ReportUploadBody = components['schemas']['PreviewTestReport'];
export type BindingSuggestions =
  components['schemas']['MarketingTestBindingSuggestions'];
export type BindingSuggestion =
  components['schemas']['MarketingTestBindingSuggestion'];
export type BindingEntry = components['schemas']['MarketingTestBindingEntry'];
export type BindingChoice = components['schemas']['MarketingTestBindingChoice'];
export type TestBindings = components['schemas']['MarketingTestBindings'];

/** What a Meta ad's choice starts as: its match now, or what its name suggests; empty skips it. */
export function initialChoices(
  suggestions: BindingSuggestions
): Record<string, string> {
  return Object.fromEntries(
    suggestions.ads.map((ad) => [ad.ad_id, ad.entry_id ?? ''])
  );
}

/** The Meta ad's match now, if it has one in this round. */
function currentMatch(ad: BindingSuggestion): string | null {
  return ad.status === 'bound' ? ad.entry_id : null;
}

/**
 * The choices that change something, as the confirm command takes them: a
 * Meta ad matched or matched differently, or (before the first confirmed
 * report) a match taken away. A skipped ad that has no match is left out.
 */
export function bindingChanges(
  suggestions: BindingSuggestions,
  choices: Record<string, string>
): BindingChoice[] {
  const changes: BindingChoice[] = [];
  for (const ad of suggestions.ads) {
    const chosen = choices[ad.ad_id] || null;
    if (chosen === currentMatch(ad)) continue;
    if (suggestions.locked && currentMatch(ad) !== null) continue;
    changes.push({ ad_id: ad.ad_id, entry_id: chosen });
  }
  return changes;
}

/** How many Meta ads of the report will show one of the round's ads once the choices are confirmed. */
export function matchedCount(
  suggestions: BindingSuggestions,
  choices: Record<string, string>
): number {
  return suggestions.ads.filter((ad) =>
    suggestions.locked && currentMatch(ad) !== null
      ? true
      : Boolean(choices[ad.ad_id])
  ).length;
}

/**
 * The day to ask about on the round's first report (design §5.7): "it looks
 * like you started on X -- change the start to X?". Asked only while the
 * start can still move (no report confirmed, not ended early) and the
 * report's first day is not the round's start.
 */
export function startDateQuestion(
  report: TestReport | null,
  schedule: TestSchedule | undefined
): string | null {
  if (!report || !schedule?.start_movable || !report.first_date) return null;
  return report.first_date === schedule.effective_start_date
    ? null
    : report.first_date;
}

/** The compared groups holding any of these ads: recording a deviation draws no conclusion on them. */
export function affectedComparisons(
  comparisons: readonly TestComparison[],
  assetVersionIds: readonly string[]
): TestComparison[] {
  const picked = new Set(assetVersionIds);
  return comparisons.filter((comparison) =>
    comparison.asset_version_ids.some((id) => picked.has(id))
  );
}

/** Whether the round has a report counted already. */
export function hasConfirmedReport(round: TestRound | undefined): boolean {
  return Boolean(round?.batches.some((batch) => batch.state === 'confirmed'));
}

export type ChecklistStep = 'run' | 'report' | 'result';

export interface Checklist {
  /** The step the user is on; `null` once the result is read and current. */
  active: ChecklistStep | null;
  done: Record<ChecklistStep, boolean>;
}

/**
 * The three steps of a running test (design §5.11): put the ad package in
 * Meta, send the report back and match the ads, read the result once it is
 * due. A step is done by what the server already has: a Meta ad matched or
 * a report counted, a report counted, a result read on the current data.
 */
export function checklist(
  round: TestRound | undefined,
  result: TestResult | undefined
): Checklist {
  const reported = hasConfirmedReport(round);
  const done = {
    run: reported || Boolean(round?.bindings.length),
    report: reported,
    result: Boolean(result?.evaluation_id && result.current),
  };
  let active: ChecklistStep | null;
  if (result?.mature) active = done.result ? null : 'result';
  else active = done.run ? 'report' : 'run';
  return { active, done };
}

/** The round's ad package is ready, so its ads may be running in Meta. */
export function packageReady(round: TestRound | undefined): boolean {
  return round?.handoff?.state === 'ready';
}

/** The package ad a Meta ad can be matched to, by the name the reader sees. */
export function entryName(entry: {
  display_name?: string | null;
  ad_name: string;
}): string {
  return entry.display_name || entry.ad_name;
}
