/** @jest-environment jsdom */
import fs from 'node:fs';
import path from 'node:path';
import { useState } from 'react';
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { SWRConfig } from 'swr';
import {
  MarketingBoard,
  type BoardExecution,
} from '@/components/workspace/MarketingBoard';
import type { components } from '@/lib/domain-client/generated/domain-v1';
// Produced by the server projection (tests/test_marketing_board_web_fixtures.py).
import fixture from '../../fixtures/marketing-board.json';
import messages from '../../../locales/en/common.json';

jest.mock('next-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, unknown>) =>
      ((messages as Record<string, string>)[key] ?? key).replace(
        /\{\{(\w+)\}\}/g,
        (_: string, name: string) => String(options?.[name] ?? '')
      ),
  }),
}));
jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { workspaceId: 'ws', slug: 'alpha' } }),
}));
const gateway = {
  createGenerationAssetVersionDownloadIntent: jest.fn(),
  getGenerationAssetVersion: jest.fn(),
};
jest.mock('@/lib/domain-client/gateway-provider', () => ({
  useDomainGateway: () => gateway,
}));
const blobs = { downloadBlob: jest.fn() };
jest.mock('@/lib/domain-client/upload-deps', () => ({
  useBlobTransferClient: () => blobs,
}));

type Board = components['schemas']['MarketingBoard'];
const screenBoard = fixture.screen as Board;
const matchBoard = fixture.match as Board;
const renderActions = jest.fn((execution: BoardExecution) => ({
  primary: <button type="button">{`Primary ${execution.next_action}`}</button>,
  secondary: <p>{`Secondary tools`}</p>,
}));

function Harness({ board }: { board: Board }) {
  const [selected, setSelected] = useState<string | null>(null);
  return (
    <MarketingBoard
      board={board}
      productName={fixture.product_name}
      selected={selected}
      onSelect={setSelected}
      renderActions={renderActions}
    />
  );
}
function show(board = screenBoard) {
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <Harness board={board} />
    </SWRConfig>
  );
}
function card(angle: string) {
  return screen.getByRole('article', { name: angle });
}
function tile(angle: string, name: string) {
  return within(card(angle)).getByRole('button', { name });
}
function flowOf(element: HTMLElement) {
  const list = within(element.closest('li')!).getByRole('list', {
    name: 'Progress',
  });
  return Object.fromEntries(
    within(list)
      .getAllByRole('listitem')
      .map((item) => [item.dataset.segment, item.dataset.state])
  );
}
function details(summary: string) {
  const element = screen.getByText(summary).closest('details')!;
  element.open = true;
  fireEvent(element, new Event('toggle'));
  return element;
}

beforeEach(() => {
  jest.clearAllMocks();
  URL.createObjectURL = jest.fn(() => 'blob:board');
  URL.revokeObjectURL = jest.fn();
  gateway.createGenerationAssetVersionDownloadIntent.mockImplementation(
    async ({ versionId }: { versionId: string }) => ({
      kind: 'created',
      data: { download_grant: { url: 'https://private.test/' + versionId } },
    })
  );
  gateway.getGenerationAssetVersion.mockResolvedValue({
    kind: 'ok',
    data: {
      provider: 'mock',
      model: 'mock-image-v1',
      content_sha256: 'c'.repeat(64),
      prompt: 'Proposition: One more charging option',
    },
  });
  blobs.downloadBlob.mockResolvedValue(new Blob(['image']));
});

test('product appearance pending verification is stated for the round and for each image', async () => {
  show();
  expect(
    screen.getByText(
      'Product appearance is pending verification for 5 combinations without a current checked product photo.'
    )
  ).toBeInTheDocument();
  const angle = 'One more charging option';
  fireEvent.click(tile(angle, 'Route choice diagram · Execution 1'));
  const backed = await screen.findByRole('dialog');
  expect(backed).not.toHaveTextContent(
    'Product appearance pending verification'
  );
  fireEvent.keyDown(document, { key: 'Escape' });
  fireEvent.click(tile(angle, 'Route choice diagram · Execution 2'));
  expect(await screen.findByRole('dialog')).toHaveTextContent(
    'Product appearance pending verification: no checked product photo was used for this image.'
  );
});

