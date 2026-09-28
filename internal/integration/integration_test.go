//go:build integration

// Package integration runs the Phase 2 "done when" checks against real Docker
// with gVisor. It needs:
//   - Docker with the runsc runtime registered
//   - the backend's infra stack up (sandbox network, egress proxy, LiteLLM, api /mcp)
//   - the agent image built: docker build -t personal-agent/agent:dev agent-image/
//   - Postgres: VM_TEST_DATABASE_URL (or DATABASE_URL)
//   - the same env vars as the service (SANDBOX_NETWORK, EGRESS_PROXY_URL,
//     EGRESS_ALLOWLIST_DIR, LITELLM_BASE_URL, PLATFORM_MCP_URL, AGENT_IMAGE, ...)
//   - IT_LITELLM_KEY: a LiteLLM virtual key the test user's agent may use
//
// Run: go test -tags=integration -v -timeout 30m ./internal/integration/
package integration

import (
	"bytes"
	"context"
	"fmt"
	"log/slog"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/network"
	"github.com/docker/docker/client"
	"github.com/docker/docker/pkg/stdcopy"

	"github.com/personal-agent/vm-manager/internal/bridge"
	"github.com/personal-agent/vm-manager/internal/clock"
	"github.com/personal-agent/vm-manager/internal/config"
	"github.com/personal-agent/vm-manager/internal/docker"
	"github.com/personal-agent/vm-manager/internal/egress"
	"github.com/personal-agent/vm-manager/internal/sandbox"
	"github.com/personal-agent/vm-manager/internal/store"
	"github.com/personal-agent/vm-manager/migrations"
)

type env struct {
	cfg *config.Config
	m   *sandbox.Manager
	eng *docker.Client
	st  store.Store
}

func setup(t *testing.T, mutate func(*config.Config)) *env {
	t.Helper()
	getenv := func(k string) string {
		if k == "DATABASE_URL" && os.Getenv("VM_TEST_DATABASE_URL") != "" {
			return os.Getenv("VM_TEST_DATABASE_URL")
		}
		if k == "VM_INTERNAL_SECRET" && os.Getenv(k) == "" {
			return strings.Repeat("i", 32)
		}
		return os.Getenv(k)
	}
	cfg, err := config.Load(getenv)
	if err != nil {
		t.Fatal(err)
	}
	if mutate != nil {
		mutate(cfg)
	}
	ctx := context.Background()
	st, err := store.OpenPostgres(ctx, cfg.DatabaseURL, migrations.FS)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(st.Close)
	eng, err := docker.NewClient(ctx)
	if err != nil {
		t.Fatal(err)
	}
	log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelDebug}))
	eg := egress.NewWriter(cfg.EgressAllowlistDir, cfg.EgressProxyContainer, eng, 0, log)
	if err := eg.Init(); err != nil {
		t.Fatal(err)
	}
	rctx, cancel := context.WithCancel(ctx)
	t.Cleanup(cancel)
	go eg.Run(rctx)
	m := sandbox.New(cfg, st, eng, bridge.NewClient(), eg, clock.Real{}, log)
	return &env{cfg: cfg, m: m, eng: eng, st: st}
}

func (e *env) create(t *testing.T, userID string, allow []string) {
	t.Helper()
	key := os.Getenv("IT_LITELLM_KEY")
	if key == "" {
		t.Fatal("IT_LITELLM_KEY is required")
	}
	_ = e.m.Delete(context.Background(), userID)
	if _, _, err := e.m.Create(context.Background(), sandbox.CreateRequest{UserID: userID, LiteLLMKey: key,
		Priority: sandbox.PriorityNormal, Allowlist: allow, Timezone: "UTC"}); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = e.m.Delete(context.Background(), userID) })
}

func deliver(t *testing.T, m *sandbox.Manager, userID, id, text string) []sandbox.Event {
	t.Helper()
	var evs []sandbox.Event
	err := m.Deliver(context.Background(), sandbox.DeliverRequest{UserID: userID,
		Bridge: bridge.DeliverRequest{DeliveryID: id, Kind: "message", SessionKey: "web", Channel: "web", Text: text}},
		func(ev sandbox.Event) error { evs = append(evs, ev); t.Logf("event: %+v", ev); return nil })
	if err != nil {
		t.Fatal(err)
	}
	return evs
}

// Done when: Create -> Wake -> Deliver("hello") streams turn_done. Also records RAM.
func TestCreateWakeDeliverHello(t *testing.T) {
	e := setup(t, nil)
	uid := fmt.Sprintf("it-hello-%d", time.Now().Unix())
	e.create(t, uid, nil)
	sb, was, err := e.m.Wake(context.Background(), uid, 0)
	if err != nil || !was {
		t.Fatalf("wake: %v", err)
	}
	evs := deliver(t, e.m, uid, "it-1", "hello")
	if evs[len(evs)-1].Type != bridge.EventTurnDone {
		t.Fatalf("last event: %+v", evs[len(evs)-1])
	}
	mem, err := e.eng.MemoryUsage(context.Background(), sb.ContainerID)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("MEASURED RAM after one turn: %d bytes (%.0f MiB) -- record it in README", mem, float64(mem)/(1<<20))
}

