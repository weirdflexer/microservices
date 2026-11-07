package gokit

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	amqp "github.com/rabbitmq/amqp091-go"
)

const Exchange = "billiards.events"
const DeadExchange = "billiards.dead"

var Bindings = map[string][]string{
	"schedule":      {"booking.cancelled", "booking.expired"},
	"bookings":      {"payment.created", "payment.succeeded", "payment.failed", "payment.refunded", "payment.refund_failed", "schedule.hold_expired", "session.completed", "session.no_show"},
	"payments":      {"booking.payment_requested", "booking.refund_requested"},
	"sessions":      {"booking.confirmed", "booking.cancelled", "booking.expired"},
	"notifications": {"booking.confirmed", "booking.cancelled", "booking.expired", "payment.refunded"},
}

func DeclareTopology(ch *amqp.Channel) error {
	if err := ch.ExchangeDeclare(Exchange, "topic", true, false, false, false, nil); err != nil {
		return err
	}
	if err := ch.ExchangeDeclare(DeadExchange, "direct", true, false, false, false, nil); err != nil {
		return err
	}
	if _, err := ch.QueueDeclare("billiards.audit", true, false, false, false, amqp.Table{"x-max-length": int32(10000)}); err != nil {
		return err
	}
	if err := ch.QueueBind("billiards.audit", "#", Exchange, false, nil); err != nil {
		return err
	}
	for service, keys := range Bindings {
		queue := "billiards." + service
		if _, err := ch.QueueDeclare(queue+".dead", true, false, false, false, nil); err != nil {
			return err
		}
		if err := ch.QueueBind(queue+".dead", service, DeadExchange, false, nil); err != nil {
			return err
		}
		if _, err := ch.QueueDeclare(queue, true, false, false, false, amqp.Table{"x-dead-letter-exchange": DeadExchange, "x-dead-letter-routing-key": service}); err != nil {
			return err
		}
		for _, key := range keys {
			if err := ch.QueueBind(queue, key, Exchange, false, nil); err != nil {
				return err
			}
		}
	}
	return nil
}

// Publisher serializes mandatory publishes and waits for broker confirms before the caller commits removal/ack.
type Publisher interface {
	Publish(context.Context, string, string, []byte, amqp.Table, string) error
}
type deferredPublisherChannel interface {
	PublishWithDeferredConfirmWithContext(context.Context, string, string, bool, bool, amqp.Publishing) (*amqp.DeferredConfirmation, error)
}
type confirmedPublisher struct {
	channel  deferredPublisherChannel
	returned <-chan amqp.Return
	abort    func()
	timeout  time.Duration
}

func newPublisher(ch *amqp.Channel, abort func()) (*confirmedPublisher, error) {
	if err := ch.Confirm(false); err != nil {
		return nil, err
	}
	return &confirmedPublisher{channel: ch, returned: ch.NotifyReturn(make(chan amqp.Return, 1)), abort: abort, timeout: 10 * time.Second}, nil
}
func (p *confirmedPublisher) Publish(parent context.Context, exchange, key string, body []byte, headers amqp.Table, id string) error {
	ctx, cancel := context.WithTimeout(parent, p.timeout)
	// amqp091-go v1.10 deliberately ignores the context in PublishWithDeferredConfirmWithContext.
	// A separate watchdog closes the socket with a deadline, unblocking the publish itself,
	// not only the subsequent wait for a broker confirmation.
	stopAbort := context.AfterFunc(ctx, p.abort)
	defer func() { stopAbort(); cancel() }()
	confirmation, err := p.channel.PublishWithDeferredConfirmWithContext(ctx, exchange, key, true, false, amqp.Publishing{ContentType: "application/json", DeliveryMode: amqp.Persistent, Headers: headers, MessageId: id, Body: body})
	if err != nil {
		return err
	}
	if confirmation == nil {
		return fmt.Errorf("publisher confirms disabled")
	}
	ack, err := confirmation.WaitContext(ctx)
	if err != nil {
		return err
	}
	if !ack {
		return fmt.Errorf("broker rejected publish")
	}
	// basic.return is delivered before the corresponding basic.ack on this channel.
	select {
	case returned, ok := <-p.returned:
		if !ok {
			return fmt.Errorf("publisher channel closed")
		}
		return fmt.Errorf("unroutable %s: %s", key, returned.ReplyText)
	default:
		return nil
	}
}
func Flush(ctx context.Context, c *Context, p Publisher) error {
	snapshot, err := c.Store.Snapshot(ctx)
	if err != nil {
		return err
	}
	events := snapshot.Outbox
	if len(events) > 100 {
		events = events[:100]
	}
	for _, event := range events {
		raw, err := json.Marshal(event)
		if err != nil {
			return err
		}
		if err = p.Publish(ctx, Exchange, event.Type, raw, nil, event.ID); err != nil {
			return err
		}
		if err = c.Store.WithTx(ctx, func(tx *Tx) error {
			kept := make([]Event, 0, len(tx.State.Outbox))
			for _, queued := range tx.State.Outbox {
				if queued.ID != event.ID {
					kept = append(kept, queued)
				}
			}
			tx.State.Outbox = kept
			return nil
		}); err != nil {
			return err
		}
	}
	return nil
}
func attemptNumber(headers amqp.Table) (int, error) {
	value, ok := headers["x-attempt"]
	if !ok {
		return 1, nil
	}
	var attempt int64
	switch n := value.(type) {
	case int:
		attempt = int64(n)
	case int8:
		attempt = int64(n)
	case int16:
		attempt = int64(n)
	case int32:
		attempt = int64(n)
	case int64:
		attempt = n
	default:
		return 1, Fail(422, "Некорректный номер попытки доставки")
	}
	if attempt < 1 || attempt > 2147483647 {
		return 1, Fail(422, "Некорректный номер попытки доставки")
	}
	return int(attempt), nil
}