test('without the execution plan a revision still reads as revised', () => {
  const board = JSON.parse(JSON.stringify(screenBoard)) as Board;
  for (const angle of board.tree.angles)
    for (const expression of angle.expressions)
      for (const combination of expression.combinations)
        for (const execution of combination.executions)
          delete execution.execution_version;
  show(board);
  expect(
    tile('Safer at high power', 'Live connection demo · Execution 1 · revised')
  ).toBeInTheDocument();
});

test('an angle card does not repeat a group named after its angle', () => {
  const board = JSON.parse(JSON.stringify(screenBoard)) as Board;
  for (const expression of board.tree.angles[0].expressions)
    for (const combination of expression.combinations)
      combination.group_name = board.tree.angles[0].proposition;
  show(board);
  expect(
    within(card('One more charging option')).getByRole('heading', { level: 4 })
  ).toHaveTextContent(/^One more charging option$/);
});

test('the header states the server question, main comparison and supporting observation', () => {
  show();
  expect(
    screen.getByRole('heading', {
      name: 'Which buying proposition is worth exploring further?',
    })
  ).toBeInTheDocument();
  expect(screen.getByText('Explore buying propositions')).toBeInTheDocument();
  expect(
    screen.getByText('Main comparison: Group A ↔ Group B')
  ).toBeInTheDocument();
  expect(
    screen.getByText(
      'Within groups: expression coverage, a supporting observation'
    )
  ).toBeInTheDocument();
});

test('images are grouped by angle, then expression, with readable execution marks', async () => {
  show();
  expect(screen.getAllByRole('article')).toHaveLength(2);
  const a = card('One more charging option');
  expect(
    within(a).getByRole('heading', {
      name: 'Group A · One more charging option',
    })
  ).toBeInTheDocument();
  expect(within(a).getByText('Expressions: 3')).toBeInTheDocument();
  const route = within(a)
    .getByRole('heading', { name: 'Route choice diagram' })
    .closest('li')!;
  expect(within(route).getByText('Executions: 2')).toBeInTheDocument();
  expect(
    within(route).getByRole('button', {
      name: 'Route choice diagram · Execution 2',
    })
  ).toBeInTheDocument();
  const b = card('Safer at high power');
  expect(within(b).getByText('Shared across angles')).toBeInTheDocument();
  // A revision keeps its execution's number with the next version.
  expect(
    within(b).getByRole('button', {
      name: 'Live connection demo · Execution 1 v2',
    })
  ).toBeInTheDocument();
  expect(
    within(b).getByText(
      (_, node) =>
        node?.tagName === 'H5' &&
        node.textContent === 'Live connection demo' &&
        node.closest('li')!.textContent!.includes('Executions: 1')
    )
  ).toBeInTheDocument();
  expect(await screen.findAllByRole('img')).toHaveLength(7);
});

test('each image shows its five-step flow with the current step and the gap from the server', () => {
  show();
  const angle = 'One more charging option';
  // Single-image QC done: the comparison check is the gap.
  expect(flowOf(tile(angle, 'Route choice diagram · Execution 1'))).toEqual({
    generation: 'done',
    single_qc: 'done',
    paired_qc: 'gap',
    review: 'todo',
    delivery: 'todo',
  });
  // Single-image QC still running: the step is under way, not missing (#385).
  const running = tile(angle, 'Route choice diagram · Execution 2');
  expect(flowOf(running)).toEqual({
    generation: 'done',
    single_qc: 'current',
    paired_qc: 'todo',
    review: 'todo',
    delivery: 'todo',
  });
  expect(
    within(running.closest('li')!).getByText('Single-image QC running')
  ).toHaveClass('text-gray-600');
  expect(running.closest('li')).not.toHaveTextContent(
    'Single-image QC not done'
  );
  expect(flowOf(tile(angle, 'Finding a charger story · Execution 1'))).toEqual({
    generation: 'gap',
    single_qc: 'todo',
    paired_qc: 'todo',
    review: 'todo',
    delivery: 'todo',
  });
  const placeholder = tile(angle, 'Finding a charger story · Execution 1');
  expect(placeholder).toHaveTextContent('Not generated');
  expect(within(placeholder).queryByRole('img')).not.toBeInTheDocument();
  expect(
    within(placeholder.closest('li')!).getByText('Image not generated')
  ).toHaveClass('text-red-700');
  expect(
    flowOf(
      tile(
        'Safer at high power',
        'Certification evidence explained · Execution 1'
      )
    )
  ).toEqual({
    generation: 'done',
    single_qc: 'done',
    paired_qc: 'gap',
    review: 'todo',
    delivery: 'todo',
  });
});

