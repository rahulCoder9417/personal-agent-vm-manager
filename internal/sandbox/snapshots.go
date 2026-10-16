package sandbox

import (
	"context"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"time"

	"github.com/personal-agent/vm-manager/internal/ids"
)

// archiveRe matches the archives Snapshot writes: <user id>-<UTC time>.tar.gz.
var archiveRe = regexp.MustCompile(`^(.+)-(\d{8}T\d{6}Z)\.tar\.gz$`)

const archiveTimeLayout = "20060102T150405Z"

// parseArchiveName returns the user id and the time of an archive file name, or ok=false for any
// other file (the backend's database dumps share the same folder on the host).
func parseArchiveName(name string) (userID string, at time.Time, ok bool) {
	m := archiveRe.FindStringSubmatch(name)
	if m == nil || !ids.ValidUserID(m[1]) {
		return "", time.Time{}, false
	}
	at, err := time.Parse(archiveTimeLayout, m[2])
	if err != nil {
		return "", time.Time{}, false
	}
	return m[1], at, true
}

// removeArchives walks SNAPSHOT_ROOT and removes every archive for which drop is true, then the
// folders that this left empty (never the root itself). A missing root is nothing to do. Safe to
// run again: a file already gone is skipped.
func (m *Manager) removeArchives(drop func(userID string, at time.Time) bool) (int, error) {
	root := filepath.Clean(m.cfg.SnapshotRoot)
	removed := 0
	emptied := map[string]bool{}
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			if errors.Is(err, fs.ErrNotExist) {
				return nil
			}
			return err
		}
		if d.IsDir() {
			return nil
		}
		userID, at, ok := parseArchiveName(d.Name())
		if !ok || !drop(userID, at) {
			return nil
		}
		if err := os.Remove(path); err != nil && !errors.Is(err, fs.ErrNotExist) {
			return err
		}
		removed++
		emptied[filepath.Dir(path)] = true
		return nil
	})
	for dir := range emptied {
		if dir != root {
			_ = os.Remove(dir) // fails, harmlessly, while the folder still holds other files
		}
	}
	return removed, err
}

// removeUserSnapshots deletes every archive of one user's /data (account deletion).
func (m *Manager) removeUserSnapshots(userID string) error {
	n, err := m.removeArchives(func(u string, _ time.Time) bool { return u == userID })
	if n > 0 {
		m.log.Info("volume snapshots deleted", "user_id", userID, "count", n)
	}
	return err
}

// PruneSnapshotsOnce deletes the archives older than SNAPSHOT_KEEP_DAYS, whoever they belong to,
// so no copy of a user's files is kept longer than that.
func (m *Manager) PruneSnapshotsOnce(context.Context) {
	cutoff := m.clk.Now().Add(-time.Duration(m.cfg.SnapshotKeepDays) * 24 * time.Hour)
	n, err := m.removeArchives(func(_ string, at time.Time) bool { return at.Before(cutoff) })
	if err != nil {
		m.log.Error("snapshot prune failed", "err", err, "alert", true)
	}
	if n > 0 {
		m.log.Info("old volume snapshots deleted", "count", n, "keep_days", m.cfg.SnapshotKeepDays)
	}
}
