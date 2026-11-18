/**
 * @jest-environment jsdom
 */
import type { ReactNode } from 'react';
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { SWRConfig } from 'swr';
import { ProductProfile } from '@/components/ideas/ProductProfile';
import { getWorkspacePageProps } from '@/lib/workspace-bootstrap';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import { ad, ideasDetail, product } from './fixtures';

/**
 * The product profile (ADR 0018, design §5.6, #202): the facts the AI read,
 * each checked, changed or deleted by the user; the AI's guesses apart from
 * them; photos and brand. Only what the user checks becomes confirmed.
 */
type Detail = components['schemas']['IdeasProjectDetail'];
type Understanding = components['schemas']['Understanding'];

jest.mock('@/lib/workspace-bootstrap', () => ({
  getWorkspacePageProps: jest.fn(() => jest.fn()),
}));
jest.mock('@/components/layouts/WorkspaceLayout', () => ({
  __esModule: true,
  default: ({ children }: { children: unknown }) => children,
}));
jest.mock('next/router', () => ({
  useRouter: () => ({
    asPath: '/w/acme/p/prod_home/profile',
    query: { productId: 'prod_home' },
  }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    current: { slug: 'acme', workspaceId: 'ws_1', role: 'OWNER' },
  }),
}));
const mockGateway = {
  getMarketingIdeas: jest.fn(),
  editMarketingIdeasUnderstanding: jest.fn(),
  verifyMarketingIdeas: jest.fn(),
};
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));
/** What each read the photo and brand blocks make answers. */
const mockReads: Record<string, unknown> = {};
const mockRefreshRead = jest.fn().mockResolvedValue(undefined);
jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: (
    operation: string,
    _params: unknown,
    options?: { enabled?: boolean }
  ) => ({
    data: options?.enabled === false ? undefined : mockReads[operation],
    problem: undefined,
    isLoading: false,
    isStale: false,
    lastSuccessfulAt: null,
    refresh: mockRefreshRead,
  }),
}));
jest.mock('@/hooks/library/useReferenceImageUrl', () => ({
  useReferenceImageUrl: (id: string) => ({
    blobUrl: `blob:photo-${id}`,
    isLoading: false,
    problem: null,
  }),
}));
let mockFrameProduct = product();
jest.mock('@/hooks/products/useWorkspaceProducts', () => ({
  useWorkspaceProducts: () => ({
    find: () => mockFrameProduct,
    isLoading: false,
    problem: null,
    refresh: jest.fn(),
  }),
}));

// Loaded after the mocks: the page registers its server props once, before
// `beforeEach` clears the mocks.
import ProfilePage from '../../../pages/w/[slug]/p/[productId]/profile';
const registered = [...jest.mocked(getWorkspacePageProps).mock.calls];

function ok<T>(data: T) {
  return { kind: 'ok' as const, status: 200, data, meta: {} };
}

function refused(code: string, status = 409) {
  return {
    kind: 'problem' as const,
    status,
    problem: {
      type: 'about:blank',
      title: code,
      status,
      detail: code,
      instance: '',
      code,
      trace_id: 't',
      retryable: false,
      field_errors: [],
    },
    meta: {},
  };
}

/** Ideas whose ads stand on facts: two ads on f1, one on f2, none on f3. */
function detailWithUses(extra: Partial<Detail> = {}): Detail {
  const base = ideasDetail();
  return {
    ...base,
    understanding: {
      ...base.understanding!,
      unknowns: ['How long is the warranty?'],
    },
    ads: [
      ad('av_1', 'c1', { kept: true, cited_fact_ids: ['f1'] }),
      ad('av_2', 'c2', { cited_fact_ids: ['f1'] }),
      ad('av_3', 'c3', { cited_fact_ids: ['f2'] }),
    ],
    pending: [],
    ...extra,
  };
}

/** The project after a change: the server's answer with these facts. */
function answered(detail: Detail, understanding: Understanding): Detail {
  return { ...detail, revision: detail.revision + 1, understanding };
}

function Fresh({ children }: { children: ReactNode }) {
  return (
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      {children}
    </SWRConfig>
  );
}

