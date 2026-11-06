import { useTranslation } from 'next-i18next';
import {
  betterIsRight,
  formatLift,
  formatRate,
  headline,
  liftInterval,
  sharedDomain,
  thousands,
  type DirectionLearning,
  type LiftInterval,
  type MetricComparison,
  type RoundLearning,
  type ValueLearning,
} from '@/lib/learnings/format';
import { LiftStrip } from './LiftStrip';
import { VerdictChip } from './VerdictChip';

type Row = (ValueLearning | DirectionLearning) & { label: string };

function stripInterval(row: Row): LiftInterval | null {
  if (row.verdict === 'insufficient_data') return null;
  const interval = liftInterval(headline(row));
  return interval ? betterIsRight(interval, row.verdict_metric) : null;
}

function DetailRow({ comparison }: { comparison: MetricComparison }) {
  const { t, i18n } = useTranslation('learnings');
  const n = comparison.n;
  const lift =
    comparison.lift === null || comparison.lift === undefined
      ? '—'
      : `${formatLift(comparison.lift)} (${formatLift(comparison.lift_low)}~${
          comparison.lift_high ? formatLift(comparison.lift_high) : '∞'
        })`;
  return (
    <tr>
      <td>{comparison.metric.toUpperCase()}</td>
      <td className="tabular-nums">
        {comparison.metric === 'ctr'
          ? formatRate(comparison.estimate)
          : comparison.estimate
            ? Number(comparison.estimate).toFixed(2)
            : '—'}
      </td>
      <td className="tabular-nums">
        {comparison.metric === 'ctr'
          ? formatRate(comparison.baseline)
          : comparison.baseline
            ? Number(comparison.baseline).toFixed(2)
            : '—'}
      </td>
      <td className="tabular-nums">{lift}</td>
      <td className="tabular-nums">
        {comparison.metric === 'ctr'
          ? t('detail-n-ctr', {
              impressions: thousands(n.impressions ?? 0, i18n.language),
              clicks: thousands(n.clicks ?? 0, i18n.language),
              assets: n.assets ?? 0,
            })
          : t('detail-n-cpa', {
              conversions: n.conversions ?? '0',
              spend: n.spend ?? '0',
              assets: n.assets ?? 0,
            })}
      </td>
    </tr>
  );
}

function Rows({ title, rows }: { title: string; rows: Row[] }) {
  const { t } = useTranslation('learnings');
  const intervals = rows
    .map(stripInterval)
    .filter((item): item is LiftInterval => item !== null);
  const domain = sharedDomain(intervals);
  return (
    <section className="space-y-2" aria-label={title}>
      <h4 className="text-sm font-semibold">{title}</h4>
      <ul className="space-y-3">
        {rows.map((row) => {
          const main = headline(row);
          return (
            <li
              key={row.label}
              className="space-y-1"
              data-testid="learning-row"
            >
              <div className="grid grid-cols-[minmax(0,9rem)_minmax(0,1fr)_5rem] items-center gap-3">
                <div className="flex min-w-0 flex-col items-start gap-1">
                  <span
                    className="truncate text-sm font-medium"
                    title={row.label}
                  >
                    {row.label}
                  </span>
                  <VerdictChip verdict={row.verdict} />
                </div>
                <LiftStrip
                  interval={stripInterval(row)}
                  verdict={row.verdict}
                  domain={domain}
                  label={row.sentence}
                />
                <span className="text-right text-sm tabular-nums text-base-content/80">
                  {row.verdict === 'insufficient_data'
                    ? '—'
                    : `${main.metric.toUpperCase()} ${formatLift(main.lift)}`}
                </span>
              </div>
              <p className="text-sm text-base-content/70">{row.sentence}</p>
            </li>
          );
        })}
      </ul>
      <div
        className="grid grid-cols-[minmax(0,9rem)_minmax(0,1fr)_5rem] gap-3 text-xs text-base-content/50"
        aria-hidden="true"
      >
        <span />
        <div className="flex justify-between">
          <span>{t('axis-worse')}</span>
          <span>{t('axis-even')}</span>
          <span>{t('axis-better')}</span>
        </div>
        <span className="text-right">
          {t('axis-scale', { percent: Math.round(domain * 100) })}
        </span>
      </div>
      <details className="text-sm">
        <summary className="cursor-pointer text-base-content/60">
          {t('details')}
        </summary>
        <div className="overflow-x-auto">
          <table className="table table-xs mt-2">
            <thead>
              <tr>
                <th>{t('detail-metric')}</th>
                <th>{t('detail-rate')}</th>
                <th>{t('detail-baseline')}</th>
                <th>{t('detail-lift')}</th>
                <th>{t('detail-n')}</th>
              </tr>
            </thead>
            {rows.map((row) => (
              <tbody key={row.label}>
                <tr>
                  <th colSpan={5} className="font-medium">
                    {row.label}
                  </th>
                </tr>
                <DetailRow comparison={row.ctr} />
                <DetailRow comparison={row.cpa} />
              </tbody>
            ))}
          </table>
        </div>
      </details>
    </section>
  );
}

/** One round ("第 N 轮"): every factor's values, then the directions. */
export function RoundView({ round }: { round: RoundLearning }) {
  const { t } = useTranslation('learnings');
  return (
    <div className="space-y-6">
      {(round.factors ?? []).map((factor) => (
        <Rows
          key={factor.key}
          title={factor.label}
          rows={(factor.values ?? []).map((value) => ({
            ...value,
            label: value.label,
          }))}
        />
      ))}
      {(round.directions ?? []).length > 0 && (
        <Rows
          title={t('directions-title')}
          rows={(round.directions ?? []).map((direction) => ({
            ...direction,
            label: direction.title,
          }))}
        />
      )}
    </div>
  );
}
