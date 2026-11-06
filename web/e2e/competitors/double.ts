import type { Page, Route } from '@playwright/test';
import type { components } from '@/lib/domain-client/generated/domain-v1';

/**
 * A stateful stand-in for the competitor and competitor-scan BFF routes
 * (C-13 #269, C-21 #270), shaped by `contracts/openapi/marketai-domain-v1.json`.
 *
 * The page shell still needs a signed-in Workspace (SSR); everything the
 * competitors area fetches below that boundary is answered here through
 * `page.route()`, including the one marketing project the test opens, so the
 * spec needs no Worker, no provider key and no AI planner. `credentials`
 * says which keys the double pretends the Workspace has: `missing` offers
 * Controlled Mock only, `ready` is the controlled double of a Workspace with
 * both keys (nothing real is called either way).
 *
 * Scans move one step per poll: queued -> fetch -> media -> the next
 * terminal state from `scanOutcomes` (then `completed` for every resume).
 *
 * `realCompetitors` lets the competitor routes through to the real C-13
 * backend (with a real `productId`) and only learns the competitors from its
 * answers, so the doubled scans can name their targets. `realScans` lets the
 * scan, board and product-settings routes through as well (C-21 to C-23),
 * so only the marketing project is doubled; `project` adds fields to its
 * projection (e.g. C-24's `competitor_snapshot_stale`). While the scans are
 * doubled, a competitor's `last_scanned_at` (#332) is projected from them,
 * real competitors included: the real backend cannot see doubled scans.
 */
type Schemas = components['schemas'];
type Competitor = Schemas['Competitor'];
type Resolution = Schemas['CompetitorResolution'];
type Scan = Schemas['CompetitorScan'];
type ScanQuote = Schemas['CompetitorScanQuote'];
type Credentials = 'missing' | 'ready';
type Outcome = 'completed' | 'partial' | 'uncertain';

export const PROJECT_ID = 'mp_c31_e2e';
export const PRODUCT_ID = 'prod_c31_e2e';
const CREDIT_USD = 0.00188;
const usd = (credits: number) => (credits * CREDIT_USD).toFixed(5);
const now = () => new Date().toISOString();

const PROJECT = {
  id: PROJECT_ID,
  revision: 1,
  state: 'understanding_draft',
  source_text: 'Acme Charge NACS adapter',
  product_id: PRODUCT_ID,
  language_support: 'v1',
  plan_review_support: 'v1',
  brand_style_support: 'v1',
  evidence_status: 'unvalidated',
  latest_call: null,
  generation_draft_ids: [],
  plan: null,
  understanding: {
    product_name: 'NACS to J1772 adapter',
    category_need: 'Charge a J1772 car at Halcyon Superchargers.',
    product_choice_reason: 'UL-listed, 48A.',
    audience: 'EV owners in North America',
    situations: ['Road trips'],
    objections: [],
    facts: [],
    inferences: [
      {
        id: 'i1',
        statement: 'Charging access matters on road trips.',
        rationale: 'Example.',
        fact_ids: [],
      },
    ],
    unknowns: [],
  },
};

/** What a name search returns: the true page ranks first after the probe. */
function candidatesFor(query: string, website: string | null) {
  const domain = website ?? 'acme-charge.example';
  return [
    {
      page_id: '100000000000010',
      page_name: query,
      page_alias: query.toLowerCase().replace(/\s+/g, '') + 'ev',
      category: 'Automotive Parts Store',
      likes: 3509,
      ig_username: 'acme_charge_ev',
      ig_followers: 1200,
      avatar: null,
      provider_rank: 2,
      rank: 0,
      name_match: 'exact',
      alias_match: false,
      category_class: 'commercial',
      probe: {
        status: 'succeeded',
        country: 'US',
        total_active_ads: 58,
        sampled_ads: 30,
        landing_domains: [domain],
      },
      domain_hit: website ? true : null,
    },
    {
      page_id: '200000000000001',
      page_name: `${query} Fuel Systems`,
      page_alias: 'fuelsystems',
      category: 'Motor Vehicle Company',
      likes: 270000,
      ig_username: null,
      avatar: null,
      provider_rank: 0,
      rank: 1,
      name_match: 'partial',
      alias_match: false,
      category_class: 'commercial',
      probe: {
        status: 'succeeded',
        country: 'US',
        total_active_ads: 4,
        sampled_ads: 4,
        landing_domains: ['fuelsystems.example'],
      },
      domain_hit: website ? false : null,
    },
    {
      page_id: '200000000000002',
      page_name: `${query} Fan Club`,
      page_alias: null,
      category: 'Fan Page',
      likes: 120,
      avatar: null,
      provider_rank: 1,
      rank: 2,
      name_match: 'partial',
      alias_match: false,
      category_class: 'excluded',
      excluded_reason: 'category',
      probe: null,
      domain_hit: null,
    },
  ];
}

