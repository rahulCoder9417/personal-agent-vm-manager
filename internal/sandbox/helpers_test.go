package sandbox

import (
	"context"
	"io"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/personal-agent/vm-manager/internal/bridge"
	"github.com/personal-agent/vm-manager/internal/clock"
	"github.com/personal-agent/vm-manager/internal/config"
	"github.com/personal-agent/vm-manager/internal/docker"
	"github.com/personal-agent/vm-manager/internal/egress"
	"github.com/personal-agent/vm-manager/internal/sandbox/sandboxtest"
	"github.com/personal-agent/vm-manager/internal/store"
)

var t0 = time.Date(2026, 9, 1, 12, 0, 0, 0, time.UTC)

type testEnv struct {
	m   *Manager
	st  *store.Memory
	eng *docker.Fake
	br  *sandboxtest.FakeBridge
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
	br := sandboxtest.NewFakeBridge(eng)
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	eg := egress.NewWriter(cfg.EgressAllowlistDir, cfg.EgressProxyContainer, eng, 0, log)
	if err := eg.Init(); err != nil {
		t.Fatal(err)
	}
	st := store.NewMemory()
	clk := clock.NewFake(t0)
	m := New(cfg, st, eng, br, eg, clk, log)
	m.PollInterval = 5 * time.Millisecond
	m.diskUsedPercent = func(string) (float64, error) { return 40, nil } // never the laptop's real disk
	return &testEnv{m: m, st: st, eng: eng, br: br, eg: eg, clk: clk, cfg: cfg, dir: cfg.EgressAllowlistDir}
}

// restart builds a fresh Manager over the same store / Docker (vm-manager restart).
func (e *testEnv) restart() {
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	e.m = New(e.cfg, e.st, e.eng, e.br, e.eg, e.clk, log)
	e.m.PollInterval = 5 * time.Millisecond
	e.m.diskUsedPercent = func(string) (float64, error) { return 40, nil }
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
