import { VISUAL_MECHANISMS } from '@/components/workspace/MarketingPlanReview';
import {
  allTicked,
  checkSheet,
  factKey,
  findingKey,
  leftToTick,
  refusedAds,
  verifyBody,
} from '@/lib/ideas/check-sheet';
import {
  checkView,
  openFindings,
  stillWorking,
  versionsOf,
} from '@/lib/ideas/ideas-view';
import { TELLING_TYPES } from '@/lib/ideas/mechanisms';
import { ad, check, finding, ideasDetail } from './fixtures';

/**
 * The check before downloading, as data (ADR 0018 §3, #198, #201): only what
 * the kept ads use, one tick per thing, one `verify_marketing_ideas` body.
 */

const open = (ids: string[]) => ({
  code: 'findings_open' as const,
  fact_ids: [],
  finding_ids: ids,
});
const reason = (code: string, factIds: string[] = []) => ({
  code: code as 'facts_unverified',
  fact_ids: factIds,
  finding_ids: [],
});

function sheetDetail() {
  return ideasDetail({
    ads: [
      // Kept, cites f1 (unchecked); the check found a copy mismatch.
      ad('av_1', 'c1', {
        kept: true,
        cited_fact_ids: ['f1'],
        check: check('qc_1', [
          finding('copy_consistent', {
            gate_reason: 'rendered_text_mismatch',
          }),
        ]),
        reasons: [
          reason('facts_unverified', ['f1']),
          open(['copy_consistent']),
        ],
      }),
      // Kept, made without a photo: its look is asked once, not as a finding.
      ad('av_3', 'c3', {
        kept: true,
        used_product_photo: false,
        cited_fact_ids: ['f2', 'f3'],
        check: check('qc_3', [
          finding('product_fidelity', {
            status: 'unknown',
            gate_reason: 'product_reference_missing',
          }),
        ]),
        reasons: [
          reason('facts_unverified', ['f2']),
          reason('appearance_unconfirmed'),
          open(['product_fidelity']),
        ],
      }),
      // Not kept: none of its facts are listed.
      ad('av_4', 'c4', {
        cited_fact_ids: ['f2'],
        reasons: [reason('facts_unverified', ['f2'])],
      }),
      // Kept, never checked: waits for a check.
      ad('av_5', 'c5', {
        kept: true,
        origin: 'corrected',
        source_asset_version_id: 'av_x',
        cited_fact_ids: ['f3'],
        reasons: [reason('check_missing')],
      }),
      // Kept, needs redoing: checking cannot rescue it.
      ad('av_6', 'c6', {
        kept: true,
        status: 'needs_redo',
        cited_fact_ids: ['f1'],
        reasons: [reason('facts_removed', ['f9'])],
      }),
    ],
    pending: [],
  });
}

