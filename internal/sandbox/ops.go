package sandbox

import (
	"compress/gzip"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/personal-agent/vm-manager/internal/egress"
	"github.com/personal-agent/vm-manager/internal/platformfile"
	"github.com/personal-agent/vm-manager/internal/store"
)

// Info is a sandbox plus live resource use.
type Info struct {
	store.Sandbox
	MemoryBytes int64
}

// Touch refreshes last-active (every delivered message and Platform MCP call).
func (m *Manager) Touch(ctx context.Context, userID string) error {
	if err := validateUserID(userID); err != nil {
		return err
	}
	err := m.store.Touch(ctx, userID, m.clk.Now())
	if errors.Is(err, store.ErrNotFound) {
		return ErrNotFound
	}
	return err
}

func (m *Manager) withMemory(ctx context.Context, sb store.Sandbox) Info {
	info := Info{Sandbox: sb}
	if sb.State.Running() && sb.ContainerID != "" {
		mctx, cancel := context.WithTimeout(ctx, 3*time.Second)
		defer cancel()
		if mem, err := m.eng.MemoryUsage(mctx, sb.ContainerID); err == nil {
			info.MemoryBytes = mem
		}
	}
	return info
}

// Get returns one sandbox with its current memory use.
func (m *Manager) Get(ctx context.Context, userID string) (Info, error) {
	if err := validateUserID(userID); err != nil {
		return Info{}, err
	}
	sb, err := m.getSandbox(ctx, userID)
	if err != nil {
		return Info{}, err
	}
	return m.withMemory(ctx, sb), nil
}

// List returns sandboxes in the given states (all when empty).
func (m *Manager) List(ctx context.Context, states ...store.State) ([]Info, error) {
	for _, s := range states {
		if !s.Valid() {
			return nil, invalid("unknown state %q", s)
		}
	}
	list, err := m.store.List(ctx, states...)
	if err != nil {
		return nil, err
	}
	out := make([]Info, 0, len(list))
	for _, sb := range list {
		out = append(out, m.withMemory(ctx, sb))
	}
	return out, nil
}

// Usage returns awake seconds per user in [since, until). until zero = now.
func (m *Manager) Usage(ctx context.Context, userID string, since, until time.Time) ([]store.Usage, error) {
	if userID != "" {
		if err := validateUserID(userID); err != nil {
			return nil, err
		}
	}
	now := m.clk.Now()
	if until.IsZero() {
		until = now
	}
	if !since.Before(until) {
		return nil, invalid("since must be before until")
	}
	return m.store.Usage(ctx, userID, since, until, now)
}

// SetEgressAllowlist replaces the user's allowed domains (applied at once if
// awake) and MCP servers (written into /data now, used by Hermes on the next wake).
// The lists are full replacements.
func (m *Manager) SetEgressAllowlist(ctx context.Context, userID string, domains []string, servers []MCPServer) error {
	if err := validateUserID(userID); err != nil {
		return err
	}
	allow, err := egress.NormalizeDomains(domains)
	if err != nil {
		return invalid("domains: %v", err)
	}
	if err := validateMCPServers(servers); err != nil {
		return err
	}
	l := m.userLock(userID)
	l.Lock()
	defer l.Unlock()
	ctx, cancel := detached(ctx, time.Minute)
	defer cancel()
	sb, err := m.getSandbox(ctx, userID)
	if err != nil {
		return err
	}
	if err := m.store.SetAllowlist(ctx, userID, allow, m.clk.Now()); err != nil {
		return err
	}
	sb.Allowlist = allow

	// Update the MCP servers in the secrets file. A missing container is re-created
	// (stopped) first so the file can be written; running containers are left as they are.
	if !sb.State.Running() {
		if err := m.ensureContainerLocked(ctx, &sb); err != nil {
			return err
		}
	}
	f, err := m.loadPlatformFile(ctx, userID, sb.ContainerID, sb.MCPTokenHash, sb.BridgeSecretHash)
	if err != nil {
		return err
	}
	f.MCPServers = servers
	if err := platformfile.Write(ctx, m.eng, sb.ContainerID, m.cfg.AgentUID, f); err != nil {
		return err
	}

	if sb.State.Running() {
		c, err := m.eng.InspectContainer(ctx, sb.ContainerID)
		if err == nil && c.Running && c.IPs[m.cfg.SandboxNetwork] != "" {
			if err := m.eg.Write(userID, c.IPs[m.cfg.SandboxNetwork], allow); err != nil {
				return err
			}
		}
	}
	m.log.Info("egress allowlist updated", "user_id", userID, "domains", len(allow), "mcp_servers", len(servers))
	return nil
}

