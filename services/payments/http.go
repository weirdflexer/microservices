package payments

import (
	"crypto/subtle"
	_ "embed"
	"net/http"
	"strings"

	"billiards/internal/gokit"
)

//go:embed openapi.json
var OpenAPI []byte

type resultInput struct {
	Status   string  `json:"status"`
	Amount   *int64  `json:"amount"`
	Currency *string `json:"currency"`
}
type refundInput struct {
	Status string `json:"status"`
}

func (s *Service) authorize(r *http.Request) error {
	token := r.Header.Get("X-Provider-Token")
	expected := s.C.Settings.ProviderToken
	if token == "" || subtle.ConstantTimeCompare([]byte(token), []byte(expected)) != 1 {
		return gokit.Fail(401, "Некорректный X-Provider-Token")
	}
	return nil
}
func (s *Service) Handler(connected func() bool) http.Handler {
	mux := http.NewServeMux()
	gokit.TechnicalRoutes(mux, "payments", OpenAPI, connected)
	mux.HandleFunc("/api/payments", gokit.ServeEndpoint(func(r *http.Request) (int, any, error) {
		if r.Method != "GET" {
			return 0, nil, gokit.Method("GET")
		}
		offset, limit, err := gokit.Pagination(r)
		if err != nil {
			return 0, nil, err
		}
		booking, err := gokit.OptionalUUID(r, "booking_id")
		if err != nil {
			return 0, nil, err
		}
		rows, err := s.List(r.Context(), booking, offset, limit)
		return 200, rows, err
	}))
	mux.HandleFunc("/api/payments/", gokit.ServeEndpoint(func(r *http.Request) (int, any, error) {
		tail := strings.TrimPrefix(r.URL.Path, "/api/payments/")
		idText, action, _ := strings.Cut(tail, ":")
		id, err := gokit.ParseUUID(idText)
		if err != nil {
			return 0, nil, err
		}
		if action == "" {
			if r.Method != "GET" {
				return 0, nil, gokit.Method("GET")
			}
			row, err := s.Get(r.Context(), id)
			return 200, row, err
		}
		if action != "result" && action != "refund-result" {
			return 0, nil, gokit.Fail(404, "Маршрут не найден")
		}
		if r.Method != "POST" {
			return 0, nil, gokit.Method("POST")
		}
		if action == "result" {
			currency := "RUB"
			body := resultInput{Currency: &currency}
			if err := gokit.ReadJSON(r, &body); err != nil {
				return 0, nil, err
			}
			if body.Amount == nil || body.Currency == nil || *body.Amount <= 0 || *body.Currency != "RUB" || (body.Status != "succeeded" && body.Status != "failed") {
				return 0, nil, gokit.Fail(422, "Некорректный результат оплаты")
			}
			if err := s.authorize(r); err != nil {
				return 0, nil, err
			}
			row, err := s.Result(r.Context(), id, body.Status, *body.Amount, *body.Currency)
			return 200, row, err
		}
		var body refundInput
		if err := gokit.ReadJSON(r, &body); err != nil {
			return 0, nil, err
		}
		if body.Status != "succeeded" && body.Status != "failed" {
			return 0, nil, gokit.Fail(422, "Некорректный результат возврата")
		}
		if err := s.authorize(r); err != nil {
			return 0, nil, err
		}
		row, err := s.RefundResult(r.Context(), id, body.Status)
		return 200, row, err
	}))
	return mux
}
