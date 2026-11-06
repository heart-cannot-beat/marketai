import { useTranslation } from 'next-i18next';
import classNames from 'classnames';
import {
  ArrowTrendingDownIcon,
  ArrowTrendingUpIcon,
  ClockIcon,
  EqualsIcon,
} from '@heroicons/react/20/solid';
import type { Verdict } from '@/lib/learnings/format';

const STYLE: Record<Verdict, { className: string; Icon: typeof ClockIcon }> = {
  winner: { className: 'badge-success', Icon: ArrowTrendingUpIcon },
  loser: { className: 'badge-error', Icon: ArrowTrendingDownIcon },
  no_difference: { className: 'badge-ghost', Icon: EqualsIcon },
  insufficient_data: { className: 'badge-outline', Icon: ClockIcon },
};

/** Verdict as icon + word: colour never carries the meaning alone. */
export function VerdictChip({ verdict }: { verdict: Verdict }) {
  const { t } = useTranslation('learnings');
  const { className, Icon } = STYLE[verdict];
  return (
    <span
      className={classNames(
        'badge badge-sm gap-1 whitespace-nowrap',
        className
      )}
      data-verdict={verdict}
    >
      <Icon className="h-3 w-3" aria-hidden="true" />
      {t(`verdict-${verdict}`)}
    </span>
  );
}