test('a missing human review is its own step, never a missing paired check (#299)', () => {
  show();
  const flows = within(card('Safer at high power'))
    .getAllByRole('button', { name: /^Live connection demo · Execution 1/ })
    .map(flowOf);
  // Both checks done, only the quality review is missing.
  expect(flows).toContainEqual({
    generation: 'done',
    single_qc: 'done',
    paired_qc: 'done',
    review: 'gap',
    delivery: 'todo',
  });
  // A review that asked for changes holds the image at review, not at paired QC.
  expect(
    flowOf(
      tile('One more charging option', 'Live connection demo · Execution 1')
    )
  ).toEqual({
    generation: 'done',
    single_qc: 'done',
    paired_qc: 'done',
    review: 'gap',
    delivery: 'todo',
  });
});

/** A copy of `board` with the matching executions changed. */
function withExecution(
  board: Board,
  matches: (execution: BoardExecution) => boolean,
  change: (execution: BoardExecution) => void
) {
  const copy = JSON.parse(JSON.stringify(board)) as Board;
  for (const angle of copy.tree.angles)
    for (const expression of angle.expressions)
      for (const combination of expression.combinations)
        for (const execution of combination.executions)
          if (matches(execution)) change(execution);
  return copy;
}
/** Route choice diagram 1 with its paired check in `state`, as the server reads it. */
const pairedCheck = (state: string) =>
  withExecution(
    screenBoard,
    (e) => e.asset_version_id === 'v_route_1',
    (e) =>
      Object.assign(e, {
        stage: 'paired_qc_running',
        gap: 'paired_qc',
        next_action: 'none',
        paired_ai_qc: {
          id: 'aqc_paired_route_1',
          state,
          current: true,
          quality_suggestion: null,
        },
      })
  );
const routeOne = () =>
  tile('One more charging option', 'Route choice diagram · Execution 1');
function matrixButton(name: string) {
  fireEvent.click(screen.getByRole('button', { name: 'Comparison table' }));
  return within(screen.getByRole('table')).getByRole('button', { name });
}

test.each(['queued', 'running'])(
  'a %s paired check shows its step in progress, never missing (#385)',
  async (state) => {
    show(pairedCheck(state));
    const image = routeOne();
    const item = image.closest('li')!;
    expect(flowOf(image)).toEqual({
      generation: 'done',
      single_qc: 'done',
      paired_qc: 'current',
      review: 'todo',
      delivery: 'todo',
    });
    expect(item.querySelector('[data-state="gap"]')).toBeNull();
    expect(item.querySelector('[data-segment="paired_qc"]')).toHaveAttribute(
      'aria-current',
      'step'
    );
    expect(within(item).getByText('Comparison QC running')).toHaveClass(
      'text-gray-600'
    );
    expect(item).not.toHaveTextContent('Comparison QC not done');

    fireEvent.click(image);
    const drawer = await screen.findByRole('dialog');
    expect(drawer).toHaveTextContent('Status: Comparison QC running');
    expect(drawer).not.toHaveTextContent('Comparison QC not done');
    expect(drawer).not.toHaveTextContent('Nothing missing');
    expect(drawer.querySelector('[data-segment="paired_qc"]')).toHaveAttribute(
      'data-state',
      'current'
    );
    fireEvent.keyDown(document, { key: 'Escape' });

    expect(matrixButton('Route choice diagram · Execution 1')).not.toHaveClass(
      'ring-red-300'
    );
  }
);

test('an uncertain paired check waits for a person, so it stays missing (#385)', async () => {
  show(pairedCheck('uncertain'));
  const image = routeOne();
  const item = image.closest('li')!;
  expect(flowOf(image)).toMatchObject({ paired_qc: 'gap' });
  expect(within(item).getByText('Comparison QC not done')).toHaveClass(
    'text-red-700'
  );
  expect(item).not.toHaveTextContent('Comparison QC running');
  fireEvent.click(image);
  expect(await screen.findByRole('dialog')).toHaveTextContent(
    'Comparison QC not done'
  );
  fireEvent.keyDown(document, { key: 'Escape' });
  expect(matrixButton('Route choice diagram · Execution 1')).toHaveClass(
    'ring-red-300'
  );
});

