package sandbox

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"time"

	"github.com/personal-agent/vm-manager/internal/bridge"
	"github.com/personal-agent/vm-manager/internal/docker"
	"github.com/personal-agent/vm-manager/internal/egress"
	"github.com/personal-agent/vm-manager/internal/ids"
	"github.com/personal-agent/vm-manager/internal/platformfile"
	"github.com/personal-agent/vm-manager/internal/store"
)

// CreateRequest is the input of Create.
type CreateRequest struct {
	UserID     string
	LiteLLMKey string
	Priority   int32
	Allowlist  []string
	MCPServers []MCPServer
	Timezone   string
	Runtime    store.Runtime // "" = hermes
	// BrowserEnabled turns Scout's browser on from the start (see SetBrowser).
	BrowserEnabled bool
}

// Create makes the container, its /data volume and its secrets, and leaves it
// stopped. Idempotent per user: a second call returns the existing sandbox.
// Returns the sandbox and the sha256 hex of the Platform MCP token.
func (m *Manager) Create(ctx context.Context, req CreateRequest) (store.Sandbox, string, error) {
	if err := validateUserID(req.UserID); err != nil {
		return store.Sandbox{}, "", err
	}
	if req.LiteLLMKey == "" {
		return store.Sandbox{}, "", invalid("litellm_key is required")
	}
	allow, err := egress.NormalizeDomains(req.Allowlist)
	if err != nil {
		return store.Sandbox{}, "", invalid("allowlist: %v", err)
	}
	if err := validateMCPServers(req.MCPServers); err != nil {
		return store.Sandbox{}, "", err
	}
	if len(req.Timezone) > 64 {
		return store.Sandbox{}, "", invalid("timezone too long")
	}
	if req.Runtime == "" {
		req.Runtime = store.RuntimeHermes
	}
	if !req.Runtime.Valid() {
		return store.Sandbox{}, "", invalid("runtime must be hermes or claude_code")
	}

	l := m.userLock(req.UserID)
	l.Lock()
	defer l.Unlock()
	ctx, cancel := detached(ctx, 2*time.Minute)
	defer cancel()

	if existing, err := m.store.Get(ctx, req.UserID); err == nil {
		return existing, existing.MCPTokenHash, nil
	} else if !errors.Is(err, store.ErrNotFound) {
		return store.Sandbox{}, "", err
	}

	token, err := ids.NewSecret()
	if err != nil {
		return store.Sandbox{}, "", err
	}
	secret, err := ids.NewSecret()
	if err != nil {
		return store.Sandbox{}, "", err
	}
	spec := m.spec(req.UserID, req.BrowserEnabled)
	if err := m.eng.EnsureVolume(ctx, spec.VolumeName, m.cfg.VolumeDriver, m.cfg.VolumeOpts,
		map[string]string{LabelManaged: "true", LabelUserID: req.UserID}); err != nil {
		return store.Sandbox{}, "", err
	}
	// A previous attempt may have crashed after creating the container but before the row.
	if err := m.eng.RemoveContainer(ctx, spec.Name); err != nil {
		return store.Sandbox{}, "", err
	}
	containerID, err := m.eng.CreateContainer(ctx, spec)
	if err != nil {
		return store.Sandbox{}, "", err
	}
	pf := platformfile.File{
		UserID: req.UserID, MCPToken: token, BridgeSecret: secret, LiteLLMKey: req.LiteLLMKey,
		Timezone: req.Timezone, MCPServers: req.MCPServers, Runtime: string(req.Runtime),
		BrowserEnabled: req.BrowserEnabled,
	}
	if err := platformfile.Write(ctx, m.eng, containerID, m.cfg.AgentUID, pf); err != nil {
		_ = m.eng.RemoveContainer(ctx, containerID)
		return store.Sandbox{}, "", err
	}
	now := m.clk.Now()
	sb := store.Sandbox{
		UserID: req.UserID, ContainerID: containerID, ImageVersion: m.cfg.AgentImageVersion,
		State: store.StateStopped, Priority: normPriority(req.Priority), LastActiveAt: now,
		MCPTokenHash: ids.Hash(token), BridgeSecretHash: ids.Hash(secret), Allowlist: allow,
		Runtime: req.Runtime, BrowserEnabled: req.BrowserEnabled, CreatedAt: now, UpdatedAt: now,
	}
	if err := m.store.Insert(ctx, sb); err != nil {
		_ = m.eng.RemoveContainer(ctx, containerID)
		return store.Sandbox{}, "", err
	}
	m.mu.Lock()
	m.secrets[req.UserID] = secret
	m.mu.Unlock()
	m.log.Info("sandbox created", "user_id", req.UserID, "container_id", containerID, "image_version", sb.ImageVersion,
		"runtime", string(sb.Runtime), "browser", sb.BrowserEnabled)
	return sb, sb.MCPTokenHash, nil
}

