package sandbox

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/personal-agent/vm-manager/internal/config"
	"github.com/personal-agent/vm-manager/internal/docker"
	"github.com/personal-agent/vm-manager/internal/ids"
	"github.com/personal-agent/vm-manager/internal/store"
)

// Done when: stopped after 3 idle minutes and wakes again with memory intact.
func TestIdleReaperStopsAfterThreeMinutesAndWakeKeepsMemory(t *testing.T) {
	e := newEnv(t)
	sb, _ := e.create(t, "u1")
	e.wake(t, "u1")
	// Hermes writes memory into /data while awake (simulated).
	vol, _ := e.eng.Volume(ids.VolumeName("u1"))
	vol.Files["hermes/memories/MEMORY.md"] = []byte("user likes tea")

	e.clk.Advance(2*time.Minute + 59*time.Second)
	e.m.ReapOnce(context.Background())
	if e.state(t, "u1") != store.StateAwake {
		t.Fatal("must not stop before 3 idle minutes")
	}
	e.clk.Advance(time.Second)
	e.m.ReapOnce(context.Background())
	if e.state(t, "u1") != store.StateStopped {
		t.Fatal("must stop after 3 idle minutes")
	}
	if e.br.ShutdownCount() != 1 {
		t.Fatal("idle stop must be polite (/shutdown first)")
	}
	c, _ := e.eng.Get(sb.ContainerID)
	if c.Running {
		t.Fatal("container must be stopped")
	}
	if _, err := os.Stat(filepath.Join(e.dir, "u1.conf")); !os.IsNotExist(err) {
		t.Fatal("egress file must be removed on stop")
	}
	usage, _ := e.m.Usage(context.Background(), "u1", t0, t0.Add(time.Hour))
	if usage[0].AwakeSeconds != 180 {
		t.Fatalf("awake period must be closed at the stop: %+v", usage)
	}

	// Wake again: same container, same volume, memory intact.
	woke := e.wake(t, "u1")
	if woke.ContainerID != sb.ContainerID {
		t.Fatal("idle stop must keep the container")
	}
	if b, ok := e.eng.File(ids.VolumeName("u1"), "hermes/memories/MEMORY.md"); !ok || string(b) != "user likes tea" {
		t.Fatal("memory in /data must survive stop + wake")
	}
}

func TestTouchPostponesIdleStop(t *testing.T) {
	e := newEnv(t)
	e.create(t, "u1")
	e.wake(t, "u1")
	e.clk.Advance(2 * time.Minute)
	if err := e.m.Touch(context.Background(), "u1"); err != nil {
		t.Fatal(err)
	}
	e.clk.Advance(2 * time.Minute)
	e.m.ReapOnce(context.Background())
	if e.state(t, "u1") != store.StateAwake {
		t.Fatal("touch must reset the idle timer")
	}
}

// Never stop a container in the middle of a turn.
func TestReaperNeverStopsMidTurn(t *testing.T) {
	e := newEnv(t)
	e.create(t, "u1")
	e.br.Block = make(chan struct{})
	done := make(chan struct{})
	go func() { _, _ = collect(t, e.m, deliverReq("u1", "long")); close(done) }()
	waitFor(t, func() bool { return e.state(t, "u1") == store.StateBusy })
	e.clk.Advance(10 * time.Minute)
	e.m.ReapOnce(context.Background())
	if e.state(t, "u1") != store.StateBusy || e.eng.RunningCount() != 1 {
		t.Fatal("reaper stopped a busy sandbox")
	}
	close(e.br.Block)
	<-done

	// Awake in the table, but the Bridge says Hermes is still working (e.g. after a vm-manager restart).
	e.br.SetBusy("u1", 1)
	e.clk.Advance(10 * time.Minute)
	e.m.ReapOnce(context.Background())
	if e.state(t, "u1") != store.StateAwake {
		t.Fatal("reaper must respect the Bridge's busy count")
	}
}

// Done when: the awake cap is respected.
func TestCapacityGuardEvictsLongestIdleLowestPriority(t *testing.T) {
	e := newEnv(t, func(c *config.Config) { c.AwakeCap = 2 })
	e.create(t, "old")
	e.create(t, "newer")
	e.create(t, "third")
	e.wake(t, "old")
	e.clk.Advance(time.Minute)
	e.wake(t, "newer")
	e.clk.Advance(time.Minute)

	e.wake(t, "third")
	if e.state(t, "old") != store.StateStopped || e.state(t, "newer") != store.StateAwake || e.eng.RunningCount() != 2 {
		t.Fatalf("longest idle must be evicted: old=%s newer=%s running=%d", e.state(t, "old"), e.state(t, "newer"), e.eng.RunningCount())
	}
	if e.br.ShutdownCount() != 1 {
		t.Fatal("eviction must be polite")
	}

	// Priority beats idleness: a LOW sandbox is evicted before an idler HIGH one.
	e2 := newEnv(t, func(c *config.Config) { c.AwakeCap = 2 })
	e2.create(t, "max", func(r *CreateRequest) { r.Priority = PriorityHigh })
	e2.create(t, "free", func(r *CreateRequest) { r.Priority = PriorityLow })
	e2.create(t, "pro", func(r *CreateRequest) { r.Priority = PriorityNormal })
	e2.wake(t, "max")
	e2.clk.Advance(time.Minute)
	e2.wake(t, "free")
	e2.clk.Advance(time.Minute)
	e2.wake(t, "pro")
	if e2.state(t, "free") != store.StateStopped || e2.state(t, "max") != store.StateAwake {
		t.Fatalf("lowest priority first: free=%s max=%s", e2.state(t, "free"), e2.state(t, "max"))
	}
}