test('a failed or missing check still shows the step as missing (#385)', () => {
  show(
    withExecution(
      withExecution(
        screenBoard,
        // A paired check that failed leaves the image at single_qc_done.
        (e) => e.asset_version_id === 'v_route_1',
        (e) => {
          e.paired_ai_qc = {
            id: 'aqc_paired_route_1',
            state: 'failed',
            current: true,
            quality_suggestion: null,
          };
        }
      ),
      (e) => e.asset_version_id === 'v_route_2',
      (e) => {
        e.ai_qc = { ...e.ai_qc!, state: 'uncertain' };
      }
    )
  );
  const angle = 'One more charging option';
  expect(flowOf(routeOne())).toMatchObject({ paired_qc: 'gap' });
  // The single-image check's outcome is unknown: it waits for a person.
  const uncertain = tile(angle, 'Route choice diagram · Execution 2');
  expect(flowOf(uncertain)).toMatchObject({ single_qc: 'gap' });
  expect(
    within(uncertain.closest('li')!).getByText('Single-image QC not done')
  ).toHaveClass('text-red-700');
  expect(uncertain.closest('li')).not.toHaveTextContent(
    'Single-image QC running'
  );
  // Waiting for the comparison images, and a check never run.
  const cert = tile(
    'Safer at high power',
    'Certification evidence explained · Execution 1'
  );
  expect(flowOf(cert)).toMatchObject({ paired_qc: 'gap' });
  expect(cert.closest('li')).toHaveTextContent('Comparison QC not done');
  for (const pending of within(card(angle)).getAllByRole('button', {
    name: /^Route choice diagram · Execution 1 · /,
  }))
    expect(flowOf(pending)).toMatchObject({ single_qc: 'gap' });
});

test('a placeholder being generated shows generation in progress, never missing (#385)', () => {
  show(matchBoard);
  const items = screen
    .getAllByRole('button', { name: /^Finding a charger story · / })
    .map((button) => button.closest('li')!);
  const generating = items.find((item) =>
    item.textContent!.includes('Generating…')
  )!;
  expect(flowOf(generating)).toMatchObject({ generation: 'current' });
  expect(generating.querySelector('[data-state="gap"]')).toBeNull();
  expect(generating).not.toHaveTextContent('Image not generated');
  expect(within(generating).getAllByText(/^Generating/)).toHaveLength(1);
  // A placeholder not yet sent to generation is still missing its image.
  const waiting = screen
    .getAllByText('Not generated')
    .map((preview) => preview.closest('li')!);
  expect(waiting.length).toBeGreaterThan(0);
  for (const item of waiting) {
    expect(flowOf(item)).toMatchObject({ generation: 'gap' });
    expect(item).toHaveTextContent('Image not generated');
  }
});

function withThumbnails(
  board: Board,
  link: (execution: BoardExecution) => BoardExecution['thumbnail']
) {
  const copy = JSON.parse(JSON.stringify(board)) as Board;
  for (const angle of copy.tree.angles)
    for (const expression of angle.expressions)
      for (const combination of expression.combinations)
        for (const execution of combination.executions)
          if (execution.asset_version_id) execution.thumbnail = link(execution);
  return copy;
}
const signed = (execution: BoardExecution) => ({
  url: `https://bucket.test/thumb/${execution.asset_version_id}.jpg?sig=1`,
  expires_at: '2026-10-24T00:15:00+00:00',
  mime_type: 'image/jpeg',
  width: 512,
  height: 512,
});
const downloaded = () =>
  blobs.downloadBlob.mock.calls.map(([grant]) => grant.url as string);
const intents = () =>
  gateway.createGenerationAssetVersionDownloadIntent.mock.calls.map(
    ([params]) => params.versionId as string
  );

test('tiles load the signed 512px thumbnail without a download intent; the drawer preview loads the original', async () => {
  show(withThumbnails(screenBoard, signed));
  expect(await screen.findAllByRole('img')).toHaveLength(7);
  await waitFor(() => expect(blobs.downloadBlob).toHaveBeenCalledTimes(7));
  expect(
    downloaded().every((url) => url.startsWith('https://bucket.test/thumb/'))
  ).toBe(true);
  expect(
    gateway.createGenerationAssetVersionDownloadIntent
  ).not.toHaveBeenCalled();

  fireEvent.click(
    tile('One more charging option', 'Live connection demo · Execution 1')
  );
  const drawer = await screen.findByRole('dialog', { name: 'Image details' });
  await waitFor(() => expect(intents()).toHaveLength(1));
  const [version] = intents();
  await waitFor(() =>
    expect(downloaded()).toContain('https://private.test/' + version)
  );
  // Comparison peers in the drawer are tiles too: still the derivative.
  const peers = within(
    within(drawer).getByText('Compared with').closest('section')!
  );
  expect(await peers.findAllByRole('img')).toHaveLength(3);
  expect(intents()).toEqual([version]);
});

