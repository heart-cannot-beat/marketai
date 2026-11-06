/**
 * @jest-environment jsdom
 */
import {
  BrowserFileDigest,
  TestFileDigest,
} from '@/lib/domain-client/file-digest';

describe('BrowserFileDigest', () => {
  it('computes SHA-256 base64 for a file', async () => {
    const file = new File(['hello world'], 'test.txt', { type: 'text/plain' });
    const digest = new BrowserFileDigest(1024);
    const hash = await digest.computeSha256(file);
    // SHA-256 of 'hello world' in base64
    expect(hash).toBe('uU0nuZNNPgilLlLX2n2r+sSE7+N6U4DukIj3rOLvzek=');
  });

  it('produces the same digest for the same file', async () => {
    const file = new File(['deterministic'], 'same.txt');
    const digest = new BrowserFileDigest(512);
    const h1 = await digest.computeSha256(file);
    const h2 = await digest.computeSha256(file);
    expect(h1).toBe(h2);
  });

  it('reports progress up to 1', async () => {
    const content = new Uint8Array(4096);
    const file = new File([content], 'big.bin');
    const digest = new BrowserFileDigest(1024);
    const progressValues: number[] = [];
    await digest.computeSha256(file, (p) => progressValues.push(p));
    expect(progressValues.length).toBeGreaterThan(0);
    expect(progressValues[progressValues.length - 1]).toBe(1);
  });

  it('produces a digest that changes when chunkSize differs but content is the same', async () => {
    const content = new Uint8Array(10000).map((_, i) => i % 256);
    const fileA = new File([content], 'a.bin');
    const fileB = new File([content], 'b.bin');
    const smallChunks = new BrowserFileDigest(37); // deliberately misaligned chunk size
    const bigChunks = new BrowserFileDigest(8192);
    const hashA = await smallChunks.computeSha256(fileA);
    const hashB = await bigChunks.computeSha256(fileB);
    expect(hashA).toBe(hashB);
  });

  it('hashes an empty file and reports a final progress of 1', async () => {
    const file = new File([], 'empty.txt');
    const digest = new BrowserFileDigest(1024);
    const progressValues: number[] = [];
    const hash = await digest.computeSha256(file, (p) =>
      progressValues.push(p)
    );
    // SHA-256 of the empty string
    expect(hash).toBe('47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=');
    expect(progressValues[progressValues.length - 1]).toBe(1);
  });

  it('rejects a non-positive chunkSize', () => {
    expect(() => new BrowserFileDigest(0)).toThrow();
  });
});

describe('TestFileDigest', () => {
  it('returns a fixed hash and reports progress of 1 without reading the file', async () => {
    const digest = new TestFileDigest();
    const file = new File(['irrelevant'], 'x.bin');
    const progressValues: number[] = [];
    const hash = await digest.computeSha256(file, (p) =>
      progressValues.push(p)
    );
    expect(hash).toBe('test-sha256-hash-base64');
    expect(progressValues).toEqual([1]);
  });

  it('accepts a custom fixed hash', async () => {
    const digest = new TestFileDigest('custom-hash');
    const hash = await digest.computeSha256(new File(['x'], 'x.bin'));
    expect(hash).toBe('custom-hash');
  });
});
