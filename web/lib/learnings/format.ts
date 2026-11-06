import type { components } from '@/lib/domain-client/generated/domain-v1';
import { formatNumber } from '@/lib/i18n/format';

export type ProductLearnings = components['schemas']['ProductLearnings'];
export type RoundLearning = components['schemas']['RoundLearning'];
export type FactorLearning = components['schemas']['FactorLearning'];
export type ValueLearning = components['schemas']['ValueLearning'];
export type DirectionLearning = components['schemas']['DirectionLearning'];
export type MetricComparison = components['schemas']['MetricComparison'];
export type Verdict = ValueLearning['verdict'];

/** The comparison a row's headline verdict is about. */
export function headline(
  row: ValueLearning | DirectionLearning
): MetricComparison {
  return row.verdict_metric === 'cpa' ? row.cpa : row.ctr;
}

function num(value: string | null | undefined): number | null {
  if (value === null || value === undefined || value === '') return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** `"0.38"` -> `+38%`; one decimal below 1%. */
export function formatLift(value: string | null | undefined): string {
  const parsed = num(value);
  if (parsed === null) return '—';
  const scaled = parsed * 100;
  const digits = Math.abs(scaled) < 1 ? 1 : 0;
  const text = Math.abs(scaled).toFixed(digits);
  if (Number(text) === 0) return '0%';
  return `${scaled < 0 ? '-' : '+'}${text}%`;
}

export interface LiftInterval {
  point: number;
  low: number;
  /** `null` when the interval has no upper bound. */
  high: number | null;
}

export function liftInterval(
  comparison: MetricComparison
): LiftInterval | null {
  const point = num(comparison.lift);
  const low = num(comparison.lift_low);
  if (point === null || low === null) return null;
  return { point, low, high: num(comparison.lift_high) };
}

/** For CPA a lower cost is better: flip so "right of 持平" always means better. */
export function betterIsRight(
  interval: LiftInterval,
  metric: 'ctr' | 'cpa'
): LiftInterval {
  if (metric === 'ctr') return interval;
  return {
    point: -interval.point,
    low: interval.high === null ? -10 : -interval.high,
    high: -interval.low,
  };
}

/**
 * A symmetric domain shared by every row of a factor, so their strips are
 * comparable: the widest finite bound, rounded up to a readable step, at
 * least ±20% and at most ±200%.
 */
export function sharedDomain(intervals: LiftInterval[]): number {
  let widest = 0.2;
  for (const interval of intervals) {
    widest = Math.max(widest, Math.abs(interval.low), Math.abs(interval.point));
    if (interval.high !== null)
      widest = Math.max(widest, Math.abs(interval.high));
  }
  const steps = [0.2, 0.5, 1, 1.5, 2];
  return steps.find((step) => widest <= step) ?? 2;
}

/** A count with the interface language's digit grouping (`12,345`). */
export function thousands(value: number | string, locale: string): string {
  const parsed = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(parsed) ? formatNumber(locale, parsed) : String(value);
}

export function formatRate(value: string | null | undefined): string {
  const parsed = num(value);
  if (parsed === null) return '—';
  return `${(parsed * 100).toFixed(2)}%`;
}
