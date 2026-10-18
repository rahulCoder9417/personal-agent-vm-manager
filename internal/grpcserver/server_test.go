package grpcserver

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net"
	"regexp"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"

	vmv1 "github.com/personal-agent/vm-manager/gen/vm/v1"
	"github.com/personal-agent/vm-manager/internal/bridge"
	"github.com/personal-agent/vm-manager/internal/clock"
	"github.com/personal-agent/vm-manager/internal/config"
	"github.com/personal-agent/vm-manager/internal/docker"
	"github.com/personal-agent/vm-manager/internal/egress"
	"github.com/personal-agent/vm-manager/internal/sandbox"
	"github.com/personal-agent/vm-manager/internal/sandbox/sandboxtest"
	"github.com/personal-agent/vm-manager/internal/store"
)

var secret = strings.Repeat("k", 40)

type harness struct {
	client vmv1.VmManagerClient
	br     *sandboxtest.FakeBridge
	eng    *docker.Fake
	clk    *clock.Fake
}

func newHarness(t *testing.T, awakeCap int) *harness {
	t.Helper()
	return newHarnessLog(t, awakeCap, io.Discard)
}

// newHarnessLog logs everything (debug level) to w.
func newHarnessLog(t *testing.T, awakeCap int, w io.Writer) *harness {
	t.Helper()
	cfg, err := config.Load(func(k string) string {
		return map[string]string{
			"VM_INTERNAL_SECRET": secret, "DATABASE_URL": "postgres://unused",
			"WAKE_TIMEOUT": "300ms", "SHUTDOWN_GRACE": "200ms",
		}[k]
	})
	if err != nil {
		t.Fatal(err)
	}
	cfg.AwakeCap = awakeCap
	cfg.EgressAllowlistDir = t.TempDir()
	cfg.SnapshotRoot = t.TempDir()
	log := slog.New(slog.NewTextHandler(w, &slog.HandlerOptions{Level: slog.LevelDebug}))
	eng := docker.NewFake()
	br := sandboxtest.NewFakeBridge(eng)
	eg := egress.NewWriter(cfg.EgressAllowlistDir, cfg.EgressProxyContainer, eng, 0, log)
	if err := eg.Init(); err != nil {
		t.Fatal(err)
	}
	clk := clock.NewFake(time.Date(2026, 9, 1, 12, 0, 0, 0, time.UTC))
	m := sandbox.New(cfg, store.NewMemory(), eng, br, eg, clk, log)
	m.PollInterval = 5 * time.Millisecond

	lis := bufconn.Listen(1 << 20)
	srv := NewGRPCServer(m, secret, log)
	go func() { _ = srv.Serve(lis) }()
	t.Cleanup(srv.Stop)
	conn, err := grpc.NewClient("passthrough:///bufnet",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return lis.DialContext(ctx) }),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return &harness{client: vmv1.NewVmManagerClient(conn), br: br, eng: eng, clk: clk}
}

func authed() context.Context {
	return metadata.AppendToOutgoingContext(context.Background(), SecretHeader, secret)
}

func code(err error) codes.Code { return status.Code(err) }

func TestAuthRequired(t *testing.T) {
	h := newHarness(t, 5)
	ctx := context.Background()
	if _, err := h.client.GetSandbox(ctx, &vmv1.GetSandboxRequest{UserId: "u1"}); code(err) != codes.Unauthenticated {
		t.Fatalf("no secret: %v", err)
	}
	bad := metadata.AppendToOutgoingContext(ctx, SecretHeader, "wrong")
	if _, err := h.client.GetSandbox(bad, &vmv1.GetSandboxRequest{UserId: "u1"}); code(err) != codes.Unauthenticated {
		t.Fatalf("wrong secret: %v", err)
	}
	// Streaming RPC too.
	st, err := h.client.Deliver(ctx, &vmv1.DeliverRequest{UserId: "u1", DeliveryId: "d", Kind: vmv1.DeliverKind_DELIVER_KIND_MESSAGE})
	if err == nil {
		_, err = st.Recv()
	}
	if code(err) != codes.Unauthenticated {
		t.Fatalf("stream without secret: %v", err)
	}
	// With the secret: reaches the handler (NotFound for an unknown user).
	if _, err := h.client.GetSandbox(authed(), &vmv1.GetSandboxRequest{UserId: "u1"}); code(err) != codes.NotFound {
		t.Fatalf("authed: %v", err)
	}
}

func recvAll(t *testing.T, st grpc.ServerStreamingClient[vmv1.DeliverEvent]) ([]*vmv1.DeliverEvent, error) {
	t.Helper()
	var out []*vmv1.DeliverEvent
	for {
		ev, err := st.Recv()
		if errors.Is(err, io.EOF) {
			return out, nil
		}
		if err != nil {
			return out, err
		}
		out = append(out, ev)
	}
}

