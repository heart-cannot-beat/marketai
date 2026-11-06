/** @jest-environment jsdom */
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { SWRConfig } from 'swr';
import type { MarketingQCAsset } from '@/components/workspace/MarketingAIQC';
import { MarketingBoard } from '@/components/workspace/MarketingBoard';
import { MarketingExecution } from '@/components/workspace/MarketingExecution';
import type { MarketingRound } from '@/components/workspace/MarketingExecution';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import boardFixture from '../../fixtures/marketing-board.json';
import fixture from '../../fixtures/marketing-ai-qc.json';
import en from '../../../locales/en/common.json';
import zhCN from '../../../locales/zh-CN/common.json';

/**
 * #299, #300, #308 and #385 in both interface languages, worded by the real
 * locale files: a missing human review is its own step, a check under way is
 * never missing, a round without images says so, and the image review names
 * its checks in the interface language.
 */
let mockMessages: Record<string, string> = en;
jest.mock('next-i18next', () => ({
  useTranslation: () => ({
    i18n: { language: 'en' },
    t: (key: string, options?: Record<string, unknown>) =>
      (mockMessages[key] ?? key).replace(/\{\{(\w+)\}\}/g, (_, name: string) =>
        String(options?.[name] ?? '')
      ),
  }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { workspaceId: 'ws', slug: 'alpha' } }),
}));
const gateway = {
  getMarketingWorkflow: jest.fn(),
  getMarketingAIQCQuote: jest.fn(),
  reviewMarketingCreative: jest.fn(),
  createGenerationAssetVersionDownloadIntent: jest.fn(),
  getGenerationAssetVersion: jest.fn(),
};
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => gateway,
}));
jest.mock('@/lib/domain-client/upload-deps', () => ({
  useBlobTransferClient: () => ({
    downloadBlob: jest.fn(async () => new Blob(['image'])),
  }),
}));

const project =
  fixture.project as components['schemas']['MarketingProjectDetail'];
const ok = (data: unknown) => ({ kind: 'ok', data });

beforeEach(() => {
  jest.clearAllMocks();
  URL.createObjectURL = jest.fn(() => 'blob:qc-states');
  URL.revokeObjectURL = jest.fn();
  gateway.createGenerationAssetVersionDownloadIntent.mockResolvedValue(
    ok({ download_grant: { url: 'http://localhost/private.png' } })
  );
});

/** The first image of the AI QC fixture's round, ready to adjust. */
const firstAsset = () =>
  JSON.parse(JSON.stringify(fixture.row.assets[0])) as MarketingQCAsset;

type Board = components['schemas']['MarketingBoard'];
/** Shows a board fixture, with Route choice diagram 1's paired check in `pairedState`. */
function showBoard(name: 'screen' | 'match', pairedState?: string) {
  const board = JSON.parse(JSON.stringify(boardFixture[name])) as Board;
  if (pairedState)
    for (const angle of board.tree.angles)
      for (const expression of angle.expressions)
        for (const combination of expression.combinations)
          for (const e of combination.executions)
            if (e.asset_version_id === 'v_route_1')
              Object.assign(e, {
                stage: 'paired_qc_running',
                gap: 'paired_qc',
                next_action: 'none',
                paired_ai_qc: {
                  id: 'aqc_paired_route_1',
                  state: pairedState,
                  current: true,
                  quality_suggestion: null,
                },
              });
  render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <MarketingBoard
        board={board}
        productName={boardFixture.product_name}
        selected={null}
        onSelect={() => undefined}
        renderActions={() => ({ primary: null })}
      />
    </SWRConfig>
  );
}

