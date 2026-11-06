/**
 * `BrowserBlobTransferClient` — the browser-only direct-transfer boundary
 * (spec §7.7). The `DomainGateway` only ever exchanges intent/finalize
 * metadata with the BFF; this Module performs Phase 2 of the file lifecycle
 * (§7.2) — the binary POST/PUT to object storage — bypassing Next.js and
 * FastAPI entirely. Binary data never transits Next.js memory.
 *
 * Discriminates on `grant.kind` itself so UI components never branch on
 * storage-adapter shape: S3 uses a presigned POST policy
 * (`content-length-range` enforced at the storage layer); the local adapter
 * uses an HMAC-signed PUT (presigned POST has no meaning for local
 * filesystem storage). Both fetches use `credentials: "omit"` and
 * `redirect: "error"` — the storage layer, not a Next.js/FastAPI session,
 * authorizes the write via the capability alone.
 */
import type { StorageOriginPolicy } from './storage-origin-policy';
import type { components } from './generated/domain-v1';

export type UploadGrantResponse = components['schemas']['UploadGrantResponse'];
export type DownloadGrant = components['schemas']['DownloadGrantResponse'];

export interface BrowserBlobTransferClient {
  upload(grant: UploadGrantResponse, file: Blob): Promise<void>;
  /** Origin-checks the grant before it is handed to a user-initiated navigation — never persisted in browser storage. */
  validatedDownloadUrl(grant: DownloadGrant): URL;
  downloadBlob(
    grant: DownloadGrant,
    options?: { signal?: AbortSignal }
  ): Promise<Blob>;
}

export class DefaultBrowserBlobTransferClient implements BrowserBlobTransferClient {
  constructor(
    private readonly originPolicy: StorageOriginPolicy,
    private readonly fetchFn: typeof fetch = fetch
  ) {}

  async upload(grant: UploadGrantResponse, file: Blob): Promise<void> {
    const url = this.originPolicy.assertAllowed(grant.url);

    if (grant.kind === 'presigned_post') {
      if (!grant.fields) {
        throw new Error('presigned_post grant is missing fields');
      }
      const formData = new FormData();
      for (const [key, value] of Object.entries(grant.fields)) {
        formData.append(key, value);
      }
      // The file field is appended last so every signed policy field
      // precedes it, matching S3's presigned POST field ordering
      // expectations.
      formData.append('file', file);
      const response = await this.fetchFn.call(globalThis, url.toString(), {
        method: 'POST',
        body: formData,
        credentials: 'omit',
        redirect: 'error',
      });
      if (!response.ok) {
        throw new Error(`Upload failed: ${response.status}`);
      }
      return;
    }

    if (grant.kind === 'signed_put') {
      if (!grant.headers) {
        throw new Error('signed_put grant is missing headers');
      }
      const response = await this.fetchFn.call(globalThis, url.toString(), {
        method: 'PUT',
        body: file,
        headers: grant.headers,
        credentials: 'omit',
        redirect: 'error',
      });
      if (!response.ok) {
        throw new Error(`Upload failed: ${response.status}`);
      }
      return;
    }

    throw new Error(`Unknown upload grant kind: ${String(grant.kind)}`);
  }

  validatedDownloadUrl(grant: DownloadGrant): URL {
    return this.originPolicy.assertAllowed(grant.url);
  }

  async downloadBlob(
    grant: DownloadGrant,
    options?: { signal?: AbortSignal }
  ): Promise<Blob> {
    const url = this.originPolicy.assertAllowed(grant.url);
    const response = await this.fetchFn.call(globalThis, url.toString(), {
      method: 'GET',
      credentials: 'omit',
      redirect: 'error',
      signal: options?.signal,
    });
    if (!response.ok) {
      throw new Error(`Download failed: ${response.status}`);
    }
    return response.blob();
  }
}
