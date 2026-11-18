/**
 * @jest-environment jsdom
 *
 * A draft of ads-first ideas does not leave through the creative library
 * (Issue #219). The library still shows it; the pages mark it, do not offer
 * to export it, and say in plain words which ads an export refused and why.
 */
import { render, screen, within } from '@testing-library/react';
import zhAssets from '../../locales/zh-CN/assets.json';
import enAssets from '../../locales/en/assets.json';

const mockUseAssetImageUrl = jest.fn();
jest.mock('@/hooks/assets/useAssetImageUrl', () => ({
  useAssetImageUrl: (...args: unknown[]) => mockUseAssetImageUrl(...args),
}));

const mockSelected = new Set<string>();
jest.mock('@/components/assets/AssetsProvider', () => ({
  useAssets: () => ({
    selection: {
      isSelected: (id: string) => mockSelected.has(id),
      toggle: jest.fn(),
      selectedIds: mockSelected,
      count: mockSelected.size,
    },
    urlState: { filters: {}, setExportId: jest.fn() },
  }),
}));

const mockFlow = jest.fn();
jest.mock('@/hooks/assets/useExportFlow', () => ({
  useExportFlow: () => mockFlow(),
}));

const mockDownloadIntent = jest.fn();
jest.mock('@/hooks/domain/useDomainCommand', () => ({
  useDomainCommand: () => mockDownloadIntent(),
}));

jest.mock('@/hooks/domain/useGrantedDownload', () => ({
  useGrantedDownload: () => ({ startDownload: jest.fn() }),
}));

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { slug: 'acme' } }),
}));

jest.mock('@/components/assets/ExportManifestView', () => ({
  ExportManifestView: () => <div data-testid="manifest" />,
}));

import { AssetCard } from '@/components/assets/AssetCard';
import { AssetGrid } from '@/components/assets/AssetGrid';
import { ExportFlow } from '@/components/assets/ExportFlow';
import { SelectionBar } from '@/components/assets/SelectionBar';
import {
  exportBlockReasons,
  exportRefusal,
  type AssetExportBlock,
} from '@/lib/assets/export-block';

const reason = (code: string, fact_ids: string[] = []) => ({
  code,
  fact_ids,
  finding_ids: [],
});

const draft: AssetExportBlock = {
  status: 'draft',
  reasons: [
    reason('facts_unverified', ['f1']),
    reason('appearance_unconfirmed'),
    reason('findings_open'),
  ],
};
const stale: AssetExportBlock = {
  status: 'needs_redo',
  reasons: [reason('facts_changed', ['f1']), reason('check_outdated')],
};

function summary(overrides: Record<string, unknown> = {}) {
  return {
    id: 'av-1',
    asset_id: 'a1',
    product_id: 'p1',
    run_id: 'r1',
    provider: 'openai',
    model: 'gpt-image-2',
    status: 'ok',
    aspect_ratio: '1:1',
    stable_dimension_signature: '',
    tags: [],
    effective_review_value: null,
    latest_qc_state: null,
    contextual_conclusion: null,
    origin_kind: 'generation',
    execution_mode: 'byok',
    mock_evidence: null,
    byte_size: 1024,
    thumbnail: null,
    created_at: '2026-11-18T00:00:00Z',
    ...overrides,
  } as any;
}

function refusal(ads: unknown[], count = ads.length) {
  return {
    type: '',
    title: 'ads_not_exportable',
    status: 409,
    detail: '',
    instance: '',
    code: 'ads_not_exportable',
    trace_id: '',
    retryable: false,
    field_errors: [],
    params: { ads, count },
  };
}

function flow(overrides: Record<string, unknown> = {}) {
  return {
    step: 'configure',
    packageId: undefined,
    operationId: undefined,
    packageDetail: undefined,
    manifest: undefined,
    problem: undefined,
    isExporting: false,
    startExport: jest.fn(),
    reset: jest.fn(),
    ...overrides,
  };
}

function command(problem?: unknown) {
  return {
    execute: jest.fn().mockResolvedValue({ kind: 'problem', problem }),
    result: undefined,
    problem,
    isPending: false,
    reset: jest.fn(),
    retry: jest.fn(),
    reconciliation: undefined,
  };
}

