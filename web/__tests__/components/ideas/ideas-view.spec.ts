import { ideasView, stillWorking, withKept } from '@/lib/ideas/ideas-view';
import { ad, ideasDetail } from './fixtures';

/** How the ideas page reads `get_marketing_ideas` (#196). */
describe('ideasView', () => {
  it('places each ad, image being made, failure and missing image under its claim', () => {
    const view = ideasView(ideasDetail());
    expect(
      view.claims.map((claim) => [
        claim.number,
        claim.slots.map((slot) => slot.kind),
      ])
    ).toEqual([
      [1, ['ad', 'ad']],
      [2, ['ad', 'ad']],
      [3, ['making', 'failed']],
    ]);
    expect(view).toMatchObject({
      adCount: 4,
      keptCount: 1,
      makingCount: 1,
      withoutPhotoCount: 1,
      earlier: [],
    });
  });

  it('keeps an older image beside a newer attempt at the same ad', () => {
    const view = ideasView(
      ideasDetail({
        ads: [ad('av_1', 'c1')],
        pending: [{ combination_id: 'c1', state: 'running' }],
      })
    );
    expect(view.claims[0].slots.map((slot) => slot.kind)).toEqual([
      'ad',
      'making',
      'not_made',
    ]);
  });

  it('keeps ads whose ad or claim the ideas no longer list', () => {
    const redrafted = ad('av_old', 'c_gone', {
      angle_id: 'a1',
      expression_id: 'e2',
    });
    const orphan = ad('av_orphan', 'c_gone', {
      angle_id: 'a_gone',
      expression_id: 'e_gone',
    });
    const view = ideasView(
      ideasDetail({ ads: [redrafted, orphan], pending: [] })
    );
    const underClaim = view.claims[0].slots.find(
      (slot) => slot.kind === 'ad' && slot.ad.asset_version_id === 'av_old'
    );
    expect(underClaim).toMatchObject({
      headline: null,
      telling: { name: 'App demo' },
    });
    expect(view.earlier).toEqual([
      expect.objectContaining({ kind: 'ad', telling: null, headline: null }),
    ]);
    expect(view.adCount).toBe(2);
  });

  it('says whether anything is still on its way', () => {
    expect(stillWorking(ideasDetail())).toBe(true);
    expect(
      stillWorking(
        ideasDetail({ pending: [{ combination_id: 'c6', state: 'failed' }] })
      )
    ).toBe(false);
    const drafting = ideasDetail({ pending: [] });
    drafting.latest_call = { ...drafting.latest_call!, state: 'running' };
    expect(stillWorking(drafting)).toBe(true);
    expect(stillWorking(undefined)).toBe(false);
  });

  it('sets one keep mark and leaves the others', () => {
    const detail = withKept(ideasDetail(), 'av_2', true);
    expect(detail.ads.map((item) => item.kept)).toEqual([
      true,
      true,
      false,
      false,
    ]);
  });
});