describe('checkSheet (#201)', () => {
  it('lists only what the kept ads that can be downloaded use', () => {
    const model = checkSheet(sheetDetail());
    expect(model.kept).toBe(4);
    expect(model.ready.map((a) => a.asset_version_id)).toEqual([
      'av_1',
      'av_3',
    ]);
    expect(model.waiting).toEqual([
      expect.objectContaining({ why: 'check_needed' }),
    ]);
    expect(model.redo.map((r) => r.ad.asset_version_id)).toEqual(['av_6']);
    // f1 and f2 are unchecked and cited by ready ads; f3 is checked already.
    expect(model.facts.map((fact) => fact.id)).toEqual(['f1', 'f2']);
    expect(model.checkedFacts).toBe(1);
    expect(model.looks.map((a) => a.asset_version_id)).toEqual(['av_3']);
    // The no-photo ad's product finding is its look, not a separate finding.
    expect(model.findings).toEqual([
      expect.objectContaining({
        checkId: 'qc_1',
        findings: [expect.objectContaining({ check_id: 'copy_consistent' })],
      }),
    ]);
  });

  it('can be confirmed only once every fact, look and finding is ticked', () => {
    const detail = sheetDetail();
    const model = checkSheet(detail);
    const facts = new Set<string>();
    const looks = new Set<string>();
    const findings = new Set<string>();
    expect(allTicked(model, { facts, looks, findings })).toBe(false);
    expect(leftToTick(model, { facts, looks, findings })).toBe(4);
    model.facts.forEach((fact) => facts.add(factKey(fact)));
    looks.add('av_3');
    expect(allTicked(model, { facts, looks, findings })).toBe(false);
    findings.add(findingKey('av_1', 'qc_1', 'copy_consistent'));
    expect(allTicked(model, { facts, looks, findings })).toBe(true);
    expect(verifyBody(model, { facts, looks, findings })).toEqual({
      fact_ids: ['f1', 'f2'],
      appearance_confirmed: ['av_3'],
      accepted_findings: [
        {
          asset_version_id: 'av_1',
          check_id: 'qc_1',
          finding_ids: ['copy_consistent'],
        },
      ],
    });
  });

  it('asks again after a newer check, and after a fact changed its words', () => {
    const detail = sheetDetail();
    const before = checkSheet(detail);
    const ticks = {
      facts: new Set(before.facts.map(factKey)),
      looks: new Set(['av_3']),
      findings: new Set([findingKey('av_1', 'qc_1', 'copy_consistent')]),
    };
    expect(allTicked(before, ticks)).toBe(true);
    const newer = sheetDetail();
    newer.ads[0] = {
      ...newer.ads[0],
      check: { ...newer.ads[0].check!, id: 'qc_2' },
    };
    newer.understanding = {
      ...newer.understanding!,
      facts: newer.understanding!.facts!.map((fact) =>
        fact.id === 'f1' ? { ...fact, statement: 'Changed words' } : fact
      ),
    };
    const after = checkSheet(newer);
    expect(allTicked(after, ticks)).toBe(false);
    expect(leftToTick(after, ticks)).toBe(2);
  });

  it('records nothing when every kept ad is exportable already', () => {
    const detail = ideasDetail({
      ads: [
        ad('av_1', 'c1', {
          kept: true,
          status: 'exportable',
          check: check('qc_1'),
        }),
      ],
      pending: [],
    });
    const model = checkSheet(detail);
    const none = {
      facts: new Set<string>(),
      looks: new Set<string>(),
      findings: new Set<string>(),
    };
    expect(allTicked(model, none)).toBe(true);
    expect(verifyBody(model, none)).toBeNull();
  });

  it('reads the ads a refused download names, and nothing else', () => {
    expect(
      refusedAds({
        ads: [
          { asset_version_id: 'av_1', status: 'draft', reasons: [] },
          'not an ad',
        ],
      })
    ).toEqual([{ asset_version_id: 'av_1', status: 'draft', reasons: [] }]);
    expect(refusedAds(undefined)).toEqual([]);
  });
});

describe('ideas view for S13 (#201)', () => {
  it('names where each check stands, and lists open findings one by one', () => {
    const detail = sheetDetail();
    expect(checkView(detail.ads[0], detail)).toBe('findings');
    expect(openFindings(detail.ads[0]).map((f) => f.check_id)).toEqual([
      'copy_consistent',
    ]);
    expect(checkView(detail.ads[3], detail)).toBe('unchecked');
    const running = {
      ...detail.ads[0],
      check: { ...detail.ads[0].check!, state: 'running' as const },
    };
    expect(checkView(running, detail)).toBe('checking');
    // While the first batch checks its ads, a generated ad without a check yet is "being checked".
    const checking = ideasDetail({
      first_batch: {
        id: 'mfb_1',
        state: 'checking' as unknown as 'done',
        needs_attention: false,
        reason: null,
      },
      pending: [],
    });
    expect(checkView(ad('av_9', 'c1'), checking)).toBe('checking');
    expect(stillWorking(checking)).toBe(true);
    expect(stillWorking(ideasDetail({ pending: [], ads: [running] }))).toBe(
      true
    );
  });

  it('groups an ad with the versions it was made from and made into', () => {
    const detail = ideasDetail({
      ads: [
        ad('av_1', 'c1', { created_at: '2026-11-17T08:00:00+00:00' }),
        ad('av_2', 'c1', {
          origin: 'corrected',
          source_asset_version_id: 'av_1',
          created_at: '2026-11-17T09:00:00+00:00',
        }),
        ad('av_3', 'c1', {
          origin: 'image_edit',
          source_asset_version_id: 'av_2',
          created_at: '2026-11-17T10:00:00+00:00',
        }),
        ad('av_4', 'c2'),
      ],
    });
    expect(versionsOf(detail, 'av_3').map((a) => a.asset_version_id)).toEqual([
      'av_1',
      'av_2',
      'av_3',
    ]);
    expect(versionsOf(detail, 'av_4').map((a) => a.asset_version_id)).toEqual([
      'av_4',
    ]);
  });

  it('offers the same ten ways of telling as the plan review', () => {
    expect([...TELLING_TYPES].sort()).toEqual([...VISUAL_MECHANISMS].sort());
  });
});
