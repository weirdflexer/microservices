import {randomUUID} from 'node:crypto';
import {DomainError} from '../../common-node/errors.mjs';
import {iso, parseTime, validateInterval, MINUTE} from '../../common-node/time.mjs';
import {object, uuid} from '../../common-node/validation.mjs';

export class Service {
  constructor(ctx) { this.ctx = ctx; }
  async table(tableId) {
    const table = await this.ctx.http.request('catalog', 'GET', `/api/tables/${tableId}`);
    if (!table.published) throw new DomainError(409, 'Стол не опубликован');
    return table;
  }
  static overlaps(firstStart, firstEnd, secondStart, secondEnd) { return firstStart < secondEnd && secondStart < firstEnd; }
  static active(hold, now) { return hold.status === 'confirmed' || hold.status === 'held' && parseTime(hold.expires_at) > now; }
  async setSchedule(tableId, data) {
    tableId = uuid(tableId); object(data, ['opens_at', 'closes_at']);
    const opens = parseTime(data.opens_at), closes = parseTime(data.closes_at);
    if (opens >= closes) throw new DomainError(422, 'Конец рабочего окна должен быть позже начала');
    await this.table(tableId);
    return this.ctx.store.transaction(tx => {
      const now = parseTime(this.ctx.now());
      for (const hold of Object.values(tx.bucket('holds'))) {
        if (hold.table_id === tableId && Service.active(hold, now) && parseTime(hold.end_at) > now &&
            (parseTime(hold.start_at) < opens || parseTime(hold.end_at) > closes)) {
          throw new DomainError(409, 'Новый график нарушает действующий резерв или бронь');
        }
      }
      const schedule = {table_id: tableId, opens_at: iso(opens), closes_at: iso(closes)};
      tx.bucket('schedules')[tableId] = schedule;
      return schedule;
    });
  }
  async availability(tableId, startAt, endAt) {
    tableId = uuid(tableId);
    const [start, end] = validateInterval(startAt, endAt);
    return this.ctx.store.transaction(tx => {
      const schedule = tx.bucket('schedules')[tableId];
      if (!schedule) throw new DomainError(404, 'Рабочее окно стола не задано');
      const now = parseTime(this.ctx.now());
      const busy = Object.values(tx.bucket('holds')).filter(hold => hold.table_id === tableId && Service.active(hold, now) &&
        Service.overlaps(start, end, parseTime(hold.start_at), parseTime(hold.end_at)));
      const inWindow = parseTime(schedule.opens_at) <= start && end <= parseTime(schedule.closes_at);
      return {table_id: tableId, start_at: iso(start), end_at: iso(end), available: inWindow && busy.length === 0 && start > now, busy_intervals: busy};
    });
  }
  async hold(data) {
    object(data, ['booking_id', 'table_id', 'start_at', 'end_at']);
    const bookingId = uuid(data.booking_id), tableId = uuid(data.table_id);
    const now = parseTime(this.ctx.now());
    const [start, end] = validateInterval(data.start_at, data.end_at, this.ctx.now());
    await this.table(tableId);
    return this.ctx.store.transaction(tx => {
      const holds = tx.bucket('holds');
      const existing = Object.values(holds).find(hold => hold.booking_id === bookingId);
      if (existing) {
        const matches = existing.table_id === tableId && parseTime(existing.start_at) === start && parseTime(existing.end_at) === end;
        if (matches && Service.active(existing, now)) return existing;
        throw new DomainError(409, 'Для этой брони уже существует другой или закрытый резерв');
      }
      const schedule = tx.bucket('schedules')[tableId];
      if (!schedule) throw new DomainError(409, 'Рабочее окно стола не задано');
      if (!(parseTime(schedule.opens_at) <= start && end <= parseTime(schedule.closes_at))) throw new DomainError(409, 'Интервал выходит за рабочее окно');
      for (const hold of Object.values(holds)) {
        if (hold.table_id === tableId && Service.active(hold, now) && Service.overlaps(start, end, parseTime(hold.start_at), parseTime(hold.end_at))) {
          throw new DomainError(409, 'Интервал уже занят');
        }
      }
      const hold = {id: randomUUID(), booking_id: bookingId, table_id: tableId, start_at: iso(start), end_at: iso(end), expires_at: iso(Math.min(now + 10 * MINUTE, start)), status: 'held'};
      holds[hold.id] = hold;
      return hold;
    });
  }
  static ownedHold(tx, holdId, bookingId) {
    const hold = tx.bucket('holds')[holdId];
    if (!hold) throw new DomainError(404, 'Резерв не найден');
    if (hold.booking_id !== bookingId) throw new DomainError(409, 'Резерв принадлежит другой брони');
    return hold;
  }
  async confirm(holdId, bookingId) {
    holdId = uuid(holdId); bookingId = uuid(bookingId);
    return this.ctx.store.transaction(tx => {
      const hold = Service.ownedHold(tx, holdId, bookingId);
      if (hold.status === 'confirmed') return hold;
      if (hold.status !== 'held' || parseTime(hold.expires_at) <= parseTime(this.ctx.now())) throw new DomainError(409, 'Резерв больше не действует');
      hold.status = 'confirmed'; return hold;
    });
  }
  async release(holdId, bookingId) {
    holdId = uuid(holdId); bookingId = uuid(bookingId);
    return this.ctx.store.transaction(tx => {
      const hold = Service.ownedHold(tx, holdId, bookingId);
      if (!['released', 'expired'].includes(hold.status)) hold.status = 'released';
      return hold;
    });
  }
  async tick() {
    await this.ctx.store.transaction(tx => {
      const now = parseTime(this.ctx.now());
      for (const hold of Object.values(tx.bucket('holds'))) {
        if (hold.status === 'held' && parseTime(hold.expires_at) <= now) {
          hold.status = 'expired';
          tx.emit('schedule.hold_expired', {booking_id: hold.booking_id, hold_id: hold.id});
        }
      }
    });
  }
  async handle(event, tx) {
    if (!['booking.cancelled', 'booking.expired'].includes(event.type)) return;
    const data = event.data, bookingId = uuid(data.booking_id), holdId = data.hold_id ? uuid(data.hold_id) : null;
    for (const hold of Object.values(tx.bucket('holds'))) {
      if (hold.booking_id === bookingId && (!holdId || hold.id === holdId) && !['released', 'expired'].includes(hold.status)) hold.status = 'released';
    }
  }
}