test('a tile without a thumbnail, or whose thumbnail fails, falls back to the original', async () => {
  const [broken] = screenBoard.tree.angles
    .flatMap((a) => a.expressions)
    .flatMap((x) => x.combinations)
    .flatMap((c) => c.executions)
    .filter((e) => e.asset_version_id);
  blobs.downloadBlob.mockImplementation(async ({ url }: { url: string }) => {
    if (url.includes('expired')) throw new Error('403');
    return new Blob(['image']);
  });
  show(
    withThumbnails(screenBoard, (execution) =>
      execution.asset_version_id === broken.asset_version_id
        ? { ...signed(execution), url: 'https://bucket.test/expired.jpg' }
        : null
    )
  );
  expect(await screen.findAllByRole('img')).toHaveLength(7);
  await waitFor(() => expect(new Set(intents()).size).toBe(7));
  expect(intents()).toContain(broken.asset_version_id);
  expect(downloaded()).toContain('https://bucket.test/expired.jpg');
  expect(downloaded()).toContain(
    'https://private.test/' + broken.asset_version_id
  );
  expect(screen.queryByText('Image unavailable')).not.toBeInTheDocument();
});

test('the drawer answers belongs to, compared with, what is missing and the one next step', async () => {
  show();
  fireEvent.click(
    tile('One more charging option', 'Live connection demo · Execution 1')
  );
  const drawer = await screen.findByRole('dialog', { name: 'Image details' });
  expect(within(drawer).getByRole('button', { name: 'Close' })).toHaveFocus();
  const facts = within(drawer)
    .getByText('Belongs to')
    .closest('section')!.textContent;
  for (const value of [
    'CCS to NACS adapter',
    'One more charging option',
    'Live connection demo',
    'Plug in and charge',
    'Group A',
    'Execution 1',
  ])
    expect(facts).toContain(value);
  const peers = within(
    within(drawer).getByText('Compared with').closest('section')!
  ).getAllByRole('button');
  expect(peers.map((p) => p.getAttribute('aria-label'))).toEqual([
    'Certification evidence explained · Group B · Execution 1',
    'Live connection demo · Group B · Execution 1',
    'Live connection demo · Group B · Execution 1 v2',
  ]);
  const missing = within(drawer)
    .getByText('What is missing')
    .closest('section')!;
  expect(missing).toHaveTextContent('Status: Blocked');
  expect(within(missing).getByText('Quality review not done')).toHaveClass(
    'text-red-700'
  );
  expect(missing).toHaveTextContent('The quality review asked for changes.');
  const next = within(drawer).getByText('Next step').closest('section')!;
  expect(next).toHaveTextContent('Request a revision');
  expect(
    within(next).getByRole('button', { name: 'Primary revise' })
  ).toBeInTheDocument();
  expect(
    within(drawer).getByText('More actions').closest('details')
  ).not.toHaveAttribute('open');
  expect(
    within(drawer).getByText('Technical details').closest('details')
  ).not.toHaveAttribute('open');
});

test('an original whose revision is approved asks for nothing more (#161)', async () => {
  show(fixture.revision.approved as Board);
  const angle = 'One more charging option';
  const original = tile(angle, 'Live connection demo · Execution 1');
  // Its tile no longer names a missing step.
  expect(original.closest('li')).not.toHaveTextContent(
    'Quality review not done'
  );
  fireEvent.click(original);
  const drawer = await screen.findByRole('dialog', { name: 'Image details' });
  const missing = within(drawer)
    .getByText('What is missing')
    .closest('section')!;
  // The review's request stays on record; nothing is missing any more.
  expect(missing).toHaveTextContent('The quality review asked for changes.');
  expect(within(missing).getByText('Nothing missing')).not.toHaveClass(
    'text-red-700'
  );
  const next = within(drawer).getByText('Next step').closest('section')!;
  expect(next).toHaveTextContent('Nothing to do on this image right now.');
  expect(next).not.toHaveTextContent('Request a revision');
  expect(within(next).queryByRole('button')).not.toBeInTheDocument();
});

