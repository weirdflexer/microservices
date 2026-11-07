package gokit

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"time"
)

type Settings struct {
	DatabaseURL, RabbitMQURL, ProviderToken, ServiceHostTemplate, ListenAddr string
	HTTPTimeout                                                              time.Duration
}

func env(key, fallback string) string {
	if value, ok := os.LookupEnv(key); ok {
		return value
	}
	return fallback
}
func LoadSettings(name string) Settings {
	seconds, _ := strconv.ParseFloat(env("HTTP_TIMEOUT", "5"), 64)
	if seconds <= 0 {
		seconds = 5
	}
	return Settings{DatabaseURL: strings.ReplaceAll(env("DATABASE_URL", "postgresql://"+name+":"+name+"_dev@localhost:5432/"+name), "{service}", name), RabbitMQURL: env("RABBITMQ_URL", "amqp://micro:micro@localhost:5672/"), ProviderToken: env("PROVIDER_TOKEN", "demo-provider-token"), ServiceHostTemplate: env("SERVICE_HOST_TEMPLATE", "http://{service}:8000"), ListenAddr: env("LISTEN_ADDR", ":8000"), HTTPTimeout: time.Duration(seconds * float64(time.Second))}
}

type HTTPGateway struct {
	Client   *http.Client
	Settings Settings
}

func NewHTTPGateway(settings Settings) *HTTPGateway {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	return &HTTPGateway{Client: &http.Client{Timeout: settings.HTTPTimeout, Transport: transport}, Settings: settings}
}
func (h *HTTPGateway) Request(ctx context.Context, service, method, path string, data Object) (Object, error) {
	base := env(strings.ToUpper(service)+"_URL", strings.ReplaceAll(h.Settings.ServiceHostTemplate, "{service}", service))
	var body io.Reader
	if data != nil {
		raw, _ := json.Marshal(data)
		body = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(ctx, method, strings.TrimRight(base, "/")+path, body)
	if err != nil {
		return nil, Fail(503, "Сервис "+service+" недоступен")
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := h.Client.Do(req)
	if err != nil {
		return nil, Fail(503, "Сервис "+service+" недоступен")
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return nil, Fail(503, "Не удалось прочитать ответ "+service)
	}
	var result Object
	if len(raw) > 0 {
		_ = Decode(raw, &result)
	}
	if resp.StatusCode >= 400 {
		status := resp.StatusCode
		if status >= 500 {
			status = 503
		}
		detail := string(raw)
		if d, ok := result["detail"]; ok {
			detail = fmt.Sprint(d)
		}
		return nil, Fail(status, detail)
	}
	if len(raw) > 0 && result == nil {
		return nil, Fail(503, "Некорректный ответ "+service)
	}
	return result, nil
}

type Endpoint func(*http.Request) (int, any, error)

func ServeEndpoint(fn Endpoint) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if recovered := recover(); recovered != nil {
				log.Printf("HTTP panic: %v", recovered)
				WriteJSON(w, 500, Object{"detail": "Внутренняя ошибка сервиса"})
			}
		}()
		status, data, err := fn(r)
		if err != nil {
			code := Status(err)
			detail := err.Error()
			if code == 500 {
				log.Printf("HTTP failure: %v", err)
				detail = "Внутренняя ошибка сервиса"
			}
			WriteJSON(w, code, Object{"detail": detail})
			return
		}
		WriteJSON(w, status, data)
	}
}
func WriteJSON(w http.ResponseWriter, status int, data any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(data)
}
func ReadJSON(r *http.Request, target any) error {
	raw, err := io.ReadAll(io.LimitReader(r.Body, (1<<20)+1))
	if err != nil || len(raw) > 1<<20 {
		return Fail(422, "Не удалось прочитать тело запроса или превышен размер 1 MiB")
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil || fields == nil {
		return Fail(422, "Тело запроса должно быть JSON-объектом")
	}
	// encoding/json normally matches struct fields case-insensitively; the public contracts require exact names.
	typ := reflect.TypeOf(target).Elem()
	allowed := map[string]bool{}
	for i := 0; i < typ.NumField(); i++ {
		name := strings.Split(typ.Field(i).Tag.Get("json"), ",")[0]
		allowed[name] = true
	}
	for name := range fields {
		if !allowed[name] {
			return Fail(422, "Неизвестное поле запроса: "+name)
		}
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return Fail(422, "Некорректное тело запроса: "+err.Error())
	}
	var extra any
	if err := decoder.Decode(&extra); err != io.EOF {
		return Fail(422, "Тело должно содержать один JSON-объект")
	}
	return nil
}

func Pagination(r *http.Request) (int, int, error) {
	offset, limit := 0, 100
	var err error
	query := r.URL.Query()
	if _, ok := query["offset"]; ok {
		offset, err = strconv.Atoi(query.Get("offset"))
		if err != nil || offset < 0 {
			return 0, 0, Fail(422, "offset должен быть неотрицательным целым")
		}
	}
	if _, ok := query["limit"]; ok {
		limit, err = strconv.Atoi(query.Get("limit"))
		if err != nil || limit < 1 || limit > 100 {
			return 0, 0, Fail(422, "limit должен быть от 1 до 100")
		}
	}
	return offset, limit, nil
}
func OptionalUUID(r *http.Request, name string) (string, error) {
	if _, ok := r.URL.Query()[name]; !ok {
		return "", nil
	}
	return ParseUUID(r.URL.Query().Get(name))
}
func Page(rows []Object, offset, limit int) []Object {
	sort.SliceStable(rows, func(i, j int) bool {
		a, b := Str(rows[i]["created_at"]), Str(rows[j]["created_at"])
		if a != b {
			return a < b
		}
		return Str(rows[i]["id"]) < Str(rows[j]["id"])
	})
	if offset >= len(rows) {
		return []Object{}
	}
	end := offset + limit
	if end > len(rows) {
		end = len(rows)
	}
	return rows[offset:end]
}
func Method(allowed string) error {
	return Fail(http.StatusMethodNotAllowed, "Разрешён метод "+allowed)
}
func TechnicalRoutes(mux *http.ServeMux, name string, openapi []byte, connected func() bool) {
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		WriteJSON(w, 200, Object{"service": name, "status": "ok", "broker_connected": connected()})
	})
	mux.HandleFunc("GET /openapi.json", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(openapi)
	})
	mux.HandleFunc("GET /docs", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = io.WriteString(w, `<!doctype html><html><head><title>Billiards API</title><link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css"></head><body><div id="swagger-ui"></div><script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script><script>SwaggerUIBundle({url:'/openapi.json',dom_id:'#swagger-ui'});</script></body></html>`)
	})
}
func IsClosed(err error) bool { return errors.Is(err, http.ErrServerClosed) }
