import http from 'node:http';
import { createLedger } from './ledger';
import { hashKey, fingerprint } from './schemas';
import type Database from 'better-sqlite3';

const PORT = parseInt(process.env.PORT || '9090', 10);
const DB_PATH = process.env.LEDGER_PATH || ':memory:';

let db: Database.Database;

function readBody(req: http.IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

function json(res: http.ServerResponse, status: number, body: unknown) {
  res.writeHead(status, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(body));
}

function routeMatch(
  method: string,
  url: string,
  pattern: string
): Record<string, string> | null {
  if (method !== pattern.split(' ')[0]) return null;
  const patternPath = pattern.split(' ')[1];
  const patternParts = patternPath.split('/');
  const urlParts = url.split('?')[0].split('/');
  if (patternParts.length !== urlParts.length) return null;
  const params: Record<string, string> = {};
  for (let i = 0; i < patternParts.length; i++) {
    if (patternParts[i].startsWith(':')) {
      params[patternParts[i].slice(1)] = urlParts[i];
    } else if (patternParts[i] !== urlParts[i]) {
      return null;
    }
  }
  return params;
}

async function handleRequest(
  req: http.IncomingMessage,
  res: http.ServerResponse
) {
  const method = req.method || 'GET';
  const url = req.url || '/';
  let params: Record<string, string> | null;

  // Health
  if (method === 'GET' && url === '/test/health') {
    return json(res, 200, { status: 'ok' });
  }

  // POST /provider/generate
  if (method === 'POST' && url === '/provider/generate') {
    const body = JSON.parse(await readBody(req));
    const { namespace, idempotency_key, run_item, effect_kind, payload } = body;
    const keyHash = hashKey(idempotency_key);
    const fp = fingerprint(payload);

    db.prepare(
      `INSERT INTO request_attempts (namespace, idempotency_key_hash, request_kind, request_fingerprint)
       VALUES (?, ?, 'generate', ?)`
    ).run(namespace, keyHash, fp);

    const existing = db
      .prepare(
        'SELECT * FROM effects WHERE namespace = ? AND idempotency_key = ?'
      )
      .get(namespace, idempotency_key) as any;

    if (existing) {
      if (existing.request_fingerprint === fp) {
        return json(res, 200, {
          effect_id: existing.id,
          idempotent_replay: true,
          effect_data: existing.effect_data
            ? JSON.parse(existing.effect_data)
            : null,
        });
      }
      return json(res, 409, {
        error: 'idempotency_conflict',
        message: 'Same key, different fingerprint',
      });
    }

    const effectData = { generated: true, fingerprint: fp };
    const result = db
      .prepare(
        `INSERT INTO effects (namespace, idempotency_key, request_fingerprint, run_item, effect_kind, effect_data)
         VALUES (?, ?, ?, ?, ?, ?)`
      )
      .run(
        namespace,
        idempotency_key,
        fp,
        run_item || null,
        effect_kind || null,
        JSON.stringify(effectData)
      );

    return json(res, 201, {
      effect_id: result.lastInsertRowid,
      idempotent_replay: false,
      effect_data: effectData,
    });
  }

  // POST /provider/reconcile
  if (method === 'POST' && url === '/provider/reconcile') {
    const body = JSON.parse(await readBody(req));
    const { namespace, idempotency_key } = body;
    const keyHash = hashKey(idempotency_key);
    const fp = keyHash.slice(0, 16);

    db.prepare(
      `INSERT INTO request_attempts (namespace, idempotency_key_hash, request_kind, request_fingerprint)
       VALUES (?, ?, 'reconcile', ?)`
    ).run(namespace, keyHash, fp);

    const existing = db
      .prepare(
        'SELECT * FROM effects WHERE namespace = ? AND idempotency_key = ?'
      )
      .get(namespace, idempotency_key) as any;

    if (!existing) {
      return json(res, 404, {
        error: 'not_found',
        message: 'No committed effect for this key',
      });
    }

    return json(res, 200, {
      effect_id: existing.id,
      effect_data: existing.effect_data
        ? JSON.parse(existing.effect_data)
        : null,
    });
  }

  // POST /test/runs/:namespace/barriers
  params = routeMatch(method, url, 'POST /test/runs/:namespace/barriers');
  if (params) {
    const body = JSON.parse(await readBody(req));
    db.prepare(
      `INSERT OR REPLACE INTO barriers (namespace, name, match_ordinal)
       VALUES (?, ?, ?)`
    ).run(params.namespace, body.name, body.matchOrdinal ?? null);
    return json(res, 201, { name: body.name });
  }

  // POST /test/runs/:namespace/barriers/:name/arrive
  params = routeMatch(
    method,
    url,
    'POST /test/runs/:namespace/barriers/:name/arrive'
  );
  if (params) {
    db.prepare(
      `UPDATE barriers SET arrived = 1 WHERE namespace = ? AND name = ?`
    ).run(params.namespace, params.name);
    return json(res, 200, { arrived: true });
  }

  // GET /test/runs/:namespace/checkpoints/:name
  params = routeMatch(
    method,
    url,
    'GET /test/runs/:namespace/checkpoints/:name'
  );
  if (params) {
    const barrier = db
      .prepare(
        'SELECT * FROM barriers WHERE namespace = ? AND name = ?'
      )
      .get(params.namespace, params.name) as any;
    if (!barrier) {
      return json(res, 404, { error: 'not_found' });
    }
    return json(res, 200, {
      name: barrier.name,
      arrived: barrier.arrived === 1,
      matchOrdinal: barrier.match_ordinal,
    });
  }

  // DELETE /test/runs/:namespace/barriers/:name
  params = routeMatch(
    method,
    url,
    'DELETE /test/runs/:namespace/barriers/:name'
  );
  if (params) {
    db.prepare(
      'DELETE FROM barriers WHERE namespace = ? AND name = ?'
    ).run(params.namespace, params.name);
    return json(res, 204, null);
  }

  // GET /test/runs/:namespace/effects
  params = routeMatch(method, url, 'GET /test/runs/:namespace/effects');
  if (params) {
    const effects = db
      .prepare(
        `SELECT id, namespace, request_fingerprint, run_item, effect_kind, success, created_at
         FROM effects WHERE namespace = ?`
      )
      .all(params.namespace);
    return json(res, 200, { effects });
  }

  // GET /test/runs/:namespace/request_attempts
  params = routeMatch(
    method,
    url,
    'GET /test/runs/:namespace/request_attempts'
  );
  if (params) {
    const query = new URL(url, 'http://localhost').searchParams;
    const keyHash = query.get('idempotency_key_hash');
    const requestKind = query.get('request_kind');
    if (!keyHash || !requestKind) {
      return json(res, 400, {
        error: 'missing_filter',
        message: 'Both idempotency_key_hash and request_kind are required',
      });
    }
    const attempts = db
      .prepare(
        `SELECT * FROM request_attempts
         WHERE namespace = ? AND idempotency_key_hash = ? AND request_kind = ?
         ORDER BY sequence`
      )
      .all(params.namespace, keyHash, requestKind);
    return json(res, 200, { attempts });
  }

  json(res, 404, { error: 'not_found' });
}

export interface ControlledProviderInstance {
  server: http.Server;
  db: Database.Database;
}

export function createServer(ledgerPath?: string): ControlledProviderInstance {
  db = createLedger(ledgerPath || DB_PATH);
  const server = http.createServer((req, res) => {
    handleRequest(req, res).catch((err) => {
      json(res, 500, { error: 'internal', message: (err as Error).message });
    });
  });
  return { server, db };
}

if (require.main === module) {
  const { server } = createServer();
  server.listen(PORT, () => {
    process.stdout.write(`controlled-provider listening on :${PORT}\n`);
  });
}
