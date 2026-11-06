import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
} from 'react';
import type { ReactNode } from 'react';
import { useAssetsUrlState } from '@/hooks/assets/useAssetsUrlState';
import type { UseAssetsUrlStateResult } from '@/hooks/assets/useAssetsUrlState';

export interface AssetSelection {
  selectedIds: Set<string>;
  toggle: (id: string) => void;
  selectAll: (ids: string[]) => void;
  clear: () => void;
  isSelected: (id: string) => boolean;
  count: number;
}

export interface AssetsContextValue {
  urlState: UseAssetsUrlStateResult;
  selection: AssetSelection;
}

const AssetsContext = createContext<AssetsContextValue | null>(null);

export function useAssets(): AssetsContextValue {
  const ctx = useContext(AssetsContext);
  if (!ctx) {
    throw new Error('useAssets must be used within AssetsProvider');
  }
  return ctx;
}

export function AssetsProvider({ children }: { children: ReactNode }) {
  const urlState = useAssetsUrlState();
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  const toggle = useCallback((id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }, []);

  const selectAll = useCallback((ids: string[]) => {
    setSelectedIds(new Set(ids));
  }, []);

  const clear = useCallback(() => {
    setSelectedIds(new Set());
  }, []);

  const isSelected = useCallback(
    (id: string) => selectedIds.has(id),
    [selectedIds]
  );

  const selection = useMemo<AssetSelection>(
    () => ({
      selectedIds,
      toggle,
      selectAll,
      clear,
      isSelected,
      count: selectedIds.size,
    }),
    [selectedIds, toggle, selectAll, clear, isSelected]
  );

  const value = useMemo<AssetsContextValue>(
    () => ({ urlState, selection }),
    [urlState, selection]
  );

  return (
    <AssetsContext.Provider value={value}>{children}</AssetsContext.Provider>
  );
}
