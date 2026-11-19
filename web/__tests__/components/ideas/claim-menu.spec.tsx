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
import { IdeasPage } from '@/components/ideas/IdeasPage';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import {
  ad,
  ANGLES,
  COMBINATIONS,
  generationCapabilities,
  ideasDetail,
  plannerCapabilities,
  product,
} from './fixtures';

/**
 * A claim's "More" on the ideas page (#224, design §5.7): change the claim in
 * the user's own words, or delete it. Both are free; what happens to the
 * images already made is said before anything is sent.
 */
type Detail = components['schemas']['IdeasProjectDetail'];

jest.mock('next/router', () => ({
  useRouter: () => ({ asPath: '/w/acme/p/prod_home/ideas', query: {} }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme', workspaceId: 'ws_1' } }),
}));
const mockGateway = {
  getMarketingIdeas: jest.fn(),
  setMarketingAdKept: jest.fn(),
  getMarketingCapabilities: jest.fn(),
  getGenerationCapabilities: jest.fn(),
  getMarketingIdeasChecksQuote: jest.fn(),
  editMarketingIdeasClaim: jest.fn(),
  deleteMarketingIdeasClaim: jest.fn(),
};
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));
jest.mock('@/components/workspace/useMarketingImageSource', () => ({
  useMarketingImageSource: (id: string) => ({
    imageUrl: `blob:image-${id}`,
    imageFailed: false,
    onImageError: jest.fn(),
  }),
}));
jest.mock('@/lib/domain-client/upload-deps', () => ({
  useBlobTransferClient: () => ({ downloadBlob: jest.fn() }),
}));
jest.mock('@/hooks/products/useWorkspaceProducts', () => ({
  useWorkspaceProducts: () => ({ refresh: jest.fn() }),
}));

function ok<T>(data: T) {
  return { kind: 'ok' as const, status: 200, data, meta: {} };
}

function refused(code: string, status = 422, params?: unknown) {
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
      ...(params ? { params } : {}),
    },
    meta: {},
  };
}

function Fresh({ children }: { children: ReactNode }) {
  return (
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      {children}
    </SWRConfig>
  );
}

/** Two images of claim 1, one of them kept; one image of claim 2. */
function withImages(extra: Partial<Detail> = {}): Detail {
  return ideasDetail({
    ads: [ad('av_1', 'c1', { kept: true }), ad('av_2', 'c2'), ad('av_3', 'c3')],
    pending: [],
    ...extra,
  });
}

async function renderPage(detail: Detail = withImages()) {
  mockGateway.getMarketingIdeas.mockResolvedValue(ok(detail));
  const view = render(
    <Fresh>
      <IdeasPage product={product()} />
    </Fresh>
  );
  await screen.findByRole('heading', { level: 2, name: /claims/ });
  return view;
}

const claim = (number: number) =>
  screen.getByRole('region', { name: new RegExp(`^Claim ${number}`) });

function openMore(
  number: number,
  action: 'Change this claim' | 'Delete this claim'
) {
  const section = within(claim(number));
  fireEvent.click(
    section.getByRole('button', { name: `More for claim ${number}` })
  );
  fireEvent.click(section.getByRole('button', { name: action }));
}

beforeEach(() => {
  jest.clearAllMocks();
  mockGateway.getMarketingCapabilities.mockResolvedValue(
    ok(plannerCapabilities('0.08'))
  );
  mockGateway.getGenerationCapabilities.mockResolvedValue(
    ok(generationCapabilities())
  );
});

