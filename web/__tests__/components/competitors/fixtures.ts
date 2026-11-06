import type {
  Competitor,
  CompetitorAd,
  CompetitorCandidate,
  CompetitorResolution,
  CompetitorResolveQuote,
  CompetitorScan,
  CompetitorScanQuote,
  CompetitorSnapshotSummary,
} from '@/components/competitors/shared';

export const PRODUCT_ID = 'prod_1';

/**
 * The generated response types require every field the contract gives a
 * default; fixtures spell out only what a test reads.
 */
type Loose<T> = { [K in keyof T]?: unknown };

export function competitor(overrides: Loose<Competitor> = {}): Competitor {
  return {
    competitor_id: 'cmp_acme',
    product_id: PRODUCT_ID,
    role: 'competitor',
    status: 'active',
    name: 'Acme Charge',
    website_domain: 'acme-charge.example',
    input_kind: 'name',
    input_page_alias: null,
    confirmation_status: 'unconfirmed',
    confirmation: null,
    latest_resolution: null,
    can_resolve: true,
    created_at: '2026-10-30T08:00:00Z',
    ...overrides,
  } as unknown as Competitor;
}

export function confirmed(overrides: Loose<Competitor> = {}): Competitor {
  return competitor({
    confirmation_status: 'confirmed',
    confirmation: {
      revision: 1,
      platform: 'meta',
      page_id: '100000000000010',
      page_alias: 'acmecharge',
      page_name: 'Acme Charge',
      source: 'user',
    },
    ...overrides,
  });
}

export function resolveQuote(
  overrides: Loose<CompetitorResolveQuote> = {}
): CompetitorResolveQuote {
  return {
    competitor_id: 'cmp_acme',
    execution_mode: 'byok',
    available_modes: ['byok', 'mock'],
    provider: 'scrapecreators',
    query: 'Acme Charge',
    website_domain: 'acme-charge.example',
    country: 'US',
    max_credits: 4,
    max_usd: '0.00752',
    currency: 'USD',
    lines: [
      { operation: 'page_search', calls: 1, credits: 1, usd: '0.00188' },
      { operation: 'probe', calls: 3, credits: 3, usd: '0.00564' },
    ],
    ...overrides,
  } as unknown as CompetitorResolveQuote;
}

export function candidate(
  overrides: Loose<CompetitorCandidate> = {}
): CompetitorCandidate {
  return {
    page_id: '100000000000010',
    page_name: 'Acme Charge',
    page_alias: 'acmecharge',
    category: 'Automotive Parts Store',
    likes: 3509,
    ig_username: 'acme_charge_ev',
    ig_followers: 1200,
    avatar: null,
    provider_rank: 0,
    rank: 0,
    name_match: 'exact',
    category_class: 'commercial',
    probe: {
      status: 'succeeded',
      country: 'US',
      total_active_ads: 58,
      sampled_ads: 30,
      landing_domains: ['acme-charge.example'],
    },
    domain_hit: true,
    ...overrides,
  } as unknown as CompetitorCandidate;
}

export function resolution(
  overrides: Loose<CompetitorResolution> = {}
): CompetitorResolution {
  return {
    competitor_id: 'cmp_acme',
    resolve_job_id: 'job_resolve_1',
    operation_id: 'op_resolve_1',
    status: 'completed',
    execution_mode: 'byok',
    provider: 'scrapecreators',
    query: 'Acme Charge',
    website_domain: 'acme-charge.example',
    country: 'US',
    quote: resolveQuote(),
    cost: { calls: 4, credits_charged: 4, usd: '0.00752' },
    candidates: [
      candidate(),
      candidate({
        page_id: '200',
        page_name: 'Acme Charge Fuel Systems',
        page_alias: 'acmechargefuel',
        likes: 270000,
        rank: 1,
        provider_rank: 1,
        domain_hit: false,
        avatar: {
          url: 'https://storage.example/avatar-200.jpg?sig=1',
          expires_at: '2026-10-30T09:00:00Z',
          mime_type: 'image/jpeg',
        },
        probe: {
          status: 'succeeded',
          country: 'US',
          total_active_ads: 4,
          sampled_ads: 4,
          landing_domains: ['acmechargefuelsystems.example'],
        },
      }),
    ],
    auto_confirmed: false,
    waits: 0,
    reusable: true,
    expires_at: '2026-11-02T08:00:00Z',
    ...overrides,
  } as unknown as CompetitorResolution;
}

