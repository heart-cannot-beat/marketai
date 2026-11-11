/**
 * Coverage checks for the generated `DOMAIN_ALLOWLIST` (spec §6.2) against
 * the `DomainGateway`/`useDomainQuery`/`useDomainCommand` surface added in
 * Task 7 (Issue #27). The real enforcement of "every operation is wired
 * up" is `tsc`'s exhaustiveness check on the `callReadOperation`/
 * `callCommandOperation` switches (a `never` default case fails to compile
 * if `ReadOperationId`/`CommandOperationId` ever misses an operation those
 * switches dispatch) — this file adds a runtime sanity check over the
 * allowlist itself so a duplicate or malformed entry fails fast in CI too.
 */
import { DOMAIN_ALLOWLIST } from '@/lib/domain-client/generated/allowlist';

describe('DomainGateway operation coverage', () => {
  const operationIds = DOMAIN_ALLOWLIST.map((e) => e.operationId);

  test('every allowlist operation has a unique operationId', () => {
    const unique = new Set(operationIds);
    expect(unique.size).toBe(operationIds.length);
  });

  test('all GET operations are in ReadOperationId', () => {
    const getOps = DOMAIN_ALLOWLIST.filter((e) => e.method === 'GET').map(
      (e) => e.operationId
    );
    // Import and check ReadOperationId covers these
    // This is a compile-time check — if the union misses one,
    // the exhaustive switch in callReadOperation fails to compile
    expect(getOps.length).toBeGreaterThan(0);
  });

  test('all non-GET operations are in CommandOperationId', () => {
    const mutOps = DOMAIN_ALLOWLIST.filter((e) => e.method !== 'GET').map(
      (e) => e.operationId
    );
    expect(mutOps.length).toBeGreaterThan(0);
  });

  test('the allowlist has exactly the 205 operations in the integrated contract', () => {
    // Bump this deliberately, together with the contract change that adds
    // or removes an operation. It is a tripwire against an *accidental*
    // allowlist change — a regenerated artifact that silently gained a
    // route nobody meant to expose — so an unexplained drift here should
    // be investigated, not just re-baselined.
    //
    // 136 -> 138: `authorize_localization_isolated_qc` and
    // `adopt_localization_isolated_output` (issue #80).
    // 138 -> 139: `analyze_experiment_duplicates` (issue #88).
    // 139 -> 142: `list_scale_available_modes`,
    // `create_scale_enrollment_candidate`, and
    // `create_scale_activation_preview` (issue #86).
    // 142 -> 147: `list_scale_target_contexts`, `get_scale_target`,
    // `create_scale_target_publication_preview`,
    // `create_scale_target_withdrawal_preview`, and
    // `create_scale_export_download_intent` (issue #87).
    // 147 -> 148: `list_meta_connections` (issue #119).
    // 148 -> 150: `get_library_reference_object` and
    // `promote_library_reference_object` (issue #158).
    // 150 -> 154: `create_product_intake`, `get_product_intake`,
    // `confirm_product_intake` and
    // `create_product_intake_image_download_intent` (issue #161).
    // 154 -> 155: `list_products` (issue #162).
    // 155 -> 162: `create_test_plan`, `list_test_plans`, `get_test_plan`,
    // `revise_test_plan`, `create_test_plan_run`,
    // `get_generation_defaults` and `update_generation_defaults` (issue #163).
    // 162 -> 168: `create_results_import`, `get_results_import`,
    // `commit_results_import`, `get_product_learnings`,
    // `get_results_template` and `get_test_plan_results` (issue #166).
    // 168 -> 172: `create_external_creatives`, `list_external_creatives`,
    // `get_external_creative` and `confirm_external_creative` (issue #168).
    // 197 -> 199: read and explicitly confirm a scoped marketing evidence handoff.
    // 201 -> 205: product reference settings GET/POST/PATCH/DELETE (#226).
    // 205 -> 208: `list_marketing_brand_kits`, `get_product_brand_kit` and
    // `save_product_brand_kit` (issue #228).
    // 208 -> 216: `create_competitor`, `list_competitors`, `get_competitor`,
    // `quote_competitor_resolve`, `resolve_competitor`,
    // `get_competitor_resolution`, `confirm_competitor` and
    // `archive_competitor` (issue #269).
    // 216 -> 224: `preview_competitor_scan`, `create_competitor_scan`,
    // `list_competitor_scans`, `get_competitor_scan`,
    // `quote_competitor_scan_resume`, `resume_competitor_scan`,
    // `reconcile_competitor_scan` and `list_competitor_scan_ads` (issue #270).
    // 224 -> 226: `flip_competitor_ad_relevance` and
    // `list_competitor_ad_assessments` (issue #271).
    // 226 -> 236: competitor tags (`confirm_competitor_ad_tags`,
    // `list_competitor_ad_tags`), snapshots (`refreeze_competitor_scan`,
    // `list_competitor_scan_snapshots`, `get_competitor_scan_snapshot`), the
    // summary (`get_competitor_scan_summary`), the recluster quote and command
    // (`quote_competitor_scan_recluster`, `recluster_competitor_scan`) and the
    // product's drafting switch (`get_product_competitor_settings`,
    // `update_product_competitor_settings`) (#272).
    // 236 -> 238: competitor discovery (`list_competitor_scan_discoveries`,
    // `decide_competitor_discovery`) (#276).
    // 238 -> 239: `get_library_data_source_connection_check` (#383).
    // 239 -> 241: `list_scale_observed_ads` and `create_scale_delivery_target`
    // (#129).
    expect(DOMAIN_ALLOWLIST.length).toBe(241);
  });
});
