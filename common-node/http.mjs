import {DomainError} from './errors.mjs';

export class HTTPGateway {
  constructor({timeout = Number(process.env.HTTP_TIMEOUT ?? 5) * 1000, fetchImpl = fetch} = {}) { this.timeout = timeout; this.fetch = fetchImpl; }
  async request(service, method, path, json = undefined) {
    const template = process.env.SERVICE_HOST_TEMPLATE ?? 'http://{service}:8000';
    const base = process.env[`${service.toUpperCase()}_URL`] ?? template.replace('{service}', service);
    let response;
    try {
      response = await this.fetch(base.replace(/\/$/, '') + path, {
        method, signal: AbortSignal.timeout(this.timeout),
        headers: json === undefined ? {} : {'content-type': 'application/json'},
        body: json === undefined ? undefined : JSON.stringify(json),
      });
      const text = await response.text();
      let data;
      try { data = text ? JSON.parse(text) : null; }
      catch { throw new DomainError(503, `Сервис ${service} вернул некорректный ответ`); }
      if (!response.ok) throw new DomainError(response.status >= 500 ? 503 : response.status,
        typeof data?.detail === 'string' ? data.detail : JSON.stringify(data?.detail ?? data));
      return data;
    } catch (error) {
      if (error instanceof DomainError) throw error;
      throw new DomainError(503, `Сервис ${service} недоступен`);
    }
  }
  async close() {}
}