// Done when: stopped after the idle timeout and wakes again with memory intact.
func TestIdleStopAndWakeKeepsMemory(t *testing.T) {
	e := setup(t, func(c *config.Config) { c.IdleTimeout = 20 * time.Second })
	uid := fmt.Sprintf("it-idle-%d", time.Now().Unix())
	e.create(t, uid, nil)
	deliver(t, e.m, uid, "it-1", "Please remember that my favourite colour is teal.")
	time.Sleep(25 * time.Second)
	e.m.ReapOnce(context.Background())
	sb, _ := e.st.Get(context.Background(), uid)
	if sb.State != store.StateStopped {
		t.Fatalf("state after idle: %s", sb.State)
	}
	if _, _, err := e.m.Wake(context.Background(), uid, 0); err != nil {
		t.Fatal(err)
	}
	// Memory lives in /data: the volume still has Hermes's state dir.
	rc, err := e.eng.CopyFrom(context.Background(), sb.ContainerID, "/data/hermes")
	if err != nil {
		t.Fatalf("hermes home missing after wake: %v", err)
	}
	_ = rc.Close()
	deliver(t, e.m, uid, "it-2", "What is my favourite colour?") // check the reply in the channel / api logs
}

// Done when: the awake cap is respected.
func TestAwakeCap(t *testing.T) {
	e := setup(t, func(c *config.Config) { c.AwakeCap = 1 })
	a, b := fmt.Sprintf("it-cap-a-%d", time.Now().Unix()), fmt.Sprintf("it-cap-b-%d", time.Now().Unix())
	e.create(t, a, nil)
	e.create(t, b, nil)
	if _, _, err := e.m.Wake(context.Background(), a, 0); err != nil {
		t.Fatal(err)
	}
	if _, _, err := e.m.Wake(context.Background(), b, 0); err != nil {
		t.Fatal(err)
	}
	sa, _ := e.st.Get(context.Background(), a)
	if sa.State != store.StateStopped {
		t.Fatalf("a should have been evicted, is %s", sa.State)
	}
}

// Done when: a container can't reach anything except the egress proxy's allowlisted domains.
// A probe container with the sandbox runtime/network gets an allowlist for one domain.
func TestEgressOnlyThroughAllowlist(t *testing.T) {
	e := setup(t, nil)
	ctx := context.Background()
	api, err := client.NewClientWithOpts(client.FromEnv, client.WithAPIVersionNegotiation())
	if err != nil {
		t.Fatal(err)
	}
	cfg := &container.Config{Image: "curlimages/curl:8.10.1", Entrypoint: []string{"sleep"}, Cmd: []string{"600"}, User: "10001:10001"}
	host := &container.HostConfig{Runtime: e.cfg.Runtime, NetworkMode: container.NetworkMode(e.cfg.SandboxNetwork),
		ReadonlyRootfs: true, CapDrop: []string{"ALL"}, SecurityOpt: []string{"no-new-privileges:true"}}
	netCfg := &network.NetworkingConfig{EndpointsConfig: map[string]*network.EndpointSettings{e.cfg.SandboxNetwork: {}}}
	c, err := api.ContainerCreate(ctx, cfg, host, netCfg, nil, "")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = api.ContainerRemove(context.Background(), c.ID, container.RemoveOptions{Force: true}) })
	if err := api.ContainerStart(ctx, c.ID, container.StartOptions{}); err != nil {
		t.Fatal(err)
	}
	info, err := e.eng.InspectContainer(ctx, c.ID)
	if err != nil {
		t.Fatal(err)
	}
	ip := info.IPs[e.cfg.SandboxNetwork]

	log := slog.New(slog.NewTextHandler(os.Stderr, nil))
	eg := egress.NewWriter(e.cfg.EgressAllowlistDir, e.cfg.EgressProxyContainer, e.eng, 0, log)
	if err := eg.Write("it-probe", ip, []string{"example.com"}); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = eg.Remove("it-probe"); _ = eg.ReloadNow(context.Background()) })
	if err := eg.ReloadNow(ctx); err != nil {
		t.Fatal(err)
	}
	time.Sleep(3 * time.Second) // squid reconfigure

	run := func(args ...string) int {
		ex, err := api.ContainerExecCreate(ctx, c.ID, container.ExecOptions{Cmd: args, AttachStdout: true, AttachStderr: true})
		if err != nil {
			t.Fatal(err)
		}
		att, err := api.ContainerExecAttach(ctx, ex.ID, container.ExecStartOptions{})
		if err != nil {
			t.Fatal(err)
		}
		var out bytes.Buffer
		_, _ = stdcopy.StdCopy(&out, &out, att.Reader)
		att.Close()
		insp, err := api.ContainerExecInspect(ctx, ex.ID)
		if err != nil {
			t.Fatal(err)
		}
		t.Logf("%v -> exit %d: %s", args, insp.ExitCode, strings.TrimSpace(out.String()))
		return insp.ExitCode
	}
	proxy := e.cfg.EgressProxyURL
	if run("curl", "-sS", "-m", "10", "-o", "/dev/null", "https://example.com") == 0 {
		t.Fatal("direct internet access must fail")
	}
	if run("curl", "-sS", "-m", "15", "-f", "-x", proxy, "-o", "/dev/null", "https://example.com") != 0 {
		t.Fatal("allowlisted domain through the proxy must work")
	}
	if run("curl", "-sS", "-m", "15", "-f", "-x", proxy, "-o", "/dev/null", "https://example.org") == 0 {
		t.Fatal("non-allowlisted domain through the proxy must be denied")
	}
}