async function renderProfile(detail: Detail = detailWithUses()) {
  mockGateway.getMarketingIdeas.mockResolvedValue(ok(detail));
  const view = render(
    <Fresh>
      <ProductProfile product={product()} />
    </Fresh>
  );
  await screen.findByRole('heading', { level: 2, name: /^Facts/ });
  return view;
}

const facts = () => screen.getByRole('region', { name: /^Facts/ });
const guesses = () => screen.getByRole('region', { name: "The AI's guesses" });
const factRow = (statement: string) =>
  within(facts())
    .getAllByTestId('profile-fact')
    .find((row) => within(row).queryByText(statement, { selector: 'p' }))!;

beforeEach(() => {
  jest.clearAllMocks();
  mockFrameProduct = product();
  for (const key of Object.keys(mockReads)) delete mockReads[key];
  mockReads.list_products = {
    items: [{ product_id: 'prod_home', entry_id: 'le_1', ready: false }],
  };
});

describe('ProductProfile (#202)', () => {
  it('shows the facts with their sources apart from the AI guesses, then photos and brand', async () => {
    const { container } = await renderProfile();

    expect(
      within(facts()).getByRole('heading', { name: 'Facts (3)' })
    ).toBeInTheDocument();
    const first = factRow('Schedule charging in the Acme Charge app');
    expect(within(first).getByText('Not checked yet')).toBeInTheDocument();
    expect(
      within(first).getByText(
        'Source: “Schedule charging from the Acme Charge app.”'
      )
    ).toBeInTheDocument();
    expect(within(first).getByText('Used in 2 ads')).toBeInTheDocument();
    expect(
      within(factRow('Load balancing with the home')).getByText('Checked')
    ).toBeInTheDocument();
    // A checked fact has nothing left to check.
    expect(
      within(factRow('Load balancing with the home')).queryByRole('button', {
        name: 'This is right',
      })
    ).toBeNull();

    // The guesses are their own block, never marked as checked facts.
    const block = guesses();
    expect(
      within(block).getByText(
        "These are guesses. Nobody has checked them, and they're never written into ads as facts."
      )
    ).toBeInTheDocument();
    expect(
      within(block).getByText('Electric car owners with a driveway.')
    ).toBeInTheDocument();
    expect(
      within(block).getByText('Owners want charging to need no attention.')
    ).toBeInTheDocument();
    // Only the audience can be checked (#224); no other guess carries a mark.
    const audience = within(block).getByTestId('profile-audience');
    expect(within(block).getAllByText(/^Checked$|^Not checked yet$/)).toEqual([
      within(audience).getByText('Not checked yet'),
    ]);
    expect(
      within(facts()).queryByText('Electric car owners with a driveway.')
    ).toBeNull();

    expect(
      screen.getByRole('region', { name: 'Product photos' })
    ).toHaveTextContent('This product has no product photos yet.');
    expect(screen.getByRole('region', { name: 'Brand' })).toBeInTheDocument();

    // No internal ids anywhere on the screen.
    expect(container.textContent).not.toMatch(
      /mp_secret|av_\d|prod_home|f\d\b/
    );
  });

  it('checks a fact: the check names only that fact and the revision seen', async () => {
    const detail = detailWithUses();
    await renderProfile(detail);
    const checkedUnderstanding = {
      ...detail.understanding!,
      facts: detail.understanding!.facts!.map((fact) =>
        fact.id === 'f1'
          ? { ...fact, confirmation: 'confirmed' as const }
          : fact
      ),
    };
    mockGateway.verifyMarketingIdeas.mockResolvedValue(
      ok(answered(detail, checkedUnderstanding))
    );

    fireEvent.click(
      within(factRow('Schedule charging in the Acme Charge app')).getByRole(
        'button',
        { name: 'This is right' }
      )
    );

    await waitFor(() =>
      expect(
        within(factRow('Schedule charging in the Acme Charge app')).getByText(
          'Checked'
        )
      ).toBeInTheDocument()
    );
    expect(mockGateway.verifyMarketingIdeas).toHaveBeenCalledTimes(1);
    expect(mockGateway.verifyMarketingIdeas).toHaveBeenCalledWith(
      expect.objectContaining({
        workspaceSlug: 'acme',
        projectId: 'mp_secret',
        expectedRevision: 3,
        body: {
          fact_ids: ['f1'],
          appearance_confirmed: [],
          accepted_findings: [],
        },
      })
    );
    expect(mockGateway.editMarketingIdeasUnderstanding).not.toHaveBeenCalled();
  });

  it('edits a fact ads use: says they need making again before anything is sent', async () => {
    const detail = detailWithUses();
    await renderProfile(detail);

    fireEvent.click(
      screen.getByRole('button', {
        name: 'Edit “Schedule charging in the Acme Charge app”',
      })
    );
    const row = factRow('Schedule charging in the Acme Charge app');
    expect(within(row).getByRole('note')).toHaveTextContent(
      "2 ads use this fact. Once you change it, they need to be made again, and can't be downloaded until then."
    );
    expect(mockGateway.editMarketingIdeasUnderstanding).not.toHaveBeenCalled();

    const changed = 'Schedule charging hours in the Acme Charge app';
    fireEvent.change(within(row).getByLabelText(/^Fact/), {
      target: { value: changed },
    });
    const sent = {
      ...detail.understanding!,
      facts: detail.understanding!.facts!.map((fact) =>
        fact.id === 'f1' ? { ...fact, statement: changed } : fact
      ),
    };
    mockGateway.editMarketingIdeasUnderstanding.mockResolvedValue(
      ok(
        answered(detail, {
          ...sent,
          facts: sent.facts.map((fact) =>
            fact.id === 'f1'
              ? { ...fact, confirmation: 'unconfirmed' as const }
              : fact
          ),
        })
      )
    );
    fireEvent.click(within(row).getByRole('button', { name: 'Save' }));

    await screen.findByText(changed);
    expect(mockGateway.editMarketingIdeasUnderstanding).toHaveBeenCalledWith(
      expect.objectContaining({
        projectId: 'mp_secret',
        expectedRevision: 3,
        body: { understanding: sent },
      })
    );
    // The other facts went back exactly as they were, checking included.
    const body = mockGateway.editMarketingIdeasUnderstanding.mock.calls[0][0]
      .body as { understanding: Understanding };
    expect(body.understanding.facts!.slice(1)).toEqual(
      detail.understanding!.facts!.slice(1)
    );
    expect(
      within(factRow(changed)).getByText('Not checked yet')
    ).toBeInTheDocument();
  });

  it('edits a fact no ad uses without a warning', async () => {
    await renderProfile();
    fireEvent.click(
      screen.getByRole('button', {
        name: 'Edit “Load balancing with the home”',
      })
    );
    expect(
      within(factRow('Load balancing with the home')).queryByRole('note')
    ).toBeNull();
  });

  it('deletes a fact: asks first, says its claims keep it, and the guesses stop naming it', async () => {
    const detail = detailWithUses();
    await renderProfile(detail);

    fireEvent.click(
      screen.getByRole('button', {
        name: 'Delete “Schedule charging in the Acme Charge app”',
      })
    );
    const question = within(
      factRow('Schedule charging in the Acme Charge app')
    ).getByRole('group', { name: 'Delete this fact?' });
    expect(question).toHaveTextContent(
      "2 ads use this fact. Once it's deleted, they need to be made again"
    );
    // Every claim's ways of telling cite this fact in the fixture: the honest
    // answer is that making them again is not enough, and where to change them.
    expect(question).toHaveTextContent(
      "3 claims are built on this fact directly. Once it's deleted, their ads stay “needs redoing” until you change or delete those claims on the ideas page (under “More” on each claim)."
    );
    expect(mockGateway.editMarketingIdeasUnderstanding).not.toHaveBeenCalled();

    const sent: Understanding = {
      ...detail.understanding!,
      facts: detail.understanding!.facts!.filter((fact) => fact.id !== 'f1'),
      inferences: [{ ...detail.understanding!.inferences[0], fact_ids: [] }],
    };
    mockGateway.editMarketingIdeasUnderstanding.mockResolvedValue(
      ok(answered(detail, sent))
    );
    fireEvent.click(within(question).getByRole('button', { name: 'Delete' }));

    await waitFor(() =>
      expect(
        screen.queryByText('Schedule charging in the Acme Charge app')
      ).toBeNull()
    );
    expect(mockGateway.editMarketingIdeasUnderstanding).toHaveBeenCalledWith(
      expect.objectContaining({
        expectedRevision: 3,
        body: { understanding: sent },
      })
    );
    expect(
      within(facts()).getByRole('heading', { name: 'Facts (2)' })
    ).toBeInTheDocument();
  });

  it('a fact only its own ad uses: deleting it says the ad needs making again, nothing about claims', async () => {
    const detail = detailWithUses();
    detail.ideas = {
      ...detail.ideas!,
      angles: detail.ideas!.angles.map((angle) => ({
        ...angle,
        fact_ids: [],
        expressions: angle.expressions.map((expression) => ({
          ...expression,
          evidence_basis: { fact_ids: [], gap: 'Not needed.' },
        })),
      })),
    };
    await renderProfile(detail);
    fireEvent.click(
      screen.getByRole('button', { name: 'Delete “IP55-rated enclosure”' })
    );
    const question = screen.getByRole('group', { name: 'Delete this fact?' });
    expect(question).toHaveTextContent(
      "1 ad uses this fact. Once it's deleted, that ad needs to be made again"
    );
    expect(question).not.toHaveTextContent(/claim/);
    fireEvent.click(within(question).getByRole('button', { name: 'Cancel' }));
    expect(
      screen.queryByRole('group', { name: 'Delete this fact?' })
    ).toBeNull();
    expect(mockGateway.editMarketingIdeasUnderstanding).not.toHaveBeenCalled();
  });

  it('a source for what the AI did not know turns it into an unchecked fact', async () => {
    const detail = detailWithUses();
    await renderProfile(detail);
    const block = guesses();
    expect(
      within(block).getByText('How long is the warranty?')
    ).toBeInTheDocument();

    fireEvent.click(
      within(block).getByRole('button', {
        name: 'Add a source for “How long is the warranty?”',
      })
    );
    expect(
      within(block).getByText('Answers: How long is the warranty?')
    ).toBeInTheDocument();
    fireEvent.change(within(block).getByLabelText(/^Fact/), {
      target: { value: '3-year warranty' },
    });
    fireEvent.change(within(block).getByLabelText(/^Source words/), {
      target: { value: 'Covered by a 3-year warranty.' },
    });
    fireEvent.change(within(block).getByLabelText(/^Add material/), {
      target: { value: 'Warranty card: Covered by a 3-year warranty.' },
    });
    mockGateway.editMarketingIdeasUnderstanding.mockImplementation(
      async ({ body }: { body: { understanding: Understanding } }) =>
        ok(answered(detail, body.understanding))
    );
    fireEvent.click(within(block).getByRole('button', { name: 'Save' }));

    const added = await within(facts()).findByText('3-year warranty');
    const call = mockGateway.editMarketingIdeasUnderstanding.mock.calls[0][0];
    expect(call.body.source_addition).toBe(
      'Warranty card: Covered by a 3-year warranty.'
    );
    expect(call.body.understanding.unknowns).toEqual([]);
    expect(call.body.understanding.facts).toEqual([
      ...detail.understanding!.facts!,
      {
        id: expect.stringMatching(/^fact-[a-zA-Z0-9]+$/),
        statement: '3-year warranty',
        source_quote: 'Covered by a 3-year warranty.',
        confirmation: 'unconfirmed',
      },
    ]);
    expect(
      within(factRow('3-year warranty')).getByText('Not checked yet')
    ).toBeInTheDocument();
    expect(added).toBeInTheDocument();
    expect(
      within(guesses()).queryByText('How long is the warranty?')
    ).toBeNull();
  });

  it('adds a fact the user writes, unchecked, with the material for its source', async () => {
    const detail = detailWithUses();
    await renderProfile(detail);
    fireEvent.click(screen.getByRole('button', { name: 'Add a fact' }));
    fireEvent.change(within(facts()).getByLabelText(/^Fact/), {
      target: { value: 'Works with a 32 A circuit' },
    });
    fireEvent.change(within(facts()).getByLabelText(/^Source words/), {
      target: { value: 'Needs a 32 A circuit.' },
    });
    mockGateway.editMarketingIdeasUnderstanding.mockImplementation(
      async ({ body }: { body: { understanding: Understanding } }) =>
        ok(answered(detail, body.understanding))
    );
    fireEvent.click(within(facts()).getByRole('button', { name: 'Save' }));

    await within(facts()).findByText('Works with a 32 A circuit');
    const call = mockGateway.editMarketingIdeasUnderstanding.mock.calls[0][0];
    expect(call.body).not.toHaveProperty('source_addition');
    expect(call.body.understanding.facts.at(-1)).toMatchObject({
      statement: 'Works with a 32 A circuit',
      source_quote: 'Needs a 32 A circuit.',
      confirmation: 'unconfirmed',
    });
  });

  it('someone changed it first: says so and refreshes before trying again', async () => {
    await renderProfile();
    mockGateway.editMarketingIdeasUnderstanding.mockResolvedValue(
      refused('stale_marketing_revision')
    );
    fireEvent.click(
      screen.getByRole('button', {
        name: 'Edit “Load balancing with the home”',
      })
    );
    const row = factRow('Load balancing with the home');
    fireEvent.change(within(row).getByLabelText(/^Fact/), {
      target: { value: 'Load balancing' },
    });
    fireEvent.click(within(row).getByRole('button', { name: 'Save' }));

    const alert = await within(row).findByRole('alert');
    expect(alert).toHaveTextContent(
      'Someone else just changed this product profile. Refresh to see the latest, then try again.'
    );
    // The form keeps what the user typed.
    expect(within(row).getByLabelText(/^Fact/)).toHaveValue('Load balancing');
    expect(mockGateway.getMarketingIdeas).toHaveBeenCalledTimes(1);
    fireEvent.click(within(alert).getByRole('button', { name: 'Refresh' }));
    await waitFor(() =>
      expect(mockGateway.getMarketingIdeas).toHaveBeenCalledTimes(2)
    );
  });

  it('a source the material does not hold: says how to fix it', async () => {
    await renderProfile();
    mockGateway.editMarketingIdeasUnderstanding.mockResolvedValue(
      refused('fact_source_missing', 422)
    );
    fireEvent.click(screen.getByRole('button', { name: 'Add a fact' }));
    fireEvent.change(within(facts()).getByLabelText(/^Fact/), {
      target: { value: 'Charges twice as fast' },
    });
    fireEvent.change(within(facts()).getByLabelText(/^Source words/), {
      target: { value: 'Twice as fast as a wall plug.' },
    });
    fireEvent.click(within(facts()).getByRole('button', { name: 'Save' }));
    expect(await within(facts()).findByRole('alert')).toHaveTextContent(
      "Those source words aren't in the product material. Check they match word for word, or paste the text that has them under “Add material”."
    );
  });

  it('lists the published photos with their checking, read only', async () => {
    mockReads.list_products = {
      items: [{ product_id: 'prod_home', entry_id: 'le_1', ready: true }],
    };
    mockReads.get_product_references = {
      product_id: 'prod_home',
      entry_id: 'le_1',
      version_id: 'lv_1',
      revision: 2,
      model_variants: [],
      has_unpublished_changes: false,
      items: [
        {
          item_id: 'pri_1',
          reference_object_id: 'ro_1',
          reference_version_id: 'rv_1',
          model_variant: '',
          role: 'primary',
          identity_status: 'verified_physical',
          note: '',
          added_at: '2026-11-17T08:00:00+00:00',
          available: true,
        },
      ],
    };
    await renderProfile();
    const photos = screen.getByRole('region', { name: 'Product photos' });
    expect(
      within(photos).getByRole('img', { name: 'Main photo' })
    ).toHaveAttribute('src', 'blob:photo-rv_1');
    expect(within(photos).getByText('Checked')).toBeInTheDocument();
    // Nothing here changes a photo yet (#199).
    expect(within(photos).queryByRole('button')).toBeNull();
    expect(within(photos).queryByRole('combobox')).toBeNull();
  });

  it('a product from before the new flow keeps its product card', () => {
    render(
      <Fresh>
        <ProductProfile product={product(null)} />
      </Fresh>
    );
    expect(
      screen.getByRole('link', { name: 'Open the product card' })
    ).toHaveAttribute('href', '/w/acme/products');
    expect(mockGateway.getMarketingIdeas).not.toHaveBeenCalled();
  });
});

