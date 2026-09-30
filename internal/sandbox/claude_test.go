package sandbox

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/personal-agent/vm-manager/internal/config"
	"github.com/personal-agent/vm-manager/internal/platformfile"
	"github.com/personal-agent/vm-manager/internal/sandbox/sandboxtest"
	"github.com/personal-agent/vm-manager/internal/store"
)

var claudeHosts = []string{"api.anthropic.com", "claude.ai", "claude.com", "platform.claude.com"}

func (e *testEnv) egressFile(t *testing.T, userID string) string {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(e.dir, userID+".conf"))
	if os.IsNotExist(err) {
		return ""
	}
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func (e *testEnv) row(t *testing.T, userID string) store.Sandbox {
	t.Helper()
	sb, err := e.st.Get(context.Background(), userID)
	if err != nil {
		t.Fatal(err)
	}
	return sb
}

func (e *testEnv) platformRuntime(t *testing.T, userID string) string {
	t.Helper()
	f, err := platformfile.Read(context.Background(), e.eng, e.row(t, userID).ContainerID)
	if err != nil {
		t.Fatal(err)
	}
	return f.Runtime
}

func hasClaudeHosts(conf string) bool {
	for _, h := range claudeHosts {
		if !strings.Contains(conf, " "+h) {
			return false
		}
	}
	return true
}

func hasAnyClaudeHost(conf string) bool {
	return strings.Contains(conf, "anthropic") || strings.Contains(conf, "claude.")
}

// syncBuffer is a log sink safe for concurrent writes.
type syncBuffer struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (s *syncBuffer) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.Write(p)
}

func (s *syncBuffer) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.String()
}

func TestCreateStoresRuntime(t *testing.T) {
	e := newEnv(t)
	sb, _ := e.create(t, "u1")
	if sb.Runtime != store.RuntimeHermes || e.platformRuntime(t, "u1") != "hermes" {
		t.Fatalf("default runtime: %q / %q", sb.Runtime, e.platformRuntime(t, "u1"))
	}
	sb, _ = e.create(t, "u2", func(r *CreateRequest) { r.Runtime = store.RuntimeClaudeCode })
	if sb.Runtime != store.RuntimeClaudeCode || e.row(t, "u2").Runtime != store.RuntimeClaudeCode ||
		e.platformRuntime(t, "u2") != "claude_code" {
		t.Fatalf("claude runtime not stored: %+v", sb)
	}
	if _, _, err := e.m.Create(context.Background(), CreateRequest{UserID: "u3", LiteLLMKey: "k", Runtime: "gpt"}); !errors.Is(err, ErrInvalid) {
		t.Fatalf("bad runtime: %v", err)
	}
}

func TestSetAgentRuntimeAwake(t *testing.T) {
	e := newEnv(t)
	e.create(t, "u1")
	e.wake(t, "u1")
	if conf := e.egressFile(t, "u1"); !strings.Contains(conf, "mcp.example.com") || hasAnyClaudeHost(conf) {
		t.Fatalf("hermes egress must not include Anthropic hosts:\n%s", conf)
	}

	sb, err := e.m.SetAgentRuntime(context.Background(), "u1", store.RuntimeClaudeCode)
	if err != nil {
		t.Fatal(err)
	}
	if sb.Runtime != store.RuntimeClaudeCode || e.platformRuntime(t, "u1") != "claude_code" {
		t.Fatalf("runtime not applied: %+v", sb)
	}
	conf := e.egressFile(t, "u1")
	if !hasClaudeHosts(conf) || !strings.Contains(conf, "mcp.example.com") {
		t.Fatalf("claude_code egress must add Anthropic hosts to the user's own:\n%s", conf)
	}
	if strings.Join(sb.Allowlist, ",") != "mcp.example.com" {
		t.Fatalf("the reported allowlist stays the user's own: %v", sb.Allowlist)
	}
	if e.state(t, "u1") != store.StateAwake {
		t.Fatal("switching runtime needs no restart")
	}

	// The user's own domains change: Anthropic's hosts stay while the runtime is claude_code.
	if err := e.m.SetEgressAllowlist(context.Background(), "u1", []string{"api.notion.com"}, nil); err != nil {
		t.Fatal(err)
	}
	if conf := e.egressFile(t, "u1"); !hasClaudeHosts(conf) || !strings.Contains(conf, "api.notion.com") {
		t.Fatalf("allowlist update lost the claude hosts:\n%s", conf)
	}
	// Reconcile rewrites the same.
	e.m.ReconcileOnce(context.Background())
	if !hasClaudeHosts(e.egressFile(t, "u1")) {
		t.Fatal("reconcile dropped the claude hosts")
	}

	if _, err := e.m.SetAgentRuntime(context.Background(), "u1", store.RuntimeHermes); err != nil {
		t.Fatal(err)
	}
	if conf := e.egressFile(t, "u1"); hasAnyClaudeHost(conf) || !strings.Contains(conf, "api.notion.com") {
		t.Fatalf("switching back must remove Anthropic hosts:\n%s", conf)
	}
	if e.platformRuntime(t, "u1") != "hermes" || e.row(t, "u1").Runtime != store.RuntimeHermes {
		t.Fatal("runtime not switched back")
	}
}

