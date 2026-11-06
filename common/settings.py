from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite+pysqlite:///./{service}.db"
    rabbitmq_url: str = "amqp://micro:micro@localhost:5672/"
    provider_token: str = "demo-provider-token"
    service_host_template: str = "http://{service}:8000"
    http_timeout: float = 5.0
    timer_interval: float = 1.0
