import {
  copyChanged,
  copyImages,
  copyOf,
  copyReady,
  trimmedCopy,
} from '@/lib/ideas/copy-edit';
import { ideasView } from '@/lib/ideas/ideas-view';
import { ad, ideasDetail } from './fixtures';

/** What changing an ad's copy touches, as the edit screen and the ideas page read it (#228). */
const changed = {
  code: 'ideas_changed' as const,
  fact_ids: [],
  finding_ids: [],
};

describe('copy-edit', () => {
  it('reads the copy of a place, and nothing for a place the ideas no longer hold', () => {
    expect(copyOf(ideasDetail(), 'c3')).toEqual({
      headline: 'Rain or shine.',
      body_copy: 'Rain or shine. Acme Charge Home.',
    });
    expect(copyOf(ideasDetail(), 'c9')).toBeNull();
  });

  it('compares and checks the form as the server reads it', () => {
    const current = { headline: 'Set it once.', body_copy: 'Set it once.' };
    expect(
      copyChanged(current, { ...current, headline: ' Set it once. ' })
    ).toBe(false);
    expect(
      copyChanged(current, { ...current, body_copy: 'Set it twice.' })
    ).toBe(true);
    expect(trimmedCopy({ headline: ' A ', body_copy: 'B\n' })).toEqual({
      headline: 'A',
      body_copy: 'B',
    });
    expect(copyReady({ headline: 'A', body_copy: '  ' })).toBe(false);
    expect(copyReady({ headline: 'x'.repeat(2001), body_copy: 'B' })).toBe(
      false
    );
    expect(copyReady({ headline: 'x'.repeat(2000), body_copy: 'B' })).toBe(
      true
    );
  });

  it('counts the images that need making again: not an upload or its crop, not one already of earlier content', () => {
    const detail = ideasDetail({
      ads: [
        ad('av_1', 'c1', { kept: true }),
        ad('av_2', 'c1', {
          origin: 'image_edit',
          source_asset_version_id: 'av_1',
        }),
        ad('av_3', 'c1', { origin: 'uploaded' }),
        ad('av_4', 'c1', {
          origin: 'image_edit',
          source_asset_version_id: 'av_3',
          kept: true,
        }),
        ad('av_5', 'c1', { reasons: [changed], kept: true }),
        ad('av_6', 'c2', { kept: true }),
      ],
      pending: [],
    });
    const { ads, kept } = copyImages(detail, 'c1');
    expect(ads.map((item) => item.asset_version_id)).toEqual(['av_1', 'av_2']);
    expect(kept).toBe(1);
  });
});

describe('ideasView after a change of content (#224, #228)', () => {
  it('lets a place whose images are all of earlier content wait for a new one, under its new headline', () => {
    const detail = ideasDetail({
      ads: [ad('av_1', 'c1', { reasons: [changed] }), ad('av_2', 'c2')],
      pending: [],
    });
    const [first] = ideasView(detail).claims;
    expect(first.slots.map((slot) => [slot.kind, slot.headline])).toEqual([
      // The old image does not borrow the words the place holds now.
      ['ad', null],
      ['not_made', 'Plug in. Go to bed.'],
      ['ad', 'Set it once.'],
    ]);
  });

  it('waits for nothing while an image is being made, or one of the place is current', () => {
    const making = ideasView(
      ideasDetail({
        ads: [ad('av_1', 'c1', { reasons: [changed] })],
        pending: [{ combination_id: 'c1', state: 'running' }],
      })
    );
    expect(making.claims[0].slots.map((slot) => slot.kind)).toEqual([
      'ad',
      'making',
      'not_made',
    ]);
    const current = ideasView(
      ideasDetail({
        ads: [ad('av_1', 'c1', { reasons: [changed] }), ad('av_2', 'c1')],
        pending: [],
      })
    );
    expect(current.claims[0].slots.map((slot) => slot.kind)).toEqual([
      'ad',
      'ad',
      'not_made',
    ]);
  });
});