func TestSetAgentRuntimeStoppedAndErrors(t *testing.T) {
	e := newEnv(t)
	e.create(t, "u1")
	if _, err := e.m.SetAgentRuntime(context.Background(), "u1", store.RuntimeClaudeCode); err != nil {
		t.Fatal(err)
	}
	if e.platformRuntime(t, "u1") != "claude_code" || e.egressFile(t, "u1") != "" {
		t.Fatal("a stopped sandbox gets the runtime in /data and no egress file")
	}
	// Survives an image rollout (platform.json lives on the volume) and the wake adds the hosts.
	e.cfg.AgentImageVersion = "v2"
	e.wake(t, "u1")
	if e.platformRuntime(t, "u1") != "claude_code" || !hasClaudeHosts(e.egressFile(t, "u1")) {
		t.Fatal("wake of a claude_code sandbox must allow Anthropic's hosts")
	}
	if _, err := e.m.Stop(context.Background(), "u1", false, "test"); err != nil {
		t.Fatal(err)
	}
	if e.egressFile(t, "u1") != "" {
		t.Fatal("stop removes the egress file")
	}
	if _, err := e.m.SetAgentRuntime(context.Background(), "u1", "gpt"); !errors.Is(err, ErrInvalid) {
		t.Fatalf("bad runtime: %v", err)
	}
	if _, err := e.m.SetAgentRuntime(context.Background(), "ghost", store.RuntimeHermes); !errors.Is(err, ErrNotFound) {
		t.Fatalf("unknown user: %v", err)
	}
	if rt, err := ParseRuntime(""); err != nil || rt != store.RuntimeHermes {
		t.Fatal("empty runtime means hermes")
	}
}

