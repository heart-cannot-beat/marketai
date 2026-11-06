import Database from 'better-sqlite3';

export function createLedger(dbPath: string): Database.Database {
  const db = new Database(dbPath);
  db.pragma('journal_mode = WAL');
  db.exec(`
    CREATE TABLE IF NOT EXISTS effects (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      namespace TEXT NOT NULL,
      idempotency_key TEXT NOT NULL,
      request_fingerprint TEXT NOT NULL,
      run_item TEXT,
      effect_kind TEXT,
      effect_data TEXT,
      success INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      UNIQUE (namespace, idempotency_key)
    );

    CREATE TABLE IF NOT EXISTS request_attempts (
      sequence INTEGER PRIMARY KEY AUTOINCREMENT,
      namespace TEXT NOT NULL,
      idempotency_key_hash TEXT NOT NULL,
      request_kind TEXT NOT NULL CHECK (request_kind IN ('generate', 'reconcile')),
      request_fingerprint TEXT NOT NULL,
      received_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_request_attempts_key_kind
      ON request_attempts (namespace, idempotency_key_hash, request_kind);

    CREATE TABLE IF NOT EXISTS barriers (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      namespace TEXT NOT NULL,
      name TEXT NOT NULL,
      arrived INTEGER NOT NULL DEFAULT 0,
      match_ordinal INTEGER,
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      UNIQUE (namespace, name)
    );
  `);
  return db;
}