// Done when: Create -> Wake -> Deliver("hello") streams events ending in turn_done (over gRPC).
func TestEndToEndOverGRPC(t *testing.T) {
	h := newHarness(t, 5)
	ctx := authed()
	cr, err := h.client.CreateSandbox(ctx, &vmv1.CreateSandboxRequest{
		UserId: "u1", LitellmKey: "sk-1", Priority: vmv1.Priority_PRIORITY_NORMAL,
		Allowlist:  []string{"mcp.example.com"},
		McpServers: []*vmv1.UserMcpServer{{Id: "m1", Name: "notes", Url: "https://mcp.example.com/mcp", AuthHeaderName: "Authorization", AuthSecret: "Bearer x"}},
		Timezone:   "Europe/Berlin",
	})
	if err != nil {
		t.Fatal(err)
	}
	if !regexp.MustCompile(`^[0-9a-f]{64}$`).MatchString(cr.GetMcpTokenHash()) {
		t.Fatalf("mcp_token_hash must be lowercase sha256 hex: %q", cr.GetMcpTokenHash())
	}
	if cr.GetSandbox().GetState() != vmv1.SandboxState_SANDBOX_STATE_STOPPED {
		t.Fatalf("created state: %v", cr.GetSandbox().GetState())
	}
	again, _ := h.client.CreateSandbox(ctx, &vmv1.CreateSandboxRequest{UserId: "u1", LitellmKey: "sk-1"})
	if again.GetMcpTokenHash() != cr.GetMcpTokenHash() {
		t.Fatal("CreateSandbox must be idempotent")
	}

	wr, err := h.client.WakeSandbox(ctx, &vmv1.WakeSandboxRequest{UserId: "u1"})
	if err != nil || !wr.GetWasStopped() || wr.GetSandbox().GetState() != vmv1.SandboxState_SANDBOX_STATE_AWAKE {
		t.Fatalf("wake: %v %v", wr, err)
	}
	// Where the wake's time went: a cold start, nothing evicted, nothing re-created.
	if tm := wr.GetTimings(); tm == nil || tm.GetEvicted() || tm.GetRecreated() || tm.GetContainerStartMs() < 0 ||
		tm.GetBridgeReadyMs() < 0 {
		t.Fatalf("wake timings: %v", tm)
	}

	st, err := h.client.Deliver(ctx, &vmv1.DeliverRequest{
		UserId: "u1", DeliveryId: "evt-1", Kind: vmv1.DeliverKind_DELIVER_KIND_MESSAGE,
		SessionKey: "telegram:42", Channel: "telegram", Text: "hello",
		Attachments:  []*vmv1.Attachment{{Id: "a1", Name: "x.png", MimeType: "image/png", Url: "http://api:8080/files/a1"}},
		MetadataJson: `{"k":"v"}`,
	})
	if err != nil {
		t.Fatal(err)
	}
	evs, err := recvAll(t, st)
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, ev := range evs {
		got = append(got, ev.GetType().String())
		if ev.GetAtUnixMs() == 0 {
			t.Error("events must carry at_unix_ms")
		}
	}
	if strings.Join(got, ",") != "DELIVER_EVENT_TYPE_ACCEPTED,DELIVER_EVENT_TYPE_WORKING,DELIVER_EVENT_TYPE_TURN_DONE" {
		t.Fatalf("events: %v", got)
	}
	d := h.br.Delivered()[0]
	if d.Kind != "message" || d.SessionKey != "telegram:42" || d.Text != "hello" || len(d.Attachments) != 1 ||
		d.Attachments[0].MimeType != "image/png" || string(d.Metadata) != `{"k":"v"}` {
		t.Fatalf("bridge request: %+v", d)
	}

	if _, err := h.client.Touch(ctx, &vmv1.TouchRequest{UserId: "u1"}); err != nil {
		t.Fatal(err)
	}
	gs, err := h.client.GetSandbox(ctx, &vmv1.GetSandboxRequest{UserId: "u1"})
	if err != nil || gs.GetMemoryBytes() <= 0 || gs.GetAllowlist()[0] != "mcp.example.com" || gs.GetLastActiveAtUnixMs() == 0 {
		t.Fatalf("get: %v %v", gs, err)
	}
	ls, _ := h.client.ListSandboxes(ctx, &vmv1.ListSandboxesRequest{States: []vmv1.SandboxState{vmv1.SandboxState_SANDBOX_STATE_AWAKE}})
	if len(ls.GetSandboxes()) != 1 {
		t.Fatalf("list awake: %v", ls)
	}
	if _, err := h.client.ListSandboxes(ctx, &vmv1.ListSandboxesRequest{States: []vmv1.SandboxState{vmv1.SandboxState_SANDBOX_STATE_UNSPECIFIED}}); code(err) != codes.InvalidArgument {
		t.Fatalf("list unspecified state: %v", err)
	}

	h.clk.Advance(90 * time.Second)
	sr, err := h.client.StopSandbox(ctx, &vmv1.StopSandboxRequest{UserId: "u1", Reason: "test"})
	if err != nil || sr.GetSandbox().GetState() != vmv1.SandboxState_SANDBOX_STATE_STOPPED {
		t.Fatalf("stop: %v %v", sr, err)
	}
	us, err := h.client.GetUsage(ctx, &vmv1.GetUsageRequest{SinceUnixMs: 0})
	if err != nil || len(us.GetUsage()) != 1 || us.GetUsage()[0].GetAwakeSeconds() != 90 {
		t.Fatalf("usage: %v %v", us, err)
	}

	if _, err := h.client.SetEgressAllowlist(ctx, &vmv1.SetEgressAllowlistRequest{UserId: "u1", Domains: []string{"not a domain"}}); code(err) != codes.InvalidArgument {
		t.Fatalf("bad domain: %v", err)
	}
	if _, err := h.client.SetEgressAllowlist(ctx, &vmv1.SetEgressAllowlistRequest{UserId: "u1", Domains: []string{"api.notion.com"}}); err != nil {
		t.Fatal(err)
	}
	snap, err := h.client.SnapshotVolume(ctx, &vmv1.SnapshotVolumeRequest{UserId: "u1", Destination: "nightly"})
	if err != nil || !strings.HasSuffix(snap.GetLocation(), ".tar.gz") || snap.GetSizeBytes() <= 0 {
		t.Fatalf("snapshot: %v %v", snap, err)
	}

	if _, err := h.client.DeleteSandbox(ctx, &vmv1.DeleteSandboxRequest{UserId: "u1"}); err != nil {
		t.Fatal(err)
	}
	if _, err := h.client.GetSandbox(ctx, &vmv1.GetSandboxRequest{UserId: "u1"}); code(err) != codes.NotFound {
		t.Fatalf("after delete: %v", err)
	}
}

