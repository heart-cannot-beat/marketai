import sample from '../fixtures/marketing-plan.json';

type Doc = Record<string, unknown>;
type Items = Doc[];

const COMPETITOR_KINDS = [
  'competitor_crowded',
  'competitor_gap',
  'competitor_copy_reuse',
];
const DETAILS = ['how_to_tell', 'visual_mechanism', 'evidence_basis'];
const STYLE_FIELDS = ['style_variation', 'style_notes', 'brand_kit'];

/**
 * #367: a plan as a draft stores it since #222 / #228 (review, expression
 * details, style); `cited` adds #273's competitor review. The project read
 * keeps all of it.
 */
export function storedPlan(cited: boolean): Doc {
  const plan = JSON.parse(JSON.stringify(sample.plan)) as Doc;
  plan.groups = (plan.groups as Items).map((g) => ({ ...g, style: '' }));
  Object.assign(plan, {
    style_variation: 'held',
    style_notes: '',
    brand_kit: null,
  });
  if (!cited) return plan;
  plan.angles = (plan.angles as Items).map((angle, i) => ({
    ...angle,
    competitor_overlap: i === 0 ? ['k1'] : [],
  }));
  plan.plan_review = [
    ...(plan.plan_review as Items),
    {
      id: 'system-competitor-crowded-1',
      kind: 'competitor_crowded',
      severity: 'advisory',
      target_ids: ['a1'],
      finding: '3 advertisers other than your own page make this claim.',
      suggestion: 'Consider a differentiated way of telling.',
      source: 'system',
      resolved: false,
      observations: [
        {
          ad_id: 'cad_1',
          cluster_id: 'k1',
          advertiser_name: 'Acme Charge',
          library_url: 'https://www.facebook.com/ads/library/?id=1234567890',
          days_running: 12,
        },
      ],
    },
  ];
  plan.competitor_snapshot_id = 'csnap_1';
  return plan;
}

/**
 * A round's frozen plan as the workflow read returns it
 * (`app/marketing/workflow.py` `round_dict`): never its competitor review,
 * and the review and style fields only when asked for.
 */
export function roundRead(
  plan: Doc,
  params: { includePlanReview?: boolean; includeBrandStyle?: boolean }
): Doc {
  const result = JSON.parse(JSON.stringify(plan)) as Doc;
  delete result.competitor_snapshot_id;
  for (const angle of result.angles as Items) delete angle.competitor_overlap;
  if (result.plan_review) {
    result.plan_review = (result.plan_review as Items).filter(
      (item) => !COMPETITOR_KINDS.includes(item.kind as string)
    );
    for (const item of result.plan_review as Items) delete item.observations;
  }
  if (!params.includePlanReview) {
    delete result.plan_review;
    delete result.guiding_questions;
    for (const angle of result.angles as Items)
      for (const expression of angle.expressions as Items)
        for (const key of DETAILS) delete expression[key];
  }
  if (!params.includeBrandStyle) {
    for (const key of STYLE_FIELDS) delete result[key];
    for (const group of result.groups as Items) delete group.style;
  }
  return result;
}
