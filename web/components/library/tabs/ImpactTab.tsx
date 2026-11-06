import { useTranslation } from 'next-i18next';
import { useResolvedLibraryVersion } from '@/hooks/library/useResolvedLibraryVersion';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';
import { ProblemState } from '../detail/ProblemState';

interface ImpactTabProps {
  entryId: string;
  versionId: string | null;
  currentVersionId: string | null;
}

interface ImpactDependent {
  entry_id: string;
  entry_type: string;
  display_name: string;
  relationship: string;
}

interface ImpactData {
  version_id: string;
  entry_id: string;
  entry_type: string;
  state: string;
  eligibility: string;
  dependents?: ImpactDependent[];
  reason: string;
}

export function ImpactTab({
  entryId,
  versionId,
  currentVersionId,
}: ImpactTabProps) {
  const { t } = useTranslation('library');
  const effectiveVersionId = versionId ?? currentVersionId;

  const {
    version,
    mismatch,
    isLoading: isLoadingVersion,
  } = useResolvedLibraryVersion(effectiveVersionId, entryId);

  const {
    data: impactData,
    problem: impactProblem,
    isLoading: isLoadingImpact,
    refresh,
  } = useDomainQuery(
    'get_library_version_impact',
    effectiveVersionId
      ? ({ version_id: effectiveVersionId } as any)
      : undefined,
    { enabled: !!effectiveVersionId && !mismatch }
  );

  const impact = impactData as unknown as ImpactData | undefined;

  if (!effectiveVersionId) {
    return (
      <div className="flex items-center justify-center py-12 text-center">
        <p className="text-sm text-gray-500 dark:text-gray-400">
          {t('tabs.impact.no-version')}
        </p>
      </div>
    );
  }

  if (isLoadingVersion || isLoadingImpact) {
    return (
      <div className="flex items-center justify-center py-12" aria-busy="true">
        <p className="text-sm text-gray-500 dark:text-gray-400">
          {t('detail.loading')}
        </p>
      </div>
    );
  }

  if (mismatch) {
    return (
      <div className="py-8 text-center" role="alert">
        <p className="text-sm text-red-600 dark:text-red-400">
          {t('tabs.current.version-mismatch')}
        </p>
      </div>
    );
  }

  if (impactProblem) {
    return <ProblemState onRetry={() => refresh()} />;
  }

  if (!impact) return null;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
          {t('tabs.impact.title')}
        </span>
        {version && (
          <span className="text-xs text-gray-500 dark:text-gray-400">
            {t('tabs.versions.version-label', {
              no: String(version.version_no),
            })}
          </span>
        )}
      </div>
      {impact.dependents && impact.dependents.length > 0 ? (
        <ul
          role="list"
          className="divide-y divide-gray-100 dark:divide-gray-800"
        >
          {impact.dependents.map((dep) => (
            <li key={dep.entry_id} className="py-2 px-3">
              <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
                {dep.display_name}
              </span>
              <span className="ml-2 text-xs text-gray-500 dark:text-gray-400">
                {dep.entry_type} — {dep.relationship}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-gray-500 dark:text-gray-400">
          {t('tabs.impact.no-dependents')}
        </p>
      )}
    </div>
  );
}
