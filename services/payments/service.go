// Package payments implements S6: payment accounting with a demo provider only.
package payments

import (
	"context"

	"billiards/internal/gokit"
)

type Service struct{ C *gokit.Context }

func New(c *gokit.Context) *Service { return &Service{c} }
func get(tx *gokit.Tx, id string) (gokit.Object, error) {
	return gokit.Row(tx, "payments", id, "Платёж не найден")
}
func response(row gokit.Object) gokit.Object {
	out := gokit.Clone(row)
	if _, ok := out["refund_reason"]; !ok {
		out["refund_reason"] = nil
	}
	return out
}
func payload(row gokit.Object) gokit.Object {
	return gokit.Object{"payment_id": row["id"], "booking_id": row["booking_id"], "user_id": row["user_id"], "amount": row["amount"], "currency": row["currency"]}
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
func (s *Service) List(ctx context.Context, booking string, offset, limit int) ([]gokit.Object, error) {
	rows := []gokit.Object{}
	err := s.C.Store.WithTx(ctx, func(tx *gokit.Tx) error {
		for _, value := range tx.Bucket("payments") {
			row := gokit.Map(value)
			if booking != "" && row["booking_id"] != booking {
				continue
			}
			rows = append(rows, response(row))
		}
		return nil
	})
	return gokit.Page(rows, offset, limit), err
}
func (s *Service) Handle(ctx context.Context, tx *gokit.Tx, event gokit.Event) error {
	data := event.Data
	switch event.Type {
	case "booking.payment_requested":
		if err := gokit.RequireString(data, "booking_id", "user_id", "currency"); err != nil {
			return err
		}
		amount, valid := gokit.Number(data["amount"])
		if !valid || amount <= 0 || data["currency"] != "RUB" {
			return gokit.Fail(422, "Платёж требует положительную сумму в копейках и RUB")
		}
		bookingID := gokit.Str(data["booking_id"])
		index := tx.Bucket("by_booking")
		if existingID, exists := index[bookingID]; exists {
			existing, err := get(tx, gokit.Str(existingID))
			if err != nil {
				return err
			}
			if !gokit.Equal(existing["amount"], data["amount"]) || existing["currency"] != data["currency"] {
				return gokit.Fail(409, "Повторная заявка изменила сумму платежа")
			}
			return nil
		}
		id := gokit.UUID()
		row := gokit.Object{"id": id, "booking_id": bookingID, "user_id": data["user_id"], "amount": amount, "currency": data["currency"], "status": "pending", "refund_status": nil, "refunded_amount": int64(0), "created_at": gokit.ISO(s.C.Now()), "payment_url": "/api/payments/" + id + ":result", "provider": "demo"}
		tx.Bucket("payments")[id] = row
		index[bookingID] = id
		tx.Emit("payment.created", gokit.Object{"booking_id": bookingID, "payment_id": id, "payment_url": row["payment_url"]}, bookingID)
	case "booking.refund_requested":
		if err := gokit.RequireString(data, "payment_id", "booking_id", "user_id", "currency", "reason"); err != nil {
			return err
		}
		row, err := get(tx, gokit.Str(data["payment_id"]))
		if err != nil {
			return err
		}
		for _, key := range []string{"booking_id", "user_id", "amount", "currency"} {
			if !gokit.Equal(row[key], data[key]) {
				return gokit.Fail(409, "Возврат не соответствует платежу")
			}
		}
		if row["status"] != "succeeded" {
			return gokit.Fail(409, "Вернуть можно только полученную оплату")
		}
		if row["refund_status"] == "pending" || row["refund_status"] == "succeeded" {
			return nil
		}
		row["refund_status"] = "pending"
		row["refund_reason"] = data["reason"]
	}
	return nil
}
func (s *Service) Result(ctx context.Context, id, status string, amount int64, currency string) (gokit.Object, error) {
	if status != "succeeded" && status != "failed" {
		return nil, gokit.Fail(422, "Некорректный статус платежа")
	}
	if amount <= 0 || currency != "RUB" {
		return nil, gokit.Fail(422, "Сумма должна быть положительным целым, валюта RUB")
	}
	var out gokit.Object
	err := s.C.Store.WithTx(ctx, func(tx *gokit.Tx) error {
		row, err := get(tx, id)
		if err != nil {
			return err
		}
		if !gokit.Equal(row["amount"], amount) || row["currency"] != currency {
			return gokit.Fail(409, "Сумма или валюта callback не совпадает с платежом")
		}
		if row["status"] == "succeeded" || row["status"] == status {
			out = response(row)
			return nil
		}
		row["status"] = status
		kind := "payment.failed"
		if status == "succeeded" {
			kind = "payment.succeeded"
		}
		tx.Emit(kind, payload(row), gokit.Str(row["booking_id"]))
		out = response(row)
		return nil
	})
	return out, err
}
func (s *Service) RefundResult(ctx context.Context, id, status string) (gokit.Object, error) {
	if status != "succeeded" && status != "failed" {
		return nil, gokit.Fail(422, "Некорректный статус возврата")
	}
	var out gokit.Object
	err := s.C.Store.WithTx(ctx, func(tx *gokit.Tx) error {
		row, err := get(tx, id)
		if err != nil {
			return err
		}
		if row["refund_status"] == "succeeded" {
			out = response(row)
			return nil
		}
		if row["refund_status"] != "pending" && row["refund_status"] != "failed" {
			return gokit.Fail(409, "Возврат не запрошен")
		}
		if row["refund_status"] == status {
			out = response(row)
			return nil
		}
		row["refund_status"] = status
		kind := "payment.refund_failed"
		if status == "succeeded" {
			row["refunded_amount"] = row["amount"]
			kind = "payment.refunded"
		}
		tx.Emit(kind, payload(row), gokit.Str(row["booking_id"]))
		out = response(row)
		return nil
	})
	return out, err
}
