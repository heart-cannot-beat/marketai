import { getI18n } from 'react-i18next';
import {
  executionModeLabelKey,
  exportPackageStatus,
  formatRunTime,
  originLabelKey,
  qcStatus,
  reviewStatus,
  runStatus,
  verdictStatus,
} from '@/lib/assets/status-labels';
import { buildRunOrigins, describeRun } from '@/lib/assets/run-display';

/** Issue #191: every enum the asset screens show maps to a translated label;
 * an unknown value gets a neutral label, never the raw string. */
const tc = getI18n().getFixedT('en', 'common');
const ta = getI18n().getFixedT('en', 'assets');

describe('status labels', () => {
  it.each([
    ['approved', 'Approved', 'badge-success'],
    ['needs_changes', 'Needs changes', 'badge-warning'],
    ['rejected', 'Rejected', 'badge-error'],
    ['cleared', 'Not reviewed', 'badge-ghost'],
    ['unreviewed', 'Not reviewed', 'badge-ghost'],
    [null, 'Not reviewed', 'badge-ghost'],
    ['escalated', 'Review status unknown', 'badge-ghost'],
  ])('review %s -> %s', (value, label, badge) => {
    const status = reviewStatus(value);
    expect(tc(status.labelKey)).toBe(label);
    expect(status.badgeClass).toBe(badge);
  });

  it.each([
    ['passed', 'Passed', 'badge-success'],
    ['failed_blocking', 'Failed (blocking)', 'badge-error'],
    ['failed_nonblocking', 'Failed (warning)', 'badge-error'],
    ['uncertain', 'Inconclusive', 'badge-warning'],
    ['skipped', 'Skipped', 'badge-ghost'],
    ['pending', 'Awaiting QC', 'badge-ghost'],
    ['running', 'QC running', 'badge-info'],
    ['vlm_timeout', 'QC status unknown', 'badge-ghost'],
    [null, 'QC status unknown', 'badge-ghost'],
  ])('QC %s -> %s', (value, label, badge) => {
    const status = qcStatus(value);
    expect(tc(status.labelKey)).toBe(label);
    expect(status.badgeClass).toBe(badge);
  });

  it.each([
    ['queued', 'Queued'],
    ['running', 'Generating'],
    ['succeeded', 'Completed'],
    ['partially_succeeded', 'Partly completed'],
    ['failed', 'Failed'],
    ['cancelled', 'Cancelled'],
    ['paused', 'Paused'],
    ['uncertain', 'Awaiting confirmation'],
    ['archived_somehow', 'Status unknown'],
  ])('run %s -> %s', (value, label) => {
    expect(tc(runStatus(value).labelKey)).toBe(label);
  });

  it.each([
    ['byok', 'Real'],
    ['sandbox', 'Sandbox'],
    ['mock', 'Example (controlled Mock)'],
    ['controlled_mock', 'Example (controlled Mock)'],
  ])('execution mode %s -> %s', (mode, label) => {
    expect(tc(executionModeLabelKey(mode))).toBe(label);
  });

  it('labels origins, verdicts and export states with neutral fallbacks', () => {
    expect(tc(originLabelKey('generation'))).toBe('Generated here');
    expect(tc(originLabelKey('upload'))).toBe('External upload');
    expect(tc(originLabelKey('teleported'))).toBe('Origin unknown');
    expect(tc(verdictStatus('winner').labelKey)).toBe('Winner');
    expect(tc(verdictStatus('tie').labelKey)).toBe('Verdict unknown');
    expect(tc(exportPackageStatus('ready').labelKey)).toBe('Ready');
    expect(tc(exportPackageStatus('archived').labelKey)).toBe('Status unknown');
  });

  it('formats a creation time as MM/DD HH:mm, or null when unusable', () => {
    expect(formatRunTime('2026-03-04T05:06:00')).toBe('03/04 05:06');
    expect(formatRunTime('not a date')).toBeNull();
    expect(formatRunTime(null)).toBeNull();
  });
});

describe('run display', () => {
  const plans = [
    {
      plan_id: 'tp_1',
      product_name: ' Grizzl-E Classic ',
      directions: [
        { removed: false },
        { removed: true },
        { removed: false },
      ] as never,
      runs: [
        { stage: 'sample', run_id: 'gr_sample' },
        { stage: 'full', run_id: 'gr_full' },
        { stage: 'full', run_id: null },
      ] as never,
    },
  ];

  it('indexes plan runs by run id with the active direction count', () => {
    const origins = buildRunOrigins(plans);
    expect(origins.get('gr_sample')).toEqual({
      planId: 'tp_1',
      productName: 'Grizzl-E Classic',
      stage: 'sample',
      directionCount: 2,
    });
    expect(origins.get('gr_full')?.stage).toBe('full');
    expect(origins.size).toBe(2);
  });

  it('describes plan and workbench runs without their ids', () => {
    const origins = buildRunOrigins(plans);
    const full = describeRun(
      {
        id: 'gr_full',
        status: 'partially_succeeded',
        done: 7,
        total: 8,
        created_at: '2026-10-12T14:05:00',
      },
      origins.get('gr_full'),
      undefined,
      ta,
      tc
    );
    expect(full.title).toBe('Ad test for Grizzl-E Classic · Full test');
    expect(full.subtitle).toBe('2 directions · 10/12 14:05');
    expect(full.statusLabel).toBe('Partly completed');
    expect(full.ariaLabel).toBe(
      'Ad test for Grizzl-E Classic · Full test, Partly completed, 7/8 done'
    );

    const loading = describeRun({ id: 'gr_x' }, undefined, undefined, ta, tc);
    expect(loading.title).toBe('Loading product');
    expect(loading.subtitle).toBe('Advanced workbench');
    expect(loading.statusLabel).toBe('Status unknown');

    for (const display of [full, loading]) {
      expect(JSON.stringify(display)).not.toMatch(/gr_|tp_/);
    }
  });
});
