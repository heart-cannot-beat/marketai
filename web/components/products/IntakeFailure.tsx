import { useState } from 'react';
import Link from 'next/link';
import { useTranslation } from 'next-i18next';
import {
  offersSupplement,
  suggestedAction,
  type IntakeDetail,
} from '@/lib/products/intake-model';
import type { PasteInput } from '@/hooks/products/useProductIntake';
import { PasteBox } from './PasteBox';
import { MockIntakeBadge } from './IntakeProgress';
import { errorCodeMessage } from '@/lib/i18n/domain-errors';

interface IntakeFailureProps {
  detail: IntakeDetail;
  slug: string;
  isOwner: boolean;
  lastInput: PasteInput | null;
  busy: boolean;
  onResubmit: (input: PasteInput) => void;
  onStartOver: () => void;
}

/**
 * A failed intake, explained by what the user can do next
 * (`failure.suggested_action`): add text or screenshots in place, check the
 * link, retry, or -- for the Owner -- bind the OpenAI key / adjust the spend
 * guardrail in Settings. The reason reads by the failure's code (#253);
 * the server's own sentence is a diagnostic.
 */
export function IntakeFailure({
  detail,
  slug,
  isOwner,
  lastInput,
  busy,
  onResubmit,
  onStartOver,
}: IntakeFailureProps) {
  const { t } = useTranslation('products');
  const action = suggestedAction(detail.failure);
  const [value, setValue] = useState(lastInput?.raw ?? '');
  const [files, setFiles] = useState<File[]>(lastInput?.files ?? []);

  const ownerScoped =
    action === 'bind_openai_key' || action === 'adjust_spend_guardrail';
  const guidanceKey =
    ownerScoped && !isOwner
      ? `failure-guidance-${action}-member`
      : `failure-guidance-${action}`;

  return (
    <section
      className="rounded-box border border-error/40 bg-base-100 p-6"
      aria-labelledby="intake-failure-title"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 id="intake-failure-title" className="text-lg font-semibold">
          {t('failure-title')}
        </h2>
        <MockIntakeBadge mode={detail.execution_mode} />
      </div>
      <div role="alert" className="mt-2 space-y-1">
        <p data-testid="intake-failure-guidance">{t(guidanceKey)}</p>
        {detail.failure?.code && (
          <p className="text-sm text-base-content/60">
            {errorCodeMessage(t, detail.failure.code)}
          </p>
        )}
      </div>

      {offersSupplement(action) && (
        <div className="mt-5">
          <PasteBox
            id="intake-supplement"
            label={t('failure-supplement-label')}
            value={value}
            onValueChange={setValue}
            files={files}
            onFilesChange={setFiles}
            onSubmit={() => onResubmit({ raw: value, files })}
            submitLabel={t('failure-resubmit')}
            busy={busy}
            busyLabel={t('submitting')}
            compact
          />
        </div>
      )}

      <div className="mt-5 flex flex-wrap gap-2">
        {action === 'retry' && lastInput && (
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={busy}
            onClick={() => onResubmit(lastInput)}
          >
            {t('failure-retry')}
          </button>
        )}
        {ownerScoped && isOwner && (
          <Link href={`/w/${slug}/settings`} className="btn btn-primary btn-sm">
            {action === 'bind_openai_key'
              ? t('failure-open-credentials')
              : t('failure-open-guardrail')}
          </Link>
        )}
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={onStartOver}
        >
          {t('progress-start-over')}
        </button>
      </div>
    </section>
  );
}
