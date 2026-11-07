import {test} from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {randomUUID} from 'node:crypto';
import {MemoryStore, SQLStore} from '../store.mjs';
import {DomainError} from '../errors.mjs';
import {parseTime, validateInterval} from '../time.mjs';
import {HTTPGateway} from '../http.mjs';
import {BrokerWorker, ConfirmPublisher, processIncoming, validateEnvelope, EXCHANGE, DLX, declareTopology, closeConnection} from '../messaging.mjs';

const envelope = (changes = {}) => ({id: randomUUID(), type: 'booking.confirmed', occurred_at: '2030-01-01T08:00:00Z',
  producer: 'bookings', correlation_id: randomUUID(), data: {booking_id: randomUUID()}, ...changes});
const ctx = () => ({name: 'sessions', store: new MemoryStore('sessions')});
const logger = {error() {}};
class CountingService {
  constructor(error = null) { this.calls = 0; this.error = error; }
  async handle(event, tx) {
    this.calls++;
    tx.bucket('effects').count = (tx.bucket('effects').count ?? 0) + 1;
    tx.emit('session.completed', {booking_id: event.data.booking_id});
    if (this.error) throw this.error;
  }
}
const delivery = (event = envelope(), {headers = {}, exchange = EXCHANGE, key = event?.type ?? 'booking.confirmed', raw = null} = {}) => ({
  fields: {exchange, routingKey: key}, properties: {headers}, content: raw ?? Buffer.from(JSON.stringify(event)),
});
class FakePublisher {
  constructor(failAt = 0, operations = []) { this.calls = []; this.failAt = failAt; this.operations = operations; }
  async publish(...args) {
    this.operations.push('publish');
    if (this.calls.length + 1 === this.failAt) throw new Error('publish failed');
    this.calls.push(args);
  }
}

function fakeWorker(service = new CountingService()) {
  const context = ctx();
  return {context, service, worker: new BrokerWorker(context, service, {sleep: async () => {}, logger})};
}

test('transaction rollback covers business changes, inbox and outbox atomically', async () => {
  const store = new MemoryStore('sessions');
  await store.transaction(tx => { tx.bucket('rows').one = {status: 'expected'}; });
  const before = await store.snapshot();
  await assert.rejects(store.transaction(tx => {
    tx.bucket('rows').one.status = 'active'; tx.inbox.event = 'processed'; tx.emit('session.completed', {}); throw new Error('crash');
  }), /crash/);
  assert.deepEqual(await store.snapshot(), before);
});

test('returned values and emitted payloads are detached, failed lock holder does not block next transaction', async () => {
  const store = new MemoryStore('sessions'), payload = {nested: {value: 1}};
  const result = await store.transaction(tx => { tx.bucket('rows').one = payload; const event = tx.emit('session.completed', payload); payload.nested.value = 2; return event; });
  result.data.nested.value = 99; payload.nested.value = 99;
  const state = await store.snapshot();
  assert.equal(state.outbox[0].data.nested.value, 1); assert.equal(state.data.rows.one.nested.value, 2);
  await assert.rejects(store.transaction(async () => { throw new Error('fail'); }));
  await store.transaction(tx => { tx.bucket('rows').two = true; });
  assert.equal((await store.snapshot()).data.rows.two, true);
});

test('PostgreSQL transaction uses one client, FOR UPDATE, COMMIT and release', async () => {
  const queries = [], state = {data: {}, inbox: {}, outbox: []};
  const client = {async query(sql, values) { queries.push([sql, values]); return {rows: [{document: structuredClone(state)}]}; }, release() { queries.push(['release']); }};
  const store = new SQLStore('sessions', 'postgresql://unused', {async connect() { return client; }});
  await store.transaction(tx => { tx.bucket('rows').one = 1; tx.emit('session.completed', {}); });
  assert.equal(queries[0][0], 'BEGIN');
  assert.match(queries[1][0], /FOR UPDATE$/);
  const persisted = JSON.parse(queries[2][1][0]);
  assert.equal(persisted.data.rows.one, 1); assert.equal(persisted.outbox.length, 1);
  assert.deepEqual(queries.slice(3).map(row => row[0]), ['COMMIT', 'release']);
});