// Wake starts the container if needed and waits for its Bridge to be healthy.
// Returns ErrCapacity when the awake cap is reached and nothing can be evicted.
func (m *Manager) Wake(ctx context.Context, userID string, priority int32) (store.Sandbox, bool, error) {
	return m.wake(ctx, userID, priority, false)
}

// wake is Wake; forDeliver also counts a Deliver in flight before releasing the
// user lock, so the reaper / capacity guard can't stop the container in between.
func (m *Manager) wake(ctx context.Context, userID string, priority int32, forDeliver bool) (store.Sandbox, bool, error) {
	if err := validateUserID(userID); err != nil {
		return store.Sandbox{}, false, err
	}
	l := m.userLock(userID)
	l.Lock()
	defer l.Unlock()
	ctx, cancel := detached(ctx, m.cfg.WakeTimeout*3+m.cfg.ShutdownGrace+time.Minute)
	defer cancel()

	sb, err := m.getSandbox(ctx, userID)
	if err != nil {
		return store.Sandbox{}, false, err
	}
	if priority != 0 && normPriority(priority) != sb.Priority {
		if err := m.store.SetPriority(ctx, userID, normPriority(priority), m.clk.Now()); err != nil {
			return store.Sandbox{}, false, err
		}
		sb.Priority = normPriority(priority)
	}

	if sb.State == store.StateAwake || sb.State == store.StateBusy {
		c, err := m.eng.InspectContainer(ctx, sb.ContainerID)
		if err == nil && c.Running {
			if forDeliver {
				m.addInflight(userID, 1)
			}
			return sb, false, nil
		}
		// The table says awake but the container is gone or stopped (crash): treat as stopped.
		m.log.Warn("awake sandbox not running; restarting it", "user_id", userID)
		if err := m.markStoppedLocked(ctx, sb, store.StateStopped); err != nil {
			return store.Sandbox{}, false, err
		}
		sb.State = store.StateStopped
	}

	if err := m.reserveCapacity(ctx, sb); err != nil {
		return store.Sandbox{}, false, err
	}
	if err := m.startLocked(ctx, &sb); err != nil {
		m.log.Error("sandbox failed to wake; marked broken", "user_id", userID, "err", err, "alert", true)
		_ = m.store.SetState(ctx, userID, store.StateBroken, m.clk.Now())
		return store.Sandbox{}, false, fmt.Errorf("%w: %v", ErrUnavailable, err)
	}
	if forDeliver {
		m.addInflight(userID, 1)
	}
	sb, err = m.getSandbox(ctx, userID)
	return sb, true, err
}

