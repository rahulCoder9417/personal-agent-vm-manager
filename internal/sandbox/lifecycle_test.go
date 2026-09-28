package sandbox

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/personal-agent/vm-manager/internal/bridge"
	"github.com/personal-agent/vm-manager/internal/ids"
	"github.com/personal-agent/vm-manager/internal/platformfile"
	"github.com/personal-agent/vm-manager/internal/store"
)

func TestCreateMakesStoppedSandboxWithHashedSecrets(t *testing.T) {
	e := newEnv(t)
	sb, hash := e.create(t, "u1")
	if sb.State != store.StateStopped || sb.ContainerID == "" || sb.ImageVersion != "v1" {
		t.Fatalf("sandbox: %+v", sb)
	}
	c, ok := e.eng.Get(sb.ContainerID)
	if !ok || c.Running {
		t.Fatal("container must exist and be stopped")
	}
	// Spec: gVisor, limits, read-only rootfs, sandbox network, /data volume, non-root.
	s := c.Spec
	if s.Runtime != "runsc" || !s.ReadonlyRootfs || s.MemoryBytes != 600<<20 || s.NanoCPUs != 1e9 || s.PidsLimit != 256 ||
		s.Network != "personal-agent_sandbox" || s.VolumeName != ids.VolumeName("u1") || s.VolumeTarget != "/data" ||
		s.User != "10001:10001" || s.Image != "agent:v1" {
		t.Fatalf("spec: %+v", s)
	}
	// The plaintext secrets are only in the volume; the DB has their hashes.
	raw, ok := e.eng.File(ids.VolumeName("u1"), ".platform/platform.json")
	if !ok {
		t.Fatal("platform.json not written")
	}
	f, err := platformfile.Read(context.Background(), e.eng, sb.ContainerID)
	if err != nil {
		t.Fatal(err)
	}
	if hash != ids.Hash(f.MCPToken) || sb.MCPTokenHash != hash || sb.BridgeSecretHash != ids.Hash(f.BridgeSecret) {
		t.Fatal("stored hashes must be sha256 hex of the tokens in platform.json")
	}
	if f.LiteLLMKey != "sk-litellm-u1" || !strings.Contains(string(raw), "sk-litellm-u1") {
		t.Fatal("litellm key must be in the secrets file")
	}
	// Env carries no secrets.
	for _, kv := range s.Env {
		for _, secret := range []string{f.MCPToken, f.BridgeSecret, f.LiteLLMKey, e.cfg.InternalSecret} {
			if strings.Contains(kv, secret) {
				t.Fatalf("secret leaked into env: %s", kv)
			}
		}
	}
	if !slices.Contains(s.Env, "HTTPS_PROXY="+e.cfg.EgressProxyURL) || !slices.Contains(s.Env, "USER_ID=u1") {
		t.Fatalf("env: %v", s.Env)
	}

	// Idempotent: same sandbox, same hash, no new container.
	sb2, hash2 := e.create(t, "u1")
	if sb2.ContainerID != sb.ContainerID || hash2 != hash {
		t.Fatal("second create must return the existing sandbox")
	}
}

func TestCreateValidation(t *testing.T) {
	e := newEnv(t)
	cases := []CreateRequest{
		{UserID: "../x", LiteLLMKey: "k"},
		{UserID: "u1"},
		{UserID: "u1", LiteLLMKey: "k", Allowlist: []string{"localhost"}},
		{UserID: "u1", LiteLLMKey: "k", MCPServers: []MCPServer{{ID: "a", URL: "ftp://x"}}},
		{UserID: "u1", LiteLLMKey: "k", MCPServers: []MCPServer{{ID: "a", URL: "https://x.com", AuthHeaderName: "Bad Header"}}},
	}
	for i, req := range cases {
		if _, _, err := e.m.Create(context.Background(), req); !errors.Is(err, ErrInvalid) {
			t.Errorf("case %d: want ErrInvalid, got %v", i, err)
		}
	}
}

