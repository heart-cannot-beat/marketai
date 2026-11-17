import type { components } from '@/lib/domain-client/generated/domain-v1';
import type { WorkspaceProduct } from '@/lib/products/workspace-products';

/**
 * A fictional Acme Charge product's ad ideas (ADR 0018, #196): three claims
 * with two ways of telling each, as the first batch drafts them.
 */
type Detail = components['schemas']['IdeasProjectDetail'];
type Ad = components['schemas']['AdSummary'];
type Angle = components['schemas']['Angle'];

const evidence = { fact_ids: ['f1'], gap: '' };

function telling(id: string, name: string, description: string) {
  return {
    id,
    name,
    description,
    fit_reason: 'It suits the claim.',
    how_to_tell: description,
    visual_mechanism: name,
    evidence_basis: evidence,
  };
}

export const ANGLES: Angle[] = [
  {
    id: 'a1',
    proposition: 'Plug in at night and forget about it',
    rationale: 'Owners charge at home overnight and want it to just work.',
    fact_ids: ['f1'],
    expressions: [
      telling('e1', 'Bedtime story', 'Plugging in on the way to bed.'),
      telling('e2', 'App demo', 'Setting the charging hours once.'),
    ],
  },
  {
    id: 'a2',
    proposition: 'Safe to leave outside',
    rationale: 'Many driveways have no garage.',
    fact_ids: ['f2'],
    expressions: [
      telling('e3', 'Close-up', 'The enclosure in the rain.'),
      telling(
        'e4',
        'Questions and answers',
        'Answering "can it live outside?".'
      ),
    ],
  },
  {
    id: 'a3',
    proposition: 'No more tripped breakers',
    rationale: 'Load balancing shares power with the home.',
    fact_ids: ['f3'],
    expressions: [
      telling(
        'e5',
        'Side by side',
        'The charger easing off when the dryer runs.'
      ),
      telling(
        'e6',
        'Questions and answers',
        'Answering "will it trip my breaker?".'
      ),
    ],
  },
];

function combination(
  id: string,
  angle: string,
  expression: string,
  headline: string
) {
  return {
    id,
    angle_id: angle,
    expression_id: expression,
    headline,
    body_copy: `${headline} Acme Charge Home.`,
    visual_brief: 'A home charger in a driveway.',
    checks: ['The charger looks like the product photo.'],
  };
}

export const COMBINATIONS = [
  combination('c1', 'a1', 'e1', 'Plug in. Go to bed.'),
  combination('c2', 'a1', 'e2', 'Set it once.'),
  combination('c3', 'a2', 'e3', 'Rain or shine.'),
  combination('c4', 'a2', 'e4', 'Can it live outside?'),
  combination('c5', 'a3', 'e5', 'Dryer on? It eases off.'),
  combination('c6', 'a3', 'e6', 'Will it trip my breaker?'),
];

export function ad(
  id: string,
  combinationId: string,
  extra: Partial<Ad> = {}
): Ad {
  const combo = COMBINATIONS.find((c) => c.id === combinationId)!;
  return {
    asset_version_id: id,
    combination_id: combinationId,
    angle_id: combo?.angle_id ?? 'a_gone',
    expression_id: combo?.expression_id ?? 'e_gone',
    status: 'draft',
    kept: false,
    used_product_photo: true,
    created_at: '2026-11-17T08:00:00+00:00',
    ...extra,
  };
}

export function ideasDetail(extra: Partial<Detail> = {}): Detail {
  return {
    id: 'mp_secret',
    revision: 3,
    state: 'ideas_ready',
    product_id: 'prod_home',
    market: 'United States',
    ad_language: 'en-US',
    understanding: {
      product_name: 'Acme Charge Home',
      category_need: 'Charging an electric car at home.',
      product_choice_reason: 'Scheduling and load balancing.',
      audience: 'Electric car owners with a driveway.',
      situations: ['Charging overnight'],
      objections: [],
      facts: [
        {
          id: 'f1',
          statement: 'Schedule charging in the Acme Charge app',
          source_quote: 'Schedule charging from the Acme Charge app.',
          confirmation: 'unconfirmed',
        },
        {
          id: 'f2',
          statement: 'IP55-rated enclosure',
          source_quote: 'An IP55-rated enclosure for outdoor installs.',
          confirmation: 'unconfirmed',
        },
        {
          id: 'f3',
          statement: 'Load balancing with the home',
          source_quote: 'Load balancing adjusts to your home.',
          confirmation: 'confirmed',
        },
      ],
      inferences: [
        {
          id: 'i1',
          statement: 'Owners want charging to need no attention.',
          rationale: 'Overnight charging.',
          fact_ids: ['f1'],
        },
      ],
      unknowns: [],
    },
    ideas: {
      market: 'United States',
      ad_language: 'en-US',
      strategy_language: 'en-US',
      angles: ANGLES,
      combinations: COMBINATIONS,
      style_notes: '',
      competitor_snapshot_id: null,
    },
    latest_call: {
      id: 'pc_secret',
      operation_id: 'op_secret',
      kind: 'ideas',
      state: 'succeeded',
      execution_mode: 'byok',
      error_code: null,
      maximum_cost: '0.20',
    },
    ads: [
      ad('av_1', 'c1', { kept: true }),
      ad('av_2', 'c2'),
      ad('av_3', 'c3', { used_product_photo: false }),
      ad('av_4', 'c4'),
    ],
    pending: [
      { combination_id: 'c5', state: 'running' },
      { combination_id: 'c6', state: 'failed' },
    ],
    ...extra,
  };
}

export function product(
  ideas: WorkspaceProduct['ideas'] = {
    projectId: 'mp_secret',
    adCount: 4,
    keptCount: 1,
  }
): WorkspaceProduct {
  return {
    productId: 'prod_home',
    name: 'Acme Charge Home',
    ideas,
    signals: {
      needsAttention: false,
      reportMissing: false,
      conclusionUnseen: false,
      keptUntested: 0,
      draftsUnhandled: 0,
      hasIdeas: ideas !== null,
    },
    step: 'drafts_unhandled',
    stepCount: 0,
  };
}