// reserveCapacity marks self as waking if there is room under the awake cap,
// evicting the longest-idle, not-busy container (lowest priority first) when needed.
func (m *Manager) reserveCapacity(ctx context.Context, self store.Sandbox) error {
	for attempt := 0; attempt < 5; attempt++ {
		m.capMu.Lock()
		active, err := m.store.List(ctx, store.StateWaking, store.StateAwake, store.StateBusy)
		if err != nil {
			m.capMu.Unlock()
			return err
		}
		count := 0
		for _, a := range active {
			if a.UserID != self.UserID {
				count++
			}
		}
		if count < m.cfg.AwakeCap {
			err := m.store.SetState(ctx, self.UserID, store.StateWaking, m.clk.Now())
			m.capMu.Unlock()
			return err
		}
		victim, unlock := m.pickVictim(active, self.UserID)
		if unlock == nil {
			m.capMu.Unlock()
			m.log.Warn("awake cap reached and nothing is evictable", "user_id", self.UserID, "cap", m.cfg.AwakeCap)
			return ErrCapacity
		}
		// Reserve our slot now; the victim still counts until it is stopped, so
		// concurrent wakers pick other victims instead of overshooting the cap.
		if err := m.store.SetState(ctx, self.UserID, store.StateWaking, m.clk.Now()); err != nil {
			unlock()
			m.capMu.Unlock()
			return err
		}
		m.capMu.Unlock()

		m.log.Info("capacity guard evicting sandbox", "victim", victim.UserID, "for", self.UserID)
		err = m.stopLocked(ctx, victim, false, "capacity", store.StateStopped)
		unlock()
		if err == nil {
			return nil
		}
		m.log.Error("eviction failed", "victim", victim.UserID, "err", err)
		// Give the slot back and try again.
		_ = m.store.SetState(ctx, self.UserID, self.State, m.clk.Now())
	}
	return ErrCapacity
}

// pickVictim chooses an evictable sandbox and returns it with its user lock held.
func (m *Manager) pickVictim(active []store.Sandbox, self string) (store.Sandbox, func()) {
	cands := []store.Sandbox{}
	for _, a := range active {
		if a.UserID != self && a.State == store.StateAwake && m.inflightCount(a.UserID) == 0 && !m.loginPending(a.UserID) {
			cands = append(cands, a)
		}
	}
	sort.SliceStable(cands, func(i, j int) bool {
		if cands[i].Priority != cands[j].Priority {
			return cands[i].Priority < cands[j].Priority
		}
		return cands[i].LastActiveAt.Before(cands[j].LastActiveAt)
	})
	for _, c := range cands {
		l := m.userLock(c.UserID)
		if !l.TryLock() {
			continue // someone is working on it right now
		}
		// Re-check under its lock.
		if m.inflightCount(c.UserID) != 0 {
			l.Unlock()
			continue
		}
		return c, l.Unlock
	}
	return store.Sandbox{}, nil
}

// ensureContainerLocked makes sure the sandbox has a container running the
// current image with the limits it should have, re-creating it (keeping /data)
// when it is missing, outdated, or was created with or without Scout's browser
// and the switch has changed since (memory and process limits are fixed at creation).
// The container must not be running when it has to be re-created.
func (m *Manager) ensureContainerLocked(ctx context.Context, sb *store.Sandbox) error {
	if sb.ContainerID != "" {
		c, err := m.eng.InspectContainer(ctx, sb.ContainerID)
		switch {
		case err == nil && sb.ImageVersion == m.cfg.AgentImageVersion && hasBrowser(c) == sb.BrowserEnabled:
			return nil
		case err == nil && sb.ImageVersion == m.cfg.AgentImageVersion:
			m.log.Info("browser switch changed: re-creating container with its new limits", "user_id", sb.UserID,
				"browser", sb.BrowserEnabled)
		case err == nil:
			m.log.Info("image rollout: re-creating container", "user_id", sb.UserID,
				"from", sb.ImageVersion, "to", m.cfg.AgentImageVersion)
		case errors.Is(err, docker.ErrNotFound):
			m.log.Warn("container missing: re-creating it", "user_id", sb.UserID)
		default:
			return err
		}
	}
	return m.recreateLocked(ctx, sb)
}

