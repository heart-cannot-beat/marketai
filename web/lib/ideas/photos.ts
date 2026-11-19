import type { components } from '@/lib/domain-client/generated/domain-v1';
import type { IdeasAd, IdeasDetail } from './ideas-view';

/**
 * The product photos of ad ideas (ADR 0018 §3, design §5.3, #199, #229,
 * #233): the photos the product's reading found and the ones the user added
 * to the product, each with the user's checking.
 * Both the check before downloading and the product profile read them from
 * the same `get_marketing_ideas` answer, so a photo reads the same on both.
 *
 * Checking a photo declares that it is a real photo of this product and that
 * the user may use it in ads; rejecting it makes every ad made with it need
 * redoing. Both are `verify_marketing_ideas` (`photo_ids`,
 * `rejected_photo_ids`).
 */

export type IdeasPhoto = components['schemas']['IdeasPhoto'];
export type PhotoStatus = IdeasPhoto['status'];
/** What a photo shows: the whole product, another angle, or a close-up. */
export type PhotoRole = IdeasPhoto['role'];
/** A logo the user added to the product (#233): listed apart, never a photo of the product. */
export type IdeasLogo = components['schemas']['IdeasLogo'];
/** What an upload is added to the product as: a photo of it, or the brand's logo. */
export type UploadRole = components['schemas']['AddIdeasPhoto']['role'];

/** The ads of these ideas made with this photo, in the ideas' order. */
export function adsWithPhoto(detail: IdeasDetail, photoId: string): IdeasAd[] {
  const photo = detail.photos?.find((item) => item.photo_id === photoId);
  const named = new Set(photo?.asset_version_ids ?? []);
  return detail.ads.filter(
    (ad) =>
      (ad.photo_ids ?? []).includes(photoId) || named.has(ad.asset_version_id)
  );
}

/** The photos this ad was made with that the user has not checked yet. */
export function uncheckedPhotosOf(
  detail: IdeasDetail,
  ad: IdeasAd
): IdeasPhoto[] {
  const ids = new Set(ad.photo_ids ?? []);
  return (detail.photos ?? []).filter(
    (photo) => photo.status === 'unverified' && ids.has(photo.photo_id)
  );
}

/** Whether a product photo this ad used was rejected or may no longer be used. */
export function photoGone(ad: { reasons?: IdeasAd['reasons'] }): boolean {
  return (ad.reasons ?? []).some(
    (reason) => reason.code === 'photo_unavailable'
  );
}

/** The verify body that checks or rejects photos and records nothing else. */
export function photoDecision(
  checked: string[],
  rejected: string[]
): components['schemas']['VerifyIdeas'] {
  return {
    fact_ids: [],
    appearance_confirmed: [],
    accepted_findings: [],
    photo_ids: checked,
    rejected_photo_ids: rejected,
  };
}
