import http from 'node:http';
import { createServer } from '../server';
import { hashKey } from '../schemas';

let server: http.Server;
let db: ReturnType<typeof import('../ledger').createLedger>;
let baseUrl: string;

beforeAll(async () => {
  const s = createServer(':memory:');
  server = s.server;
  db = s.db;
  await new Promise<void>((resolve) => {
    server.listen(0, () => resolve());
  });
  const addr = server.address() as { port: number };
  baseUrl = `http://localhost:${addr.port}`;
});

afterAll(() => {
  server.close();
  db.close();
});

async function request(
  method: string,
  path: string,
  body?: unknown
): Promise<{ status: number; body: any }> {
  return new Promise((resolve, reject) => {
    const url = new URL(path, baseUrl);
    const req = http.request(url, { method }, (res) => {
      const chunks: Buffer[] = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf8');
        resolve({
          status: res.statusCode!,
          body: text ? JSON.parse(text) : null,
        });
      });
    });
    req.on('error', reject);
    if (body) req.write(JSON.stringify(body));
    req.end();
  });
}

describe('health', () => {
  it('returns ok', async () => {
    const res = await request('GET', '/test/health');
    expect(res.status).toBe(200);
    expect(res.body.status).toBe('ok');
  });
});

describe('POST /provider/generate', () => {
  it('creates a new effect', async () => {
    const res = await request('POST', '/provider/generate', {
      namespace: 'ns1',
      idempotency_key: 'key-1',
      payload: { prompt: 'test' },
    });
    expect(res.status).toBe(201);
    expect(res.body.idempotent_replay).toBe(false);
    expect(res.body.effect_id).toBeDefined();
  });

  it('same key + same fingerprint returns original (idempotent)', async () => {
    const res = await request('POST', '/provider/generate', {
      namespace: 'ns1',
      idempotency_key: 'key-1',
      payload: { prompt: 'test' },
    });
    expect(res.status).toBe(200);
    expect(res.body.idempotent_replay).toBe(true);
  });

  it('same key + different fingerprint returns 409', async () => {
    const res = await request('POST', '/provider/generate', {
      namespace: 'ns1',
      idempotency_key: 'key-1',
      payload: { prompt: 'DIFFERENT' },
    });
    expect(res.status).toBe(409);
    expect(res.body.error).toBe('idempotency_conflict');
  });
});

describe('POST /provider/reconcile', () => {
  it('returns committed effect', async () => {
    const res = await request('POST', '/provider/reconcile', {
      namespace: 'ns1',
      idempotency_key: 'key-1',
    });
    expect(res.status).toBe(200);
    expect(res.body.effect_id).toBeDefined();
  });

  it('returns 404 for unknown key', async () => {
    const res = await request('POST', '/provider/reconcile', {
      namespace: 'ns1',
      idempotency_key: 'key-nonexistent',
    });
    expect(res.status).toBe(404);
  });
});

describe('request_attempts', () => {
  it('records generate and reconcile attempts separately', async () => {
    const keyHash = hashKey('key-1');
    const genRes = await request(
      'GET',
      `/test/runs/ns1/request_attempts?idempotency_key_hash=${keyHash}&request_kind=generate`
    );
    expect(genRes.status).toBe(200);
    expect(genRes.body.attempts.length).toBeGreaterThanOrEqual(1);
    expect(
      genRes.body.attempts.every((a: any) => a.request_kind === 'generate')
    ).toBe(true);

    const recRes = await request(
      'GET',
      `/test/runs/ns1/request_attempts?idempotency_key_hash=${keyHash}&request_kind=reconcile`
    );
    expect(recRes.status).toBe(200);
    expect(recRes.body.attempts.length).toBeGreaterThanOrEqual(1);
    expect(
      recRes.body.attempts.every((a: any) => a.request_kind === 'reconcile')
    ).toBe(true);
  });

  it('requires both idempotency_key_hash and request_kind filters', async () => {
    const res = await request(
      'GET',
      '/test/runs/ns1/request_attempts?idempotency_key_hash=abc'
    );
    expect(res.status).toBe(400);
    expect(res.body.error).toBe('missing_filter');
  });
});

describe('barriers', () => {
  it('creates a barrier and marks it arrived', async () => {
    const createRes = await request('POST', '/test/runs/ns1/barriers', {
      name: 'before-sigkill',
      matchOrdinal: 1,
    });
    expect(createRes.status).toBe(201);

    const checkBefore = await request(
      'GET',
      '/test/runs/ns1/checkpoints/before-sigkill'
    );
    expect(checkBefore.status).toBe(200);
    expect(checkBefore.body.arrived).toBe(false);

    const arriveRes = await request(
      'POST',
      '/test/runs/ns1/barriers/before-sigkill/arrive'
    );
    expect(arriveRes.status).toBe(200);

    const checkAfter = await request(
      'GET',
      '/test/runs/ns1/checkpoints/before-sigkill'
    );
    expect(checkAfter.body.arrived).toBe(true);
  });

  it('checkpoint returns 404 for unknown barrier', async () => {
    const res = await request(
      'GET',
      '/test/runs/ns1/checkpoints/nonexistent'
    );
    expect(res.status).toBe(404);
  });
});

describe('effects', () => {
  it('returns effects without raw idempotency keys', async () => {
    const res = await request('GET', '/test/runs/ns1/effects');
    expect(res.status).toBe(200);
    expect(res.body.effects.length).toBeGreaterThanOrEqual(1);
    for (const effect of res.body.effects) {
      expect(effect).not.toHaveProperty('idempotency_key');
      expect(effect).toHaveProperty('request_fingerprint');
    }
  });
});