// recreateLocked replaces the container with a fresh one from the current image.
// The /data volume (and so memory, sessions and the secrets file) is kept.
func (m *Manager) recreateLocked(ctx context.Context, sb *store.Sandbox) error {
	spec := m.spec(sb.UserID, sb.BrowserEnabled)
	if sb.ContainerID != "" {
		_ = m.eng.StopContainer(ctx, sb.ContainerID, 10*time.Second)
		if err := m.eng.RemoveContainer(ctx, sb.ContainerID); err != nil {
			return err
		}
	}
	if err := m.eng.RemoveContainer(ctx, spec.Name); err != nil {
		return err
	}
	if err := m.eng.EnsureVolume(ctx, spec.VolumeName, m.cfg.VolumeDriver, m.cfg.VolumeOpts,
		map[string]string{LabelManaged: "true", LabelUserID: sb.UserID}); err != nil {
		return err
	}
	id, err := m.eng.CreateContainer(ctx, spec)
	if err != nil {
		return err
	}
	// The secrets must still be on the volume, or the backend's token hash no longer matches.
	if _, err := m.loadPlatformFile(ctx, sb.UserID, id, sb.MCPTokenHash, sb.BridgeSecretHash); err != nil {
		return fmt.Errorf("re-created container has no valid secrets file (volume lost?): %w", err)
	}
	if err := m.store.SetContainer(ctx, sb.UserID, id, m.cfg.AgentImageVersion, m.clk.Now()); err != nil {
		return err
	}
	sb.ContainerID, sb.ImageVersion = id, m.cfg.AgentImageVersion
	return nil
}

// loadPlatformFile reads platform.json, checks it against the stored hashes and caches the Bridge secret.
func (m *Manager) loadPlatformFile(ctx context.Context, userID, containerID, mcpHash, bridgeHash string) (platformfile.File, error) {
	f, err := platformfile.Read(ctx, m.eng, containerID)
	if err != nil {
		return platformfile.File{}, err
	}
	if ids.Hash(f.MCPToken) != mcpHash || ids.Hash(f.BridgeSecret) != bridgeHash || f.UserID != userID {
		m.log.Error("secrets file does not match stored hashes", "user_id", userID, "alert", true)
		return platformfile.File{}, errors.New("secrets file does not match stored hashes")
	}
	m.mu.Lock()
	m.secrets[userID] = f.BridgeSecret
	m.mu.Unlock()
	return f, nil
}

// bridgeSecret returns the cached Bridge secret, loading it from /data if needed.
func (m *Manager) bridgeSecret(ctx context.Context, sb store.Sandbox) (string, error) {
	m.mu.Lock()
	s, ok := m.secrets[sb.UserID]
	m.mu.Unlock()
	if ok {
		return s, nil
	}
	f, err := m.loadPlatformFile(ctx, sb.UserID, sb.ContainerID, sb.MCPTokenHash, sb.BridgeSecretHash)
	if err != nil {
		return "", err
	}
	return f.BridgeSecret, nil
}

// startLocked starts the container and waits for the Bridge. HLD 10.1:
// retry -> re-create from the image (keep /data) -> give up (caller marks broken).
func (m *Manager) startLocked(ctx context.Context, sb *store.Sandbox) error {
	if err := m.ensureContainerLocked(ctx, sb); err != nil {
		return err
	}
	// The Bridge reads platform.json when it boots: the browser switch in it must match the
	// container that is about to start (its limits and, below, its egress rule).
	if err := m.syncPlatformFileLocked(ctx, *sb); err != nil {
		return err
	}
	var lastErr error
	for attempt := 1; attempt <= 2; attempt++ {
		if attempt == 2 {
			m.log.Warn("sandbox did not start; re-creating from the image", "user_id", sb.UserID, "err", lastErr)
			if err := m.recreateLocked(ctx, sb); err != nil {
				return err
			}
		}
		c, err := m.startContainer(ctx, *sb)
		if err != nil {
			lastErr = err
			continue
		}
		ip := c.IPs[m.cfg.SandboxNetwork]
		h, err := m.waitHealthy(ctx, ip)
		if err != nil {
			lastErr = err
			_ = m.eng.StopContainer(ctx, sb.ContainerID, 0)
			_ = m.store.EndAwake(ctx, sb.UserID, m.clk.Now())
			continue
		}
		m.noteClaudeStatus(ctx, *sb, h)
		if err := m.eg.Write(sb.UserID, ip, m.egressRule(*sb, c)); err != nil {
			m.log.Error("egress allowlist write failed", "user_id", sb.UserID, "err", err, "alert", true)
		}
		now := m.clk.Now()
		if err := m.store.SetState(ctx, sb.UserID, store.StateAwake, now); err != nil {
			return err
		}
		if err := m.store.Touch(ctx, sb.UserID, now); err != nil {
			return err
		}
		m.resetHealth(sb.UserID)
		m.log.Info("sandbox awake", "user_id", sb.UserID, "container_id", sb.ContainerID, "ip", ip, "browser", hasBrowser(c))
		return nil
	}
	return lastErr
}