// Done when: Create -> Wake -> Deliver("hello") streams events ending in turn_done.
func TestCreateWakeDeliverHello(t *testing.T) {
	e := newEnv(t)
	e.create(t, "u1")
	sb, wasStopped, err := e.m.Wake(context.Background(), "u1", 0)
	if err != nil || !wasStopped || sb.State != store.StateAwake {
		t.Fatalf("wake: %+v %v %v", sb, wasStopped, err)
	}
	c, _ := e.eng.Get(sb.ContainerID)
	if !c.Running {
		t.Fatal("container should run")
	}
	// Egress file written for the container's IP.
	body, err := os.ReadFile(filepath.Join(e.dir, "u1.conf"))
	if err != nil || !strings.Contains(string(body), c.IP+"/32") || !strings.Contains(string(body), "mcp.example.com") {
		t.Fatalf("egress file: %q %v", body, err)
	}
	// Wake again: already awake, not stopped.
	if _, was, _ := e.m.Wake(context.Background(), "u1", 0); was {
		t.Fatal("second wake must report was_stopped=false")
	}

	var midState store.State
	e.br.onEvent = func(uid string, ev bridge.Event) {
		if ev.Type == "working" {
			midState = e.state(t, uid)
		}
	}
	evs, err := collect(t, e.m, deliverReq("u1", "evt-1"))
	if err != nil {
		t.Fatal(err)
	}
	if types(evs) != "accepted,working,turn_done" {
		t.Fatalf("events: %s", types(evs))
	}
	if midState != store.StateBusy {
		t.Fatalf("state during the turn = %s, want busy", midState)
	}
	if e.state(t, "u1") != store.StateAwake || e.m.inflightCount("u1") != 0 {
		t.Fatal("after turn_done: awake and nothing in flight")
	}
	if len(e.br.delivered) != 1 || e.br.delivered[0].DeliveryID != "evt-1" || e.br.delivered[0].Text != "hello" {
		t.Fatalf("bridge got: %+v", e.br.delivered)
	}
}

func TestDeliverWakesStoppedSandbox(t *testing.T) {
	e := newEnv(t)
	e.create(t, "u1")
	evs, err := collect(t, e.m, deliverReq("u1", "evt-1"))
	if err != nil || !strings.HasSuffix(types(evs), "turn_done") {
		t.Fatalf("deliver: %s %v", types(evs), err)
	}
}

func TestDeliverValidationAndNotFound(t *testing.T) {
	e := newEnv(t)
	e.create(t, "u1")
	if _, err := collect(t, e.m, DeliverRequest{UserID: "u1", Bridge: bridge.DeliverRequest{Kind: "message"}}); !errors.Is(err, ErrInvalid) {
		t.Fatalf("missing delivery id: %v", err)
	}
	req := deliverReq("u1", "d")
	req.MetadataJSON = "{not json"
	if _, err := collect(t, e.m, req); !errors.Is(err, ErrInvalid) {
		t.Fatalf("bad metadata: %v", err)
	}
	if _, err := collect(t, e.m, deliverReq("nobody", "d")); !errors.Is(err, ErrNotFound) {
		t.Fatalf("unknown user: %v", err)
	}
}

func TestDeliverRelaysBridgeErrorAndBrokenStream(t *testing.T) {
	e := newEnv(t)
	e.create(t, "u1")
	e.br.script = func(string, bridge.DeliverRequest) []bridge.Event {
		return []bridge.Event{{Type: "accepted"}, {Type: "error", Message: "model refused", PlatformFault: false}}
	}
	evs, _ := collect(t, e.m, deliverReq("u1", "d1"))
	if types(evs) != "accepted,error" || evs[1].PlatformFault || evs[1].Message != "model refused" {
		t.Fatalf("events: %+v", evs)
	}
	if e.state(t, "u1") != store.StateAwake {
		t.Fatal("error ends the turn: awake")
	}
	// Stream closes without turn_done -> platform fault.
	e.br.script = func(string, bridge.DeliverRequest) []bridge.Event {
		return []bridge.Event{{Type: "accepted"}, {Type: "working"}}
	}
	evs, _ = collect(t, e.m, deliverReq("u1", "d2"))
	if types(evs) != "accepted,working,error" || !evs[2].PlatformFault {
		t.Fatalf("events: %+v", evs)
	}
}

