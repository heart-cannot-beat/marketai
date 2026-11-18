import { useEffect, useId, useRef, useState } from 'react';
import { useTranslation } from 'next-i18next';
import type { ProblemDetails } from '@/lib/domain-client/result-types';
import {
  ACCEPTED_TYPES,
  MADE_BY,
  MAX_FILE_BYTES,
  isSettled,
  type MadeBy,
} from '@/lib/external-creatives/model';
import { ideasProblem } from '@/lib/ideas/ideas-problems';
import { errorCodeMessage } from '@/lib/i18n/domain-errors';
import type { IdeasDetail } from '@/lib/ideas/ideas-view';
import { useExternalCreatives } from '@/hooks/external-creatives/useExternalCreatives';
import type { IdeasCommands } from '@/hooks/ideas/useIdeasCommands';
import { BUTTON, PRIMARY } from './PaidImageAction';

/** An uploaded image that may be placed (#200): its upload checks are done. */
const PLACEABLE = new Set(['ready', 'tagging_failed', 'confirmed']);
const INPUT =
  'w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-base dark:border-gray-600 dark:bg-gray-900';

/**
 * "Upload my own image" (design §5.3, #200): the user's image goes under one
 * claim and way of telling, after they say who made it and that they may use
 * it in ads. It uploads like any external creative (#168), then is placed
 * (`place_marketing_ideas_upload`); from then on it is kept, checked and
 * downloaded like the other ads. It cannot be moved or made again.
 */
