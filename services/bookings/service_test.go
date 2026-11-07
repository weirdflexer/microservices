package bookings

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"billiards/internal/gokit"
)

type fakeGateway struct {
	calls        []string
	hold, expiry string
	confirmError error
	allError     error
}

func (f *fakeGateway) Request(ctx context.Context, service, method, path string, data gokit.Object) (gokit.Object, error) {
	f.calls = append(f.calls, service+" "+method+" "+path)
	if f.allError != nil {
		return nil, f.allError
	}
	if strings.HasSuffix(path, ":confirm") {
		return gokit.Object{}, f.confirmError
	}
	if service == "pricing" {
		return gokit.Object{"amount": int64(120000), "currency": "RUB"}, nil
	}
	if service == "schedule" {
		return gokit.Object{"id": f.hold, "expires_at": f.expiry}, nil
	}
	return gokit.Object{"id": gokit.UUID()}, nil
}

type fixture struct {
	s       *Service
	c       *gokit.Context
	gateway *fakeGateway
	input   CreateInput
	now     time.Time
	payment string
}

func setup(t *testing.T) *fixture {
	t.Helper()
	now := time.Date(2030, 1, 1, 10, 0, 0, 0, time.UTC)
	gateway := &fakeGateway{hold: gokit.UUID(), expiry: gokit.ISO(now.Add(10 * time.Minute))}
	c := gokit.NewContext("bookings", gokit.NewMemoryStore("bookings"), gateway)
	f := &fixture{s: New(c), c: c, gateway: gateway, now: now, payment: gokit.UUID(), input: CreateInput{gokit.UUID(), gokit.UUID(), gokit.UUID(), gokit.ISO(now.Add(3 * time.Hour)), gokit.ISO(now.Add(5 * time.Hour))}}
	c.Now = func() time.Time { return f.now }
	return f
}
func (f *fixture) create(t *testing.T) gokit.Object {
	t.Helper()
	row, err := f.s.Create(context.Background(), f.input)
	if err != nil {
		t.Fatal(err)
	}
	return row
}
func (f *fixture) event(kind string, row gokit.Object) gokit.Event {
	id := gokit.Str(row["id"])
	return gokit.Event{ID: gokit.UUID(), Type: kind, OccurredAt: gokit.ISO(f.now), Producer: "test", CorrelationID: &id, Data: gokit.Object{"booking_id": id, "payment_id": f.payment, "amount": row["amount"], "currency": row["currency"]}}
}
func (f *fixture) deliver(e gokit.Event) error {
	_, err := gokit.ProcessIncoming(context.Background(), f.s, f.c, e)
	return err
}
func (f *fixture) get(t *testing.T, row gokit.Object) gokit.Object {
	t.Helper()
	out, err := f.s.Get(context.Background(), gokit.Str(row["id"]))
	if err != nil {
		t.Fatal(err)
	}
	return out
}
func (f *fixture) count(t *testing.T, kind string) int {
	t.Helper()
	state, err := f.c.Store.Snapshot(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	n := 0
	for _, e := range state.Outbox {
		if e.Type == kind {
			n++
		}
	}
	return n
}
func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}
func TestCreateChecksDependenciesAndEmitsRequest(t *testing.T) {
	f := setup(t)
	row := f.create(t)
	if row["status"] != "waiting_payment" || len(f.gateway.calls) != 3 || !strings.HasPrefix(f.gateway.calls[0], "users ") || !strings.HasPrefix(f.gateway.calls[1], "pricing ") || !strings.HasPrefix(f.gateway.calls[2], "schedule ") {
		t.Fatalf("row=%v calls=%v", row, f.gateway.calls)
	}
	if f.count(t, "booking.payment_requested") != 1 {
		t.Fatal("missing request")
	}
}
func TestConfirmationIdempotenceAndInbox(t *testing.T) {
	f := setup(t)
	row := f.create(t)
	event := f.event("payment.succeeded", row)
	must(t, f.deliver(event))
	must(t, f.deliver(event))
	must(t, f.deliver(f.event("payment.succeeded", row)))
	if f.get(t, row)["status"] != "confirmed" || f.count(t, "booking.confirmed") != 1 {
		t.Fatal("duplicate or missing confirmation")
	}
	if len(f.gateway.calls) != 4 {
		t.Fatal("duplicate confirm HTTP call")
	}
}
func TestPaymentAmountAndIdentityMismatchRollback(t *testing.T) {
	f := setup(t)
	row := f.create(t)
	e := f.event("payment.succeeded", row)
	e.Data["amount"] = int64(1)
	if gokit.Status(f.deliver(e)) != 409 {
		t.Fatal("accepted wrong amount")
	}
	if f.get(t, row)["paid"] != false {
		t.Fatal("paid changed")
	}
	must(t, f.deliver(f.event("payment.succeeded", row)))
	e = f.event("payment.succeeded", row)
	e.Data["payment_id"] = gokit.UUID()
	if gokit.Status(f.deliver(e)) != 409 {
		t.Fatal("accepted wrong payment")
	}
}
func TestTransientConfirmRollsBackPaidInboxAndOutbox(t *testing.T) {
	f := setup(t)
	row := f.create(t)
	event := f.event("payment.succeeded", row)
	f.gateway.confirmError = gokit.Fail(503, "network")
	if gokit.Status(f.deliver(event)) != 503 {
		t.Fatal("expected transient")
	}
	state, _ := f.c.Store.Snapshot(context.Background())
	if _, ok := state.Inbox[event.ID]; ok {
		t.Fatal("failed event entered inbox")
	}
	if f.get(t, row)["paid"] != false || f.count(t, "booking.refund_requested") != 0 {
		t.Fatal("transaction did not roll back")
	}
	f.gateway.confirmError = nil
	must(t, f.deliver(event))
	if f.get(t, row)["status"] != "confirmed" {
		t.Fatal("retry did not confirm")
	}
}
func TestPermanentConfirmFailureCompensates(t *testing.T) {
	f := setup(t)
	row := f.create(t)
	f.gateway.confirmError = gokit.Fail(409, "expired")
	must(t, f.deliver(f.event("payment.succeeded", row)))
	got := f.get(t, row)
	if got["status"] != "expired" || got["refund_status"] != "pending" || f.count(t, "booking.expired") != 1 || f.count(t, "booking.refund_requested") != 1 {
		t.Fatalf("bad compensation %v", got)
	}
}
func TestRefundBoundaryAndDuplicatePaidCallback(t *testing.T) {
	for _, minutes := range []int{180, 120, 119} {
		t.Run(time.Duration(minutes).String(), func(t *testing.T) {
			f := setup(t)
			row := f.create(t)
			must(t, f.deliver(f.event("payment.succeeded", row)))
			f.now = time.Date(2030, 1, 1, 13, 0, 0, 0, time.UTC).Add(-time.Duration(minutes) * time.Minute)
			_, err := f.s.Cancel(context.Background(), gokit.Str(row["id"]))
			must(t, err)
			_, err = f.s.Cancel(context.Background(), gokit.Str(row["id"]))
			must(t, err)
			must(t, f.deliver(f.event("payment.succeeded", row)))
			count := 0
			if minutes >= 120 {
				count = 1
			}
			if f.count(t, "booking.refund_requested") != count {
				t.Fatalf("incorrect refund at %d", minutes)
			}
		})
	}
}
func TestLatePaymentAfterUnpaidCancellationAlwaysRefunds(t *testing.T) {
	f := setup(t)
	row := f.create(t)
	f.now = time.Date(2030, 1, 1, 12, 30, 0, 0, time.UTC)
	_, err := f.s.Cancel(context.Background(), gokit.Str(row["id"]))
	must(t, err)
	must(t, f.deliver(f.event("payment.succeeded", row)))
	if f.get(t, row)["refund_status"] != "pending" {
		t.Fatal("late payment not compensated")
	}
}
func TestExpiryOnlyClosesMatchingWaitingHold(t *testing.T) {
	for _, confirmed := range []bool{false, true} {
		f := setup(t)
		row := f.create(t)
		if confirmed {
			must(t, f.deliver(f.event("payment.succeeded", row)))
		}
		e := f.event("schedule.hold_expired", row)
		e.Data["hold_id"] = gokit.UUID()
		must(t, f.deliver(e))
		e.ID = gokit.UUID()
		e.Data["hold_id"] = f.gateway.hold
		must(t, f.deliver(e))
		expected := "expired"
		if confirmed {
			expected = "confirmed"
		}
		if f.get(t, row)["status"] != expected {
			t.Fatal("stale expiry changed confirmed booking")
		}
		if !confirmed {
			must(t, f.deliver(f.event("payment.succeeded", row)))
			if f.get(t, row)["refund_status"] != "pending" {
				t.Fatal("late expiry payment not refunded")
			}
		}
	}
}
func TestCancelAfterStartRejected(t *testing.T) {
	f := setup(t)
	row := f.create(t)
	f.now = f.now.Add(3 * time.Hour)
	_, err := f.s.Cancel(context.Background(), gokit.Str(row["id"]))
	if gokit.Status(err) != 409 {
		t.Fatal("cancelled started booking")
	}
}
func TestPaymentCreatedFailureAndRefundResults(t *testing.T) {
	f := setup(t)
	row := f.create(t)
	e := f.event("payment.created", row)
	e.Data["payment_url"] = "/demo"
	must(t, f.deliver(e))
	must(t, f.deliver(f.event("payment.failed", row)))
	must(t, f.deliver(f.event("payment.failed", row)))
	got := f.get(t, row)
	if got["payment_url"] != "/demo" || len(got["payment_attempts"].([]any)) != 1 {
		t.Fatal("payment details missing")
	}
	must(t, f.deliver(f.event("payment.succeeded", row)))
	_, err := f.s.Cancel(context.Background(), gokit.Str(row["id"]))
	must(t, err)
	must(t, f.deliver(f.event("payment.refund_failed", row)))
	must(t, f.deliver(f.event("payment.refunded", row)))
	must(t, f.deliver(f.event("payment.refund_failed", row)))
	if f.get(t, row)["refund_status"] != "succeeded" {
		t.Fatal("stale refund failure demoted success")
	}
}
func TestSessionOutcomesCloseBookingWithoutRefund(t *testing.T) {
	for _, kind := range []string{"session.completed", "session.no_show"} {
		f := setup(t)
		row := f.create(t)
		must(t, f.deliver(f.event("payment.succeeded", row)))
		must(t, f.deliver(f.event(kind, row)))
		expected := strings.TrimPrefix(kind, "session.")
		if f.get(t, row)["status"] != expected || f.count(t, "booking.refund_requested") != 0 {
			t.Fatal("bad session outcome")
		}
	}
}
func TestHTTPCreateReadListCancelAndValidation(t *testing.T) {
	f := setup(t)
	handler := f.s.Handler(func() bool { return false })
	raw, _ := json.Marshal(f.input)
	request := httptest.NewRequest("POST", "/api/bookings", bytes.NewReader(raw))
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, request)
	if rec.Code != 201 {
		t.Fatalf("%d %s", rec.Code, rec.Body)
	}
	var row gokit.Object
	must(t, gokit.Decode(rec.Body.Bytes(), &row))
	id := gokit.Str(row["id"])
	for _, test := range []struct {
		method, path string
		code         int
	}{{"GET", "/api/bookings/" + id, 200}, {"GET", "/api/bookings?user_id=" + f.input.UserID, 200}, {"GET", "/api/bookings?limit=0", 422}, {"GET", "/api/bookings/invalid", 422}, {"GET", "/api/bookings/" + gokit.UUID(), 404}, {"POST", "/api/bookings/" + id + ":cancel", 200}, {"GET", "/openapi.json", 200}, {"GET", "/docs", 200}, {"GET", "/health", 200}} {
		rec = httptest.NewRecorder()
		handler.ServeHTTP(rec, httptest.NewRequest(test.method, test.path, nil))
		if rec.Code != test.code {
			t.Fatalf("%s got %d expected %d: %s", test.path, rec.Code, test.code, rec.Body)
		}
	}
	for _, body := range []string{`{}`, `null`, strings.Replace(string(raw), f.input.UserID, "bad", 1), strings.TrimSuffix(string(raw), "}") + `,"extra":1}`, strings.Replace(string(raw), f.input.StartAt, "2030-01-01T13:00:00", 1)} {
		rec = httptest.NewRecorder()
		handler.ServeHTTP(rec, httptest.NewRequest("POST", "/api/bookings", strings.NewReader(body)))
		if rec.Code != 422 {
			t.Fatalf("accepted invalid %s: %d", body, rec.Code)
		}
	}
}
func TestHTTPDownstreamFailureNoPartialBooking(t *testing.T) {
	f := setup(t)
	f.gateway.allError = gokit.Fail(503, "unavailable")
	raw, _ := json.Marshal(f.input)
	rec := httptest.NewRecorder()
	f.s.Handler(func() bool { return false }).ServeHTTP(rec, httptest.NewRequest("POST", "/api/bookings", bytes.NewReader(raw)))
	if rec.Code != 503 {
		t.Fatal(rec.Code)
	}
	rows, err := f.s.List(context.Background(), "", "", 0, 100)
	must(t, err)
	if len(rows) != 0 {
		t.Fatal("partial booking")
	}
}
