import {
  betterIsRight,
  formatLift,
  liftInterval,
  sharedDomain,
} from '@/lib/learnings/format';
import { buildTemplateCsv, decodeSheet } from '@/lib/learnings/sheet';
import { position } from '@/components/learnings/LiftStrip';

describe('learnings formatting', () => {
  it('formats lifts like the server sentences', () => {
    expect(formatLift('0.38')).toBe('+38%');
    expect(formatLift('-0.123')).toBe('-12%');
    expect(formatLift('0.004')).toBe('+0.4%');
    expect(formatLift('0')).toBe('0%');
    expect(formatLift(null)).toBe('—');
  });

  it('flips CPA so that right always means better', () => {
    const cpa = { point: -0.25, low: -0.4, high: -0.1 };
    expect(betterIsRight(cpa, 'cpa')).toEqual({
      point: 0.25,
      low: 0.1,
      high: 0.4,
    });
    expect(betterIsRight(cpa, 'ctr')).toBe(cpa);
    expect(
      betterIsRight({ point: 0.2, low: -0.1, high: null }, 'cpa').low
    ).toBe(-10);
  });

  it('shares one readable, symmetric domain per factor', () => {
    expect(sharedDomain([])).toBe(0.2);
    expect(sharedDomain([{ point: 0.3, low: 0.1, high: 0.45 }])).toBe(0.5);
    expect(sharedDomain([{ point: 0.8, low: 0.6, high: 1.2 }])).toBe(1.5);
    expect(sharedDomain([{ point: 3, low: 2, high: null }])).toBe(2);
    expect(position(0, 0.5)).toBe(50);
    expect(position(5, 0.5)).toBe(100);
  });

  it('reads an interval only when the server gave one', () => {
    const base = { lift: null, lift_low: null, lift_high: null } as any;
    expect(liftInterval(base)).toBeNull();
    expect(
      liftInterval({ ...base, lift: '0.1', lift_low: '0.05', lift_high: null })
    ).toEqual({
      point: 0.1,
      low: 0.05,
      high: null,
    });
  });
});

describe('results sheets', () => {
  it('escapes cells and starts with a BOM for Excel', () => {
    const csv = buildTemplateCsv({
      product_id: 'p',
      file_name: 'f.csv',
      currency: 'USD',
      columns: [
        { key: 'asset_code', label: '素材编号' },
        { key: 'combination', label: '画面组合' },
        { key: 'currency', label: '币种' },
      ],
      rows: [
        {
          asset_code: 'MK-7F3K2Q',
          round_number: 1,
          direction: 'd',
          combination: '居家, "真人"',
          attributable: true,
          evidence_mode: 'production',
        },
      ],
    });
    expect(csv).toBe(
      '\ufeff素材编号,画面组合,币种\r\nMK-7F3K2Q,"居家, ""真人""",USD\r\n'
    );
  });

  it('decodes UTF-8 and the GB18030 Chinese Excel writes', () => {
    const utf8 = new TextEncoder().encode(
      '\ufeff素材编号,展示次数\nMK-7F3K2Q,1'
    );
    expect(decodeSheet(utf8.buffer as ArrayBuffer)).toBe(
      '素材编号,展示次数\nMK-7F3K2Q,1'
    );
    // "素材" in GB18030.
    const gbk = new Uint8Array([0xcb, 0xd8, 0xb2, 0xc4, 0x2c, 0x31]);
    expect(decodeSheet(gbk.buffer)).toBe('素材,1');
  });
});
