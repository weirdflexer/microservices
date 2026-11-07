package payments

import (
	"context"
	"encoding/json"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"billiards/internal/gokit"
)

type fixture struct {
	s       *Service
	c       *gokit.Context
	row     gokit.Object
	request gokit.Event
}

func setup(t *testing.T) *fixture {
	t.Helper()
	c := gokit.NewContext("payments", gokit.NewMemoryStore("payments"), nil)
	c.Settings.ProviderToken = "demo-provider-token"
	c.Now = func() time.Time { return time.Date(2030, 1, 1, 10, 0, 0, 0, time.UTC) }
	s := New(c)
	booking := gokit.UUID()
	event := gokit.Event{ID: gokit.UUID(), Type: "booking.payment_requested", OccurredAt: gokit.ISO(c.Now()), Producer: "bookings", CorrelationID: &booking, Data: gokit.Object{"booking_id": booking, "user_id": gokit.UUID(), "amount": int64(50000), "currency": "RUB"}}
	_, err := gokit.ProcessIncoming(context.Background(), s, c, event)
	if err != nil {
		t.Fatal(err)
	}
	rows, err := s.List(context.Background(), "", 0, 100)
	if err != nil {
		t.Fatal(err)
	}
	return &fixture{s, c, rows[0], event}
}
func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}
func (f *fixture) id() string { return gokit.Str(f.row["id"]) }
func (f *fixture) get(t *testing.T) gokit.Object {
	t.Helper()
	row, err := f.s.Get(context.Background(), f.id())
	must(t, err)
	return row
}
func (f *fixture) count(t *testing.T, kind string) int {
	t.Helper()
	state, err := f.c.Store.Snapshot(context.Background())
	must(t, err)
	n := 0
	for _, e := range state.Outbox {
		if e.Type == kind {
			n++
		}
	}
	return n
}
func (f *fixture) refund(t *testing.T) {
	t.Helper()
	data := gokit.Clone(f.request.Data)
	data["payment_id"] = f.id()
	data["reason"] = "cancelled"
	event := f.request
	event.ID = gokit.UUID()
	event.Type = "booking.refund_requested"
	event.Data = data
	_, err := gokit.ProcessIncoming(context.Background(), f.s, f.c, event)
	must(t, err)
}
func TestRequestUniqueByBookingAndRejectChangedAmount(t *testing.T) {
	f := setup(t)
	e := f.request
	e.ID = gokit.UUID()
	_, err := gokit.ProcessIncoming(context.Background(), f.s, f.c, e)
	must(t, err)
	if f.count(t, "payment.created") != 1 {
		t.Fatal("duplicate payment")
	}
	e.ID = gokit.UUID()
	e.Data = gokit.Clone(e.Data)
	e.Data["amount"] = int64(10)
	_, err = gokit.ProcessIncoming(context.Background(), f.s, f.c, e)
	if gokit.Status(err) != 409 {
		t.Fatal("amount change accepted")
	}
}
func TestSuccessIdempotentAndCannotDemote(t *testing.T) {
	f := setup(t)
	for _, status := range []string{"succeeded", "succeeded", "failed"} {
		_, err := f.s.Result(context.Background(), f.id(), status, 50000, "RUB")
		must(t, err)
	}
	if f.get(t)["status"] != "succeeded" || f.count(t, "payment.succeeded") != 1 || f.count(t, "payment.failed") != 0 {
		t.Fatal("duplicate success or demotion")
	}
}
func TestFailureCanLaterSucceed(t *testing.T) {
	f := setup(t)
	_, err := f.s.Result(context.Background(), f.id(), "failed", 50000, "RUB")
	must(t, err)
	_, err = f.s.Result(context.Background(), f.id(), "succeeded", 50000, "RUB")
	must(t, err)
	if f.get(t)["status"] != "succeeded" || f.count(t, "payment.failed") != 1 {
		t.Fatal("failed attempt blocked success")
	}
}
func TestWrongAmountDoesNotMutate(t *testing.T) {
	f := setup(t)
	_, err := f.s.Result(context.Background(), f.id(), "succeeded", 1, "RUB")
	if gokit.Status(err) != 409 || f.get(t)["status"] != "pending" {
		t.Fatal("amount mismatch accepted")
	}
}
func TestRefundRequiresPaidAndRequested(t *testing.T) {
	f := setup(t)
	_, err := f.s.RefundResult(context.Background(), f.id(), "succeeded")
	if gokit.Status(err) != 409 {
		t.Fatal("unrequested refund accepted")
	}
	data := gokit.Clone(f.request.Data)
	data["payment_id"] = f.id()
	data["reason"] = "cancelled"
	event := f.request
	event.ID = gokit.UUID()
	event.Type = "booking.refund_requested"
	event.Data = data
	_, err = gokit.ProcessIncoming(context.Background(), f.s, f.c, event)
	if gokit.Status(err) != 409 {
		t.Fatal("unpaid refund accepted")
	}
}
func TestFullRefundIdempotenceAndStaleFailure(t *testing.T) {
	f := setup(t)
	_, err := f.s.Result(context.Background(), f.id(), "succeeded", 50000, "RUB")
	must(t, err)
	f.refund(t)
	f.refund(t)
	for _, status := range []string{"failed", "succeeded", "succeeded", "failed"} {
		_, err = f.s.RefundResult(context.Background(), f.id(), status)
		must(t, err)
	}
	row := f.get(t)
	if !gokit.Equal(row["refunded_amount"], int64(50000)) || row["refund_status"] != "succeeded" || f.count(t, "payment.refunded") != 1 {
		t.Fatalf("invalid refund %v", row)
	}
}
func TestConcurrentCallbacksEmitOnce(t *testing.T) {
	f := setup(t)
	var wg sync.WaitGroup
	errs := make(chan error, 20)
	for range 20 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, err := f.s.Result(context.Background(), f.id(), "succeeded", 50000, "RUB")
			errs <- err
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		must(t, err)
	}
	if f.count(t, "payment.succeeded") != 1 {
		t.Fatal("concurrent callbacks duplicated event")
	}
}
func TestHTTPAuthenticationStrictAmountsAndDefaultCurrency(t *testing.T) {
	f := setup(t)
	handler := f.s.Handler(func() bool { return false })
	path := "/api/payments/" + f.id() + ":result"
	for _, test := range []struct {
		body, token string
		code        int
	}{{`{"status":"succeeded","amount":50000}`, "", 401}, {`{"status":"succeeded","amount":50000}`, "wrong", 401}, {`{"status":"succeeded","amount":"50000"}`, "demo-provider-token", 422}, {`{"status":"succeeded","amount":true}`, "demo-provider-token", 422}, {`{"status":"succeeded","amount":50000.0}`, "demo-provider-token", 422}, {`{"status":"succeeded","amount":50000,"currency":null}`, "demo-provider-token", 422}, {`{"status":"succeeded","amount":50000,"extra":1}`, "demo-provider-token", 422}, {`{"status":"succeeded","amount":null}`, "demo-provider-token", 422}, {`{"status":"succeeded","amount":1}`, "demo-provider-token", 409}, {`{"status":"succeeded","amount":50000}`, "demo-provider-token", 200}} {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest("POST", path, strings.NewReader(test.body))
		req.Header.Set("X-Provider-Token", test.token)
		handler.ServeHTTP(rec, req)
		if rec.Code != test.code {
			t.Fatalf("%s got %d want %d: %s", test.body, rec.Code, test.code, rec.Body)
		}
	}
	if f.count(t, "payment.succeeded") != 1 {
		t.Fatal("incorrect callback count")
	}
}
func TestHTTPGetFilterPaginationAndRefundCallback(t *testing.T) {
	f := setup(t)
	handler := f.s.Handler(func() bool { return false })
	for _, test := range []struct {
		path string
		code int
	}{{"/api/payments", 200}, {"/api/payments/" + f.id(), 200}, {"/api/payments/" + gokit.UUID(), 404}, {"/api/payments?booking_id=bad", 422}, {"/api/payments?offset=-1", 422}, {"/api/payments?limit=101", 422}, {"/openapi.json", 200}, {"/health", 200}, {"/docs", 200}} {
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, httptest.NewRequest("GET", test.path, nil))
		if rec.Code != test.code {
			t.Fatalf("%s %d: %s", test.path, rec.Code, rec.Body)
		}
	}
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest("GET", "/api/payments?booking_id="+gokit.UUID(), nil))
	if strings.TrimSpace(rec.Body.String()) != "[]" {
		t.Fatal("empty list must be []")
	}
	_, err := f.s.Result(context.Background(), f.id(), "succeeded", 50000, "RUB")
	must(t, err)
	f.refund(t)
	rec = httptest.NewRecorder()
	req := httptest.NewRequest("POST", "/api/payments/"+f.id()+":refund-result", strings.NewReader(`{"status":"succeeded"}`))
	req.Header.Set("X-Provider-Token", "demo-provider-token")
	handler.ServeHTTP(rec, req)
	if rec.Code != 200 {
		t.Fatal(rec.Code, rec.Body)
	}
	var row gokit.Object
	must(t, json.Unmarshal(rec.Body.Bytes(), &row))
	if row["refunded_amount"] != float64(50000) {
		t.Fatal("wrong HTTP refunded amount")
	}
}