// Stuck turn: timeout -> force stop + ERROR with platform_fault.
func TestDeliverTimeoutForceStops(t *testing.T) {
	e := newEnv(t)
	sb, _ := e.create(t, "u1")
	e.br.block = make(chan struct{}) // never released
	req := deliverReq("u1", "slow")
	req.Timeout = 50 * time.Millisecond
	evs, err := collect(t, e.m, req)
	if err != nil {
		t.Fatal(err)
	}
	last := evs[len(evs)-1]
	if last.Type != "error" || !last.PlatformFault || !strings.Contains(last.Message, "timed out") {
		t.Fatalf("events: %+v", evs)
	}
	if e.state(t, "u1") != store.StateStopped {
		t.Fatalf("state = %s, want stopped", e.state(t, "u1"))
	}
	c, _ := e.eng.Get(sb.ContainerID)
	if c.Running || !slices.Contains(e.eng.Stops, ids.ContainerName("u1")+":0s") {
		t.Fatalf("container must be force-stopped (timeout 0): stops=%v", e.eng.Stops)
	}
	if e.br.shutdownCount() != 0 {
		t.Fatal("a forced stop must not wait for a polite shutdown")
	}
}

// The turn is tracked to the end even when the caller disappears.
func TestDeliverContinuesWhenCallerGoesAway(t *testing.T) {
	e := newEnv(t)
	e.create(t, "u1")
	e.br.block = make(chan struct{})
	gone := errors.New("client gone")
	err := e.m.Deliver(context.Background(), deliverReq("u1", "d1"), func(ev Event) error { return gone })
	if !errors.Is(err, gone) {
		t.Fatalf("want the send error back, got %v", err)
	}
	if e.m.inflightCount("u1") != 1 {
		t.Fatal("the turn must still be tracked")
	}
	close(e.br.block)
	waitFor(t, func() bool { return e.m.inflightCount("u1") == 0 && e.state(t, "u1") == store.StateAwake })
}

func TestStopRules(t *testing.T) {
	e := newEnv(t)
	e.create(t, "u1")
	e.wake(t, "u1")
	e.br.block = make(chan struct{})
	done := make(chan struct{})
	go func() { _, _ = collect(t, e.m, deliverReq("u1", "d1")); close(done) }()
	waitFor(t, func() bool { return e.state(t, "u1") == store.StateBusy })
	if _, err := e.m.Stop(context.Background(), "u1", false, "test"); !errors.Is(err, ErrBusy) {
		t.Fatalf("polite stop mid-turn must be refused, got %v", err)
	}
	close(e.br.block)
	<-done
	sb, err := e.m.Stop(context.Background(), "u1", false, "test")
	if err != nil || sb.State != store.StateStopped {
		t.Fatalf("stop: %+v %v", sb, err)
	}
	if e.br.shutdownCount() != 1 {
		t.Fatal("polite stop must call /shutdown first")
	}
	if _, err := os.Stat(filepath.Join(e.dir, "u1.conf")); !os.IsNotExist(err) {
		t.Fatal("egress file must be removed on stop (the IP can be reused)")
	}
	// Stopping a stopped sandbox is fine.
	if _, err := e.m.Stop(context.Background(), "u1", true, "again"); err != nil {
		t.Fatal(err)
	}
}

func TestDeleteRemovesContainerVolumeAndEgress(t *testing.T) {
	e := newEnv(t)
	sb, _ := e.create(t, "u1")
	e.wake(t, "u1")
	e.clk.Advance(time.Minute)
	if err := e.m.Delete(context.Background(), "u1"); err != nil {
		t.Fatal(err)
	}
	if _, ok := e.eng.Get(sb.ContainerID); ok {
		t.Fatal("container must be removed")
	}
	if _, ok := e.eng.Volume(ids.VolumeName("u1")); ok {
		t.Fatal("volume must be removed")
	}
	if _, err := os.Stat(filepath.Join(e.dir, "u1.conf")); !os.IsNotExist(err) {
		t.Fatal("egress file must be removed")
	}
	if _, err := e.st.Get(context.Background(), "u1"); !errors.Is(err, store.ErrNotFound) {
		t.Fatal("row must be gone")
	}
	usage, _ := e.m.Usage(context.Background(), "u1", t0, t0.Add(time.Hour))
	if len(usage) != 1 || usage[0].AwakeSeconds != 60 {
		t.Fatalf("awake time must still be billable after delete: %+v", usage)
	}
	if err := e.m.Delete(context.Background(), "u1"); err != nil {
		t.Fatalf("delete must be idempotent: %v", err)
	}
}

