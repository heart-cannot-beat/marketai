import { useId, useState } from 'react';
import { useTranslation } from 'next-i18next';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import {
  ASPECT_RATIOS,
  IMAGE_SIZES,
  MAX_VARIANTS,
  type UseImageSettings,
} from '@/hooks/ideas/useImageSettings';
import { BUTTON } from './PaidImageAction';

type GuidingQuestion = components['schemas']['GuidingQuestion'];
type GuidingAnswer = components['schemas']['IdeasGuidingAnswer'];

const PANEL =
  'space-y-3 rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900 sm:p-5';
const INPUT =
  'w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-base dark:border-gray-600 dark:bg-gray-900';

/**
 * "Image settings" beside the ideas (design §5.3): which provider and model
 * make new ads, the aspect ratio, the size and how many per way of telling.
 * They apply to images made from now on; ads already made stay as they are.
 */
export function ImageSettingsPanel({ image }: { image: UseImageSettings }) {
  const { t } = useTranslation('ideas');
  const title = useId();
  const field = {
    provider: useId(),
    model: useId(),
    ratio: useId(),
    size: useId(),
    variants: useId(),
  };
  const [open, setOpen] = useState(false);
  const { settings, options } = image;
  const provider = options.find((p) => p.id === settings?.provider);
  return (
    <section className={PANEL} aria-labelledby={title}>
      <h2 id={title} className="text-base font-semibold">
        {t('settings-title')}
      </h2>
      {!settings ? (
        <p className="text-sm text-gray-600 dark:text-gray-400" role="note">
          {image.failed ? t('settings-failed') : t('settings-unavailable')}
        </p>
      ) : (
        <>
          <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-sm">
            <dt className="text-gray-600 dark:text-gray-400">
              {t('settings-provider')}
            </dt>
            <dd>{provider?.label ?? settings.provider}</dd>
            <dt className="text-gray-600 dark:text-gray-400">
              {t('settings-ratio')}
            </dt>
            <dd>
              {t(`settings-ratio-${settings.aspect_ratio.replace(':', '-')}`)}
            </dd>
            <dt className="text-gray-600 dark:text-gray-400">
              {t('settings-size')}
            </dt>
            <dd>{t(`settings-size-${settings.image_size}`)}</dd>
            <dt className="text-gray-600 dark:text-gray-400">
              {t('settings-variants')}
            </dt>
            <dd>
              {t('settings-variants-value', { count: settings.variants })}
            </dd>
          </dl>
          <button
            type="button"
            className={BUTTON}
            aria-expanded={open}
            onClick={() => setOpen((value) => !value)}
          >
            {open ? t('settings-done') : t('settings-change')}
          </button>
          {open && (
            <div className="space-y-3">
              <Select
                id={field.provider}
                label={t('settings-provider')}
                value={settings.provider}
                onChange={(value) =>
                  image.change({
                    provider: value as typeof settings.provider,
                    model: '',
                  })
                }
                options={options.map((p) => [p.id, p.label])}
              />
              <Select
                id={field.model}
                label={t('settings-model')}
                value={settings.model}
                onChange={(value) => image.change({ model: value })}
                options={(provider?.models ?? [])
                  .filter((m) => m.available)
                  .map((m) => [m.id, m.id])}
              />
              <Select
                id={field.ratio}
                label={t('settings-ratio')}
                value={settings.aspect_ratio}
                onChange={(value) =>
                  image.change({
                    aspect_ratio: value as typeof settings.aspect_ratio,
                  })
                }
                options={ASPECT_RATIOS.filter(
                  (ratio) =>
                    !provider?.aspect_ratios?.length ||
                    (provider.aspect_ratios as string[]).includes(ratio)
                ).map((ratio) => [
                  ratio,
                  t(`settings-ratio-${ratio.replace(':', '-')}`),
                ])}
              />
              <Select
                id={field.size}
                label={t('settings-size')}
                value={settings.image_size}
                onChange={(value) =>
                  image.change({
                    image_size: value as typeof settings.image_size,
                  })
                }
                options={IMAGE_SIZES.filter((size) =>
                  provider?.models
                    .find((m) => m.id === settings.model)
                    ?.image_sizes.includes(size)
                ).map((size) => [size, t(`settings-size-${size}`)])}
              />
              <Select
                id={field.variants}
                label={t('settings-variants')}
                value={String(settings.variants)}
                onChange={(value) => image.change({ variants: Number(value) })}
                options={Array.from({ length: MAX_VARIANTS }, (_, i) => [
                  String(i + 1),
                  t('settings-variants-value', { count: i + 1 }),
                ])}
              />
            </div>
          )}
          <p className="text-xs text-gray-600 dark:text-gray-400">
            {t('settings-note')}
          </p>
        </>
      )}
    </section>
  );
}