describe.each([
  ['en', en],
  ['zh-CN', zhCN],
] as const)('%s', (locale, messages) => {
  beforeEach(() => {
    mockMessages = messages;
  });

  test('a missing human review is labelled as the review step, never as paired QC (#299)', () => {
    render(
      <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
        <MarketingBoard
          board={boardFixture.screen as components['schemas']['MarketingBoard']}
          productName={boardFixture.product_name}
          selected={null}
          onSelect={() => undefined}
          renderActions={() => ({ primary: null })}
        />
      </SWRConfig>
    );
    const gaps = screen.getAllByText(messages['marketing-board-gap-review']);
    expect(gaps.length).toBeGreaterThan(0);
    for (const gap of gaps) {
      const flow = within(gap.closest('li')!).getByRole('list', {
        name: messages['marketing-board-flow'],
      });
      const missing = flow.querySelector('[data-state="gap"]')!;
      expect(missing).toHaveAttribute('data-segment', 'review');
      expect(missing).toHaveTextContent(
        `${messages['marketing-board-flow-review']}(${messages['marketing-board-flow-gap']})`
      );
      expect(flow.querySelector('[data-segment="paired_qc"]')).toHaveAttribute(
        'data-state',
        'done'
      );
    }
  });

  describe('a step under way (#385)', () => {
    const copy = messages as Record<string, string>;
    const routeChoice = (number: number) =>
      within(screen.getByRole('article', { name: 'One more charging option' }))
        .getByRole('button', {
          name: `Route choice diagram · ${copy['marketing-board-execution'].replace('{{number}}', String(number))}`,
        })
        .closest('li')!;
    const segment = (item: HTMLElement, name: string) =>
      within(item)
        .getByRole('list', { name: copy['marketing-board-flow'] })
        .querySelector(`[data-segment="${name}"]`)!;
    const reads = (name: string, state: 'current' | 'gap') =>
      `${copy[`marketing-board-flow-${name}`]}(${copy[`marketing-board-flow-${state}`]})`;

    test('a running check reads as the current step and its stage, never missing', () => {
      showBoard('screen', 'running');
      const paired = routeChoice(1);
      expect(segment(paired, 'paired_qc')).toHaveTextContent(
        reads('paired_qc', 'current')
      );
      expect(paired).toHaveTextContent(
        copy['marketing-board-stage-paired_qc_running']
      );
      expect(paired).not.toHaveTextContent(copy['marketing-board-flow-gap']);
      expect(paired).not.toHaveTextContent(
        copy['marketing-board-gap-paired_qc']
      );
      // The fixture's single-image check is running.
      const single = routeChoice(2);
      expect(segment(single, 'single_qc')).toHaveTextContent(
        reads('single_qc', 'current')
      );
      expect(single).toHaveTextContent(
        copy['marketing-board-stage-single_qc_running']
      );
      expect(single).not.toHaveTextContent(copy['marketing-board-flow-gap']);
      expect(single).not.toHaveTextContent(
        copy['marketing-board-gap-single_qc']
      );
    });

    test('a placeholder being generated reads as the current step, never missing', () => {
      showBoard('match');
      const generating = screen
        .getByText(copy['marketing-board-generating'])
        .closest('li')!;
      expect(segment(generating, 'generation')).toHaveTextContent(
        reads('generation', 'current')
      );
      expect(generating).not.toHaveTextContent(
        copy['marketing-board-flow-gap']
      );
      expect(generating).not.toHaveTextContent(
        copy['marketing-board-gap-pending_generation']
      );
    });

    test('an uncertain check and a check never run still read as missing', () => {
      showBoard('screen', 'uncertain');
      const uncertain = routeChoice(1);
      expect(segment(uncertain, 'paired_qc')).toHaveTextContent(
        reads('paired_qc', 'gap')
      );
      expect(uncertain).toHaveTextContent(
        copy['marketing-board-gap-paired_qc']
      );
      expect(uncertain).not.toHaveTextContent(
        copy['marketing-board-stage-paired_qc_running']
      );
      const cert = within(
        screen.getByRole('article', { name: 'Safer at high power' })
      )
        .getByRole('button', {
          name: `Certification evidence explained · ${copy['marketing-board-execution'].replace('{{number}}', '1')}`,
        })
        .closest('li')!;
      expect(segment(cert, 'paired_qc')).toHaveTextContent(
        reads('paired_qc', 'gap')
      );
    });
  });

  test('a round without images shows a truthful empty state in both checks (#300)', async () => {
    const current = {
      ...JSON.parse(JSON.stringify(fixture.row)),
      assets: [],
    } as MarketingRound;
    gateway.getMarketingWorkflow.mockResolvedValue(
      ok({
        project_id: project.id,
        current,
        rounds: [],
        next_action: 'review_creatives',
      })
    );
    render(
      <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
        <MarketingExecution project={project} />
      </SWRConfig>
    );
    expect(
      await screen.findAllByText(messages['marketing-ai-qc-no-images'])
    ).toHaveLength(2);
    expect(
      screen.queryByText(
        messages['marketing-ai-qc-unavailable-nothing_to_check']
      )
    ).not.toBeInTheDocument();
    expect(gateway.getMarketingAIQCQuote).not.toHaveBeenCalled();
  });

  describe('check names (#308)', () => {
    const copy = messages as Record<string, string>;
    /** A standard check reads as its copy; a plan check as the plan words it. */
    const nameOf = (asset: MarketingQCAsset, key: string) =>
      key.startsWith('intent_')
        ? asset.required_checks[key]
        : copy[`marketing-ai-qc-check-${key}`];

    beforeEach(() => {
      Object.defineProperty(globalThis.crypto, 'randomUUID', {
        configurable: true,
        value: () => 'qc-states-action',
      });
      gateway.getMarketingAIQCQuote.mockResolvedValue(
        ok({
          ...fixture.quote,
          available: false,
          unavailable_reason: 'nothing_to_check',
        })
      );
    });

    async function openDrawer(asset: MarketingQCAsset) {
      const current = {
        ...JSON.parse(JSON.stringify(fixture.row)),
        assets: [asset],
      } as MarketingRound;
      gateway.getMarketingWorkflow.mockResolvedValue(
        ok({
          project_id: project.id,
          current,
          rounds: [],
          next_action: 'review_creatives',
        })
      );
      gateway.reviewMarketingCreative.mockResolvedValue(
        ok({ id: current.id, data: current })
      );
      render(
        <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
          <MarketingExecution project={project} />
        </SWRConfig>
      );
      fireEvent.click(
        await screen.findByRole('button', {
          name: `使用情境演示 · ${copy['marketing-board-execution'].replace('{{number}}', '1')}`,
        })
      );
      const drawer = await screen.findByRole('dialog', {
        name: copy['marketing-board-drawer-title'],
      });
      fireEvent.click(
        within(drawer).getByText(copy['marketing-ai-qc-adjust-suggestions'])
      );
      fireEvent.click(
        within(drawer).getByText(copy['marketing-ai-qc-view-passing'])
      );
      return drawer;
    }

    test('doubts, passing items and review fields name every check by its id', async () => {
      const asset = firstAsset();
      // A doubt in each kind: the single-image check holds product_fidelity back.
      asset.paired_ai_qc!.result!.findings[0].status = 'fail';
      const drawer = await openDrawer(asset);
      const single = within(drawer).getByRole('region', {
        name: copy['marketing-ai-qc-kind-single'],
      });
      const paired = within(drawer).getByRole('region', {
        name: copy['marketing-ai-qc-kind-paired'],
      });
      for (const key of ['product_fidelity', 'file_valid', 'intent_1'])
        expect(
          within(single).getByText(nameOf(asset, key))
        ).toBeInTheDocument();
      for (const key of [
        'belongs_to_intended_combination',
        'comparison_faithful',
      ])
        expect(
          within(paired).getByText(nameOf(asset, key))
        ).toBeInTheDocument();
      for (const key of Object.keys(asset.required_checks)) {
        const field = within(drawer).getByRole('combobox', {
          name: nameOf(asset, key),
        });
        expect(field.closest('label')).toHaveTextContent(nameOf(asset, key));
      }
      // The fixture's comparison_faithful criterion predates the current one:
      // the name follows the id, never the text the round carries.
      expect(drawer).not.toHaveTextContent(
        asset.required_checks.comparison_faithful
      );
      if (locale === 'zh-CN')
        for (const [key, text] of Object.entries(asset.required_checks))
          if (!key.startsWith('intent_'))
            expect(drawer).not.toHaveTextContent(text);
    });

    test('a check the interface does not know shows the round wording, or its id', async () => {
      const asset = firstAsset();
      asset.required_checks.future_check = 'A check added after this release';
      asset.ai_qc!.result!.findings.push({
        ...asset.ai_qc!.result!.findings[0],
        check_id: 'unlisted_check',
      });
      const drawer = await openDrawer(asset);
      expect(
        within(drawer).getByRole('combobox', {
          name: 'A check added after this release',
        })
      ).toBeInTheDocument();
      expect(
        within(
          within(drawer).getByRole('region', {
            name: copy['marketing-ai-qc-kind-single'],
          })
        ).getByText('unlisted_check')
      ).toBeInTheDocument();
    });

    test('the review is sent keyed by check id, whatever the names read', async () => {
      const asset = firstAsset();
      const drawer = await openDrawer(asset);
      for (const key of Object.keys(asset.required_checks))
        fireEvent.change(
          within(drawer).getByRole('combobox', { name: nameOf(asset, key) }),
          { target: { value: 'pass' } }
        );
      fireEvent.change(
        within(drawer).getByLabelText(copy['marketing-ai-qc-override-note']),
        { target: { value: 'Compared with the physical product.' } }
      );
      fireEvent.change(
        within(drawer).getByLabelText(copy['marketing-review-note']),
        { target: { value: 'Checked the exact image.' } }
      );
      fireEvent.click(
        within(drawer).getByRole('button', {
          name: copy['marketing-ai-review-approved'],
        })
      );
      await waitFor(() =>
        expect(gateway.reviewMarketingCreative).toHaveBeenCalled()
      );
      expect(
        gateway.reviewMarketingCreative.mock.calls[0][0].body.checks
      ).toEqual(
        Object.fromEntries(
          Object.keys(asset.required_checks).map((key) => [key, 'pass'])
        )
      );
    });
  });
});
