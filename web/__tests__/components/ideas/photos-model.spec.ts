import {
  allTicked,
  checkSheet,
  factKey,
  leftToTick,
  verifyBody,
} from '@/lib/ideas/check-sheet';
import {
  adsWithPhoto,
  photoDecision,
  photoGone,
  renewRestores,
  uncheckedPhotosOf,
} from '@/lib/ideas/photos';
import { ad, check, ideasDetail, photo } from './fixtures';

/**
 * Product photos in the check before downloading, as data (#199, #229): the
 * unchecked photos the ads to download were made with are asked once, with
 * the facts, looks and findings; a photo these ideas do not list keeps its
 * ad out of the download.
 */

const reason = (code: string, factIds: string[] = []) => ({
  code: code as 'facts_unverified',
  fact_ids: factIds,
  finding_ids: [],
});

/** Two kept drafts on the main photo p1 (one also on p2), a kept one on the checked p3, one not kept. */
function photoDetail() {
  return ideasDetail({
    photos: [
      photo('p1', { asset_version_ids: ['av_1', 'av_2', 'av_4'] }),
      photo('p2', { role: 'detail', asset_version_ids: ['av_2'] }),
      photo('p3', {
        role: 'angle',
        status: 'verified',
        asset_version_ids: ['av_5'],
      }),
      photo('p4', { role: 'detail', status: 'rejected' }),
    ],
    ads: [
      ad('av_1', 'c1', {
        kept: true,
        photo_ids: ['p1'],
        cited_fact_ids: ['f3'],
        check: check('qc_1'),
        reasons: [reason('photo_unverified')],
      }),
      ad('av_2', 'c2', {
        kept: true,
        photo_ids: ['p1', 'p2'],
        cited_fact_ids: ['f1'],
        check: check('qc_2'),
        reasons: [
          reason('facts_unverified', ['f1']),
          reason('photo_unverified'),
        ],
      }),
      ad('av_4', 'c4', {
        photo_ids: ['p1'],
        reasons: [reason('photo_unverified'), reason('check_missing')],
      }),
      ad('av_5', 'c5', {
        kept: true,
        status: 'exportable',
        photo_ids: ['p3'],
        check: check('qc_5'),
      }),
    ],
    pending: [],
  });
}

const none = () => ({
  facts: new Set<string>(),
  photos: new Set<string>(),
  looks: new Set<string>(),
  findings: new Set<string>(),
});

describe('product photos in the check before downloading (#229)', () => {
  it('lists the unchecked photos the ads to download were made with, in the reading’s order', () => {
    const model = checkSheet(photoDetail());

    // An unchecked photo no longer keeps an ad waiting: it is checked here.
    expect(model.waiting).toEqual([]);
    expect(model.ready.map((item) => item.asset_version_id)).toEqual([
      'av_1',
      'av_2',
      'av_5',
    ]);
    expect(model.photos.map((item) => item.photo.photo_id)).toEqual([
      'p1',
      'p2',
    ]);
    const [main, detail] = model.photos;
    expect(main.ready.map((item) => item.asset_version_id)).toEqual([
      'av_1',
      'av_2',
    ]);
    // Rejecting it would touch the ad not kept too.
    expect(main.used.map((item) => item.asset_version_id)).toEqual([
      'av_1',
      'av_2',
      'av_4',
    ]);
    expect(detail.ready.map((item) => item.asset_version_id)).toEqual(['av_2']);
    // The ad on a checked photo is no look to confirm and no photo to check.
    expect(model.looks).toEqual([]);
    expect(model.withPhoto.map((item) => item.asset_version_id)).toEqual([
      'av_5',
    ]);
  });

  it('confirms only once every photo is ticked too, and sends them with the facts', () => {
    const model = checkSheet(photoDetail());
    const ticks = none();
    expect(leftToTick(model, ticks)).toBe(3);
    model.facts.forEach((fact) => ticks.facts.add(factKey(fact)));
    ticks.photos.add('p1');
    expect(allTicked(model, ticks)).toBe(false);
    ticks.photos.add('p2');
    // A photo ticked earlier that the sheet no longer lists is not sent.
    ticks.photos.add('p_gone');
    expect(allTicked(model, ticks)).toBe(true);
    expect(verifyBody(model, ticks)).toEqual({
      fact_ids: ['f1'],
      appearance_confirmed: [],
      accepted_findings: [],
      photo_ids: ['p1', 'p2'],
    });
  });

  it('records the photos alone when nothing else is left to confirm', () => {
    const detail = photoDetail();
    detail.ads = [detail.ads[0]];
    const model = checkSheet(detail);
    const ticks = none();
    ticks.photos.add('p1');
    expect(verifyBody(model, ticks)).toEqual({
      fact_ids: [],
      appearance_confirmed: [],
      accepted_findings: [],
      photo_ids: ['p1'],
    });
  });

  it('keeps an ad out when its unchecked photo is not one these ideas list', () => {
    const detail = photoDetail();
    detail.ads[0] = { ...detail.ads[0], photo_ids: ['p_elsewhere'] };
    const model = checkSheet(detail);
    expect(model.waiting).toEqual([
      expect.objectContaining({
        ad: expect.objectContaining({ asset_version_id: 'av_1' }),
        why: 'photo',
      }),
    ]);
    expect(model.photos.map((item) => item.photo.photo_id)).toEqual([
      'p1',
      'p2',
    ]);
    expect(model.photos[0].ready.map((item) => item.asset_version_id)).toEqual([
      'av_2',
    ]);
  });

  it('an ad on a rejected photo needs redoing: nothing to check for it', () => {
    const detail = photoDetail();
    detail.ads[0] = {
      ...detail.ads[0],
      status: 'needs_redo',
      photo_ids: ['p4'],
      reasons: [reason('photo_unavailable')],
    };
    const model = checkSheet(detail);
    expect(model.redo.map(({ ad: item }) => item.asset_version_id)).toEqual([
      'av_1',
    ]);
    expect(model.photos[0].ready.map((item) => item.asset_version_id)).toEqual([
      'av_2',
    ]);
    expect(photoGone(detail.ads[0])).toBe(true);
    expect(photoGone(detail.ads[1])).toBe(false);
  });
});

