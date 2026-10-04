package sandbox

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/personal-agent/vm-manager/internal/bridge"
	"github.com/personal-agent/vm-manager/internal/config"
	"github.com/personal-agent/vm-manager/internal/egress"
	"github.com/personal-agent/vm-manager/internal/ids"
	"github.com/personal-agent/vm-manager/internal/platformfile"
	"github.com/personal-agent/vm-manager/internal/store"
)

func withBrowser(r *CreateRequest) { r.BrowserEnabled = true }

func (e *testEnv) platformFile(t *testing.T, containerID string) platformfile.File {
	t.Helper()
	f, err := platformfile.Read(context.Background(), e.eng, containerID)
	if err != nil {
		t.Fatal(err)
	}
	return f
}

// openWeb reports whether a Squid file opens the public web (ports 80 and 443) for its container.
func openWeb(file string) bool {
	return strings.Contains(file, "_web port 80 443\n") && strings.Contains(file, "_int_ip dst ") &&
		strings.Contains(file, "_int_name dstdom_regex ")
}

// A sandbox created with the browser: more memory and processes, the label, the switch in
// platform.json and in the row. One without it keeps today's limits and has none of them.
func TestCreateWithBrowser(t *testing.T) {
	e := newEnv(t)
	with, _ := e.create(t, "max", withBrowser)
	without, _ := e.create(t, "pro")

	c, _ := e.eng.Get(with.ContainerID)
	if c.Spec.MemoryBytes != 2048<<20 || c.Spec.PidsLimit != 1024 || c.Spec.Labels[LabelBrowser] != "true" {
		t.Fatalf("browser container limits: mem=%d pids=%d labels=%v", c.Spec.MemoryBytes, c.Spec.PidsLimit, c.Spec.Labels)
	}
	// Everything else about the container is unchanged: gVisor, read-only rootfs, one CPU, non-root.
	if c.Spec.Runtime != "runsc" || !c.Spec.ReadonlyRootfs || c.Spec.NanoCPUs != 1e9 || c.Spec.User != "10001:10001" ||
		c.Spec.Network != "personal-agent_sandbox" {
		t.Fatalf("browser container spec: %+v", c.Spec)
	}
	if !with.BrowserEnabled || !e.platformFile(t, with.ContainerID).BrowserEnabled {
		t.Fatal("the switch must be in the row and in platform.json")
	}

	c2, _ := e.eng.Get(without.ContainerID)
	if _, labelled := c2.Spec.Labels[LabelBrowser]; labelled || c2.Spec.MemoryBytes != 600<<20 || c2.Spec.PidsLimit != 256 {
		t.Fatalf("a container without the browser keeps today's limits: mem=%d pids=%d labels=%v",
			c2.Spec.MemoryBytes, c2.Spec.PidsLimit, c2.Spec.Labels)
	}
	if without.BrowserEnabled || e.platformFile(t, without.ContainerID).BrowserEnabled {
		t.Fatal("the browser is off unless asked for")
	}
	raw, _ := e.eng.File(ids.VolumeName("pro"), ".platform/platform.json")
	if strings.Contains(string(raw), "browser_enabled") {
		t.Fatalf("platform.json of a sandbox without the browser: %s", raw)
	}

	// The setting decides the browser limits; the others keep theirs.
	e2 := newEnv(t, func(c *config.Config) { c.BrowserMemoryBytes = 3072 << 20; c.BrowserPidsLimit = 2048 })
	sb, _ := e2.create(t, "max", withBrowser)
	c3, _ := e2.eng.Get(sb.ContainerID)
	if c3.Spec.MemoryBytes != 3072<<20 || c3.Spec.PidsLimit != 2048 {
		t.Fatalf("SANDBOX_BROWSER_MEMORY_MB / SANDBOX_BROWSER_PIDS_LIMIT not applied: %+v", c3.Spec)
	}
}

