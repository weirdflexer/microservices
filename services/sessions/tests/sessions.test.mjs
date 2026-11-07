import {test} from 'node:test';
import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
import {MemoryStore} from '../../../common-node/store.mjs';
import {DomainError} from '../../../common-node/errors.mjs';
import {Service} from '../service.mjs';
import {buildApp} from '../main.mjs';

const rejectsStatus = (promise, status) => assert.rejects(promise, error => error instanceof DomainError && error.status === status);
async function fixture() {
  const clock = {now: new Date('2030-01-01T12:00:00Z')};
  const data = {booking_id: randomUUID(), user_id: randomUUID(), table_id: randomUUID(), start_at: '2030-01-01T12:00:00+00:00', end_at: '2030-01-01T14:00:00+00:00'};
  const calls = [];
  const ctx = {name: 'sessions', store: new MemoryStore('sessions'), now: () => clock.now, http: {async request(...args) { calls.push(args); return {...data, status: 'confirmed', start_at: '2030-01-01T12:00:00Z'}; }}};
  const service = new Service(ctx);
  await ctx.store.transaction(tx => service.handle({type: 'booking.confirmed', data}, tx));
  const session = (await service.list())[0];
  return {service, ctx, data, clock, session, calls};
}

test('one expected visit per booking and equal timezone spellings work', async () => {
  const {service, ctx, data, session, calls} = await fixture();
  await ctx.store.transaction(tx => service.handle({type: 'booking.confirmed', data}, tx));
  assert.equal((await service.list()).length, 1);
  assert.equal((await service.start(session.id)).status, 'active');
  assert.deepEqual(calls[0], ['bookings', 'GET', `/api/bookings/${data.booking_id}`]);
  await service.start(session.id);
  assert.equal(calls.length, 1);
});

test('completion is idempotent and emits exactly one event', async () => {
  const {service, ctx, clock, session} = await fixture();
  await rejectsStatus(service.complete(session.id), 409);
  await service.start(session.id);
  clock.now = new Date('2030-01-01T13:00:00Z');
  await service.complete(session.id); await service.complete(session.id);
  assert.equal((await service.get(session.id)).status, 'completed');
  const outbox = (await ctx.store.snapshot()).outbox;
  assert.equal(outbox.length, 1); assert.equal(outbox[0].type, 'session.completed');
});

for (const timestamp of ['2030-01-01T11:59:59Z', '2030-01-01T12:15:00Z']) test(`start outside interval rejects ${timestamp}`, async () => {
  const {service, clock, session, calls} = await fixture();
  clock.now = new Date(timestamp);
  await rejectsStatus(service.start(session.id), 409);
  assert.equal(calls.length, 0);
});

test('start just before cutoff succeeds; exact cutoff creates one no-show', async () => {
  const first = await fixture();
  first.clock.now = new Date('2030-01-01T12:14:59.999Z');
  assert.equal((await first.service.start(first.session.id)).status, 'active');
  const {service, ctx, clock, session} = await fixture();
  clock.now = new Date('2030-01-01T12:15:00Z');
  await Promise.all([service.tick(), service.tick(), service.tick()]);
  assert.equal((await service.get(session.id)).status, 'no_show');
  const events = (await ctx.store.snapshot()).outbox;
  assert.equal(events.length, 1); assert.equal(events[0].type, 'session.no_show');
});

test('concurrent start and timer preserve mutually exclusive states', async () => {
  const {service, ctx, clock, session} = await fixture();
  clock.now = new Date('2030-01-01T12:15:00Z');
  const result = await Promise.allSettled([service.start(session.id), service.tick()]);
  assert.equal(result[0].status, 'rejected');
  assert.equal((await service.get(session.id)).status, 'no_show');
  assert.equal((await ctx.store.snapshot()).outbox.length, 1);
});

test('active session auto-completes at booked end, never before started_at', async () => {
  const {service, ctx, clock, session} = await fixture();
  await service.start(session.id);
  clock.now = new Date('2030-01-01T11:59:00Z');
  await rejectsStatus(service.complete(session.id), 409);
  clock.now = new Date('2030-01-01T14:00:00Z');
  await service.tick(); await service.tick();
  assert.equal((await service.get(session.id)).status, 'completed');
  assert.equal((await ctx.store.snapshot()).outbox.length, 1);
});

test('cancellation prevents no-show and tombstone blocks out-of-order confirmation', async () => {
  const {service, ctx, data, clock, session} = await fixture();
  await ctx.store.transaction(tx => service.handle({type: 'booking.cancelled', data}, tx));
  const later = {...data, booking_id: randomUUID()};
  await ctx.store.transaction(tx => service.handle({type: 'booking.expired', data: later}, tx));
  await ctx.store.transaction(tx => service.handle({type: 'booking.confirmed', data: later}, tx));
  clock.now = new Date('2030-01-01T16:00:00Z');
  await service.tick();
  assert.equal((await service.list()).length, 1);
  assert.equal((await service.get(session.id)).status, 'cancelled');
  assert.equal((await ctx.store.snapshot()).outbox.length, 0);
});

for (const changed of [{status: 'cancelled'}, {user_id: randomUUID()}, {table_id: randomUUID()}, {end_at: '2030-01-01T15:00:00Z'}]) {
  test(`start validates booking ${Object.keys(changed)[0]}`, async () => {
    const {service, ctx, data, session} = await fixture();
    ctx.http.request = async () => ({...data, status: 'confirmed', ...changed});
    await rejectsStatus(service.start(session.id), 409);
    assert.equal((await service.get(session.id)).status, 'expected');
  });
}

test('dependency failure rolls back start and can be retried', async () => {
  const {service, ctx, session} = await fixture();
  const original = ctx.http.request;
  ctx.http.request = async () => { throw new DomainError(503, 'Недоступен'); };
  await rejectsStatus(service.start(session.id), 503);
  assert.equal((await service.get(session.id)).status, 'expected');
  ctx.http.request = original;
  assert.equal((await service.start(session.id)).status, 'active');
});

test('HTTP contract: list filters, pagination, UUID, start, complete and errors', async t => {
  const {ctx, session, data} = await fixture();
  const app = await buildApp(ctx, false), address = await app.listen();
  t.after(() => app.close());
  const base = `http://127.0.0.1:${address.port}`;
  const get = async (path, method = 'GET') => {
    const response = await fetch(base + path, {method});
    return {status: response.status, body: await response.json()};
  };
  assert.equal((await get('/api/sessions?booking_id=' + data.booking_id)).body.length, 1);
  assert.equal((await get('/api/sessions?offset=1&limit=1')).body.length, 0);
  for (const path of ['/api/sessions?limit=101', '/api/sessions?offset=-1', '/api/sessions?booking_id=x', '/api/sessions/not-a-uuid']) assert.equal((await get(path)).status, 422);
  assert.equal((await get(`/api/sessions/${randomUUID()}`)).status, 404);
  assert.equal((await get(`/api/sessions/${session.id}:complete`, 'POST')).status, 409);
  assert.equal((await get(`/api/sessions/${session.id}:start`, 'POST')).body.status, 'active');
  assert.equal((await get(`/api/sessions/${session.id}:complete`, 'POST')).body.status, 'completed');
  assert.equal((await get('/health')).body.broker_connected, false);
});
