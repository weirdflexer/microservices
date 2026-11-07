import {randomUUID} from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';
import amqp from 'amqplib';
import {DomainError} from './errors.mjs';
import {object, uuid, requiredText} from './validation.mjs';
import {iso, parseTime, utcnow} from './time.mjs';

export const EXCHANGE = 'billiards.events';
export const DLX = 'billiards.dead';
export const BINDINGS = {
  users: [], catalog: [], pricing: [],
  schedule: ['booking.cancelled', 'booking.expired'],
  bookings: ['payment.created', 'payment.succeeded', 'payment.failed', 'payment.refunded', 'payment.refund_failed', 'schedule.hold_expired', 'session.completed', 'session.no_show'],
  payments: ['booking.payment_requested', 'booking.refund_requested'],
  sessions: ['booking.confirmed', 'booking.cancelled', 'booking.expired'],
  notifications: ['booking.confirmed', 'booking.cancelled', 'booking.expired', 'payment.refunded'],
};

export async function declareTopology(channel) {
  await channel.assertExchange(EXCHANGE, 'topic', {durable: true});
  await channel.assertExchange(DLX, 'direct', {durable: true});
  await channel.assertQueue('billiards.audit', {durable: true, arguments: {'x-max-length': 10000}});
  await channel.bindQueue('billiards.audit', EXCHANGE, '#');
  for (const [service, keys] of Object.entries(BINDINGS)) {
    if (!keys.length) continue;
    const queue = `billiards.${service}`;
    await channel.assertQueue(`${queue}.dead`, {durable: true});
    await channel.bindQueue(`${queue}.dead`, DLX, service);
    await channel.assertQueue(queue, {durable: true, arguments: {'x-dead-letter-exchange': DLX, 'x-dead-letter-routing-key': service}});
    for (const key of keys) await channel.bindQueue(queue, EXCHANGE, key);
  }
}

export function validateEnvelope(event) {
  try {
    object(event);
    const id = uuid(event.id);
    if (typeof event.occurred_at !== 'string') throw new DomainError(422, 'Timestamp must be a string');
    parseTime(event.occurred_at);
    object(event.data);
    requiredText(event.type, 'type');
    requiredText(event.producer, 'producer');
    if (event.correlation_id !== null && typeof event.correlation_id !== 'string') throw new DomainError(422, 'Invalid correlation id');
    return id;
  } catch (error) { throw new DomainError(422, 'Некорректный конверт события'); }
}

export async function processIncoming(service, ctx, event) {
  const id = validateEnvelope(event);
  return ctx.store.transaction(async tx => {
    if (Object.hasOwn(tx.inbox, id)) return false;
    await service.handle(event, tx);
    tx.inbox[id] = iso(utcnow());
    return true;
  });
}

/** A confirm alone is insufficient: mandatory returns are also publication errors. */
export class ConfirmPublisher {
  constructor(channel, timeoutMs = 10000) {
    this.channel = channel; this.timeoutMs = timeoutMs; this.pending = new Map();
    this.onReturn = message => {
      const entry = this.pending.get(message.properties.headers?.['x-publication-id']);
      if (entry) entry.fail(new Error(`Unroutable message: ${message.fields.replyText ?? 'NO_ROUTE'}`));
    };
    this.onClose = () => this.failAll(new Error('AMQP channel closed before confirm'));
    this.onError = error => this.failAll(error);
    channel.on('return', this.onReturn);
    channel.on('close', this.onClose);
    channel.on('error', this.onError);
  }
  failAll(error) { for (const entry of [...this.pending.values()]) entry.fail(error); }
  publish(exchange, routingKey, body, properties = {}) {
    return new Promise((resolve, reject) => {
      const id = randomUUID();
      let timer;
      const finish = error => {
        if (!this.pending.delete(id)) return;
        clearTimeout(timer);
        error ? reject(error) : resolve();
      };
      this.pending.set(id, {fail: finish});
      timer = setTimeout(() => finish(new Error('Publisher confirm timeout')), this.timeoutMs);
      try {
        // Publish is callback-based even on amqplib's promise API. Waiting for its
        // confirm also bounds buffer growth; false is backpressure, not failure.
        this.channel.publish(exchange, routingKey, body, {
          persistent: true, contentType: 'application/json', ...properties,
          mandatory: true, headers: {...properties.headers, 'x-publication-id': id},
        }, error => finish(error));
      } catch (error) { finish(error); }
    });
  }
  close() {
    this.failAll(new Error('Publisher closed'));
    this.channel.off('return', this.onReturn); this.channel.off('close', this.onClose); this.channel.off('error', this.onError);
  }
}

// AMQP close waits for CloseOk; a blocked broker may never answer. The installed
// amqplib ChannelModel exposes its underlying connection stream. A bounded close
// destroys that stream, requeues unacked deliveries and unblocks pending confirms.
const closingConnections = new WeakMap();
export function closeConnection(connection, timeoutMs = 1000) {
  if (!connection) return Promise.resolve();
  if (closingConnections.has(connection)) return closingConnections.get(connection);
  const closing = (async () => {
    let timer;
    const timeout = new Promise(resolve => { timer = setTimeout(resolve, timeoutMs); });
    try {
      await Promise.race([Promise.resolve().then(() => connection.close()).catch(() => {}), timeout]);
    } finally {
      clearTimeout(timer);
      connection.connection?.stream?.destroy();
    }
  })();
  closingConnections.set(connection, closing);
  return closing;
}