describe('ProductProfile: audience and claims without a source (#224)', () => {
  const unsourced = [
    { id: 'u1', text: 'Charges 3x faster than a wall socket' },
    { id: 'u2', text: 'The quietest home charger on the market' },
  ];
  const audienceBlock = () => screen.getByTestId('profile-audience');
  const unsourcedBlock = () =>
    screen.getByRole('region', { name: /^Claims without a source/ });

  it('checks the audience as worded now, and says what the published details name', async () => {
    const detail = detailWithUses({ audience_confirmed: false });
    await renderProfile(detail);
    expect(
      within(audienceBlock()).getByText(
        "Until you check it, the product's published details name the market you chose as the audience."
      )
    ).toBeInTheDocument();
    mockGateway.verifyMarketingIdeas.mockResolvedValue(
      ok({ ...detail, revision: 4, audience_confirmed: true })
    );
    fireEvent.click(
      within(audienceBlock()).getByRole('button', { name: 'This is right' })
    );
    await waitFor(() =>
      expect(within(audienceBlock()).getByText('Checked')).toBeInTheDocument()
    );
    expect(mockGateway.verifyMarketingIdeas).toHaveBeenCalledWith(
      expect.objectContaining({
        expectedRevision: 3,
        body: { audience_confirmed: true },
      })
    );
    expect(
      within(audienceBlock()).queryByRole('button', { name: 'This is right' })
    ).toBeNull();
    expect(
      within(audienceBlock()).getByText(
        "A checked audience goes into the product's published details. Change the words and it needs checking again."
      )
    ).toBeInTheDocument();
  });

  it('edits the audience in the user words: it needs checking again', async () => {
    const detail = detailWithUses({ audience_confirmed: true });
    await renderProfile(detail);
    fireEvent.click(
      within(audienceBlock()).getByRole('button', {
        name: 'Edit who might buy it',
      })
    );
    fireEvent.change(within(audienceBlock()).getByRole('textbox'), {
      target: { value: '  Renters who park on the street  ' },
    });
    expect(
      within(audienceBlock()).getByText('Once saved, it needs checking again.')
    ).toBeInTheDocument();
    mockGateway.editMarketingIdeasUnderstanding.mockImplementation(
      async ({ body }: { body: { understanding: Understanding } }) =>
        ok({
          ...answered(detail, body.understanding),
          audience_confirmed: false,
        })
    );
    fireEvent.click(
      within(audienceBlock()).getByRole('button', { name: 'Save' })
    );
    await within(audienceBlock()).findByText('Renters who park on the street');
    const call = mockGateway.editMarketingIdeasUnderstanding.mock.calls[0][0];
    expect(call.body.understanding).toEqual({
      ...detail.understanding,
      audience: 'Renters who park on the street',
    });
    expect(
      within(audienceBlock()).getByText('Not checked yet')
    ).toBeInTheDocument();
  });

  it('lists what the AI read without a source, apart from the facts', async () => {
    const base = detailWithUses();
    await renderProfile({
      ...base,
      understanding: { ...base.understanding!, unsupported_claims: unsourced },
    });
    const block = unsourcedBlock();
    expect(
      within(block).getByRole('heading', {
        name: 'Claims without a source (2)',
      })
    ).toBeInTheDocument();
    expect(
      within(block).getByText('Charges 3x faster than a wall socket')
    ).toBeInTheDocument();
    expect(
      within(facts()).queryByText('Charges 3x faster than a wall socket')
    ).toBeNull();
    expect(
      within(block).getByText(/so no claim or ad uses them/)
    ).toBeInTheDocument();
  });

  it('nothing to list: no empty section', async () => {
    await renderProfile();
    expect(
      screen.queryByRole('region', { name: /^Claims without a source/ })
    ).toBeNull();
  });

  it('adding a source turns one into an unchecked fact and takes it off the list', async () => {
    const base = detailWithUses();
    const detail = {
      ...base,
      understanding: { ...base.understanding!, unsupported_claims: unsourced },
    };
    await renderProfile(detail);
    fireEvent.click(
      within(unsourcedBlock()).getByRole('button', {
        name: 'Add a source for “Charges 3x faster than a wall socket”',
      })
    );
    const block = unsourcedBlock();
    expect(
      within(block).getByText(
        'Without a source: Charges 3x faster than a wall socket'
      )
    ).toBeInTheDocument();
    // The claim's words start the fact; the user adds where it is written.
    expect(within(block).getByLabelText(/^Fact/)).toHaveValue(
      'Charges 3x faster than a wall socket'
    );
    fireEvent.change(within(block).getByLabelText(/^Source words/), {
      target: { value: 'In our lab test it charged 3x faster.' },
    });
    fireEvent.change(within(block).getByLabelText(/^Add material/), {
      target: { value: 'Lab report: In our lab test it charged 3x faster.' },
    });
    mockGateway.editMarketingIdeasUnderstanding.mockImplementation(
      async ({ body }: { body: { understanding: Understanding } }) =>
        ok(answered(detail, body.understanding))
    );
    fireEvent.click(within(block).getByRole('button', { name: 'Save' }));
    await within(facts()).findByText('Charges 3x faster than a wall socket');
    const call = mockGateway.editMarketingIdeasUnderstanding.mock.calls[0][0];
    expect(call.body.source_addition).toBe(
      'Lab report: In our lab test it charged 3x faster.'
    );
    expect(call.body.understanding.unsupported_claims).toEqual([unsourced[1]]);
    expect(call.body.understanding.facts.at(-1)).toEqual({
      id: expect.stringMatching(/^fact-[a-zA-Z0-9]+$/),
      statement: 'Charges 3x faster than a wall socket',
      source_quote: 'In our lab test it charged 3x faster.',
      confirmation: 'unconfirmed',
    });
    expect(
      within(unsourcedBlock()).queryByText(
        'Charges 3x faster than a wall socket'
      )
    ).toBeNull();
  });

  it("don't need it takes one off the list and adds nothing", async () => {
    const base = detailWithUses();
    const detail = {
      ...base,
      understanding: { ...base.understanding!, unsupported_claims: unsourced },
    };
    await renderProfile(detail);
    mockGateway.editMarketingIdeasUnderstanding.mockImplementation(
      async ({ body }: { body: { understanding: Understanding } }) =>
        ok(answered(detail, body.understanding))
    );
    fireEvent.click(
      within(unsourcedBlock()).getByRole('button', {
        name: "Don't need “The quietest home charger on the market”",
      })
    );
    await waitFor(() =>
      expect(
        within(unsourcedBlock()).queryByText(
          'The quietest home charger on the market'
        )
      ).toBeNull()
    );
    const call = mockGateway.editMarketingIdeasUnderstanding.mock.calls[0][0];
    expect(call.body.understanding).toEqual({
      ...detail.understanding,
      unsupported_claims: [unsourced[0]],
    });
    expect(call.body.source_addition).toBeUndefined();
  });
});

describe('/w/{slug}/p/{productId}/profile (#202)', () => {
  it('exists only while the ads-first switch is on', () => {
    // `adsFirstOnly` answers 404 while the switch is off
    // (workspace-bootstrap.spec.ts).
    expect(registered).toEqual([
      [
        {
          namespaces: ['common', 'ideas', 'products', 'library'],
          adsFirstOnly: true,
        },
      ],
    ]);
  });

  it('the Product profile view shows the profile', async () => {
    mockGateway.getMarketingIdeas.mockResolvedValue(ok(detailWithUses()));
    render(
      <Fresh>
        <ProfilePage
          bootstrap={{
            workspaceId: 'ws_1',
            slug: 'acme',
            displayName: 'Acme Charge',
            membershipId: 'mem_1',
            role: 'OWNER',
          }}
        />
      </Fresh>
    );
    expect(
      screen.getByRole('link', { name: 'Product profile' })
    ).toHaveAttribute('aria-current', 'page');
    expect(
      await screen.findByRole('heading', { level: 2, name: 'Facts (3)' })
    ).toBeInTheDocument();
  });
});
