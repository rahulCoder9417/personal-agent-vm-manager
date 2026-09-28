package store

import (
	"context"
	"slices"
	"sort"
	"sync"
	"time"
)

type period struct {
	userID         string
	startedAt      time.Time
	endedAt        *time.Time
	browserSeconds int64
}

// Memory is an in-memory Store for unit tests. Same semantics as Postgres.
type Memory struct {
	mu        sync.Mutex
	sandboxes map[string]Sandbox
	periods   []*period
}

// NewMemory returns an empty in-memory store.
func NewMemory() *Memory {
	return &Memory{sandboxes: map[string]Sandbox{}}
}

func copySandbox(sb Sandbox) Sandbox {
	sb.Allowlist = slices.Clone(sb.Allowlist)
	if sb.Allowlist == nil {
		sb.Allowlist = []string{}
	}
	return sb
}

// Insert implements Store.
func (m *Memory) Insert(_ context.Context, sb Sandbox) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, ok := m.sandboxes[sb.UserID]; ok {
		return ErrExists
	}
	for _, other := range m.sandboxes {
		if other.MCPTokenHash == sb.MCPTokenHash {
			return ErrExists
		}
	}
	m.sandboxes[sb.UserID] = copySandbox(sb)
	return nil
}

// Get implements Store.
func (m *Memory) Get(_ context.Context, userID string) (Sandbox, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	sb, ok := m.sandboxes[userID]
	if !ok {
		return Sandbox{}, ErrNotFound
	}
	return copySandbox(sb), nil
}

// List implements Store.
func (m *Memory) List(_ context.Context, states ...State) ([]Sandbox, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := []Sandbox{}
	for _, sb := range m.sandboxes {
		if len(states) == 0 || slices.Contains(states, sb.State) {
			out = append(out, copySandbox(sb))
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].UserID < out[j].UserID })
	return out, nil
}

func (m *Memory) update(userID string, fn func(*Sandbox)) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	sb, ok := m.sandboxes[userID]
	if !ok {
		return ErrNotFound
	}
	fn(&sb)
	m.sandboxes[userID] = sb
	return nil
}

// SetState implements Store.
func (m *Memory) SetState(_ context.Context, userID string, s State, at time.Time) error {
	return m.update(userID, func(sb *Sandbox) { sb.State = s; sb.UpdatedAt = at })
}

// SetContainer implements Store.
func (m *Memory) SetContainer(_ context.Context, userID, containerID, imageVersion string, at time.Time) error {
	return m.update(userID, func(sb *Sandbox) {
		sb.ContainerID = containerID
		sb.ImageVersion = imageVersion
		sb.UpdatedAt = at
	})
}

// Touch implements Store.
func (m *Memory) Touch(_ context.Context, userID string, at time.Time) error {
	return m.update(userID, func(sb *Sandbox) {
		if at.After(sb.LastActiveAt) {
			sb.LastActiveAt = at
		}
		sb.UpdatedAt = at
	})
}

// SetAllowlist implements Store.
func (m *Memory) SetAllowlist(_ context.Context, userID string, domains []string, at time.Time) error {
	return m.update(userID, func(sb *Sandbox) { sb.Allowlist = slices.Clone(domains); sb.UpdatedAt = at })
}

// SetPriority implements Store.
func (m *Memory) SetPriority(_ context.Context, userID string, priority int32, at time.Time) error {
	return m.update(userID, func(sb *Sandbox) { sb.Priority = priority; sb.UpdatedAt = at })
}

// Delete implements Store.
func (m *Memory) Delete(_ context.Context, userID string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	delete(m.sandboxes, userID)
	return nil
}

// StartAwake implements Store.
func (m *Memory) StartAwake(_ context.Context, userID string, at time.Time) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, p := range m.periods {
		if p.userID == userID && p.endedAt == nil {
			return nil
		}
	}
	m.periods = append(m.periods, &period{userID: userID, startedAt: at})
	return nil
}

// EndAwake implements Store.
func (m *Memory) EndAwake(_ context.Context, userID string, at time.Time) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, p := range m.periods {
		if p.userID == userID && p.endedAt == nil {
			end := at
			if end.Before(p.startedAt) {
				end = p.startedAt
			}
			p.endedAt = &end
		}
	}
	return nil
}

// AddBrowserSeconds is a test helper (nothing records browser time yet in the MVP).
func (m *Memory) AddBrowserSeconds(userID string, seconds int64) {
	m.mu.Lock()
	defer m.mu.Unlock()
	for i := len(m.periods) - 1; i >= 0; i-- {
		if m.periods[i].userID == userID {
			m.periods[i].browserSeconds += seconds
			return
		}
	}
}

// Usage implements Store.
func (m *Memory) Usage(_ context.Context, userID string, since, until, now time.Time) ([]Usage, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	type acc struct{ ms, browser int64 }
	byUser := map[string]*acc{}
	openEnd := until
	if now.Before(openEnd) {
		openEnd = now
	}
	for _, p := range m.periods {
		if userID != "" && p.userID != userID {
			continue
		}
		if !p.startedAt.Before(until) {
			continue
		}
		if p.endedAt != nil && p.endedAt.Before(since) {
			continue
		}
		end := openEnd
		if p.endedAt != nil {
			end = *p.endedAt
		}
		a := byUser[p.userID]
		if a == nil {
			a = &acc{}
			byUser[p.userID] = a
		}
		a.ms += clipMillis(p.startedAt, end, since, until)
		if p.endedAt != nil && !p.endedAt.Before(since) && p.endedAt.Before(until) {
			a.browser += p.browserSeconds
		}
	}
	out := []Usage{}
	for uid, a := range byUser {
		out = append(out, Usage{UserID: uid, AwakeSeconds: a.ms / 1000, BrowserSeconds: a.browser})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].UserID < out[j].UserID })
	return out, nil
}

// Close implements Store.
func (m *Memory) Close() {}
