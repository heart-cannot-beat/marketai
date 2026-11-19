import {
  factUse,
  newFactId,
  withAudience,
  withFactAdded,
  withFactChanged,
  withFactRemoved,
  withUnknownAnswered,
  withUnsourcedAnswered,
  withUnsourcedDropped,
} from '@/lib/ideas/profile-view';
import {
  claimChanged,
  claimFactIds,
  claimFormStart,
  claimImages,
  removedFactsOf,
} from '@/lib/ideas/claim-edit';
import { ad, ideasDetail } from './fixtures';

/** The documents the product profile sends back (#202). */
describe('profile view', () => {
  const understanding = ideasDetail().understanding!;

  it('counts the ads standing on a fact and the claims naming it themselves', () => {
    const detail = ideasDetail({
      ads: [
        ad('av_1', 'c1', { cited_fact_ids: ['f1', 'f3'] }),
        ad('av_2', 'c3', { cited_fact_ids: ['f2'] }),
      ],
    });
    // Every way of telling in the fixture cites f1; only the second claim names f2.
    expect(factUse(detail, 'f1')).toEqual({ ads: 1, claims: 3 });
    expect(factUse(detail, 'f2')).toEqual({ ads: 1, claims: 1 });
    expect(factUse(detail, 'f3')).toEqual({ ads: 1, claims: 1 });
    expect(factUse(detail, 'f9')).toEqual({ ads: 0, claims: 0 });
  });

  it('changes one fact and leaves every other part as it was', () => {
    const next = withFactChanged(understanding, 'f2', {
      statement: '  IP55 enclosure ',
      source_quote: 'An IP55-rated enclosure for outdoor installs.',
    });
    expect(next.facts![1]).toEqual({
      ...understanding.facts![1],
      statement: 'IP55 enclosure',
    });
    expect(next.facts![0]).toBe(understanding.facts![0]);
    expect(next.inferences).toBe(understanding.inferences);
  });

  it('removes a fact from the facts and from every guess that named it', () => {
    const next = withFactRemoved(understanding, 'f1');
    expect(next.facts!.map((fact) => fact.id)).toEqual(['f2', 'f3']);
    expect(next.inferences[0].fact_ids).toEqual([]);
  });

  it('adds a fact unchecked under an id nothing uses', () => {
    const next = withFactAdded(
      understanding,
      { statement: 'A', source_quote: 'B' },
      newFactId(understanding, () => 'n-e-w')
    );
    expect(next.facts!.at(-1)).toEqual({
      id: 'fact-new',
      statement: 'A',
      source_quote: 'B',
      confirmation: 'unconfirmed',
    });
  });

  it('never reuses an id the understanding holds', () => {
    const taken = {
      ...understanding,
      facts: [
        ...understanding.facts!,
        {
          id: 'fact-a',
          statement: 'x',
          source_quote: 'y',
          confirmation: 'unconfirmed' as const,
        },
      ],
    };
    const ids = ['a', 'b'];
    expect(newFactId(taken, () => ids.shift()!)).toBe('fact-b');
  });

  it('answers what the AI did not know with a fact and takes it off that list', () => {
    const asked = {
      ...understanding,
      unknowns: ['Warranty length', 'Installer needed?'],
    };
    const next = withUnknownAnswered(
      asked,
      'Warranty length',
      { statement: '3-year warranty', source_quote: '3-year warranty.' },
      'fact-w'
    );
    expect(next.unknowns).toEqual(['Installer needed?']);
    expect(next.facts!.at(-1)).toMatchObject({
      id: 'fact-w',
      confirmation: 'unconfirmed',
    });
  });
});

/** Claims without a source and the audience (#224). */
describe('unsourced claims and the audience', () => {
  const base = ideasDetail().understanding!;
  const understanding = {
    ...base,
    unsupported_claims: [
      { id: 'u1', text: 'Charges 3x faster than a wall socket' },
      { id: 'u2', text: 'The quietest home charger on the market' },
    ],
  };

  it('a source turns one into an unchecked fact and takes only it off the list', () => {
    const next = withUnsourcedAnswered(
      understanding,
      'u1',
      { statement: ' 3x faster ', source_quote: 'Lab test: 3x faster.' },
      'fact-lab'
    );
    expect(next.unsupported_claims).toEqual([
      understanding.unsupported_claims[1],
    ]);
    expect(next.facts!.at(-1)).toEqual({
      id: 'fact-lab',
      statement: '3x faster',
      source_quote: 'Lab test: 3x faster.',
      confirmation: 'unconfirmed',
    });
  });

  it("don't need it takes one off and adds nothing", () => {
    const next = withUnsourcedDropped(understanding, 'u2');
    expect(next.unsupported_claims).toEqual([
      understanding.unsupported_claims[0],
    ]);
    expect(next.facts).toBe(understanding.facts);
  });

  it('a new fact never takes an unsourced claim id', () => {
    const ids = ['u1', 'abc'];
    expect(newFactId(understanding, () => ids.shift()!)).toBe('fact-u1');
  });

  it('the audience in the user words, trimmed', () => {
    expect(withAudience(understanding, '  Renters  ').audience).toBe('Renters');
  });
});

/** What changing or deleting a claim touches (#224). */
describe('claim edit', () => {
  const detail = ideasDetail({
    ads: [ad('av_1', 'c1', { kept: true }), ad('av_2', 'c2'), ad('av_3', 'c3')],
  });
  const first = detail.ideas!.angles[0];

  it('a claim names its own facts and its ways of telling facts', () => {
    const angle = {
      ...first,
      fact_ids: ['f3'],
      expressions: first.expressions.map((e) => ({
        ...e,
        evidence_basis: { fact_ids: ['f1', 'f9'], gap: '' },
      })),
    };
    expect(claimFactIds(angle)).toEqual(['f3', 'f1', 'f9']);
    const named = {
      ...detail,
      ideas: {
        ...detail.ideas!,
        angles: [angle, ...detail.ideas!.angles.slice(1)],
      },
    };
    expect(removedFactsOf(named, first.id)).toEqual(['f9']);
    // The form starts from the facts that still exist, in the profile's order.
    expect(claimFormStart(named, angle).fact_ids).toEqual(['f1', 'f3']);
    // A deleted fact the claim names is a change on its own.
    expect(claimChanged(named, angle, claimFormStart(named, angle))).toBe(true);
  });

  it('nothing changed is nothing to send', () => {
    expect(claimChanged(detail, first, claimFormStart(detail, first))).toBe(
      false
    );
    expect(
      claimChanged(detail, first, {
        ...claimFormStart(detail, first),
        proposition: ` ${first.proposition} `,
      })
    ).toBe(false);
    expect(
      claimChanged(detail, first, {
        ...claimFormStart(detail, first),
        fact_ids: [],
      })
    ).toBe(true);
  });

  it('counts the images made for a claim and the kept ones', () => {
    const images = claimImages(detail, 'a1');
    expect(images.ads.map((item) => item.asset_version_id)).toEqual([
      'av_1',
      'av_2',
    ]);
    expect(images.kept).toBe(1);
  });
});