export function scanQuote(
  overrides: Loose<CompetitorScanQuote> = {}
): CompetitorScanQuote {
  return {
    kind: 'scan',
    product_id: PRODUCT_ID,
    execution_mode: 'byok',
    available_modes: ['byok', 'mock'],
    credentials: [
      { binding_scope: 'market_data', provider: 'scrapecreators', ready: true },
      { binding_scope: 'generation', provider: 'openai', ready: true },
    ],
    country: 'US',
    language: 'en',
    competitor_count: 1,
    keyword_count: 0,
    market_data: {
      provider: 'scrapecreators',
      max_credits: 13,
      max_usd: '0.02444',
      lines: [
        { operation: 'page', calls: 3, credits: 3, usd: '0.00564' },
        { operation: 'ad_refresh', calls: 10, credits: 10, usd: '0.0188' },
      ],
    },
    llm: {
      provider: 'openai',
      model: 'gpt-4.1-mini',
      calls: 66,
      max_usd: '0.84',
      price_rule_version: 'v1',
      lines: [],
    },
    max_usd: '0.86444',
    currency: 'USD',
    ...overrides,
  } as unknown as CompetitorScanQuote;
}

export function scan(overrides: Loose<CompetitorScan> = {}): CompetitorScan {
  return {
    scan_id: 'scan_1',
    product_id: PRODUCT_ID,
    operation_id: 'op_scan_1',
    job_id: 'job_scan_1',
    status: 'completed',
    stage: 'freeze',
    execution_mode: 'byok',
    provider: 'scrapecreators',
    platform: 'meta',
    country: 'US',
    params: {
      competitor_ids: ['cmp_acme'],
      keywords: [],
      country: 'US',
      platform: 'meta',
      ad_status: 'active',
      max_pages_per_competitor: 3,
      keyword_pages: 2,
      max_media_refreshes: 10,
      max_tagged_ads: 60,
      max_discover_advertisers: 30,
      language: 'en',
    },
    targets: [
      {
        origin: 'page:cmp_acme',
        kind: 'page',
        competitor_id: 'cmp_acme',
        role: 'competitor',
        page_id: '100000000000010',
        page_name: 'Acme Charge',
        status: 'completed',
        pages_planned: 3,
        pages_fetched: 3,
        ads_seen: 50,
      },
    ],
    steps: [
      { step: 'fetch', status: 'completed' },
      { step: 'media', status: 'completed' },
      { step: 'filter', status: 'skipped', reason: 'not_implemented' },
      { step: 'tag', status: 'skipped', reason: 'not_implemented' },
      { step: 'cluster', status: 'skipped', reason: 'not_implemented' },
      { step: 'freeze', status: 'skipped', reason: 'not_implemented' },
    ],
    quote: scanQuote(),
    cost: {
      market_data: { calls: 3, credits_charged: 3, usd: '0.00564' },
      llm: { calls: 0, usd: '0' },
      usd: '0.00564',
    },
    resumes: [],
    reconciliation: [],
    media: {
      planned: 50,
      available: 48,
      unavailable: 2,
      pending: 0,
      refreshes_used: 1,
      refresh_quota: 10,
    },
    observation_count: 50,
    waits: 0,
    can_resume: false,
    created_at: '2026-10-30T09:00:00Z',
    finished_at: '2026-10-30T09:05:00Z',
    ...overrides,
  } as unknown as CompetitorScan;
}

