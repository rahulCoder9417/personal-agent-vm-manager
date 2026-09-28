// Package store persists the sandboxes table and awake periods.
// Postgres (pgx) in production; an in-memory implementation for unit tests.
package store

import (
	"context"
	"errors"
	"time"
)

var (
	// ErrNotFound means there is no sandbox row for the user.
	ErrNotFound = errors.New("store: sandbox not found")
	// ErrExists means a sandbox row already exists for the user.
	ErrExists = errors.New("store: sandbox already exists")
)

// State is the sandbox lifecycle state.
type State string

// Sandbox states (same names as the table's CHECK constraint).
const (
	StateStopped State = "stopped"
	StateWaking  State = "waking"
	StateAwake   State = "awake"
	StateBusy    State = "busy"
	StateBroken  State = "broken"
)

// Valid reports whether s is a known state.
func (s State) Valid() bool {
	switch s {
	case StateStopped, StateWaking, StateAwake, StateBusy, StateBroken:
		return true
	}
	return false
}

// Running reports whether the state means the container should be running.
func (s State) Running() bool {
	return s == StateWaking || s == StateAwake || s == StateBusy
}

// Sandbox is one row of the sandboxes table.
type Sandbox struct {
	UserID           string
	ContainerID      string
	ImageVersion     string
	State            State
	Priority         int32
	LastActiveAt     time.Time
	MCPTokenHash     string
	BridgeSecretHash string
	Allowlist        []string
	CreatedAt        time.Time
	UpdatedAt        time.Time
}

// Usage is awake time for one user in a window.
type Usage struct {
	UserID         string
	AwakeSeconds   int64
	BrowserSeconds int64
}

// Store is everything the manager needs from persistence.
// Every method that takes a userID touches only that user's rows.
type Store interface {
	// Insert adds a new sandbox. ErrExists if the user already has one.
	Insert(ctx context.Context, sb Sandbox) error
	// Get returns the user's sandbox or ErrNotFound.
	Get(ctx context.Context, userID string) (Sandbox, error)
	// List returns sandboxes in any of the states (all when none given), ordered by user id.
	List(ctx context.Context, states ...State) ([]Sandbox, error)
	// SetState changes the state.
	SetState(ctx context.Context, userID string, s State, at time.Time) error
	// SetContainer records a (re-)created container and the image version it runs.
	SetContainer(ctx context.Context, userID, containerID, imageVersion string, at time.Time) error
	// Touch moves last_active_at forward to at (never backwards).
	Touch(ctx context.Context, userID string, at time.Time) error
	// SetAllowlist replaces the egress allowlist.
	SetAllowlist(ctx context.Context, userID string, domains []string, at time.Time) error
	// SetPriority changes the awake-cap priority.
	SetPriority(ctx context.Context, userID string, priority int32, at time.Time) error
	// Delete removes the sandbox row (awake periods are kept for billing). No error if missing.
	Delete(ctx context.Context, userID string) error

	// StartAwake opens an awake period at `at` unless one is already open.
	StartAwake(ctx context.Context, userID string, at time.Time) error
	// EndAwake closes the open awake period (if any) at `at`.
	EndAwake(ctx context.Context, userID string, at time.Time) error
	// Usage sums awake seconds per user clipped to [since, until); open periods count up to
	// min(until, now). Browser seconds are counted for periods that ended in [since, until).
	// userID "" means all users. Users with no overlapping period are omitted.
	Usage(ctx context.Context, userID string, since, until, now time.Time) ([]Usage, error)

	Close()
}

// clipMillis returns the milliseconds of [start, end) that fall inside [since, until).
func clipMillis(start, end, since, until time.Time) int64 {
	if start.Before(since) {
		start = since
	}
	if end.After(until) {
		end = until
	}
	if !end.After(start) {
		return 0
	}
	return end.Sub(start).Milliseconds()
}
