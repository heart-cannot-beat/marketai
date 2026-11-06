import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'next-i18next';
import useSWR from 'swr';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import { useAssetImageUrl } from '@/hooks/assets/useAssetImageUrl';
import { useMarketingImageSource } from './useMarketingImageSource';
import { useWorkspace } from './WorkspaceProvider';
import { TechnicalFacts } from './MarketingTechnicalInfo';

type Board = components['schemas']['MarketingBoard'];
type Angle = components['schemas']['BoardAngle'];
type Expression = components['schemas']['BoardExpression'];
type Combination = components['schemas']['BoardCombination'];
type HandoffEntry = components['schemas']['HandoffEntry'];
export type BoardExecution = components['schemas']['BoardExecution'];
export type BoardActions = {
  primary?: ReactNode;
  secondary?: ReactNode;
  secondaryOpen?: boolean;
};
type Placed = {
  execution: BoardExecution;
  angle: Angle;
  expression: Expression;
  combination: Combination;
};

const SEGMENTS = [
  'generation',
  'single_qc',
  'paired_qc',
  'review',
  'delivery',
] as const;
type Segment = (typeof SEGMENTS)[number];
// Where the server's stage and gap sit on the five-step flow. A finished
// single-image check waits on its paired check; once both are done the human
// quality review is its own step, so a missing review is never shown as a
// missing paired check (#299); a blocked execution is held at its gap.
const STAGE_SEGMENT: Record<BoardExecution['stage'], Segment | null> = {
  pending_generation: 'generation',
  generating: 'generation',
  single_qc_pending: 'single_qc',
  single_qc_running: 'single_qc',
  single_qc_done: 'paired_qc',
  paired_qc_pending: 'paired_qc',
  paired_qc_running: 'paired_qc',
  paired_qc_done: 'review',
  ready: 'delivery',
  exported: 'delivery',
  bound: 'delivery',
  has_data: 'delivery',
  blocked: null,
};
const GAP_SEGMENT: Record<BoardExecution['gap'], Segment | null> = {
  pending_generation: 'generation',
  single_qc: 'single_qc',
  paired_qc: 'paired_qc',
  review: 'review',
  export: 'delivery',
  bind: 'delivery',
  data: 'delivery',
  none: null,
};
const RUNNING = ['queued', 'running'];

/**
 * Whether the execution's own step is under way: its image is being
 * generated, or its single-image or paired check is queued or running.
 *
 * The server still names that step as the gap until it finishes; the board
 * shows it as the current step in progress instead, never as missing (#385).
 * These are the board's share of the states the page keeps polling for
 * (#307), so a step shown in progress refreshes on its own. An `uncertain`
 * call waits for a person to reconcile it, so it stays missing.
 */
export function stepInProgress(execution: BoardExecution) {
  switch (execution.stage) {
    case 'generating':
      return true;
    case 'single_qc_running':
      return RUNNING.includes(execution.ai_qc?.state ?? '');
    case 'paired_qc_running':
      return RUNNING.includes(execution.paired_ai_qc?.state ?? '');
    default:
      return false;
  }
}

const SECONDARY =
  'rounded-lg border border-gray-300 px-3 py-1.5 text-sm disabled:opacity-40';
// A pressed view switch is a state, not the page's main action (J-32).
const PRESSED =
  'rounded-lg border border-gray-900 bg-gray-100 px-3 py-1.5 text-sm font-medium';

/** A tile's identity: its AssetVersion, or the combination slot a placeholder fills. */
export function executionKey(e: BoardExecution) {
  return e.asset_version_id ?? `${e.combination_id}#${e.execution_index}`;
}

function place(board: Board) {
  const placed: Placed[] = [];
  for (const angle of board.tree.angles)
    for (const expression of angle.expressions)
      for (const combination of expression.combinations)
        for (const execution of combination.executions)
          placed.push({ execution, angle, expression, combination });
  return placed;
}

