import type { ReactNode } from 'react';
import Link from 'next/link';
import { useTranslation } from 'next-i18next';
import { formatDate } from '@/lib/i18n/format';
import { useGenerationNav } from '@/components/generation/shared/useGenerationNav';
import { ExecutionModeTag } from '@/components/generation/shared/ExecutionModeTag';

/**
 * Display-oriented summary consumed by `DraftList`. `list_generation_drafts`
 * (`app/generation/run_queries.py::_draft_summary`) returns
 * `{ id, product_id, provider, model, execution_mode, state, revision,
 * updated_at }` — no `title` and no `allowed_actions` (those only exist on
 * the single-draft detail, `GenerationDraftDetail`). `GenerationListView`
 * maps the raw domain items into this shape (the product's name as the
 * display title -- never the `prod_` id -- and `allowed_actions` defaulted
 * to `[]`) so this component can
 * stay decoupled from that asymmetry and remain simple to test in isolation.
 */
export interface DraftSummary {
  id: string;
  title: ReactNode;
  execution_mode: string;
  updated_at: string;
  allowed_actions: Array<{
    action: string;
    allowed: boolean;
    reason_code: string | null;
  }>;
  provider?: string;
  model?: string;
  state?: string;
}

interface DraftListProps {
  drafts: DraftSummary[];
}

export function DraftList({ drafts }: DraftListProps) {
  const { t, i18n } = useTranslation('common');
  const { buildDraftUrl } = useGenerationNav();

  if (drafts.length === 0) {
    return (
      <p className="text-sm text-base-content/60">
        {t('generation-list-drafts-empty')}
      </p>
    );
  }

  return (
    <ul className="space-y-2">
      {drafts.map((draft) => (
        <li key={draft.id} className="card card-compact bg-base-100 shadow-sm">
          <div className="card-body flex-row items-center justify-between">
            <div>
              <Link
                href={buildDraftUrl(draft.id)}
                className="font-medium hover:underline"
              >
                {draft.title}
              </Link>
              <div className="mt-1 flex items-center gap-2">
                <ExecutionModeTag
                  mode={draft.execution_mode}
                  provider={draft.provider}
                  model={draft.model}
                />
                {draft.state && (
                  <span className="badge badge-ghost badge-sm">
                    {draft.state}
                  </span>
                )}
              </div>
            </div>
            <span className="text-xs text-base-content/60">
              {draft.updated_at
                ? formatDate(i18n.language, draft.updated_at)
                : ''}
            </span>
          </div>
        </li>
      ))}
    </ul>
  );
}