describe('changing a claim (#224)', () => {
  it('says what saving does to the images made, then sends the claim in the user words for free', async () => {
    const detail = withImages();
    await renderPage(detail);
    openMore(1, 'Change this claim');
    const form = screen.getByRole('form', { name: 'Change claim 1' });
    expect(form).toHaveTextContent(
      'Once saved, the 2 images already made for this claim need making again on the new claim before they can be downloaded. 1 of them is kept.'
    );
    expect(form).toHaveTextContent(
      'Changing a claim is free. The ads keep their headline and body text.'
    );
    // It starts from the claim as it is: its words and the facts it rests on.
    expect(within(form).getByLabelText(/^The claim/)).toHaveValue(
      'Plug in at night and forget about it'
    );
    expect(
      within(form).getByRole('checkbox', {
        name: /Schedule charging in the Acme Charge app/,
      })
    ).toBeChecked();
    expect(
      within(form).getByRole('checkbox', { name: /IP55-rated enclosure/ })
    ).not.toBeChecked();

    fireEvent.change(within(form).getByLabelText(/^The claim/), {
      target: { value: '  Wake up to a charged car  ' },
    });
    fireEvent.click(
      within(form).getByRole('checkbox', {
        name: /Load balancing with the home/,
      })
    );
    const changed = {
      ...detail,
      revision: 4,
      ideas: {
        ...detail.ideas!,
        angles: detail.ideas!.angles.map((angle) =>
          angle.id === 'a1'
            ? {
                ...angle,
                proposition: 'Wake up to a charged car',
                fact_ids: ['f1', 'f3'],
              }
            : angle
        ),
      },
    };
    mockGateway.editMarketingIdeasClaim.mockResolvedValue(ok(changed));
    fireEvent.click(
      within(form).getByRole('button', { name: 'Save the claim' })
    );

    await within(claim(1)).findByRole('heading', {
      name: 'Wake up to a charged car',
    });
    expect(mockGateway.editMarketingIdeasClaim).toHaveBeenCalledWith({
      workspaceSlug: 'acme',
      actionInstanceId: expect.any(String),
      expectedRevision: 3,
      projectId: 'mp_secret',
      angleId: 'a1',
      body: {
        proposition: 'Wake up to a charged car',
        rationale: ANGLES[0].rationale,
        fact_ids: ['f1', 'f3'],
      },
    });
    expect(screen.queryByRole('form', { name: 'Change claim 1' })).toBeNull();
  });

  it('a claim that still names a deleted fact: saving takes it off', async () => {
    const detail = withImages();
    detail.ideas = {
      ...detail.ideas!,
      angles: detail.ideas!.angles.map((angle) =>
        angle.id === 'a2' ? { ...angle, fact_ids: ['f2', 'f9'] } : angle
      ),
    };
    await renderPage(detail);
    openMore(2, 'Change this claim');
    const form = screen.getByRole('form', { name: 'Change claim 2' });
    expect(form).toHaveTextContent(
      'This claim still names a deleted fact. Saving takes it off.'
    );
    mockGateway.editMarketingIdeasClaim.mockResolvedValue(
      ok({ ...detail, revision: 4 })
    );
    // Nothing else changed, and still it is sent: the deleted fact goes.
    fireEvent.click(
      within(form).getByRole('button', { name: 'Save the claim' })
    );
    await waitFor(() =>
      expect(mockGateway.editMarketingIdeasClaim).toHaveBeenCalledTimes(1)
    );
    expect(
      mockGateway.editMarketingIdeasClaim.mock.calls[0][0].body.fact_ids
    ).toEqual(['f1', 'f2']);
  });

  it('nothing changed: nothing is sent', async () => {
    await renderPage();
    openMore(3, 'Change this claim');
    fireEvent.click(screen.getByRole('button', { name: 'Save the claim' }));
    expect(screen.queryByRole('form', { name: 'Change claim 3' })).toBeNull();
    expect(mockGateway.editMarketingIdeasClaim).not.toHaveBeenCalled();
  });

  it('words the facts do not support: names them and says how to fix it', async () => {
    await renderPage();
    openMore(1, 'Change this claim');
    const form = screen.getByRole('form', { name: 'Change claim 1' });
    fireEvent.change(within(form).getByLabelText(/^The claim/), {
      target: { value: 'Charges at 22 kW, 3x faster' },
    });
    mockGateway.editMarketingIdeasClaim.mockResolvedValue(
      refused('claim_unsupported', 422, {
        claims: ['22 kW', 'Charges 3x faster than a wall socket'],
      })
    );
    fireEvent.click(
      within(form).getByRole('button', { name: 'Save the claim' })
    );
    expect(await within(form).findByRole('alert')).toHaveTextContent(
      "These aren't in the facts you picked: 22 kW, Charges 3x faster than a wall socket. Change them, or add a source in the product profile first."
    );
    expect(
      screen.getByRole('form', { name: 'Change claim 1' })
    ).toBeInTheDocument();
  });

  it('someone changed the ideas first: says so and offers to refresh', async () => {
    await renderPage();
    openMore(1, 'Change this claim');
    const form = screen.getByRole('form', { name: 'Change claim 1' });
    fireEvent.change(within(form).getByLabelText(/^Why it is worth trying/), {
      target: { value: 'Most owners charge overnight.' },
    });
    mockGateway.editMarketingIdeasClaim.mockResolvedValue(
      refused('stale_marketing_revision', 409)
    );
    fireEvent.click(
      within(form).getByRole('button', { name: 'Save the claim' })
    );
    const alert = await within(form).findByRole('alert');
    expect(alert).toHaveTextContent(
      'These ideas just changed. Refresh and try again.'
    );
    fireEvent.click(within(alert).getByRole('button', { name: 'Refresh' }));
    await waitFor(() =>
      expect(mockGateway.getMarketingIdeas).toHaveBeenCalledTimes(2)
    );
  });
  it('while the first batch still makes ads, claims are not changed', async () => {
    await renderPage(
      withImages({
        first_batch: {
          id: 'mfb_1',
          state: 'generating',
          needs_attention: false,
          reason: null,
        },
      })
    );
    expect(
      within(claim(1)).queryByRole('button', { name: 'More for claim 1' })
    ).toBeNull();
  });
});

