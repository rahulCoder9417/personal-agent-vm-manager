package grpcserver

import (
	"bytes"
	"strings"
	"sync"
	"testing"

	"google.golang.org/grpc/codes"

	vmv1 "github.com/personal-agent/vm-manager/gen/vm/v1"
	"github.com/personal-agent/vm-manager/internal/sandbox/sandboxtest"
)

type lockedBuffer struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (l *lockedBuffer) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.b.Write(p)
}

func (l *lockedBuffer) String() string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.b.String()
}

func TestAgentRuntimeAndClaudeSignInOverGRPC(t *testing.T) {
	logs := &lockedBuffer{}
	h := newHarnessLog(t, 5, logs)
	ctx := authed()
	const wrongCode = "wrong-code-4242"

	cr, err := h.client.CreateSandbox(ctx, &vmv1.CreateSandboxRequest{UserId: "u1", LitellmKey: "sk-1",
		Runtime: vmv1.AgentRuntime_AGENT_RUNTIME_CLAUDE_CODE})
	if err != nil {
		t.Fatal(err)
	}
	if cr.GetSandbox().GetRuntime() != vmv1.AgentRuntime_AGENT_RUNTIME_CLAUDE_CODE || cr.GetSandbox().GetClaudeConnected() {
		t.Fatalf("create: %v", cr.GetSandbox())
	}
	if _, err := h.client.CreateSandbox(ctx, &vmv1.CreateSandboxRequest{UserId: "u2", LitellmKey: "sk-1"}); err != nil {
		t.Fatal(err)
	}
	if gs, _ := h.client.GetSandbox(ctx, &vmv1.GetSandboxRequest{UserId: "u2"}); gs.GetRuntime() != vmv1.AgentRuntime_AGENT_RUNTIME_HERMES {
		t.Fatalf("unspecified runtime is hermes: %v", gs)
	}

	// Sign in (wakes the sandbox).
	st, err := h.client.StartClaudeLogin(ctx, &vmv1.StartClaudeLoginRequest{UserId: "u1"})
	if err != nil || st.GetLoginId() == "" || !strings.HasPrefix(st.GetAuthorizeUrl(), "https://claude.ai/") || st.GetExpiresAtUnixMs() == 0 {
		t.Fatalf("start: %v %v", st, err)
	}
	wr, _ := h.client.WakeSandbox(ctx, &vmv1.WakeSandboxRequest{UserId: "u1"})
	if wr.GetWasStopped() {
		t.Fatal("start must have woken the sandbox")
	}
	res, err := h.client.CompleteClaudeLogin(ctx, &vmv1.CompleteClaudeLoginRequest{UserId: "u1", LoginId: st.GetLoginId(), Code: wrongCode})
	if err != nil || res.GetConnected() || res.GetMessage() == "" {
		t.Fatalf("wrong code: %v %v", res, err)
	}
	if _, err := h.client.CompleteClaudeLogin(ctx, &vmv1.CompleteClaudeLoginRequest{UserId: "u1", LoginId: st.GetLoginId(), Code: sandboxtest.GoodCode}); code(err) != codes.FailedPrecondition {
		t.Fatalf("used login: %v", err)
	}
	st, _ = h.client.StartClaudeLogin(ctx, &vmv1.StartClaudeLoginRequest{UserId: "u1"})
	if _, err := h.client.CompleteClaudeLogin(ctx, &vmv1.CompleteClaudeLoginRequest{UserId: "u1", LoginId: "nope", Code: sandboxtest.GoodCode}); code(err) != codes.FailedPrecondition {
		t.Fatalf("unknown login: %v", err)
	}
	if _, err := h.client.CompleteClaudeLogin(ctx, &vmv1.CompleteClaudeLoginRequest{UserId: "u1", LoginId: st.GetLoginId(), Code: "a\nb"}); code(err) != codes.InvalidArgument {
		t.Fatalf("multi-line code: %v", err)
	}
	res, err = h.client.CompleteClaudeLogin(ctx, &vmv1.CompleteClaudeLoginRequest{UserId: "u1", LoginId: st.GetLoginId(), Code: sandboxtest.GoodCode})
	if err != nil || !res.GetConnected() {
		t.Fatalf("complete: %v %v", res, err)
	}
	gs, _ := h.client.GetSandbox(ctx, &vmv1.GetSandboxRequest{UserId: "u1"})
	if !gs.GetClaudeConnected() || gs.GetRuntime() != vmv1.AgentRuntime_AGENT_RUNTIME_CLAUDE_CODE {
		t.Fatalf("get after sign-in: %v", gs)
	}
	ls, _ := h.client.ListSandboxes(ctx, &vmv1.ListSandboxesRequest{})
	for _, sb := range ls.GetSandboxes() {
		if sb.GetUserId() == "u1" && !sb.GetClaudeConnected() {
			t.Fatal("list must report claude_connected")
		}
	}

	// Runtime switch.
	sr, err := h.client.SetAgentRuntime(ctx, &vmv1.SetAgentRuntimeRequest{UserId: "u1", Runtime: vmv1.AgentRuntime_AGENT_RUNTIME_HERMES})
	if err != nil || sr.GetSandbox().GetRuntime() != vmv1.AgentRuntime_AGENT_RUNTIME_HERMES {
		t.Fatalf("set runtime: %v %v", sr, err)
	}
	if _, err := h.client.SetAgentRuntime(ctx, &vmv1.SetAgentRuntimeRequest{UserId: "u1", Runtime: vmv1.AgentRuntime(42)}); code(err) != codes.InvalidArgument {
		t.Fatalf("unknown runtime: %v", err)
	}
	if _, err := h.client.CreateSandbox(ctx, &vmv1.CreateSandboxRequest{UserId: "u3", LitellmKey: "k", Runtime: vmv1.AgentRuntime(42)}); code(err) != codes.InvalidArgument {
		t.Fatalf("create with unknown runtime: %v", err)
	}

	// Sign out.
	if _, err := h.client.ClaudeLogout(ctx, &vmv1.ClaudeLogoutRequest{UserId: "u1"}); err != nil {
		t.Fatal(err)
	}
	if gs, _ := h.client.GetSandbox(ctx, &vmv1.GetSandboxRequest{UserId: "u1"}); gs.GetClaudeConnected() {
		t.Fatal("logout must clear claude_connected")
	}

	// Error codes.
	for name, err := range map[string]error{
		"set": func() error {
			_, e := h.client.SetAgentRuntime(ctx, &vmv1.SetAgentRuntimeRequest{UserId: "ghost"})
			return e
		}(),
		"start": func() error {
			_, e := h.client.StartClaudeLogin(ctx, &vmv1.StartClaudeLoginRequest{UserId: "ghost"})
			return e
		}(),
		"complete": func() error {
			_, e := h.client.CompleteClaudeLogin(ctx, &vmv1.CompleteClaudeLoginRequest{UserId: "ghost", LoginId: "x", Code: "y"})
			return e
		}(),
		"logout": func() error { _, e := h.client.ClaudeLogout(ctx, &vmv1.ClaudeLogoutRequest{UserId: "ghost"}); return e }(),
	} {
		if code(err) != codes.NotFound {
			t.Errorf("%s for an unknown user: %v", name, err)
		}
	}
	h.br.ClaudeDown = true
	if _, err := h.client.StartClaudeLogin(ctx, &vmv1.StartClaudeLoginRequest{UserId: "u1"}); code(err) != codes.Unavailable {
		t.Fatalf("bridge down: %v", err)
	}
	if _, err := h.client.ClaudeLogout(ctx, &vmv1.ClaudeLogoutRequest{UserId: "u1"}); code(err) != codes.Unavailable {
		t.Fatalf("logout bridge down: %v", err)
	}

	// The code never appears in any log line; the interceptor marks it redacted.
	out := logs.String()
	if strings.Contains(out, sandboxtest.GoodCode) || strings.Contains(out, wrongCode) {
		t.Fatalf("the sign-in code leaked into the logs:\n%s", out)
	}
	if !strings.Contains(out, "CompleteClaudeLogin") || !strings.Contains(out, "code=[REDACTED]") {
		t.Fatalf("expected a redacted interceptor line:\n%s", out)
	}
}