// The Bridge's text_delta (the whole draft of the answer so far) reaches the backend as TEXT_DELTA, with
// the draft in `message`, and every Bridge event type has a proto type.
func TestDeliverStreamsTextDeltas(t *testing.T) {
	h := newHarness(t, 5)
	ctx := authed()
	if _, err := h.client.CreateSandbox(ctx, &vmv1.CreateSandboxRequest{UserId: "u1", LitellmKey: "sk-1"}); err != nil {
		t.Fatal(err)
	}
	h.br.Script = func(string, bridge.DeliverRequest) []bridge.Event {
		return []bridge.Event{{Type: "accepted"}, {Type: "text_delta", Message: "Hel"},
			{Type: "text_delta", Message: "Hello there"},
			{Type: "turn_done", Timings: []byte(`{"v":1,"marks":{"first_text":900}}`)}}
	}
	st, err := h.client.Deliver(ctx, &vmv1.DeliverRequest{
		UserId: "u1", DeliveryId: "evt-1", Kind: vmv1.DeliverKind_DELIVER_KIND_MESSAGE,
		SessionKey: "web", Channel: "web", Text: "hello", MetadataJson: `{"stream_text":true}`,
	})
	if err != nil {
		t.Fatal(err)
	}
	evs, err := recvAll(t, st)
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, ev := range evs {
		got = append(got, ev.GetType().String()+"="+ev.GetMessage())
	}
	want := "DELIVER_EVENT_TYPE_ACCEPTED=,DELIVER_EVENT_TYPE_TEXT_DELTA=Hel,DELIVER_EVENT_TYPE_TEXT_DELTA=Hello there," +
		"DELIVER_EVENT_TYPE_TURN_DONE="
	if strings.Join(got, ",") != want {
		t.Fatalf("events: %v", got)
	}
	// The Bridge's stage times reach the backend on the last event, as the Bridge wrote them.
	if last := evs[len(evs)-1]; last.GetTimingsJson() != `{"v":1,"marks":{"first_text":900}}` || evs[1].GetTimingsJson() != "" {
		t.Fatalf("timings_json: %q", last.GetTimingsJson())
	}
	if d := h.br.Delivered()[0]; string(d.Metadata) != `{"stream_text":true}` {
		t.Fatalf("the metadata goes to the Bridge as it came: %s", d.Metadata)
	}
	for _, typ := range []string{bridge.EventAccepted, bridge.EventWorking, bridge.EventTextDelta, bridge.EventTurnDone,
		bridge.EventError} {
		if p, ok := eventToProto[typ]; !ok || p == vmv1.DeliverEventType_DELIVER_EVENT_TYPE_UNSPECIFIED {
			t.Errorf("bridge event %q has no proto type", typ)
		}
	}
}