// HandleDelivery publishes retries only to the current service. A failed publication leaves the original unacked.
func HandleDelivery(ctx context.Context, c *Context, service EventHandler, p Publisher, d amqp.Delivery) error {
	attempt, err := attemptNumber(d.Headers)
	key := d.RoutingKey
	if err == nil && d.Exchange == "" {
		if original, ok := d.Headers["x-original-key"]; ok {
			var valid bool
			key, valid = original.(string)
			if !valid || strings.TrimSpace(key) == "" {
				err = Fail(422, "Некорректный исходный routing key")
			}
		}
	}
	var event Event
	if err == nil {
		event, err = ReadEvent(d.Body)
	}
	if err == nil && event.Type != key {
		err = Fail(422, "Routing key не совпадает с типом события")
	}
	if err == nil {
		_, err = ProcessIncoming(ctx, service, c, event)
	}
	if err == nil {
		return d.Ack(false)
	}
	log.Printf("event failed service=%s attempt=%d: %v", c.Name, attempt, err)
	if Status(err) < 500 || attempt >= 3 {
		detail := err.Error()
		if len(detail) > 500 {
			detail = detail[:500]
		}
		headers := amqp.Table{"x-original-key": key, "x-error": detail, "x-attempt": int32(attempt)}
		if publishErr := p.Publish(ctx, DeadExchange, c.Name, d.Body, headers, d.MessageId); publishErr != nil {
			return publishErr
		}
		return d.Ack(false)
	}
	select {
	case <-time.After(time.Duration(attempt) * 200 * time.Millisecond):
	case <-ctx.Done():
		return ctx.Err()
	}
	headers := amqp.Table{}
	for k, v := range d.Headers {
		headers[k] = v
	}
	headers["x-attempt"] = int32(attempt + 1)
	headers["x-original-key"] = event.Type
	if publishErr := p.Publish(ctx, "", "billiards."+c.Name, d.Body, headers, d.MessageId); publishErr != nil {
		return publishErr
	}
	return d.Ack(false)
}

type Worker struct {
	Context   *Context
	Service   EventHandler
	connected atomic.Bool
}

func (w *Worker) Connected() bool { return w.connected.Load() }
func (w *Worker) Run(ctx context.Context) {
	for ctx.Err() == nil {
		if err := w.runConnection(ctx); err != nil && ctx.Err() == nil {
			log.Printf("RabbitMQ reconnect service=%s: %v", w.Context.Name, err)
		}
		w.connected.Store(false)
		select {
		case <-ctx.Done():
			return
		case <-time.After(2 * time.Second):
		}
	}
}
func (w *Worker) runConnection(ctx context.Context) error {
	conn, err := amqp.DialConfig(w.Context.Settings.RabbitMQURL, amqp.Config{Heartbeat: 60 * time.Second, Dial: amqp.DefaultDial(5 * time.Second)})
	if err != nil {
		return err
	}
	// CloseDeadline bounds both blocked writes and waiting for connection.close-ok.
	// Closing the connection also closes its channels; a preceding channel.Close could hang forever.
	var closeOnce sync.Once
	abort := func() { closeOnce.Do(func() { _ = conn.CloseDeadline(time.Now().Add(time.Second)) }) }
	defer abort()
	stopAbort := context.AfterFunc(ctx, abort)
	defer stopAbort()
	ch, err := conn.Channel()
	if err != nil {
		return err
	}
	if err = DeclareTopology(ch); err != nil {
		return err
	}
	if err = ch.Qos(1, 0, false); err != nil {
		return err
	}
	publishChannel, err := conn.Channel()
	if err != nil {
		return err
	}
	publisher, err := newPublisher(publishChannel, abort)
	if err != nil {
		return err
	}
	deliveries, err := ch.Consume("billiards."+w.Context.Name, "", false, false, false, false, nil)
	if err != nil {
		return err
	}
	closed := conn.NotifyClose(make(chan *amqp.Error, 1))
	w.connected.Store(true)
	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case brokerErr, ok := <-closed:
			if !ok {
				return fmt.Errorf("broker connection closed")
			}
			return brokerErr
		case delivery, ok := <-deliveries:
			if !ok {
				return fmt.Errorf("consumer channel closed")
			}
			if err := HandleDelivery(ctx, w.Context, w.Service, publisher, delivery); err != nil {
				return err
			}
		case <-ticker.C:
			if err := Flush(ctx, w.Context, publisher); err != nil {
				return err
			}
		}
	}
}
