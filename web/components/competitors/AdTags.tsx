import { useState } from 'react';
import { useTranslation } from 'next-i18next';
import useSWR from 'swr';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import { formatDateTime } from '@/lib/i18n/format';
import {
  dataOf,
  problemMessage,
  useCommand,
  type CompetitorAd,
  type CompetitorAdTagAssessment,
  type CompetitorSnapshotSummary,
  type CompetitorTagVocabulary,
} from './shared';

const SECONDARY =
  'rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm disabled:opacity-40';
const BUTTON =
  'rounded-lg bg-gray-900 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-40';

/** Factor and value labels of the scan's frozen vocabulary, by ID. */
export type TagLabels = Map<
  string,
  { label: string; values: Map<string, string> }
>;

/**
 * The summary's dimension counts list every factor and value of the scan's
 * frozen vocabulary, so the cards can name their tags without a request each.
 */
export function tagLabelsOf(
  summary: CompetitorSnapshotSummary | null | undefined
): TagLabels {
  return new Map(
    (summary?.dimension_counts ?? []).map((factor) => [
      factor.factor_id,
      {
        label: factor.label,
        values: new Map(
          (factor.values ?? []).map((value) => [value.value_id, value.label])
        ),
      },
    ])
  );
}

function vocabularyLabels(vocabulary: CompetitorTagVocabulary): TagLabels {
  return new Map(
    (vocabulary.factors ?? []).map((factor) => [
      factor.factor_id,
      {
        label: factor.label,
        values: new Map(
          (factor.values ?? []).map((value) => [value.value_id, value.label])
        ),
      },
    ])
  );
}

/**
 * An observation's dimension tags (spec §7.1): the scan's AI suggestion until
 * a member confirms or changes it. Both are revisions; the suggestion stays
 * in the history, and only `ai` reads as "AI suggestion".
 */
export function AdTags({
  ad,
  labels,
  onChanged,
}: {
  ad: CompetitorAd;
  labels: TagLabels;
  onChanged: () => Promise<unknown>;
}) {
  const { t } = useTranslation('competitors');
  const [editing, setEditing] = useState(false);
  const tags = ad.tags ?? null;
  const chosen = (tags?.factors ?? []).filter((item) => item.value_id);

  return (
    <div
      className="space-y-2 border-t border-gray-100 pt-2 text-sm"
      data-testid="competitor-ad-tags"
      data-source={tags?.source ?? ''}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-medium text-gray-700">
          {t('card-tags-title')}
        </span>
        {tags && (
          <span
            className={`rounded px-1.5 py-0.5 text-xs ${
              tags.ai_suggestion
                ? 'bg-amber-100 font-medium text-amber-900'
                : 'bg-gray-100 text-gray-700'
            }`}
            data-testid="competitor-ad-tags-state"
          >
            {tags.ai_suggestion
              ? t('card-tags-suggested')
              : t('card-tags-confirmed')}
          </span>
        )}
      </div>
      {!tags && <p className="text-xs text-gray-600">{t('card-tags-none')}</p>}
      {tags && chosen.length === 0 && (
        <p className="text-xs text-gray-600">{t('card-tags-empty')}</p>
      )}
      {chosen.length > 0 && (
        <ul className="flex flex-wrap gap-1">
          {chosen.map((item) => {
            const factor = labels.get(item.factor_id);
            const value = factor?.values.get(item.value_id ?? '');
            return (
              <li
                key={item.factor_id}
                className="break-words rounded bg-gray-100 px-1.5 py-0.5 text-xs"
              >
                {factor && value
                  ? t('card-tags-value', { factor: factor.label, value })
                  : t('card-tags-value-unnamed')}
              </li>
            );
          })}
        </ul>
      )}
      {tags?.ai_suggestion && tags.summary && (
        <p className="break-words text-xs text-gray-600">{tags.summary}</p>
      )}
      {!editing && (
        <button
          type="button"
          className="text-xs underline"
          onClick={() => setEditing(true)}
          data-testid="competitor-ad-tags-edit"
        >
          {tags?.ai_suggestion ? t('card-tags-review') : t('card-tags-edit')}
        </button>
      )}
      {editing && (
        <TagEditor
          ad={ad}
          onClose={() => setEditing(false)}
          onConfirmed={async () => {
            setEditing(false);
            await onChanged();
          }}
        />
      )}
    </div>
  );
}

