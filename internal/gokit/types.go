// Package gokit contains infrastructure shared by the two independently deployed Go services.
package gokit

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"strings"
	"time"
)

type Object = map[string]any

type DomainError struct {
	Status int
	Detail string
}

func (e *DomainError) Error() string       { return e.Detail }
func Fail(status int, detail string) error { return &DomainError{status, detail} }
func Status(err error) int {
	var domain *DomainError
	if errors.As(err, &domain) {
		return domain.Status
	}
	return 500
}
func Decode(data []byte, target any) error {
	d := json.NewDecoder(bytes.NewReader(data))
	d.UseNumber()
	if err := d.Decode(target); err != nil {
		return err
	}
	var extra any
	if err := d.Decode(&extra); err != io.EOF {
		return fmt.Errorf("expected one JSON value")
	}
	return nil
}
func Clone[T any](value T) T {
	raw, err := json.Marshal(value)
	if err != nil {
		panic(err)
	}
	var out T
	if err = Decode(raw, &out); err != nil {
		panic(err)
	}
	return out
}
func Map(value any) Object {
	if m, ok := value.(map[string]any); ok {
		return m
	}
	return nil
}
func Str(value any) string { s, _ := value.(string); return s }
func Number(value any) (int64, bool) {
	switch n := value.(type) {
	case int:
		return int64(n), true
	case int64:
		return n, true
	case json.Number:
		i, e := n.Int64()
		return i, e == nil
	case float64:
		if !math.IsNaN(n) && !math.IsInf(n, 0) && n == math.Trunc(n) && n >= math.MinInt64 && n < math.MaxInt64 {
			return int64(n), true
		}
	}
	return 0, false
}
func Equal(a, b any) bool {
	na, oka := Number(a)
	nb, okb := Number(b)
	if oka && okb {
		return na == nb
	}
	return fmt.Sprintf("%T:%v", a, a) == fmt.Sprintf("%T:%v", b, b)
}
func UUID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err)
	}
	b[6] = (b[6] & 15) | 64
	b[8] = (b[8] & 63) | 128
	s := hex.EncodeToString(b[:])
	return s[:8] + "-" + s[8:12] + "-" + s[12:16] + "-" + s[16:20] + "-" + s[20:]
}
func ParseUUID(value string) (string, error) {
	s := strings.TrimPrefix(strings.ToLower(value), "urn:uuid:")
	s = strings.Trim(s, "{}")
	s = strings.ReplaceAll(s, "-", "")
	if len(s) != 32 {
		return "", Fail(422, "Некорректный UUID")
	}
	if _, err := hex.DecodeString(s); err != nil {
		return "", Fail(422, "Некорректный UUID")
	}
	return s[:8] + "-" + s[8:12] + "-" + s[12:16] + "-" + s[16:20] + "-" + s[20:], nil
}
func ISO(t time.Time) string { return t.UTC().Format(time.RFC3339Nano) }
func ParseTime(value string) (time.Time, error) {
	t, e := time.Parse(time.RFC3339Nano, value)
	if e != nil {
		return t, Fail(422, "Укажите время ISO 8601 с часовым поясом")
	}
	return t.UTC(), nil
}
func Interval(start, end string, now time.Time) (time.Time, time.Time, error) {
	s, e := ParseTime(start)
	if e != nil {
		return s, s, e
	}
	f, e := ParseTime(end)
	if e != nil {
		return s, f, e
	}
	if s.Minute()%30 != 0 || s.Second() != 0 || s.Nanosecond() != 0 {
		return s, f, Fail(422, "Начало игры должно быть кратно 30 минутам")
	}
	duration := f.Sub(s)
	if duration < time.Hour || duration > 4*time.Hour || duration%(30*time.Minute) != 0 {
		return s, f, Fail(422, "Длительность: от 1 до 4 часов с шагом 30 минут")
	}
	if !s.After(now) {
		return s, f, Fail(422, "Начало игры должно быть в будущем")
	}
	return s, f, nil
}

type Event struct {
	ID            string  `json:"id"`
	Type          string  `json:"type"`
	OccurredAt    string  `json:"occurred_at"`
	Producer      string  `json:"producer"`
	CorrelationID *string `json:"correlation_id"`
	Data          Object  `json:"data"`
}

func ReadEvent(body []byte) (Event, error) {
	var object Object
	var event Event
	if err := Decode(body, &object); err != nil {
		return event, Fail(422, "Некорректный конверт события")
	}
	for _, key := range []string{"id", "type", "occurred_at", "producer", "correlation_id", "data"} {
		if _, ok := object[key]; !ok {
			return event, Fail(422, "Некорректный конверт события")
		}
	}
	if err := Decode(body, &event); err != nil || event.Data == nil || strings.TrimSpace(event.Type) == "" || strings.TrimSpace(event.Producer) == "" {
		return event, Fail(422, "Некорректный конверт события")
	}
	id, err := ParseUUID(event.ID)
	if err != nil {
		return event, Fail(422, "Некорректный конверт события")
	}
	event.ID = id
	if _, err = ParseTime(event.OccurredAt); err != nil {
		return event, Fail(422, "Некорректный конверт события")
	}
	return event, nil
}
func Require(data Object, fields ...string) error {
	for _, field := range fields {
		if _, ok := data[field]; !ok {
			return Fail(422, "Отсутствует поле события: "+field)
		}
	}
	return nil
}
func RequireString(data Object, fields ...string) error {
	for _, field := range fields {
		if s, ok := data[field].(string); !ok || s == "" {
			return Fail(422, "Некорректное поле события: "+field)
		}
	}
	return nil
}

type Gateway interface {
	Request(context.Context, string, string, string, Object) (Object, error)
}
type Context struct {
	Name     string
	Store    Store
	HTTP     Gateway
	Now      func() time.Time
	Settings Settings
}

func NewContext(name string, store Store, http Gateway) *Context {
	return &Context{Name: name, Store: store, HTTP: http, Now: time.Now, Settings: LoadSettings(name)}
}

type EventHandler interface {
	Handle(context.Context, *Tx, Event) error
}

func ProcessIncoming(ctx context.Context, service EventHandler, c *Context, event Event) (bool, error) {
	raw, _ := json.Marshal(event)
	checked, err := ReadEvent(raw)
	if err != nil {
		return false, err
	}
	processed := false
	err = c.Store.WithTx(ctx, func(tx *Tx) error {
		if _, exists := tx.State.Inbox[checked.ID]; exists {
			return nil
		}
		if err := service.Handle(ctx, tx, checked); err != nil {
			return err
		}
		tx.State.Inbox[checked.ID] = ISO(c.Now())
		processed = true
		return nil
	})
	return processed, err
}