export const ok = <T>(data: T) => ({ kind: 'ok', status: 200, data });
export const created = <T>(data: T) => ({ kind: 'created', status: 201, data });
export const accepted = <T>(data: T) => ({
  kind: 'accepted',
  status: 202,
  data,
});
export const problem = (
  status: number,
  code: string,
  params?: Record<string, unknown>
) => ({ kind: 'problem', status, problem: { code, params } });

export function ad(overrides: Loose<CompetitorAd> = {}): CompetitorAd {
  return {
    ad_id: 'ad_1',
    scan_id: 'scan_1',
    provider: 'scrapecreators',
    ad_archive_id: '1203',
    advertiser_page_id: '100000000000010',
    advertiser_name: 'Acme Charge',
    page_categories: [],
    days_running: 64,
    display_format: 'IMAGE',
    publisher_platforms: ['facebook'],
    cards: [
      {
        index: 0,
        title: 'One adapter for every charger',
        body: 'Charge at Superchargers.',
        cta_text: 'Shop now',
        image: {
          kind: 'image',
          card_index: 0,
          source_field: 'snapshot.images[0]',
          status: 'available',
        },
      },
    ],
    landing_domain: 'acme-charge.example',
    library_url: 'https://www.facebook.com/ads/library/?id=1203',
    observed_at: '2026-10-30T09:01:00Z',
    origins: [{ origin: 'page:cmp_acme', kind: 'page' }],
    competitor_ids: ['cmp_acme'],
    role: 'competitor',
    media_status: 'available',
    thumbnail: {
      url: 'https://storage.example/thumb/ad_1.jpg?sig=1',
      expires_at: '2026-10-30T09:16:00Z',
      mime_type: 'image/jpeg',
    },
    field_sources: {},
    relevance: {
      relevance: 'relevant',
      source: 'ai',
      revision: 1,
      confidence: 0.9,
      reason: 'Sells the same adapter.',
    },
    tags: null,
    ...overrides,
  } as unknown as CompetitorAd;
}

export function snapshotSummary(
  overrides: Loose<CompetitorSnapshotSummary> = {}
): CompetitorSnapshotSummary {
  return {
    snapshot_id: 'snap_1',
    scan_id: 'scan_1',
    product_id: PRODUCT_ID,
    country: 'US',
    execution_mode: 'mock',
    hash: 'sha256:1',
    cluster_revision: 1,
    current: true,
    counts: {
      observations: 3,
      relevant: 1,
      adjacent: 1,
      unrelated: 1,
      unassessed: 0,
      self: 0,
    },
    created_at: '2026-10-30T09:06:00Z',
    mock: true,
    language: 'en',
    pending_changes: false,
    can_recluster: true,
    relevance_sources: { ai: 1, rule: 0, user: 0, ai_inferred: 1, non_ai: 0 },
    clusters: [
      {
        cluster_id: 'c1',
        claim: 'One adapter covers every charger',
        ad_ids: ['ad_1'],
        observation_count: 1,
        advertisers: [
          {
            advertiser_page_id: '100000000000010',
            advertiser_name: 'Acme Charge',
            role: 'competitor',
          },
        ],
        advertiser_count: 1,
        self_included: false,
        max_days_running: 64,
        library_urls: ['https://www.facebook.com/ads/library/?id=1203'],
        ai_inference: true,
        ads: [],
      },
    ],
    unclustered: [],
    adjacent: [],
    long_running: [],
    self_observations: [],
    long_running_days: 60,
    dimension_counts: [
      {
        factor_id: 'angle',
        label: 'Angle',
        values: [
          {
            value_id: 'v1',
            label: 'Charging access',
            confirmed: 0,
            suggested: 1,
          },
          { value_id: 'v2', label: 'Safety', confirmed: 0, suggested: 0 },
        ],
        none: { confirmed: 0, suggested: 0, missing: 0 },
      },
    ],
    conclusion: {
      text: 'Most competitors promise one adapter for every charger.',
      ai_inference: true,
      cluster_revision: 1,
    },
    ...overrides,
  } as unknown as CompetitorSnapshotSummary;
}