func TestCapacityGuardNothingEvictable(t *testing.T) {
	e := newEnv(t, func(c *config.Config) { c.AwakeCap = 1 })
	e.create(t, "a")
	e.create(t, "b")
	e.br.Block = make(chan struct{})
	done := make(chan struct{})
	go func() { _, _ = collect(t, e.m, deliverReq("a", "d1")); close(done) }()
	waitFor(t, func() bool { return e.state(t, "a") == store.StateBusy })

	if _, _, err := e.m.Wake(context.Background(), "b", 0); !errors.Is(err, ErrCapacity) {
		t.Fatalf("want ErrCapacity, got %v", err)
	}
	if e.state(t, "b") != store.StateStopped || e.state(t, "a") != store.StateBusy {
		t.Fatal("a failed wake must leave both untouched")
	}
	close(e.br.Block)
	<-done
	// Now a is idle-awake and can be evicted.
	e.wake(t, "b")
	if e.state(t, "a") != store.StateStopped {
		t.Fatal("a should have been evicted")
	}
}

func TestConcurrentWakesNeverExceedCap(t *testing.T) {
	const capN, users = 3, 12
	e := newEnv(t, func(c *config.Config) { c.AwakeCap = capN })
	var running, maxRunning atomic.Int32
	e.eng.OnStart = func(docker.Container) {
		n := running.Add(1)
		for {
			m := maxRunning.Load()
			if n <= m || maxRunning.CompareAndSwap(m, n) {
				break
			}
		}
	}
	e.eng.OnStop = func(docker.Container) { running.Add(-1) }
	for i := range users {
		e.create(t, fmt.Sprintf("u%d", i))
	}
	var wg sync.WaitGroup
	var ok, full atomic.Int32
	for i := range users {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, _, err := e.m.Wake(context.Background(), fmt.Sprintf("u%d", i), 0)
			switch {
			case err == nil:
				ok.Add(1)
			case errors.Is(err, ErrCapacity):
				full.Add(1)
			default:
				t.Errorf("wake u%d: %v", i, err)
			}
		}()
	}
	wg.Wait()
	if maxRunning.Load() > capN {
		t.Fatalf("cap exceeded: %d containers ran at once (cap %d)", maxRunning.Load(), capN)
	}
	if e.eng.RunningCount() > capN {
		t.Fatalf("running now: %d", e.eng.RunningCount())
	}
	if ok.Load()+full.Load() != users || ok.Load() < capN {
		t.Fatalf("ok=%d full=%d", ok.Load(), full.Load())
	}
}

func TestHealthCheckerRestartsThenMarksBroken(t *testing.T) {
	e := newEnv(t, func(c *config.Config) { c.HealthFailuresBeforeRestart = 2; c.HealthMaxRestarts = 1 })
	sb, _ := e.create(t, "u1")
	e.wake(t, "u1")
	starts := func() int { c, _ := e.eng.Get(sb.ContainerID); return c.Starts }

	e.m.HealthOnce(context.Background()) // healthy: nothing
	if starts() != 1 {
		t.Fatal("healthy container must not restart")
	}
	// Bridge stops answering, then recovers after the restart.
	e.br.SetUnhealthy("u1", true)
	e.m.HealthOnce(context.Background()) // 1st failure
	if starts() != 1 {
		t.Fatal("one failure is not enough to restart")
	}
	e.eng.OnStart = func(docker.Container) { e.br.SetUnhealthy("u1", false) }
	e.m.HealthOnce(context.Background()) // 2nd failure -> restart
	if starts() != 2 || e.state(t, "u1") != store.StateAwake {
		t.Fatalf("restart expected: starts=%d state=%s", starts(), e.state(t, "u1"))
	}
	e.eng.OnStart = nil

	// Keeps failing: restart budget exhausted -> broken.
	e.br.SetUnhealthy("u1", true)
	e.m.HealthOnce(context.Background())
	e.m.HealthOnce(context.Background()) // restart #2 > max 1 -> broken
	if e.state(t, "u1") != store.StateBroken {
		t.Fatalf("state = %s, want broken", e.state(t, "u1"))
	}
	c, _ := e.eng.Get(sb.ContainerID)
	if c.Running {
		t.Fatal("broken container must be stopped")
	}
}

