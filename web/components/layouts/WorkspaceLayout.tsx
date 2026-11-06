import { useEffect } from 'react';
import { useRouter } from 'next/router';
import { useTranslation } from 'next-i18next';
import { SWRConfig } from 'swr';
import { DomainGatewayProvider } from '@/lib/domain-client/gateway-provider';
import { retryTransientProblems } from '@/lib/domain-client/transient-problems';
import { WorkspaceProvider } from '@/components/workspace/WorkspaceProvider';
import { NavigationBlockerProvider } from '@/components/workspace/NavigationBlocker';
import { DefaultBrowserBlobTransferClient } from '@/lib/domain-client/blob-transfer';
import { BrowserFileDigest } from '@/lib/domain-client/file-digest';
import { StorageOriginPolicyImpl } from '@/lib/domain-client/storage-origin-policy';
import { parseStorageOrigins } from '@/lib/domain-client/storage-origin-parser';
import {
  BlobClientProvider,
  FileDigestProvider,
} from '@/lib/domain-client/upload-deps';
import { WorkspaceSelector } from '@/components/workspace/WorkspaceSelector';
import { WorkspaceNavigation } from '@/components/workspace/WorkspaceNavigation';
import { WorkspaceAccountMenu } from '@/components/workspace/WorkspaceAccountMenu';
import { WorkspaceRouteBoundary } from '@/components/workspace/WorkspaceRouteBoundary';
import { ForbiddenState } from '@/components/workspace/ForbiddenState';
import Brand from '@/components/shared/shell/Brand';
import { LanguageSwitcher } from '@/components/shared/LanguageSwitcher';
import type { WorkspaceBootstrap } from '@/lib/workspace-bootstrap';

interface WorkspaceLayoutProps {
  children: React.ReactNode;
  bootstrap: WorkspaceBootstrap | null;
  errorCode?: number;
  errorContext?: string;
}

const storageOrigins = parseStorageOrigins(
  process.env.NEXT_PUBLIC_MARKETAI_STORAGE_ORIGINS ?? '',
  {
    production: process.env.NODE_ENV === 'production',
    allowInsecureLoopback:
      process.env.NEXT_PUBLIC_MARKETAI_ALLOW_INSECURE_LOOPBACK_STORAGE ===
      'true',
  }
);
const blobTransferClient = new DefaultBrowserBlobTransferClient(
  new StorageOriginPolicyImpl(storageOrigins)
);
const fileDigest = new BrowserFileDigest();
// A read that throws a transient access problem retries every 5 seconds
// (#309); everything else keeps SWR's backoff.
const swrConfig = {
  revalidateOnFocus: false,
  onErrorRetry: retryTransientProblems,
};

function WorkspaceLayoutInner({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const { t } = useTranslation('common');
  const slug = router.query.slug as string;

  // `children` is an opaque `ReactNode` — the page's own <h1> lives inside
  // it, out of reach of a ref declared here. Route focus is therefore wired
  // by querying the rendered DOM after each navigation rather than via a
  // ref, mirroring what `ForbiddenState` already does locally for its own
  // heading. Pages do not need `tabIndex`/`ref` on their `<h1>` themselves —
  // this effect adds `tabindex="-1"` dynamically so the heading is
  // programmatically focusable without being in the normal Tab order.
  useEffect(() => {
    const h1 = document.querySelector('h1');
    if (h1) {
      h1.setAttribute('tabindex', '-1');
      (h1 as HTMLElement).focus();
    }
  }, [router.asPath]);

  return (
    <div className="flex min-h-screen">
      {/*
        Skip-to-content must be the first focusable element in the DOM so it
        is the very first Tab stop from page load — it is rendered ahead of
        the sidebar here rather than inside the "Main" column below, which
        would put the sidebar's WorkspaceSelector/nav links first in Tab
        order instead.
      */}
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:absolute focus:z-50 focus:p-4"
      >
        {t('workspace-skip-to-content')}
      </a>

      {/* Desktop sidebar */}
      <div className="hidden lg:fixed lg:inset-y-0 lg:flex lg:w-64 lg:flex-col">
        <div className="flex grow flex-col gap-y-5 overflow-y-auto border-r border-gray-200 bg-white px-4 pb-4">
          <Brand />
          <WorkspaceSelector />
          <nav className="flex flex-1 flex-col">
            <WorkspaceNavigation slug={slug} activePathname={router.asPath} />
          </nav>
          <div className="space-y-2 border-t border-gray-200 pt-3">
            <LanguageSwitcher className="px-2" />
            <WorkspaceAccountMenu placement="up" />
          </div>
        </div>
      </div>

      {/* Main */}
      <div className="lg:pl-64 flex-1 min-w-0">
        {/* Below lg the sidebar is hidden; keep the account menu (and sign
            out) reachable from a slim bar instead. */}
        <div className="sticky top-0 z-40 flex items-center justify-between gap-x-4 border-b border-gray-200 bg-white px-4 py-2 lg:hidden">
          <div className="[&>div]:pt-0">
            <Brand />
          </div>
          <div className="flex min-w-0 flex-1 items-center justify-end gap-x-2">
            <LanguageSwitcher compact />
            <div className="min-w-0 max-w-56 flex-1">
              <WorkspaceAccountMenu placement="down" />
            </div>
          </div>
        </div>
        {/* `tabIndex={-1}` makes the skip link's `#main-content` fragment
            target programmatically focusable — without it, activating the
            skip link scrolls here but does not move DOM focus, since a
            `<main>` element is not focusable by default. */}
        <main id="main-content" tabIndex={-1} className="py-5 outline-none">
          <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
            <WorkspaceRouteBoundary resetKey={router.asPath}>
              {children}
            </WorkspaceRouteBoundary>
          </div>
        </main>
      </div>
    </div>
  );
}

export default function WorkspaceLayout({
  children,
  bootstrap,
  errorCode,
  errorContext,
}: WorkspaceLayoutProps) {
  if (errorCode === 403 || !bootstrap) {
    return (
      <SWRConfig value={swrConfig}>
        <ForbiddenState
          context={errorContext as 'no-membership' | 'owner-only' | undefined}
        />
      </SWRConfig>
    );
  }

  return (
    <SWRConfig value={swrConfig}>
      <DomainGatewayProvider>
        <BlobClientProvider client={blobTransferClient}>
          <FileDigestProvider digest={fileDigest}>
            <WorkspaceProvider bootstrap={bootstrap}>
              <NavigationBlockerProvider>
                <WorkspaceLayoutInner>{children}</WorkspaceLayoutInner>
              </NavigationBlockerProvider>
            </WorkspaceProvider>
          </FileDigestProvider>
        </BlobClientProvider>
      </DomainGatewayProvider>
    </SWRConfig>
  );
}
