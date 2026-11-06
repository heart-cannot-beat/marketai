import { useTranslation } from 'next-i18next';
import type { LibraryEventSummary } from '@/hooks/library/useLibraryEvents';

interface EventTimelineProps {
  events: LibraryEventSummary[];
}

export function EventTimeline({ events }: EventTimelineProps) {
  const { t } = useTranslation('library');

  return (
    <ol
      className="relative border-l border-gray-200 dark:border-gray-700 ml-3 space-y-4"
      aria-label={t('tabs.events.timeline')}
    >
      {events.map((event) => (
        <li key={event.id} className="ml-6">
          <div className="absolute w-3 h-3 bg-gray-200 dark:bg-gray-600 rounded-full -left-1.5 border border-white dark:border-gray-900" />
          <div className="text-sm">
            <span className="font-medium text-gray-900 dark:text-gray-100">
              {t(`events.${event.event_type}`, {
                defaultValue: event.event_type,
              })}
            </span>
            {event.version_no != null && (
              <span className="ml-2 text-gray-500 dark:text-gray-400">
                {t('tabs.versions.version-label', { no: event.version_no })}
              </span>
            )}
          </div>
          <time className="text-xs text-gray-400 dark:text-gray-500">
            {event.occurred_at}
          </time>
        </li>
      ))}
    </ol>
  );
}
