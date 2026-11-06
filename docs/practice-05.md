# Практическая работа 5. RabbitMQ и асинхронные сообщения

Система: бронирование столов бильярдного клуба. Группа ИКБО-63-23.
Основание: задание методички, PDF стр. 10–11 (печатные 32–33), и итоговая `system-diagram.md`.

## Задание 1. Индивидуальные варианты

Номер варианта — остаток последней цифры студенческого билета от деления на 4;
нулевой остаток означает вариант 4. Внутри команды варианты не повторяются.
Анвар и Андрей получают 4 по формуле, поэтому **Андрею назначен свободный вариант 3**.
Распределение вариантов согласовано; имена и фамилии участников уточнены.
Имена очередей и варианты редактируются в `practice5/profiles.json` без изменения Python-кода.

| Студент | Фамилия и инициалы из отчёта | Цифра | Вариант | Очередь | Exchange | Задержка |
|---|---|---:|---:|---|---|---|
| Данек | Черновасиленко Д. С. | 1 | 1 | auto-delete | topic | число точек в теле |
| Саша | Волентир А. Ф. | 2 | 2 | durable | direct | длина тела |
| Андрей | Исаченко А. Д. | 8 | 3, перенос с 4 | auto-delete | fanout | случайное целое 1–5 |
| Анвар | Нартаджиев А. Р. | 8 | 4 | durable | direct | число в теле |

Длина тела — число Unicode-символов после декодирования UTF-8: `Привет` даёт 6 секунд.
Для варианта 4 допустимо конечное неотрицательное число, включая дробное; `NaN`,
`inf`, отрицательное значение и нечисловой текст отклоняются без повторной доставки.

| Профиль CLI | Имя очереди | Имя exchange | Binding по умолчанию | Routing key публикации |
|---|---|---|---|---|
| `danek` | `ikbo-63-23_chernovasilenko` | `practice5.ikbo-63-23_chernovasilenko` | `billiards.*` | `billiards.demo` |
| `sasha` | `ikbo-63-23_volentir` | `practice5.ikbo-63-23_volentir` | `billiards.demo` | `billiards.demo` |
| `andrey` | `ikbo-63-23_isachenko` | `practice5.ikbo-63-23_isachenko` | пустой, fanout игнорирует ключ | пустой |
| `anvar` | `ikbo-63-23_nartadzhiev` | `practice5.ikbo-63-23_nartadzhiev` | `billiards.demo` | `billiards.demo` |

`exclusive=False` позволяет открыть несколько consumer одной очереди. У вариантов 2 и 4
очередь и exchange устойчивы, сообщения отправляются с `delivery_mode=2`.
У вариантов 1 и 3 очередь `durable=False, auto_delete=True`, сообщение непостоянное.
Exchange этих двух вариантов непостоянный, но не auto-delete: жизненный цикл exchange
не подменяет требование об auto-delete очереди.