// New image: a stopped container is re-created on its next wake, /data and token kept.
func TestImageRolloutRecreatesOnWake(t *testing.T) {
	e := newEnv(t)
	sb, hash := e.create(t, "u1")
	e.cfg.AgentImage, e.cfg.AgentImageVersion = "agent:v2", "v2"
	woke := e.wake(t, "u1")
	if woke.ContainerID == sb.ContainerID || woke.ImageVersion != "v2" {
		t.Fatalf("container must be re-created: %+v", woke)
	}
	if _, ok := e.eng.Get(sb.ContainerID); ok {
		t.Fatal("old container must be removed")
	}
	c, _ := e.eng.Get(woke.ContainerID)
	if c.Spec.Image != "agent:v2" || c.Spec.VolumeName != ids.VolumeName("u1") || c.Spec.Labels[LabelImageVersion] != "v2" {
		t.Fatalf("new spec: %+v", c.Spec)
	}
	f, err := platformfile.Read(context.Background(), e.eng, woke.ContainerID)
	if err != nil || ids.Hash(f.MCPToken) != hash {
		t.Fatal("the Platform MCP token must survive re-creation")
	}
}

func TestWakeRetriesThenRecreatesThenBroken(t *testing.T) {
	e := newEnv(t)
	e.create(t, "u1")
	e.eng.StartErrors[ids.ContainerName("u1")] = 1 // first start fails, retry works
	e.wake(t, "u1")
	_, _ = e.m.Stop(context.Background(), "u1", true, "t")

	e.eng.StartErrors[ids.ContainerName("u1")] = 2 // both tries fail -> re-create -> works
	sb := e.wake(t, "u1")
	if sb.State != store.StateAwake {
		t.Fatal("re-create should have fixed it")
	}
	_, _ = e.m.Stop(context.Background(), "u1", true, "t")

	e.eng.StartErrors[ids.ContainerName("u1")] = 100
	if _, _, err := e.m.Wake(context.Background(), "u1", 0); !errors.Is(err, ErrUnavailable) {
		t.Fatalf("want ErrUnavailable, got %v", err)
	}
	if e.state(t, "u1") != store.StateBroken {
		t.Fatal("must be marked broken")
	}
	// A later wake tries again (and succeeds once Docker behaves).
	e.eng.StartErrors[ids.ContainerName("u1")] = 0
	if sb := e.wake(t, "u1"); sb.State != store.StateAwake {
		t.Fatal("broken sandbox should be wakeable again")
	}
}

func TestWakeUnhealthyBridgeMarksBroken(t *testing.T) {
	e := newEnv(t)
	e.create(t, "u1")
	e.br.setUnhealthy("u1", true)
	if _, _, err := e.m.Wake(context.Background(), "u1", 0); !errors.Is(err, ErrUnavailable) {
		t.Fatalf("want ErrUnavailable, got %v", err)
	}
	if e.state(t, "u1") != store.StateBroken || e.eng.RunningCount() != 0 {
		t.Fatal("unhealthy wake: broken and nothing left running")
	}
	usage, _ := e.m.Usage(context.Background(), "u1", t0.Add(-time.Hour), t0.Add(time.Hour))
	if len(usage) != 1 {
		t.Fatal("the failed start is still an (empty) awake period")
	}
	e.clk.Advance(time.Hour)
	usage, _ = e.m.Usage(context.Background(), "u1", t0.Add(-time.Hour), t0.Add(2*time.Hour))
	if usage[0].AwakeSeconds != 0 {
		t.Fatalf("no open period may be left behind: %+v", usage)
	}
}

// After a vm-manager restart the Bridge secret is loaded back from /data.
func TestManagerRestartReloadsSecretFromVolume(t *testing.T) {
	e := newEnv(t)
	e.create(t, "u1")
	e.wake(t, "u1")
	e.restart()
	evs, err := collect(t, e.m, deliverReq("u1", "d1"))
	if err != nil || types(evs) != "accepted,working,turn_done" {
		t.Fatalf("deliver after restart: %s %v", types(evs), err)
	}
}

