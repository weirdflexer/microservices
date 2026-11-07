package gokit

import (
	"context"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"
)

type Application interface {
	EventHandler
	Handler(func() bool) http.Handler
}

// Run starts a standalone service; it never embeds another business service in the same process.
func Run(name string, build func(*Context) Application) {
	if len(os.Args) > 1 && os.Args[1] == "healthcheck" {
		if err := Healthcheck(LoadSettings(name)); err != nil {
			log.Print(err)
			os.Exit(1)
		}
		return
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	settings := LoadSettings(name)
	connectCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	store, err := NewPostgresStore(connectCtx, name, settings.DatabaseURL)
	cancel()
	if err != nil {
		log.Fatalf("database startup: %v", err)
	}
	defer store.Close()
	c := NewContext(name, store, NewHTTPGateway(settings))
	c.Settings = settings
	service := build(c)
	worker := &Worker{Context: c, Service: service}
	workerDone := make(chan struct{})
	go func() { defer close(workerDone); worker.Run(ctx) }()
	server := &http.Server{Addr: settings.ListenAddr, Handler: service.Handler(worker.Connected), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second, WriteTimeout: 30 * time.Second, IdleTimeout: 60 * time.Second}
	fail := make(chan error, 1)
	go func() { log.Printf("%s listening on %s", name, settings.ListenAddr); fail <- server.ListenAndServe() }()
	select {
	case <-ctx.Done():
	case err = <-fail:
		if !IsClosed(err) {
			log.Printf("HTTP server: %v", err)
		}
		stop()
	}
	shutdownCtx, done := context.WithTimeout(context.Background(), 10*time.Second)
	defer done()
	_ = server.Shutdown(shutdownCtx)
	stop()
	select {
	case <-workerDone:
	case <-shutdownCtx.Done():
	}
}

func Healthcheck(settings Settings) error {
	client := &http.Client{Timeout: 3 * time.Second}
	address := settings.ListenAddr
	if strings.HasPrefix(address, ":") {
		address = "127.0.0.1" + address
	} else if host, port, err := net.SplitHostPort(address); err == nil && (host == "0.0.0.0" || host == "::" || host == "") {
		address = net.JoinHostPort("127.0.0.1", port)
	}
	response, err := client.Get("http://" + address + "/health")
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return fmt.Errorf("health returned %d", response.StatusCode)
	}
	return nil
}