func TestClaudeLoginStartAndComplete(t *testing.T) {
	e := newEnv(t)
	logs := &syncBuffer{}
	e.m.log = slog.New(slog.NewTextHandler(logs, &slog.HandlerOptions{Level: slog.LevelDebug}))
	e.create(t, "u1") // stopped, runtime hermes: connecting comes before switching

	l, err := e.m.StartClaudeLogin(context.Background(), "u1")
	if err != nil {
		t.Fatal(err)
	}
	if l.LoginID == "" || !strings.HasPrefix(l.AuthorizeURL, "https://claude.ai/oauth/authorize") || l.ExpiresAt == 0 {
		t.Fatalf("login: %+v", l)
	}
	if e.state(t, "u1") != store.StateAwake {
		t.Fatal("start must wake the sandbox")
	}
	if !hasClaudeHosts(e.egressFile(t, "u1")) {
		t.Fatal("a pending sign-in needs Anthropic's hosts even on hermes")
	}

	res, err := e.m.CompleteClaudeLogin(context.Background(), "u1", l.LoginID, sandboxtest.GoodCode)
	if err != nil || !res.Connected {
		t.Fatalf("complete: %+v %v", res, err)
	}
	if !e.row(t, "u1").ClaudeConnected {
		t.Fatal("claude_connected must be stored")
	}
	if hasAnyClaudeHost(e.egressFile(t, "u1")) {
		t.Fatal("after sign-in on hermes the hosts go away again (SetAgentRuntime adds them back)")
	}
	if codes := e.br.Codes(); len(codes) != 1 || codes[0] != sandboxtest.GoodCode {
		t.Fatalf("the code must reach the Bridge unchanged: %v", codes)
	}
	// One attempt per sign-in.
	if _, err := e.m.CompleteClaudeLogin(context.Background(), "u1", l.LoginID, sandboxtest.GoodCode); !errors.Is(err, ErrLoginNotFound) {
		t.Fatalf("reused login: %v", err)
	}

	// Wrong code: not connected, the message is passed on.
	l2, _ := e.m.StartClaudeLogin(context.Background(), "u1")
	res, err = e.m.CompleteClaudeLogin(context.Background(), "u1", l2.LoginID, "wrong-code-777")
	if err != nil || res.Connected || res.Message == "" {
		t.Fatalf("wrong code: %+v %v", res, err)
	}
	if e.row(t, "u1").ClaudeConnected {
		t.Fatal("a failed sign-in stores claude_connected=false")
	}

	if strings.Contains(logs.String(), sandboxtest.GoodCode) || strings.Contains(logs.String(), "wrong-code-777") {
		t.Fatalf("the code must never be logged:\n%s", logs.String())
	}
	if !strings.Contains(logs.String(), "claude sign-in finished") {
		t.Fatal("expected sign-in log lines (without the code)")
	}
}

func TestClaudeLoginErrors(t *testing.T) {
	e := newEnv(t)
	ctx := context.Background()
	e.create(t, "u1")

	if _, err := e.m.StartClaudeLogin(ctx, "ghost"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("unknown user: %v", err)
	}
	if _, err := e.m.CompleteClaudeLogin(ctx, "ghost", "x", "code"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("unknown user complete: %v", err)
	}
	// Stopped sandbox: the waiting CLI is gone.
	if _, err := e.m.CompleteClaudeLogin(ctx, "u1", "login-1", "code"); !errors.Is(err, ErrLoginNotFound) {
		t.Fatalf("stopped sandbox: %v", err)
	}
	l, err := e.m.StartClaudeLogin(ctx, "u1")
	if err != nil {
		t.Fatal(err)
	}
	for _, bad := range []string{"", "two\nlines", strings.Repeat("x", MaxClaudeCodeLen+1)} {
		if _, err := e.m.CompleteClaudeLogin(ctx, "u1", l.LoginID, bad); !errors.Is(err, ErrInvalid) {
			t.Fatalf("bad code %q: %v", bad[:min(len(bad), 10)], err)
		}
		if err != nil && strings.Contains(err.Error(), "two") {
			t.Fatal("errors must not echo the code")
		}
	}
	if _, err := e.m.CompleteClaudeLogin(ctx, "u1", "", "code"); !errors.Is(err, ErrInvalid) {
		t.Fatalf("empty login id: %v", err)
	}
	if _, err := e.m.CompleteClaudeLogin(ctx, "u1", "not-the-id", sandboxtest.GoodCode); !errors.Is(err, ErrLoginNotFound) {
		t.Fatalf("unknown login id: %v", err)
	}
	// Expired at the Bridge (its 10-minute timer).
	e.br.ExpireLogin("u1")
	if _, err := e.m.CompleteClaudeLogin(ctx, "u1", l.LoginID, sandboxtest.GoodCode); !errors.Is(err, ErrLoginNotFound) {
		t.Fatalf("expired login: %v", err)
	}
	if e.m.loginPending("u1") {
		t.Fatal("an expired sign-in is no longer pending")
	}

	// Bridge unreachable.
	e.br.ClaudeDown = true
	if _, err := e.m.StartClaudeLogin(ctx, "u1"); !errors.Is(err, ErrAgentUnavailable) {
		t.Fatalf("bridge down: %v", err)
	}
	if e.m.loginPending("u1") || hasAnyClaudeHost(e.egressFile(t, "u1")) {
		t.Fatal("a failed start leaves nothing pending and no Anthropic hosts")
	}
	if err := e.m.ClaudeLogout(ctx, "u1"); !errors.Is(err, ErrAgentUnavailable) {
		t.Fatalf("logout bridge down: %v", err)
	}
}

