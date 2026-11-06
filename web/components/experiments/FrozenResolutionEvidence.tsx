import { useTranslation } from 'next-i18next';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import { formatDate } from '@/lib/i18n/format';

type ConceptResolutionEvidence =
  components['schemas']['ConceptResolutionEvidence'];
type ResolutionEvidenceRow = components['schemas']['ResolutionEvidenceRow'];

interface FrozenResolutionEvidenceProps {
  evidence: ConceptResolutionEvidence;
}

export function FrozenResolutionEvidence({
  evidence,
}: FrozenResolutionEvidenceProps) {
  const { t } = useTranslation('experiments');
  if (!evidence.had_duplicates) {
    return (
      <section className="space-y-2">
        <h3 className="text-lg font-semibold">
          {t('frozen-resolution.title')}
        </h3>
        <p className="text-sm text-gray-600">{t('frozen-resolution.none')}</p>
      </section>
    );
  }

  return (
    <section className="space-y-4">
      <h3 className="text-lg font-semibold">{t('frozen-resolution.title')}</h3>
      {(evidence.resolutions ?? []).map((row) => (
        <EvidenceCard key={row.duplicate_group_hash} row={row} />
      ))}
    </section>
  );
}

function EvidenceCard({ row }: { row: ResolutionEvidenceRow }) {
  const { t, i18n } = useTranslation('experiments');
  return (
    <div className="rounded-lg border border-gray-200 p-4 space-y-2">
      <div className="font-medium">
        {row.dimension_key} — {row.concept_key_name}
      </div>
      <div className="flex flex-wrap gap-4 text-sm">
        <span className="rounded bg-blue-50 px-2 py-0.5 text-blue-700">
          {t(`frozen-resolution.actions.${row.action}`, {
            defaultValue: row.action,
          })}
        </span>
        <span className="text-gray-500">
          {row.actor_role} · {formatDate(i18n.language, row.resolved_at)}
        </span>
      </div>
      <p className="text-sm text-gray-700">{row.reason}</p>
      {row.action === 'exclude_source_candidate' &&
        row.affected_candidate_fingerprint && (
          <p className="text-xs text-gray-500 font-mono">
            {t('frozen-resolution.excluded', {
              fingerprint: row.affected_candidate_fingerprint.slice(0, 16),
            })}
          </p>
        )}
      {row.action === 'remap_before_freeze' && (
        <p className="text-xs text-gray-500">
          {t('frozen-resolution.remapped', {
            concept: row.remap_destination_concept_key_name,
          })}
        </p>
      )}
    </div>
  );
}
