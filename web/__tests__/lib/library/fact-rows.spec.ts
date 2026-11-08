import {
  factIdsOf,
  groupByFactRow,
  storedBlockingFindings,
} from '@/lib/library/fact-rows';

const finding = (code: string, path: string) => ({ code, message: '', path });

describe('groupByFactRow (#23)', () => {
  it('groups findings by fact row in row order, keeping the server order in a row', () => {
    const findings = [
      finding('fact_valid_range', 'facts[10].valid_to'),
      finding('dependency_not_found', 'data_source_entry_id'),
      finding('fact_value_missing', 'facts[2].currency'),
      finding('fact_shape', 'facts[3]'),
      finding('fact_amount_parse', 'facts[2].amount'),
      finding('fact_set_empty', 'facts'),
    ];

    expect(groupByFactRow(findings)).toEqual({
      general: [findings[1], findings[5]],
      rows: [
        {
          index: 2,
          findings: [
            { field: 'currency', finding: findings[2] },
            { field: 'amount', finding: findings[4] },
          ],
        },
        { index: 3, findings: [{ field: '', finding: findings[3] }] },
        { index: 10, findings: [{ field: 'valid_to', finding: findings[0] }] },
      ],
    });
  });

  it('keeps a path that only looks like a row with the fact set', () => {
    const findings = [
      finding('x', 'facts[a].currency'),
      finding('x', 'other_facts[0].currency'),
      finding('x', ''),
    ];
    expect(groupByFactRow(findings)).toEqual({ general: findings, rows: [] });
  });
});

describe('factIdsOf', () => {
  it('reads each fact_id by index', () => {
    expect(
      factIdsOf({
        facts: [{ fact_id: 'price-us' }, { fact_id: '  ' }, 'not a fact', {}],
      })
    ).toEqual(['price-us', null, null, null]);
  });

  it('reads nothing from content without a fact list', () => {
    expect(factIdsOf(null)).toEqual([]);
    expect(factIdsOf({ facts: 'x' })).toEqual([]);
  });
});

describe('storedBlockingFindings', () => {
  it('reads code, message and path of each blocking finding', () => {
    expect(
      storedBlockingFindings({
        blocking: [
          {
            code: 'fact_currency',
            message: 'm',
            path: 'facts[0].currency',
            severity: 'blocking',
          },
          { code: 7 },
        ],
        warnings: [{ code: 'w', message: '', path: '' }],
      })
    ).toEqual([
      { code: 'fact_currency', message: 'm', path: 'facts[0].currency' },
      { code: '', message: '', path: '' },
    ]);
  });

  it('reads nothing from a value that is no report', () => {
    expect(storedBlockingFindings(null)).toEqual([]);
    expect(storedBlockingFindings({ blocking: 'x' })).toEqual([]);
  });
});