export function MarketingBoard({
  board,
  productName,
  deliveryEntries = [],
  selected,
  onSelect,
  renderActions,
}: {
  board: Board;
  productName: string;
  deliveryEntries?: HandoffEntry[];
  selected: string | null;
  onSelect: (key: string | null) => void;
  renderActions: (execution: BoardExecution, close: () => void) => BoardActions;
}) {
  const { t } = useTranslation('common');
  const [view, setView] = useState<'groups' | 'matrix'>('groups');
  const [chosenAngle, setChosenAngle] = useState<string | null>(null);
  const placed = place(board);
  const byKey = new Map(placed.map((p) => [executionKey(p.execution), p]));
  const versions = new Set(
    placed.map((p) => p.execution.asset_version_id).filter(Boolean)
  );
  const combinations = new Map(
    board.tree.angles.flatMap((angle) =>
      angle.expressions.flatMap((expression) =>
        expression.combinations.map(
          (combination) =>
            [
              combination.combination_id,
              { angle, expression, combination },
            ] as const
        )
      )
    )
  );
  const groupNames = new Map(
    Array.from(combinations.values(), ({ combination }) => [
      combination.group_id,
      combination.group_name,
    ])
  );
  const primary = board.primary_comparison;
  const within = primary.kind === 'within_angle' ? primary : null;
  // The server names the round's current angle; switching only changes what
  // is shown, never the comparison stated above.
  const shownAngle =
    within &&
    (board.tree.angles.some((a) => a.angle_id === chosenAngle)
      ? chosenAngle!
      : within.angle_id);
  const proposition = (angleId: string | null) =>
    board.tree.angles.find((a) => a.angle_id === angleId)?.proposition ??
    angleId ??
    '';

  function label(p: Placed, withGroup = false) {
    const { execution } = p;
    // A revision keeps its execution's number with the next version (1 v2);
    // reads without the execution plan only know that it was revised.
    const version = execution.execution_version;
    return [
      withGroup ? p.combination.group_name : null,
      version && version > 1
        ? t('marketing-board-execution-version', {
            number: execution.execution_index,
            version,
          })
        : t('marketing-board-execution', { number: execution.execution_index }),
      !version &&
      execution.parent_version_id &&
      versions.has(execution.parent_version_id)
        ? t('marketing-board-revised')
        : null,
      execution.duplicate_of ? t('marketing-board-duplicate-mark') : null,
    ]
      .filter(Boolean)
      .join(' · ');
  }

  function primaryValue() {
    switch (primary.kind) {
      case 'between_groups':
        return primary.groups.map((g) => g.name).join(' ↔ ');
      case 'within_angle': {
        const angle = board.tree.angles.find(
          (a) => a.angle_id === primary.angle_id
        );
        return primary.expression_ids
          .map(
            (id) =>
              angle?.expressions.find((x) => x.expression_id === id)?.name ?? id
          )
          .join(' ↔ ');
      }
      case 'retest':
        return t('marketing-board-primary-retest', {
          original: primary.original_asset_version_ids.length,
          new: primary.new_execution_ids.length,
        });
      case 'factor':
        return [
          t('marketing-board-primary-factor', {
            changed: primary.changed.join(t('list-comma')),
            count: primary.pairs.length,
          }),
          primary.unpaired_combination_ids.length
            ? t('marketing-board-primary-factor-unpaired', {
                count: primary.unpaired_combination_ids.length,
              })
            : null,
        ]
          .filter(Boolean)
          .join(' · ');
    }
  }

  const secondary = board.secondary_observation;
  const opened = selected ? byKey.get(selected) : undefined;
  // Focus returns to the image that opened the drawer; a click does not
  // focus a button in every browser.
  const opener = useRef<HTMLElement | null>(null);
  const open = (p: Placed, from?: HTMLElement) => {
    if (from) opener.current = from;
    onSelect(executionKey(p.execution));
  };

  function tile(p: Placed, withGroup: boolean) {
    const name = label(p, withGroup);
    return (
      <li key={executionKey(p.execution)} className="min-w-0 space-y-1.5">
        <button
          type="button"
          className="block w-full rounded-lg text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-gray-900"
          aria-label={`${p.expression.name} · ${name}`}
          onClick={(e) => open(p, e.currentTarget)}
        >
          <Preview
            execution={p.execution}
            alt={name}
            className="aspect-square w-full"
          />
          <span className="mt-1 block truncate text-xs font-medium text-gray-900">
            {name}
          </span>
        </button>
        <Flow execution={p.execution} />
        {/* A placeholder being generated already says so in its preview. */}
        {stepInProgress(p.execution)
          ? p.execution.stage !== 'generating' && (
              <p className="text-xs text-gray-600">
                {t(`marketing-board-stage-${p.execution.stage}`)}
              </p>
            )
          : p.execution.gap !== 'none' && (
              <p className="text-xs text-red-700">
                {t(`marketing-board-gap-${p.execution.gap}`)}
              </p>
            )}
      </li>
    );
  }

  function expressionRow(angle: Angle, expression: Expression) {
    const items = placed.filter(
      (p) => p.angle === angle && p.expression === expression
    );
    const count = new Set(
      items.map(
        (p) => `${p.combination.combination_id}#${p.execution.execution_index}`
      )
    ).size;
    return (
      <li key={expression.expression_id} className="space-y-3 px-4 py-4">
        <div className="flex items-start justify-between gap-3">
          <h5 className="min-w-0 break-words font-medium underline decoration-gray-300 underline-offset-4">
            {expression.name}
          </h5>
          <span className="shrink-0 text-sm text-gray-600">
            {t('marketing-board-expression-executions', { count })}
          </span>
        </div>
        {(expression.shared || expression.details_missing) && (
          <div className="flex flex-wrap gap-1.5 text-xs">
            {expression.shared && (
              <span className="rounded bg-gray-100 px-1.5 py-0.5 text-gray-700">
                {t('marketing-board-shared')}
              </span>
            )}
            {expression.details_missing && (
              <span className="rounded bg-amber-50 px-1.5 py-0.5 text-amber-800">
                {t('marketing-board-details-missing')}
              </span>
            )}
          </div>
        )}
        {items.length ? (
          <ul className="grid grid-cols-2 gap-3">
            {items.map((p) => tile(p, expression.combinations.length > 1))}
          </ul>
        ) : (
          <p className="text-sm text-gray-500">
            {t('marketing-board-not-arranged')}
          </p>
        )}
      </li>
    );
  }

  return (
    <section aria-label={t('marketing-board-label')} className="space-y-4">
      <header className="space-y-1">
        <p className="text-xs font-medium uppercase tracking-wide text-gray-500">
          {t(`marketing-stage-${board.stage}`)}
        </p>
        <h3 className="text-lg font-semibold">{board.question}</h3>
        <p className="text-sm text-gray-600">
          <span>{t('marketing-board-primary', { value: primaryValue() })}</span>
          {secondary && (
            <>
              <span aria-hidden="true" className="mx-2 text-gray-300">
                |
              </span>
              <span>
                {secondary.kind === 'same_angle'
                  ? t('marketing-board-secondary-same_angle', {
                      angle: proposition(secondary.angle_id),
                    })
                  : t('marketing-board-secondary-expression_coverage')}
              </span>
            </>
          )}
        </p>
        {board.product_appearance &&
          board.product_appearance.status !== 'reference_backed' && (
            <p className="text-sm text-amber-800">
              {t(
                `marketing-board-appearance-${board.product_appearance.status}`,
                {
                  count:
                    board.product_appearance.pending_combination_ids.length,
                }
              )}
            </p>
          )}
      </header>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div
          role="group"
          aria-label={t('marketing-board-view')}
          className="flex gap-2"
        >
          {(['groups', 'matrix'] as const).map((value) => (
            <button
              key={value}
              type="button"
              aria-pressed={view === value}
              className={view === value ? PRESSED : SECONDARY}
              onClick={() => setView(value)}
            >
              {t(`marketing-board-view-${value}`)}
            </button>
          ))}
        </div>
        {within && view === 'groups' && (
          <label className="flex items-center gap-2 text-sm">
            <span>{t('marketing-board-current-angle')}</span>
            <select
              className="max-w-[14rem] rounded-lg border border-gray-300 bg-white px-2 py-1.5 text-sm"
              value={shownAngle ?? ''}
              onChange={(e) => setChosenAngle(e.target.value)}
            >
              {board.tree.angles.map((a) => (
                <option key={a.angle_id} value={a.angle_id}>
                  {a.proposition}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
      {within && view === 'groups' && shownAngle !== within.angle_id && (
        <p
          role="note"
          className="rounded-lg bg-gray-50 p-3 text-sm text-gray-700"
        >
          {t('marketing-board-viewing-other-angle', {
            angle: proposition(within.angle_id),
          })}
        </p>
      )}
      {view === 'groups' ? (
        <div className="grid gap-4 md:grid-cols-2">
          {board.tree.angles
            .filter((a) => !shownAngle || a.angle_id === shownAngle)
            .map((angle) => (
              <article
                key={angle.angle_id}
                aria-label={angle.proposition}
                className="min-w-0 overflow-hidden rounded-xl border border-gray-200"
              >
                <header className="border-b border-gray-200 bg-gray-50 px-4 py-3">
                  <h4 className="break-words font-semibold">
                    {[
                      // Groups are often named after their angle.
                      angle.group_ids
                        .map((id) => groupNames.get(id) ?? id)
                        .filter((name) => name !== angle.proposition)
                        .join(' · '),
                      angle.proposition,
                    ]
                      .filter(Boolean)
                      .join(' · ')}
                  </h4>
                  <p className="text-sm text-gray-600">
                    {t('marketing-board-angle-expressions', {
                      count: angle.expressions.length,
                    })}
                  </p>
                </header>
                <ul className="divide-y divide-gray-200">
                  {angle.expressions.map((expression) =>
                    expressionRow(angle, expression)
                  )}
                </ul>
              </article>
            ))}
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-sm">
            <caption className="pb-2 text-left text-xs text-gray-500">
              {t('marketing-board-matrix-caption')}
            </caption>
            <thead>
              <tr className="border-b border-gray-200 text-left align-bottom">
                <th scope="col" className="py-2 pr-3 font-medium">
                  {t('marketing-board-matrix-expression')}
                </th>
                {board.matrix.cols.map((col) => (
                  <th
                    key={col.angle_id}
                    scope="col"
                    className="min-w-[7rem] px-2 py-2 font-medium"
                  >
                    {col.proposition}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {board.matrix.rows.map((row, i) => (
                <tr
                  key={`${row.expression_id}:${i}`}
                  className="border-b border-gray-100 align-top"
                >
                  <th scope="row" className="py-3 pr-3 text-left font-normal">
                    <span className="block break-words font-medium">
                      {row.name}
                    </span>
                    {row.shared && (
                      <span className="mt-1 inline-block rounded bg-gray-100 px-1.5 py-0.5 text-xs text-gray-700">
                        {t('marketing-board-shared')}
                      </span>
                    )}
                  </th>
                  {board.matrix.cols.map((col, j) => {
                    const cell = board.matrix.cells[i]?.[j] ?? null;
                    return (
                      <td key={col.angle_id} className="px-2 py-3">
                        {cell === null ? (
                          <span className="text-gray-400">
                            {t('marketing-board-untested')}
                          </span>
                        ) : (
                          <ul className="flex flex-wrap gap-1.5">
                            {placed
                              .filter((p) =>
                                cell.includes(p.combination.combination_id)
                              )
                              .map((p) => (
                                <li key={executionKey(p.execution)}>
                                  <button
                                    type="button"
                                    aria-label={`${p.expression.name} · ${label(p, cell.length > 1)}`}
                                    className={`block rounded-md focus:outline-none focus-visible:ring-2 focus-visible:ring-gray-900 ${
                                      p.execution.gap === 'none' ||
                                      stepInProgress(p.execution)
                                        ? ''
                                        : 'ring-1 ring-red-300'
                                    }`}
                                    onClick={(e) => open(p, e.currentTarget)}
                                  >
                                    <Preview
                                      execution={p.execution}
                                      alt={label(p, cell.length > 1)}
                                      className="h-14 w-14"
                                      compact
                                    />
                                  </button>
                                </li>
                              ))}
                          </ul>
                        )}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="text-xs text-gray-500">{t('marketing-board-open-hint')}</p>
      {opened && (
        <Drawer
          key="drawer"
          placed={opened}
          productName={productName}
          label={label}
          peers={opened.execution.compare_with
            .map((id) => byKey.get(id))
            .filter((p): p is Placed => !!p)}
          awaiting={opened.execution.awaiting_combination_ids.map((id) => {
            const found = combinations.get(id);
            return found
              ? `${found.combination.group_name} · ${found.expression.name}`
              : id;
          })}
          entry={deliveryEntries.find(
            (e) => e.asset_version_id === opened.execution.asset_version_id
          )}
          actions={renderActions(opened.execution, () => onSelect(null))}
          onSelect={(p) => open(p)}
          onClose={() => onSelect(null)}
          returnFocus={() => opener.current}
        />
      )}
    </section>
  );
}

function Preview({
  execution,
  alt,
  className,
  compact = false,
  original = false,
}: {
  execution: BoardExecution;
  alt: string;
  className: string;
  compact?: boolean;
  /** The drawer's large preview shows the original; tiles show the 512px derivative. */
  original?: boolean;
}) {
  const { t } = useTranslation('common');
  if (!execution.asset_version_id)
    return (
      <span
        className={`flex items-center justify-center rounded-lg border border-dashed border-gray-300 bg-gray-50 p-1 text-center text-xs text-gray-500 ${className}`}
      >
        {compact
          ? ''
          : t(
              execution.stage === 'generating'
                ? 'marketing-board-generating'
                : 'marketing-board-not-generated'
            )}
      </span>
    );
  return original ? (
    <Original
      versionId={execution.asset_version_id}
      alt={alt}
      className={className}
    />
  ) : (
    <Thumbnail
      versionId={execution.asset_version_id}
      thumbnail={execution.thumbnail}
      alt={alt}
      className={className}
    />
  );
}

/** The signed 512px derivative; the hook falls back to the original when it is missing or fails. */
function Thumbnail({
  versionId,
  thumbnail,
  alt,
  className,
}: {
  versionId: string;
  thumbnail: BoardExecution['thumbnail'];
  alt: string;
  className: string;
}) {
  const { blobUrl, problem } = useAssetImageUrl(versionId, { thumbnail });
  const [broken, setBroken] = useState<string | null>(null);
  return (
    <ImageFrame
      url={blobUrl}
      failed={Boolean(problem) || (blobUrl !== null && broken === blobUrl)}
      onError={() => setBroken(blobUrl)}
      alt={alt}
      className={className}
    />
  );
}

function Original({
  versionId,
  alt,
  className,
}: {
  versionId: string;
  alt: string;
  className: string;
}) {
  const { imageUrl, imageFailed, onImageError } = useMarketingImageSource(
    versionId,
    true
  );
  return (
    <ImageFrame
      url={imageUrl}
      failed={imageFailed}
      onError={onImageError}
      alt={alt}
      className={className}
    />
  );
}

function ImageFrame({
  url,
  failed,
  onError,
  alt,
  className,
}: {
  url: string | null;
  failed: boolean;
  onError: () => void;
  alt: string;
  className: string;
}) {
  const { t } = useTranslation('common');
  return (
    <span
      className={`relative block overflow-hidden rounded-lg border border-gray-200 bg-gray-100 ${className}`}
    >
      {url && !failed ? (
        // Private blob URL from a download grant; next/image cannot load it.
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={url}
          alt={alt}
          className="h-full w-full object-cover"
          onError={onError}
        />
      ) : (
        failed && (
          <span className="absolute inset-0 flex items-center justify-center p-1 text-center text-xs text-gray-500">
            {t('marketing-board-image-failed')}
          </span>
        )
      )}
    </span>
  );
}

function Flow({ execution }: { execution: BoardExecution }) {
  const { t } = useTranslation('common');
  const gap = stepInProgress(execution) ? null : GAP_SEGMENT[execution.gap];
  const current = STAGE_SEGMENT[execution.stage] ?? gap;
  const reached = current ? SEGMENTS.indexOf(current) : -1;
  return (
    <ol
      aria-label={t('marketing-board-flow')}
      className="flex flex-wrap items-center gap-x-1 text-[11px] leading-4"
    >
      {SEGMENTS.map((segment, i) => {
        const state =
          segment === gap
            ? 'gap'
            : segment === current
              ? 'current'
              : i < reached
                ? 'done'
                : 'todo';
        return (
          <li
            key={segment}
            data-segment={segment}
            data-state={state}
            aria-current={segment === current ? 'step' : undefined}
            className="flex items-center gap-1"
          >
            {i > 0 && (
              <span aria-hidden="true" className="text-gray-300">
                →
              </span>
            )}
            <span
              className={
                state === 'gap'
                  ? 'font-semibold text-red-700'
                  : state === 'current'
                    ? 'font-semibold text-gray-900'
                    : state === 'done'
                      ? 'text-gray-600'
                      : 'text-gray-400'
              }
            >
              {t(`marketing-board-flow-${segment}`)}
            </span>
            {state === 'gap' && (
              <span className="sr-only">({t('marketing-board-flow-gap')})</span>
            )}
            {state === 'current' && (
              <span className="sr-only">
                ({t('marketing-board-flow-current')})
              </span>
            )}
          </li>
        );
      })}
    </ol>
  );
}

function Drawer({
  placed,
  productName,
  label,
  peers,
  awaiting,
  entry,
  actions,
  onSelect,
  onClose,
  returnFocus: focusTarget,
}: {
  placed: Placed;
  productName: string;
  label: (p: Placed, withGroup?: boolean) => string;
  peers: Placed[];
  awaiting: string[];
  entry?: HandoffEntry;
  actions: BoardActions;
  onSelect: (p: Placed) => void;
  onClose: () => void;
  returnFocus: () => HTMLElement | null;
}) {
  const { t } = useTranslation('common');
  const titleId = useId();
  const closeRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const closeAction = useRef(onClose);
  closeAction.current = onClose;
  const returnFocus = useRef(focusTarget);
  returnFocus.current = focusTarget;
  const { execution, angle, expression, combination } = placed;
  const key = executionKey(execution);
  useEffect(() => {
    const previous =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closeAction.current();
    };
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('keydown', onKey);
      const target = returnFocus.current() ?? previous;
      if (target?.isConnected) target.focus();
    };
  }, []);
  useEffect(() => {
    panelRef.current?.scrollTo?.({ top: 0 });
  }, [key]);
  const name = label(placed, true);
  const facts: [string, string][] = [
    [t('marketing-board-product'), productName],
    [t('marketing-board-angle'), angle.proposition],
    [t('marketing-board-expression'), expression.name],
    [t('marketing-board-combination'), combination.headline],
    [t('marketing-board-group'), combination.group_name],
    [t('marketing-board-execution-label'), label(placed)],
  ];
  return createPortal(
    <div className="fixed inset-0 z-50 flex justify-end" style={{ margin: 0 }}>
      <div
        aria-hidden="true"
        className="absolute inset-0 bg-gray-900/30"
        onClick={onClose}
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="relative h-full w-full max-w-lg overflow-y-auto bg-white shadow-xl"
      >
        <header className="sticky top-0 z-10 flex items-start justify-between gap-3 border-b border-gray-200 bg-white px-4 py-3">
          <div className="min-w-0">
            <h3 id={titleId} className="font-semibold">
              {t('marketing-board-drawer-title')}
            </h3>
            <p className="break-words text-sm text-gray-600">
              {expression.name} · {name}
            </p>
          </div>
          <button
            ref={closeRef}
            type="button"
            className={SECONDARY}
            onClick={onClose}
          >
            {t('close')}
          </button>
        </header>
        <div key={key} className="space-y-6 px-4 py-4">
          <Preview
            execution={execution}
            alt={name}
            className="aspect-square w-full"
            original
          />
          <section className="space-y-2">
            <h4 className="font-medium">{t('marketing-board-ownership')}</h4>
            <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-sm">
              {facts.map(([term, value]) => (
                <div key={term} className="contents">
                  <dt className="text-gray-600">{term}</dt>
                  <dd className="break-words">{value}</dd>
                </div>
              ))}
            </dl>
          </section>
          <section className="space-y-2">
            <h4 className="font-medium">
              {t('marketing-board-compared-with')}
            </h4>
            {peers.length > 0 && (
              <ul className="grid grid-cols-3 gap-2 sm:grid-cols-4">
                {peers.map((peer) => (
                  <li key={executionKey(peer.execution)} className="min-w-0">
                    <button
                      type="button"
                      className="block w-full text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-gray-900"
                      aria-label={`${peer.expression.name} · ${label(peer, true)}`}
                      onClick={() => onSelect(peer)}
                    >
                      <Preview
                        execution={peer.execution}
                        alt={label(peer, true)}
                        className="aspect-square w-full"
                        compact
                      />
                      <span className="mt-1 block truncate text-xs">
                        {peer.expression.name}
                      </span>
                      <span className="block truncate text-xs text-gray-600">
                        {label(peer, true)}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
            {awaiting.length > 0 && (
              <p className="text-sm text-amber-800">
                {t('marketing-board-awaiting', {
                  names: awaiting.join(t('list-separator')),
                })}
              </p>
            )}
            {!peers.length && !awaiting.length && (
              <p className="text-sm text-gray-600">
                {t('marketing-board-no-comparison')}
              </p>
            )}
          </section>
          <section className="space-y-2">
            <h4 className="font-medium">{t('marketing-board-missing')}</h4>
            <p className="text-sm text-gray-600">
              {t('marketing-board-status', {
                stage: t(`marketing-board-stage-${execution.stage}`),
              })}
            </p>
            <Flow execution={execution} />
            {/* The status line above already says the step is under way. */}
            {!stepInProgress(execution) && (
              <p
                className={
                  execution.gap === 'none'
                    ? 'text-sm text-gray-700'
                    : 'text-sm font-medium text-red-700'
                }
              >
                {t(`marketing-board-gap-${execution.gap}`)}
              </p>
            )}
            {execution.blocked_reason && (
              <p className="text-sm text-red-700">
                {t(`marketing-board-blocked-${execution.blocked_reason}`)}
              </p>
            )}
            {execution.duplicate_of && (
              <p className="text-sm text-gray-600">
                {t('marketing-board-duplicate')}
              </p>
            )}
            {execution.product_appearance?.status ===
              'pending_verification' && (
              <p className="text-sm text-amber-800">
                {t(
                  `marketing-board-appearance-${execution.product_appearance.reason ?? 'no_reference'}`
                )}
              </p>
            )}
          </section>
          <section className="space-y-3">
            <h4 className="font-medium">{t('marketing-board-next-step')}</h4>
            <p
              className={
                execution.next_action === 'none'
                  ? 'text-sm text-gray-600'
                  : 'text-sm font-medium'
              }
            >
              {t(`marketing-board-action-${execution.next_action}`)}
            </p>
            {execution.next_action !== 'none' && actions.primary}
          </section>
          {actions.secondary && (
            <details
              open={actions.secondaryOpen || undefined}
              className="space-y-3 border-t border-gray-200 pt-4"
            >
              <summary className="cursor-pointer text-sm font-medium">
                {t('marketing-board-more-actions')}
              </summary>
              <div className="mt-3 space-y-4">{actions.secondary}</div>
            </details>
          )}
          <TechnicalDetails execution={execution} entry={entry} />
        </div>
      </div>
    </div>,
    document.body
  );
}

function TechnicalDetails({
  execution,
  entry,
}: {
  execution: BoardExecution;
  entry?: HandoffEntry;
}) {
  const { t } = useTranslation('common');
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const [open, setOpen] = useState(false);
  const versionId = execution.asset_version_id;
  const detail = useSWR(
    open && versionId
      ? ['marketing-board-version', current.workspaceId, versionId]
      : null,
    async () => {
      const result = await gateway.getGenerationAssetVersion({
        workspaceSlug: current.slug,
        versionId: versionId!,
      });
      if (!('data' in result) || !result.data)
        throw new Error('marketing-action-failed');
      return result.data;
    },
    { revalidateOnFocus: false }
  );
  const facts: [string, string | null | undefined][] = [
    [t('marketing-board-version'), versionId],
    [t('marketing-board-parent-version'), execution.parent_version_id],
    [t('marketing-board-provider'), detail.data?.provider ?? entry?.provider],
    [t('marketing-board-model'), detail.data?.model ?? entry?.model],
    [t('marketing-board-recipe-hash'), entry?.recipe_hash],
    [
      t('marketing-board-content-hash'),
      detail.data?.content_sha256 ?? entry?.content_sha256,
    ],
  ];
  const prompt = detail.data?.prompt ?? entry?.provenance?.original_prompt;
  return (
    <details
      onToggle={(e) => setOpen(e.currentTarget.open)}
      className="border-t border-gray-200 pt-4"
    >
      <summary className="cursor-pointer text-sm font-medium">
        {t('marketing-board-technical')}
      </summary>
      {versionId ? (
        <div className="mt-3 space-y-3 text-sm">
          {detail.isLoading && (
            <p role="status">{t('marketing-board-technical-loading')}</p>
          )}
          {detail.error && (
            <p role="alert">{t('marketing-board-technical-failed')}</p>
          )}
          <TechnicalFacts facts={facts} />
          {prompt && (
            <div className="space-y-1">
              <p className="text-gray-600">{t('marketing-board-prompt')}</p>
              <pre className="max-h-60 overflow-auto whitespace-pre-wrap break-words rounded bg-gray-50 p-2 text-xs">
                {prompt}
              </pre>
            </div>
          )}
        </div>
      ) : (
        <p className="mt-3 text-sm text-gray-600">
          {t('marketing-board-no-version')}
        </p>
      )}
    </details>
  );
}