test('an original whose revision awaits review or was sent back still asks for a revision (#161)', async () => {
  for (const state of ['pending', 'changes_requested'] as const) {
    const { unmount } = show(fixture.revision[state] as Board);
    const original = tile(
      'One more charging option',
      'Live connection demo · Execution 1'
    );
    expect(original.closest('li')).toHaveTextContent('Quality review not done');
    fireEvent.click(original);
    const drawer = await screen.findByRole('dialog', {
      name: 'Image details',
    });
    const next = within(drawer).getByText('Next step').closest('section')!;
    expect(next).toHaveTextContent('Request a revision');
    expect(
      within(next).getByRole('button', { name: 'Primary revise' })
    ).toBeInTheDocument();
    unmount();
  }
});

test('a waiting image names the counterparts it waits for', async () => {
  show();
  fireEvent.click(
    tile(
      'Safer at high power',
      'Certification evidence explained · Execution 1'
    )
  );
  const drawer = await screen.findByRole('dialog');
  expect(drawer).toHaveTextContent(
    'Waiting for: Group A · Live connection demo; Group A · Finding a charger story'
  );
  expect(drawer).toHaveTextContent('Status: Waiting for comparison images');
  expect(drawer).toHaveTextContent('Comparison QC not done');
  expect(drawer).toHaveTextContent('Nothing to do on this image right now.');
  expect(
    within(drawer).queryByRole('button', { name: /^Primary/ })
  ).not.toBeInTheDocument();
});

test('a comparison peer opens in place, and Escape closes back to the image', async () => {
  show();
  const origin = tile(
    'One more charging option',
    'Route choice diagram · Execution 1'
  );
  fireEvent.click(origin);
  const drawer = await screen.findByRole('dialog');
  fireEvent.click(
    within(drawer).getByRole('button', {
      name: 'Certification evidence explained · Group B · Execution 1',
    })
  );
  expect(
    within(drawer).getByText('Belongs to').closest('section')
  ).toHaveTextContent('Safer at high power');
  fireEvent.keyDown(document, { key: 'Escape' });
  await waitFor(() =>
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  );
  expect(origin).toHaveFocus();
});

test('an identical file keeps its execution mark, says so, and is compared with nothing', async () => {
  show();
  const route = within(card('One more charging option'))
    .getByRole('heading', { name: 'Route choice diagram' })
    .closest('li')!;
  // Two files, one execution: the copy does not become execution 3.
  expect(within(route).getByText('Executions: 2')).toBeInTheDocument();
  fireEvent.click(
    within(route).getByRole('button', {
      name: 'Route choice diagram · Execution 1 · same file',
    })
  );
  const drawer = await screen.findByRole('dialog');
  expect(drawer).toHaveTextContent(
    'Same file as an earlier image in this combination; not a separate execution.'
  );
  expect(drawer).toHaveTextContent(
    'Not compared with another image in this round.'
  );
});

test('a placeholder has no version: its next step is generation and technical details stay empty', async () => {
  show();
  fireEvent.click(
    tile('One more charging option', 'Finding a charger story · Execution 1')
  );
  const drawer = await screen.findByRole('dialog');
  expect(drawer).toHaveTextContent('Status: Waiting to generate');
  expect(drawer).toHaveTextContent('Generate this image');
  expect(
    within(drawer).getByRole('button', { name: 'Primary generate' })
  ).toBeInTheDocument();
  details('Technical details');
  expect(drawer).toHaveTextContent('No image version yet.');
  expect(gateway.getGenerationAssetVersion).not.toHaveBeenCalled();
});

test('technical details load the exact version only when expanded', async () => {
  show();
  fireEvent.click(
    tile('One more charging option', 'Route choice diagram · Execution 1')
  );
  await screen.findByRole('dialog');
  expect(gateway.getGenerationAssetVersion).not.toHaveBeenCalled();
  const technical = details('Technical details');
  await waitFor(() =>
    expect(technical).toHaveTextContent('Proposition: One more charging option')
  );
  expect(gateway.getGenerationAssetVersion).toHaveBeenCalledWith({
    workspaceSlug: 'alpha',
    versionId: 'v_route_1',
  });
  expect(technical).toHaveTextContent('v_route_1');
  expect(technical).toHaveTextContent('mock-image-v1');
});

