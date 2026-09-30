package sandbox

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/personal-agent/vm-manager/internal/bridge"
	"github.com/personal-agent/vm-manager/internal/egress"
	"github.com/personal-agent/vm-manager/internal/platformfile"
	"github.com/personal-agent/vm-manager/internal/store"
)

// Agent runtime and the Claude Code sign-in (the user's own Claude plan).
//
// The sign-in runs the official `claude auth login` inside the user's container (through the
// Bridge). Claude Code keeps its own credentials in the user's /data volume; the vm-manager
// never sees them. The code the user pastes is passed straight to the Bridge and is never
// logged or stored here.

// MaxClaudeCodeLen bounds the pasted sign-in code.
const MaxClaudeCodeLen = 4096

// ParseRuntime maps a runtime name ("" = hermes) to a store.Runtime.
func ParseRuntime(name string) (store.Runtime, error) {
	if name == "" {
		return store.RuntimeHermes, nil
	}
	rt := store.Runtime(name)
	if !rt.Valid() {
		return "", invalid("runtime must be hermes or claude_code")
	}
	return rt, nil
}

// --- pending sign-ins and sign-outs (memory only) ---

func (m *Manager) setLoginPending(userID string, until time.Time) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.logins[userID] = until
}

func (m *Manager) clearLoginPending(userID string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	delete(m.logins, userID)
}

// loginPending reports whether a Claude sign-in started here is still waiting for its code.
// A pending sign-in counts as "busy" for the idle reaper and the capacity guard.
func (m *Manager) loginPending(userID string) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	until, ok := m.logins[userID]
	if !ok {
		return false
	}
	if !m.clk.Now().Before(until) {
		delete(m.logins, userID)
		return false
	}
	return true
}

func (m *Manager) addClaudeOp(userID string, d int) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.claudeOps[userID] += d
	if m.claudeOps[userID] <= 0 {
		delete(m.claudeOps, userID)
	}
}

