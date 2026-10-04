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

// execIn runs a command in a running container and returns its exit code and output.
func execIn(t *testing.T, api *client.Client, containerID string, args ...string) (int, string) {
	t.Helper()
	ctx := context.Background()
	ex, err := api.ContainerExecCreate(ctx, containerID, container.ExecOptions{Cmd: args, AttachStdout: true, AttachStderr: true})
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
	return insp.ExitCode, out.String()
}

// probe starts a curl container with the sandbox runtime, network and restrictions, writes the
// egress rule for it (as user `name`) and returns a function that runs a command in it.
func probe(t *testing.T, e *env, name string, rule egress.Rule) func(args ...string) int {
	t.Helper()
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
	log := slog.New(slog.NewTextHandler(os.Stderr, nil))
	eg := egress.NewWriter(e.cfg.EgressAllowlistDir, e.cfg.EgressProxyContainer, e.eng, 0, log)
	if err := eg.Write(name, info.IPs[e.cfg.SandboxNetwork], rule); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = eg.Remove(name); _ = eg.ReloadNow(context.Background()) })
	if err := eg.ReloadNow(ctx); err != nil {
		t.Fatal(err)
	}
	time.Sleep(3 * time.Second) // squid reconfigure
	return func(args ...string) int {
		code, _ := execIn(t, api, c.ID, args...)
		return code
	}
}