// Only the container with the browser gets the open web; another user's container keeps the
// strict rule; the file goes away when the container stops.
func TestBrowserContainerGetsOpenWebAndOthersDoNot(t *testing.T) {
	e := newEnv(t)
	e.create(t, "max", withBrowser)
	e.create(t, "pro")
	e.wake(t, "max")
	e.wake(t, "pro")

	maxSb, _ := e.st.Get(context.Background(), "max")
	maxC, _ := e.eng.Get(maxSb.ContainerID)
	proSb, _ := e.st.Get(context.Background(), "pro")
	proC, _ := e.eng.Get(proSb.ContainerID)

	maxFile := e.egressFile(t, "max")
	want, err := egress.Render("max", maxC.IP, egress.Rule{Domains: []string{"mcp.example.com"}, OpenWeb: true})
	if err != nil || maxFile != want {
		t.Fatalf("egress file of the browser user:\n%s\nwant:\n%s", maxFile, want)
	}
	if !openWeb(maxFile) || !strings.Contains(maxFile, "src "+maxC.IP+"/32\n") || strings.Contains(maxFile, proC.IP+"/32") {
		t.Fatalf("the open web must be tied to the browser container's own address:\n%s", maxFile)
	}
	proFile := e.egressFile(t, "pro")
	want, _ = egress.Render("pro", proC.IP, egress.Rule{Domains: []string{"mcp.example.com"}})
	if proFile != want || openWeb(proFile) || strings.Contains(proFile, "port") || strings.Contains(proFile, "deny") {
		t.Fatalf("another user's container keeps the strict rule:\n%s", proFile)
	}

	// Every later rewrite keeps both as they are: allowlist change, runtime change, reconcile.
	ctx := context.Background()
	for _, uid := range []string{"max", "pro"} {
		if err := e.m.SetEgressAllowlist(ctx, uid, []string{"notes.example.org"}, nil); err != nil {
			t.Fatal(err)
		}
		if _, err := e.m.SetAgentRuntime(ctx, uid, store.RuntimeClaudeCode); err != nil {
			t.Fatal(err)
		}
	}
	e.m.ReconcileOnce(ctx)
	maxFile, proFile = e.egressFile(t, "max"), e.egressFile(t, "pro")
	if !openWeb(maxFile) || !strings.Contains(maxFile, "notes.example.org") || !strings.Contains(maxFile, "api.anthropic.com") {
		t.Fatalf("browser user after rewrites:\n%s", maxFile)
	}
	if openWeb(proFile) || !strings.Contains(proFile, "notes.example.org") || !strings.Contains(proFile, "api.anthropic.com") {
		t.Fatalf("strict user after rewrites:\n%s", proFile)
	}

	// Stopped: the rule is gone with the container's address.
	if _, err := e.m.Stop(ctx, "max", false, "t"); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(e.dir, "max.conf")); !os.IsNotExist(err) {
		t.Fatal("no egress file while stopped")
	}
}

