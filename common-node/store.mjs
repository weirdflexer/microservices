import {randomUUID} from 'node:crypto';
import pg from 'pg';
import {iso, utcnow} from './time.mjs';

export const initialState = () => ({data: {}, inbox: {}, outbox: []});
export const clone = value => structuredClone(value);

export class Transaction {
  constructor(state, name) { this.state = state; this.name = name; this.data = state.data; this.inbox = state.inbox; this.outbox = state.outbox; }
  bucket(name) { return this.data[name] ??= {}; }
  emit(type, data, correlationId = null) {
    const event = {id: randomUUID(), type, occurred_at: iso(utcnow()), producer: this.name,
      correlation_id: correlationId ?? data.booking_id ?? null, data: clone(data)};
    this.outbox.push(event);
    return event;
  }
}

export class MemoryStore {
  constructor(name, state = initialState()) { this.name = name; this.state = clone(state); this.tail = Promise.resolve(); }
  async transaction(callback) {
    const previous = this.tail;
    let release;
    this.tail = new Promise(resolve => { release = resolve; });
    await previous;
    try {
      const state = clone(this.state);
      const result = await callback(new Transaction(state, this.name));
      this.state = clone(state);
      return clone(result);
    } finally { release(); }
  }
  async snapshot() { return this.transaction(tx => clone(tx.state)); }
  async close() { await this.tail; }
}

export class SQLStore {
  constructor(name, url, pool = null) {
    this.name = name;
    this.pool = pool ?? new pg.Pool({connectionString: url.replace(/^postgresql\+psycopg:/, 'postgresql:'), max: 10, connectionTimeoutMillis: 5000});
    this.pool.on?.('error', error => console.error('Idle PostgreSQL connection error:', error.message));
  }
  async initialize() {
    await this.pool.query('CREATE TABLE IF NOT EXISTS service_state (id INTEGER PRIMARY KEY, document JSON NOT NULL)');
    await this.pool.query('INSERT INTO service_state (id, document) VALUES (1, $1::json) ON CONFLICT (id) DO NOTHING', [JSON.stringify(initialState())]);
    return this;
  }
  async transaction(callback) {
    const client = await this.pool.connect();
    try {
      await client.query('BEGIN');
      const result = await client.query('SELECT document FROM service_state WHERE id = 1 FOR UPDATE');
      if (result.rows.length !== 1) throw new Error('Missing service_state row');
      const state = clone(result.rows[0].document);
      const value = await callback(new Transaction(state, this.name));
      await client.query('UPDATE service_state SET document = $1::json WHERE id = 1', [JSON.stringify(state)]);
      await client.query('COMMIT');
      return clone(value);
    } catch (error) {
      try { await client.query('ROLLBACK'); } catch { /* The connection may already be closed. */ }
      throw error;
    } finally { client.release(); }
  }
  async snapshot() { return this.transaction(tx => clone(tx.state)); }
  async close() { await this.pool.end(); }
}