export class CompetitorDouble {
  readonly competitors = new Map<string, Competitor>();
  readonly resolutions = new Map<string, Resolution & { polls: number }>();
  readonly scans = new Map<
    string,
    Scan & { polls: number; outcome: Outcome }
  >();
  /** Every mutating request the page sent, for assertions. */
  readonly commands: { operation: string; body: unknown; intent?: string }[] =
    [];
  private sequence = 0;
  private outcomes: Outcome[];

  constructor(
    readonly options: {
      slug: string;
      credentials: Credentials;
      scanOutcomes: Outcome[];
      productId?: string;
      realCompetitors?: boolean;
      realScans?: boolean;
    }
  ) {
    this.outcomes = [...options.scanOutcomes];
  }

  /** Extra fields of the doubled marketing project's projection. */
  project: Record<string, unknown> = {};

  /** `last_scanned_at` from the doubled scans: the newest finished one with it. */
  private projectScans(competitor: Competitor) {
    if (this.options.realScans) return competitor;
    for (const scan of this.scans.values()) {
      if (['queued', 'running'].includes(scan.status) || !scan.finished_at)
        continue;
      if (!scan.params.competitor_ids?.includes(competitor.competitor_id))
        continue;
      const known = competitor.last_scanned_at;
      if (!known || Date.parse(scan.finished_at) > Date.parse(known))
        competitor.last_scanned_at = scan.finished_at;
    }
    return competitor;
  }

  async install(page: Page) {
    await page.route(`**/api/domain/v1/w/${this.options.slug}/**`, (route) =>
      this.handle(route)
    );
  }

  private id(prefix: string) {
    this.sequence += 1;
    return `${prefix}_${this.sequence}`;
  }

  private mode(requested?: string | null): 'byok' | 'mock' {
    if (this.options.credentials === 'missing') return 'mock';
    return requested === 'mock' ? 'mock' : 'byok';
  }

