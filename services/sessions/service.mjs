import {randomUUID} from 'node:crypto';
import {DomainError} from '../../common-node/errors.mjs';
import {iso, parseTime, MINUTE} from '../../common-node/time.mjs';
import {uuid} from '../../common-node/validation.mjs';

export class Service {
  constructor(ctx) { this.ctx = ctx; }
  static getRow(tx, sessionId) {
    const session = tx.bucket('sessions')[sessionId];
    if (!session) throw new DomainError(404, 'Посещение не найдено');
    return session;
  }
  async list(bookingId = null, offset = 0, limit = 100) {
    return this.ctx.store.transaction(tx => Object.values(tx.bucket('sessions')).filter(row => !bookingId || row.booking_id === bookingId).slice(offset, offset + limit));
  }
  async get(sessionId) { return this.ctx.store.transaction(tx => Service.getRow(tx, uuid(sessionId))); }
  async handle(event, tx) {
    const data = event.data;
    if (event.type === 'booking.confirmed') {
      const bookingId = uuid(data.booking_id);
      if (Object.hasOwn(tx.bucket('by_booking'), bookingId) || Object.hasOwn(tx.bucket('cancelled_bookings'), bookingId)) return;
      const start = parseTime(data.start_at), end = parseTime(data.end_at);
      if (end <= start) throw new DomainError(422, 'Окончание должно быть позже начала');
      const session = {id: randomUUID(), booking_id: bookingId, user_id: uuid(data.user_id), table_id: uuid(data.table_id),
        start_at: iso(start), end_at: iso(end), status: 'expected', started_at: null, completed_at: null};
      tx.bucket('sessions')[session.id] = session;
      tx.bucket('by_booking')[bookingId] = session.id;
    } else if (['booking.cancelled', 'booking.expired'].includes(event.type)) {
      const bookingId = uuid(data.booking_id);
      tx.bucket('cancelled_bookings')[bookingId] = true;
      const sessionId = tx.bucket('by_booking')[bookingId];
      if (sessionId) {
        const session = Service.getRow(tx, sessionId);
        if (session.status === 'expected') session.status = 'cancelled';
      }
    }
  }
  async start(sessionId) {
    return this.ctx.store.transaction(async tx => {
      const session = Service.getRow(tx, uuid(sessionId));
      if (session.status === 'active') return session;
      if (session.status !== 'expected') throw new DomainError(409, 'Посещение уже не ожидается');
      const start = parseTime(session.start_at), now = parseTime(this.ctx.now());
      if (!(start <= now && now < start + 15 * MINUTE)) throw new DomainError(409, 'Начало возможно только в первые 15 минут брони');
      const booking = await this.ctx.http.request('bookings', 'GET', `/api/bookings/${session.booking_id}`);
      if (booking.status !== 'confirmed' || ['user_id', 'table_id'].some(key => booking[key] !== session[key]) ||
          ['start_at', 'end_at'].some(key => parseTime(booking[key]) !== parseTime(session[key]))) {
        throw new DomainError(409, 'Бронь не подтверждена или не соответствует посещению');
      }
      session.status = 'active'; session.started_at = iso(this.ctx.now()); return session;
    });
  }
  completeRow(tx, session) {
    session.status = 'completed'; session.completed_at = iso(this.ctx.now());
    tx.emit('session.completed', {booking_id: session.booking_id, session_id: session.id}, session.booking_id);
  }
  async complete(sessionId) {
    return this.ctx.store.transaction(tx => {
      const session = Service.getRow(tx, uuid(sessionId));
      if (session.status === 'completed') return session;
      if (session.status !== 'active') throw new DomainError(409, 'Завершить можно только начатый сеанс');
      if (parseTime(this.ctx.now()) < parseTime(session.started_at)) throw new DomainError(409, 'Окончание не может предшествовать началу');
      this.completeRow(tx, session); return session;
    });
  }
  async tick() {
    await this.ctx.store.transaction(tx => {
      for (const session of Object.values(tx.bucket('sessions'))) {
        const now = parseTime(this.ctx.now());
        if (session.status === 'expected' && now >= parseTime(session.start_at) + 15 * MINUTE) {
          session.status = 'no_show'; tx.emit('session.no_show', {booking_id: session.booking_id, session_id: session.id}, session.booking_id);
        } else if (session.status === 'active' && now >= parseTime(session.end_at)) this.completeRow(tx, session);
      }
    });
  }
}
