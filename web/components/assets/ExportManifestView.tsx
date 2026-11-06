import { useMemo } from 'react';
import { useTranslation } from 'next-i18next';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import { ExecutionModeTag } from '@/components/generation/shared/ExecutionModeTag';
import {
  exportPackageStatus,
  originLabelKey,
} from '@/lib/assets/status-labels';
import { InternalView } from './InternalView';

type ExportManifest = components['schemas']['ExportManifest'];
type ExportPackageDetail = components['schemas']['ExportPackageDetail'];
type ManifestEntry = components['schemas']['ExportManifestEntry'];

interface ExportManifestViewProps {
  manifest: ExportManifest;
  packageDetail?: ExportPackageDetail;
}

function deduplicationKey(entry: ManifestEntry): string {
  return entry.content_sha256 ?? `version:${entry.asset_version_id}`;
}

/**
 * The ready export package: state, execution mode, count, size and the
 * dedup summary. The per-version table and the ZIP hash carry `av_` ids and
 * hashes, so they sit in the collapsed「内部视图」(Issue #191).
 */
export function ExportManifestView({
  manifest,
  packageDetail,
}: ExportManifestViewProps) {
  const { t } = useTranslation('assets');
  const { t: tc } = useTranslation('common');
  const entries = useMemo(() => manifest.entries ?? [], [manifest.entries]);
  const uniquePayloadCount = useMemo(
    () => new Set(entries.map(deduplicationKey)).size,
    [entries]
  );
  const dedupCount = entries.length - uniquePayloadCount;
  const packageState = packageDetail
    ? exportPackageStatus(packageDetail.state)
    : null;

  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
        {packageDetail && packageState && (
          <>
            <span className="text-base-content/60">{t('manifest-state')}</span>
            <span
              className={
                packageDetail.state === 'ready'
                  ? 'text-success font-medium'
                  : ''
              }
              data-testid="export-package-state"
              data-state={packageDetail.state}
            >
              {tc(packageState.labelKey)}
            </span>
          </>
        )}
        <span className="text-base-content/60">{t('mode-label')}</span>
        <ExecutionModeTag mode={manifest.execution_mode} />
        <span className="text-base-content/60">{t('manifest-assets')}</span>
        <span>{manifest.total_versions}</span>
        {packageDetail?.byte_size != null && (
          <>
            <span className="text-base-content/60">
              {t('manifest-zip-size')}
            </span>
            <span>{(packageDetail.byte_size / 1024 / 1024).toFixed(1)} MB</span>
          </>
        )}
      </div>

      {dedupCount > 0 && (
        <div className="alert alert-info text-sm" data-testid="export-dedup">
          <span>
            {t('manifest-deduplicated', {
              dedupCount,
              uniqueCount: uniquePayloadCount,
            })}
          </span>
        </div>
      )}

      {(entries.length > 0 || packageDetail?.zip_hash) && (
        <InternalView testId="export-internal-view">
          {() => (
            <>
              {packageDetail?.zip_hash && (
                <p className="text-xs">
                  {t('manifest-hash')}{' '}
                  <span className="font-mono">
                    {packageDetail.zip_hash.slice(0, 16)}…
                  </span>
                </p>
              )}
              {entries.length > 0 && (
                <div className="overflow-x-auto">
                  <table className="table table-xs">
                    <thead>
                      <tr>
                        <th>{t('manifest-version')}</th>
                        <th>{t('manifest-asset')}</th>
                        <th>{t('origin-label')}</th>
                        <th>{t('manifest-mime')}</th>
                        <th>{t('manifest-bytes')}</th>
                        <th>{t('manifest-content-hash')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {entries.map((entry) => (
                        <tr key={entry.asset_version_id}>
                          <td className="font-mono text-xs">
                            {entry.asset_version_id.slice(0, 8)}
                          </td>
                          <td className="font-mono text-xs">
                            {entry.asset_id.slice(0, 8)}
                          </td>
                          <td>
                            {entry.origin_kind
                              ? tc(originLabelKey(entry.origin_kind))
                              : '—'}
                          </td>
                          <td>{entry.mime_type ?? '—'}</td>
                          <td>{entry.byte_size ?? '—'}</td>
                          <td className="font-mono text-xs">
                            {entry.content_sha256?.slice(0, 12) ?? '—'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </InternalView>
      )}
    </div>
  );
}
