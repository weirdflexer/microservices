# Протокол проверок после переноса на Node.js и Go

Дата: 7 ноября 2026 г., Europe/Moscow. S1/S2/S5/S8 используют Python, S3/S7 —
Node.js, S4/S6 — Go. Проверка выполнена на macOS; контейнерный стенд — Podman.

## Unit и компонентные тесты

| Набор | Результат | Что проверено |
|---|---:|---|
| Python / pytest | 91 passed: 86 unit + 5 component | Четыре Python-сервиса, общая инфраструктура, варианты ПР5 |
| Node.js / node:test | 75 passed | S3/S7, HTTP, конкурентные удержания, границы времени, транзакции, inbox/outbox, AMQP |
| Go / testing | 35 top-level tests passed | S4/S6, HTTP, возвраты, повторные события, локальные транзакции, тайм-ауты AMQP |

Всего 201 проверка в нативных наборах. Для Go дополнительно выполнены `-race` и
`go vet ./...`: ошибок и гонок данных не обнаружено. В pytest остаётся одно
предупреждение Starlette об устаревающем HTTPX-адаптере TestClient; падений нет.

Команды из корня проекта:

```bash
python -m pytest tests/unit tests/component --cov --cov-report=term \
  --cov-report=json:reports/coverage.json --junitxml=reports/tests.xml -q
npm --prefix common-node ci
npm --prefix common-node test
go test -race -cover ./...
go vet ./...
```

Локальные версии: Python 3.13.5, Node.js 24.15.0, Go 1.27.3. Go-контейнеры также
успешно собраны отдельным компилятором Go 1.25 из Dockerfile. Все восемь образов
собраны; Node.js и Go контейнеры запускают собственные программы без Python.

## Покрытие

Показатели измеряются разными инструментами и не складываются в общий процент.

| Область | Покрытие |
|---|---:|
| Python: common + practice5 + четыре сервиса, строки | 78,9% |
| Node.js: common-node + API/логика S3/S7, строки | 90,38% |
| Node.js: бизнес-логика schedule и sessions, строки | 100% / 100% |
| Go: bookings, statements | 87,3% |
| Go: payments, statements | 86,9% |
| Go: общий runtime gokit, statements | 42,4% |

Точки входа не включены в приведённые показатели. Значительная часть непокрытого
runtime требует настоящих PostgreSQL/RabbitMQ и запуска процессов. Отдельные проверки
реального стенда ниже не включены в эти проценты. Текстовые журналы находятся в
`reports/tests.txt`, `reports/node-tests.txt`, `reports/go-tests.txt`; машинные результаты —
`reports/tests.xml`, `reports/coverage.json`, `reports/go-tests.json`, `reports/go-coverage.out`.

## Межъязыковый контейнерный стенд

Использованы настоящие PostgreSQL и RabbitMQ, четыре Python-, два Node.js- и два
Go-процесса в отдельных контейнерах. Порты доступны только на `127.0.0.1`.

| Проверка | Результат | Журнал |
|---|---|---|
| HTTP-сценарии, валидация и конкуренция | 42 passed | `reports/integration.txt`, `reports/integration.xml` |
| Оформление → оплата → подтверждение → отмена → возврат | PASS; cancelled / succeeded, посещение cancelled, интервал свободен | `reports/demo.json` |
| Чтение старых записей Python через новые S3/S4/S6/S7 | PASS; прежние UUID, состояния и сумма 120000 коп. сохранены | `reports/legacy-data.json` |
| Пересоздание контейнеров с сохранёнными томами | PASS; все восемь контрольных объектов совпали после полного down/up | `reports/persistence-write.txt`, `reports/persistence-verify.txt`, `reports/recreate.txt` |
| Четыре варианта ПР5 × simple/variant | 8 из 8 PASS | `reports/rabbitmq.txt` |
| Сборка Node.js / Go Dockerfile | PASS | `reports/node-containers-build.log`, `reports/go-containers-build.log` |

HTTP-тесты не импортируют реализации сервисов: запросы проходят через сеть, а события —
через RabbitMQ. Они проверяют в том числе одновременные удержания и бронирования,
повторные callback, запоздалую оплату ранее неоплаченной отменённой брони, границу
возврата и отсутствие ложного возврата от дубликата успешной оплаты.

```bash
RUN_HTTP_INTEGRATION=1 python -m pytest tests/integration \
  --ignore=tests/integration/test_http_persistence.py -q
python -m scripts.demo
python -m scripts.check_rabbitmq
```

Проверка сохранения — две фазы одного теста: `HTTP_PERSISTENCE_MODE=write` создаёт
снимок публичных объектов всех восьми сервисов; после реального `compose down` без
`-v` и `compose up -d` фаза `verify` читает те же записи без их повторного создания.
Полные команды приведены в [tests/integration/README.md](../tests/integration/README.md).

## Статические проверки и границы результата

- Экспорт Python OpenAPI и проверка статических контрактов Node.js/Go: 33 операции,
  по 4 у семи сервисов и 5 у расписания; встроенные Go-схемы совпадают с docs/openapi.
- Python-код проходит compileall; Go проходит vet; git diff --check не обнаружил ошибок.
- Compose разобран и содержит 10 контейнеров с healthcheck своего языка.
- Формат service_state PostgreSQL, 13 типов событий и RabbitMQ bindings сохранены.
- Старые Python-тесты заменённых сервисов и тест восьми ASGI-приложений удалены;
  их заменяют нативные тесты Node.js/Go и независимые HTTP-проверки.

Нагрузочные испытания, кластеризация PostgreSQL/RabbitMQ и полная сохранённая сага
не входят в проверенный объём. Тестовые записи остаются в именованных томах.

После проверок все 10 контейнеров имели статус healthy. Учебный стенд и запущенная
для него Podman-машина остановлены; именованные тома с данными сохранены.
