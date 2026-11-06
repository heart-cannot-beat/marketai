import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { useRouter } from 'next/router';

type BlockerRegistration = {
  isBlocked: () => boolean;
  onDiscard: () => void;
};

type NavigationBlockerContextValue = {
  register: (reg: BlockerRegistration) => () => void;
  navigate: (destination: string) => Promise<boolean>;
  isBlocked: () => boolean;
};

const NavigationBlockerContext =
  createContext<NavigationBlockerContextValue | null>(null);

export function useNavigationBlocker(): NavigationBlockerContextValue {
  const ctx = useContext(NavigationBlockerContext);
  if (!ctx) {
    throw new Error(
      'useNavigationBlocker must be used within NavigationBlockerProvider'
    );
  }
  return ctx;
}

export function useOptionalNavigationBlocker(): NavigationBlockerContextValue | null {
  return useContext(NavigationBlockerContext);
}

export function NavigationBlockerProvider({
  children,
}: {
  children: ReactNode;
}) {
  const router = useRouter();
  const blockersRef = useRef<Set<BlockerRegistration>>(new Set());
  const [, forceRender] = useState(0);
  const navigatingRef = useRef(false);

  const isBlocked = useCallback((): boolean => {
    const entries = Array.from(blockersRef.current);
    for (let i = 0; i < entries.length; i++) {
      if (entries[i].isBlocked()) return true;
    }
    return false;
  }, []);

  const discardAll = useCallback(() => {
    Array.from(blockersRef.current).forEach((reg) => {
      if (reg.isBlocked()) {
        reg.onDiscard();
      }
    });
  }, []);

  const register = useCallback((reg: BlockerRegistration): (() => void) => {
    blockersRef.current.add(reg);
    forceRender((n) => n + 1);
    return () => {
      blockersRef.current.delete(reg);
      forceRender((n) => n + 1);
    };
  }, []);

  const navigate = useCallback(
    async (destination: string): Promise<boolean> => {
      if (!isBlocked()) {
        navigatingRef.current = true;
        try {
          await router.push(destination);
          return true;
        } finally {
          navigatingRef.current = false;
        }
      }

      const confirmed = window.confirm(
        'You have unsaved changes. Discard them and leave this page?'
      );
      if (!confirmed) return false;

      discardAll();
      navigatingRef.current = true;
      try {
        await router.push(destination);
        return true;
      } finally {
        navigatingRef.current = false;
      }
    },
    [isBlocked, discardAll, router]
  );

  useEffect(() => {
    const handleBeforeUnload = (e: BeforeUnloadEvent) => {
      if (isBlocked()) {
        e.preventDefault();
      }
    };
    window.addEventListener('beforeunload', handleBeforeUnload);
    return () => window.removeEventListener('beforeunload', handleBeforeUnload);
  }, [isBlocked]);

  useEffect(() => {
    router.beforePopState(() => {
      if (navigatingRef.current || !isBlocked()) {
        return true;
      }

      const confirmed = window.confirm(
        'You have unsaved changes. Discard them and leave this page?'
      );
      if (confirmed) {
        discardAll();
        return true;
      }

      window.history.pushState(null, '', router.asPath);
      return false;
    });

    return () => {
      router.beforePopState(() => true);
    };
  }, [router, isBlocked, discardAll]);

  const value = useMemo<NavigationBlockerContextValue>(
    () => ({ register, navigate, isBlocked }),
    [register, navigate, isBlocked]
  );

  return (
    <NavigationBlockerContext.Provider value={value}>
      {children}
    </NavigationBlockerContext.Provider>
  );
}