func TestHealthCheckerFailedRestartStopsAndClosesPeriod(t *testing.T) {
	e := newEnv(t, func(c *config.Config) { c.HealthFailuresBeforeRestart = 1; c.HealthMaxRestarts = 3 })
	e.create(t, "u1")
	e.wake(t, "u1")
	e.br.SetUnhealthy("u1", true) // stays unhealthy after the restart too
	e.m.HealthOnce(context.Background())
	if e.state(t, "u1") != store.StateStopped || e.eng.RunningCount() != 0 {
		t.Fatalf("failed restart: state=%s running=%d", e.state(t, "u1"), e.eng.RunningCount())
	}
	u1, _ := e.m.Usage(context.Background(), "u1", t0, t0.Add(time.Hour))
	e.clk.Advance(time.Hour)
	u2, _ := e.m.Usage(context.Background(), "u1", t0, t0.Add(2*time.Hour))
	if u1[0].AwakeSeconds != u2[0].AwakeSeconds {
		t.Fatal("awake period must be closed after a failed restart")
	}
}

func TestHealthCheckerSkipsBusy(t *testing.T) {
	e := newEnv(t, func(c *config.Config) { c.HealthFailuresBeforeRestart = 1 })
	sb, _ := e.create(t, "u1")
	e.br.Block = make(chan struct{})
	done := make(chan struct{})
	go func() { _, _ = collect(t, e.m, deliverReq("u1", "d1")); close(done) }()
	waitFor(t, func() bool { return e.state(t, "u1") == store.StateBusy })
	e.br.SetUnhealthy("u1", true)
	e.m.HealthOnce(context.Background())
	if c, _ := e.eng.Get(sb.ContainerID); c.Starts != 1 {
		t.Fatal("must not restart mid-turn")
	}
	close(e.br.Block)
	<-done
}

func TestReconcilerFixesDrift(t *testing.T) {
	e := newEnv(t)
	ctx := context.Background()
	crashed, _ := e.create(t, "crashed")
	e.wake(t, "crashed")
	e.create(t, "rogue")
	e.create(t, "stale")
	e.wake(t, "stale")
	e.clk.Advance(time.Minute)

	// 1. "crashed": table awake, container died.
	e.eng.Crash(crashed.ContainerID)
	// 2. "rogue": table stopped, container running (started behind our back).
	rogue, _ := e.st.Get(ctx, "rogue")
	_ = e.eng.StartContainer(ctx, rogue.ContainerID)
	// 3. "stale": busy left over from a vm-manager restart (nothing in flight).
	_ = e.st.SetState(ctx, "stale", store.StateBusy, e.clk.Now())
	// 4. an orphan managed container with no row.
	orphan, _ := e.eng.CreateContainer(ctx, docker.ContainerSpec{Name: "pa-sbx-ghost", VolumeName: "pa-data-ghost", VolumeTarget: "/data",
		Labels: map[string]string{LabelManaged: "true", LabelUserID: "ghost"}})
	// 5. a leftover egress file for a user that is not awake.
	_ = e.eg.Write("rogue", "172.30.9.9", nil)

	e.restart()
	e.m.ReconcileOnce(ctx)

	if e.state(t, "crashed") != store.StateStopped {
		t.Fatalf("crashed: %s", e.state(t, "crashed"))
	}
	usage, _ := e.m.Usage(ctx, "crashed", t0, t0.Add(time.Hour))
	e.clk.Advance(time.Hour)
	usage2, _ := e.m.Usage(ctx, "crashed", t0, t0.Add(2*time.Hour))
	if usage[0].AwakeSeconds != usage2[0].AwakeSeconds {
		t.Fatal("crashed sandbox's awake period must be closed")
	}
	if c, _ := e.eng.Get(rogue.ContainerID); c.Running || e.state(t, "rogue") != store.StateStopped {
		t.Fatal("rogue container must be stopped")
	}
	if e.state(t, "stale") != store.StateAwake {
		t.Fatalf("stale busy must settle to awake, got %s", e.state(t, "stale"))
	}
	if _, ok := e.eng.Get(orphan); ok {
		t.Fatal("orphan container must be removed")
	}
	if _, ok := e.eng.Volume("pa-data-ghost"); !ok {
		t.Fatal("the reconciler must never delete volumes")
	}
	if _, err := os.Stat(filepath.Join(e.dir, "rogue.conf")); !os.IsNotExist(err) {
		t.Fatal("egress file of a stopped user must be removed")
	}
	if _, err := os.Stat(filepath.Join(e.dir, "stale.conf")); err != nil {
		t.Fatal("egress file of an awake user must exist")
	}
}

func TestWakeWhenTableSaysAwakeButContainerDied(t *testing.T) {
	e := newEnv(t)
	sb, _ := e.create(t, "u1")
	e.wake(t, "u1")
	e.eng.Crash(sb.ContainerID)
	woke, was, err := e.m.Wake(context.Background(), "u1", 0)
	if err != nil || !was || woke.State != store.StateAwake {
		t.Fatalf("wake after crash: %+v %v %v", woke, was, err)
	}
	if c, _ := e.eng.Get(sb.ContainerID); !c.Running {
		t.Fatal("container must be running again")
	}
}