// SetBrowser stores the switch; the container gets it on its next wake: re-created with the
// browser's limits (keeping /data), platform.json rewritten, the egress rule opened. Turning
// it off works the same way.
func TestSetBrowserTakesEffectOnNextWake(t *testing.T) {
	e := newEnv(t)
	ctx := context.Background()
	created, hash := e.create(t, "u1")

	sb, err := e.m.SetBrowser(ctx, "u1", true)
	if err != nil || !sb.BrowserEnabled {
		t.Fatalf("SetBrowser: %+v %v", sb, err)
	}
	// Nothing else happens yet: same container, old limits, platform.json untouched.
	if sb.ContainerID != created.ContainerID || sb.State != store.StateStopped {
		t.Fatalf("SetBrowser must not touch the container: %+v", sb)
	}
	if e.platformFile(t, sb.ContainerID).BrowserEnabled {
		t.Fatal("platform.json changes on the next wake, not before")
	}

	woke := e.wake(t, "u1")
	if woke.ContainerID == created.ContainerID {
		t.Fatal("the container must be re-created: its limits changed")
	}
	if _, ok := e.eng.Get(created.ContainerID); ok {
		t.Fatal("the old container must be removed")
	}
	c, _ := e.eng.Get(woke.ContainerID)
	if c.Spec.MemoryBytes != 2048<<20 || c.Spec.PidsLimit != 1024 || c.Spec.Labels[LabelBrowser] != "true" ||
		c.Spec.VolumeName != ids.VolumeName("u1") {
		t.Fatalf("re-created container: %+v", c.Spec)
	}
	f := e.platformFile(t, woke.ContainerID)
	if !f.BrowserEnabled || ids.Hash(f.MCPToken) != hash || f.LiteLLMKey != "sk-litellm-u1" {
		t.Fatalf("platform.json after the wake (the token must survive): %+v", f)
	}
	if !openWeb(e.egressFile(t, "u1")) {
		t.Fatalf("egress after the wake:\n%s", e.egressFile(t, "u1"))
	}

	// A second wake of the same sandbox re-creates nothing.
	if _, err := e.m.Stop(ctx, "u1", false, "t"); err != nil {
		t.Fatal(err)
	}
	if again := e.wake(t, "u1"); again.ContainerID != woke.ContainerID {
		t.Fatal("an unchanged switch must not re-create the container")
	}

	// Turned off while awake: stored at once, and the running container keeps what it was woken
	// with (the browser, the open web) until it stops.
	sb, err = e.m.SetBrowser(ctx, "u1", false)
	if err != nil || sb.BrowserEnabled || sb.ContainerID != woke.ContainerID || sb.State != store.StateAwake {
		t.Fatalf("SetBrowser(false) while awake: %+v %v", sb, err)
	}
	e.m.ReconcileOnce(ctx)
	if err := e.m.SetEgressAllowlist(ctx, "u1", []string{"notes.example.org"}, nil); err != nil {
		t.Fatal(err)
	}
	if !openWeb(e.egressFile(t, "u1")) || !e.platformFile(t, woke.ContainerID).BrowserEnabled {
		t.Fatal("a running container keeps the browser and its network until the next wake")
	}

	// Next wake: back to a container without the browser, the strict rule, the switch off in platform.json.
	if _, err := e.m.Stop(ctx, "u1", false, "t"); err != nil {
		t.Fatal(err)
	}
	off := e.wake(t, "u1")
	c, _ = e.eng.Get(off.ContainerID)
	if off.ContainerID == woke.ContainerID || c.Spec.MemoryBytes != 600<<20 || c.Spec.PidsLimit != 256 ||
		c.Spec.Labels[LabelBrowser] != "" {
		t.Fatalf("container after turning the browser off: %+v", c.Spec)
	}
	f = e.platformFile(t, off.ContainerID)
	if f.BrowserEnabled || ids.Hash(f.MCPToken) != hash {
		t.Fatalf("platform.json after turning the browser off: %+v", f)
	}
	if file := e.egressFile(t, "u1"); openWeb(file) || strings.Contains(file, "deny") {
		t.Fatalf("egress after turning the browser off:\n%s", file)
	}
}

// Turned on while awake: the running container gets neither the browser nor the open web,
// whatever rewrites its egress file, until it is woken again.
func TestSetBrowserOnAwakeSandboxWaitsForTheNextWake(t *testing.T) {
	e := newEnv(t)
	ctx := context.Background()
	e.create(t, "u1")
	woke := e.wake(t, "u1")

	sb, err := e.m.SetBrowser(ctx, "u1", true)
	if err != nil || !sb.BrowserEnabled || sb.ContainerID != woke.ContainerID {
		t.Fatalf("SetBrowser while awake: %+v %v", sb, err)
	}
	e.m.ReconcileOnce(ctx)
	if _, err := e.m.SetAgentRuntime(ctx, "u1", store.RuntimeClaudeCode); err != nil {
		t.Fatal(err)
	}
	if err := e.m.SetEgressAllowlist(ctx, "u1", []string{"notes.example.org"}, nil); err != nil {
		t.Fatal(err)
	}
	if file := e.egressFile(t, "u1"); openWeb(file) {
		t.Fatalf("the network must not open before the container has the browser:\n%s", file)
	}
	if e.platformFile(t, woke.ContainerID).BrowserEnabled {
		t.Fatal("platform.json of a running container must not change")
	}
	// A turn still works, in the same container.
	if evs, err := collect(t, e.m, deliverReq("u1", "d1")); err != nil || types(evs) != "accepted,working,turn_done" {
		t.Fatalf("deliver: %v %v", evs, err)
	}
	if cur, _ := e.st.Get(ctx, "u1"); cur.ContainerID != woke.ContainerID {
		t.Fatal("a delivery to an awake sandbox must not re-create it")
	}
	// A health-check restart keeps the container as it is.
	e.br.SetUnhealthy("u1", true)
	e.m.HealthOnce(ctx)
	e.br.SetUnhealthy("u1", false)
	e.m.HealthOnce(ctx)
	if file := e.egressFile(t, "u1"); openWeb(file) {
		t.Fatalf("after a restart of the same container:\n%s", file)
	}
}

