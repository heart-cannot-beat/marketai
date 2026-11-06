import { useTranslation } from 'next-i18next';
import { useId } from 'react';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import { CompetitorReviewObservations } from './CompetitorReviewObservations';

type Plan = components['schemas']['FrozenTestPlan'];
type Expression = components['schemas']['FrozenExpression'];
type Fact = components['schemas']['Fact'];
type GuidingQuestion = components['schemas']['GuidingQuestion'];
type ReviewItem = components['schemas']['ReviewItem'];
type Messages = (path: string) => string[];
export type ExpressionDetails = Pick<
  Expression,
  'how_to_tell' | 'visual_mechanism' | 'evidence_basis'
>;

// Mirrors app/marketing/expression_catalog.py, a reference list rather than
// a closed enum: anything else is proposed as `other:<description>`.
export const VISUAL_MECHANISMS = [
  'route_map',
  'live_connection_demo',
  'scenario_story',
  'certification_walkthrough',
  'qa_objection',
  'test_process',
  'side_by_side_comparison',
  'owner_testimonial',
  'feature_closeup',
  'fit_guide',
] as const;
const OTHER = 'other:';
// #273: findings the server derives from the cited competitor snapshot; their copy is in `competitors`.
const COMPETITOR_KINDS: readonly ReviewItem['kind'][] = [
  'competitor_crowded',
  'competitor_gap',
  'competitor_copy_reuse',
];
export const isCompetitorKind = (kind: ReviewItem['kind']) =>
  COMPETITOR_KINDS.includes(kind);
const INPUT =
  'w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm focus:border-gray-600 focus:outline-none focus:ring-2 focus:ring-gray-200';
const SECONDARY =
  'rounded-lg border border-gray-300 px-4 py-2 text-sm disabled:opacity-40';

export function expressionDetailsMissing(expression: Expression) {
  return (
    !expression.how_to_tell ||
    !expression.visual_mechanism ||
    !expression.evidence_basis
  );
}

export function planDetailsMissing(plan: Plan | null | undefined) {
  return !!plan?.angles.some((a) =>
    a.expressions.some(expressionDetailsMissing)
  );
}

const answered = (question: GuidingQuestion) =>
  !!(question.selected ?? []).length || !!question.custom_answer;

/** System findings are recomputed by the server on confirmation. */
export function planReviewBlocked(plan: Plan) {
  return (
    (plan.plan_review ?? []).some(
      (r) => r.source !== 'system' && r.severity === 'blocking' && !r.resolved
    ) || (plan.guiding_questions ?? []).some((q) => !answered(q))
  );
}

export function nextMechanism(expressions: Expression[]) {
  const used = new Set(expressions.map((e) => e.visual_mechanism));
  return VISUAL_MECHANISMS.find((key) => !used.has(key)) ?? OTHER;
}

/** Writes require every element; blanks let the server report each one in place. */
export function planBody(plan: Plan) {
  return {
    ...plan,
    plan_review: plan.plan_review ?? [],
    guiding_questions: plan.guiding_questions ?? [],
    angles: plan.angles.map((angle) => ({
      ...angle,
      expressions: angle.expressions.map((expression) => ({
        ...expression,
        how_to_tell: expression.how_to_tell ?? '',
        visual_mechanism: expression.visual_mechanism ?? '',
        evidence_basis: expression.evidence_basis ?? { fact_ids: [], gap: '' },
      })),
    })),
  };
}

function Errors({ id, errors }: { id: string; errors: string[] }) {
  return errors.length ? (
    <p id={id} className="text-sm text-red-700">
      {errors.join(' ')}
    </p>
  ) : null;
}