export class BrokerWorker {
  constructor(ctx, service, {connect = amqp.connect, sleep = delay, logger = console} = {}) {
    this.ctx = ctx; this.service = service; this.connect = connect; this.sleep = sleep; this.logger = logger;
    this.connected = false; this.abort = new AbortController(); this.connection = null; this.running = null;
  }
  start() { this.running ??= this.run(); return this; }
  async close() {
    this.abort.abort();
    await closeConnection(this.connection);
    await this.running;
  }
  async receive(channel, publisher, message) {
    if (!message) throw new Error('Broker cancelled consumer');
    let attempt = 1, headers = {}, event;
    let routingKey = message.fields.routingKey;
    try {
      headers = message.properties.headers ?? {};
      object(headers);
      if (message.fields.exchange === '' && headers['x-original-key']) routingKey = headers['x-original-key'];
      attempt = headers['x-attempt'] ?? 1;
      if (!Number.isSafeInteger(attempt) || attempt < 1) throw new DomainError(422, 'Некорректный номер попытки доставки');
      event = JSON.parse(message.content.toString('utf8'));
      validateEnvelope(event);
      if (event.type !== routingKey) throw new DomainError(422, 'Routing key не совпадает с типом события');
      await processIncoming(this.service, this.ctx, event);
    } catch (error) {
      const permanent = error instanceof SyntaxError || error instanceof TypeError || error instanceof DomainError && error.status < 500;
      this.logger.error(`Event handling failed in ${this.ctx.name}: ${error.message}`);
      if (permanent || attempt >= 3) {
        const deadHeaders = {'x-original-key': typeof routingKey === 'string' ? routingKey : '', 'x-error': error.message.slice(0, 500)};
        if (Number.isSafeInteger(attempt) && attempt >= 1) deadHeaders['x-attempt'] = attempt;
        await publisher.publish(DLX, this.ctx.name, message.content, {headers: deadHeaders});
      } else {
        await this.sleep(200 * attempt);
        await publisher.publish('', `billiards.${this.ctx.name}`, message.content,
          {headers: {...headers, 'x-attempt': attempt + 1, 'x-original-key': event.type}});
      }
      // A publication failure propagates before ACK. The worker closes the
      // connection, so RabbitMQ redelivers the still-unacknowledged original.
      channel.ack(message);
      return;
    }
    channel.ack(message);
  }
  async flush(publisher) {
    const events = await this.ctx.store.transaction(tx => structuredClone(tx.outbox.slice(0, 100)));
    for (const event of events) {
      await publisher.publish(EXCHANGE, event.type, Buffer.from(JSON.stringify(event)), {messageId: event.id});
      await this.ctx.store.transaction(tx => {
        tx.outbox.splice(0, tx.outbox.length, ...tx.outbox.filter(row => row.id !== event.id));
      });
    }
  }
  async run() {
    while (!this.abort.signal.aborted) {
      let connection, publisher, channel, failed = false;
      let activeDelivery = Promise.resolve();
      try {
        const url = new URL(this.ctx.settings.rabbitmqUrl);
        url.searchParams.set('heartbeat', '60');
        connection = await this.connect(url.toString(), {timeout: 5000});
        this.connection = connection;
        const fail = error => {
          failed = true;
          this.logger.error(`RabbitMQ reconnect in ${this.ctx.name}: ${error.message}`);
          closeConnection(connection).catch(() => {});
        };
        connection.on('error', fail);
        connection.on('close', () => { failed = true; });
        channel = await connection.createConfirmChannel();
        channel.on('error', fail);
        channel.on('close', () => { failed = true; });
        publisher = new ConfirmPublisher(channel);
        await declareTopology(channel);
        await channel.prefetch(1);
        await channel.consume(`billiards.${this.ctx.name}`, message => {
          activeDelivery = activeDelivery.then(() => this.receive(channel, publisher, message));
          activeDelivery.catch(fail);
        }, {noAck: false});
        this.connected = true;
        while (!failed && !this.abort.signal.aborted) {
          await this.flush(publisher);
          await this.sleep(100, undefined, {signal: this.abort.signal}).catch(error => { if (error.name !== 'AbortError') throw error; });
        }
      } catch (error) {
        if (!this.abort.signal.aborted) this.logger.error(`RabbitMQ unavailable in ${this.ctx.name}: ${error.message}`);
      } finally {
        this.connected = false;
        await closeConnection(connection);
        await activeDelivery.catch(() => {});
        publisher?.close();
        this.connection = null;
      }
      if (!this.abort.signal.aborted) await this.sleep(2000, undefined, {signal: this.abort.signal}).catch(() => {});
    }
  }
}
