// Package bookings implements S4: booking lifecycle and compensations.
package bookings

import (
	"context"
	"strings"
	"time"

	"billiards/internal/gokit"
)

type Service struct{ C *gokit.Context }

func New(c *gokit.Context) *Service { return &Service{c} }

type CreateInput struct {
	UserID  string `json:"user_id"`
	TableID string `json:"table_id"`
	QuoteID string `json:"quote_id"`
	StartAt string `json:"start_at"`
	EndAt   string `json:"end_at"`
}

func response(row gokit.Object) gokit.Object {
	copy := gokit.Clone(row)
	if _, ok := copy["cancelled_at"]; !ok {
		copy["cancelled_at"] = nil
	}
	return copy
}
func payload(row gokit.Object) gokit.Object {
	out := gokit.Object{"booking_id": row["id"]}
	for _, key := range []string{"user_id", "table_id", "start_at", "end_at", "amount", "currency"} {
		out[key] = row[key]
	}
	return out
}
func get(tx *gokit.Tx, id string) (gokit.Object, error) {
	return gokit.Row(tx, "bookings", id, "Бронь не найдена")
}
func (s *Service) Get(ctx context.Context, id string) (gokit.Object, error) {
	var out gokit.Object
	err := s.C.Store.WithTx(ctx, func(tx *gokit.Tx) error {
		row, err := get(tx, id)
		if err != nil {
			return err
		}
		out = response(row)
		return nil
	})
	return out, err
}
func (s *Service) List(ctx context.Context, user, status string, offset, limit int) ([]gokit.Object, error) {
	rows := []gokit.Object{}
	err := s.C.Store.WithTx(ctx, func(tx *gokit.Tx) error {
		for _, value := range tx.Bucket("bookings") {
			row := gokit.Map(value)
			if user != "" && row["user_id"] != user {
				continue
			}
			if status != "" && row["status"] != status {
				continue
			}
			rows = append(rows, response(row))
		}
		return nil
	})
	return gokit.Page(rows, offset, limit), err
}
func (s *Service) Create(ctx context.Context, input CreateInput) (gokit.Object, error) {
	start, end, err := gokit.Interval(input.StartAt, input.EndAt, s.C.Now())
	if err != nil {
		return nil, err
	}
	user, err := gokit.ParseUUID(input.UserID)
	if err != nil {
		return nil, err
	}
	table, err := gokit.ParseUUID(input.TableID)
	if err != nil {
		return nil, err
	}
	quoteID, err := gokit.ParseUUID(input.QuoteID)
	if err != nil {
		return nil, err
	}
	if _, err = s.C.HTTP.Request(ctx, "users", "GET", "/api/users/"+user, nil); err != nil {
		return nil, err
	}
	interval := gokit.Object{"table_id": table, "start_at": gokit.ISO(start), "end_at": gokit.ISO(end)}
	quote, err := s.C.HTTP.Request(ctx, "pricing", "POST", "/api/quotes/"+quoteID+":validate", interval)
	if err != nil {
		return nil, err
	}
	amount, valid := gokit.Number(quote["amount"])
	if !valid || amount <= 0 || quote["currency"] != "RUB" {
		return nil, gokit.Fail(503, "Некорректное ценовое предложение")
	}
	bookingID := gokit.UUID()
	var out gokit.Object
	err = s.C.Store.WithTx(ctx, func(tx *gokit.Tx) error {
		holdInput := gokit.Clone(interval)
		holdInput["booking_id"] = bookingID
		hold, err := s.C.HTTP.Request(ctx, "schedule", "POST", "/api/holds", holdInput)
		if err != nil {
			return err
		}
		holdID, err := gokit.ParseUUID(gokit.Str(hold["id"]))
		if err != nil {
			return gokit.Fail(503, "Некорректный ответ расписания")
		}
		expiry, err := gokit.ParseTime(gokit.Str(hold["expires_at"]))
		if err != nil {
			return gokit.Fail(503, "Некорректный ответ расписания")
		}
		booking := gokit.Object{"id": bookingID, "user_id": user, "table_id": table, "quote_id": quoteID, "start_at": gokit.ISO(start), "end_at": gokit.ISO(end), "amount": amount, "currency": "RUB", "hold_id": holdID, "expires_at": gokit.ISO(expiry), "status": "waiting_payment", "payment_id": nil, "payment_url": nil, "paid": false, "refund_status": nil, "payment_attempts": []string{}, "created_at": gokit.ISO(s.C.Now())}
		tx.Bucket("bookings")[bookingID] = booking
		eventData := payload(booking)
		eventData["expires_at"] = booking["expires_at"]
		tx.Emit("booking.payment_requested", eventData, bookingID)
		out = response(booking)
		return nil
	})
	return out, err
}
func refund(tx *gokit.Tx, booking gokit.Object, reason string) {
	if booking["refund_status"] == "pending" || booking["refund_status"] == "succeeded" {
		return
	}
	booking["refund_status"] = "pending"
	tx.Emit("booking.refund_requested", gokit.Object{"booking_id": booking["id"], "user_id": booking["user_id"], "payment_id": booking["payment_id"], "amount": booking["amount"], "currency": booking["currency"], "reason": reason}, gokit.Str(booking["id"]))
}
func closed(tx *gokit.Tx, booking gokit.Object, kind string) {
	data := payload(booking)
	data["hold_id"] = booking["hold_id"]
	tx.Emit(kind, data, gokit.Str(booking["id"]))
}
func (s *Service) Cancel(ctx context.Context, id string) (gokit.Object, error) {
	var out gokit.Object
	err := s.C.Store.WithTx(ctx, func(tx *gokit.Tx) error {
		booking, err := get(tx, id)
		if err != nil {
			return err
		}
		if booking["status"] == "cancelled" {
			out = response(booking)
			return nil
		}
		if booking["status"] != "waiting_payment" && booking["status"] != "confirmed" {
			return gokit.Fail(409, "Эту бронь уже нельзя отменить")
		}
		start, err := gokit.ParseTime(gokit.Str(booking["start_at"]))
		if err != nil {
			return err
		}
		now := s.C.Now()
		if !now.Before(start) {
			return gokit.Fail(409, "Отмена разрешена только до начала игры")
		}
		booking["status"] = "cancelled"
		booking["cancelled_at"] = gokit.ISO(now)
		closed(tx, booking, "booking.cancelled")
		if booking["paid"] == true && start.Sub(now) >= 2*time.Hour {
			refund(tx, booking, "cancelled_at_least_two_hours_before_start")
		}
		out = response(booking)
		return nil
	})
	return out, err
}
func (s *Service) Handle(ctx context.Context, tx *gokit.Tx, event gokit.Event) error {
	kind, data := event.Type, event.Data
	switch kind {
	case "payment.created", "payment.succeeded", "payment.failed", "payment.refunded", "payment.refund_failed", "schedule.hold_expired", "session.completed", "session.no_show":
	default:
		return nil
	}
	if err := gokit.RequireString(data, "booking_id"); err != nil {
		return err
	}
	booking, err := get(tx, gokit.Str(data["booking_id"]))
	if err != nil {
		return err
	}
	if strings.HasPrefix(kind, "payment.") {
		if err := gokit.RequireString(data, "payment_id"); err != nil {
			return err
		}
		incoming := data["payment_id"]
		if booking["payment_id"] != nil && booking["payment_id"] != incoming {
			return gokit.Fail(409, "Платёж не принадлежит брони")
		}
		if kind != "payment.created" {
			amount, valid := gokit.Number(data["amount"])
			if !valid || amount <= 0 {
				return gokit.Fail(422, "Некорректная сумма события")
			}
			if !gokit.Equal(data["amount"], booking["amount"]) || data["currency"] != booking["currency"] {
				return gokit.Fail(409, "Сумма или валюта платежа не совпадает с бронью")
			}
		}
		booking["payment_id"] = incoming
	}
	switch kind {
	case "payment.created":
		if err := gokit.RequireString(data, "payment_url"); err != nil {
			return err
		}
		booking["payment_url"] = data["payment_url"]
	case "payment.failed":
		if booking["paid"] != true {
			already := false
			attempts, _ := booking["payment_attempts"].([]any)
			for _, attempt := range attempts {
				if attempt == "failed" {
					already = true
				}
			}
			if !already {
				booking["payment_attempts"] = append(attempts, "failed")
			}
		}
	case "payment.succeeded":
		wasPaid := booking["paid"] == true
		booking["paid"] = true
		if booking["status"] == "cancelled" || booking["status"] == "expired" || booking["status"] == "rejected" {
			if !wasPaid {
				refund(tx, booking, "late_payment")
			}
			return nil
		}
		if booking["status"] != "waiting_payment" {
			return nil
		}
		if _, err = s.C.HTTP.Request(ctx, "schedule", "POST", "/api/holds/"+gokit.Str(booking["hold_id"])+":confirm", gokit.Object{"booking_id": booking["id"]}); err != nil {
			if gokit.Status(err) >= 500 {
				return err
			}
			booking["status"] = "expired"
			closed(tx, booking, "booking.expired")
			refund(tx, booking, "hold_cannot_be_confirmed")
			return nil
		}
		booking["status"] = "confirmed"
		tx.Emit("booking.confirmed", payload(booking), gokit.Str(booking["id"]))
	case "schedule.hold_expired":
		if err := gokit.RequireString(data, "hold_id"); err != nil {
			return err
		}
		if data["hold_id"] == booking["hold_id"] && booking["status"] == "waiting_payment" {
			booking["status"] = "expired"
			closed(tx, booking, "booking.expired")
			if booking["paid"] == true {
				refund(tx, booking, "hold_expired")
			}
		}
	case "payment.refunded", "payment.refund_failed":
		if booking["refund_status"] == "pending" || booking["refund_status"] == "failed" {
			if kind == "payment.refunded" {
				booking["refund_status"] = "succeeded"
			} else {
				booking["refund_status"] = "failed"
			}
		}
	case "session.completed", "session.no_show":
		if booking["status"] == "confirmed" {
			if kind == "session.completed" {
				booking["status"] = "completed"
			} else {
				booking["status"] = "no_show"
			}
		}
	}
	return nil
}
