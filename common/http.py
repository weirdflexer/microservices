import os

import httpx

from common.errors import DomainError


class HTTPGateway:
    def __init__(self, settings):
        self.settings = settings
        self.client = httpx.Client(timeout=settings.http_timeout, trust_env=False)

    def request(self, service, method, path, json=None):
        base = os.getenv(f"{service.upper()}_URL", self.settings.service_host_template.format(service=service))
        try:
            response = self.client.request(method, base.rstrip("/") + path, json=json)
        except httpx.RequestError as exc:
            raise DomainError(503, f"Сервис {service} недоступен") from exc
        if response.is_error:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise DomainError(response.status_code if response.status_code < 500 else 503, str(detail))
        return response.json() if response.content else None

    def close(self):
        self.client.close()
