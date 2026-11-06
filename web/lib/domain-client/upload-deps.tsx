/**
 * Dependency-injection seam for `useUpload` (spec §9). `BrowserBlobTransferClient`
 * and `FileDigest` are constructed once at the app root (alongside
 * `DomainGatewayProvider`, see `WorkspaceLayout.tsx`) and threaded through
 * React context — this keeps `useUpload` itself free of any concrete
 * adapter construction, so a test can swap in `TestFileDigest` / a mock
 * `BrowserBlobTransferClient` without reaching into module internals.
 */
import { createContext, useContext } from 'react';
import type { ReactNode } from 'react';
import type { BrowserBlobTransferClient } from './blob-transfer';
import type { FileDigest } from './file-digest';

const BlobClientContext = createContext<BrowserBlobTransferClient | null>(null);
const FileDigestContext = createContext<FileDigest | null>(null);

export function useBlobTransferClient(): BrowserBlobTransferClient {
  const client = useContext(BlobClientContext);
  if (!client) {
    throw new Error(
      'useBlobTransferClient must be used within BlobClientProvider'
    );
  }
  return client;
}

export function useFileDigest(): FileDigest {
  const digest = useContext(FileDigestContext);
  if (!digest) {
    throw new Error('useFileDigest must be used within FileDigestProvider');
  }
  return digest;
}

export function BlobClientProvider({
  client,
  children,
}: {
  client: BrowserBlobTransferClient;
  children: ReactNode;
}) {
  return (
    <BlobClientContext.Provider value={client}>
      {children}
    </BlobClientContext.Provider>
  );
}

export function FileDigestProvider({
  digest,
  children,
}: {
  digest: FileDigest;
  children: ReactNode;
}) {
  return (
    <FileDigestContext.Provider value={digest}>
      {children}
    </FileDigestContext.Provider>
  );
}
