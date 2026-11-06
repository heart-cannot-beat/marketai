/**
 * @jest-environment jsdom
 */
import { act, fireEvent, render, screen } from '@testing-library/react';
import { ResultsImportDialog } from '@/components/learnings/ResultsImportDialog';
import { importView, loadLearningsNamespace } from './fixtures';

const mockGateway = {
  createResultsImport: jest.fn(),
  commitResultsImport: jest.fn(),
  getResultsImport: jest.fn(),
  getResultsTemplate: jest.fn(),
  pollOperation: jest.fn(),
};
const mockDownload = jest.fn();

jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => mockGateway,
}));
jest.mock('@/hooks/domain/useUpload', () => ({
  useUpload: () => ({ startUpload: jest.fn(), state: 'idle', hashProgress: 0 }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme', role: 'MEMBER' } }),
}));
jest.mock('@/lib/learnings/sheet', () => ({
  ...jest.requireActual('@/lib/learnings/sheet'),
  downloadText: (...args: unknown[]) => mockDownload(...args),
}));

beforeAll(loadLearningsNamespace);
beforeEach(() => {
  Object.values(mockGateway).forEach((fn) => fn.mockReset());
  mockDownload.mockReset();
});

const PASTE =
  '素材编号\t开始日期\t结束日期\t展示次数\t链接点击量\nMK-7F3K2Q\t2026-09-01\t2026-09-07\t1000\t20';

describe('ResultsImportDialog', () => {
  it('previews pasted rows, explains the refusals, then imports on confirm', async () => {
    mockGateway.createResultsImport.mockResolvedValue({
      kind: 'created',
      status: 201,
      data: importView(),
      meta: {},
    });
    mockGateway.commitResultsImport.mockResolvedValue({
      kind: 'ok',
      status: 200,
      data: importView({ status: 'committed', snapshot_ids: ['lrn_1'] }),
      meta: {},
    });
    const onCommitted = jest.fn();
    render(
      <ResultsImportDialog
        productId="prod_1"
        open
        onClose={jest.fn()}
        onCommitted={onCommitted}
      />
    );

    fireEvent.change(
      screen.getByLabelText('Pasted table (the first row is the header)'),
      {
        target: { value: PASTE },
      }
    );
    await act(async () => {
      fireEvent.click(
        screen.getByRole('button', { name: 'Check the pasted data' })
      );
    });
    expect(mockGateway.createResultsImport).toHaveBeenCalledWith(
      expect.objectContaining({
        workspaceSlug: 'acme',
        body: {
          product_id: 'prod_1',
          source: 'csv',
          csv_text: PASTE,
          currency: 'USD',
        },
      })
    );
    expect(
      screen.getByText('3 rows: 2 can be imported, 1 will not be imported.')
    ).toBeInTheDocument();
    // Worded from the code's error-<code> copy (#253, #255), never the stored Chinese sentence.
    expect(
      screen.getByText('1 rows: This creative code was not found.')
    ).toBeInTheDocument();
    expect(screen.queryByText(/找不到/)).not.toBeInTheDocument();
    expect(screen.queryByText(/av_1|prod_1/)).not.toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Import 2 rows' }));
    });
    expect(mockGateway.commitResultsImport).toHaveBeenCalledWith(
      expect.objectContaining({ importId: 'rimp_1' })
    );
    expect(onCommitted).toHaveBeenCalled();
    expect(
      screen.getByText('Imported 2 rows; the conclusions are updated.')
    ).toBeInTheDocument();
  });

  it('words each row from its code in the error table, not the stored Chinese text', async () => {
    const view = importView();
    view.summary.rejected_reasons = [
      {
        code: 'currency_mismatch',
        reason: '币种和这个产品已导入的数据不一致',
        count: 1,
      },
    ];
    view.rows = [
      {
        ...view.rows[1],
        reason_code: 'currency_mismatch',
        reason: '币种和这个产品已导入的数据不一致',
        reason_params: { expected: 'USD', found: 'EUR' },
      },
      {
        ...view.rows[0],
        line: 5,
        status: 'unattributable',
        reason_code: 'external_untagged',
        reason: '外部素材还没有打标签，打标后才能按元素归因',
        description: '外部素材',
        description_code: 'external_asset',
      },
    ] as unknown as typeof view.rows;
    mockGateway.createResultsImport.mockResolvedValue({
      kind: 'created',
      status: 201,
      data: view,
      meta: {},
    });
    render(<ResultsImportDialog productId="prod_1" open onClose={jest.fn()} />);
    fireEvent.change(
      screen.getByLabelText('Pasted table (the first row is the header)'),
      { target: { value: PASTE } }
    );
    await act(async () => {
      fireEvent.click(
        screen.getByRole('button', { name: 'Check the pasted data' })
      );
    });
    expect(
      screen.getByText(
        '1 rows: The currency differs from data already imported for this product.'
      )
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        'The currency differs from data already imported for this product.'
      )
    ).toBeInTheDocument();
    expect(screen.getByText('External asset')).toBeInTheDocument();
    expect(screen.queryByText(/币种|外部素材/)).not.toBeInTheDocument();
  });

  it('says when the same data was already imported', async () => {
    mockGateway.createResultsImport.mockResolvedValue({
      kind: 'created',
      status: 201,
      data: importView({ status: 'committed', duplicate: true }),
      meta: {},
    });
    render(<ResultsImportDialog productId="prod_1" open onClose={jest.fn()} />);
    fireEvent.change(
      screen.getByLabelText('Pasted table (the first row is the header)'),
      {
        target: { value: PASTE },
      }
    );
    await act(async () => {
      fireEvent.click(
        screen.getByRole('button', { name: 'Check the pasted data' })
      );
    });
    expect(
      screen.getByText(
        'This data was imported before and will not be counted twice.'
      )
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /确认导入/ })
    ).not.toBeInTheDocument();
  });

  it('explains a refused sheet by its code, not the server text', async () => {
    mockGateway.createResultsImport.mockResolvedValue({
      kind: 'problem',
      status: 422,
      problem: {
        code: 'missing_identifier_column',
        detail: '表头里需要「素材编号」「广告名称」或「广告编号」其中一列',
      },
      meta: {},
    });
    render(<ResultsImportDialog productId="prod_1" open onClose={jest.fn()} />);
    fireEvent.change(
      screen.getByLabelText('Pasted table (the first row is the header)'),
      {
        target: { value: 'a,b\n1,2' },
      }
    );
    await act(async () => {
      fireEvent.click(
        screen.getByRole('button', { name: 'Check the pasted data' })
      );
    });
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Not completed: The header needs an asset ID, ad name or ad ID column.'
    );
    expect(screen.getByRole('alert')).not.toHaveTextContent('素材编号');
  });

  it('a stale preview is refreshed and must be confirmed again', async () => {
    mockGateway.createResultsImport.mockResolvedValue({
      kind: 'created',
      status: 201,
      data: importView(),
      meta: {},
    });
    mockGateway.commitResultsImport.mockResolvedValue({
      kind: 'problem',
      status: 409,
      problem: { code: 'preview_stale', detail: 'stale' },
      meta: {},
    });
    mockGateway.getResultsImport.mockResolvedValue({
      kind: 'ok',
      status: 200,
      data: importView(),
      meta: {},
    });
    render(<ResultsImportDialog productId="prod_1" open onClose={jest.fn()} />);
    fireEvent.change(
      screen.getByLabelText('Pasted table (the first row is the header)'),
      {
        target: { value: PASTE },
      }
    );
    await act(async () => {
      fireEvent.click(
        screen.getByRole('button', { name: 'Check the pasted data' })
      );
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Import 2 rows' }));
    });
    expect(mockGateway.getResultsImport).toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent('checked again');
    expect(
      screen.getByRole('button', { name: 'Import 2 rows' })
    ).toBeInTheDocument();
  });

  it('downloads a template listing every image code', async () => {
    mockGateway.getResultsTemplate.mockResolvedValue({
      kind: 'ok',
      status: 200,
      meta: {},
      data: {
        product_id: 'prod_1',
        file_name: 'VoltNest-投放结果模板.csv',
        currency: null,
        columns: [
          { key: 'asset_code', label: '素材编号' },
          { key: 'direction', label: '方向' },
          { key: 'impressions', label: '展示次数' },
        ],
        rows: [
          {
            asset_code: 'MK-7F3K2Q',
            round_number: 1,
            direction: '主打快充',
            combination: 'x',
            attributable: true,
            evidence_mode: 'production',
          },
        ],
      },
    });
    render(<ResultsImportDialog productId="prod_1" open onClose={jest.fn()} />);
    await act(async () => {
      fireEvent.click(
        screen.getByRole('button', {
          name: 'Download the template (with every creative code of this product)',
        })
      );
    });
    expect(mockDownload).toHaveBeenCalledWith(
      'VoltNest-投放结果模板.csv',
      '\ufeff素材编号,方向,展示次数\r\nMK-7F3K2Q,主打快充,\r\n'
    );
  });
});
