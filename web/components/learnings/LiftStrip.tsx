import classNames from 'classnames';
import type { LiftInterval, Verdict } from '@/lib/learnings/format';

const TONE: Record<Verdict, string> = {
  winner: 'text-success',
  loser: 'text-error',
  no_difference: 'text-base-content/50',
  insufficient_data: 'text-base-content/30',
};

/** Position of a lift on the shared scale, in percent of the strip width. */
export function position(value: number, domain: number): number {
  const clamped = Math.max(-domain, Math.min(domain, value));
  return ((clamped + domain) / (2 * domain)) * 100;
}

/**
 * One row of a forest plot: the 95% interval of "how much better than the
 * other values" as a thin bar, the point estimate as a dot ringed in the
 * surface colour, and the 持平 (no difference) line in the middle. Every
 * row of a factor shares `domain`, so rows are comparable; right is always
 * better (a CPA interval arrives already flipped). Without an interval
 * (insufficient data) only the dashed track is drawn -- no invented number.
 */
export function LiftStrip({
  interval,
  verdict,
  domain,
  label,
}: {
  interval: LiftInterval | null;
  verdict: Verdict;
  domain: number;
  /** The row's sentence: the accessible name and the hover text. */
  label: string;
}) {
  const low = interval ? position(interval.low, domain) : 0;
  const high = interval
    ? interval.high === null
      ? 100
      : position(interval.high, domain)
    : 0;
  return (
    <div
      className={classNames('relative h-6 w-full', TONE[verdict])}
      role="img"
      aria-label={label}
      title={label}
      data-testid="lift-strip"
    >
      <div
        className={classNames(
          'absolute inset-x-0 top-1/2 border-t',
          interval
            ? 'border-base-content/15'
            : 'border-dashed border-base-content/25'
        )}
        aria-hidden="true"
      />
      <div
        className="absolute inset-y-1 left-1/2 w-px bg-base-content/40"
        aria-hidden="true"
      />
      {interval && (
        <>
          <div
            className="absolute top-1/2 h-1.5 -translate-y-1/2 rounded-full bg-current opacity-40"
            style={{
              left: `${Math.min(low, high)}%`,
              width: `max(2px, ${Math.abs(high - low)}%)`,
            }}
            aria-hidden="true"
            data-testid="lift-interval"
          />
          <div
            className="absolute top-1/2 h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-current ring-2 ring-base-100"
            style={{ left: `${position(interval.point, domain)}%` }}
            aria-hidden="true"
            data-testid="lift-point"
          />
        </>
      )}
    </div>
  );
}
