FROM golang:1.25-alpine AS builder
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY internal/gokit ./internal/gokit
COPY services/bookings ./services/bookings
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/server ./services/bookings/cmd

FROM alpine:3.22
RUN apk add --no-cache ca-certificates && adduser -D -u 10001 app
WORKDIR /app
COPY --from=builder /out/server /app/server
USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=4s --retries=5 CMD ["/app/server", "healthcheck"]
ENTRYPOINT ["/app/server"]