export function UploadOwnImage({
  detail,
  commands,
  onClose,
}: {
  detail: IdeasDetail;
  commands: Pick<IdeasCommands, 'placeUpload'>;
  onClose: () => void;
}) {
  const { t } = useTranslation('ideas');
  const ids = {
    place: useId(),
    file: useId(),
    madeBy: useId(),
    rights: useId(),
  };
  const upload = useExternalCreatives();
  const angles = detail.ideas?.angles ?? [];
  const combinations = detail.ideas?.combinations ?? [];
  const [combinationId, setCombinationId] = useState(combinations[0]?.id ?? '');
  const [file, setFile] = useState<File | null>(null);
  const [fileProblem, setFileProblem] = useState<'type' | 'size' | null>(null);
  const [madeBy, setMadeBy] = useState<MadeBy>('self');
  const [rights, setRights] = useState(false);
  const [placing, setPlacing] = useState(false);
  const [placed, setPlaced] = useState(false);
  const [problem, setProblem] = useState<ProblemDetails | null | undefined>(
    undefined
  );
  const placedFor = useRef<string | null>(null);

  const creative = upload.creatives[0];
  // Once the upload's own checks are done, place it under the chosen ad, once.
  useEffect(() => {
    if (!creative || upload.phase !== 'review') return;
    if (!isSettled(creative.status)) return;
    if (placedFor.current === creative.external_creative_id) return;
    placedFor.current = creative.external_creative_id;
    if (!PLACEABLE.has(creative.status) || !creative.asset_version_id) {
      setProblem({
        type: 'about:blank',
        title: '',
        status: 409,
        detail: '',
        instance: '',
        code: 'upload_not_ready',
        trace_id: '',
        retryable: false,
        field_errors: [],
      });
      return;
    }
    setPlacing(true);
    void commands
      .placeUpload(creative.external_creative_id, combinationId)
      .then((outcome) => {
        if (outcome.kind === 'done') setPlaced(true);
        else {
          setProblem(outcome.kind === 'refused' ? outcome.problem : null);
          if (outcome.kind === 'uncertain') placedFor.current = null;
        }
      })
      .finally(() => setPlacing(false));
  }, [combinationId, commands, creative, upload.phase]);

  function choose(next: File | null) {
    setFile(null);
    setFileProblem(null);
    if (!next) return;
    if (!ACCEPTED_TYPES.includes(next.type)) setFileProblem('type');
    else if (next.size > MAX_FILE_BYTES) setFileProblem('size');
    else setFile(next);
  }

  const working =
    upload.phase === 'uploading' ||
    upload.phase === 'submitting' ||
    (upload.phase === 'review' && !placed && problem === undefined) ||
    placing;
  const ready = Boolean(file && combinationId && rights) && !working && !placed;

  function start() {
    if (!file) return;
    setProblem(undefined);
    placedFor.current = null;
    void upload.submit({
      productId: detail.product_id,
      files: [file],
      madeBy,
      permissionConfirmed: rights,
    });
  }

  if (placed)
    return (
      <section
        className="space-y-3 rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900"
        data-testid="upload-own"
      >
        <p className="text-sm" role="status">
          {t('upload-placed')}
        </p>
        <button type="button" className={BUTTON} onClick={onClose}>
          {t('upload-close')}
        </button>
      </section>
    );

  return (
    <section
      className="space-y-3 rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900"
      aria-labelledby={`${ids.place}-title`}
      data-testid="upload-own"
    >
      <h2 id={`${ids.place}-title`} className="text-base font-semibold">
        {t('upload-title')}
      </h2>
      <p className="text-sm text-gray-600 dark:text-gray-400">
        {t('upload-intro')}
      </p>
      <label htmlFor={ids.place} className="block text-sm font-medium">
        {t('upload-where')}
      </label>
      <select
        id={ids.place}
        className={INPUT}
        value={combinationId}
        disabled={working}
        onChange={(e) => setCombinationId(e.target.value)}
      >
        {combinations.map((combination) => {
          const index = angles.findIndex((a) => a.id === combination.angle_id);
          const expression = angles[index]?.expressions.find(
            (e) => e.id === combination.expression_id
          );
          return (
            <option key={combination.id} value={combination.id}>
              {t('upload-where-option', {
                number: index + 1,
                telling: expression?.name ?? '',
                headline: combination.headline,
              })}
            </option>
          );
        })}
      </select>
      <label htmlFor={ids.file} className="block text-sm font-medium">
        {t('upload-file')}
      </label>
      <input
        id={ids.file}
        type="file"
        accept={ACCEPTED_TYPES.join(',')}
        disabled={working}
        className="block w-full text-sm"
        onChange={(e) => choose(e.target.files?.[0] ?? null)}
      />
      {fileProblem && (
        <p className="text-sm text-red-700 dark:text-red-300" role="alert">
          {t(`upload-file-${fileProblem}`)}
        </p>
      )}
      <label htmlFor={ids.madeBy} className="block text-sm font-medium">
        {t('upload-made-by')}
      </label>
      <select
        id={ids.madeBy}
        className={INPUT}
        value={madeBy}
        disabled={working}
        onChange={(e) => setMadeBy(e.target.value as MadeBy)}
      >
        {MADE_BY.map((value) => (
          <option key={value} value={value}>
            {t(`upload-made-by-${value}`)}
          </option>
        ))}
      </select>
      <div className="flex items-start gap-3">
        <input
          id={ids.rights}
          type="checkbox"
          className="mt-0.5 h-6 w-6 shrink-0 accent-gray-900"
          checked={rights}
          disabled={working}
          onChange={(e) => setRights(e.target.checked)}
        />
        <label htmlFor={ids.rights} className="min-h-11 text-sm font-medium">
          {t('upload-rights')}
        </label>
      </div>
      <p className="text-sm text-gray-600 dark:text-gray-400">
        {t('upload-note')}
      </p>
      {working && (
        <p className="text-sm" role="status">
          {t('upload-working')}
        </p>
      )}
      {upload.problem && (
        <p className="text-sm text-red-700 dark:text-red-300" role="alert">
          {upload.problem.problem
            ? errorCodeMessage(
                t,
                upload.problem.problem.code,
                upload.problem.problem.params
              )
            : t('upload-failed')}
        </p>
      )}
      {problem !== undefined && (
        <p className="text-sm text-red-700 dark:text-red-300" role="alert">
          {ideasProblem(t, problem)}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          className={PRIMARY}
          disabled={!ready}
          onClick={start}
        >
          {t('upload-start')}
        </button>
        <button type="button" className={BUTTON} onClick={onClose}>
          {t('upload-cancel')}
        </button>
      </div>
    </section>
  );
}
