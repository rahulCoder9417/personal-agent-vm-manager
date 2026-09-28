package sandbox

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/personal-agent/vm-manager/internal/bridge"
	"github.com/personal-agent/vm-manager/internal/clock"
	"github.com/personal-agent/vm-manager/internal/config"
	"github.com/personal-agent/vm-manager/internal/docker"
	"github.com/personal-agent/vm-manager/internal/egress"
	"github.com/personal-agent/vm-manager/internal/platformfile"
	"github.com/personal-agent/vm-manager/internal/store"
)

var t0 = time.Date(2026, 9, 1, 12, 0, 0, 0, time.UTC)

// fakeBridge answers like the Bridge in each fake container, found by IP.
type fakeBridge struct {
	eng *docker.Fake

	mu        sync.Mutex
	unhealthy map[string]bool // user -> /healthz fails
	busy      map[string]int  // user -> busy count reported by /healthz
	shutdowns []string        // users
	delivered []bridge.DeliverRequest
	// script returns the events for a delivery; block (if set) is waited on before the last event.
	script func(userID string, req bridge.DeliverRequest) []bridge.Event
	block  chan struct{}
	// onEvent runs after each event is handed out (lets tests look at state mid-turn).
	onEvent func(userID string, ev bridge.Event)
}

func newFakeBridge(eng *docker.Fake) *fakeBridge {
	return &fakeBridge{eng: eng, unhealthy: map[string]bool{}, busy: map[string]int{}}
}

func (b *fakeBridge) containerFor(addr string) (docker.Container, bool) {
	ip := addr[:strings.LastIndex(addr, ":")]
	list, _ := b.eng.ListContainers(context.Background(), nil)
	for _, c := range list {
		for _, cip := range c.IPs {
			if cip == ip && c.Running {
				return c, true
			}
		}
	}
	return docker.Container{}, false
}

func (b *fakeBridge) checkSecret(c docker.Container, secret string) error {
	f, err := platformfile.Read(context.Background(), b.eng, c.ID)
	if err != nil {
		return err
	}
	if f.BridgeSecret != secret {
		return bridge.ErrUnauthorized
	}
	return nil
}

func (b *fakeBridge) Health(_ context.Context, addr string) (bridge.Health, error) {
	c, ok := b.containerFor(addr)
	if !ok {
		return bridge.Health{}, errors.New("connection refused")
	}
	uid := c.Labels[LabelUserID]
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.unhealthy[uid] {
		return bridge.Health{}, errors.New("healthz: 503")
	}
	return bridge.Health{OK: true, Busy: b.busy[uid]}, nil
}

func (b *fakeBridge) Shutdown(_ context.Context, addr, secret string) error {
	c, ok := b.containerFor(addr)
	if !ok {
		return errors.New("connection refused")
	}
	if err := b.checkSecret(c, secret); err != nil {
		return err
	}
	b.mu.Lock()
	b.shutdowns = append(b.shutdowns, c.Labels[LabelUserID])
	b.mu.Unlock()
	return nil
}

type sliceStream struct {
	b      *fakeBridge
	userID string
	ctx    context.Context
	events []bridge.Event
	block  chan struct{}
	i      int
}

func (s *sliceStream) Next() (bridge.Event, error) {
	if s.i >= len(s.events) {
		return bridge.Event{}, io.EOF
	}
	if s.block != nil && s.i == len(s.events)-1 {
		select {
		case <-s.block:
		case <-s.ctx.Done():
			return bridge.Event{}, s.ctx.Err()
		}
	}
	ev := s.events[s.i]
	s.i++
	if s.b.onEvent != nil {
		s.b.onEvent(s.userID, ev)
	}
	return ev, nil
}

func (s *sliceStream) Close() error { return nil }

func (b *fakeBridge) Deliver(ctx context.Context, addr, secret string, req bridge.DeliverRequest) (bridge.EventStream, error) {
	c, ok := b.containerFor(addr)
	if !ok {
		return nil, errors.New("connection refused")
	}
	if err := b.checkSecret(c, secret); err != nil {
		return nil, err
	}
	uid := c.Labels[LabelUserID]
	b.mu.Lock()
	b.delivered = append(b.delivered, req)
	script, block := b.script, b.block
	b.mu.Unlock()
	events := []bridge.Event{{Type: "accepted"}, {Type: "working", Message: "thinking"}, {Type: "turn_done"}}
	if script != nil {
		events = script(uid, req)
	}
	return &sliceStream{b: b, userID: uid, ctx: ctx, events: events, block: block}, nil
}

