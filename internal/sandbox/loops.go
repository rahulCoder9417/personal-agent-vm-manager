package sandbox

import (
	"context"
	"errors"
	"time"

	"github.com/personal-agent/vm-manager/internal/docker"
	"github.com/personal-agent/vm-manager/internal/store"
)

// Run starts the background loops and blocks until ctx ends.
// Call ReconcileOnce before serving; Run reconciles again every ReconcileInterval.
func (m *Manager) Run(ctx context.Context) {
	loop := func(name string, every time.Duration, fn func(context.Context)) {
		t := time.NewTicker(every)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				func() {
					defer func() {
						if r := recover(); r != nil {
							m.log.Error("loop panicked", "loop", name, "panic", r, "alert", true)
						}
					}()
					fn(ctx)
				}()
			}
		}
	}
	done := make(chan struct{}, 3)
	go func() { loop("idle-reaper", m.cfg.ReaperInterval, m.ReapOnce); done <- struct{}{} }()
	go func() { loop("health-checker", m.cfg.HealthInterval, m.HealthOnce); done <- struct{}{} }()
	go func() { loop("reconciler", m.cfg.ReconcileInterval, m.ReconcileOnce); done <- struct{}{} }()
	for range 3 {
		<-done
	}
}

// ReapOnce stops awake sandboxes that are not in a turn and have been idle for
// IdleTimeout or more. Always a polite stop.
func (m *Manager) ReapOnce(ctx context.Context) {
	list, err := m.store.List(ctx, store.StateAwake)
	if err != nil {
		m.log.Error("reaper: list failed", "err", err)
		return
	}
	now := m.clk.Now()
	for _, sb := range list {
		if now.Sub(sb.LastActiveAt) < m.cfg.IdleTimeout || m.inflightCount(sb.UserID) > 0 || m.loginPending(sb.UserID) {
			continue // a pending Claude sign-in counts as busy: its CLI waits in the container
		}
		l := m.userLock(sb.UserID)
		if !l.TryLock() {
			continue // busy with something else; next round
		}
		func() {
			defer l.Unlock()
			cur, err := m.store.Get(ctx, sb.UserID)
			if err != nil || cur.State != store.StateAwake || m.inflightCount(cur.UserID) > 0 ||
				m.loginPending(cur.UserID) || m.clk.Now().Sub(cur.LastActiveAt) < m.cfg.IdleTimeout {
				return
			}
			// Belt and braces: ask the Bridge whether a turn or a Claude sign-in is in progress
			// (covers a vm-manager restart, which forgets its own pending sign-ins).
			if m.bridgeBusy(ctx, cur) {
				m.log.Info("reaper: bridge reports a turn or sign-in in progress; not stopping", "user_id", cur.UserID)
				return
			}
			sctx, cancel := detached(ctx, m.cfg.ShutdownGrace+time.Minute)
			defer cancel()
			if err := m.stopLocked(sctx, cur, false, "idle", store.StateStopped); err != nil {
				m.log.Error("reaper: stop failed", "user_id", cur.UserID, "err", err)
			}
		}()
	}
}

func (m *Manager) bridgeBusy(ctx context.Context, sb store.Sandbox) bool {
	c, err := m.eng.InspectContainer(ctx, sb.ContainerID)
	if err != nil || !c.Running || c.IPs[m.cfg.SandboxNetwork] == "" {
		return false
	}
	hctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	h, err := m.br.Health(hctx, m.bridgeAddr(c.IPs[m.cfg.SandboxNetwork]))
	return err == nil && (h.Busy > 0 || h.ClaudeLoginPending)
}

