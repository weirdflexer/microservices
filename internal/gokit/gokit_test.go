package gokit

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	amqp "github.com/rabbitmq/amqp091-go"
)

func TestMemoryTransactionRollbackAndDetachedObjects(t *testing.T) {
	store := NewMemoryStore("test")
	ctx := context.Background()
	row := Object{"amount": int64(9007199254740993)}
	if err := store.WithTx(ctx, func(tx *Tx) error {
		tx.Bucket("items")["1"] = row
		tx.Emit("created", Object{"item_id": "1"}, "")
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	row["amount"] = 0
	state, _ := store.Snapshot(ctx)
	if n, _ := Number(Map(state.Data["items"]["1"])["amount"]); n != 9007199254740993 {
		t.Fatal("integer precision or isolation lost")
	}
	err := store.WithTx(ctx, func(tx *Tx) error {
		tx.Bucket("items")["bad"] = true
		tx.Emit("bad", Object{}, "")
		return Fail(409, "rollback")
	})
	if Status(err) != 409 {
		t.Fatal(err)
	}
	state, _ = store.Snapshot(ctx)
	if len(state.Data["items"]) != 1 || len(state.Outbox) != 1 {
		t.Fatal("failed transaction committed")
	}
}
func TestIntervalAndTimezoneValidation(t *testing.T) {
	now := time.Date(2030, 1, 1, 10, 0, 0, 0, time.UTC)
	s, e, err := Interval("2030-01-01T16:00:00+03:00", "2030-01-01T17:30:00+03:00", now)
	if err != nil || ISO(s) != "2030-01-01T13:00:00Z" || e.Sub(s) != 90*time.Minute {
		t.Fatal(s, e, err)
	}
	for _, pair := range [][2]string{{"2030-01-01T13:10:00Z", "2030-01-01T14:10:00Z"}, {"2030-01-01T13:00:00Z", "2030-01-01T13:30:00Z"}, {"2030-01-01T13:00:00Z", "2030-01-01T18:00:00Z"}, {"2030-01-01T09:00:00Z", "2030-01-01T10:00:00Z"}, {"2030-01-01T13:00:00", "2030-01-01T14:00:00Z"}} {
		if _, _, err := Interval(pair[0], pair[1], now); Status(err) != 422 {
			t.Fatal("accepted invalid interval", pair)
		}
	}
}

type handlerFunc func(context.Context, *Tx, Event) error

func (f handlerFunc) Handle(c context.Context, tx *Tx, e Event) error { return f(c, tx, e) }
func validEvent() Event {
	id := UUID()
	return Event{ID: UUID(), Type: "test.created", OccurredAt: ISO(time.Now()), Producer: "test", CorrelationID: &id, Data: Object{"booking_id": id}}
}
func TestInboxDedupCanonicalUUIDAndRollback(t *testing.T) {
	c := NewContext("test", NewMemoryStore("test"), nil)
	event := validEvent()
	calls := 0
	handler := handlerFunc(func(ctx context.Context, tx *Tx, event Event) error {
		calls++
		tx.Bucket("items")["1"] = true
		return nil
	})
	processed, err := ProcessIncoming(context.Background(), handler, c, event)
	if !processed || err != nil {
		t.Fatal(err)
	}
	event.ID = strings.ToUpper(event.ID)
	processed, err = ProcessIncoming(context.Background(), handler, c, event)
	if processed || err != nil || calls != 1 {
		t.Fatal("inbox dedup failed")
	}
	event.ID = UUID()
	bad := handlerFunc(func(ctx context.Context, tx *Tx, event Event) error {
		tx.Bucket("items")["bad"] = true
		return Fail(503, "temporary")
	})
	_, err = ProcessIncoming(context.Background(), bad, c, event)
	if Status(err) != 503 {
		t.Fatal(err)
	}
	state, _ := c.Store.Snapshot(context.Background())
	if _, exists := state.Inbox[event.ID]; exists || len(state.Data["items"]) != 1 {
		t.Fatal("failed event partially committed")
	}
}
func TestInvalidEnvelopeRejected(t *testing.T) {
	event := validEvent()
	raw, _ := json.Marshal(event)
	var body Object
	_ = Decode(raw, &body)
	for _, key := range []string{"id", "type", "occurred_at", "producer", "correlation_id", "data"} {
		copy := Clone(body)
		delete(copy, key)
		invalid, _ := json.Marshal(copy)
		if _, err := ReadEvent(invalid); Status(err) != 422 {
			t.Fatal("missing field accepted", key)
		}
	}
	for _, invalid := range []string{`null`, `[]`, `{"id":1}`, string(raw) + `{}`} {
		if _, err := ReadEvent([]byte(invalid)); Status(err) != 422 {
			t.Fatal("invalid envelope accepted")
		}
	}
}
func TestHTTPGatewayMapsErrorsAndUsesServiceOverride(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/ok":
			WriteJSON(w, 200, Object{"amount": int64(9007199254740993)})
		case "/conflict":
			WriteJSON(w, 409, Object{"detail": "conflict"})
		case "/down":
			WriteJSON(w, 500, Object{"detail": "down"})
		default:
			_, _ = w.Write([]byte("not-json"))
		}
	}))
	defer server.Close()
	t.Setenv("USERS_URL", server.URL)
	gateway := NewHTTPGateway(LoadSettings("bookings"))
	row, err := gateway.Request(context.Background(), "users", "GET", "/ok", nil)
	if err != nil {
		t.Fatal(err)
	}
	if n, _ := Number(row["amount"]); n != 9007199254740993 {
		t.Fatal("gateway rounded money")
	}
	for path, want := range map[string]int{"/conflict": 409, "/down": 503, "/invalid": 503} {
		_, err := gateway.Request(context.Background(), "users", "GET", path, nil)
		if Status(err) != want {
			t.Fatal(path, err)
		}
	}
	server.Close()
	_, err = gateway.Request(context.Background(), "users", "GET", "/ok", nil)
	if Status(err) != 503 {
		t.Fatal("network error not mapped")
	}
}

