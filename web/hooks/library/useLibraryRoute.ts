import { useMemo, useCallback } from 'react';
import { useRouter } from 'next/router';
import { useWorkspace } from '@/components/workspace/WorkspaceProvider';
import { useOptionalNavigationBlocker } from '@/components/workspace/NavigationBlocker';
import type {
  LibraryCategory,
  LibraryEntryType,
} from '@/lib/library/navigation';
import type { LibraryRouteState, LibraryTab } from '@/lib/library/route-codec';
import {
  parseLibraryRoute,
  normalizeLibraryRoute,
  buildLibraryPath,
  routesEqual,
} from '@/lib/library/route-codec';

export interface UseLibraryRouteResult {
  state: LibraryRouteState;
  setCategory: (cat: LibraryCategory) => void;
  setEntry: (entryId: string | null, tab?: LibraryTab | null) => void;
  setTab: (tab: LibraryTab | null) => void;
  setVersion: (versionId: string | null) => void;
  setResource: (
    resourceType: LibraryEntryType | null,
    resourceId: string | null,
    parentType?: LibraryEntryType | null,
    parentId?: string | null
  ) => void;
  setResourceTab: (tab: LibraryTab | null) => void;
  setResourceVersion: (versionId: string | null) => void;
}

export function useLibraryRoute(): UseLibraryRouteResult {
  const router = useRouter();
  const { current } = useWorkspace();
  const blocker = useOptionalNavigationBlocker();

  const state = useMemo(
    () => normalizeLibraryRoute(parseLibraryRoute(router.query)),
    [router.query]
  );

  const push = useCallback(
    (next: LibraryRouteState) => {
      const normalized = normalizeLibraryRoute(next);
      if (routesEqual(normalized, state)) return;

      const path = buildLibraryPath(current.slug, normalized);
      if (blocker) {
        blocker.navigate(path);
      } else {
        router.push(path);
      }
    },
    [state, current.slug, blocker, router]
  );

  const setCategory = useCallback(
    (cat: LibraryCategory) => {
      push({
        cat,
        entry: null,
        tab: null,
        version: null,
        resource_type: null,
        resource: null,
        resource_tab: null,
        resource_version: null,
        parent_type: null,
        parent: null,
      });
    },
    [push]
  );

  const setEntry = useCallback(
    (entryId: string | null, tab?: LibraryTab | null) => {
      push({
        ...state,
        entry: entryId,
        tab: tab ?? (entryId ? state.tab : null),
        version: null,
        resource_type: null,
        resource: null,
        resource_tab: null,
        resource_version: null,
        parent_type: null,
        parent: null,
      });
    },
    [state, push]
  );

  const setTab = useCallback(
    (tab: LibraryTab | null) => {
      const clearVersion =
        tab !== 'versions' && tab !== 'impact' && tab !== 'bindings';
      push({
        ...state,
        tab,
        version: clearVersion ? null : state.version,
      });
    },
    [state, push]
  );

  const setVersion = useCallback(
    (versionId: string | null) => {
      push({
        ...state,
        version: versionId,
      });
    },
    [state, push]
  );

  const setResource = useCallback(
    (
      resourceType: LibraryEntryType | null,
      resourceId: string | null,
      parentType?: LibraryEntryType | null,
      parentId?: string | null
    ) => {
      push({
        ...state,
        resource_type: resourceType,
        resource: resourceId,
        resource_tab: null,
        resource_version: null,
        parent_type: parentType ?? null,
        parent: parentId ?? null,
      });
    },
    [state, push]
  );

  const setResourceTab = useCallback(
    (tab: LibraryTab | null) => {
      const clearVersion =
        tab !== 'versions' && tab !== 'impact' && tab !== 'bindings';
      push({
        ...state,
        resource_tab: tab,
        resource_version: clearVersion ? null : state.resource_version,
      });
    },
    [state, push]
  );

  const setResourceVersion = useCallback(
    (versionId: string | null) => {
      push({
        ...state,
        resource_version: versionId,
      });
    },
    [state, push]
  );

  return {
    state,
    setCategory,
    setEntry,
    setTab,
    setVersion,
    setResource,
    setResourceTab,
    setResourceVersion,
  };
}