func TestErrorCodes(t *testing.T) {
	h := newHarness(t, 1)
	ctx := authed()
	if _, err := h.client.CreateSandbox(ctx, &vmv1.CreateSandboxRequest{UserId: "bad/id", LitellmKey: "k"}); code(err) != codes.InvalidArgument {
		t.Fatalf("bad id: %v", err)
	}
	st, _ := h.client.Deliver(ctx, &vmv1.DeliverRequest{UserId: "u1", DeliveryId: "d"})
	if _, err := st.Recv(); code(err) != codes.InvalidArgument {
		t.Fatalf("unspecified kind: %v", err)
	}
	if _, err := h.client.WakeSandbox(ctx, &vmv1.WakeSandboxRequest{UserId: "ghost"}); code(err) != codes.NotFound {
		t.Fatalf("unknown user: %v", err)
	}

	// Capacity: cap 1, "a" is mid-turn -> waking "b" is RESOURCE_EXHAUSTED (retryable).
	for _, u := range []string{"a", "b"} {
		if _, err := h.client.CreateSandbox(ctx, &vmv1.CreateSandboxRequest{UserId: u, LitellmKey: "k"}); err != nil {
			t.Fatal(err)
		}
	}
	h.br.Block = make(chan struct{})
	busy, _ := h.client.Deliver(ctx, &vmv1.DeliverRequest{UserId: "a", DeliveryId: "d1", Kind: vmv1.DeliverKind_DELIVER_KIND_MESSAGE, Text: "long"})
	if ev, err := busy.Recv(); err != nil || ev.GetType() != vmv1.DeliverEventType_DELIVER_EVENT_TYPE_ACCEPTED {
		t.Fatalf("first event: %v %v", ev, err)
	}
	if _, err := h.client.WakeSandbox(ctx, &vmv1.WakeSandboxRequest{UserId: "b"}); code(err) != codes.ResourceExhausted {
		t.Fatalf("want RESOURCE_EXHAUSTED, got %v", err)
	}
	if _, err := h.client.StopSandbox(ctx, &vmv1.StopSandboxRequest{UserId: "a"}); code(err) != codes.FailedPrecondition {
		t.Fatalf("polite stop mid-turn: %v", err)
	}
	close(h.br.Block)
	if _, err := recvAll(t, busy); err != nil {
		t.Fatal(err)
	}

	// A stuck turn ends with ERROR + platform_fault.
	h.br.Block = make(chan struct{})
	stuck, _ := h.client.Deliver(ctx, &vmv1.DeliverRequest{UserId: "a", DeliveryId: "d2", Kind: vmv1.DeliverKind_DELIVER_KIND_MESSAGE, TimeoutSeconds: 1})
	evs, err := recvAll(t, stuck)
	if err != nil {
		t.Fatal(err)
	}
	last := evs[len(evs)-1]
	if last.GetType() != vmv1.DeliverEventType_DELIVER_EVENT_TYPE_ERROR || !last.GetPlatformFault() {
		t.Fatalf("stuck turn: %v", evs)
	}
}

// More than CLAUDE_LOGINS_MAX (2) waiting Claude sign-ins: RESOURCE_EXHAUSTED (retry later), with
// a message the backend can tell apart.
func TestClaudeLoginsFullIsResourceExhausted(t *testing.T) {
	h := newHarness(t, 5)
	ctx := authed()
	for _, u := range []string{"a", "b", "c"} {
		if _, err := h.client.CreateSandbox(ctx, &vmv1.CreateSandboxRequest{UserId: u, LitellmKey: "k"}); err != nil {
			t.Fatal(err)
		}
	}
	for _, u := range []string{"a", "b"} {
		if _, err := h.client.StartClaudeLogin(ctx, &vmv1.StartClaudeLoginRequest{UserId: u}); err != nil {
			t.Fatalf("%s: %v", u, err)
		}
	}
	_, err := h.client.StartClaudeLogin(ctx, &vmv1.StartClaudeLoginRequest{UserId: "c"})
	if code(err) != codes.ResourceExhausted || !strings.HasPrefix(status.Convert(err).Message(), "logins_full:") {
		t.Fatalf("third sign-in: %v", err)
	}
}