test('PostgreSQL rollback releases client and never sends UPDATE after failed business logic', async () => {
  const queries = [], client = {async query(sql) { queries.push(sql); return {rows: [{document: {data: {}, inbox: {}, outbox: []}}]}; }, release() { queries.push('release'); }};
  const store = new SQLStore('sessions', 'postgresql://unused', {async connect() { return client; }});
  await assert.rejects(store.transaction(tx => { tx.emit('session.completed', {}); throw new Error('business failure'); }), /business failure/);
  assert.equal(queries.at(-2), 'ROLLBACK'); assert.equal(queries.at(-1), 'release');
  assert.equal(queries.some(query => query.startsWith('UPDATE')), false);
});

test('inbox canonical UUID makes duplicate uppercase event id harmless', async () => {
  const {context, service} = fakeWorker(), event = envelope();
  assert.equal(await processIncoming(service, context, event), true);
  assert.equal(await processIncoming(service, context, {...event, id: event.id.toUpperCase()}), false);
  assert.equal(service.calls, 1); assert.equal((await context.store.snapshot()).outbox.length, 1);
});

test('failed event handler leaves no inbox, outbox or partial state and later succeeds', async () => {
  const service = new CountingService(new DomainError(503, 'dependency unavailable'));
  const {context} = fakeWorker(service), event = envelope(), before = await context.store.snapshot();
  await assert.rejects(processIncoming(service, context, event));
  assert.deepEqual(await context.store.snapshot(), before);
  service.error = null;
  assert.equal(await processIncoming(service, context, event), true);
});

for (const invalid of [null, [], {}, envelope({id: 7}), envelope({id: 'bad'}), envelope({occurred_at: '2030-01-01T08:00:00'}),
  envelope({data: []}), envelope({producer: ''}), envelope({type: ''}), envelope({correlation_id: []}), envelope({correlation_id: undefined})]) {
  test(`invalid envelope is permanent 422: ${JSON.stringify(invalid)}`, () => {
    assert.throws(() => validateEnvelope(invalid), error => error instanceof DomainError && error.status === 422);
  });
}

test('retry publishes only own queue and ACK follows confirmation', async () => {
  const {worker, context} = fakeWorker(new CountingService(new DomainError(503, 'temporary')));
  const operations = [], publisher = new FakePublisher(0, operations), channel = {ack() { operations.push('ack'); }};
  const message = delivery();
  await worker.receive(channel, publisher, message);
  assert.deepEqual(operations, ['publish', 'ack']);
  const [exchange, key, raw, properties] = publisher.calls[0];
  assert.equal(exchange, ''); assert.equal(key, 'billiards.sessions'); assert.equal(raw, message.content);
  assert.equal(properties.headers['x-attempt'], 2); assert.equal(properties.headers['x-original-key'], 'booking.confirmed');
  assert.equal((await context.store.snapshot()).outbox.length, 0);
});

test('failed retry publication leaves original unacked', async () => {
  const {worker} = fakeWorker(new CountingService(new DomainError(503, 'temporary')));
  let acknowledgements = 0;
  await assert.rejects(worker.receive({ack() { acknowledgements++; }}, new FakePublisher(1), delivery()), /publish failed/);
  assert.equal(acknowledgements, 0);
});

test('retry restores original routing key and successful event is acknowledged', async () => {
  const {worker, service} = fakeWorker();
  let acknowledgements = 0;
  await worker.receive({ack() { acknowledgements++; }}, new FakePublisher(), delivery(envelope(), {
    exchange: '', key: 'billiards.sessions', headers: {'x-attempt': 2, 'x-original-key': 'booking.confirmed'},
  }));
  assert.equal(service.calls, 1); assert.equal(acknowledgements, 1);
});