// startContainer starts the container (one retry), opens the awake period and returns the
// running container (with its IP on the sandbox network).
func (m *Manager) startContainer(ctx context.Context, sb store.Sandbox) (docker.Container, error) {
	err := m.eng.StartContainer(ctx, sb.ContainerID)
	if err != nil {
		m.log.Warn("container start failed; retrying", "user_id", sb.UserID, "err", err)
		err = m.eng.StartContainer(ctx, sb.ContainerID)
	}
	if err != nil {
		return docker.Container{}, err
	}
	if err := m.store.StartAwake(ctx, sb.UserID, m.clk.Now()); err != nil {
		return docker.Container{}, err
	}
	c, err := m.eng.InspectContainer(ctx, sb.ContainerID)
	if err != nil {
		return docker.Container{}, err
	}
	if c.IPs[m.cfg.SandboxNetwork] == "" {
		_ = m.eng.StopContainer(ctx, sb.ContainerID, 0)
		_ = m.store.EndAwake(ctx, sb.UserID, m.clk.Now())
		return docker.Container{}, fmt.Errorf("container has no IP on network %s", m.cfg.SandboxNetwork)
	}
	return c, nil
}

// waitHealthy polls GET /healthz until it answers 200 or WakeTimeout passes.
func (m *Manager) waitHealthy(ctx context.Context, ip string) (bridge.Health, error) {
	wctx, cancel := context.WithTimeout(ctx, m.cfg.WakeTimeout)
	defer cancel()
	addr := m.bridgeAddr(ip)
	var lastErr error
	for {
		hctx, hcancel := context.WithTimeout(wctx, 5*time.Second)
		h, err := m.br.Health(hctx, addr)
		hcancel()
		if err == nil {
			return h, nil
		}
		lastErr = err
		select {
		case <-wctx.Done():
			return bridge.Health{}, fmt.Errorf("bridge not healthy after %s: %v", m.cfg.WakeTimeout, lastErr)
		case <-time.After(m.PollInterval):
		}
	}
}

// Stop stops the sandbox. Polite unless force. A polite stop of a sandbox in a
// turn is refused (never stop mid-turn); the stuck-turn path uses force. So is a
// polite stop while a Claude sign-in waits for its code (stopping would end the
// waiting CLI), the same rule the idle stop and eviction keep. The backend asks
// for a polite stop after a crew job, to let the container sleep at once.
func (m *Manager) Stop(ctx context.Context, userID string, force bool, reason string) (store.Sandbox, error) {
	if err := validateUserID(userID); err != nil {
		return store.Sandbox{}, err
	}
	l := m.userLock(userID)
	l.Lock()
	defer l.Unlock()
	ctx, cancel := detached(ctx, m.cfg.ShutdownGrace+time.Minute)
	defer cancel()
	sb, err := m.getSandbox(ctx, userID)
	if err != nil {
		return store.Sandbox{}, err
	}
	if !force && (sb.State == store.StateBusy || m.inflightCount(userID) > 0) {
		return store.Sandbox{}, fmt.Errorf("%w: use force to stop a stuck turn", ErrBusy)
	}
	if !force && m.loginPending(userID) {
		return store.Sandbox{}, fmt.Errorf("%w: a Claude sign-in is waiting for its code", ErrBusy)
	}
	final := store.StateStopped
	if sb.State == store.StateBroken {
		final = store.StateBroken
	}
	if reason == "" {
		reason = "requested"
	}
	if err := m.stopLocked(ctx, sb, force, reason, final); err != nil {
		return store.Sandbox{}, err
	}
	return m.getSandbox(ctx, userID)
}