test('the comparison table keeps unique expressions and shows untested cells as not tested', async () => {
  show();
  fireEvent.click(screen.getByRole('button', { name: 'Comparison table' }));
  expect(
    screen.getByRole('button', { name: 'Comparison table' })
  ).toHaveAttribute('aria-pressed', 'true');
  const table = screen.getByRole('table');
  expect(
    within(table)
      .getAllByRole('columnheader')
      .map((h) => h.textContent)
  ).toEqual(['Expression', 'One more charging option', 'Safer at high power']);
  const rows = within(table).getAllByRole('row').slice(1);
  expect(
    rows.map((row) => within(row).getByRole('rowheader').textContent)
  ).toEqual([
    'Route choice diagram',
    'Live connection demo' + 'Shared across angles',
    'Finding a charger story',
    'Certification evidence explained',
  ]);
  const cells = rows.map((row) =>
    within(row)
      .getAllByRole('cell')
      .map((cell) =>
        cell.textContent === 'Not tested'
          ? 'untested'
          : within(cell).getAllByRole('button').length
      )
  );
  expect(cells).toEqual([
    [3, 'untested'],
    [1, 2],
    [1, 'untested'],
    ['untested', 1],
  ]);
  fireEvent.click(
    within(rows[3]).getByRole('button', {
      name: 'Certification evidence explained · Execution 1',
    })
  );
  expect(await screen.findByRole('dialog')).toHaveTextContent(
    'Safer at high power'
  );
});

test('matching expressions shows only the server current angle; switching keeps the round comparison', () => {
  show(matchBoard);
  const comparison =
    'Main comparison: Route choice diagram ↔ Live connection demo ↔ Finding a charger story';
  expect(screen.getByText(comparison)).toBeInTheDocument();
  expect(
    screen.getByText('Same angle: One more charging option')
  ).toBeInTheDocument();
  expect(screen.getAllByRole('article')).toHaveLength(1);
  const select = screen.getByRole('combobox', { name: 'Current angle' });
  expect(select).toHaveValue('a_option');
  expect(screen.queryByRole('note')).not.toBeInTheDocument();
  fireEvent.change(select, { target: { value: 'a_safe' } });
  expect(screen.getAllByRole('article')).toHaveLength(1);
  expect(card('Safer at high power')).toBeInTheDocument();
  expect(
    screen.queryByRole('article', { name: 'One more charging option' })
  ).not.toBeInTheDocument();
  expect(screen.getByText(comparison)).toBeInTheDocument();
  expect(screen.getByRole('note')).toHaveTextContent(
    'main comparison stays within “One more charging option”'
  );
});

test('every board string is localized and the component hardcodes no Chinese', () => {
  const source = fs.readFileSync(
    path.join(__dirname, '../../../components/workspace/MarketingBoard.tsx'),
    'utf8'
  );
  expect(source).not.toMatch(/[\u3400-\u9fff]/);
  const used = Array.from(
    source.matchAll(/'(marketing-board-[a-z_-]+)'/g),
    (m) => m[1]
  );
  const templated: Record<string, string[]> = {
    'marketing-board-stage-': [
      'pending_generation',
      'generating',
      'single_qc_pending',
      'single_qc_running',
      'single_qc_done',
      'paired_qc_pending',
      'paired_qc_running',
      'paired_qc_done',
      'ready',
      'exported',
      'bound',
      'has_data',
      'blocked',
    ],
    'marketing-board-gap-': [
      'pending_generation',
      'single_qc',
      'paired_qc',
      'review',
      'export',
      'bind',
      'data',
      'none',
    ],
    'marketing-board-action-': [
      'generate',
      'single_qc',
      'paired_qc',
      'ai_qc',
      'review',
      'revise',
      'export',
      'bind',
      'import',
      'evaluate',
      'none',
    ],
    'marketing-board-blocked-': [
      'quality_changes_requested',
      'file_qc_failed',
      'reference_not_current',
      'review_withdrawn',
    ],
    'marketing-board-flow-': [
      'generation',
      'single_qc',
      'paired_qc',
      'review',
      'delivery',
    ],
    'marketing-board-view-': ['groups', 'matrix'],
    'marketing-board-appearance-': [
      'pending_verification',
      'partial',
      'no_reference',
      'reference_not_current',
    ],
  };
  const keys = [
    ...used,
    ...Object.entries(templated).flatMap(([prefix, values]) =>
      values.map((v) => prefix + v)
    ),
  ];
  expect(keys.filter((key) => !(key in messages))).toEqual([]);
});
