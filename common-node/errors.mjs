export class DomainError extends Error {
  constructor(status, detail) { super(detail); this.name = 'DomainError'; this.status = status; this.detail = detail; }
}
