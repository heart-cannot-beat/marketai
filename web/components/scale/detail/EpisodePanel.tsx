import { useTranslation } from 'next-i18next';
import { useScaleEpisode } from '@/hooks/scale/useScaleEvidence';
import { LifecycleBadge } from '@/components/scale/shared/LifecycleBadge';
import { EvidenceTimeline } from '@/components/scale/detail/EvidenceTimeline';
import type { ScaleMetaMode } from '@/lib/scale/meta-mode';

interface EpisodePanelProps {
  enrollmentId: string;
  episodeId: string | null;
  metaMode: ScaleMetaMode;
}

/**
 * Episode phase axis (spec §4.4) plus the current Episode's evidence
 * (delegated to `EvidenceTimeline`). An Enrollment with no
 * `current_episode_id` (still `draft`, or `closed` with its last Episode
 * already ended) renders a "no active episode" state rather than fetching --
 * Episode carries no directly HTTP-mutable action of its own (it only moves
 * as a side effect of an Enrollment lifecycle command), so this panel is
 * read-only.
 */
export function EpisodePanel({
  enrollmentId,
  episodeId,
  metaMode,
}: EpisodePanelProps) {
  const { t } = useTranslation('scale');
  const { data: episode, isLoading } = useScaleEpisode(
    enrollmentId,
    episodeId,
    metaMode
  );

  if (!episodeId) {
    return (
      <div className="rounded-lg border p-4">
        <h3 className="text-sm font-semibold">{t('episode.heading')}</h3>
        <p className="mt-2 text-sm text-base-content/60">
          {t('episode.no-active-episode')}
        </p>
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="rounded-lg border p-4" aria-busy="true">
        <span className="loading loading-spinner loading-sm" />
      </div>
    );
  }

  if (!episode) {
    return (
      <div className="rounded-lg border p-4">
        <p className="text-sm text-base-content/60">
          {t('episode.unavailable')}
        </p>
      </div>
    );
  }

  return (
    <div className="rounded-lg border p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">{t('episode.heading')}</h3>
        <LifecycleBadge scope="episode" value={episode.phase} />
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-2 text-sm sm:grid-cols-3">
        <div>
          <dt className="text-xs text-base-content/60">
            {t('episode.started-local-day')}
          </dt>
          <dd>{episode.started_local_day}</dd>
        </div>
        <div>
          <dt className="text-xs text-base-content/60">
            {t('episode.timezone')}
          </dt>
          <dd>{episode.timezone_name}</dd>
        </div>
        <div>
          <dt className="text-xs text-base-content/60">
            {t('episode.currency')}
          </dt>
          <dd>{episode.currency}</dd>
        </div>
      </dl>

      {episode.closure_reason && (
        <p className="mt-2 text-sm text-base-content/70">
          {t('episode.closure-reason', { reason: episode.closure_reason })}
        </p>
      )}

      <div className="mt-4 border-t pt-3">
        <h4 className="text-xs font-semibold text-base-content/60">
          {t('evidence.heading')}
        </h4>
        <EvidenceTimeline enrollmentId={enrollmentId} metaMode={metaMode} />
      </div>
    </div>
  );
}