// Snapshot writes a tar.gz of the user's /data volume under destination
// (which must be inside SNAPSHOT_ROOT) and returns the file path and size.
// It streams the volume out through the Docker archive API (works whether the
// container is running or stopped); no helper container is needed.
func (m *Manager) Snapshot(ctx context.Context, userID, destination string) (string, int64, error) {
	if err := validateUserID(userID); err != nil {
		return "", 0, err
	}
	dest, err := m.snapshotDir(destination)
	if err != nil {
		return "", 0, err
	}
	l := m.userLock(userID)
	l.Lock()
	defer l.Unlock()
	ctx, cancel := detached(ctx, 30*time.Minute)
	defer cancel()
	sb, err := m.getSandbox(ctx, userID)
	if err != nil {
		return "", 0, err
	}
	if !sb.State.Running() {
		if err := m.ensureContainerLocked(ctx, &sb); err != nil {
			return "", 0, err
		}
	}
	if err := os.MkdirAll(dest, 0o700); err != nil {
		return "", 0, fmt.Errorf("snapshot dir: %w", err)
	}
	name := fmt.Sprintf("%s-%s.tar.gz", userID, m.clk.Now().Format("20060102T150405Z"))
	final := filepath.Join(dest, name)

	rc, err := m.eng.CopyFrom(ctx, sb.ContainerID, platformfile.DataDir)
	if err != nil {
		return "", 0, err
	}
	defer func() { _ = rc.Close() }()
	tmp, err := os.CreateTemp(dest, ".tmp-"+name+"-*")
	if err != nil {
		return "", 0, fmt.Errorf("snapshot: %w", err)
	}
	defer func() { _ = os.Remove(tmp.Name()) }() // no-op after the rename
	gz := gzip.NewWriter(tmp)
	if _, err := io.Copy(gz, rc); err != nil {
		_ = tmp.Close()
		return "", 0, fmt.Errorf("snapshot copy: %w", err)
	}
	if err := gz.Close(); err != nil {
		_ = tmp.Close()
		return "", 0, err
	}
	if err := tmp.Chmod(0o600); err != nil {
		_ = tmp.Close()
		return "", 0, err
	}
	if err := tmp.Close(); err != nil {
		return "", 0, err
	}
	if err := os.Rename(tmp.Name(), final); err != nil {
		return "", 0, fmt.Errorf("snapshot: %w", err)
	}
	st, err := os.Stat(final)
	if err != nil {
		return "", 0, err
	}
	m.log.Info("volume snapshot written", "user_id", userID, "location", final, "size_bytes", st.Size())
	return final, st.Size(), nil
}

// snapshotDir resolves destination inside SNAPSHOT_ROOT. A relative destination
// is taken relative to the root; anything escaping the root is rejected.
func (m *Manager) snapshotDir(destination string) (string, error) {
	root := filepath.Clean(m.cfg.SnapshotRoot)
	if destination == "" {
		return root, nil
	}
	d := destination
	if !filepath.IsAbs(d) {
		d = filepath.Join(root, d)
	}
	d = filepath.Clean(d)
	rel, err := filepath.Rel(root, d)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", invalid("destination must be inside %s", root)
	}
	return d, nil
}
