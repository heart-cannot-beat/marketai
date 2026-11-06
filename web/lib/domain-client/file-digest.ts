/**
 * `FileDigest` — browser-only SHA-256 hashing for the upload flow's Phase 1
 * (spec §9.3). `crypto.subtle.digest()` is one-shot: it must be handed the
 * complete input in a single call, so hashing a File through it directly
 * would require materializing the whole file in memory first (the exact
 * `file.arrayBuffer()` anti-pattern this module exists to avoid). Instead,
 * `BrowserFileDigest` reads the File through its `ReadableStream`
 * (`file.stream()`) and feeds each chunk into a hand-rolled incremental
 * SHA-256 implementation (FIPS 180-4) — memory use is bounded by the
 * constructor's `chunkSize`, not by the file's total size, whether the file
 * is a kilobyte or several gigabytes.
 */

export interface FileDigest {
  computeSha256(
    file: File,
    onProgress?: (fraction: number) => void
  ): Promise<string>;
}

// SHA-256 round constants (FIPS 180-4 §4.2.2) — the first 32 bits of the
// fractional parts of the cube roots of the first 64 primes.
const K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
  0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
  0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
  0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
  0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
  0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
  0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
  0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
  0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

function rotr(x: number, n: number): number {
  return (x >>> n) | (x << (32 - n));
}

/**
 * Streaming SHA-256 hasher: `update()` may be called any number of times
 * with chunks of any size — only a &lt;64-byte carry buffer plus the running
 * 8-word state are retained between calls, so total memory never grows with
 * the amount of data hashed so far.
 */
class IncrementalSha256 {
  private readonly h = new Uint32Array([
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c,
    0x1f83d9ab, 0x5be0cd19,
  ]);
  private readonly pending = new Uint8Array(64);
  private pendingLength = 0;
  // Total bytes hashed so far. A `number` is exact up to 2^53 bytes — far
  // beyond any file this module will ever be asked to hash.
  private totalBytes = 0;
  private readonly w = new Uint32Array(64);

  update(chunk: Uint8Array): void {
    this.totalBytes += chunk.byteLength;
    let offset = 0;

    if (this.pendingLength > 0) {
      const needed = 64 - this.pendingLength;
      const take = Math.min(needed, chunk.byteLength);
      this.pending.set(chunk.subarray(0, take), this.pendingLength);
      this.pendingLength += take;
      offset += take;
      if (this.pendingLength === 64) {
        this.processBlock(this.pending, 0);
        this.pendingLength = 0;
      }
    }

    while (offset + 64 <= chunk.byteLength) {
      this.processBlock(chunk, offset);
      offset += 64;
    }

    if (offset < chunk.byteLength) {
      const remaining = chunk.byteLength - offset;
      this.pending.set(chunk.subarray(offset), 0);
      this.pendingLength = remaining;
    }
  }

  digest(): Uint8Array {
    const bitLength = this.totalBytes * 8;
    const finalBlockCount = this.pendingLength < 56 ? 1 : 2;
    const padded = new Uint8Array(finalBlockCount * 64);
    padded.set(this.pending.subarray(0, this.pendingLength));
    padded[this.pendingLength] = 0x80;

    const view = new DataView(padded.buffer);
    // 64-bit big-endian bit-length trailer, split into two 32-bit halves —
    // `bitLength` stays within `Number.MAX_SAFE_INTEGER` for any realistic
    // file, so this split never loses precision.
    const high = Math.floor(bitLength / 0x100000000);
    const low = bitLength >>> 0;
    view.setUint32(padded.length - 8, high, false);
    view.setUint32(padded.length - 4, low, false);

    for (let block = 0; block < finalBlockCount; block++) {
      this.processBlock(padded, block * 64);
    }

    const out = new Uint8Array(32);
    const outView = new DataView(out.buffer);
    for (let i = 0; i < 8; i++) {
      outView.setUint32(i * 4, this.h[i], false);
    }
    return out;
  }