В RabbitMQ 4.3+ сочетание `durable=False, exclusive=False` по умолчанию запрещено.
Для воспроизведения вариантов методички Compose подключает `infra/rabbitmq.conf`
с `deprecated_features.permit.transient_nonexcl_queues = true`.
Это настройка совместимости индивидуального задания; очереди восьми сервисов durable.
Основание: [официальное описание временных очередей RabbitMQ](https://www.rabbitmq.com/docs/queues).

### Запуск

Команды выполняются из корня проекта после установки зависимостей по README.
Брокер запускается локально, внешний учебный сервер из примера методички не используется.

```bash
docker compose up -d rabbitmq
export RABBITMQ_URL='amqp://micro:micro@localhost:5672/'
```

В первом терминале запустить consumer, во втором producer. Для каждого профиля
заменить обе команды согласно таблице:

```bash
python -m practice5.consumer --student danek
python -m practice5.producer --student danek 'Бронь...'
```

| Профиль | Тело для producer | Ожидаемая задержка |
|---|---|---|
| `danek` | `'Бронь...'` | 3 секунды |
| `sasha` | `'Стол'` | 4 секунды |
| `andrey` | `'Подтверждение'` | от 1 до 5 секунд |
| `anvar` | `'2.5'` | 2,5 секунды |

Для простейшей схемы «producer → очередь → consumer» используется встроенный exchange:

```bash
python -m practice5.consumer --student sasha --mode simple
python -m practice5.producer --student sasha --mode simple 'Стол'
```

В `simple` exchange — пустая строка, routing key — имя очереди; параметры самой очереди
и задержка сохраняются по варианту. В `variant`, выбранном по умолчанию, используется
собственный topic/direct/fanout exchange. Это две отдельные демонстрации; consumer
одной очереди не нужно запускать одновременно в разных режимах при сравнении маршрутов.

### Маршрутизация, подтверждения и жизненный цикл

Обе программы объявляют очередь и нужную привязку: отправка до первого запуска consumer
не теряет сообщение только из-за отсутствующей топологии. Producer использует
`mandatory=True` и publisher confirms; сообщение `CONFIRMED` означает подтверждение
брокера, а не завершение обработки. При отсутствии подходящей привязки producer
завершается с кодом 2 и сообщением об ошибке. Пример непопадающего topic-ключа:

```bash
python -m practice5.producer --student danek --routing-key other.event 'Бронь.'
```

Binding можно переопределить `--binding`, но объявления добавляют привязки и не удаляют
прежние. Для чистого опыта используйте исходную топологию либо удалите только свою
учебную очередь/exchange через Management UI перед сменой правил. Смена durable или
auto-delete у уже существующей очереди приводит к `PRECONDITION_FAILED`: остановите
свои consumer и пересоздайте только относящуюся к эксперименту очередь.

Получатель устанавливает `prefetch_count=1`, `auto_ack=False` и вызывает `basic_ack`
после задержки. Задержка реализована через `BlockingConnection.sleep`, чтобы обслуживать
heartbeat соединения. Несколько consumer одной очереди делят работу. Для fanout копия
сообщения попадает в каждую **разную привязанную очередь**, а два consumer одной очереди
по-прежнему конкурируют за доставку. В индивидуальной схеме одна очередь, как требует
рисунок 5.2 методички.

Auto-delete очередь удаляется после ухода последнего consumer, если consumer уже был
зарегистрирован. Простое объявление producer с последующим отключением её не удаляет.
Если последний consumer auto-delete очереди оборвался посреди работы, очередь и её
сообщения могут быть удалены: свойство варианта не даёт гарантий долговечности.
У durable очереди неподтверждённая доставка возвращается после разрыва соединения.
Невалидный текст/число отклоняется через `nack(requeue=False)` и в индивидуальной
демонстрации удаляется; это видно в логе `REJECT`. Бизнес-система ниже использует DLQ.
Сбой брокера завершает CLI с ошибкой; после восстановления его следует запустить снова.

Проверка задержек и правил без брокера:

```bash
python -m pytest tests/unit/test_practice5.py -q
```

Ручная демонстрация с RabbitMQ: отправить по сообщению для четырёх вариантов; увидеть
`CONFIRMED`, `RECEIVED` и `ACK`; проверить durable и auto-delete в UI
`http://localhost:15672` (локальные учебные `micro` / `micro`); остановить и заново
запустить consumer, проверить удаление временных очередей. Для проверки сохранения
вариантов 2/4 остановить consumer, отправить сообщение, перезапустить RabbitMQ, затем
consumer. Не использовать `docker compose down -v`: эта команда удаляет том с данными.
Это инструкция воспроизведения, а не заявление, что тест с живым брокером уже пройден.

## Задание 2. Сообщения доменной системы

Учебные очереди `<группа>_<фамилия>` относятся к заданию 1. Доменная система использует
собственные очереди `billiards.<service>`: одно событие может получить несколько сервисов,
поэтому у них отдельные очереди. Отправитель не ждёт ответа consumer; следующий результат
приходит отдельным событием. HTTP-проверки не превращаются в сообщения RabbitMQ.

Все доменные сообщения — JSON UTF-8 с общей оболочкой:

```json
{
  "id": "a518fe1b-6aeb-4b71-90ad-203b107ca46c",
  "type": "booking.confirmed",
  "occurred_at": "2026-11-06T18:00:00+00:00",
  "producer": "bookings",
  "correlation_id": "4b849709-dace-4d04-8175-ef3fe66170a9",
  "data": {
    "booking_id": "4b849709-dace-4d04-8175-ef3fe66170a9",
    "user_id": "7e13d2ae-5d40-4a6b-bcb8-22a4c1cb4cdd",
    "table_id": "f28c26c5-43cf-432a-ad3e-8300f34f7d95",
    "start_at": "2026-11-07T14:00:00+00:00",
    "end_at": "2026-11-07T15:00:00+00:00",
    "amount": 100000,
    "currency": "RUB"
  }
}
```

| Поле оболочки | Тип | Назначение |
|---|---|---|
| `id` | UUID-строка | Идентификатор события; не меняется при повторной доставке |
| `type` | строка | Тип события, совпадает с routing key |
| `occurred_at` | ISO 8601, с часовым поясом | Время факта, в UTC |
| `producer` | строка | Имя публикующего сервиса |
| `correlation_id` | UUID-строка или null | Для процесса бронирования равен `booking_id` |
| `data` | объект | Тело по каталогу ниже |

Все `*_id` — идентификаторы, а не вложенные сущности другого сервиса. `amount` — целое
число копеек, `currency` — `RUB`, `start_at`, `end_at`, `expires_at` — ISO 8601 с часовым
поясом. `payment_url` — ссылка учебной оплаты; `reason` — объяснение причины возврата.
API внешних платёжных/почтовых провайдеров в учебной реализации заменены локальными
адаптерами: реальные списания и отправка email/SMS не выполняются.

### Каталог сообщений и реакций

| Routing key | Издатель → подписчики | Поля `data` | Выполняемое действие |
|---|---|---|---|
| `booking.payment_requested` | S4 bookings → S6 payments | `booking_id`, `user_id`, `table_id`, `start_at`, `end_at`, `amount`, `currency`, `expires_at` | Создать один платёж на бронь и получить данные оплаты |
| `payment.created` | S6 payments → S4 bookings | `booking_id`, `payment_id`, `payment_url` | Сохранить идентификатор и ссылку оплаты |
| `payment.succeeded` | S6 payments → S4 bookings | `booking_id`, `user_id`, `payment_id`, `amount`, `currency` | Проверить сумму, синхронно закрепить действующее удержание в S3, затем подтвердить бронь; для поздней оплаты инициировать возврат |
| `payment.failed` | S6 payments → S4 bookings | `booking_id`, `user_id`, `payment_id`, `amount`, `currency` | Учесть неуспешную попытку оплаты; бронь не считать оплаченной |
| `booking.confirmed` | S4 bookings → S7 sessions, S8 notifications | `booking_id`, `user_id`, `table_id`, `start_at`, `end_at`, `amount`, `currency` | S7: создать ожидаемое посещение; S8: подготовить подтверждение и напоминание |
| `booking.cancelled` | S4 bookings → S3 schedule, S7 sessions, S8 notifications | `booking_id`, `user_id`, `table_id`, `start_at`, `end_at`, `amount`, `currency`, `hold_id` | S3: освободить интервал; S7: отменить ожидаемое посещение; S8: снять неотправленное напоминание |
| `booking.expired` | S4 bookings → S3 schedule, S7 sessions, S8 notifications | `booking_id`, `user_id`, `table_id`, `start_at`, `end_at`, `amount`, `currency`, `hold_id` | S3: освободить оставшийся интервал; S7: отменить ожидаемое посещение, если оно существовало; S8: снять напоминание |
| `booking.refund_requested` | S4 bookings → S6 payments | `booking_id`, `user_id`, `payment_id`, `amount`, `currency`, `reason` | Запросить возврат, не превышающий полученную сумму; повтор не создаёт второй возврат |
| `payment.refunded` | S6 payments → S4 bookings, S8 notifications | `booking_id`, `user_id`, `payment_id`, `amount`, `currency` | S4: отметить завершённый возврат; S8: подготовить сообщение клиенту |
| `payment.refund_failed` | S6 payments → S4 bookings | `booking_id`, `user_id`, `payment_id`, `amount`, `currency` | Зафиксировать незавершённое обязательство; не объявлять деньги возвращёнными |
| `schedule.hold_expired` | S3 schedule → S4 bookings | `booking_id`, `hold_id` | Закрыть ещё неоплаченную бронь; устаревшее сообщение не меняет подтверждённую бронь |
| `session.completed` | S7 sessions → S4 bookings | `booking_id`, `session_id` | Завершить подтверждённую бронь после окончания игры |
| `session.no_show` | S7 sessions → S4 bookings | `booking_id`, `session_id` | Закрыть бронь по неявке, не выполнять автоматический возврат |

В исходной итоговой диаграмме связь S4→S8 для отмены/истечения не показана.
Здесь это дополнение необходимо, чтобы отменять напоминания; оно согласуется с
правилами предыдущего отчёта. S3 также получает `booking.expired` для безопасного
освобождения интервала, если закрытие инициировано самим S4. Внутренние факты регистрации/изменения профиля,
публикации стола/тарифа и доставки сообщения не имеют обязательных внешних подписчиков
в итоговой диаграмме, поэтому отдельная межсервисная доставка для них не требуется.
У S1 users, S2 catalog и S5 pricing нет входящих доменных сообщений RabbitMQ.

### Exchange, очереди и привязки

Exchange `billiards.events`: `type=topic`, `durable=True`, `auto_delete=False`.
Сообщения постоянные (`delivery_mode=2`). Рабочие очереди durable, не exclusive и не
auto-delete. В таблице перечислены точные binding keys; topic выбран, чтобы в дальнейшем
подключать наблюдателей по шаблонам, но потребители связываются с нужными типами явно.

| Очередь | Binding keys |
|---|---|
| `billiards.bookings` | `payment.created`, `payment.succeeded`, `payment.failed`, `payment.refunded`, `payment.refund_failed`, `schedule.hold_expired`, `session.completed`, `session.no_show` |
| `billiards.payments` | `booking.payment_requested`, `booking.refund_requested` |
| `billiards.schedule` | `booking.cancelled`, `booking.expired` |
| `billiards.sessions` | `booking.confirmed`, `booking.cancelled`, `booking.expired` |
| `billiards.notifications` | `booking.confirmed`, `booking.cancelled`, `booking.expired`, `payment.refunded` |

Публикация идёт через локальный transactional outbox: изменение состояния и запись
события сохраняются одной транзакцией. После подтверждения брокера запись удаляется из outbox. Потребитель подтверждает доставку только после обработки; `id` события
записывается в inbox одной транзакцией с бизнес-изменением. Дубликаты подтверждаются
без повторного изменения состояния. При повторе сохраняется первоначальный `id`.
Дедупликация событий дополняется бизнес-ограничениями: один платёж/визит на бронь.

Дополнительно очередь `billiards.audit` привязана к `#` для наблюдения, её предел —
10 000 сообщений. Для каждого потребителя предусмотрена durable DLQ
`billiards.<service>.dead`, привязанная к direct exchange `billiards.dead` ключом имени
сервиса. Временная ошибка даёт ограниченные повторы (до трёх попыток); невалидное
сообщение или окончательная неудача публикуется в DLQ с `mandatory` и publisher
confirm, затем исходная доставка подтверждается. Если публикация повтора или DLQ
не удалась, исходная доставка остаётся неподтверждённой. В заголовках сохраняются
`x-original-key`, `x-error` и корректный `x-attempt`; бесконечный
`nack(requeue=True)` не используется. Изменение состояния и публикация в
RabbitMQ не объявляются общей распределённой транзакцией: outbox даёт доставку как
минимум один раз. Таймеры S3/S7 и отправка напоминаний S8 запускаются внутри сервисов.

## Источники и проверка

- `manual5-8.pdf`, раздел практической работы 5, таблица 1.
- `module1_pr1-4.pdf`, состав команды, бизнес-правила и обработка сбоев.
- `system-diagram.md`, итоговый перечень межсервисных связей.
- [RabbitMQ: свойства и жизненный цикл очередей](https://www.rabbitmq.com/docs/queues).
- [Pika: BlockingConnection, mandatory и UnroutableError](https://pika.github.io/pika/latest/modules/adapters/blocking/).
- [Pika: подтверждения издателю](https://pika.github.io/pika/latest/examples/blocking_delivery_confirmations/).

Unit-тесты проверяют точные задержки, границы случайного варианта, корректность профилей,
настройки маршрутизации, ACK после работы, отсутствие ACK при прерывании и отклонение
невалидных сообщений без зацикливания. Они не доказывают доступность живого RabbitMQ.
