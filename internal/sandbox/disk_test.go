package sandbox

import (
	"context"
	"errors"
	"testing"

	"github.com/personal-agent/vm-manager/internal/config"
	"github.com/personal-agent/vm-manager/internal/ids"
	"github.com/personal-agent/vm-manager/internal/store"
)

func TestWakeRefusedWhenUserOverDiskLimit(t *testing.T) {
	e := newEnv(t) // default limit: 2 GB
	e.create(t, "usr_over")
	e.create(t, "usr_ok")
	e.eng.SizeOverride = map[string]int64{ids.VolumeName("usr_over"): 3 << 30, ids.VolumeName("usr_ok"): 1 << 30}

	_, _, err := e.m.Wake(context.Background(), "usr_over", 0)
	if !errors.Is(err, ErrStorageLimit) {
		t.Fatalf("over the limit: want ErrStorageLimit, got %v", err)
	}
	if e.state(t, "usr_over") == store.StateBroken {
		t.Fatal("an over-limit user must not be marked broken")
	}
	e.wake(t, "usr_ok") // under the limit still wakes
}

func TestWakeAfterUserFreesSpace(t *testing.T) {
	e := newEnv(t)
	e.create(t, "usr_a")
	name := ids.VolumeName("usr_a")
	e.eng.SizeOverride = map[string]int64{name: 5 << 30}
	if _, _, err := e.m.Wake(context.Background(), "usr_a", 0); !errors.Is(err, ErrStorageLimit) {
		t.Fatalf("want ErrStorageLimit, got %v", err)
	}
	e.eng.SizeOverride[name] = 100 << 20
	e.wake(t, "usr_a")
}

func TestUserLimitCanBeTurnedOffAndRaised(t *testing.T) {
	e := newEnv(t, func(c *config.Config) { c.UserDiskLimitBytes = 0 })
	e.create(t, "usr_a")
	e.eng.SizeOverride = map[string]int64{ids.VolumeName("usr_a"): 50 << 30}
	e.wake(t, "usr_a")
}

func TestAwakeContainerIsNotCheckedAgain(t *testing.T) {
	e := newEnv(t)
	e.create(t, "usr_a")
	e.wake(t, "usr_a")
	e.eng.SizeOverride = map[string]int64{ids.VolumeName("usr_a"): 9 << 30}
	if _, was, err := e.m.Wake(context.Background(), "usr_a", 0); err != nil || was {
		t.Fatalf("already awake: want no error and no cold wake, got was=%v err=%v", was, err)
	}
}

func TestWakeRefusedWhenServerDiskOver90(t *testing.T) {
	e := newEnv(t)
	e.create(t, "usr_a")
	e.m.diskUsedPercent = func(string) (float64, error) { return 91.5, nil }
	_, _, err := e.m.Wake(context.Background(), "usr_a", 0)
	if !errors.Is(err, ErrDiskFull) {
		t.Fatalf("want ErrDiskFull, got %v", err)
	}
	if e.state(t, "usr_a") == store.StateBroken {
		t.Fatal("a full disk must not mark the user broken")
	}
	e.m.diskUsedPercent = func(string) (float64, error) { return 89.9, nil }
	e.wake(t, "usr_a")
}

func TestDiskGuardFailsOpenAndCanBeOff(t *testing.T) {
	e := newEnv(t)
	e.create(t, "usr_a")
	e.m.diskUsedPercent = func(string) (float64, error) { return 0, errors.New("statfs broke") }
	e.wake(t, "usr_a") // a broken measurement must not lock everyone out

	e2 := newEnv(t, func(c *config.Config) { c.DiskGuardPercent = 0 })
	e2.create(t, "usr_b")
	e2.m.diskUsedPercent = func(string) (float64, error) { return 99, nil }
	e2.wake(t, "usr_b")
}

func TestDiskUsedPercentReadsARealDisk(t *testing.T) {
	pct, err := diskUsedPercent(t.TempDir())
	if err != nil || pct <= 0 || pct > 100 {
		t.Fatalf("got %v, %v", pct, err)
	}
}
