// Package sandboxtest has test doubles shared by the sandbox and grpcserver tests.
package sandboxtest

import (
	"context"
	"errors"
	"fmt"
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
	// OnHealth runs at the start of each /healthz call (lets tests make a health check slow).
	OnHealth func(userID string)

	// Claude Code sign-in, per user (like the Bridge's `claude auth login | logout`).
	logins    map[string]string // user -> pending login id
	connected map[string]bool   // user -> signed in
	codes     []string          // every code received (tests check where it went)
	loginSeq  int
	// ClaudeDown makes the Claude endpoints fail like an unreachable Bridge.
	ClaudeDown bool
	// ReportClaude makes /healthz report claude_connected (newer images do).
	ReportClaude bool
}

// GoodCode is the only code the fake sign-in accepts.
const GoodCode = "good-code-123"

func NewFakeBridge(eng *docker.Fake) *FakeBridge {
	return &FakeBridge{eng: eng, unhealthy: map[string]bool{}, busy: map[string]int{},
		logins: map[string]string{}, connected: map[string]bool{}, ReportClaude: true}
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
	if b.OnHealth != nil {
		b.OnHealth(uid)
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.unhealthy[uid] {
		return bridge.Health{}, errors.New("healthz: 503")
	}
	h := bridge.Health{OK: true, Busy: b.busy[uid], ClaudeLoginPending: b.logins[uid] != ""}
	if b.ReportClaude {
		v := b.connected[uid]
		h.ClaudeConnected = &v
	}
	return h, nil
}

func (b *FakeBridge) claudeUser(addr, secret string) (string, error) {
	c, ok := b.containerFor(addr)
	if !ok {
		return "", errors.New("connection refused")
	}
	if err := b.checkSecret(c, secret); err != nil {
		return "", err
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.ClaudeDown {
		return "", errors.New("dial tcp: connection refused")
	}
	return c.Labels[labelUserID], nil
}

func (b *FakeBridge) ClaudeLoginStart(_ context.Context, addr, secret string) (bridge.ClaudeLogin, error) {
	uid, err := b.claudeUser(addr, secret)
	if err != nil {
		return bridge.ClaudeLogin{}, err
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	b.loginSeq++
	id := fmt.Sprintf("login-%d", b.loginSeq)
	b.logins[uid] = id // a new start replaces the pending one
	return bridge.ClaudeLogin{LoginID: id, AuthorizeURL: "https://claude.ai/oauth/authorize?state=" + id,
		ExpiresAt: 1_900_000_000_000}, nil
}

func (b *FakeBridge) ClaudeLoginComplete(_ context.Context, addr, secret, loginID, code string) (bridge.ClaudeLoginResult, error) {
	uid, err := b.claudeUser(addr, secret)
	if err != nil {
		return bridge.ClaudeLoginResult{}, err
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.logins[uid] == "" || b.logins[uid] != loginID {
		return bridge.ClaudeLoginResult{}, bridge.ErrLoginNotFound
	}
	delete(b.logins, uid)
	b.codes = append(b.codes, code)
	if code != GoodCode {
		return bridge.ClaudeLoginResult{Connected: false, Message: "Sign-in did not complete."}, nil
	}
	b.connected[uid] = true
	return bridge.ClaudeLoginResult{Connected: true, Message: "Claude plan connected"}, nil
}

func (b *FakeBridge) ClaudeLogout(_ context.Context, addr, secret string) (bool, error) {
	uid, err := b.claudeUser(addr, secret)
	if err != nil {
		return false, err
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	delete(b.logins, uid)
	b.connected[uid] = false
	return false, nil
}

// ExpireLogin drops a user's pending sign-in (the Bridge's 10-minute expiry).
func (b *FakeBridge) ExpireLogin(uid string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	delete(b.logins, uid)
}

// SetClaudeConnected sets what /healthz reports as claude_connected.
func (b *FakeBridge) SetClaudeConnected(uid string, v bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.connected[uid] = v
}

// Codes returns every sign-in code the fake Bridge received.
func (b *FakeBridge) Codes() []string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return append([]string(nil), b.codes...)
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