func (b *fakeBridge) setUnhealthy(uid string, v bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.unhealthy[uid] = v
}

func (b *fakeBridge) shutdownCount() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return len(b.shutdowns)
}

type testEnv struct {
	m   *Manager
	st  *store.Memory
	eng *docker.Fake
	br  *fakeBridge
	eg  *egress.Writer
	clk *clock.Fake
	cfg *config.Config
	dir string
}

func testConfig(t *testing.T) *config.Config {
	t.Helper()
	cfg, err := config.Load(func(k string) string {
		return map[string]string{
			"VM_INTERNAL_SECRET":  strings.Repeat("x", 32),
			"DATABASE_URL":        "postgres://unused",
			"AGENT_IMAGE":         "agent:v1",
			"AGENT_IMAGE_VERSION": "v1",
			"AWAKE_CAP":           "5",
			"WAKE_TIMEOUT":        "300ms",
			"SHUTDOWN_GRACE":      "200ms",
		}[k]
	})
	if err != nil {
		t.Fatal(err)
	}
	cfg.EgressAllowlistDir = t.TempDir()
	cfg.SnapshotRoot = t.TempDir()
	return cfg
}

func newEnv(t *testing.T, mutate ...func(*config.Config)) *testEnv {
	t.Helper()
	cfg := testConfig(t)
	for _, fn := range mutate {
		fn(cfg)
	}
	eng := docker.NewFake()
	br := newFakeBridge(eng)
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	eg := egress.NewWriter(cfg.EgressAllowlistDir, cfg.EgressProxyContainer, eng, 0, log)
	if err := eg.Init(); err != nil {
		t.Fatal(err)
	}
	st := store.NewMemory()
	clk := clock.NewFake(t0)
	m := New(cfg, st, eng, br, eg, clk, log)
	m.PollInterval = 5 * time.Millisecond
	return &testEnv{m: m, st: st, eng: eng, br: br, eg: eg, clk: clk, cfg: cfg, dir: cfg.EgressAllowlistDir}
}

// restart builds a fresh Manager over the same store / Docker (vm-manager restart).
func (e *testEnv) restart() {
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	e.m = New(e.cfg, e.st, e.eng, e.br, e.eg, e.clk, log)
	e.m.PollInterval = 5 * time.Millisecond
}

func (e *testEnv) create(t *testing.T, userID string, opts ...func(*CreateRequest)) (store.Sandbox, string) {
	t.Helper()
	req := CreateRequest{UserID: userID, LiteLLMKey: "sk-litellm-" + userID, Priority: PriorityNormal,
		Allowlist: []string{"mcp.example.com"}, Timezone: "UTC"}
	for _, o := range opts {
		o(&req)
	}
	sb, hash, err := e.m.Create(context.Background(), req)
	if err != nil {
		t.Fatalf("create %s: %v", userID, err)
	}
	return sb, hash
}

func (e *testEnv) wake(t *testing.T, userID string) store.Sandbox {
	t.Helper()
	sb, _, err := e.m.Wake(context.Background(), userID, 0)
	if err != nil {
		t.Fatalf("wake %s: %v", userID, err)
	}
	return sb
}

func (e *testEnv) state(t *testing.T, userID string) store.State {
	t.Helper()
	sb, err := e.st.Get(context.Background(), userID)
	if err != nil {
		t.Fatalf("get %s: %v", userID, err)
	}
	return sb.State
}

func collect(t *testing.T, m *Manager, req DeliverRequest) ([]Event, error) {
	t.Helper()
	var evs []Event
	err := m.Deliver(context.Background(), req, func(ev Event) error { evs = append(evs, ev); return nil })
	return evs, err
}

func types(evs []Event) string {
	s := make([]string, len(evs))
	for i, e := range evs {
		s[i] = e.Type
	}
	return strings.Join(s, ",")
}

func deliverReq(userID, id string) DeliverRequest {
	return DeliverRequest{UserID: userID, Bridge: bridge.DeliverRequest{DeliveryID: id, Kind: "message", SessionKey: "web", Text: "hello"}}
}
