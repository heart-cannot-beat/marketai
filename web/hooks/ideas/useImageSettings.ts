import { useCallback, useEffect, useMemo, useState } from 'react';
import useSWR from 'swr';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import type { components } from '@/lib/domain-client/generated/domain-v1';
import type { IdeasDetail } from '@/lib/ideas/ideas-view';
import type { ImageSettings } from './useIdeasCommands';

type ProviderOption = components['schemas']['GenerationProviderOption'];

export const ASPECT_RATIOS = ['4:5', '1:1', '9:16', '16:9'] as const;
export const IMAGE_SIZES = ['1K', '2K'] as const;
export const MAX_VARIANTS = 4;

/** The first batch makes one 4:5 1K image per way of telling (#193). */
const FIRST_BATCH_FORMAT = {
  aspect_ratio: '4:5',
  image_size: '1K',
  variants: 1,
} as const;

export interface UseImageSettings {
  settings: ImageSettings | null;
  /** The providers that can make an ad with the workspace's own key. */
  options: ProviderOption[];
  change: (next: Partial<ImageSettings>) => void;
  /** The capabilities could not be read. */
  failed: boolean;
}

const storageKey = (workspaceId: string, projectId: string) =>
  `ideas-image-settings:${workspaceId}:${projectId}`;

function readStored(key: string): Partial<ImageSettings> | null {
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as Partial<ImageSettings>) : null;
  } catch {
    return null;
  }
}

function writeStored(key: string, value: ImageSettings) {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* A remembered choice is a convenience only. */
  }
}

/** Keeps only what the providers offer now; falls back to their defaults. */
export function settle(
  wanted: Partial<ImageSettings>,
  options: ProviderOption[]
): ImageSettings | null {
  const provider =
    options.find((p) => p.id === wanted.provider) ??
    options.find((p) => p.id === 'openai') ??
    options[0];
  if (!provider) return null;
  const models = provider.models.filter((m) => m.available);
  const model =
    models.find((m) => m.id === wanted.model) ??
    models.find((m) => m.id === provider.default_model) ??
    models[0];
  if (!model) return null;
  const ratios = (provider.aspect_ratios ?? []) as string[];
  const aspect = (ASPECT_RATIOS as readonly string[]).includes(
    wanted.aspect_ratio ?? ''
  )
    ? wanted.aspect_ratio!
    : FIRST_BATCH_FORMAT.aspect_ratio;
  const size =
    wanted.image_size && model.image_sizes.includes(wanted.image_size)
      ? wanted.image_size
      : model.image_sizes.includes(FIRST_BATCH_FORMAT.image_size)
        ? FIRST_BATCH_FORMAT.image_size
        : (model.image_sizes[0] as ImageSettings['image_size']);
  const variants = Math.min(
    Math.max(Math.round(wanted.variants ?? 1), 1),
    MAX_VARIANTS
  );
  return {
    provider: provider.id as ImageSettings['provider'],
    model: model.id,
    aspect_ratio: (ratios.length === 0 || ratios.includes(aspect)
      ? aspect
      : FIRST_BATCH_FORMAT.aspect_ratio) as ImageSettings['aspect_ratio'],
    image_size: size,
    variants,
  };
}

/**
 * The ideas' image settings (design §5.3, "出图设置"): which provider and
 * model make new ads, the aspect ratio, the size and how many per way of
 * telling. The server keeps no such setting, so the page remembers the
 * user's choice in this browser; it starts from what the first batch used,
 * so trying a failed image again asks for the drafts that batch prepared.
 */
export function useImageSettings(
  projectId: string,
  detail: IdeasDetail | undefined
): UseImageSettings {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  const caps = useSWR(
    ['marketing-generation-capabilities', current.workspaceId],
    async () => {
      const result = await gateway.getGenerationCapabilities({
        workspaceSlug: current.slug,
      });
      if (!('data' in result) || !result.data)
        throw new Error('marketing-action-failed');
      return result.data;
    },
    { revalidateOnFocus: false }
  );
  // Only an ended batch's settings matter here: until then the batch makes
  // its own images, and the page reads a running batch nowhere else.
  const ended =
    detail?.first_batch?.state === 'done' ||
    detail?.first_batch?.state === 'failed';
  const batchId = ended ? detail!.first_batch!.id : null;
  const batch = useSWR(
    batchId
      ? ['marketing-first-batch-settings', current.workspaceId, batchId]
      : null,
    async () => {
      const result = await gateway.getMarketingFirstBatch({
        workspaceSlug: current.slug,
        firstBatchId: batchId!,
      });
      return result.kind === 'ok' ? result.data : null;
    },
    { revalidateOnFocus: false }
  );

  const options = useMemo(
    () =>
      (caps.data?.provider_options ?? []).filter(
        (p) =>
          p.available &&
          p.execution_mode === 'byok' &&
          p.models.some((m) => m.available)
      ),
    [caps.data]
  );

  const keyName = storageKey(current.workspaceId, projectId);
  const [chosen, setChosen] = useState<Partial<ImageSettings> | null>(null);
  useEffect(() => {
    setChosen(readStored(keyName));
  }, [keyName]);

  const fromBatch = useMemo<Partial<ImageSettings>>(
    () =>
      batch.data
        ? {
            provider: batch.data.image_provider as ImageSettings['provider'],
            model: batch.data.image_model,
            ...FIRST_BATCH_FORMAT,
          }
        : {},
    [batch.data]
  );

  const settings = useMemo(
    () => settle({ ...fromBatch, ...(chosen ?? {}) }, options),
    [chosen, fromBatch, options]
  );

  const change = useCallback(
    (next: Partial<ImageSettings>) => {
      const merged = settle({ ...(settings ?? {}), ...next }, options);
      if (!merged) return;
      setChosen(merged);
      writeStored(keyName, merged);
    },
    [keyName, options, settings]
  );

  return { settings, options, change, failed: Boolean(caps.error) };
}
