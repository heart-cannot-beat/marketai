import { useEffect, useRef, useState } from 'react';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useDomainGateway } from '@/lib/domain-client/gateway-provider';
import type { DomainGateway } from '@/lib/domain-client/gateway';

export interface TemplateDimensionValue {
  /** Stable value id -- what the server matches rules and tags against. */
  id: string;
  name: string;
}

export interface TemplateDimension {
  /**
   * `<template entry id>:<dimension id>`, the exact key the preview uses for
   * this dimension in `exclusions.rules[].dimensions` and in each item's
   * `dimension_tags` (`app/generation/preview.py`
   * `_materialize_dimension_template_concepts`).
   */
  key: string;
  templateEntryId: string;
  templateName: string;
  dimensionId: string;
  name: string;
  values: TemplateDimensionValue[];
}

export interface UseTemplateDimensionsResult {
  dimensions: TemplateDimension[];
  isLoading: boolean;
  /** Entry ids whose effective version could not be read. */
  failedTemplateIds: string[];
}

interface EntryVersionLike {
  id?: string;
  content?: Record<string, unknown> | null;
}

/**
 * Project a `LibraryEntryDetail` (with its embedded `versions`) into the
 * dimension/value pairs of its *current effective* version -- the version
 * the preview resolves, so the exclusion builder offers exactly the keys
 * and values the server will accept.
 */
export function dimensionsFromEntry(
  entry: {
    id: string;
    display_name?: string;
    current_version_id?: string | null;
    versions?: EntryVersionLike[];
  } | null
): TemplateDimension[] | null {
  if (!entry || !entry.current_version_id) return null;
  const version = (entry.versions ?? []).find(
    (candidate) => candidate.id === entry.current_version_id
  );
  const content = version?.content;
  if (!content || !Array.isArray(content.dimensions)) return null;

  const dimensions: TemplateDimension[] = [];
  for (const raw of content.dimensions as unknown[]) {
    if (!raw || typeof raw !== 'object') continue;
    const dim = raw as Record<string, unknown>;
    const dimensionId = typeof dim.id === 'string' ? dim.id : '';
    if (!dimensionId) continue;
    const values: TemplateDimensionValue[] = [];
    for (const rawValue of Array.isArray(dim.values) ? dim.values : []) {
      if (!rawValue || typeof rawValue !== 'object') continue;
      const value = rawValue as Record<string, unknown>;
      const id = typeof value.id === 'string' ? value.id : '';
      if (!id) continue;
      values.push({
        id,
        name: typeof value.name === 'string' && value.name ? value.name : id,
      });
    }
    dimensions.push({
      key: `${entry.id}:${dimensionId}`,
      templateEntryId: entry.id,
      templateName: entry.display_name ?? entry.id,
      dimensionId,
      name: typeof dim.name === 'string' && dim.name ? dim.name : dimensionId,
      values,
    });
  }
  return dimensions;
}

async function loadTemplateDimensions(
  gateway: DomainGateway,
  workspaceSlug: string,
  entryId: string
): Promise<TemplateDimension[] | null> {
  const result = await gateway.getLibraryEntry({ workspaceSlug, entryId });
  if (result.kind !== 'ok') return null;
  return dimensionsFromEntry(
    result.data as unknown as Parameters<typeof dimensionsFromEntry>[0]
  );
}

/**
 * Resolves the dimensions and values of the draft's selected dimension
 * templates (`selectors.dimension_template_ids`) so the exclusion builder
 * can offer real dimension/value pairs. One `get_library_entry` read per
 * template; the hook count cannot vary with the selection, so this is an
 * effect over the gateway rather than N `useDomainQuery` calls.
 */
export function useTemplateDimensions(
  templateEntryIds: string[]
): UseTemplateDimensionsResult {
  const { current } = useWorkspace();
  const gateway = useDomainGateway();
  // The provider memoises one gateway per app, but the effect below keys on
  // the *selection*, not on the gateway identity -- a re-rendered gateway
  // must never restart the reads (or loop on its own setState).
  const gatewayRef = useRef(gateway);
  gatewayRef.current = gateway;
  const idsKey = JSON.stringify(templateEntryIds);
  const [state, setState] = useState<UseTemplateDimensionsResult>({
    dimensions: [],
    isLoading: templateEntryIds.length > 0,
    failedTemplateIds: [],
  });

  useEffect(() => {
    const ids: string[] = JSON.parse(idsKey);
    if (ids.length === 0) {
      setState({ dimensions: [], isLoading: false, failedTemplateIds: [] });
      return;
    }
    let cancelled = false;
    setState((previous) =>
      previous.isLoading ? previous : { ...previous, isLoading: true }
    );
    Promise.all(
      ids.map((entryId) =>
        loadTemplateDimensions(gatewayRef.current, current.slug, entryId).catch(
          () => null
        )
      )
    ).then((results) => {
      if (cancelled) return;
      const dimensions: TemplateDimension[] = [];
      const failed: string[] = [];
      results.forEach((resolved, index) => {
        if (resolved === null) failed.push(ids[index]);
        else dimensions.push(...resolved);
      });
      setState({ dimensions, isLoading: false, failedTemplateIds: failed });
    });
    return () => {
      cancelled = true;
    };
  }, [current.slug, idsKey]);

  return state;
}