// HealthOnce checks every awake (not busy) sandbox's Bridge. After
// HealthFailuresBeforeRestart failed checks in a row the container is
// restarted; after HealthMaxRestarts restarts without a good check it is
// stopped and marked broken (logged with alert=true).
func (m *Manager) HealthOnce(ctx context.Context) {
	list, err := m.store.List(ctx, store.StateAwake)
	if err != nil {
		m.log.Error("health: list failed", "err", err)
		return
	}
	for _, sb := range list {
		if m.inflightCount(sb.UserID) > 0 {
			continue // never restart mid-turn; the turn timeout covers stuck turns
		}
		l := m.userLock(sb.UserID)
		if !l.TryLock() {
			continue
		}
		func() {
			defer l.Unlock()
			cur, err := m.store.Get(ctx, sb.UserID)
			if err != nil || cur.State != store.StateAwake || m.inflightCount(cur.UserID) > 0 {
				return
			}
			if m.checkHealth(ctx, cur) {
				m.resetHealth(cur.UserID)
				return
			}
			m.mu.Lock()
			m.healthFails[cur.UserID]++
			fails := m.healthFails[cur.UserID]
			m.mu.Unlock()
			m.log.Warn("health check failed", "user_id", cur.UserID, "consecutive", fails)
			if fails < m.cfg.HealthFailuresBeforeRestart {
				return
			}
			m.mu.Lock()
			m.healthFails[cur.UserID] = 0
			m.restarts[cur.UserID]++
			restarts := m.restarts[cur.UserID]
			m.mu.Unlock()

			rctx, cancel := detached(ctx, m.cfg.WakeTimeout+2*time.Minute)
			defer cancel()
			if restarts > m.cfg.HealthMaxRestarts {
				m.markBroken(rctx, cur, "repeated health failures")
				return
			}
			if err := m.restartLocked(rctx, cur); err != nil {
				m.log.Warn("restart after failed health check did not help", "user_id", cur.UserID, "err", err)
				if restarts >= m.cfg.HealthMaxRestarts {
					m.markBroken(rctx, cur, "restart failed")
					return
				}
				// Don't leave it "awake" with a dead container (and an open, billed awake
				// period): stop it; the next wake retries and re-creates if needed.
				if err := m.stopLocked(rctx, cur, true, "restart failed", store.StateStopped); err != nil {
					m.log.Error("stopping sandbox after failed restart failed", "user_id", cur.UserID, "err", err)
				}
			}
		}()
	}
}

func (m *Manager) checkHealth(ctx context.Context, sb store.Sandbox) bool {
	c, err := m.eng.InspectContainer(ctx, sb.ContainerID)
	if err != nil || !c.Running || c.IPs[m.cfg.SandboxNetwork] == "" {
		return false
	}
	hctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	h, err := m.br.Health(hctx, m.bridgeAddr(c.IPs[m.cfg.SandboxNetwork]))
	if err != nil {
		return false
	}
	m.noteClaudeStatus(ctx, sb, h)
	return true
}

// restartLocked restarts an unresponsive container; the awake period stays open.
func (m *Manager) restartLocked(ctx context.Context, sb store.Sandbox) error {
	m.log.Warn("restarting unresponsive sandbox", "user_id", sb.UserID)
	if err := m.eng.StopContainer(ctx, sb.ContainerID, 5*time.Second); err != nil && !errors.Is(err, docker.ErrNotFound) {
		return err
	}
	c, err := m.startContainer(ctx, sb)
	if err != nil {
		return err
	}
	ip := c.IPs[m.cfg.SandboxNetwork]
	h, err := m.waitHealthy(ctx, ip)
	if err != nil {
		return err
	}
	m.noteClaudeStatus(ctx, sb, h)
	if err := m.eg.Write(sb.UserID, ip, m.egressRule(sb, c)); err != nil {
		m.log.Error("egress allowlist write failed", "user_id", sb.UserID, "err", err, "alert", true)
	}
	return nil
}

func (m *Manager) markBroken(ctx context.Context, sb store.Sandbox, why string) {
	m.log.Error("sandbox marked broken", "user_id", sb.UserID, "why", why, "alert", true)
	if err := m.stopLocked(ctx, sb, true, why, store.StateBroken); err != nil {
		m.log.Error("stopping broken sandbox failed", "user_id", sb.UserID, "err", err)
	}
}

