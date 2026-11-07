import {DomainError} from './errors.mjs';

export function object(value, allowed = null) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) throw new DomainError(422, 'Ожидается JSON-объект');
  if (allowed && Object.keys(value).some(key => !allowed.includes(key))) throw new DomainError(422, 'Неизвестное поле запроса');
  return value;
}

export function uuid(value) {
  if (typeof value !== 'string') throw new DomainError(422, 'Идентификатор должен быть UUID');
  let text = value.toLowerCase().replace(/^urn:uuid:/, '').replace(/^\{(.*)\}$/, '$1').replaceAll('-', '');
  if (!/^[0-9a-f]{32}$/.test(text)) throw new DomainError(422, 'Идентификатор должен быть UUID');
  return `${text.slice(0,8)}-${text.slice(8,12)}-${text.slice(12,16)}-${text.slice(16,20)}-${text.slice(20)}`;
}

export function pageNumber(value, fallback, min, max = Number.MAX_SAFE_INTEGER) {
  if (value === null || value === undefined) return fallback;
  if (!/^\d+$/.test(value) || !Number.isSafeInteger(Number(value)) || Number(value) < min || Number(value) > max) {
    throw new DomainError(422, 'Некорректный параметр пагинации');
  }
  return Number(value);
}

export function requiredText(value, name) {
  if (typeof value !== 'string' || !value.trim()) throw new DomainError(422, `Поле ${name} обязательно`);
  return value;
}
