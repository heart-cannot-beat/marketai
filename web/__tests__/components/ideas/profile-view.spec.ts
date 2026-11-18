import {
  factUse,
  newFactId,
  withFactAdded,
  withFactChanged,
  withFactRemoved,
  withUnknownAnswered,
} from '@/lib/ideas/profile-view';
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
