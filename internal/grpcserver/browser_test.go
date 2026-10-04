package grpcserver

import (
	"context"
	"testing"
	"time"

	"google.golang.org/grpc/codes"

	vmv1 "github.com/personal-agent/vm-manager/gen/vm/v1"
	"github.com/personal-agent/vm-manager/internal/bridge"
)

// The browser switch and browser time over gRPC, as the backend uses them:
// CreateSandbox.browser_enabled, Sandbox.browser_enabled, SetBrowser, GetUsage.browser_seconds.
func TestBrowserSwitchAndUsageOverGRPC(t *testing.T) {
	h := newHarness(t, 5)
	ctx := authed()

	cr, err := h.client.CreateSandbox(ctx, &vmv1.CreateSandboxRequest{UserId: "max", LitellmKey: "sk-1", BrowserEnabled: true})
	if err != nil || !cr.GetSandbox().GetBrowserEnabled() {
		t.Fatalf("create with the browser: %v %v", cr.GetSandbox(), err)
	}
	cr2, err := h.client.CreateSandbox(ctx, &vmv1.CreateSandboxRequest{UserId: "pro", LitellmKey: "sk-2"})
	if err != nil || cr2.GetSandbox().GetBrowserEnabled() {
		t.Fatalf("create without: %v %v", cr2.GetSandbox(), err)
	}
	// Reported by every answer that returns a sandbox.
	if gs, _ := h.client.GetSandbox(ctx, &vmv1.GetSandboxRequest{UserId: "max"}); !gs.GetBrowserEnabled() {
		t.Fatalf("get: %v", gs)
	}
	ls, _ := h.client.ListSandboxes(ctx, &vmv1.ListSandboxesRequest{})
	if len(ls.GetSandboxes()) != 2 || !ls.GetSandboxes()[0].GetBrowserEnabled() || ls.GetSandboxes()[1].GetBrowserEnabled() {
		t.Fatalf("list: %v", ls.GetSandboxes())
	}

	// SetBrowser: stored and returned at once; the container gets it on the next wake.
	sr, err := h.client.SetBrowser(ctx, &vmv1.SetBrowserRequest{UserId: "pro", Enabled: true})
	if err != nil || !sr.GetSandbox().GetBrowserEnabled() || sr.GetSandbox().GetUserId() != "pro" ||
		sr.GetSandbox().GetContainerId() != cr2.GetSandbox().GetContainerId() {
		t.Fatalf("SetBrowser on: %v %v", sr.GetSandbox(), err)
	}
	wr, err := h.client.WakeSandbox(ctx, &vmv1.WakeSandboxRequest{UserId: "pro"})
	if err != nil || !wr.GetSandbox().GetBrowserEnabled() || wr.GetSandbox().GetContainerId() == cr2.GetSandbox().GetContainerId() {
		t.Fatalf("the wake must re-create the container for its new limits: %v %v", wr.GetSandbox(), err)
	}
	c, _ := h.eng.Get(wr.GetSandbox().GetContainerId())
	if c.Spec.MemoryBytes != 2048<<20 {
		t.Fatalf("memory limit of the re-created container: %d", c.Spec.MemoryBytes)
	}
	sr, err = h.client.SetBrowser(ctx, &vmv1.SetBrowserRequest{UserId: "pro", Enabled: false})
	if err != nil || sr.GetSandbox().GetBrowserEnabled() {
		t.Fatalf("SetBrowser off: %v %v", sr.GetSandbox(), err)
	}
	if _, err := h.client.SetBrowser(ctx, &vmv1.SetBrowserRequest{UserId: "nobody", Enabled: true}); code(err) != codes.NotFound {
		t.Fatalf("unknown user: %v", err)
	}
	if _, err := h.client.SetBrowser(ctx, &vmv1.SetBrowserRequest{UserId: "bad id", Enabled: true}); code(err) != codes.InvalidArgument {
		t.Fatalf("bad user id: %v", err)
	}
	if _, err := h.client.SetBrowser(context.Background(), &vmv1.SetBrowserRequest{UserId: "pro", Enabled: true}); code(err) != codes.Unauthenticated {
		t.Fatalf("without the secret: %v", err)
	}

	// Browser time: the Bridge reports it with the end of a turn; GetUsage returns it once.
	h.br.Script = func(_ string, req bridge.DeliverRequest) []bridge.Event {
		last := bridge.Event{Type: "turn_done"}
		if req.DeliveryID == "d-browser" {
			last.BrowserSeconds, last.BrowserReportID = 95, "d-browser#1"
		}
		return []bridge.Event{{Type: "accepted"}, last}
	}
	start := h.clk.Now()
	deliver := func(id string) {
		t.Helper()
		st, err := h.client.Deliver(ctx, &vmv1.DeliverRequest{UserId: "max", DeliveryId: id,
			Kind: vmv1.DeliverKind_DELIVER_KIND_MESSAGE, SessionKey: "telegram:1", Text: "hi"})
		if err != nil {
			t.Fatal(err)
		}
		evs, err := recvAll(t, st)
		if err != nil || evs[len(evs)-1].GetType() != vmv1.DeliverEventType_DELIVER_EVENT_TYPE_TURN_DONE {
			t.Fatalf("deliver %s: %v %v", id, evs, err)
		}
		h.clk.Advance(time.Minute)
	}
	browser := func(since, until time.Time) int64 {
		t.Helper()
		req := &vmv1.GetUsageRequest{UserId: "max", SinceUnixMs: since.UnixMilli()}
		if !until.IsZero() {
			req.UntilUnixMs = until.UnixMilli()
		}
		res, err := h.client.GetUsage(ctx, req)
		if err != nil {
			t.Fatal(err)
		}
		if len(res.GetUsage()) == 0 {
			return 0
		}
		return res.GetUsage()[0].GetBrowserSeconds()
	}
	deliver("d-plain")
	if got := browser(start, time.Time{}); got != 0 {
		t.Fatalf("no browser use, nothing counted: %d", got)
	}
	mid := h.clk.Now()
	deliver("d-browser")
	deliver("d-browser") // the same report again
	end := h.clk.Now()
	if got := browser(start, time.Time{}); got != 95 {
		t.Fatalf("browser_seconds: %d", got)
	}
	// The windows a usage sync asks for, one after the other: 0, then 95, then 0.
	if a, b, c := browser(start, mid), browser(mid, end), browser(end, end.Add(time.Hour)); a != 0 || b != 95 || c != 0 {
		t.Fatalf("back-to-back windows: %d %d %d", a, b, c)
	}
}
