package sandbox

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/personal-agent/vm-manager/internal/bridge"
	"github.com/personal-agent/vm-manager/internal/config"
)

// The Bridge's stage times on a turn's last event are passed on as they are; anything that is not
// a JSON object of a sane size is not.
func TestDeliverPassesTheBridgesStageTimesOn(t *testing.T) {
	e := newEnv(t)
	e.create(t, "u1")
	report := json.RawMessage(`{"v":1,"engine":"claude_code","marks":{"engine_ready":1800,"first_text":5200}}`)
	e.br.Script = func(string, bridge.DeliverRequest) []bridge.Event {
		return []bridge.Event{{Type: "accepted"}, {Type: "working", Message: "using recall", Timings: report},
			{Type: "turn_done", Timings: report}}
	}
	evs, err := collect(t, e.m, deliverReq("u1", "d1"))
	if err != nil {
		t.Fatal(err)
	}
	if types(evs) != "accepted,working,turn_done" {
		t.Fatalf("events: %s", types(evs))
	}
	if evs[2].Timings != string(report) {
		t.Fatalf("turn_done timings: %q", evs[2].Timings)
	}
	if evs[0].Timings != "" || evs[1].Timings != "" {
		t.Fatal("only the last event carries the stage times")
	}

	// An error ends the turn: its stage times are passed on too.
	e.br.Script = func(string, bridge.DeliverRequest) []bridge.Event {
		return []bridge.Event{{Type: "accepted"}, {Type: "error", Message: "boom", PlatformFault: true, Timings: report}}
	}
	evs, _ = collect(t, e.m, deliverReq("u1", "d2"))
	if last := evs[len(evs)-1]; last.Type != "error" || !last.PlatformFault || last.Timings != string(report) {
		t.Fatalf("error event: %+v", last)
	}

	for name, raw := range map[string]json.RawMessage{
		"none":       nil,
		"not object": json.RawMessage(`[1,2,3]`),
		"null":       json.RawMessage(`null`),
		"too big":    json.RawMessage(`{"pad":"` + strings.Repeat("x", MaxTimingsBytes) + `"}`),
	} {
		e.br.Script = func(string, bridge.DeliverRequest) []bridge.Event {
			return []bridge.Event{{Type: "accepted"}, {Type: "turn_done", Timings: raw}}
		}
		evs, _ = collect(t, e.m, deliverReq("u1", "d-"+name))
		if last := evs[len(evs)-1]; last.Type != "turn_done" || last.Timings != "" {
			t.Fatalf("%s: %q must not be passed on", name, last.Timings)
		}
	}
}

// A wake says where its time went: a cold start has a container start and a ready check; an awake
// sandbox has neither; an image rollout re-creates; a full server evicts.
func TestWakeTimings(t *testing.T) {
	ctx := context.Background()
	e := newEnv(t, func(c *config.Config) { c.AwakeCap = 1 })
	e.create(t, "u1")

	_, was, cold, err := e.m.WakeTimed(ctx, "u1", 0)
	if err != nil || !was {
		t.Fatalf("cold wake: was=%v err=%v", was, err)
	}
	if cold.ContainerStart <= 0 || cold.BridgeReady <= 0 || cold.Recreated || cold.Evicted {
		t.Fatalf("cold wake timings: %+v", cold)
	}

	_, was, warm, err := e.m.WakeTimed(ctx, "u1", 0)
	if err != nil || was {
		t.Fatalf("warm wake: was=%v err=%v", was, err)
	}
	if warm.ContainerStart != 0 || warm.BridgeReady != 0 || warm.CapacityWait != 0 || warm.Recreated || warm.Evicted {
		t.Fatalf("an awake sandbox has nothing to start: %+v", warm)
	}

	// The server is full (cap 1): waking another user stops u1 first, politely.
	e.create(t, "u2")
	e.clk.Advance(time.Minute)
	_, _, full, err := e.m.WakeTimed(ctx, "u2", 0)
	if err != nil {
		t.Fatal(err)
	}
	if !full.Evicted || full.CapacityWait <= 0 || e.br.ShutdownCount() != 1 {
		t.Fatalf("eviction timings: %+v (shutdowns %d)", full, e.br.ShutdownCount())
	}

	// A new image: the container is made again before it starts.
	_, _ = e.m.Stop(ctx, "u2", false, "test")
	e.cfg.AgentImage, e.cfg.AgentImageVersion = "agent:v2", "v2"
	_, _, rollout, err := e.m.WakeTimed(ctx, "u2", 0)
	if err != nil || !rollout.Recreated || rollout.ContainerStart <= 0 {
		t.Fatalf("rollout timings: %+v err=%v", rollout, err)
	}
}