type recordingPublisher struct {
	calls []publishCall
	fail  error
}
type publishCall struct {
	exchange, key string
	body          []byte
	headers       amqp.Table
}

func (p *recordingPublisher) Publish(ctx context.Context, exchange, key string, body []byte, headers amqp.Table, id string) error {
	p.calls = append(p.calls, publishCall{exchange, key, body, headers})
	return p.fail
}

type ackRecorder struct{ ack, nack, reject int }

func (a *ackRecorder) Ack(tag uint64, multiple bool) error           { a.ack++; return nil }
func (a *ackRecorder) Nack(tag uint64, multiple, requeue bool) error { a.nack++; return nil }
func (a *ackRecorder) Reject(tag uint64, requeue bool) error         { a.reject++; return nil }
func delivery(event Event, a *ackRecorder) amqp.Delivery {
	body, _ := json.Marshal(event)
	return amqp.Delivery{Acknowledger: a, DeliveryTag: 1, Body: body, Exchange: Exchange, RoutingKey: event.Type}
}
func TestFlushRemovesOnlyAfterConfirmedPublication(t *testing.T) {
	c := NewContext("bookings", NewMemoryStore("bookings"), nil)
	_ = c.Store.WithTx(context.Background(), func(tx *Tx) error { tx.Emit("booking.confirmed", Object{"booking_id": UUID()}, ""); return nil })
	p := &recordingPublisher{fail: errors.New("no confirm")}
	if err := Flush(context.Background(), c, p); err == nil {
		t.Fatal("failed publish ignored")
	}
	state, _ := c.Store.Snapshot(context.Background())
	if len(state.Outbox) != 1 {
		t.Fatal("removed unconfirmed event")
	}
	p.fail = nil
	if err := Flush(context.Background(), c, p); err != nil {
		t.Fatal(err)
	}
	state, _ = c.Store.Snapshot(context.Background())
	if len(state.Outbox) != 0 {
		t.Fatal("confirmed event not removed")
	}
}
func TestDeliverySuccessAndRetryOnlyOwnQueue(t *testing.T) {
	c := NewContext("bookings", NewMemoryStore("bookings"), nil)
	event := validEvent()
	ack := &ackRecorder{}
	p := &recordingPublisher{}
	transient := handlerFunc(func(context.Context, *Tx, Event) error { return Fail(503, "temporary") })
	if err := HandleDelivery(context.Background(), c, transient, p, delivery(event, ack)); err != nil {
		t.Fatal(err)
	}
	if ack.ack != 1 || len(p.calls) != 1 || p.calls[0].exchange != "" || p.calls[0].key != "billiards.bookings" || p.calls[0].headers["x-attempt"] != int32(2) {
		t.Fatal("retry not isolated/confirmed before ack")
	}
	retry := delivery(event, ack)
	retry.Exchange = ""
	retry.RoutingKey = "billiards.bookings"
	retry.Headers = p.calls[0].headers
	success := handlerFunc(func(context.Context, *Tx, Event) error { return nil })
	if err := HandleDelivery(context.Background(), c, success, p, retry); err != nil {
		t.Fatal(err)
	}
	if ack.ack != 2 {
		t.Fatal("retry not acknowledged")
	}
}
func TestPermanentOrExhaustedDeliveryConfirmedToDLQ(t *testing.T) {
	for _, test := range []struct {
		status  int
		attempt int32
	}{{409, 1}, {503, 3}} {
		c := NewContext("bookings", NewMemoryStore("bookings"), nil)
		ack := &ackRecorder{}
		p := &recordingPublisher{}
		d := delivery(validEvent(), ack)
		d.Headers = amqp.Table{"x-attempt": test.attempt}
		handler := handlerFunc(func(context.Context, *Tx, Event) error { return Fail(test.status, "failed") })
		if err := HandleDelivery(context.Background(), c, handler, p, d); err != nil {
			t.Fatal(err)
		}
		if ack.ack != 1 || ack.nack != 0 || p.calls[0].exchange != DeadExchange || p.calls[0].key != "bookings" {
			t.Fatal("DLQ publish/ack incorrect")
		}
	}
}
func TestFailedRetryAndDLQPublishLeaveOriginalUnacked(t *testing.T) {
	for _, status := range []int{409, 503} {
		c := NewContext("bookings", NewMemoryStore("bookings"), nil)
		ack := &ackRecorder{}
		p := &recordingPublisher{fail: errors.New("broker failed")}
		handler := handlerFunc(func(context.Context, *Tx, Event) error { return Fail(status, "failed") })
		if err := HandleDelivery(context.Background(), c, handler, p, delivery(validEvent(), ack)); err == nil || ack.ack != 0 || ack.nack != 0 {
			t.Fatal("original acked despite failed publish")
		}
	}
}
func TestMalformedBodyHeaderAndRoutingKeyDeadLettered(t *testing.T) {
	for _, mode := range []string{"body", "attempt", "routing"} {
		c := NewContext("bookings", NewMemoryStore("bookings"), nil)
		ack := &ackRecorder{}
		p := &recordingPublisher{}
		d := delivery(validEvent(), ack)
		switch mode {
		case "body":
			d.Body = []byte(`{"bad":1}`)
		case "attempt":
			d.Headers = amqp.Table{"x-attempt": "2"}
		case "routing":
			d.RoutingKey = "wrong"
		}
		handler := handlerFunc(func(context.Context, *Tx, Event) error {
			t.Fatal("invalid delivery reached business handler")
			return nil
		})
		if err := HandleDelivery(context.Background(), c, handler, p, d); err != nil {
			t.Fatal(err)
		}
		if ack.ack != 1 || len(p.calls) != 1 || p.calls[0].exchange != DeadExchange {
			t.Fatal("malformed delivery not safely deadlettered")
		}
	}
}
func TestHealthcheckRequiresSuccessfulStatus(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(503) }))
	settings := LoadSettings("bookings")
	settings.ListenAddr = strings.TrimPrefix(server.URL, "http://")
	if err := Healthcheck(settings); err == nil {
		t.Fatal("unhealthy accepted")
	}
	server.Close()
}

