import { useEffect, useRef } from 'react';
import { useRouter } from 'next/router';

/**
 * Brings an element into view when the page was opened at its anchor
 * (`#anchor`): the ideas page links straight to parts of the product
 * profile (#233). A link inside the app lands before the route change has
 * finished settling the page, so it is tried once that has finished too.
 */
export function useArrivalScroll<T extends HTMLElement>(
  anchor: string,
  enabled = true
) {
  const ref = useRef<T>(null);
  const router = useRouter();
  useEffect(() => {
    if (typeof window === 'undefined' || !enabled) return;
    const bring = () => {
      if (window.location.hash === `#${anchor}`)
        ref.current?.scrollIntoView?.({ block: 'start' });
    };
    const timer = window.setTimeout(bring, 0);
    router.events?.on('routeChangeComplete', bring);
    return () => {
      window.clearTimeout(timer);
      router.events?.off('routeChangeComplete', bring);
    };
  }, [router.events, anchor, enabled]);
  return ref;
}