func TestSetBrowserErrorsAndRepeat(t *testing.T) {
	e := newEnv(t)
	ctx := context.Background()
	if _, err := e.m.SetBrowser(ctx, "nobody", true); !errors.Is(err, ErrNotFound) {
		t.Fatalf("unknown user: %v", err)
	}
	if _, err := e.m.SetBrowser(ctx, "bad id!", true); !errors.Is(err, ErrInvalid) {
		t.Fatalf("bad user id: %v", err)
	}
	created, _ := e.create(t, "u1", withBrowser)
	// The same value again changes nothing.
	sb, err := e.m.SetBrowser(ctx, "u1", true)
	if err != nil || !sb.BrowserEnabled || !sb.UpdatedAt.Equal(created.UpdatedAt) {
		t.Fatalf("repeat: %+v %v", sb, err)
	}
	if woke := e.wake(t, "u1"); woke.ContainerID != created.ContainerID {
		t.Fatal("a sandbox created with the browser needs no re-creation")
	}
}

// An image rollout re-creates a browser container with the browser's limits.
func TestImageRolloutKeepsBrowserLimits(t *testing.T) {
	e := newEnv(t)
	sb, _ := e.create(t, "u1", withBrowser)
	e.cfg.AgentImage, e.cfg.AgentImageVersion = "agent:v2", "v2"
	woke := e.wake(t, "u1")
	c, _ := e.eng.Get(woke.ContainerID)
	if woke.ContainerID == sb.ContainerID || c.Spec.Image != "agent:v2" || c.Spec.MemoryBytes != 2048<<20 ||
		c.Spec.Labels[LabelBrowser] != "true" || !e.platformFile(t, woke.ContainerID).BrowserEnabled {
		t.Fatalf("after the rollout: %+v", c.Spec)
	}
	if !openWeb(e.egressFile(t, "u1")) {
		t.Fatal("the browser user keeps the open web after a rollout")
	}
}