func TestSetEgressAllowlist(t *testing.T) {
	e := newEnv(t)
	sb, _ := e.create(t, "u1")
	servers := []MCPServer{{ID: "m1", Name: "notes", URL: "https://notes.example.org/mcp", AuthHeaderName: "Authorization", AuthSecret: "Bearer abc"}}
	// Stopped: DB + secrets file updated, no egress file yet.
	if err := e.m.SetEgressAllowlist(context.Background(), "u1", []string{"notes.example.org"}, servers); err != nil {
		t.Fatal(err)
	}
	got, _ := e.st.Get(context.Background(), "u1")
	if !slices.Equal(got.Allowlist, []string{"notes.example.org"}) {
		t.Fatalf("allowlist: %v", got.Allowlist)
	}
	f, _ := platformfile.Read(context.Background(), e.eng, sb.ContainerID)
	if len(f.MCPServers) != 1 || f.MCPServers[0].AuthSecret != "Bearer abc" {
		t.Fatalf("mcp servers: %+v", f.MCPServers)
	}
	if _, err := os.Stat(filepath.Join(e.dir, "u1.conf")); !os.IsNotExist(err) {
		t.Fatal("no egress file while stopped")
	}
	// Awake: applied right away.
	e.wake(t, "u1")
	if err := e.m.SetEgressAllowlist(context.Background(), "u1", []string{"*.other.io"}, servers); err != nil {
		t.Fatal(err)
	}
	body, _ := os.ReadFile(filepath.Join(e.dir, "u1.conf"))
	if !strings.Contains(string(body), "dstdomain .other.io") || strings.Contains(string(body), "notes.example.org") {
		t.Fatalf("egress file: %s", body)
	}
	if err := e.m.SetEgressAllowlist(context.Background(), "u1", []string{"evil\nhttp_access allow all"}, nil); !errors.Is(err, ErrInvalid) {
		t.Fatalf("config injection must be rejected: %v", err)
	}
}

func TestUsageFromAwakePeriods(t *testing.T) {
	e := newEnv(t)
	e.create(t, "u1")
	e.wake(t, "u1")
	e.clk.Advance(5 * time.Minute)
	_, _ = e.m.Stop(context.Background(), "u1", false, "t")
	e.clk.Advance(10 * time.Minute)
	e.wake(t, "u1")
	e.clk.Advance(2 * time.Minute) // still open
	usage, err := e.m.Usage(context.Background(), "", t0, time.Time{})
	if err != nil {
		t.Fatal(err)
	}
	if len(usage) != 1 || usage[0].AwakeSeconds != 7*60 {
		t.Fatalf("usage: %+v", usage)
	}
	if _, err := e.m.Usage(context.Background(), "", t0.Add(time.Hour), t0); !errors.Is(err, ErrInvalid) {
		t.Fatal("since >= until must be rejected")
	}
}

func TestSnapshotVolume(t *testing.T) {
	e := newEnv(t)
	sb, _ := e.create(t, "u1")
	loc, size, err := e.m.Snapshot(context.Background(), "u1", filepath.Join(e.cfg.SnapshotRoot, "nightly"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(loc, filepath.Join(e.cfg.SnapshotRoot, "nightly")) || !strings.HasSuffix(loc, ".tar.gz") || size <= 0 {
		t.Fatalf("snapshot: %s %d", loc, size)
	}
	st, _ := os.Stat(loc)
	if st.Size() != size || st.Mode().Perm() != 0o600 {
		t.Fatalf("file: %v", st)
	}
	if _, _, err := e.m.Snapshot(context.Background(), "u1", "/etc"); !errors.Is(err, ErrInvalid) {
		t.Fatalf("destination outside root must be rejected: %v", err)
	}
	if _, _, err := e.m.Snapshot(context.Background(), "u1", "../../x"); !errors.Is(err, ErrInvalid) {
		t.Fatalf("relative escape must be rejected: %v", err)
	}
	_ = sb
}

func TestGetAndListIncludeMemory(t *testing.T) {
	e := newEnv(t)
	e.create(t, "u1")
	e.create(t, "u2")
	e.wake(t, "u1")
	info, err := e.m.Get(context.Background(), "u1")
	if err != nil || info.MemoryBytes != e.eng.Memory {
		t.Fatalf("get: %+v %v", info, err)
	}
	list, _ := e.m.List(context.Background(), store.StateStopped)
	if len(list) != 1 || list[0].UserID != "u2" || list[0].MemoryBytes != 0 {
		t.Fatalf("list stopped: %+v", list)
	}
	if _, err := e.m.Get(context.Background(), "nope"); !errors.Is(err, ErrNotFound) {
		t.Fatal("want ErrNotFound")
	}
	if err := e.m.Touch(context.Background(), "nope"); !errors.Is(err, ErrNotFound) {
		t.Fatal("touch unknown: want ErrNotFound")
	}
}

func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(2 * time.Millisecond)
	}
	t.Fatal("condition not met in time")
}
