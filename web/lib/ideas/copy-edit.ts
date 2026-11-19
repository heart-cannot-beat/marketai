import type { components } from '@/lib/domain-client/generated/domain-v1';
import {
  fromUpload,
  type IdeasAd,
  type IdeasDetail,
} from '@/lib/ideas/ideas-view';

/**
 * What changing one ad's headline and body copy touches (#228, design
 * §5.3), read from `get_marketing_ideas`. The server decides what a change
 * means and refuses words the ad's facts do not state; these helpers only
 * say what the user is about to change.
 */

export type EditCopyBody = components['schemas']['EditIdeasCopy'];

/** The longest headline or body text an ad may have (`CreativeCombination`). */
export const COPY_LIMIT = 2000;

/**
 * The copy one place of the ideas -- a claim told one way -- holds now, or
 * `null` when the ideas no longer hold that place.
 */
export function copyOf(
  detail: IdeasDetail,
  combinationId: string
): EditCopyBody | null {
  const combination = detail.ideas?.combinations.find(
    (item) => item.id === combinationId
  );
  return combination
    ? { headline: combination.headline, body_copy: combination.body_copy }
    : null;
}

/** The form as the server reads it: the ends of each text are not part of it. */
export function trimmedCopy(form: EditCopyBody): EditCopyBody {
  return { headline: form.headline.trim(), body_copy: form.body_copy.trim() };
}

/** Whether the form says something other than the copy the place has now. */
export function copyChanged(
  current: EditCopyBody,
  form: EditCopyBody
): boolean {
  const next = trimmedCopy(form);
  return (
    next.headline !== current.headline || next.body_copy !== current.body_copy
  );
}

/** Whether the form can be saved: both texts say something, within the limit. */
export function copyReady(form: EditCopyBody): boolean {
  const next = trimmedCopy(form);
  return [next.headline, next.body_copy].every(
    (text) => text.length > 0 && text.length <= COPY_LIMIT
  );
}

/**
 * The images that need making again once this place's copy changes, and how
 * many of them the user kept: those made here that are its ad now. An image
 * made for earlier content of the place already needs making again, and an
 * uploaded image -- or a crop of one -- keeps its own words (#228).
 */
export function copyImages(
  detail: IdeasDetail,
  combinationId: string
): { ads: IdeasAd[]; kept: number } {
  const ads = detail.ads.filter(
    (ad) =>
      ad.combination_id === combinationId &&
      !(ad.reasons ?? []).some((reason) => reason.code === 'ideas_changed') &&
      !fromUpload(detail, ad)
  );
  return { ads, kept: ads.filter((ad) => ad.kept).length };
}