for (const message of [delivery(null), delivery([], {key: 'booking.confirmed'}), delivery(envelope(), {raw: Buffer.from('not-json')}),
  delivery(envelope(), {headers: {'x-attempt': 'two'}}), delivery(envelope(), {headers: {'x-attempt': 0}}),
  delivery(envelope(), {headers: {'x-attempt': true}}), delivery(envelope(), {headers: []}), delivery(envelope(), {key: 'wrong.type'})]) {
  test(`poison delivery goes to confirmed DLQ: ${message.content} ${JSON.stringify(message.properties)}`, async () => {
    const {worker, service} = fakeWorker(), operations = [], publisher = new FakePublisher(0, operations);
    await worker.receive({ack() { operations.push('ack'); }}, publisher, message);
    assert.equal(service.calls, 0); assert.deepEqual(operations, ['publish', 'ack']);
    assert.equal(publisher.calls[0][0], DLX); assert.equal(publisher.calls[0][1], 'sessions');
    assert.equal(publisher.calls[0][2], message.content);
  });
}

test('third temporary failure goes to DLQ; failed DLQ publish leaves unacked', async () => {
  const {worker} = fakeWorker(new CountingService(new DomainError(503, 'temporary'))), publisher = new FakePublisher();
  let acknowledgements = 0;
  await worker.receive({ack() { acknowledgements++; }}, publisher, delivery(envelope(), {headers: {'x-attempt': 3}}));
  assert.equal(publisher.calls[0][0], DLX); assert.equal(acknowledgements, 1);
  await assert.rejects(worker.receive({ack() { acknowledgements++; }}, new FakePublisher(1), delivery(null)), /publish failed/);
  assert.equal(acknowledgements, 1);
});

test('lost ACK after commit does not repeat business effect on redelivery', async () => {
  const {worker, service, context} = fakeWorker(), event = envelope();
  await assert.rejects(worker.receive({ack() { throw new Error('connection closed'); }}, new FakePublisher(), delivery(event)));
  let acknowledgements = 0;
  await worker.receive({ack() { acknowledgements++; }}, new FakePublisher(), delivery(event));
  assert.equal(service.calls, 1); assert.equal(acknowledgements, 1); assert.equal((await context.store.snapshot()).outbox.length, 1);
});

test('outbox removes confirmed prefix only, preserving failed publication and tail', async () => {
  const {worker, context} = fakeWorker();
  const ids = await context.store.transaction(tx => [1,2,3].map(n => tx.emit('session.completed', {n}).id));
  await assert.rejects(worker.flush(new FakePublisher(2)), /publish failed/);
  assert.deepEqual((await context.store.snapshot()).outbox.map(event => event.id), ids.slice(1));
  await worker.flush(new FakePublisher());
  assert.equal((await context.store.snapshot()).outbox.length, 0);
});

class ConfirmChannel extends EventEmitter {
  constructor(mode = 'ack') { super(); this.mode = mode; this.messages = []; }
  publish(exchange, key, body, properties, callback) {
    this.messages.push({exchange, key, body, properties});
    queueMicrotask(() => {
      if (this.mode === 'return') this.emit('return', {properties, fields: {replyText: 'NO_ROUTE'}});
      if (this.mode === 'close') return this.emit('close');
      if (this.mode === 'timeout') return;
      callback(this.mode === 'nack' ? new Error('nacked') : null);
    });
    return false; // Backpressure is not an error.
  }
}

test('publisher awaits ack, uses persistent mandatory flags, preserves envelope id', async () => {
  const channel = new ConfirmChannel(), publisher = new ConfirmPublisher(channel);
  await publisher.publish(EXCHANGE, 'session.completed', Buffer.from('{}'), {messageId: 'stable-event-id'});
  assert.equal(channel.messages[0].properties.mandatory, true);
  assert.equal(channel.messages[0].properties.persistent, true);
  assert.equal(channel.messages[0].properties.messageId, 'stable-event-id');
  assert.equal(publisher.pending.size, 0);
  publisher.close();
});

