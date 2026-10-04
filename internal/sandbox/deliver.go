package sandbox

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"time"

	"github.com/personal-agent/vm-manager/internal/bridge"
	"github.com/personal-agent/vm-manager/internal/store"
)

// DeliverRequest is the input of Deliver.
type DeliverRequest struct {
	UserID       string
	Bridge       bridge.DeliverRequest // delivery_id, kind, session_key, text, ...
	MetadataJSON string
	Timeout      time.Duration // 0 = cfg.DeliverTimeout
}

// Event is one Deliver stream event.
type Event struct {
	Type          string // bridge.EventAccepted | EventWorking | EventTurnDone | EventError
	Message       string
	PlatformFault bool
	At            time.Time
}

// Deliver wakes the sandbox if needed, posts the message to the Bridge and
// relays its events to send until turn_done or error. The turn keeps being
// tracked (busy state, timeout) even if the caller goes away. On timeout the
// container is force-stopped and an ERROR event with platform_fault is sent.
//
// Errors returned before any event (not found, capacity, invalid) are for the
// caller to map to gRPC codes; failures during the turn are ERROR events.
func (m *Manager) Deliver(ctx context.Context, req DeliverRequest, send func(Event) error) error {
	if err := validateUserID(req.UserID); err != nil {
		return err
	}
	if req.Bridge.DeliveryID == "" || len(req.Bridge.DeliveryID) > 255 {
		return invalid("delivery_id is required (max 255 chars)")
	}
	if req.Bridge.Kind == "" {
		return invalid("kind is required")
	}
	if req.MetadataJSON != "" {
		if !json.Valid([]byte(req.MetadataJSON)) {
			return invalid("metadata_json must be valid JSON")
		}
		req.Bridge.Metadata = json.RawMessage(req.MetadataJSON)
	}
	timeout := req.Timeout
	if timeout <= 0 {
		timeout = m.cfg.DeliverTimeout
	}

	sb, _, err := m.wake(ctx, req.UserID, 0, true) // counts this delivery as in flight
	if err != nil {
		return err
	}
	userID := req.UserID
	if err := m.store.Touch(ctx, userID, m.clk.Now()); err != nil {
		m.addInflight(userID, -1)
		return err
	}

	events := make(chan Event, 16)
	consumerGone := make(chan struct{})
	go m.relay(ctx, sb, req.Bridge, timeout, events, consumerGone)

	var sendErr error
	for ev := range events {
		if sendErr = send(ev); sendErr != nil {
			close(consumerGone) // the relay keeps tracking the turn in the background
			return sendErr
		}
	}
	close(consumerGone)
	return nil
}

// relay runs one turn against the Bridge. It owns the in-flight count.
func (m *Manager) relay(parent context.Context, sb store.Sandbox, body bridge.DeliverRequest, timeout time.Duration,
	events chan<- Event, consumerGone <-chan struct{}) {
	userID := sb.UserID
	defer close(events)
	defer m.addInflight(userID, -1)

	// Independent of the RPC: the turn is tracked to the end even if the caller disconnects.
	runCtx, cancel := context.WithTimeout(context.WithoutCancel(parent), timeout)
	defer cancel()
	bg := context.WithoutCancel(parent)

	emit := func(typ, msg string, fault bool) {
		ev := Event{Type: typ, Message: msg, PlatformFault: fault, At: m.clk.Now()}
		select {
		case events <- ev:
		case <-consumerGone:
		}
	}
	// endTurn marks the sandbox not busy and records activity.
	endTurn := func() {
		now := m.clk.Now()
		if cur, err := m.store.Get(bg, userID); err == nil && cur.State == store.StateBusy {
			_ = m.store.SetState(bg, userID, store.StateAwake, now)
		}
		_ = m.store.Touch(bg, userID, now)
	}
	timedOut := func() {
		m.log.Error("turn timed out; force-stopping sandbox", "user_id", userID, "delivery_id", body.DeliveryID,
			"timeout", timeout.String(), "alert", true)
		m.forceStopAfterTimeout(bg, userID)
		emit(bridge.EventError, fmt.Sprintf("turn timed out after %s; the agent was stopped", timeout), true)
	}

	c, err := m.eng.InspectContainer(runCtx, sb.ContainerID)
	ip := c.IPs[m.cfg.SandboxNetwork]
	if err != nil || ip == "" {
		endTurn()
		emit(bridge.EventError, "sandbox is not reachable", true)
		return
	}
	secret, err := m.bridgeSecret(runCtx, sb)
	if err != nil {
		endTurn()
		emit(bridge.EventError, "sandbox secrets unavailable", true)
		return
	}

	stream, err := m.br.Deliver(runCtx, m.bridgeAddr(ip), secret, body)
	if err != nil {
		if errors.Is(runCtx.Err(), context.DeadlineExceeded) {
			timedOut()
			return
		}
		m.log.Warn("bridge deliver failed", "user_id", userID, "delivery_id", body.DeliveryID, "err", err)
		endTurn()
		emit(bridge.EventError, "could not reach the agent", true)
		return
	}
	defer func() { _ = stream.Close() }()

	for {
		ev, err := stream.Next()
		if err != nil {
			if errors.Is(runCtx.Err(), context.DeadlineExceeded) {
				timedOut()
				return
			}
			endTurn()
			if errors.Is(err, io.EOF) {
				emit(bridge.EventError, "the agent ended the turn without finishing", true)
			} else {
				emit(bridge.EventError, "lost the connection to the agent", true)
			}
			return
		}
		switch ev.Type {
		case bridge.EventAccepted:
			_ = m.store.SetState(bg, userID, store.StateBusy, m.clk.Now())
			emit(ev.Type, ev.Message, false)
		case bridge.EventWorking:
			emit(ev.Type, ev.Message, false)
		case bridge.EventTurnDone:
			// Browser time first: it is stored before the caller hears that the turn is over, so
			// a usage sync that follows the turn already sees it.
			m.recordBrowser(bg, userID, body.DeliveryID, ev)
			endTurn()
			emit(ev.Type, ev.Message, false)
			return
		case bridge.EventError:
			m.recordBrowser(bg, userID, body.DeliveryID, ev)
			endTurn()
			emit(ev.Type, ev.Message, ev.PlatformFault)
			return
		default:
			m.log.Debug("ignoring unknown bridge event", "type", ev.Type)
		}
	}
}

// forceStopAfterTimeout force-stops a stuck sandbox (it is in a turn, so the
// normal "not busy" rule is bypassed on purpose).
func (m *Manager) forceStopAfterTimeout(ctx context.Context, userID string) {
	l := m.userLock(userID)
	l.Lock()
	defer l.Unlock()
	ctx, cancel := detached(ctx, time.Minute)
	defer cancel()
	sb, err := m.store.Get(ctx, userID)
	if err != nil {
		return
	}
	if err := m.stopLocked(ctx, sb, true, "turn timeout", store.StateStopped); err != nil {
		m.log.Error("force stop after timeout failed", "user_id", userID, "err", err, "alert", true)
	}
}
