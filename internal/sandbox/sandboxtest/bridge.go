// Package sandboxtest has test doubles shared by the sandbox and grpcserver tests.
package sandboxtest

import (
	"context"
	"errors"
	"io"
	"strings"
	"sync"

	"github.com/personal-agent/vm-manager/internal/bridge"
	"github.com/personal-agent/vm-manager/internal/docker"
	"github.com/personal-agent/vm-manager/internal/platformfile"
)

const labelUserID = "pa.user_id" // sandbox.LabelUserID (not imported: avoids an import cycle)

// FakeBridge answers like the Bridge in each fake container, found by IP.
type FakeBridge struct {
	eng *docker.Fake

	mu        sync.Mutex
	unhealthy map[string]bool         // user -> /healthz fails
	busy      map[string]int          // user -> busy count reported by /healthz
	shutdowns []string                // users
	delivered []bridge.DeliverRequest // see Delivered
	// script returns the events for a delivery; block (if set) is waited on before the last event.
	Script func(userID string, req bridge.DeliverRequest) []bridge.Event
	Block  chan struct{}
	// onEvent runs after each event is handed out (lets tests look at state mid-turn).
	OnEvent func(userID string, ev bridge.Event)
}

func NewFakeBridge(eng *docker.Fake) *FakeBridge {
	return &FakeBridge{eng: eng, unhealthy: map[string]bool{}, busy: map[string]int{}}
}

func (b *FakeBridge) containerFor(addr string) (docker.Container, bool) {
	ip := addr[:strings.LastIndex(addr, ":")]
	list, _ := b.eng.ListContainers(context.Background(), nil)
	for _, c := range list {
		for _, cip := range c.IPs {
			if cip == ip && c.Running {
				return c, true
			}
		}
	}
	return docker.Container{}, false
}

func (b *FakeBridge) checkSecret(c docker.Container, secret string) error {
	f, err := platformfile.Read(context.Background(), b.eng, c.ID)
	if err != nil {
		return err
	}
	if f.BridgeSecret != secret {
		return bridge.ErrUnauthorized
	}
	return nil
}

func (b *FakeBridge) Health(_ context.Context, addr string) (bridge.Health, error) {
	c, ok := b.containerFor(addr)
	if !ok {
		return bridge.Health{}, errors.New("connection refused")
	}
	uid := c.Labels[labelUserID]
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.unhealthy[uid] {
		return bridge.Health{}, errors.New("healthz: 503")
	}
	return bridge.Health{OK: true, Busy: b.busy[uid]}, nil
}

func (b *FakeBridge) Shutdown(_ context.Context, addr, secret string) error {
	c, ok := b.containerFor(addr)
	if !ok {
		return errors.New("connection refused")
	}
	if err := b.checkSecret(c, secret); err != nil {
		return err
	}
	b.mu.Lock()
	b.shutdowns = append(b.shutdowns, c.Labels[labelUserID])
	b.mu.Unlock()
	return nil
}

type sliceStream struct {
	b      *FakeBridge
	userID string
	ctx    context.Context
	events []bridge.Event
	block  chan struct{}
	i      int
}

func (s *sliceStream) Next() (bridge.Event, error) {
	if s.i >= len(s.events) {
		return bridge.Event{}, io.EOF
	}
	if s.block != nil && s.i == len(s.events)-1 {
		select {
		case <-s.block:
		case <-s.ctx.Done():
			return bridge.Event{}, s.ctx.Err()
		}
	}
	ev := s.events[s.i]
	s.i++
	if s.b.OnEvent != nil {
		s.b.OnEvent(s.userID, ev)
	}
	return ev, nil
}

func (s *sliceStream) Close() error { return nil }

func (b *FakeBridge) Deliver(ctx context.Context, addr, secret string, req bridge.DeliverRequest) (bridge.EventStream, error) {
	c, ok := b.containerFor(addr)
	if !ok {
		return nil, errors.New("connection refused")
	}
	if err := b.checkSecret(c, secret); err != nil {
		return nil, err
	}
	uid := c.Labels[labelUserID]
	b.mu.Lock()
	b.delivered = append(b.delivered, req)
	script, block := b.Script, b.Block
	b.mu.Unlock()
	events := []bridge.Event{{Type: "accepted"}, {Type: "working", Message: "thinking"}, {Type: "turn_done"}}
	if script != nil {
		events = script(uid, req)
	}
	return &sliceStream{b: b, userID: uid, ctx: ctx, events: events, block: block}, nil
}

func (b *FakeBridge) SetUnhealthy(uid string, v bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.unhealthy[uid] = v
}

func (b *FakeBridge) ShutdownCount() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return len(b.shutdowns)
}

// SetBusy sets the busy count /healthz reports for a user.
func (b *FakeBridge) SetBusy(uid string, n int) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.busy[uid] = n
}

// Delivered returns the deliveries received so far.
func (b *FakeBridge) Delivered() []bridge.DeliverRequest {
	b.mu.Lock()
	defer b.mu.Unlock()
	return append([]bridge.DeliverRequest(nil), b.delivered...)
}
