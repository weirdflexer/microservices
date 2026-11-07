package gokit

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type State struct {
	Data   map[string]Object `json:"data"`
	Inbox  map[string]string `json:"inbox"`
	Outbox []Event           `json:"outbox"`
}

func InitialState() State {
	return State{Data: map[string]Object{}, Inbox: map[string]string{}, Outbox: []Event{}}
}
func (s *State) normalize() {
	if s.Data == nil {
		s.Data = map[string]Object{}
	}
	if s.Inbox == nil {
		s.Inbox = map[string]string{}
	}
	if s.Outbox == nil {
		s.Outbox = []Event{}
	}
}

type Tx struct {
	State *State
	Name  string
}

func (tx *Tx) Bucket(name string) Object {
	bucket, ok := tx.State.Data[name]
	if !ok {
		bucket = Object{}
		tx.State.Data[name] = bucket
	}
	return bucket
}
func (tx *Tx) Emit(kind string, data Object, correlation string) {
	id := correlation
	if id == "" {
		id = Str(data["booking_id"])
	}
	var c *string
	if id != "" {
		c = &id
	}
	tx.State.Outbox = append(tx.State.Outbox, Event{UUID(), kind, ISO(time.Now()), tx.Name, c, Clone(data)})
}
func Row(tx *Tx, bucket, id, notFound string) (Object, error) {
	value := Map(tx.Bucket(bucket)[id])
	if value == nil {
		return nil, Fail(404, notFound)
	}
	return value, nil
}

type Store interface {
	WithTx(context.Context, func(*Tx) error) error
	Snapshot(context.Context) (State, error)
	Close()
}
type MemoryStore struct {
	name  string
	mu    sync.Mutex
	state State
}

func NewMemoryStore(name string) *MemoryStore { return &MemoryStore{name: name, state: InitialState()} }
func (s *MemoryStore) WithTx(ctx context.Context, fn func(*Tx) error) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := ctx.Err(); err != nil {
		return err
	}
	state := Clone(s.state)
	if err := fn(&Tx{State: &state, Name: s.name}); err != nil {
		return err
	}
	s.state = Clone(state)
	return nil
}
func (s *MemoryStore) Snapshot(ctx context.Context) (State, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return Clone(s.state), ctx.Err()
}
func (s *MemoryStore) Close() {}

type PostgresStore struct {
	name string
	pool *pgxpool.Pool
}

func NewPostgresStore(ctx context.Context, name, url string) (*PostgresStore, error) {
	// Accept the existing SQLAlchemy URL from Compose without migrating databases.
	url = strings.Replace(url, "postgresql+psycopg://", "postgresql://", 1)
	pool, err := pgxpool.New(ctx, url)
	if err != nil {
		return nil, err
	}
	closeFailure := func(err error) (*PostgresStore, error) { pool.Close(); return nil, err }
	if err = pool.Ping(ctx); err != nil {
		return closeFailure(err)
	}
	if _, err = pool.Exec(ctx, "CREATE TABLE IF NOT EXISTS service_state (id INTEGER PRIMARY KEY, document JSON NOT NULL)"); err != nil {
		return closeFailure(err)
	}
	initial, _ := json.Marshal(InitialState())
	if _, err = pool.Exec(ctx, "INSERT INTO service_state(id, document) VALUES(1, $1::json) ON CONFLICT (id) DO NOTHING", string(initial)); err != nil {
		return closeFailure(err)
	}
	return &PostgresStore{name, pool}, nil
}
func (s *PostgresStore) WithTx(ctx context.Context, fn func(*Tx) error) error {
	dbtx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return err
	}
	defer dbtx.Rollback(context.Background())
	var raw []byte
	if err = dbtx.QueryRow(ctx, "SELECT document FROM service_state WHERE id=1 FOR UPDATE").Scan(&raw); err != nil {
		return err
	}
	var state State
	if err = Decode(raw, &state); err != nil {
		return fmt.Errorf("invalid stored state: %w", err)
	}
	state.normalize()
	if err = fn(&Tx{State: &state, Name: s.name}); err != nil {
		return err
	}
	raw, err = json.Marshal(state)
	if err != nil {
		return err
	}
	if _, err = dbtx.Exec(ctx, "UPDATE service_state SET document=$1::json WHERE id=1", string(raw)); err != nil {
		return err
	}
	return dbtx.Commit(ctx)
}
func (s *PostgresStore) Snapshot(ctx context.Context) (State, error) {
	var state State
	err := s.WithTx(ctx, func(tx *Tx) error { state = Clone(*tx.State); return nil })
	return state, err
}
func (s *PostgresStore) Close() { s.pool.Close() }