// Browser time: reported by the Bridge with the end of a turn, stored per user, returned by
// Usage exactly once. Nothing is counted when the browser never started.
func TestDeliverRecordsBrowserSecondsExactlyOnce(t *testing.T) {
	e := newEnv(t)
	ctx := context.Background()
	e.create(t, "u1", withBrowser)
	e.create(t, "u2")

	last := map[string]bridge.Event{
		"d1": {Type: "turn_done", BrowserSeconds: 73, BrowserReportID: "d1#1"},
		// A turn that never opened the browser.
		"d2": {Type: "turn_done"},
		// A failed turn still reports the browser time it used (the backend decides what to refund).
		"d3": {Type: "error", Message: "boom", PlatformFault: true, BrowserSeconds: 20, BrowserReportID: "d3#1"},
		// A retry of that delivery runs again and reports its own time under a new id.
		"d3-retry": {Type: "turn_done", BrowserSeconds: 15, BrowserReportID: "d3#2"},
		// An older Bridge without report ids: the delivery id is the report id.
		"d4": {Type: "turn_done", BrowserSeconds: 5},
		// Not plausible values are refused or capped.
		"d5": {Type: "turn_done", BrowserSeconds: -30, BrowserReportID: "d5#1"},
		"d6": {Type: "turn_done", BrowserSeconds: 10 * MaxBrowserReportSeconds, BrowserReportID: "d6#1"},
	}
	e.br.Script = func(_ string, req bridge.DeliverRequest) []bridge.Event {
		return []bridge.Event{{Type: "accepted"}, {Type: "working", Message: "using browser_navigate"}, last[req.DeliveryID]}
	}
	usage := func(since, until time.Time) int64 {
		t.Helper()
		got, err := e.m.Usage(ctx, "u1", since, until)
		if err != nil {
			t.Fatal(err)
		}
		if len(got) == 0 {
			return 0
		}
		return got[0].BrowserSeconds
	}
	run := func(id string) []Event {
		t.Helper()
		evs, err := collect(t, e.m, deliverReq("u1", id))
		if err != nil {
			t.Fatal(err)
		}
		e.clk.Advance(time.Minute)
		return evs
	}

	start := e.clk.Now()
	run("d2")
	if got := usage(start, time.Time{}); got != 0 {
		t.Fatalf("nothing is counted when the browser never started: %d", got)
	}

	afterD2 := e.clk.Now()
	evs := run("d1")
	// The stream to the backend is unchanged: the seconds travel through GetUsage, not the events.
	if types(evs) != "accepted,working,turn_done" || evs[2].Message != "" {
		t.Fatalf("events: %+v", evs)
	}
	if got := usage(start, time.Time{}); got != 73 {
		t.Fatalf("after d1: %d", got)
	}
	// The same report again (the backend retried the delivery; the Bridge answered with the same
	// report): counted once.
	afterD1 := e.clk.Now()
	run("d1")
	run("d1")
	if got := usage(start, time.Time{}); got != 73 {
		t.Fatalf("a repeated report must be counted once: %d", got)
	}
	if got := usage(afterD1, time.Time{}); got != 0 {
		t.Fatalf("a repeated report must not show up in a later window: %d", got)
	}

	run("d3")
	run("d3-retry")
	run("d4")
	run("d4")
	if got := usage(start, time.Time{}); got != 73+20+15+5 {
		t.Fatalf("after d3, its retry and d4: %d", got)
	}
	run("d5")
	if got := usage(start, time.Time{}); got != 113 {
		t.Fatalf("a negative report must be ignored: %d", got)
	}
	beforeD6 := e.clk.Now()
	run("d6")
	if got := usage(beforeD6, time.Time{}); got != MaxBrowserReportSeconds {
		t.Fatalf("an implausible report must be capped: %d", got)
	}

	// Back-to-back windows, as the backend's usage sync asks for them: every second exactly once.
	end := e.clk.Now()
	total := usage(start, end)
	for _, cut := range []time.Time{afterD2, afterD1, beforeD6, start.Add(90 * time.Second)} {
		if a, b := usage(start, cut), usage(cut, end); a+b != total {
			t.Fatalf("cut at %s: %d + %d != %d", cut.Sub(start), a, b, total)
		}
	}
	// Per user: the other user has none.
	if other, _ := e.m.Usage(ctx, "u2", start, time.Time{}); len(other) != 0 {
		t.Fatalf("u2 used nothing: %+v", other)
	}
	// The reports outlive the sandbox, like awake periods.
	if err := e.m.Delete(ctx, "u1"); err != nil {
		t.Fatal(err)
	}
	if got := usage(start, end); got != total {
		t.Fatalf("browser time must survive DeleteSandbox: %d != %d", got, total)
	}
}

// Two listeners on the same turn (the backend called Deliver twice with one delivery id) both
// see the same last event: the report is stored once.
func TestBrowserReportFromTwoListenersIsStoredOnce(t *testing.T) {
	e := newEnv(t)
	ctx := context.Background()
	e.create(t, "u1", withBrowser)
	e.br.Script = func(string, bridge.DeliverRequest) []bridge.Event {
		return []bridge.Event{{Type: "accepted"}, {Type: "turn_done", BrowserSeconds: 40, BrowserReportID: "same#1"}}
	}
	done := make(chan error, 2)
	for range 2 {
		go func() {
			_, err := collect(t, e.m, deliverReq("u1", "same"))
			done <- err
		}()
	}
	for range 2 {
		if err := <-done; err != nil {
			t.Fatal(err)
		}
	}
	e.clk.Advance(time.Minute)
	got, err := e.m.Usage(ctx, "u1", t0, time.Time{})
	if err != nil || len(got) != 1 || got[0].BrowserSeconds != 40 {
		t.Fatalf("usage: %+v %v", got, err)
	}
}