// A pending sign-in counts as busy: the idle reaper and the capacity guard leave it alone.
func TestReaperKeepsSandboxWithPendingLogin(t *testing.T) {
	e := newEnv(t, func(c *config.Config) { c.AwakeCap = 1 })
	ctx := context.Background()
	e.create(t, "u1")
	e.create(t, "u2")
	if _, err := e.m.StartClaudeLogin(ctx, "u1"); err != nil {
		t.Fatal(err)
	}
	e.clk.Advance(5 * time.Minute)
	e.m.ReapOnce(ctx)
	if e.state(t, "u1") != store.StateAwake {
		t.Fatal("reaper must not stop a sandbox with a pending sign-in")
	}
	if _, _, err := e.m.Wake(ctx, "u2", PriorityHigh); !errors.Is(err, ErrCapacity) {
		t.Fatalf("capacity guard must not evict a pending sign-in: %v", err)
	}

	// Past our own TTL, but the Bridge still reports the sign-in (e.g. after a vm-manager restart).
	e.clk.Advance(6 * time.Minute)
	e.m.ReapOnce(ctx)
	if e.state(t, "u1") != store.StateAwake {
		t.Fatal("reaper must ask the Bridge about a pending sign-in")
	}
	// The Bridge expired it too: now it is idle.
	e.br.ExpireLogin("u1")
	e.m.ReapOnce(ctx)
	if e.state(t, "u1") != store.StateStopped {
		t.Fatal("idle again after the sign-in expired")
	}
}

func TestClaudeLogout(t *testing.T) {
	e := newEnv(t)
	ctx := context.Background()
	e.create(t, "u1", func(r *CreateRequest) { r.Runtime = store.RuntimeClaudeCode })
	l, _ := e.m.StartClaudeLogin(ctx, "u1")
	if _, err := e.m.CompleteClaudeLogin(ctx, "u1", l.LoginID, sandboxtest.GoodCode); err != nil {
		t.Fatal(err)
	}
	if _, err := e.m.Stop(ctx, "u1", false, "test"); err != nil {
		t.Fatal(err)
	}
	if err := e.m.ClaudeLogout(ctx, "u1"); err != nil {
		t.Fatal(err)
	}
	if e.row(t, "u1").ClaudeConnected || e.state(t, "u1") != store.StateAwake {
		t.Fatal("logout wakes the sandbox and stores claude_connected=false")
	}
	if !hasClaudeHosts(e.egressFile(t, "u1")) {
		t.Fatal("runtime is still claude_code (the backend switches it)")
	}
	if err := e.m.ClaudeLogout(ctx, "ghost"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("unknown user: %v", err)
	}
}

// The health checker keeps claude_connected in step with what the Bridge reports.
func TestHealthRecordsClaudeStatus(t *testing.T) {
	e := newEnv(t)
	ctx := context.Background()
	e.create(t, "u1")
	e.wake(t, "u1")
	e.br.SetClaudeConnected("u1", true)
	e.m.HealthOnce(ctx)
	if !e.row(t, "u1").ClaudeConnected {
		t.Fatal("health check must record claude_connected=true")
	}
	e.br.SetClaudeConnected("u1", false) // e.g. the plan was disconnected from claude.ai
	e.m.HealthOnce(ctx)
	if e.row(t, "u1").ClaudeConnected {
		t.Fatal("health check must record claude_connected=false")
	}
	// An older image that does not report it leaves the stored value alone.
	e.br.SetClaudeConnected("u1", true)
	e.m.HealthOnce(ctx)
	e.br.ReportClaude = false
	e.br.SetClaudeConnected("u1", false)
	e.m.HealthOnce(ctx)
	if !e.row(t, "u1").ClaudeConnected {
		t.Fatal("unknown status must not overwrite the stored one")
	}
}