function Select({
  id,
  label,
  value,
  onChange,
  options,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: [string, string][];
}) {
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="block text-sm font-medium">
        {label}
      </label>
      <select
        id={id}
        className={INPUT}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        {options.map(([key, text]) => (
          <option key={key} value={key}>
            {text}
          </option>
        ))}
      </select>
    </div>
  );
}

/** The answers given so far, as an addition carries them (#200): at most three, each a wish. */
export function answersOf(
  questions: GuidingQuestion[],
  picked: Record<string, string[]>,
  own: Record<string, string>
): GuidingAnswer[] {
  const answers: GuidingAnswer[] = [];
  for (const question of questions) {
    const parts = [
      ...(picked[question.id] ?? []),
      own[question.id]?.trim() ?? '',
    ]
      .filter(Boolean)
      .join('; ');
    if (parts)
      answers.push({
        question: question.question.slice(0, 300),
        answer: parts.slice(0, 1000),
      });
  }
  return answers.slice(0, 3);
}

/**
 * Optional guiding questions (design §5.3): answers make the next claims and
 * ways of telling fit better. They are the user's wishes, never product
 * facts, and go with the next addition.
 */
export function GuidingQuestions({
  questions,
  picked,
  own,
  onPick,
  onOwn,
}: {
  questions: GuidingQuestion[];
  picked: Record<string, string[]>;
  own: Record<string, string>;
  onPick: (questionId: string, values: string[]) => void;
  onOwn: (questionId: string, text: string) => void;
}) {
  const { t } = useTranslation('ideas');
  const title = useId();
  const [open, setOpen] = useState(false);
  if (questions.length === 0) return null;
  const answered = answersOf(questions, picked, own).length;
  return (
    <section className={PANEL} aria-labelledby={title}>
      <h2 id={title} className="text-base font-semibold">
        {t('guiding-title', { count: questions.length })}
      </h2>
      <p className="text-sm text-gray-600 dark:text-gray-400">
        {t('guiding-intro')}
      </p>
      {answered > 0 && !open && (
        <p className="text-sm" role="status">
          {t('guiding-answered', { count: answered })}
        </p>
      )}
      <button
        type="button"
        className={BUTTON}
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        {open ? t('guiding-done') : t('guiding-answer')}
      </button>
      {open &&
        questions.map((question) => (
          <fieldset key={question.id} className="space-y-2">
            <legend className="text-sm font-medium">{question.question}</legend>
            {question.why_needed && (
              <p className="text-xs text-gray-600 dark:text-gray-400">
                {question.why_needed}
              </p>
            )}
            {question.options.map((option) => {
              const values = picked[question.id] ?? [];
              const on = values.includes(option);
              return (
                <label
                  key={option}
                  className="flex min-h-11 items-center gap-2 text-sm"
                >
                  <input
                    type={question.multiple ? 'checkbox' : 'radio'}
                    name={question.id}
                    className="h-5 w-5 accent-gray-900"
                    checked={on}
                    onChange={() =>
                      onPick(
                        question.id,
                        question.multiple
                          ? on
                            ? values.filter((v) => v !== option)
                            : [...values, option]
                          : [option]
                      )
                    }
                  />
                  {option}
                </label>
              );
            })}
            <input
              type="text"
              className={INPUT}
              aria-label={t('guiding-own', { question: question.question })}
              placeholder={t('guiding-own-placeholder')}
              maxLength={500}
              value={own[question.id] ?? ''}
              onChange={(e) => onOwn(question.id, e.target.value)}
            />
          </fieldset>
        ))}
    </section>
  );
}
