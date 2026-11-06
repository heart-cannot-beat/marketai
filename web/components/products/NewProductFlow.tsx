import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { useTranslation } from 'next-i18next';
import { ArrowLeftIcon } from '@heroicons/react/24/outline';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import {
  useProductIntake,
  type FlowProblem,
} from '@/hooks/products/useProductIntake';
import { stashProductHandoff } from '@/lib/products/handoff';
import { PasteBox } from './PasteBox';
import { IntakeProgress } from './IntakeProgress';
import { IntakeFailure } from './IntakeFailure';
import { ProductCardEditor } from './ProductCardEditor';
import { errorMessage } from '@/lib/i18n/domain-errors';

type TFunction = ReturnType<typeof useTranslation>['t'];

const CONFIRM_CODES = new Set([
  'variant_selection_required',
  'unknown_variant',
  'image_not_available',
  'too_many_images',
  'primary_audience_out_of_range',
  'library_validation_failed',
  'intake_not_ready',
  'intake_already_confirmed',
  'intake_confirmation_in_progress',
]);

function describeProblem(problem: FlowProblem, t: TFunction): string {
  if (problem.stage === 'upload') {
    return problem.code === 'upload_rejected'
      ? t('problem-upload-rejected')
      : t('problem-upload');
  }
  const reason = problem.problem ? errorMessage(t, problem.problem) : '';
  if (problem.stage === 'intake') {
    if (problem.code === 'input_required') return t('problem-input-required');
    return reason
      ? t('problem-intake', { message: reason })
      : t('problem-intake-unknown');
  }
  if (CONFIRM_CODES.has(problem.code)) {
    return t(`confirm-problem-${problem.code}`, { message: reason });
  }
  return reason
    ? t('confirm-problem-default', { message: reason })
    : t('confirm-problem-unknown');
}

/**
 * `/w/{slug}/products/new` (Issue #162): paste → recognise → edit the card
 * → confirm → back to the product home with the new product highlighted.
 */
export function NewProductFlow() {
  const { t } = useTranslation('products');
  const router = useRouter();
  const { current } = useWorkspace();
  const slug = current.slug;
  const isOwner = current.role === 'OWNER';
  const intake = useProductIntake();
  const [value, setValue] = useState('');
  const [files, setFiles] = useState<File[]>([]);

  const { phase, detail, problem } = intake;
  const busy =
    phase === 'uploading' || phase === 'submitting' || phase === 'confirming';
  const busyLabel =
    phase === 'uploading' && intake.uploadProgress
      ? t('upload-progress', intake.uploadProgress)
      : t('submitting');

  // Confirmed (here, or found confirmed after a refresh): back to the
  // product home with the new product highlighted once.
  const navigatedRef = useRef(false);
  useEffect(() => {
    if (phase !== 'confirmed' || navigatedRef.current) return;
    navigatedRef.current = true;
    const productId =
      detail?.product_id ?? detail?.confirmation?.product_id ?? null;
    if (productId) stashProductHandoff('highlight', slug, productId);
    void router.push(`/w/${slug}/products`);
  }, [detail, phase, router, slug]);

  function startOver() {
    intake.startOver();
  }

  const showInput =
    phase === 'input' || phase === 'uploading' || phase === 'submitting';
  const showCard =
    (phase === 'ready' || phase === 'confirming') &&
    detail?.card &&
    detail.intake_id;

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <div>
        <Link
          href={`/w/${slug}/products`}
          className="link-hover link inline-flex items-center gap-1 text-sm text-base-content/70"
        >
          <ArrowLeftIcon className="h-4 w-4" aria-hidden="true" />
          {t('back-to-products')}
        </Link>
        <h1 className="mt-2 text-2xl font-bold outline-none">
          {t('new-title')}
        </h1>
        <p className="mt-1 max-w-prose text-sm text-base-content/70">
          {t('new-intro')}
        </p>
      </div>

      {problem && showInput && problem.stage !== 'confirm' && (
        <div className="alert alert-error" role="alert">
          <span>{describeProblem(problem, t)}</span>
        </div>
      )}

      {showInput && (
        <PasteBox
          id="product-intake-input"
          label={t('paste-label')}
          value={value}
          onValueChange={setValue}
          files={files}
          onFilesChange={setFiles}
          onSubmit={() => void intake.submit({ raw: value, files })}
          submitLabel={t('paste-submit')}
          busy={busy}
          busyLabel={busyLabel}
          autoFocus
        />
      )}

      {(phase === 'working' || phase === 'stalled') && (
        <IntakeProgress
          detail={detail}
          stalled={phase === 'stalled'}
          onStartOver={startOver}
          onKeepWaiting={intake.resumePolling}
        />
      )}

      {phase === 'failed' && detail && (
        <IntakeFailure
          detail={detail}
          slug={slug}
          isOwner={isOwner}
          lastInput={intake.lastInput}
          busy={busy}
          onResubmit={(input) => {
            setValue(input.raw);
            setFiles(input.files);
            void intake.submit(input);
          }}
          onStartOver={startOver}
        />
      )}

      {showCard && detail?.card && (
        <ProductCardEditor
          key={detail.intake_id}
          intakeId={detail.intake_id}
          card={detail.card}
          executionMode={detail.execution_mode}
          confirming={phase === 'confirming'}
          confirmProblem={
            problem?.stage === 'confirm' ? describeProblem(problem, t) : null
          }
          onConfirm={(body) => void intake.confirm(body)}
          onStartOver={startOver}
        />
      )}

      {phase === 'confirmed' && (
        <div className="flex justify-center py-8" role="status">
          <span className="loading loading-spinner" aria-hidden="true" />
        </div>
      )}
    </div>
  );
}
