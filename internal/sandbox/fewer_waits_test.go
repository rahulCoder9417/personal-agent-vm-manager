package sandbox

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	"github.com/personal-agent/vm-manager/internal/bridge"
	"github.com/personal-agent/vm-manager/internal/store"
)

// recordingStore notes the writes the manager makes to the sandboxes table, and can hold the next
// Touch (to see what does not wait for it) or make Touch fail.
type recordingStore struct {
	store.Store
	mu        sync.Mutex
	writes    []string
	holdNext  chan struct{} // the next Touch waits until it is closed
	touchErr  error
	touchDone chan struct{} // closed when a held Touch has finished
}

func (r *recordingStore) note(w string) {
	r.mu.Lock()
	r.writes = append(r.writes, w)
	r.mu.Unlock()
}

func (r *recordingStore) written() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]string(nil), r.writes...)
}

func (r *recordingStore) SetState(ctx context.Context, userID string, s store.State, at time.Time) error {
	r.note("SetState:" + string(s))
	return r.Store.SetState(ctx, userID, s, at)
}

func (r *recordingStore) MarkAwake(ctx context.Context, userID string, at time.Time) error {
	r.note("MarkAwake")
	return r.Store.MarkAwake(ctx, userID, at)
}

func (r *recordingStore) Touch(ctx context.Context, userID string, at time.Time) error {
	r.note("Touch")
	r.mu.Lock()
	hold, done, fail := r.holdNext, r.touchDone, r.touchErr
	r.holdNext = nil
	r.mu.Unlock()
	if hold != nil {
		<-hold
		defer close(done)
	}
	if fail != nil {
		return fail
	}
	return r.Store.Touch(ctx, userID, at)
}

// useStore rebuilds the env's manager over rec (the same Docker, Bridge and egress).
func (e *testEnv) useStore(rec *recordingStore) {
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	e.m = New(e.cfg, rec, e.eng, e.br, e.eg, e.clk, log)
	e.m.PollInterval = 5 * time.Millisecond
}

func count(ws []string, w string) int {
	n := 0
	for _, x := range ws {
		if x == w {
			n++
		}
	}
	return n
}

// A wake that starts the container writes "awake" and the last activity in one round trip.
func TestColdWakeMarksAwakeInOneWrite(t *testing.T) {
	e := newEnv(t)
	e.create(t, "u1")
	rec := &recordingStore{Store: e.st}
	e.useStore(rec)

	sb := e.wake(t, "u1")
	ws := rec.written()
	if count(ws, "MarkAwake") != 1 || count(ws, "SetState:awake") != 0 || count(ws, "Touch") != 0 {
		t.Fatalf("writes of the wake: %v", ws)
	}
	if sb.State != store.StateAwake || !sb.LastActiveAt.Equal(e.clk.Now()) {
		t.Fatalf("the wake's answer: %+v", sb)
	}

	// A wake of an awake container writes nothing of the kind.
	rec.mu.Lock()
	rec.writes = nil
	rec.mu.Unlock()
	e.wake(t, "u1")
	if ws := rec.written(); count(ws, "MarkAwake")+count(ws, "Touch")+count(ws, "SetState:awake") != 0 {
		t.Fatalf("writes of a warm wake: %v", ws)
	}
}

// The Bridge gets the message without waiting for the turn's start to be written as activity; the
// write still happens, and a failing one does not fail the delivery.
func TestDeliverDoesNotWaitForTheActivityWrite(t *testing.T) {
	e := newEnv(t)
	e.create(t, "u1")
	e.wake(t, "u1")
	rec := &recordingStore{Store: e.st}
	e.useStore(rec)
	hold := make(chan struct{})
	rec.holdNext, rec.touchDone = hold, make(chan struct{})
	e.br.Script = func(string, bridge.DeliverRequest) []bridge.Event {
		return []bridge.Event{{Type: "accepted"}, {Type: "turn_done"}}
	}
	// The turn ends only once the background write has started (and is held), so the first Touch is
	// that one and not the one at the turn's end.
	e.br.Block = make(chan struct{})
	go func() {
		for count(rec.written(), "Touch") == 0 {
			time.Sleep(time.Millisecond)
		}
		close(e.br.Block)
	}()

	e.clk.Advance(time.Minute)
	started := e.clk.Now()
	done := make(chan []Event, 1)
	go func() {
		evs, err := collect(t, e.m, deliverReq("u1", "d1"))
		if err != nil {
			t.Errorf("deliver: %v", err)
		}
		done <- evs
	}()
	var evs []Event
	select {
	case evs = <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("the delivery waited for the activity write")
	}
	if types(evs) != "accepted,turn_done" || len(e.br.Delivered()) != 1 {
		t.Fatalf("events %s, delivered %d", types(evs), len(e.br.Delivered()))
	}
	close(hold)
	<-rec.touchDone
	if got, _ := e.st.Get(context.Background(), "u1"); got.LastActiveAt.Before(started) {
		t.Fatalf("the turn's start was not written as activity: %v < %v", got.LastActiveAt, started)
	}

	// The write fails: the delivery goes through all the same.
	e.br.Block = nil
	rec.mu.Lock()
	rec.touchErr = errors.New("database unreachable")
	rec.mu.Unlock()
	evs, err := collect(t, e.m, deliverReq("u1", "d2"))
	if err != nil || types(evs) != "accepted,turn_done" {
		t.Fatalf("deliver with a failing write: %v %s", err, types(evs))
	}
}