export function MarketingExpressionDetails({
  expression,
  facts,
  path,
  flagged,
  messages,
  onChange,
}: {
  expression: Expression;
  facts: Fact[];
  path: string;
  flagged: boolean;
  messages: Messages;
  onChange: (details: Partial<ExpressionDetails>) => void;
}) {
  const { t } = useTranslation('common');
  const id = useId();
  const mechanism = expression.visual_mechanism ?? '';
  const custom = mechanism.startsWith(OTHER);
  const known = (VISUAL_MECHANISMS as readonly string[]).includes(mechanism);
  const evidence = expression.evidence_basis ?? { fact_ids: [], gap: '' };
  const errors = {
    tell: messages(`${path}.how_to_tell`),
    mechanism: messages(`${path}.visual_mechanism`),
    evidence: messages(`${path}.evidence_basis`),
  };
  return (
    <div className="mt-3 space-y-3 rounded-lg bg-gray-50 p-3 text-sm">
      {expressionDetailsMissing(expression) && (
        <p className="text-xs font-medium text-amber-800">
          {t('marketing-expression-details-missing')}
        </p>
      )}
      {flagged && (
        <p className="text-xs font-medium text-red-700">
          {t('marketing-expression-duplicate-flag')}
        </p>
      )}
      <div
        data-validation-field={`${path}.how_to_tell`}
        className="space-y-1.5"
      >
        <label htmlFor={`${id}-tell`} className="font-medium text-gray-700">
          {t('marketing-expression-how-to-tell')}
        </label>
        <textarea
          id={`${id}-tell`}
          className={INPUT}
          rows={2}
          aria-invalid={errors.tell.length > 0 || undefined}
          aria-describedby={
            errors.tell.length ? `${id}-tell-errors` : undefined
          }
          value={expression.how_to_tell ?? ''}
          onChange={(e) => onChange({ how_to_tell: e.target.value })}
        />
        <Errors id={`${id}-tell-errors`} errors={errors.tell} />
      </div>
      <div
        data-validation-field={`${path}.visual_mechanism`}
        className="space-y-1.5"
      >
        <label
          htmlFor={`${id}-mechanism`}
          className="font-medium text-gray-700"
        >
          {t('marketing-expression-visual-mechanism')}
        </label>
        <select
          id={`${id}-mechanism`}
          className={INPUT}
          aria-invalid={errors.mechanism.length > 0 || undefined}
          aria-describedby={
            errors.mechanism.length ? `${id}-mechanism-errors` : undefined
          }
          value={custom ? OTHER : mechanism}
          onChange={(e) => onChange({ visual_mechanism: e.target.value })}
        >
          <option value="" disabled>
            {t('marketing-expression-visual-mechanism-choose')}
          </option>
          {VISUAL_MECHANISMS.map((key) => (
            <option key={key} value={key}>
              {t(`marketing-visual-mechanism-${key}`)}
            </option>
          ))}
          {!!mechanism && !known && !custom && (
            <option value={mechanism}>{mechanism}</option>
          )}
          <option value={OTHER}>
            {t('marketing-expression-visual-mechanism-other')}
          </option>
        </select>
        {custom && (
          <input
            className={INPUT}
            aria-label={t('marketing-expression-visual-mechanism-custom')}
            value={mechanism.slice(OTHER.length)}
            onChange={(e) =>
              onChange({ visual_mechanism: OTHER + e.target.value })
            }
          />
        )}
        <Errors id={`${id}-mechanism-errors`} errors={errors.mechanism} />
      </div>
      <fieldset
        data-validation-field={`${path}.evidence_basis`}
        className="space-y-1.5"
        aria-describedby={
          errors.evidence.length ? `${id}-evidence-errors` : undefined
        }
      >
        <legend className="font-medium text-gray-700">
          {t('marketing-expression-evidence-basis')}
        </legend>
        {facts.map((fact) => (
          <label key={fact.id} className="flex items-start gap-2 text-gray-600">
            <input
              type="checkbox"
              className="mt-1"
              checked={(evidence.fact_ids ?? []).includes(fact.id)}
              onChange={(e) =>
                onChange({
                  evidence_basis: {
                    ...evidence,
                    fact_ids: e.target.checked
                      ? [...(evidence.fact_ids ?? []), fact.id]
                      : (evidence.fact_ids ?? []).filter((f) => f !== fact.id),
                  },
                })
              }
            />
            <span>{fact.statement}</span>
          </label>
        ))}
        <label className="block space-y-1.5">
          <span className="text-gray-600">
            {t('marketing-expression-evidence-gap')}
          </span>
          <textarea
            className={INPUT}
            rows={1}
            value={evidence.gap}
            onChange={(e) =>
              onChange({ evidence_basis: { ...evidence, gap: e.target.value } })
            }
          />
        </label>
        <Errors id={`${id}-evidence-errors`} errors={errors.evidence} />
      </fieldset>
    </div>
  );
}

