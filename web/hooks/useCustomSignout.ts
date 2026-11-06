import { useRouter } from 'next/router';
import toast from 'react-hot-toast';
import { assignLocation } from '@/lib/browser-navigation';

interface CustomSignOutOptions {
  /**
   * Leave with a full page load instead of a client-side route change, so
   * every in-memory client cache (SWR data, the BFF CSRF token) is dropped
   * with the session. Workspace pages hold such caches; the account shell
   * does not.
   */
  hardRedirect?: boolean;
}

export function useCustomSignOut({
  hardRedirect = false,
}: CustomSignOutOptions = {}) {
  const router = useRouter();

  const signOut = async () => {
    try {
      const response = await fetch('/api/auth/custom-signout', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
      });

      if (!response.ok) {
        throw new Error('Signout failed');
      }

      if (hardRedirect) {
        assignLocation('/auth/login');
      } else {
        router.push('/auth/login');
      }
    } catch {
      // Upstream printed the raw error to the browser console. Session-bearing
      // failures are not console material; the user gets a message instead.
      toast.error('Could not sign out. Please try again.');
    }
  };

  return signOut;
}