// Done when: a container can't reach anything except the egress proxy's allowlisted domains.
// A probe container with the sandbox runtime/network gets an allowlist for one domain.
func TestEgressOnlyThroughAllowlist(t *testing.T) {
	e := setup(t, nil)
	run := probe(t, e, "it-probe", egress.Rule{Domains: []string{"example.com"}})
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

// Scout's browser, the network rule: a container with the open web reaches any public web address
// on ports 80 and 443 through the proxy, and nothing internal; another container keeps the strict
// rule. (docs/build-report.md section 9 lists what a real Squid 7.7 answered for the same rule.)
func TestBrowserEgressOpenWeb(t *testing.T) {
	e := setup(t, nil)
	open := probe(t, e, "it-open", egress.Rule{OpenWeb: true})
	strict := probe(t, e, "it-strict", egress.Rule{Domains: []string{"example.com"}})
	proxy := e.cfg.EgressProxyURL
	curl := func(run func(...string) int, url string) bool {
		return run("curl", "-sS", "-m", "15", "-f", "-x", proxy, "-o", "/dev/null", url) == 0
	}
	if open("curl", "-sS", "-m", "10", "-o", "/dev/null", "https://example.org") == 0 {
		t.Fatal("direct internet access must still fail: the proxy is the only way out")
	}
	for _, url := range []string{"https://example.org", "http://example.org", "https://example.com"} {
		if !curl(open, url) {
			t.Errorf("the open web must reach %s", url)
		}
	}
	for _, url := range []string{
		"http://169.254.169.254/latest/meta-data/", // cloud metadata
		"http://127.0.0.1/", "http://localhost/",   // the proxy's own loopback
		"http://10.0.0.1/", "http://192.168.0.1/", "http://172.30.0.2/", // private ranges, the proxy itself
		"http://postgres/", "http://redis/", "http://vm-manager/", "http://egress-proxy/", // service names
		"http://api:8080/", "http://api:8080/admin", // the api outside /mcp and /git/
		"http://localtest.me/",             // a public name that points at 127.0.0.1
		"https://example.org:8443/",        // not a web port
		"http://example.org:8080/",         // not a web port
		"http://metadata.google.internal/", // a private-use name
	} {
		if curl(open, url) {
			t.Errorf("%s must be refused even with the open web", url)
		}
	}
	// What every container has still works for it: the Platform MCP path is not refused by the proxy
	// (any HTTP answer from the api counts; 403 from the proxy does not).
	if open("sh", "-c", "curl -sS -m 15 -x "+proxy+" -o /dev/null -w '%{http_code}' "+e.cfg.PlatformMCPURL+" | grep -qv '^403$'") != 0 {
		t.Error("the Platform MCP path must stay reachable for a container with the open web")
	}
	// The other container keeps today's strict rule.
	if !curl(strict, "https://example.com") || curl(strict, "https://example.org") {
		t.Error("a container without the browser must reach its own domains only")
	}
}

// Scout's browser, in a real container: a sandbox created with the browser gets the browser's
// memory limit, and the browser's self-check passes inside it: Chromium under gVisor on the
// read-only root filesystem as uid 10001, through the egress proxy with the rule the manager
// wrote for this container, measured by the Bridge's meter.
func TestBrowserInContainer(t *testing.T) {
	e := setup(t, nil)
	ctx := context.Background()
	uid := fmt.Sprintf("it-browser-%d", time.Now().Unix())
	key := os.Getenv("IT_LITELLM_KEY")
	if key == "" {
		t.Fatal("IT_LITELLM_KEY is required")
	}
	_ = e.m.Delete(ctx, uid)
	if _, _, err := e.m.Create(ctx, sandbox.CreateRequest{UserID: uid, LiteLLMKey: key, Priority: sandbox.PriorityHigh,
		Timezone: "UTC", BrowserEnabled: true}); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = e.m.Delete(context.Background(), uid) })
	sb, _, err := e.m.Wake(ctx, uid, 0)
	if err != nil {
		t.Fatalf("wake: %v", err)
	}
	if !sb.BrowserEnabled {
		t.Fatal("the sandbox must report the browser as turned on")
	}
	api, err := client.NewClientWithOpts(client.FromEnv, client.WithAPIVersionNegotiation())
	if err != nil {
		t.Fatal(err)
	}
	info, err := api.ContainerInspect(ctx, sb.ContainerID)
	if err != nil {
		t.Fatal(err)
	}
	if info.HostConfig.Memory != e.cfg.BrowserMemoryBytes || info.Config.Labels[sandbox.LabelBrowser] != "true" {
		t.Fatalf("browser container: memory %d (want %d), labels %v", info.HostConfig.Memory, e.cfg.BrowserMemoryBytes, info.Config.Labels)
	}
	time.Sleep(3 * time.Second) // squid reconfigure after the wake wrote the rule
	code, out := execIn(t, api, sb.ContainerID, "python", "-m", "bridge.browser_check")
	if code != 0 || !strings.Contains(out, "all checks passed") {
		t.Fatalf("the browser self-check failed in the container (exit %d):\n%s", code, out)
	}
	mem, err := e.eng.MemoryUsage(ctx, sb.ContainerID)
	if err == nil {
		t.Logf("MEASURED RAM of a browser container after the self-check: %.0f MiB (limit %.0f MiB) -- record it in README",
			float64(mem)/(1<<20), float64(e.cfg.BrowserMemoryBytes)/(1<<20))
	}

	// Turned off: the next wake re-creates the container with the ordinary limit and no browser.
	if _, err := e.m.SetBrowser(ctx, uid, false); err != nil {
		t.Fatal(err)
	}
	if _, err := e.m.Stop(ctx, uid, false, "it"); err != nil {
		t.Fatal(err)
	}
	off, _, err := e.m.Wake(ctx, uid, 0)
	if err != nil {
		t.Fatalf("wake after turning the browser off: %v", err)
	}
	info, err = api.ContainerInspect(ctx, off.ContainerID)
	if err != nil {
		t.Fatal(err)
	}
	if off.ContainerID == sb.ContainerID || info.HostConfig.Memory != e.cfg.MemoryBytes {
		t.Fatalf("after turning the browser off: container %s (was %s), memory %d", off.ContainerID, sb.ContainerID, info.HostConfig.Memory)
	}
	time.Sleep(3 * time.Second)
	if code, _ := execIn(t, api, off.ContainerID, "curl", "-sS", "-m", "15", "-f", "-x", e.cfg.EgressProxyURL, "-o", "/dev/null", "https://example.org"); code == 0 {
		t.Fatal("without the browser the container must not reach the open web")
	}
}