describe('photo helpers (#229)', () => {
  it('finds the ads made with a photo and the unchecked photos of an ad', () => {
    const detail = photoDetail();
    expect(
      adsWithPhoto(detail, 'p2').map((item) => item.asset_version_id)
    ).toEqual(['av_2']);
    expect(adsWithPhoto(detail, 'p4')).toEqual([]);
    expect(
      uncheckedPhotosOf(detail, detail.ads[1]).map((item) => item.photo_id)
    ).toEqual(['p1', 'p2']);
    expect(uncheckedPhotosOf(detail, detail.ads[3])).toEqual([]);
  });

  it('a photo decision records nothing but the photos', () => {
    expect(photoDecision(['p1'], [])).toEqual({
      fact_ids: [],
      appearance_confirmed: [],
      accepted_findings: [],
      photo_ids: ['p1'],
      rejected_photo_ids: [],
    });
    expect(photoDecision([], ['p2']).rejected_photo_ids).toEqual(['p2']);
  });
});

describe('an ad a renewal brings back (#233)', () => {
  const gone = [reason('photo_unavailable')];
  const ran = (extra = {}) =>
    photo('p1', {
      status: 'unavailable',
      unavailable_reason: 'expired',
      valid_to: '2026-11-17',
      ...extra,
    });

  it('when every photo it can no longer use only ran out', () => {
    const made = ad('av_1', 'c1', { photo_ids: ['p1', 'p2'], reasons: gone });
    const detail = ideasDetail({
      photos: [ran(), photo('p2', { status: 'verified' })],
      ads: [made],
    });
    expect(renewRestores(detail, made)).toBe(true);
  });

  it('not when one of them was rejected or withdrawn, or nothing ran out', () => {
    const made = ad('av_1', 'c1', { photo_ids: ['p1', 'p2'], reasons: gone });
    for (const other of [
      photo('p2', { status: 'rejected' }),
      photo('p2', { status: 'unavailable', unavailable_reason: 'withdrawn' }),
    ]) {
      expect(
        renewRestores(
          ideasDetail({ photos: [ran(), other], ads: [made] }),
          made
        )
      ).toBe(false);
    }
    expect(
      renewRestores(
        ideasDetail({
          photos: [ran({ unavailable_reason: 'file_gone' })],
          ads: [made],
        }),
        made
      )
    ).toBe(false);
    // An ad no photo of these ideas explains, or one that does not need redoing for a photo.
    expect(renewRestores(ideasDetail({ photos: [], ads: [made] }), made)).toBe(
      false
    );
    const fine = ad('av_2', 'c2', { photo_ids: ['p1'], reasons: [] });
    expect(
      renewRestores(ideasDetail({ photos: [ran()], ads: [fine] }), fine)
    ).toBe(false);
  });
});