  private async handle(route: Route) {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(
      `/api/domain/v1/w/${this.options.slug}`,
      ''
    );
    const method = request.method();
    const body = request.postData() ? request.postDataJSON() : undefined;
    const json = (status: number, data: unknown) =>
      route.fulfill({
        status,
        contentType: 'application/json',
        body: JSON.stringify(data),
      });
    const record = (operation: string) =>
      this.commands.push({
        operation,
        body,
        intent: request.headers()['x-intent-id'],
      });

    // The one marketing project the spec opens.
    if (method === 'GET' && path === `/marketing/projects/${PROJECT_ID}`)
      return json(200, {
        ...PROJECT,
        product_id: this.options.productId ?? PRODUCT_ID,
        ...this.project,
      });
    if (
      method === 'GET' &&
      path === `/marketing/projects/${PROJECT_ID}/workflow`
    )
      return json(200, {
        project_id: PROJECT_ID,
        rounds: [],
        current: null,
        next_action: 'freeze_round',
      });
    if (method === 'GET' && path === '/marketing/projects')
      return json(200, {
        items: [
          {
            id: PROJECT_ID,
            revision: 1,
            state: PROJECT.state,
            title: PROJECT.understanding.product_name,
            updated_at: now(),
          },
        ],
        next_cursor: null,
      });

    let m: RegExpMatchArray | null;
    if (
      this.options.realScans &&
      (path.startsWith('/competitor-scans') ||
        /^\/products\/[^/]+\/competitor-settings$/.test(path))
    )
      return route.fallback();
    if (this.options.realCompetitors && path.startsWith('/competitors')) {
      const response = await route.fetch();
      const data = await response.json();
      for (const item of data?.items ?? (data?.competitor_id ? [data] : [])) {
        this.projectScans(item);
        this.competitors.set(item.competitor_id, item);
      }
      return route.fulfill({ response, json: data });
    }
    if (path === '/competitors' && method === 'GET')
      return json(200, {
        items: [...this.competitors.values()]
          .filter((c) => c.status === 'active')
          .map((c) => this.projectScans(c))
          .reverse(),
      });
    if (path === '/competitors' && method === 'POST') {
      record('create_competitor');
      return json(201, this.create(body));
    }
    if ((m = path.match(/^\/competitors\/([^/]+)\/resolve-quote$/)))
      return json(
        200,
        this.resolveQuote(m[1], url.searchParams.get('execution_mode'))
      );
    if ((m = path.match(/^\/competitors\/([^/]+)\/resolve$/))) {
      record('resolve_competitor');
      return json(202, this.resolve(m[1], body));
    }
    if ((m = path.match(/^\/competitors\/([^/]+)\/resolutions\/([^/]+)$/)))
      return json(200, this.pollResolution(m[2]));
    if ((m = path.match(/^\/competitors\/([^/]+)\/confirm$/))) {
      record('confirm_competitor');
      return json(200, this.confirm(m[1], body));
    }
    if ((m = path.match(/^\/competitors\/([^/]+)\/archive$/))) {
      record('archive_competitor');
      const competitor = this.competitors.get(m[1])!;
      competitor.status = 'archived';
      return json(200, competitor);
    }

    if (path === '/competitor-scans/preview') {
      record('preview_competitor_scan');
      return json(200, this.scanQuote(body, 'scan'));
    }
    if (path === '/competitor-scans' && method === 'POST') {
      record('create_competitor_scan');
      return json(202, this.createScan(body));
    }
    if (path === '/competitor-scans' && method === 'GET')
      return json(200, {
        items: [...this.scans.values()].reverse().map((scan) => ({
          scan_id: scan.scan_id,
          product_id: scan.product_id,
          operation_id: scan.operation_id,
          job_id: scan.job_id,
          status: scan.status,
          stage: scan.stage,
          execution_mode: scan.execution_mode,
          country: scan.country,
          competitor_ids: scan.params.competitor_ids,
          keywords: scan.params.keywords ?? [],
          observation_count: scan.observation_count,
          created_at: scan.created_at,
          finished_at: scan.finished_at,
        })),
      });
    if ((m = path.match(/^\/competitor-scans\/([^/]+)\/resume-quote$/))) {
      const scan = this.scans.get(m[1])!;
      return json(200, this.scanQuote(scan.params, 'resume', scan));
    }
    if ((m = path.match(/^\/competitor-scans\/([^/]+)\/resume$/))) {
      record('resume_competitor_scan');
      return json(202, this.resume(m[1]));
    }
    if ((m = path.match(/^\/competitor-scans\/([^/]+)\/reconcile$/))) {
      record('reconcile_competitor_scan');
      return json(200, this.reconcile(m[1], body));
    }
    if ((m = path.match(/^\/competitor-scans\/([^/]+)$/)))
      return json(200, this.pollScan(m[1]));

    return route.fallback();
  }

