// Package clock lets the loops and the state machine run on a fake clock in tests.
package clock

import (
	"sync"
	"time"
)

// Clock is the time source used everywhere instead of time.Now.
type Clock interface {
	Now() time.Time
}

// Real is the wall clock (UTC).
type Real struct{}

// Now returns the current UTC time.
func (Real) Now() time.Time { return time.Now().UTC() }

// Fake is a manually advanced clock for tests.
type Fake struct {
	mu sync.Mutex
	t  time.Time
}

// NewFake starts a fake clock at t.
func NewFake(t time.Time) *Fake { return &Fake{t: t.UTC()} }

// Now returns the fake time.
func (f *Fake) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.t
}

// Advance moves the fake time forward.
func (f *Fake) Advance(d time.Duration) {
	f.mu.Lock()
	f.t = f.t.Add(d)
	f.mu.Unlock()
}

// Set jumps to t.
func (f *Fake) Set(t time.Time) {
	f.mu.Lock()
	f.t = t.UTC()
	f.mu.Unlock()
}