// stopLocked stops the container (politely: /shutdown then docker stop), removes
// its egress file, closes the awake period and sets the final state.
func (m *Manager) stopLocked(ctx context.Context, sb store.Sandbox, force bool, reason string, final store.State) error {
	if sb.ContainerID != "" {
		c, err := m.eng.InspectContainer(ctx, sb.ContainerID)
		if err != nil && !errors.Is(err, docker.ErrNotFound) {
			return err
		}
		if err == nil && c.Running {
			timeout := 10 * time.Second
			if force {
				timeout = 0
			} else if ip := c.IPs[m.cfg.SandboxNetwork]; ip != "" {
				if secret, serr := m.bridgeSecret(ctx, sb); serr == nil {
					sctx, cancel := context.WithTimeout(ctx, m.cfg.ShutdownGrace)
					if err := m.br.Shutdown(sctx, m.bridgeAddr(ip), secret); err != nil {
						m.log.Warn("polite shutdown failed; stopping anyway", "user_id", sb.UserID, "err", err)
					}
					cancel()
				}
			}
			if err := m.eng.StopContainer(ctx, sb.ContainerID, timeout); err != nil && !errors.Is(err, docker.ErrNotFound) {
				return err
			}
		}
	}
	return m.markStoppedLocked(ctx, sb, final, "reason", reason, "force", force)
}

// markStoppedLocked records that the container is not running.
func (m *Manager) markStoppedLocked(ctx context.Context, sb store.Sandbox, final store.State, logArgs ...any) error {
	if err := m.eg.Remove(sb.UserID); err != nil {
		m.log.Error("egress allowlist remove failed", "user_id", sb.UserID, "err", err)
	}
	now := m.clk.Now()
	if err := m.store.EndAwake(ctx, sb.UserID, now); err != nil {
		return err
	}
	if err := m.store.SetState(ctx, sb.UserID, final, now); err != nil && !errors.Is(err, store.ErrNotFound) {
		return err
	}
	m.resetHealth(sb.UserID)
	m.log.Info("sandbox stopped", append([]any{"user_id", sb.UserID, "state", string(final)}, logArgs...)...)
	return nil
}

// Delete removes the container, the /data volume and the egress rules. Only
// for account deletion. Idempotent. Awake periods are kept for billing.
func (m *Manager) Delete(ctx context.Context, userID string) error {
	if err := validateUserID(userID); err != nil {
		return err
	}
	l := m.userLock(userID)
	l.Lock()
	defer l.Unlock()
	ctx, cancel := detached(ctx, 2*time.Minute)
	defer cancel()
	sb, err := m.store.Get(ctx, userID)
	if err != nil && !errors.Is(err, store.ErrNotFound) {
		return err
	}
	if err == nil && sb.ContainerID != "" {
		_ = m.eng.StopContainer(ctx, sb.ContainerID, 0)
		if err := m.eng.RemoveContainer(ctx, sb.ContainerID); err != nil {
			return err
		}
	}
	if err := m.eng.RemoveContainer(ctx, ids.ContainerName(userID)); err != nil {
		return err
	}
	if err := m.eng.RemoveVolume(ctx, ids.VolumeName(userID)); err != nil {
		return err
	}
	if err := m.eg.Remove(userID); err != nil {
		return err
	}
	if err := m.store.EndAwake(ctx, userID, m.clk.Now()); err != nil {
		return err
	}
	if err := m.store.Delete(ctx, userID); err != nil {
		return err
	}
	m.forget(userID)
	m.log.Info("sandbox deleted", "user_id", userID)
	return nil
}
