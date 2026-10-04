package store

import (
	"context"
	"errors"
	"slices"
	"testing"
	"time"
)

var t0 = time.Date(2026, 9, 1, 12, 0, 0, 0, time.UTC)

func newSandbox(userID string) Sandbox {
	return Sandbox{
		UserID: userID, ContainerID: "c-" + userID, ImageVersion: "v1", State: StateStopped, Priority: 1,
		LastActiveAt: t0, MCPTokenHash: "mcp-" + userID, BridgeSecretHash: "br-" + userID,
		Allowlist: []string{"example.com"}, CreatedAt: t0, UpdatedAt: t0,
	}
}

// runSuite checks the Store contract; it runs against every implementation.
func runSuite(t *testing.T, newStore func(t *testing.T) Store) {
	ctx := context.Background()

	t.Run("insert get list", func(t *testing.T) {
		s := newStore(t)
		if _, err := s.Get(ctx, "u1"); !errors.Is(err, ErrNotFound) {
			t.Fatalf("want ErrNotFound, got %v", err)
		}
		if err := s.Insert(ctx, newSandbox("u1")); err != nil {
			t.Fatal(err)
		}
		if err := s.Insert(ctx, newSandbox("u1")); !errors.Is(err, ErrExists) {
			t.Fatalf("want ErrExists, got %v", err)
		}
		sb2 := newSandbox("u2")
		sb2.State = StateAwake
		sb2.Allowlist = nil
		if err := s.Insert(ctx, sb2); err != nil {
			t.Fatal(err)
		}
		got, err := s.Get(ctx, "u1")
		if err != nil {
			t.Fatal(err)
		}
		if got.ContainerID != "c-u1" || got.State != StateStopped || !got.LastActiveAt.Equal(t0) ||
			got.MCPTokenHash != "mcp-u1" || !slices.Equal(got.Allowlist, []string{"example.com"}) || got.Priority != 1 {
			t.Fatalf("unexpected row: %+v", got)
		}
		if got.Runtime != RuntimeHermes || got.ClaudeConnected {
			t.Fatalf("an unset runtime is stored as hermes, not connected: %+v", got)
		}
		if got.BrowserEnabled {
			t.Fatalf("the browser is off unless asked for: %+v", got)
		}
		sb3 := newSandbox("u3")
		sb3.BrowserEnabled = true
		if err := s.Insert(ctx, sb3); err != nil {
			t.Fatal(err)
		}
		if got, _ := s.Get(ctx, "u3"); !got.BrowserEnabled {
			t.Fatalf("the browser switch must be stored at insert: %+v", got)
		}
		must(t, s.Delete(ctx, "u3"))
		all, _ := s.List(ctx)
		if len(all) != 2 || all[0].UserID != "u1" || all[1].UserID != "u2" {
			t.Fatalf("list all: %+v", all)
		}
		awake, _ := s.List(ctx, StateAwake, StateBusy)
		if len(awake) != 1 || awake[0].UserID != "u2" || awake[0].Allowlist == nil {
			t.Fatalf("list awake: %+v", awake)
		}
	})

	t.Run("updates", func(t *testing.T) {
		s := newStore(t)
		_ = s.Insert(ctx, newSandbox("u1"))
		later := t0.Add(time.Minute)
		if err := s.SetState(ctx, "u1", StateBusy, later); err != nil {
			t.Fatal(err)
		}
		if err := s.SetContainer(ctx, "u1", "c-new", "v2", later); err != nil {
			t.Fatal(err)
		}
		if err := s.SetAllowlist(ctx, "u1", []string{"a.com", ".b.org"}, later); err != nil {
			t.Fatal(err)
		}
		if err := s.SetPriority(ctx, "u1", 3, later); err != nil {
			t.Fatal(err)
		}
		if err := s.SetRuntime(ctx, "u1", RuntimeClaudeCode, later); err != nil {
			t.Fatal(err)
		}
		if err := s.SetClaudeConnected(ctx, "u1", true, later); err != nil {
			t.Fatal(err)
		}
		if err := s.SetBrowserEnabled(ctx, "u1", true, later); err != nil {
			t.Fatal(err)
		}
		if err := s.Touch(ctx, "u1", later); err != nil {
			t.Fatal(err)
		}
		if err := s.Touch(ctx, "u1", t0); err != nil { // older: must not move backwards
			t.Fatal(err)
		}
		got, _ := s.Get(ctx, "u1")
		if got.State != StateBusy || got.ContainerID != "c-new" || got.ImageVersion != "v2" || got.Priority != 3 ||
			!slices.Equal(got.Allowlist, []string{"a.com", ".b.org"}) || !got.LastActiveAt.Equal(later) ||
			got.Runtime != RuntimeClaudeCode || !got.ClaudeConnected || !got.BrowserEnabled {
			t.Fatalf("unexpected row: %+v", got)
		}
		if err := s.SetBrowserEnabled(ctx, "u1", false, later); err != nil {
			t.Fatal(err)
		}
		if got, _ := s.Get(ctx, "u1"); got.BrowserEnabled {
			t.Fatalf("the browser switch must turn off again: %+v", got)
		}
		if err := s.SetBrowserEnabled(ctx, "nobody", true, later); !errors.Is(err, ErrNotFound) {
			t.Fatalf("SetBrowserEnabled: want ErrNotFound, got %v", err)
		}
		if err := s.SetState(ctx, "nobody", StateAwake, later); !errors.Is(err, ErrNotFound) {
			t.Fatalf("want ErrNotFound, got %v", err)
		}
		if err := s.SetRuntime(ctx, "nobody", RuntimeHermes, later); !errors.Is(err, ErrNotFound) {
			t.Fatalf("SetRuntime: want ErrNotFound, got %v", err)
		}
		if err := s.SetClaudeConnected(ctx, "nobody", true, later); !errors.Is(err, ErrNotFound) {
			t.Fatalf("SetClaudeConnected: want ErrNotFound, got %v", err)
		}
		if err := s.Delete(ctx, "u1"); err != nil {
			t.Fatal(err)
		}
		if err := s.Delete(ctx, "u1"); err != nil {
			t.Fatal("delete must be idempotent")
		}
		if _, err := s.Get(ctx, "u1"); !errors.Is(err, ErrNotFound) {
			t.Fatal("row should be gone")
		}
	})

	t.Run("awake periods and usage", func(t *testing.T) {
		s := newStore(t)
		// u1: [t0, t0+10m) closed; then [t0+20m, open)
		must(t, s.StartAwake(ctx, "u1", t0))
		must(t, s.StartAwake(ctx, "u1", t0.Add(time.Minute))) // already open: no-op
		must(t, s.EndAwake(ctx, "u1", t0.Add(10*time.Minute)))
		must(t, s.EndAwake(ctx, "u1", t0.Add(11*time.Minute))) // nothing open: no-op
		must(t, s.StartAwake(ctx, "u1", t0.Add(20*time.Minute)))
		// u2: [t0+5m, t0+6m)
		must(t, s.StartAwake(ctx, "u2", t0.Add(5*time.Minute)))
		must(t, s.EndAwake(ctx, "u2", t0.Add(6*time.Minute)))

		now := t0.Add(30 * time.Minute)
		// Whole window: u1 = 10m + 10m (open, up to now), u2 = 1m.
		got, err := s.Usage(ctx, "", t0, t0.Add(time.Hour), now)
		must(t, err)
		want := []Usage{{UserID: "u1", AwakeSeconds: 1200}, {UserID: "u2", AwakeSeconds: 60}}
		if !slices.Equal(got, want) {
			t.Fatalf("usage all: got %+v want %+v", got, want)
		}
		// Clipped window [t0+5m, t0+25m): u1 = 5m + 5m, u2 = 1m.
		got, _ = s.Usage(ctx, "", t0.Add(5*time.Minute), t0.Add(25*time.Minute), now)
		want = []Usage{{UserID: "u1", AwakeSeconds: 600}, {UserID: "u2", AwakeSeconds: 60}}
		if !slices.Equal(got, want) {
			t.Fatalf("usage clipped: got %+v want %+v", got, want)
		}
		// One user only.
		got, _ = s.Usage(ctx, "u2", t0, t0.Add(time.Hour), now)
		if !slices.Equal(got, []Usage{{UserID: "u2", AwakeSeconds: 60}}) {
			t.Fatalf("usage u2: %+v", got)
		}
		// Window with nothing in it.
		got, _ = s.Usage(ctx, "u2", t0.Add(7*time.Minute), t0.Add(8*time.Minute), now)
		if len(got) != 0 {
			t.Fatalf("expected no usage, got %+v", got)
		}
		// Consecutive windows add up exactly (no double counting).
		a, _ := s.Usage(ctx, "u1", t0, t0.Add(15*time.Minute), now)
		b, _ := s.Usage(ctx, "u1", t0.Add(15*time.Minute), t0.Add(time.Hour), now)
		if a[0].AwakeSeconds+b[0].AwakeSeconds != 1200 {
			t.Fatalf("windows must add up: %+v + %+v", a, b)
		}
		// Periods survive sandbox deletion.
		must(t, s.Delete(ctx, "u2"))
		got, _ = s.Usage(ctx, "u2", t0, t0.Add(time.Hour), now)
		if len(got) != 1 {
			t.Fatalf("usage must survive delete: %+v", got)
		}
	})

	t.Run("browser seconds", func(t *testing.T) {
		s := newStore(t)
		// u1 is awake [t0, t0+10m). Its container reports browser time twice in that period.
		must(t, s.StartAwake(ctx, "u1", t0))
		add := func(user, report string, seconds int64, at time.Time, want bool) {
			t.Helper()
			stored, err := s.AddBrowserSeconds(ctx, user, report, seconds, at)
			must(t, err)
			if stored != want {
				t.Fatalf("report %s/%s: stored=%v, want %v", user, report, stored, want)
			}
		}
		add("u1", "d1#1", 42, t0.Add(2*time.Minute), true)
		add("u1", "d2#1", 100, t0.Add(8*time.Minute), true)
		// The same report again (a retried delivery, a second listener): stored once.
		add("u1", "d1#1", 42, t0.Add(9*time.Minute), false)
		add("u1", "d1#1", 999, t0.Add(9*time.Minute), false)
		// Another user may use the same report id.
		add("u2", "d1#1", 7, t0.Add(3*time.Minute), true)
		must(t, s.EndAwake(ctx, "u1", t0.Add(10*time.Minute)))
		if _, err := s.AddBrowserSeconds(ctx, "u1", "zero", 0, t0); err == nil {
			t.Fatal("a report of 0 seconds must be refused: nothing is counted when the browser never started")
		}
		if _, err := s.AddBrowserSeconds(ctx, "u1", "neg", -5, t0); err == nil {
			t.Fatal("a negative report must be refused")
		}

		now := t0.Add(time.Hour)
		got, err := s.Usage(ctx, "", t0, now, now)
		must(t, err)
		// u2 has browser time and no awake period: it still gets a row.
		want := []Usage{{UserID: "u1", AwakeSeconds: 600, BrowserSeconds: 142}, {UserID: "u2", BrowserSeconds: 7}}
		if !slices.Equal(got, want) {
			t.Fatalf("usage: got %+v want %+v", got, want)
		}
		// A report counts in the window that holds the moment it was recorded: [since, until).
		got, _ = s.Usage(ctx, "u1", t0, t0.Add(2*time.Minute), now)
		if len(got) != 1 || got[0].BrowserSeconds != 0 || got[0].AwakeSeconds != 120 {
			t.Fatalf("the report at t0+2m is not in [t0, t0+2m): %+v", got)
		}
		got, _ = s.Usage(ctx, "u1", t0.Add(2*time.Minute), t0.Add(8*time.Minute), now)
		if len(got) != 1 || got[0].BrowserSeconds != 42 {
			t.Fatalf("the report at t0+2m is in [t0+2m, t0+8m): %+v", got)
		}
		// Back-to-back windows return every second exactly once, wherever the cut falls.
		for _, cut := range []time.Duration{time.Minute, 2 * time.Minute, 5 * time.Minute, 8 * time.Minute, 9 * time.Minute} {
			a, _ := s.Usage(ctx, "u1", t0, t0.Add(cut), now)
			b, _ := s.Usage(ctx, "u1", t0.Add(cut), now, now)
			var sum int64
			for _, u := range append(a, b...) {
				sum += u.BrowserSeconds
			}
			if sum != 142 {
				t.Fatalf("cut at %s: windows must add up to 142, got %d (%+v + %+v)", cut, sum, a, b)
			}
		}
		// Browser time alone, after the awake period: a row with no awake seconds.
		add("u1", "d3#1", 30, t0.Add(20*time.Minute), true)
		got, _ = s.Usage(ctx, "u1", t0.Add(15*time.Minute), t0.Add(25*time.Minute), now)
		if !slices.Equal(got, []Usage{{UserID: "u1", BrowserSeconds: 30}}) {
			t.Fatalf("browser time outside an awake period: %+v", got)
		}
		// Nothing was reported: nothing is returned.
		must(t, s.StartAwake(ctx, "u3", t0))
		must(t, s.EndAwake(ctx, "u3", t0.Add(time.Minute)))
		got, _ = s.Usage(ctx, "u3", t0, now, now)
		if !slices.Equal(got, []Usage{{UserID: "u3", AwakeSeconds: 60}}) {
			t.Fatalf("no browser use: %+v", got)
		}
		// Reports survive sandbox deletion, like awake periods.
		must(t, s.Insert(ctx, newSandbox("u2")))
		must(t, s.Delete(ctx, "u2"))
		got, _ = s.Usage(ctx, "u2", t0, now, now)
		if !slices.Equal(got, []Usage{{UserID: "u2", BrowserSeconds: 7}}) {
			t.Fatalf("usage must survive delete: %+v", got)
		}
	})
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

func TestMemoryStore(t *testing.T) {
	runSuite(t, func(t *testing.T) Store { return NewMemory() })
}