for (const mode of ['return', 'nack', 'close', 'timeout']) test(`publisher rejects ${mode} even if an ACK follows return`, async () => {
  const channel = new ConfirmChannel(mode), publisher = new ConfirmPublisher(channel, 10);
  await assert.rejects(publisher.publish(EXCHANGE, 'session.completed', Buffer.from('{}')));
  assert.equal(publisher.pending.size, 0); publisher.close();
});

test('topology matches shared Python durable queues and exact bindings', async () => {
  const queues = [], bindings = [], channel = {
    async assertExchange() {}, async assertQueue(...args) { queues.push(args); }, async bindQueue(...args) { bindings.push(args); },
  };
  await declareTopology(channel);
  assert.ok(bindings.some(args => args[0] === 'billiards.schedule' && args[2] === 'booking.expired'));
  assert.ok(bindings.some(args => args[0] === 'billiards.sessions' && args[2] === 'booking.confirmed'));
  assert.deepEqual(queues.find(args => args[0] === 'billiards.sessions')[1], {durable: true, arguments: {'x-dead-letter-exchange': DLX, 'x-dead-letter-routing-key': 'sessions'}});
});

for (const value of ['2030-02-30T12:00:00Z', '2030-01-01T24:00:00Z', '2030-01-01T12:00:00+24:00', '2030-01-01T12:00:00', 0]) {
  test(`invalid ISO timestamp is rejected: ${value}`, () => assert.throws(() => parseTime(value), DomainError));
}

test('interval validation matches UTC rule and exact 1/4 hour boundaries', () => {
  assert.equal(parseTime('2030-01-01T15:00:00+03:00'), parseTime('2030-01-01T12:00:00Z'));
  validateInterval('2030-01-01T12:00:00Z', '2030-01-01T13:00:00Z');
  validateInterval('2030-01-01T12:00:00Z', '2030-01-01T16:00:00Z');
  assert.throws(() => validateInterval('2030-01-01T12:00:00Z', '2030-01-01T13:00:00.000001Z'), DomainError);
});

test('HTTP gateway converts transport and 5xx failures to retryable 503, preserves 4xx', async () => {
  const unavailable = new HTTPGateway({fetchImpl: async () => { throw new Error('network'); }});
  await assert.rejects(unavailable.request('catalog', 'GET', '/api/tables/x'), error => error.status === 503);
  const serverFailure = new HTTPGateway({fetchImpl: async () => new Response('{"detail":"down"}', {status: 500})});
  await assert.rejects(serverFailure.request('catalog', 'GET', '/api/tables/x'), error => error.status === 503);
  const missing = new HTTPGateway({fetchImpl: async () => new Response('{"detail":"missing"}', {status: 404})});
  await assert.rejects(missing.request('catalog', 'GET', '/api/tables/x'), error => error.status === 404 && error.detail === 'missing');
});


test('AMQP reconnect cleanup cannot hang waiting for broker CloseOk', async () => {
  let calls = 0, destroyed = 0;
  const connection = {close() { calls++; return new Promise(() => {}); },
    connection: {stream: {destroy() { destroyed++; }}}};
  await Promise.all([closeConnection(connection, 10), closeConnection(connection, 10)]);
  assert.equal(calls, 1);
  assert.equal(destroyed, 1);
});

test('AMQP clean shutdown also releases socket and immediate close errors are safe', async () => {
  for (const fails of [false, true]) {
    let destroyed = 0;
    const connection = {async close() { if (fails) throw new Error('already closed'); },
      connection: {stream: {destroy() { destroyed++; }}}};
    await closeConnection(connection, 10);
    assert.equal(destroyed, 1);
  }
});
