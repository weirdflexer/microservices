# Протокол проверок

Дата: 6 ноября 2026 г., Europe/Moscow. Среда unit/component: macOS, Python 3.13.
Зависимости зафиксированы в requirements.txt и requirements-dev.txt.

## Unit и component

Команда:

```bash
python -m pytest tests/unit tests/component --cov --cov-report=term \
  --cov-report=json:reports/coverage.json --junitxml=reports/tests.xml -q
```

Результат: **142 passed**, 0 failures, 0 errors, 0 skipped.
Общее покрытие строк common + services + practice5: **85.4%**.
Непокрыты в основном участки запуска фоновых workers и CLI, требующие реального стенда.
Точки входа main.py исключены из измерения согласно pyproject.toml.

| Сервис: API + модели + бизнес-логика | Покрытие строк |
|---|---:|
| users | 97.5% |
| catalog | 97.7% |
| schedule | 95.2% |
| bookings | 97.0% |
| pricing | 98.1% |
| payments | 95.3% |
| sessions | 96.5% |
| notifications | 98.5% |

В прогоне есть одно предупреждение Starlette о будущем отказе от HTTPX в TestClient.
Оно не влияет на результат; фактически проверенные версии зависимостей зафиксированы.
Сырые результаты: reports/tests.txt, reports/tests.xml, reports/coverage.json.

## Статические проверки

- Все 8 спецификаций OpenAPI сформированы без обращения к PostgreSQL/RabbitMQ.
- У семи сервисов по 4 бизнес-операции, у расписания 5: всего 33.
- Таблицы практики 6 содержат все операции из OpenAPI.
- Код common, services, practice5, scripts проходит compileall.
- Compose содержит 8 приложений и 2 инфраструктурных контейнера; конфигурация разобрана podman-compose.

## Контейнерный стенд

Проверка выполнена на macOS через Podman / podman-compose: все 10 контейнеров
собраны и запущены, приложения используют настоящие PostgreSQL и RabbitMQ.

| Проверка | Результат | Доказательство |
|---|---|---|
| Оформление → оплата → подтверждение → отмена → возврат | PASS; бронь cancelled, refund succeeded, посещение cancelled, интервал свободен | reports/demo.json |
| Четыре варианта ПР5 × simple/variant | 8 из 8 PASS; подтверждена публикация, выдержана задержка, получен ACK | reports/rabbitmq.txt |
| Перезапуск контейнера users | PASS; ранее созданный клиент прочитан с теми же данными | reports/persistence.txt |
| Состояние контейнеров | 10 запущены; users показан starting сразу после рестарта, его HTTP-чтение уже успешно | reports/container-status.txt |
| Сборка образов и запуск Compose | PASS | reports/containers.log |

При первой проверке обнаружено изменение RabbitMQ 4.3+: непостоянные неэксклюзивные
очереди ПР5 запрещены по умолчанию. В infra/rabbitmq.conf добавлено официально
предусмотренное разрешение transient_nonexcl_queues; после этого все восемь
комбинаций индивидуальных проверок прошли. Бизнес-очереди durable и от этой
настройки не зависят.

После проверки контейнеры этого стенда и запущенная для него виртуальная машина
остановлены. Именованные тома с учебными данными сохранены. Для повторного запуска
используются команды README; очистка томов не входит в обычную остановку.