export function MarketingPlanReview({
  plan,
  savedQuestions,
  disabled,
  redraftDisabled,
  costLabel,
  messages,
  onChange,
  onSave,
  onRedraft,
}: {
  plan: Plan;
  savedQuestions: GuidingQuestion[] | undefined;
  disabled: boolean;
  redraftDisabled: boolean;
  costLabel: string;
  messages: Messages;
  onChange: (plan: Plan) => void;
  onSave: () => void;
  onRedraft: () => void;
}) {
  const { t } = useTranslation(['common', 'competitors']);
  const id = useId();
  if (!plan.plan_review && !plan.guiding_questions) return null;
  const review = plan.plan_review ?? [];
  const questions = plan.guiding_questions ?? [];
  const names = new Map<string, string>([
    ...plan.angles.map((a) => [a.id, a.proposition] as [string, string]),
    ...plan.angles.flatMap((a) =>
      a.expressions.map((e) => [e.id, e.name] as [string, string])
    ),
    ...plan.combinations.map((c) => [c.id, c.headline] as [string, string]),
    ...plan.groups.map((g) => [g.id, g.name] as [string, string]),
  ]);
  const ordered = review
    .map((item, index) => ({ item, index }))
    .sort(
      (a, b) =>
        Number(a.item.source !== 'system') - Number(b.item.source !== 'system')
    );
  const answersSaved =
    questions.some(answered) &&
    JSON.stringify(questions) === JSON.stringify(savedQuestions ?? []);
  const updateQuestion = (index: number, next: GuidingQuestion) =>
    onChange({
      ...plan,
      guiding_questions: questions.map((q, i) => (i === index ? next : q)),
    });
  return (
    <section
      aria-labelledby={`${id}-title`}
      className="space-y-4 rounded-xl border border-gray-200 p-5"
    >
      <div>
        <h3 id={`${id}-title`} className="text-base font-semibold">
          {t('marketing-plan-review-title')}
        </h3>
        <p className="mt-1 text-sm text-gray-600">
          {review.length || questions.length
            ? t('marketing-plan-review-intro')
            : t('marketing-plan-review-empty')}
        </p>
      </div>
      {ordered.length > 0 && (
        <ul className="space-y-3">
          {ordered.map(({ item, index }) => {
            const path = `plan.plan_review.${index}`;
            const targets = (item.target_ids ?? [])
              .map((target) => names.get(target))
              .filter(Boolean);
            return (
              <li
                key={item.id}
                data-validation-field={path}
                className={`rounded-lg border p-3 text-sm ${
                  item.severity === 'blocking' && !item.resolved
                    ? 'border-red-200 bg-red-50'
                    : 'border-gray-200'
                }`}
              >
                <p className="font-medium">
                  <span className="mr-2 rounded bg-white px-1.5 py-0.5 text-xs">
                    {t(`marketing-review-severity-${item.severity}`)}
                  </span>
                  {isCompetitorKind(item.kind)
                    ? t(`review-kind-${item.kind}`, { ns: 'competitors' })
                    : t(`marketing-review-kind-${item.kind}`)}
                </p>
                <p className="mt-1 leading-6">{item.finding}</p>
                <p className="mt-1 leading-6 text-gray-600">
                  {t('marketing-review-suggestion')}
                  {item.suggestion}
                </p>
                {targets.length > 0 && (
                  <p className="mt-1 text-xs text-gray-500">
                    {t('marketing-review-targets', {
                      targets: targets.join(' · '),
                    })}
                  </p>
                )}
                <CompetitorReviewObservations item={item} />
                {item.source === 'system' ? (
                  <p className="mt-2 text-xs text-gray-500">
                    {isCompetitorKind(item.kind)
                      ? t(`review-system-${item.kind}`, { ns: 'competitors' })
                      : t(`marketing-review-system-${item.kind}`)}
                  </p>
                ) : (
                  <label className="mt-2 flex items-center gap-2 text-xs">
                    <input
                      type="checkbox"
                      checked={item.resolved}
                      disabled={disabled}
                      onChange={(e) =>
                        onChange({
                          ...plan,
                          plan_review: review.map((r, i) =>
                            i === index
                              ? { ...r, resolved: e.target.checked }
                              : r
                          ),
                        })
                      }
                    />
                    {t('marketing-review-resolved')}
                  </label>
                )}
                <Errors id={`${id}-review-${index}`} errors={messages(path)} />
              </li>
            );
          })}
        </ul>
      )}
      {questions.length > 0 && (
        <div className="space-y-3">
          <h4 className="text-sm font-semibold">
            {t('marketing-guiding-questions-title')}
          </h4>
          {questions.map((question, index) => {
            const path = `plan.guiding_questions.${index}`;
            const selected = question.selected ?? [];
            return (
              <fieldset
                key={question.id}
                data-validation-field={path}
                disabled={disabled}
                className="space-y-2 rounded-lg border border-gray-200 p-3 text-sm"
              >
                <legend className="px-1 font-medium">
                  {question.question}
                </legend>
                <p className="text-xs text-gray-500">
                  {t('marketing-guiding-question-why')}
                  {question.why_needed}
                </p>
                {question.options.map((option) => (
                  <label key={option} className="flex items-center gap-2">
                    <input
                      type={question.multiple ? 'checkbox' : 'radio'}
                      name={`${id}-${question.id}`}
                      checked={selected.includes(option)}
                      onChange={(e) =>
                        updateQuestion(index, {
                          ...question,
                          selected: !question.multiple
                            ? [option]
                            : e.target.checked
                              ? [...selected, option]
                              : selected.filter((s) => s !== option),
                        })
                      }
                    />
                    {option}
                  </label>
                ))}
                <label className="block space-y-1">
                  <span className="text-gray-600">
                    {t('marketing-guiding-custom-answer')}
                  </span>
                  <input
                    className={INPUT}
                    value={question.custom_answer}
                    onChange={(e) =>
                      updateQuestion(index, {
                        ...question,
                        custom_answer: e.target.value,
                      })
                    }
                  />
                </label>
                <Errors
                  id={`${id}-question-${index}`}
                  errors={messages(path)}
                />
              </fieldset>
            );
          })}
        </div>
      )}
      {planReviewBlocked(plan) && (
        <p className="text-sm text-amber-800">
          {t('marketing-plan-review-unresolved-hint')}
        </p>
      )}
      {!disabled &&
        (review.some((r) => r.source !== 'system') || questions.length > 0) && (
          <div className="flex flex-wrap gap-3">
            <button className={SECONDARY} onClick={onSave}>
              {t('marketing-guiding-save-answers')}
            </button>
            {questions.length > 0 && (
              <button
                className={SECONDARY}
                disabled={redraftDisabled || !answersSaved}
                onClick={onRedraft}
              >
                {t('marketing-guiding-redraft')}
                {costLabel}
              </button>
            )}
            {questions.length > 0 && !answersSaved && (
              <p className="w-full text-xs text-gray-500">
                {t('marketing-guiding-redraft-hint')}
              </p>
            )}
          </div>
        )}
    </section>
  );
}