beforeEach(() => {
  mockSelected.clear();
  mockUseAssetImageUrl.mockReset();
  mockUseAssetImageUrl.mockReturnValue({
    blobUrl: 'blob:image',
    isLoading: false,
    problem: undefined,
    source: 'original',
  });
  mockFlow.mockReset();
  mockFlow.mockReturnValue(flow());
  mockDownloadIntent.mockReset();
  mockDownloadIntent.mockReturnValue(command());
});

describe('the reasons an ad may not be exported', () => {
  const t = (key: string) => (enAssets as Record<string, string>)[key] ?? key;

  it('reads each reason once, in plain words and in the order given', () => {
    expect(
      exportBlockReasons(t, {
        reasons: [
          reason('facts_unverified', ['f1']),
          reason('facts_unverified', ['f2']),
          reason('findings_open'),
        ],
      })
    ).toEqual([
      "A fact it cites hasn't been checked",
      "Findings of its check haven't been handled",
    ]);
  });

  it('never shows a reason code it has no sentence for', () => {
    expect(
      exportBlockReasons(t, { reasons: [reason('a_reason_added_later')] })
    ).toEqual(["Something it relies on hasn't been checked"]);
  });

  it('has a sentence in both languages for every reason the server gives', () => {
    for (const code of [
      'ideas_changed',
      'facts_changed',
      'facts_removed',
      'photo_unavailable',
      'image_blocked',
      'facts_unverified',
      'photo_unverified',
      'appearance_unconfirmed',
      'image_check_in_progress',
      'check_missing',
      'check_in_progress',
      'check_failed',
      'check_uncertain',
      'findings_open',
      'check_outdated',
      'other',
    ]) {
      const key = `export-block-reason-${code}`;
      expect((enAssets as Record<string, string>)[key]).toBeTruthy();
      expect((zhAssets as Record<string, string>)[key]).toBeTruthy();
    }
  });

  it('reads the refused ads only from an ads_not_exportable refusal', () => {
    const ads = [{ asset_version_id: 'av-1', ...draft }];
    expect(exportRefusal(refusal(ads, 7))).toEqual({ ads, count: 7 });
    expect(exportRefusal({ code: 'empty_selection' })).toBeNull();
    expect(exportRefusal(undefined)).toBeNull();
  });
});

describe('the gallery', () => {
  const card = (exportBlock?: AssetExportBlock | null) => (
    <AssetCard
      versionId="av-1"
      displayNumber={1}
      aspectRatio="1:1"
      provider="openai"
      effectiveReview={null}
      executionMode="byok"
      exportBlock={exportBlock}
      isSelected={false}
      onSelect={jest.fn()}
      onOpen={jest.fn()}
    />
  );

  it('shows a draft like any image and says it cannot be exported yet', () => {
    render(card(draft));
    // The card's image has an empty alt (the button names it), so it is
    // found as an element rather than by role.
    expect(document.querySelector('img')).toHaveAttribute('src', 'blob:image');
    const badge = screen.getByTestId('asset-export-block');
    expect(badge).toHaveTextContent("Ad draft · can't be exported yet");
    expect(badge).toHaveAttribute(
      'title',
      "A fact it cites hasn't been checked; The product's look in the image hasn't been confirmed; Findings of its check haven't been handled"
    );
    expect(screen.getByTestId('asset-select')).toBeEnabled();
  });

  it('says so for an ad that needs redoing', () => {
    render(card(stale));
    expect(screen.getByTestId('asset-export-block')).toHaveTextContent(
      "Needs redoing · can't be exported"
    );
  });

  it('marks nothing on any other image', () => {
    render(card(null));
    expect(screen.queryByTestId('asset-export-block')).toBeNull();
  });

  it('marks the tiles the list says are blocked', () => {
    render(
      <AssetGrid
        items={[
          summary({ id: 'av-plain' }),
          summary({ id: 'av-draft', export_block: draft }),
        ]}
        hasMore={false}
        isLoading={false}
        isLoadingMore={false}
        onLoadMore={jest.fn()}
        onOpenVersion={jest.fn()}
      />
    );
    const cards = screen.getAllByTestId('asset-card');
    expect(within(cards[0]).queryByTestId('asset-export-block')).toBeNull();
    expect(
      within(cards[1]).getByTestId('asset-export-block')
    ).toBeInTheDocument();
  });
});

