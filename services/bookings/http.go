package bookings

import (
	_ "embed"
	"net/http"
	"strings"

	"billiards/internal/gokit"
)

//go:embed openapi.json
var OpenAPI []byte

func (s *Service) Handler(connected func() bool) http.Handler {
	mux := http.NewServeMux()
	gokit.TechnicalRoutes(mux, "bookings", OpenAPI, connected)
	mux.HandleFunc("/api/bookings", gokit.ServeEndpoint(func(r *http.Request) (int, any, error) {
		switch r.Method {
		case "POST":
			var body CreateInput
			if err := gokit.ReadJSON(r, &body); err != nil {
				return 0, nil, err
			}
			row, err := s.Create(r.Context(), body)
			return 201, row, err
		case "GET":
			offset, limit, err := gokit.Pagination(r)
			if err != nil {
				return 0, nil, err
			}
			user, err := gokit.OptionalUUID(r, "user_id")
			if err != nil {
				return 0, nil, err
			}
			rows, err := s.List(r.Context(), user, r.URL.Query().Get("status"), offset, limit)
			return 200, rows, err
		default:
			return 0, nil, gokit.Method("GET, POST")
		}
	}))
	mux.HandleFunc("/api/bookings/", gokit.ServeEndpoint(func(r *http.Request) (int, any, error) {
		tail := strings.TrimPrefix(r.URL.Path, "/api/bookings/")
		idText, action, _ := strings.Cut(tail, ":")
		id, err := gokit.ParseUUID(idText)
		if err != nil {
			return 0, nil, err
		}
		if action == "cancel" {
			if r.Method != "POST" {
				return 0, nil, gokit.Method("POST")
			}
			row, err := s.Cancel(r.Context(), id)
			return 200, row, err
		}
		if action != "" {
			return 0, nil, gokit.Fail(404, "Маршрут не найден")
		}
		if r.Method != "GET" {
			return 0, nil, gokit.Method("GET")
		}
		row, err := s.Get(r.Context(), id)
		return 200, row, err
	}))
	return mux
}