// This fake reproduces amqp091-go's behavior: the publish call ignores its context
// and returns only when closing the underlying connection interrupts the write.
type blockedPublishChannel struct{ released <-chan struct{} }

func (c *blockedPublishChannel) PublishWithDeferredConfirmWithContext(_ context.Context, _ string, _ string, _ bool, _ bool, _ amqp.Publishing) (*amqp.DeferredConfirmation, error) {
	<-c.released
	return nil, errors.New("socket closed during blocked publish")
}
func TestPublishDeadlineInterruptsBlockedWriteAndRetainsOutbox(t *testing.T) {
	c := NewContext("bookings", NewMemoryStore("bookings"), nil)
	if err := c.Store.WithTx(context.Background(), func(tx *Tx) error { tx.Emit("booking.confirmed", Object{"booking_id": UUID()}, ""); return nil }); err != nil {
		t.Fatal(err)
	}
	released := make(chan struct{})
	var once sync.Once
	abort := func() { once.Do(func() { close(released) }) }
	defer abort()
	publisher := &confirmedPublisher{channel: &blockedPublishChannel{released}, returned: make(chan amqp.Return), abort: abort, timeout: 25 * time.Millisecond}
	done := make(chan error, 1)
	go func() { done <- Flush(context.Background(), c, publisher) }()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("blocked publish incorrectly succeeded")
		}
	case <-time.After(time.Second):
		t.Fatal("publish deadline failed to interrupt write")
	}
	state, err := c.Store.Snapshot(context.Background())
	if err != nil || len(state.Outbox) != 1 {
		t.Fatal("unconfirmed event was lost", err)
	}
}
func TestParentCancellationInterruptsBlockedPublish(t *testing.T) {
	released := make(chan struct{})
	var once sync.Once
	abort := func() { once.Do(func() { close(released) }) }
	defer abort()
	publisher := &confirmedPublisher{channel: &blockedPublishChannel{released}, returned: make(chan amqp.Return), abort: abort, timeout: time.Hour}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- publisher.Publish(ctx, Exchange, "test", []byte(`{}`), nil, UUID()) }()
	cancel()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("cancelled publish incorrectly succeeded")
		}
	case <-time.After(time.Second):
		t.Fatal("cancellation failed to interrupt publish")
	}
}