// ReconcileOnce makes Docker's real state match the sandboxes table:
//   - table says running but the container is not: mark stopped, close the awake period
//   - table says stopped/broken but the container runs: stop it politely
//   - stale waking/busy left by a vm-manager restart: settle to awake (or stopped)
//   - awake containers: awake period open, egress file current (IPs can change)
//   - managed containers with no row, or duplicates: removed (volumes are kept)
//   - egress files of users that are not awake: removed
func (m *Manager) ReconcileOnce(ctx context.Context) {
	containers, err := m.eng.ListContainers(ctx, map[string]string{LabelManaged: "true"})
	if err != nil {
		m.log.Error("reconcile: list containers failed", "err", err)
		return
	}
	rows, err := m.store.List(ctx)
	if err != nil {
		m.log.Error("reconcile: list sandboxes failed", "err", err)
		return
	}
	byID := map[string]docker.Container{}
	for _, c := range containers {
		byID[c.ID] = c
	}
	known := map[string]bool{}
	awake := map[string]bool{}
	for _, row := range rows {
		known[row.UserID] = true
		l := m.userLock(row.UserID)
		if !l.TryLock() {
			if row.State.Running() {
				awake[row.UserID] = true
			}
			continue // an operation is in progress for this user
		}
		func() {
			defer l.Unlock()
			rctx, cancel := detached(ctx, m.cfg.WakeTimeout+m.cfg.ShutdownGrace+time.Minute)
			defer cancel()
			cur, err := m.store.Get(rctx, row.UserID)
			if err != nil {
				return
			}
			if m.reconcileOne(rctx, cur, byID) {
				awake[cur.UserID] = true
			}
		}()
	}
	// Remove orphans and duplicates (never volumes: only DeleteSandbox deletes data).
	current := map[string]string{}
	for _, row := range rows {
		current[row.UserID] = row.ContainerID
	}
	for _, c := range containers {
		uid := c.Labels[LabelUserID]
		if !known[uid] {
			m.log.Warn("reconcile: removing container with no sandbox row", "container", c.Name, "user_id", uid)
			_ = m.eng.RemoveContainer(ctx, c.ID)
			continue
		}
		if cid := current[uid]; cid != "" && cid != c.ID {
			l := m.userLock(uid)
			if l.TryLock() {
				if cur, err := m.store.Get(ctx, uid); err == nil && cur.ContainerID != c.ID {
					m.log.Warn("reconcile: removing stale duplicate container", "container", c.Name, "user_id", uid)
					_ = m.eng.RemoveContainer(ctx, c.ID)
				}
				l.Unlock()
			}
		}
	}
	// Egress files only for awake users.
	if users, err := m.eg.Users(); err == nil {
		for _, uid := range users {
			if !awake[uid] {
				_ = m.eg.Remove(uid)
			}
		}
	}
}

// reconcileOne fixes one sandbox (user lock held). Returns true if it is awake afterwards.
func (m *Manager) reconcileOne(ctx context.Context, sb store.Sandbox, byID map[string]docker.Container) bool {
	c, exists := byID[sb.ContainerID]
	running := exists && c.Running
	switch {
	case sb.State.Running() && !running:
		m.log.Warn("reconcile: sandbox marked running but container is not; marking stopped",
			"user_id", sb.UserID, "state", string(sb.State), "container_exists", exists)
		_ = m.markStoppedLocked(ctx, sb, store.StateStopped, "reason", "reconcile")
		return false

	case !sb.State.Running() && running:
		m.log.Warn("reconcile: container running but sandbox is not awake; stopping", "user_id", sb.UserID, "state", string(sb.State))
		_ = m.stopLocked(ctx, sb, false, "reconcile", sb.State)
		return false

	case !sb.State.Running():
		// Stopped / broken and not running: make sure no awake period is left open.
		_ = m.store.EndAwake(ctx, sb.UserID, m.clk.Now())
		return false
	}

	// Running in both. Settle stale states left by a vm-manager restart.
	if m.inflightCount(sb.UserID) == 0 && (sb.State == store.StateWaking || sb.State == store.StateBusy) {
		if !m.checkHealth(ctx, sb) {
			m.log.Warn("reconcile: stale sandbox not healthy; stopping", "user_id", sb.UserID, "state", string(sb.State))
			_ = m.stopLocked(ctx, sb, true, "reconcile", store.StateStopped)
			return false
		}
		m.log.Info("reconcile: settling stale state to awake", "user_id", sb.UserID, "state", string(sb.State))
		_ = m.store.SetState(ctx, sb.UserID, store.StateAwake, m.clk.Now())
		_ = m.store.Touch(ctx, sb.UserID, m.clk.Now())
	}
	_ = m.store.StartAwake(ctx, sb.UserID, m.clk.Now()) // no-op when already open
	if ip := c.IPs[m.cfg.SandboxNetwork]; ip != "" {
		if err := m.eg.Write(sb.UserID, ip, m.egressRule(sb, c)); err != nil {
			m.log.Error("reconcile: egress write failed", "user_id", sb.UserID, "err", err)
		}
	}
	return true
}
