/**
 * The rights scopes a reference upload offers (#182). The upload stores the
 * code in `ReferenceAssetVersion.rights.scope`; the page shows its label
 * (`library:references.rights-scope-options.<code>`). The server only asks
 * for a non-empty scope of at most 200 characters
 * (`app/library/service.py::normalize_rights_declaration`), so a scope saved
 * before this list -- free text, or a code later dropped from it -- stays
 * valid and is shown as it was saved.
 */
export const RIGHTS_SCOPES = [
  'owned_product_photo',
  'owned_brand_asset',
  'supplier_provided',
  'commissioned_work',
  'licensed_stock',
  'certification_document',
] as const;

export type RightsScope = (typeof RIGHTS_SCOPES)[number];

/** What a new upload declares unless the operator picks another scope. */
export const DEFAULT_RIGHTS_SCOPE: RightsScope = 'owned_product_photo';

export function isKnownRightsScope(value: string): value is RightsScope {
  return (RIGHTS_SCOPES as readonly string[]).includes(value);
}