describe('deleting a claim (#224)', () => {
  it('says where its images go before deleting, and they move to the earlier ads', async () => {
    const detail = withImages();
    await renderPage(detail);
    openMore(1, 'Delete this claim');
    const question = screen.getByRole('group', { name: 'Delete claim 1?' });
    expect(question).toHaveTextContent(
      "The 2 images made for it move to “Ads from earlier ideas” at the bottom of the page and can't be downloaded. 1 of them is kept: it stays kept, and the check before downloading lists it."
    );
    expect(question).toHaveTextContent("This can't be undone.");
    const remaining = {
      ...detail,
      revision: 4,
      ideas: {
        ...detail.ideas!,
        angles: detail.ideas!.angles.filter((angle) => angle.id !== 'a1'),
        combinations: COMBINATIONS.filter((c) => c.angle_id !== 'a1'),
      },
      ads: detail.ads.map((item) =>
        item.angle_id === 'a1'
          ? {
              ...item,
              status: 'needs_redo' as const,
              reasons: [
                {
                  code: 'ideas_changed' as const,
                  fact_ids: [],
                  finding_ids: [],
                },
              ],
            }
          : item
      ),
    };
    mockGateway.deleteMarketingIdeasClaim.mockResolvedValue(ok(remaining));
    fireEvent.click(within(question).getByRole('button', { name: 'Delete' }));

    const earlier = await screen.findByRole('region', {
      name: 'Ads from earlier ideas',
    });
    expect(earlier).toHaveTextContent(
      "Their claim was deleted or redrafted, so these images can't be downloaded."
    );
    expect(mockGateway.deleteMarketingIdeasClaim).toHaveBeenCalledWith({
      workspaceSlug: 'acme',
      actionInstanceId: expect.any(String),
      expectedRevision: 3,
      projectId: 'mp_secret',
      angleId: 'a1',
    });
    expect(
      screen.queryByRole('heading', {
        name: 'Plug in at night and forget about it',
      })
    ).toBeNull();
  });

  it('two claims always stay: no delete button, a way forward instead', async () => {
    const detail = withImages();
    detail.ideas = {
      ...detail.ideas!,
      angles: detail.ideas!.angles.filter((angle) => angle.id !== 'a3'),
      combinations: COMBINATIONS.filter((c) => c.angle_id !== 'a3'),
    };
    await renderPage(detail);
    openMore(2, 'Delete this claim');
    const question = screen.getByRole('group', { name: 'Delete claim 2?' });
    expect(question).toHaveTextContent(
      'Keep at least two claims. Change this one instead, or add another claim before deleting it.'
    );
    expect(
      within(question).queryByRole('button', { name: 'Delete' })
    ).toBeNull();
    fireEvent.click(within(question).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('group', { name: 'Delete claim 2?' })).toBeNull();
  });
});