  private create(body: any): Competitor {
    const input: string = body.input.trim();
    const pageId =
      input.match(/view_all_page_id=(\d+)/)?.[1] ??
      input.match(/profile\.php\?id=(\d+)/)?.[1];
    const alias = !pageId
      ? input.match(/facebook\.com\/([A-Za-z0-9.]+)\/?$/)?.[1]
      : undefined;
    const website = body.website
      ? body.website.replace(/^https?:\/\//, '').replace(/\/.*$/, '')
      : null;
    const competitor: Competitor = {
      competitor_id: this.id('cmp'),
      product_id: body.product_id,
      role: body.role ?? 'competitor',
      status: 'active',
      name: pageId || alias ? null : input,
      website_domain: website,
      input_kind: pageId
        ? input.includes('ads/library')
          ? 'ad_library'
          : 'facebook_page'
        : alias
          ? 'facebook_page'
          : 'name',
      input_page_alias: alias ?? null,
      confirmation_status: pageId ? 'confirmed' : 'unconfirmed',
      confirmation: pageId
        ? {
            revision: 1,
            platform: 'meta',
            page_id: pageId,
            page_alias: null,
            page_name: pageId === '100000000000016' ? 'Brightvolt' : null,
            source: 'link',
            link_kind: input.includes('ads/library')
              ? 'ad_library'
              : 'facebook_page',
            confirmed_at: now(),
          }
        : null,
      latest_resolution: null,
      can_resolve: !pageId,
      created_at: now(),
      replayed: false,
    };
    this.competitors.set(competitor.competitor_id, competitor);
    return competitor;
  }

  private resolveQuote(competitorId: string, requested: string | null) {
    const competitor = this.competitors.get(competitorId)!;
    const mode = this.mode(requested);
    return {
      competitor_id: competitorId,
      execution_mode: mode,
      available_modes:
        this.options.credentials === 'ready' ? ['byok', 'mock'] : ['mock'],
      byok_unavailable_code:
        this.options.credentials === 'ready'
          ? null
          : 'market_data_credential_missing',
      byok_unavailable_params: {},
      provider: 'scrapecreators',
      query: competitor.name ?? competitor.input_page_alias ?? '',
      website_domain: competitor.website_domain,
      country: 'US',
      max_credits: 4,
      max_usd: mode === 'mock' ? '0' : usd(4),
      simulated_usd: mode === 'mock' ? usd(4) : null,
      currency: 'USD',
      pricing_version: 'v1',
      lines: [
        { operation: 'page_search', calls: 1, credits: 1, usd: usd(1) },
        { operation: 'probe', calls: 3, credits: 3, usd: usd(3) },
      ],
    };
  }

  private resolve(competitorId: string, body: any): Resolution {
    const competitor = this.competitors.get(competitorId)!;
    const quote = this.resolveQuote(competitorId, body?.execution_mode);
    const resolution = {
      competitor_id: competitorId,
      resolve_job_id: this.id('job_resolve'),
      operation_id: this.id('op_resolve'),
      status: 'queued',
      execution_mode: quote.execution_mode,
      provider: 'scrapecreators',
      query: quote.query,
      website_domain: competitor.website_domain,
      country: body?.country ?? 'US',
      quote,
      cost: null,
      candidates: [],
      auto_confirmed: false,
      waits: 0,
      reusable: false,
      created_at: now(),
      replayed: false,
      polls: 0,
    } as Resolution & { polls: number };
    this.resolutions.set(resolution.resolve_job_id, resolution);
    competitor.latest_resolution = {
      resolve_job_id: resolution.resolve_job_id,
      operation_id: resolution.operation_id,
      status: 'queued',
      execution_mode: resolution.execution_mode,
      auto_confirmed: false,
      reusable: false,
    };
    return resolution;
  }

  private pollResolution(jobId: string): Resolution {
    const resolution = this.resolutions.get(jobId)!;
    resolution.polls += 1;
    if (resolution.status === 'queued') resolution.status = 'running';
    else if (resolution.status === 'running') {
      resolution.status = 'completed';
      resolution.candidates = candidatesFor(
        resolution.query,
        resolution.website_domain ?? null
      ) as Resolution['candidates'];
      resolution.cost = {
        calls: 3,
        credits_charged: 3,
        usd: resolution.execution_mode === 'mock' ? '0' : usd(3),
        simulated_usd: resolution.execution_mode === 'mock' ? usd(3) : null,
      };
      resolution.reusable = true;
      resolution.expires_at = new Date(Date.now() + 72 * 3600e3).toISOString();
      resolution.finished_at = now();
      const competitor = this.competitors.get(resolution.competitor_id)!;
      competitor.latest_resolution = {
        resolve_job_id: jobId,
        operation_id: resolution.operation_id,
        status: 'completed',
        execution_mode: resolution.execution_mode,
        auto_confirmed: false,
        reusable: true,
        expires_at: resolution.expires_at,
      };
    }
    return resolution;
  }

  private confirm(competitorId: string, body: any): Competitor {
    const competitor = this.competitors.get(competitorId)!;
    const pageId =
      body.page_id ?? body.page_url?.match(/(?:page_id|id)=(\d+)/)?.[1];
    const candidate = body.resolve_job_id
      ? this.resolutions
          .get(body.resolve_job_id)!
          .candidates!.find((c) => c.page_id === pageId)
      : null;
    competitor.confirmation_status = 'confirmed';
    competitor.confirmation = {
      revision: (competitor.confirmation?.revision ?? 0) + 1,
      platform: 'meta',
      page_id: pageId,
      page_alias: candidate?.page_alias ?? null,
      page_name: candidate?.page_name ?? null,
      source: body.resolve_job_id ? 'user' : 'link',
      resolve_job_id: body.resolve_job_id ?? null,
      domain_hit: candidate?.domain_hit ?? null,
      confirmed_at: now(),
    };
    return competitor;
  }

  private scanQuote(params: any, kind: 'scan' | 'resume', scan?: Scan) {
    const competitors = params.competitor_ids.length;
    const keywords = (params.keywords ?? []).length;
    const pages = params.max_pages_per_competitor ?? 3;
    const keywordPages = params.keyword_pages ?? 2;
    const refreshes = params.max_media_refreshes ?? 10;
    const remaining =
      kind === 'resume'
        ? (scan!.targets ?? []).reduce(
            (sum, t) => sum + (t.pages_planned - t.pages_fetched),
            0
          )
        : 0;
    const lines =
      kind === 'resume'
        ? [{ operation: 'page', calls: remaining, credits: remaining }]
        : [
            {
              operation: 'page',
              calls: pages * competitors,
              credits: pages * competitors,
            },
            ...(keywords
              ? [
                  {
                    operation: 'keyword_page',
                    calls: keywords * keywordPages,
                    credits: keywords * keywordPages,
                  },
                ]
              : []),
            { operation: 'ad_refresh', calls: refreshes, credits: refreshes },
          ];
    const credits = lines.reduce((sum, line) => sum + line.credits, 0);
    const mode =
      kind === 'resume'
        ? scan!.execution_mode
        : this.mode(params.execution_mode);
    const mock = mode === 'mock';
    const llmCalls =
      kind === 'resume' ? 1 : Math.ceil((pages * 30) / 20) + 61 + keywords;
    const llmUsd = (llmCalls * 0.0125).toFixed(4);
    const ready = this.options.credentials === 'ready';
    const quote: ScanQuote = {
      kind,
      scan_id: scan?.scan_id ?? null,
      product_id: params.product_id ?? PRODUCT_ID,
      execution_mode: mode,
      available_modes: ready ? ['byok', 'mock'] : ['mock'],
      byok_unavailable_code: ready ? null : 'market_data_credential_missing',
      byok_unavailable_params: {},
      credentials: [
        {
          binding_scope: 'market_data',
          provider: 'scrapecreators',
          ready,
          code: ready ? null : 'market_data_credential_missing',
        },
        { binding_scope: 'generation', provider: 'openai', ready: true },
      ],
      country: params.country ?? 'US',
      language: 'en',
      competitor_count: competitors,
      keyword_count: keywords,
      market_data: {
        provider: 'scrapecreators',
        max_credits: credits,
        max_usd: mock ? '0' : usd(credits),
        simulated_usd: mock ? usd(credits) : null,
        pricing_version: 'v1',
        lines: lines.map((line) => ({
          ...line,
          usd: usd(line.credits),
        })) as ScanQuote['market_data']['lines'],
      },
      llm: {
        provider: 'openai',
        model: 'gpt-4.1-mini',
        calls: llmCalls,
        max_usd: mock ? '0' : llmUsd,
        simulated_usd: mock ? llmUsd : null,
        price_rule_version: 'v1',
        lines: [],
      },
      max_usd: mock ? '0' : (Number(usd(credits)) + Number(llmUsd)).toFixed(5),
      simulated_usd: mock
        ? (Number(usd(credits)) + Number(llmUsd)).toFixed(5)
        : null,
      currency: 'USD',
    };
    return quote;
  }

  private createScan(body: any): Scan {
    const quote = this.scanQuote(body, 'scan');
    const targets = [
      ...body.competitor_ids.map((id: string) => {
        const competitor = this.competitors.get(id)!;
        return {
          origin: `page:${id}`,
          kind: 'page',
          competitor_id: id,
          role: competitor.role,
          page_id: competitor.confirmation!.page_id,
          page_name:
            competitor.confirmation!.page_name ??
            competitor.name ??
            competitor.confirmation!.page_id,
          confirmation_revision: competitor.confirmation!.revision,
          status: 'pending',
          pages_planned: body.max_pages_per_competitor ?? 3,
          pages_fetched: 0,
          ads_seen: 0,
        };
      }),
      ...(body.keywords ?? []).map((keyword: string) => ({
        origin: `keyword:${keyword}`,
        kind: 'keyword',
        keyword,
        status: 'pending',
        pages_planned: body.keyword_pages ?? 2,
        pages_fetched: 0,
        ads_seen: 0,
      })),
    ];
    const scan = {
      scan_id: this.id('scan'),
      product_id: body.product_id,
      operation_id: this.id('op_scan'),
      job_id: this.id('job_scan'),
      status: 'queued',
      stage: null,
      execution_mode: quote.execution_mode,
      provider: 'scrapecreators',
      platform: 'meta',
      country: body.country,
      params: {
        ...body,
        platform: 'meta',
        ad_status: 'active',
        language: 'en',
      },
      targets,
      steps: [],
      quote,
      cost: null,
      initial_credential_version_id:
        quote.execution_mode === 'byok' ? 'cv_market_data_1' : null,
      resumes: [],
      reconciliation: [],
      media: { planned: 0, available: 0, unavailable: 0, pending: 0 },
      observation_count: 0,
      waits: 0,
      can_resume: false,
      created_at: now(),
      polls: 0,
      outcome: this.outcomes.shift() ?? 'completed',
    } as unknown as Scan & { polls: number; outcome: Outcome };
    this.scans.set(scan.scan_id, scan);
    return scan;
  }

  private steps(
    fetch: string,
    media: string
  ): Schemas['CompetitorScanStepState'][] {
    return [
      { step: 'fetch', status: fetch as never, attempts: 1 },
      { step: 'media', status: media as never, attempts: 1 },
      ...(['filter', 'tag', 'cluster', 'freeze'] as const).map((step) => ({
        step,
        status: (media === 'completed' ? 'skipped' : 'pending') as never,
        reason: media === 'completed' ? 'not_implemented' : null,
        attempts: 0,
      })),
    ];
  }

  private pollScan(scanId: string): Scan {
    const scan = this.scans.get(scanId)!;
    scan.polls += 1;
    const mock = scan.execution_mode === 'mock';
    const settle = (credits: number) => {
      scan.cost = {
        market_data: {
          calls: credits,
          credits_charged: credits,
          usd: mock ? '0' : usd(credits),
          simulated_usd: mock ? usd(credits) : null,
        },
        llm: { calls: 0, usd: '0', simulated_usd: mock ? '0' : null },
        usd: mock ? '0' : usd(credits),
        simulated_usd: mock ? usd(credits) : null,
      };
    };
    if (scan.status === 'queued') {
      scan.status = 'running';
      scan.started_at = now();
      scan.steps = this.steps('running', 'pending');
      for (const target of scan.targets ?? [])
        if (target.status !== 'completed') target.status = 'fetching';
    } else if (scan.status === 'running' && scan.stage === null) {
      scan.stage = 'fetch';
      scan.steps = this.steps(
        scan.outcome === 'completed' ? 'completed' : 'partial',
        'running'
      );
      const targets = scan.targets ?? [];
      targets.forEach((target, index) => {
        const last = index === targets.length - 1;
        if (scan.outcome !== 'completed' && last) {
          target.pages_fetched = Math.max(target.pages_fetched, 1);
          target.status = scan.outcome === 'uncertain' ? 'uncertain' : 'failed';
          target.failure =
            scan.outcome === 'uncertain'
              ? { code: 'market_data_outcome_unknown', retryable: true }
              : { code: 'market_data_insufficient_credits', retryable: true };
        } else {
          target.pages_fetched = target.pages_planned;
          target.status = 'completed';
          target.failure = null;
        }
        target.ads_seen =
          target.pages_fetched === 0 ? 0 : 30 + (target.pages_fetched - 1) * 10;
      });
      scan.observation_count = targets.reduce((sum, t) => sum + t.ads_seen, 0);
    } else if (scan.status === 'running') {
      scan.stage = 'media';
      scan.steps = this.steps(
        scan.outcome === 'completed' ? 'completed' : 'partial',
        'completed'
      );
      scan.media = {
        planned: scan.observation_count,
        available: scan.observation_count - 2,
        unavailable: 2,
        pending: 0,
        refreshes_used: 1,
        refresh_quota: scan.params.max_media_refreshes,
      };
      const fetched = (scan.targets ?? []).reduce(
        (sum, t) => sum + t.pages_fetched,
        0
      );
      settle(fetched + 1);
      scan.finished_at = now();
      scan.status = scan.outcome;
      scan.can_resume = scan.outcome !== 'completed';
      if (scan.outcome === 'partial')
        scan.failure = {
          code: 'market_data_insufficient_credits',
          retryable: true,
        };
      if (scan.outcome === 'uncertain') {
        const target = (scan.targets ?? []).at(-1)!;
        scan.reconciliation = [
          {
            attempt_id: this.id('att'),
            call_name: `${target.origin}:2`,
            origin: target.origin,
            page: 2,
            credential_version_id: 'cv_market_data_1',
            status: 'pending',
            source: null,
            balance_check: {
              outcome: 'manual_required',
              reason: 'difference',
              difference: 2,
              baseline_credits_remaining: 412,
              known_charges: 0,
              balance: 410,
              checked_at: now(),
            },
          },
        ];
      }
    }
    return scan;
  }

  private resume(scanId: string): Scan {
    const scan = this.scans.get(scanId)!;
    const quote = this.scanQuote(scan.params, 'resume', scan);
    scan.resumes = [
      ...(scan.resumes ?? []),
      {
        operation_id: this.id('op_resume'),
        job_id: this.id('job_resume'),
        credential_version_id: scan.initial_credential_version_id,
        max_credits: quote.market_data.max_credits,
        max_usd: quote.max_usd,
        requested_at: now(),
      },
    ];
    scan.status = 'queued';
    scan.stage = null;
    scan.failure = null;
    scan.can_resume = false;
    scan.finished_at = null;
    scan.outcome = 'completed';
    for (const target of scan.targets ?? [])
      if (target.status !== 'completed') target.status = 'pending';
    return scan;
  }

  private reconcile(scanId: string, body: any): Scan {
    const scan = this.scans.get(scanId)!;
    scan.reconciliation = (scan.reconciliation ?? []).map((entry) =>
      entry.attempt_id === body.attempt_id
        ? {
            ...entry,
            status: body.outcome,
            source: 'owner',
            decided_by: 'owner',
            decided_at: now(),
          }
        : entry
    );
    return scan;
  }
}
