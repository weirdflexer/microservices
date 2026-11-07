import {test} from 'node:test';
import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
import {MemoryStore} from '../../../common-node/store.mjs';
import {DomainError} from '../../../common-node/errors.mjs';
import {parseTime, iso, MINUTE, HOUR} from '../../../common-node/time.mjs';
import {Service} from '../service.mjs';
import {buildApp} from '../main.mjs';

const rejectsStatus = (promise, status) => assert.rejects(promise, error => error instanceof DomainError && error.status === status);
async function fixture() {
  const clock = {now: new Date('2030-01-01T08:00:00Z')};
  const calls = [];
  const ctx = {name: 'schedule', store: new MemoryStore('schedule'), now: () => clock.now,
    http: {async request(...args) { calls.push(args); return {published: true}; }}};
  const service = new Service(ctx), tableId = randomUUID();
  await service.setSchedule(tableId, {opens_at: '2030-01-01T09:00:00Z', closes_at: '2030-01-01T23:00:00Z'});
  const data = {booking_id: randomUUID(), table_id: tableId, start_at: '2030-01-01T12:00:00Z', end_at: '2030-01-01T13:00:00Z'};
  return {service, ctx, clock, calls, data};
}

test('32 concurrent overlapping holds have exactly one winner', async () => {
  const {service, data} = await fixture();
  const results = await Promise.allSettled(Array.from({length: 32}, () => service.hold({...data, booking_id: randomUUID()})));
  assert.equal(results.filter(row => row.status === 'fulfilled').length, 1);
  assert.ok(results.filter(row => row.status === 'rejected').every(row => row.reason.status === 409));
});

test('same booking is idempotent, changed interval conflicts, adjacent slots work', async () => {
  const {service, data} = await fixture();
  const hold = await service.hold(data);
  assert.equal((await service.hold({...data, start_at: '2030-01-01T15:00:00+03:00', end_at: '2030-01-01T16:00:00+03:00'})).id, hold.id);
  await rejectsStatus(service.hold({...data, end_at: '2030-01-01T14:00:00Z'}), 409);
  const adjacent = await service.hold({...data, booking_id: randomUUID(), start_at: data.end_at, end_at: '2030-01-01T14:00:00Z'});
  assert.notEqual(adjacent.id, hold.id);
  assert.equal(parseTime(hold.expires_at), Date.parse('2030-01-01T08:10:00Z'));
});

test('expiry is inclusive, timer emits once, expired hold no longer blocks', async () => {
  const {service, data, clock, ctx} = await fixture();
  const hold = await service.hold(data);
  clock.now = new Date('2030-01-01T08:10:00Z');
  await rejectsStatus(service.confirm(hold.id, data.booking_id), 409);
  assert.equal((await service.availability(data.table_id, data.start_at, data.end_at)).available, true);
  await service.tick(); await service.tick();
  const state = await ctx.store.snapshot();
  assert.equal(state.data.holds[hold.id].status, 'expired');
  assert.equal(state.outbox.length, 1);
  assert.deepEqual(state.outbox[0].data, {booking_id: data.booking_id, hold_id: hold.id});
});

test('hold expires no later than start and confirmed intervals survive timers', async () => {
  const {service, data, clock, ctx} = await fixture();
  clock.now = new Date('2030-01-01T11:55:00Z');
  const hold = await service.hold(data);
  assert.equal(parseTime(hold.expires_at), parseTime(data.start_at));
  assert.equal((await service.confirm(hold.id, data.booking_id)).status, 'confirmed');
  clock.now = new Date('2030-01-01T12:05:00Z');
  await service.tick();
  assert.equal((await service.confirm(hold.id, data.booking_id)).status, 'confirmed');
  assert.equal((await ctx.store.snapshot()).outbox.length, 0);
  await rejectsStatus(service.release(hold.id, randomUUID()), 409);
  assert.equal((await service.release(hold.id, data.booking_id)).status, 'released');
  assert.equal((await service.release(hold.id, data.booking_id)).status, 'released');
});