describe('the selection bar', () => {
  const bar = (blockedCount?: number) => (
    <SelectionBar
      count={3}
      onClear={jest.fn()}
      onExport={jest.fn()}
      onReviewMode={jest.fn()}
      canExport={true}
      canReview={true}
      blockedCount={blockedCount}
    />
  );

  it('does not offer export while a selected ad may not be exported, and says why', () => {
    render(bar(2));
    const exportButton = screen.getByTestId('selection-export');
    expect(exportButton).toBeDisabled();
    expect(exportButton).toHaveAccessibleDescription(
      "2 of the selected images are ads that can't be exported yet. Deselect them, or export after checking them."
    );
    // Reviewing a draft is still possible.
    expect(screen.getByTestId('selection-review')).toBeEnabled();
  });

  it('offers export as before for any other selection', () => {
    render(bar());
    expect(screen.getByTestId('selection-export')).toBeEnabled();
    expect(screen.queryByTestId('selection-export-blocked')).toBeNull();
  });
});

describe('the export dialog', () => {
  it('lists each refused ad by its image, what it is and why', () => {
    mockFlow.mockReturnValue(
      flow({
        problem: refusal(
          [
            { asset_version_id: 'av-draft', ...draft },
            { asset_version_id: 'av-stale', ...stale },
          ],
          9
        ),
      })
    );
    render(<ExportFlow onClose={jest.fn()} />);

    const refused = screen.getByTestId('export-refused');
    expect(refused).toHaveTextContent(
      "The ads below can't be exported yet, so no export package was made."
    );
    const rows = within(refused).getAllByTestId('export-refused-ad');
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent('Ad draft');
    expect(rows[0]).toHaveTextContent("A fact it cites hasn't been checked");
    expect(rows[1]).toHaveTextContent('Ad that needs redoing');
    expect(rows[1]).toHaveTextContent(
      'A fact it cites was changed after the image was made'
    );
    expect(refused).toHaveTextContent("7 more aren't listed.");
    // The ads are named by their image, never by an id.
    expect(refused).not.toHaveTextContent('av-');
    expect(mockUseAssetImageUrl).toHaveBeenCalledWith('av-draft');
    expect(
      within(rows[0]).getByAltText("An ad that can't be exported yet")
    ).toHaveAttribute('src', 'blob:image');
    expect(screen.getByTestId('export-start')).toBeEnabled();
  });

  it('keeps the plain message for any other refusal', () => {
    mockFlow.mockReturnValue(
      flow({ problem: { ...refusal([]), code: 'mixed_execution_modes' } })
    );
    render(<ExportFlow onClose={jest.fn()} />);
    expect(screen.queryByTestId('export-refused')).toBeNull();
    // jsdom leaves a <dialog>'s content out of the accessibility tree.
    expect(screen.getByRole('alert', { hidden: true })).toHaveTextContent(
      'Export real and controlled-mock images separately.'
    );
  });

  const ready = (leftOut: number) =>
    flow({
      step: 'ready',
      packageId: 'xp-1',
      manifest: { package_id: 'xp-1', entries: [], total_versions: 2 },
      packageDetail: {
        package_id: 'xp-1',
        state: 'ready',
        left_out_drafts: leftOut,
      },
    });

  it('says how many drafts a preset left out of the package', () => {
    mockFlow.mockReturnValue(ready(4));
    render(<ExportFlow packageId="xp-1" onClose={jest.fn()} />);
    expect(screen.getByTestId('export-left-out')).toHaveTextContent(
      "4 ads that can't be exported yet were left out of this package."
    );
  });

  it('says nothing about drafts for a package that left none out', () => {
    mockFlow.mockReturnValue(ready(0));
    render(<ExportFlow packageId="xp-1" onClose={jest.fn()} />);
    expect(screen.queryByTestId('export-left-out')).toBeNull();
    expect(screen.queryByTestId('export-refused')).toBeNull();
  });

  it('explains a package that may no longer be downloaded', () => {
    mockFlow.mockReturnValue(ready(0));
    mockDownloadIntent.mockReturnValue(
      command(refusal([{ asset_version_id: 'av-stale', ...stale }]))
    );
    render(<ExportFlow packageId="xp-1" onClose={jest.fn()} />);
    const refused = screen.getByTestId('export-refused');
    expect(refused).toHaveTextContent(
      'After this package was built, an ad in it stopped being exportable, so it can no longer be downloaded. Please make a new export package.'
    );
    expect(within(refused).getByTestId('export-refused-ad')).toHaveTextContent(
      'Ad that needs redoing'
    );
  });
});
