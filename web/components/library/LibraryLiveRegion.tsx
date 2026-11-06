import {
  createContext,
  useContext,
  useCallback,
  useRef,
  useState,
} from 'react';

interface LiveRegionContextValue {
  announce: (message: string, priority?: 'polite' | 'assertive') => void;
}

const LiveRegionContext = createContext<LiveRegionContextValue>({
  announce: () => {},
});

export function useLibraryAnnounce() {
  return useContext(LiveRegionContext);
}

export function LibraryLiveRegion({ children }: { children: React.ReactNode }) {
  const [politeMessage, setPoliteMessage] = useState('');
  const [assertiveMessage, setAssertiveMessage] = useState('');
  const clearTimerRef = useRef<ReturnType<typeof setTimeout>>();

  const announce = useCallback(
    (message: string, priority: 'polite' | 'assertive' = 'polite') => {
      if (clearTimerRef.current) clearTimeout(clearTimerRef.current);

      if (priority === 'assertive') {
        setAssertiveMessage(message);
      } else {
        setPoliteMessage(message);
      }

      clearTimerRef.current = setTimeout(() => {
        setPoliteMessage('');
        setAssertiveMessage('');
      }, 5000);
    },
    []
  );

  return (
    <LiveRegionContext.Provider value={{ announce }}>
      {children}
      <div
        aria-live="polite"
        aria-atomic="true"
        role="status"
        className="sr-only"
      >
        {politeMessage}
      </div>
      <div
        aria-live="assertive"
        aria-atomic="true"
        role="alert"
        className="sr-only"
      >
        {assertiveMessage}
      </div>
    </LiveRegionContext.Provider>
  );
}
