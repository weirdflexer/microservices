import {DomainError} from './errors.mjs';

export const MINUTE = 60_000;
export const HOUR = 60 * MINUTE;
export function utcnow() { return new Date(); }

export function parseTime(value) {
  if (value instanceof Date && Number.isFinite(value.getTime())) return value.getTime();
  if (typeof value !== 'string') throw new DomainError(422, 'Укажите время ISO 8601 с часовым поясом');
  const match = /^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?([Zz]|[+-]\d{2}:\d{2})$/.exec(value);
  if (!match) throw new DomainError(422, 'Укажите время ISO 8601 с часовым поясом');
  const [, year, month, day, hour, minute, second = '0', fraction = '', zone] = match;
  const calendar = new Date(0);
  calendar.setUTCFullYear(+year, +month - 1, +day);
  calendar.setUTCHours(0, 0, 0, 0);
  const validCalendar = calendar.getUTCFullYear() === +year && calendar.getUTCMonth() === +month - 1 && calendar.getUTCDate() === +day;
  if (!validCalendar || +hour > 23 || +minute > 59 || +second > 59 ||
      (zone !== 'Z' && zone !== 'z' && (+zone.slice(1, 3) > 23 || +zone.slice(4) > 59))) {
    throw new DomainError(422, 'Некорректная дата или время');
  }
  const normalized = `${year}-${month}-${day}T${hour}:${minute}:${second.padStart(2, '0')}${fraction ? '.' + fraction.slice(0,3).padEnd(3,'0') : ''}${zone.toUpperCase()}`;
  const result = Date.parse(normalized);
  if (!Number.isFinite(result)) throw new DomainError(422, 'Некорректная дата или время');
  return result;
}

export function iso(value) {
  const date = typeof value === 'number' ? new Date(value) : new Date(parseTime(value));
  return date.toISOString().replace('.000Z', '+00:00').replace('Z', '+00:00');
}

export function validateInterval(startValue, endValue, now = undefined) {
  const start = parseTime(startValue), end = parseTime(endValue);
  const subMillisecond = value => typeof value === 'string' && /\.\d*[1-9]\d*(?:Z|[+-]\d\d:\d\d)$/i.test(value);
  if (start % (30 * MINUTE) !== 0 || subMillisecond(startValue)) throw new DomainError(422, 'Начало игры должно быть кратно 30 минутам');
  const duration = end - start;
  if (duration < HOUR || duration > 4 * HOUR || duration % (30 * MINUTE) !== 0 || subMillisecond(endValue)) {
    throw new DomainError(422, 'Длительность: от 1 до 4 часов с шагом 30 минут');
  }
  if (now !== undefined && start <= parseTime(now)) throw new DomainError(422, 'Начало игры должно быть в будущем');
  return [start, end];
}