  private processBlock(block: Uint8Array, blockOffset: number): void {
    const view = new DataView(block.buffer, block.byteOffset + blockOffset, 64);
    const w = this.w;
    for (let i = 0; i < 16; i++) {
      w[i] = view.getUint32(i * 4, false);
    }
    for (let i = 16; i < 64; i++) {
      const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
      const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
    }

    let a = this.h[0];
    let b = this.h[1];
    let c = this.h[2];
    let d = this.h[3];
    let e = this.h[4];
    let f = this.h[5];
    let g = this.h[6];
    let h = this.h[7];
    for (let i = 0; i < 64; i++) {
      const s1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const temp1 = (h + s1 + ch + K[i] + w[i]) >>> 0;
      const s0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (s0 + maj) >>> 0;

      h = g;
      g = f;
      f = e;
      e = (d + temp1) >>> 0;
      d = c;
      c = b;
      b = a;
      a = (temp1 + temp2) >>> 0;
    }

    this.h[0] = (this.h[0] + a) >>> 0;
    this.h[1] = (this.h[1] + b) >>> 0;
    this.h[2] = (this.h[2] + c) >>> 0;
    this.h[3] = (this.h[3] + d) >>> 0;
    this.h[4] = (this.h[4] + e) >>> 0;
    this.h[5] = (this.h[5] + f) >>> 0;
    this.h[6] = (this.h[6] + g) >>> 0;
    this.h[7] = (this.h[7] + h) >>> 0;
  }
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = '';
  for (let i = 0; i < bytes.length; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

export class BrowserFileDigest implements FileDigest {
  constructor(private readonly chunkSize = 64 * 1024) {
    // `chunkSize` bounds how much of a single manual read this adapter will
    // ever buffer at once when the runtime's `File.stream()` yields chunks
    // larger than the caller wants to hold — see `readInChunks()` below.
    if (this.chunkSize <= 0) {
      throw new Error('chunkSize must be positive');
    }
  }

  async computeSha256(
    file: File,
    onProgress?: (fraction: number) => void
  ): Promise<string> {
    const hasher = new IncrementalSha256();
    const total = file.size;
    let bytesRead = 0;

    const reportProgress = () => {
      // A zero-byte file still reports a single, final `1` — there is no
      // meaningful partial progress to report before that.
      onProgress?.(total === 0 ? 1 : bytesRead / total);
    };

    for await (const chunk of this.readInChunks(file)) {
      hasher.update(chunk);
      bytesRead += chunk.byteLength;
      reportProgress();
    }

    if (total === 0) {
      reportProgress();
    }

    return bytesToBase64(hasher.digest());
  }

  /**
   * Yields the File's bytes as bounded-size `Uint8Array` chunks without ever
   * holding more than one chunk (plus the native stream's own internal
   * buffer) in memory at a time. Reads through `file.stream()` — never
   * `file.arrayBuffer()` on the *whole file*, which would force it entirely
   * into memory before hashing starts. A stream chunk larger than
   * `chunkSize` is split further so the caller-visible chunk size stays
   * bounded even against a runtime whose stream implementation yields large
   * buffers.
   *
   * `File.stream()` (and even `Blob.arrayBuffer()`) is unimplemented by
   * jsdom's minimal `Blob` polyfill — the DOM environment this module's own
   * jest suite runs under only implements `slice()`/`size`/`type`. When
   * `stream()` is missing, this falls back to reading fixed-size
   * `file.slice()` windows one at a time through whichever read primitive
   * the runtime actually offers (`Blob.arrayBuffer()`, or `FileReader` where
   * even that is absent). Either fallback stays bounded by `chunkSize`,
   * since only one slice's bytes are ever materialized at once — it is a
   * fallback for API availability, not a relaxation of the memory bound.
   */
  private async *readInChunks(file: File): AsyncGenerator<Uint8Array> {
    if (typeof file.stream === 'function') {
      const reader = file.stream().getReader();
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) {
            return;
          }
          let offset = 0;
          while (offset < value.byteLength) {
            const end = Math.min(offset + this.chunkSize, value.byteLength);
            yield value.subarray(offset, end);
            offset = end;
          }
        }
      } finally {
        reader.releaseLock();
      }
      return;
    }

    let offset = 0;
    while (offset < file.size) {
      const end = Math.min(offset + this.chunkSize, file.size);
      const buffer = await readBlobAsArrayBuffer(file.slice(offset, end));
      yield new Uint8Array(buffer);
      offset = end;
    }
  }
}

/**
 * Reads one Blob slice fully into memory — safe here because callers only
 * ever pass a `chunkSize`-bounded slice, never the whole File. Prefers the
 * standard `Blob.arrayBuffer()`; falls back to `FileReader.readAsArrayBuffer`
 * for DOM environments (e.g. jsdom) whose `Blob` polyfill omits it.
 */
function readBlobAsArrayBuffer(blob: Blob): Promise<ArrayBuffer> {
  if (typeof blob.arrayBuffer === 'function') {
    return blob.arrayBuffer();
  }

  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as ArrayBuffer);
    reader.onerror = () =>
      reject(reader.error ?? new Error('FileReader failed to read chunk'));
    reader.readAsArrayBuffer(blob);
  });
}

/**
 * Deterministic test double (spec §9.3) — returns a fixed digest without
 * touching `crypto.subtle` or `File.stream()`, so hook/component tests can
 * assert on upload behavior without depending on real hashing.
 */
export class TestFileDigest implements FileDigest {
  constructor(private readonly fixedHash = 'test-sha256-hash-base64') {}

  async computeSha256(
    _file: File,
    onProgress?: (fraction: number) => void
  ): Promise<string> {
    onProgress?.(1);
    return this.fixedHash;
  }
}
