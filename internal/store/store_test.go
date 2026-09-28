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
		if err := s.Touch(ctx, "u1", later); err != nil {
			t.Fatal(err)
		}
		if err := s.Touch(ctx, "u1", t0); err != nil { // older: must not move backwards
			t.Fatal(err)
		}
		got, _ := s.Get(ctx, "u1")
		if got.State != StateBusy || got.ContainerID != "c-new" || got.ImageVersion != "v2" || got.Priority != 3 ||
			!slices.Equal(got.Allowlist, []string{"a.com", ".b.org"}) || !got.LastActiveAt.Equal(later) {
			t.Fatalf("unexpected row: %+v", got)
		}
		if err := s.SetState(ctx, "nobody", StateAwake, later); !errors.Is(err, ErrNotFound) {
			t.Fatalf("want ErrNotFound, got %v", err)
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

func TestMemoryBrowserSeconds(t *testing.T) {
	ctx := context.Background()
	s := NewMemory()
	must(t, s.StartAwake(ctx, "u1", t0))
	s.AddBrowserSeconds("u1", 42)
	// Open period: browser seconds are counted only once it ends.
	got, _ := s.Usage(ctx, "u1", t0, t0.Add(time.Hour), t0.Add(time.Minute))
	if got[0].BrowserSeconds != 0 {
		t.Fatalf("open period browser seconds should wait: %+v", got)
	}
	must(t, s.EndAwake(ctx, "u1", t0.Add(2*time.Minute)))
	got, _ = s.Usage(ctx, "u1", t0, t0.Add(time.Hour), t0.Add(time.Hour))
	if got[0].BrowserSeconds != 42 || got[0].AwakeSeconds != 120 {
		t.Fatalf("usage: %+v", got)
	}
}