// needsClaudeEgress: Anthropic's hosts are reachable while the runtime is Claude Code, a
// sign-in is pending or a sign-out is running (it revokes the token at platform.claude.com).
func (m *Manager) needsClaudeEgress(sb store.Sandbox) bool {
	if sb.Runtime == store.RuntimeClaudeCode || m.loginPending(sb.UserID) {
		return true
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.claudeOps[sb.UserID] > 0
}

// egressDomains is what goes into the user's Squid file: their own allowlist, plus the
// Claude Code hosts when needed. The stored (and reported) allowlist stays the user's own.
func (m *Manager) egressDomains(sb store.Sandbox) []string {
	if !m.needsClaudeEgress(sb) || len(m.cfg.ClaudeEgressDomains) == 0 {
		return sb.Allowlist
	}
	merged, err := egress.MergeDomains(sb.Allowlist, m.cfg.ClaudeEgressDomains)
	if err != nil { // both lists are validated already; keep the user's list if not
		m.log.Error("merging claude egress domains failed", "user_id", sb.UserID, "err", err)
		return sb.Allowlist
	}
	return merged
}

// refreshEgress rewrites the user's egress file if the container is running (no-op when
// nothing changed). Used when the Claude hosts come or go without a wake.
func (m *Manager) refreshEgress(ctx context.Context, userID string) {
	sb, err := m.store.Get(ctx, userID)
	if err != nil || !sb.State.Running() {
		return
	}
	c, err := m.eng.InspectContainer(ctx, sb.ContainerID)
	if err != nil || !c.Running || c.IPs[m.cfg.SandboxNetwork] == "" {
		return
	}
	if err := m.eg.Write(userID, c.IPs[m.cfg.SandboxNetwork], m.egressDomains(sb)); err != nil {
		m.log.Error("egress allowlist write failed", "user_id", userID, "err", err, "alert", true)
	}
}

// noteClaudeStatus stores the sign-in status the Bridge reported, when it changed.
func (m *Manager) noteClaudeStatus(ctx context.Context, sb store.Sandbox, h bridge.Health) {
	if h.ClaudeConnected == nil || *h.ClaudeConnected == sb.ClaudeConnected {
		return
	}
	if err := m.store.SetClaudeConnected(ctx, sb.UserID, *h.ClaudeConnected, m.clk.Now()); err != nil {
		m.log.Warn("could not record claude sign-in status", "user_id", sb.UserID, "err", err)
		return
	}
	m.log.Info("claude sign-in status changed", "user_id", sb.UserID, "claude_connected", *h.ClaudeConnected)
}

// bridgeTarget returns the Bridge address and secret of a running sandbox.
func (m *Manager) bridgeTarget(ctx context.Context, sb store.Sandbox) (string, string, error) {
	c, err := m.eng.InspectContainer(ctx, sb.ContainerID)
	if err != nil || !c.Running || c.IPs[m.cfg.SandboxNetwork] == "" {
		return "", "", fmt.Errorf("%w: container is not running", ErrAgentUnavailable)
	}
	secret, err := m.bridgeSecret(ctx, sb)
	if err != nil {
		return "", "", err
	}
	return m.bridgeAddr(c.IPs[m.cfg.SandboxNetwork]), secret, nil
}

// claudeBridgeErr maps Bridge errors of the Claude calls to manager errors. The Bridge
// client's errors never contain the request body, so they are safe to log.
func (m *Manager) claudeBridgeErr(userID, op string, err error) error {
	switch {
	case errors.Is(err, bridge.ErrLoginNotFound):
		return ErrLoginNotFound
	case errors.Is(err, bridge.ErrBadRequest):
		return invalid("the code must be a single line")
	case errors.Is(err, ErrAgentUnavailable), errors.Is(err, ErrNotFound), errors.Is(err, ErrInvalid):
		return err
	}
	m.log.Warn("claude "+op+" failed at the bridge", "user_id", userID, "err", err)
	return fmt.Errorf("%w: %v", ErrAgentUnavailable, err)
}

// --- RPCs ---

// SetAgentRuntime chooses which agent answers (hermes | claude_code). It is written into
// platform.json, which the Bridge reads on every delivery, so no restart is needed. The egress
// file gains or loses Anthropic's hosts at once when the sandbox is awake.
func (m *Manager) SetAgentRuntime(ctx context.Context, userID string, rt store.Runtime) (store.Sandbox, error) {
	if err := validateUserID(userID); err != nil {
		return store.Sandbox{}, err
	}
	if rt == "" {
		rt = store.RuntimeHermes
	}
	if !rt.Valid() {
		return store.Sandbox{}, invalid("runtime must be hermes or claude_code")
	}
	l := m.userLock(userID)
	l.Lock()
	defer l.Unlock()
	ctx, cancel := detached(ctx, time.Minute)
	defer cancel()
	sb, err := m.getSandbox(ctx, userID)
	if err != nil {
		return store.Sandbox{}, err
	}
	if !sb.State.Running() {
		if err := m.ensureContainerLocked(ctx, &sb); err != nil {
			return store.Sandbox{}, err
		}
	}
	f, err := m.loadPlatformFile(ctx, userID, sb.ContainerID, sb.MCPTokenHash, sb.BridgeSecretHash)
	if err != nil {
		return store.Sandbox{}, err
	}
	f.Runtime = string(rt)
	if err := platformfile.Write(ctx, m.eng, sb.ContainerID, m.cfg.AgentUID, f); err != nil {
		return store.Sandbox{}, err
	}
	if err := m.store.SetRuntime(ctx, userID, rt, m.clk.Now()); err != nil {
		return store.Sandbox{}, err
	}
	sb.Runtime = rt
	if sb.State.Running() {
		if c, err := m.eng.InspectContainer(ctx, sb.ContainerID); err == nil && c.Running && c.IPs[m.cfg.SandboxNetwork] != "" {
			if err := m.eg.Write(userID, c.IPs[m.cfg.SandboxNetwork], m.egressDomains(sb)); err != nil {
				return store.Sandbox{}, err
			}
		}
	}
	m.log.Info("agent runtime set", "user_id", userID, "runtime", string(rt))
	return m.getSandbox(ctx, userID)
}

// StartClaudeLogin wakes the sandbox if needed and starts the official Claude Code sign-in in
// it. It returns Anthropic's authorize URL; the user signs in on Anthropic's page. Until the
// sign-in completes or expires, the sandbox is treated as busy (not stopped when idle, not
// evicted) and can reach Anthropic's hosts.
func (m *Manager) StartClaudeLogin(ctx context.Context, userID string) (bridge.ClaudeLogin, error) {
	if err := validateUserID(userID); err != nil {
		return bridge.ClaudeLogin{}, err
	}
	// Pending before the wake, so the egress file written by the wake already has the hosts.
	m.setLoginPending(userID, m.clk.Now().Add(m.cfg.ClaudeLoginTTL))
	sb, _, err := m.wake(ctx, userID, 0, true) // counted as in flight until we return
	if err != nil {
		m.clearLoginPending(userID)
		return bridge.ClaudeLogin{}, err
	}
	defer m.addInflight(userID, -1)
	ctx, cancel := detached(ctx, 2*time.Minute)
	defer cancel()
	m.refreshEgress(ctx, userID)

	login, err := func() (bridge.ClaudeLogin, error) {
		addr, secret, err := m.bridgeTarget(ctx, sb)
		if err != nil {
			return bridge.ClaudeLogin{}, err
		}
		return m.br.ClaudeLoginStart(ctx, addr, secret)
	}()
	if err != nil {
		m.clearLoginPending(userID)
		m.refreshEgress(ctx, userID)
		return bridge.ClaudeLogin{}, m.claudeBridgeErr(userID, "sign-in start", err)
	}
	_ = m.store.Touch(ctx, userID, m.clk.Now())
	m.log.Info("claude sign-in started", "user_id", userID)
	return login, nil
}

// CompleteClaudeLogin hands the code from Anthropic's page to the waiting sign-in. The code
// is passed straight through: never logged, never stored.
func (m *Manager) CompleteClaudeLogin(ctx context.Context, userID, loginID, code string) (bridge.ClaudeLoginResult, error) {
	if err := validateUserID(userID); err != nil {
		return bridge.ClaudeLoginResult{}, err
	}
	if loginID == "" || len(loginID) > 200 {
		return bridge.ClaudeLoginResult{}, invalid("login_id is required")
	}
	if code == "" || len(code) > MaxClaudeCodeLen {
		return bridge.ClaudeLoginResult{}, invalid("code is required (max %d characters)", MaxClaudeCodeLen)
	}
	for _, r := range code {
		if r < 0x20 || r == 0x7f {
			return bridge.ClaudeLoginResult{}, invalid("the code must be a single line")
		}
	}
	// Under the user lock: the sandbox must still be running (a stopped container has lost
	// the waiting CLI), and it is counted as in flight so nothing stops it meanwhile.
	l := m.userLock(userID)
	l.Lock()
	sb, err := m.getSandbox(ctx, userID)
	if err == nil && !sb.State.Running() {
		err = ErrLoginNotFound
	}
	if err == nil {
		m.addInflight(userID, 1)
	}
	l.Unlock()
	if err != nil {
		return bridge.ClaudeLoginResult{}, err
	}
	defer m.addInflight(userID, -1)
	ctx, cancel := detached(ctx, 4*time.Minute)
	defer cancel()

	res, err := func() (bridge.ClaudeLoginResult, error) {
		addr, secret, err := m.bridgeTarget(ctx, sb)
		if err != nil {
			return bridge.ClaudeLoginResult{}, err
		}
		return m.br.ClaudeLoginComplete(ctx, addr, secret, loginID, code)
	}()
	if err != nil {
		if errors.Is(err, bridge.ErrLoginNotFound) || errors.Is(err, ErrAgentUnavailable) {
			m.clearLoginPending(userID) // the waiting CLI is gone
			m.refreshEgress(ctx, userID)
		}
		return bridge.ClaudeLoginResult{}, m.claudeBridgeErr(userID, "sign-in", err)
	}
	m.clearLoginPending(userID) // one attempt per sign-in (the Bridge ends it either way)
	now := m.clk.Now()
	if err := m.store.SetClaudeConnected(ctx, userID, res.Connected, now); err != nil {
		m.log.Warn("could not record claude sign-in status", "user_id", userID, "err", err)
	}
	_ = m.store.Touch(ctx, userID, now)
	m.refreshEgress(ctx, userID) // drops Anthropic's hosts again unless the runtime is claude_code
	m.log.Info("claude sign-in finished", "user_id", userID, "claude_connected", res.Connected)
	return res, nil
}

// ClaudeLogout signs Claude Code out with its own `claude auth logout` (it removes its
// credentials from /data). Wakes the sandbox if needed.
func (m *Manager) ClaudeLogout(ctx context.Context, userID string) error {
	if err := validateUserID(userID); err != nil {
		return err
	}
	m.clearLoginPending(userID)
	m.addClaudeOp(userID, 1)
	sb, _, err := m.wake(ctx, userID, 0, true)
	if err != nil {
		m.addClaudeOp(userID, -1)
		return err
	}
	defer m.addInflight(userID, -1)
	ctx, cancel := detached(ctx, 2*time.Minute)
	defer cancel()
	m.refreshEgress(ctx, userID)
	connected, err := func() (bool, error) {
		addr, secret, err := m.bridgeTarget(ctx, sb)
		if err != nil {
			return false, err
		}
		return m.br.ClaudeLogout(ctx, addr, secret)
	}()
	m.addClaudeOp(userID, -1)
	m.refreshEgress(ctx, userID)
	if err != nil {
		return m.claudeBridgeErr(userID, "sign-out", err)
	}
	if err := m.store.SetClaudeConnected(ctx, userID, connected, m.clk.Now()); err != nil {
		return err
	}
	_ = m.store.Touch(ctx, userID, m.clk.Now())
	m.log.Info("claude signed out", "user_id", userID, "claude_connected", connected)
	return nil
}