test('schedule updates cannot evict an active hold and invalid windows rollback', async () => {
  const {service, data, ctx} = await fixture();
  await service.hold(data);
  const before = await ctx.store.snapshot();
  await rejectsStatus(service.setSchedule(data.table_id, {opens_at: '2030-01-01T14:00:00Z', closes_at: '2030-01-01T23:00:00Z'}), 409);
  await rejectsStatus(service.setSchedule(data.table_id, {opens_at: '2030-01-01T14:00:00Z', closes_at: '2030-01-01T13:00:00Z'}), 422);
  assert.deepEqual(await ctx.store.snapshot(), before);
});

test('cancel and expire events release only their owned hold', async () => {
  const {service, data, ctx} = await fixture();
  const hold = await service.hold(data);
  await ctx.store.transaction(tx => service.handle({type: 'booking.cancelled', data: {booking_id: randomUUID(), hold_id: hold.id}}, tx));
  assert.equal((await service.availability(data.table_id, data.start_at, data.end_at)).available, false);
  await ctx.store.transaction(tx => service.handle({type: 'booking.expired', data: {booking_id: data.booking_id, hold_id: hold.id}}, tx));
  assert.equal((await service.availability(data.table_id, data.start_at, data.end_at)).available, true);
});

for (const [name, changed] of [
  ['unaligned start', {start_at: '2030-01-01T12:15:00Z', end_at: '2030-01-01T13:15:00Z'}],
  ['short duration', {end_at: '2030-01-01T12:30:00Z'}],
  ['long duration', {end_at: '2030-01-01T16:30:00Z'}],
  ['sub-millisecond start', {start_at: '2030-01-01T12:00:00.000001Z'}],
  ['naive datetime', {start_at: '2030-01-01T12:00:00'}],
  ['bad UUID', {booking_id: '123'}],
  ['extra field', {status: 'confirmed'}],
]) test(`hold validation rejects ${name}`, async () => {
  const {service, data} = await fixture();
  await rejectsStatus(service.hold({...data, ...changed}), 422);
});

test('catalog publication and working hours are enforced', async () => {
  const {service, data, ctx} = await fixture();
  await rejectsStatus(service.hold({...data, start_at: '2030-01-02T00:00:00Z', end_at: '2030-01-02T01:00:00Z'}), 409);
  ctx.http.request = async () => ({published: false});
  await rejectsStatus(service.hold(data), 409);
  await rejectsStatus(service.setSchedule(data.table_id, {opens_at: '2030-01-01T09:00:00Z', closes_at: '2030-01-01T23:00:00Z'}), 409);
});

test('HTTP contract: schedule, hold, confirm, release, validation and Swagger', async t => {
  const {ctx, data, calls} = await fixture();
  const app = await buildApp(ctx, false);
  const address = await app.listen();
  t.after(() => app.close());
  const base = `http://127.0.0.1:${address.port}`;
  const request = async (path, method = 'GET', json) => {
    const response = await fetch(base + path, {method, headers: {'content-type': 'application/json'}, body: json === undefined ? undefined : JSON.stringify(json)});
    return {status: response.status, body: await response.json()};
  };
  const created = await request('/api/holds', 'POST', data);
  assert.equal(created.status, 201);
  assert.equal((await request('/api/holds', 'POST', {...data, booking_id: randomUUID()})).status, 409);
  assert.equal((await request(`/api/holds/${created.body.id}:confirm`, 'POST', {booking_id: data.booking_id})).body.status, 'confirmed');
  assert.equal((await request(`/api/holds/${created.body.id}?booking_id=${randomUUID()}`, 'DELETE')).status, 409);
  assert.equal((await request(`/api/holds/${created.body.id}?booking_id=${data.booking_id}`, 'DELETE')).body.status, 'released');
  const query = new URLSearchParams({start_at: data.start_at, end_at: data.end_at});
  assert.equal((await request(`/api/tables/${data.table_id}/availability?${query}`)).body.available, true);
  assert.equal((await request(`/api/tables/${data.table_id}/availability`)).status, 422);
  assert.equal((await request('/api/holds', 'POST', {...data, extra: true})).status, 422);
  assert.equal((await request('/api/holds/not-a-uuid?booking_id=' + data.booking_id, 'DELETE')).status, 422);
  const spec = (await request('/openapi.json')).body;
  assert.equal(Object.values(spec.paths).reduce((sum, value) => sum + Object.keys(value).length, 0), 5);
  assert.equal((await fetch(base + '/docs')).status, 200);
  assert.ok(calls.some(([name, method]) => name === 'catalog' && method === 'GET'));
});
