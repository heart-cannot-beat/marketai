'use client';

import { useMemo, createContext, useContext } from 'react';
import type { ReactNode } from 'react';
import type { DomainGateway } from './gateway';
import { DefaultDomainGateway } from './gateway';
import { CsrfTokenManager } from './csrf';
import { IntentTracker } from './intent';

const GatewayContext = createContext<DomainGateway | null>(null);

/**
 * Pages Router layouts render on the server before hydrating in the browser.
 * Reads are started by SWR after hydration, but the context still needs a
 * gateway instance during that server render. Keep intent state request-local
 * there; the hydrated provider is rebuilt with durable browser storage.
 */
function createRequestLocalStorage(): Storage {
  const values = new Map<string, string>();
  return {
    get length() {
      return values.size;
    },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => Array.from(values.keys())[index] ?? null,
    removeItem: (key) => values.delete(key),
    setItem: (key, value) => values.set(key, String(value)),
  };
}

export function useDomainGateway(): DomainGateway {
  const gw = useContext(GatewayContext);
  if (!gw)
    throw new Error(
      'useDomainGateway must be used within DomainGatewayProvider'
    );
  return gw;
}

export function DomainGatewayProvider({ children }: { children: ReactNode }) {
  const gateway = useMemo(() => {
    const csrf = new CsrfTokenManager({
      ttlMs: 60_000,
    });
    const intents = new IntentTracker({
      storage:
        typeof window === 'undefined'
          ? createRequestLocalStorage()
          : window.localStorage,
    });
    return new DefaultDomainGateway({
      csrfTokenManager: csrf,
      intentTracker: intents,
    });
  }, []);

  return (
    <GatewayContext.Provider value={gateway}>
      {children}
    </GatewayContext.Provider>
  );
}
