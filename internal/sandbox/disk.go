package sandbox

import (
	"context"
	"fmt"
	"syscall"

	"github.com/personal-agent/vm-manager/internal/ids"
)

// checkDiskLocked runs before a sleeping container is woken (the user lock is held).
//  1. Server guard: with the disk over DiskGuardPercent, nothing wakes, so a full disk cannot take
//     every user down. 2. User limit: a folder over UserDiskLimitBytes is not woken.
//
// Containers already awake are never touched. A measuring failure lets the wake go on (logged),
// because a broken measurement must not lock everyone out.
func (m *Manager) checkDiskLocked(ctx context.Context, userID string) error {
	if m.cfg.DiskGuardPercent > 0 {
		pct, err := m.diskUsedPercent(m.cfg.DiskGuardPath)
		switch {
		case err != nil:
			m.log.Warn("disk guard: could not measure the server disk", "err", err)
		case pct >= m.cfg.DiskGuardPercent:
			m.log.Error("DISK NEARLY FULL: refusing to wake sandboxes; free space on the server now",
				"user_id", userID, "used_percent", pct, "guard_percent", m.cfg.DiskGuardPercent, "alert", true)
			return fmt.Errorf("%w: %.0f%% used", ErrDiskFull, pct)
		}
	}
	if m.cfg.UserDiskLimitBytes > 0 {
		size, err := m.eng.VolumeSize(ctx, ids.VolumeName(userID))
		switch {
		case err != nil:
			m.log.Warn("disk limit: could not measure the user's folder", "user_id", userID, "err", err)
		case size > m.cfg.UserDiskLimitBytes:
			m.log.Error("user is over the storage limit: not waking", "user_id", userID,
				"used_bytes", size, "limit_bytes", m.cfg.UserDiskLimitBytes, "alert", true)
			return fmt.Errorf("%w: %d MB used, limit %d MB", ErrStorageLimit,
				size>>20, m.cfg.UserDiskLimitBytes>>20)
		}
	}
	return nil
}

// diskUsedPercent is how full the disk holding path is, like df's Use%.
func diskUsedPercent(path string) (float64, error) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(path, &st); err != nil {
		return 0, err
	}
	total := float64(st.Blocks) * float64(st.Bsize)
	if total == 0 {
		return 0, fmt.Errorf("statfs %s: zero size", path)
	}
	used := total - float64(st.Bfree)*float64(st.Bsize)
	return used / (used + float64(st.Bavail)*float64(st.Bsize)) * 100, nil
}