function TagEditor({
  ad,
  onClose,
  onConfirmed,
}: {
  ad: CompetitorAd;
  onClose: () => void;
  onConfirmed: () => Promise<unknown>;
}) {
  const { t, i18n } = useTranslation('competitors');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const command = useCommand();
  const list = useSWR(
    ['competitor-ad-tags', current.workspaceId, ad.ad_id, ad.tags?.revision],
    async () =>
      dataOf(
        await gateway.listCompetitorAdTags({
          workspaceSlug: current.slug,
          scanId: ad.scan_id,
          adId: ad.ad_id,
        })
      )
  );
  const [choice, setChoice] = useState<Record<string, string>>({});

  if (list.error)
    return (
      <p className="text-xs text-red-700" role="alert">
        {problemMessage(t, list.error)}
      </p>
    );
  if (!list.data)
    return (
      <p className="text-xs text-gray-500" role="status">
        {t('card-tags-loading')}
      </p>
    );

  const vocabulary = list.data.vocabulary;
  const factors = vocabulary?.factors ?? [];
  const inEffect = new Map(
    (list.data.current?.factors ?? []).map((item) => [
      item.factor_id ?? '',
      item.value_id ?? '',
    ])
  );
  const valueOf = (factorId: string) =>
    choice[factorId] ?? inEffect.get(factorId) ?? '';

  async function confirm() {
    const body = {
      tags: factors.map((factor) => ({
        factor_id: factor.factor_id,
        value_id: valueOf(factor.factor_id) || null,
      })),
    };
    const done = await command.run(
      `tags:${ad.ad_id}:${JSON.stringify(body.tags)}`,
      async (action) =>
        dataOf(
          await gateway.confirmCompetitorAdTags({
            workspaceSlug: current.slug,
            actionInstanceId: action,
            scanId: ad.scan_id,
            adId: ad.ad_id,
            body,
          })
        )
    );
    if (done) await onConfirmed();
  }

  return (
    <div
      className="space-y-3 rounded-lg bg-gray-50 p-3"
      data-testid="competitor-tag-editor"
    >
      {factors.length === 0 ? (
        <p className="text-xs text-gray-600">{t('card-tags-no-vocabulary')}</p>
      ) : (
        <>
          <p className="text-xs text-gray-600">{t('card-tags-hint')}</p>
          {factors.map((factor) => (
            <label key={factor.factor_id} className="block space-y-1">
              <span className="block break-words text-xs font-medium">
                {factor.label}
              </span>
              <select
                className="w-full rounded-lg border border-gray-300 bg-white px-2 py-1.5 text-sm"
                value={valueOf(factor.factor_id)}
                onChange={(event) =>
                  setChoice((previous) => ({
                    ...previous,
                    [factor.factor_id]: event.target.value,
                  }))
                }
              >
                <option value="">{t('card-tags-value-none')}</option>
                {(factor.values ?? []).map((value) => (
                  <option key={value.value_id} value={value.value_id}>
                    {value.label}
                  </option>
                ))}
              </select>
            </label>
          ))}
        </>
      )}
      <div className="flex flex-wrap gap-2">
        {factors.length > 0 && (
          <button
            type="button"
            className={BUTTON}
            disabled={!!command.busy}
            onClick={() => void confirm()}
            data-testid="competitor-tag-confirm"
          >
            {t('card-tags-confirm')}
          </button>
        )}
        <button type="button" className={SECONDARY} onClick={onClose}>
          {t('close')}
        </button>
      </div>
      {command.errorFor('tags:') && (
        <p className="text-xs text-red-700" role="alert">
          {command.errorFor('tags:')}
        </p>
      )}
      {(list.data.items ?? []).length > 0 && (
        <TagHistory
          items={list.data.items ?? []}
          labels={vocabulary ? vocabularyLabels(vocabulary) : new Map()}
          locale={i18n.language}
        />
      )}
    </div>
  );
}

function TagHistory({
  items,
  labels,
  locale,
}: {
  items: CompetitorAdTagAssessment[];
  labels: TagLabels;
  locale: string;
}) {
  const { t } = useTranslation('competitors');
  return (
    <div className="space-y-1 text-xs">
      <p className="font-medium">{t('card-tags-history')}</p>
      <ol className="space-y-2" data-testid="competitor-tag-history">
        {[...items].reverse().map((item) => {
          const chosen = (item.factors ?? []).filter((f) => f.value_id);
          return (
            <li
              key={item.assessment_id}
              className="space-y-0.5"
              data-testid="competitor-tag-history-entry"
              data-source={item.source}
            >
              <p className="flex flex-wrap items-center gap-1">
                <span className="font-medium">
                  {t('card-history-revision', { revision: item.revision })}
                </span>
                <span
                  className={
                    item.ai_suggestion
                      ? 'rounded bg-amber-100 px-1.5 py-0.5 font-medium text-amber-900'
                      : 'rounded bg-gray-100 px-1.5 py-0.5 text-gray-700'
                  }
                >
                  {item.ai_suggestion
                    ? t('card-tags-suggested')
                    : t('card-tags-confirmed')}
                </span>
                {item.created_at && (
                  <span className="text-gray-500">
                    {formatDateTime(locale, item.created_at, {
                      dateStyle: 'medium',
                      timeStyle: 'short',
                    })}
                  </span>
                )}
              </p>
              <p className="break-words text-gray-600">
                {chosen.length
                  ? chosen
                      .map((f) => {
                        const factor = labels.get(f.factor_id ?? '');
                        return t('card-tags-value', {
                          factor: f.factor_label ?? factor?.label ?? '',
                          value:
                            f.value_label ??
                            factor?.values.get(f.value_id ?? '') ??
                            '',
                        });
                      })
                      .join(t('card-tags-separator'))
                  : t('card-tags-empty')}
              </p>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
