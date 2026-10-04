package bridge

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func addr(srv *httptest.Server) string { return strings.TrimPrefix(srv.URL, "http://") }

func TestDeliverStreamsNDJSON(t *testing.T) {
	var got DeliverRequest
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/deliver" || r.Method != http.MethodPost {
			http.NotFound(w, r)
			return
		}
		if r.Header.Get(SecretHeader) != "s3cret" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		_ = json.NewDecoder(r.Body).Decode(&got)
		w.Header().Set("Content-Type", "application/x-ndjson")
		fl := w.(http.Flusher)
		for _, line := range []string{
			`{"type":"accepted"}`, ``, `{"type":"working","message":"using tool"}`,
			// The last event of a turn may carry the browser time, as the Bridge writes it.
			`{"type":"turn_done","message":"","platform_fault":false,"browser_seconds":73,"browser_report_id":"d1#1"}`,
		} {
			_, _ = fmt.Fprintln(w, line)
			fl.Flush()
		}
	}))
	defer srv.Close()

	c := NewClient()
	st, err := c.Deliver(context.Background(), addr(srv), "s3cret", DeliverRequest{DeliveryID: "d1", Kind: "message", Text: "hello"})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = st.Close() }()
	var types []string
	var last Event
	for {
		ev, err := st.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		types = append(types, ev.Type)
		if ev.Type != EventTurnDone && (ev.BrowserSeconds != 0 || ev.BrowserReportID != "") {
			t.Fatalf("an event without browser time must read as none: %+v", ev)
		}
		last = ev
	}
	if strings.Join(types, ",") != "accepted,working,turn_done" {
		t.Fatalf("events: %v", types)
	}
	if last.BrowserSeconds != 73 || last.BrowserReportID != "d1#1" {
		t.Fatalf("browser time on the last event: %+v", last)
	}
	if got.DeliveryID != "d1" || got.Text != "hello" || got.Attachments == nil || string(got.Metadata) != "{}" {
		t.Fatalf("request body: %+v", got)
	}

	if _, err := c.Deliver(context.Background(), addr(srv), "wrong", DeliverRequest{DeliveryID: "d2"}); !errors.Is(err, ErrUnauthorized) {
		t.Fatalf("want ErrUnauthorized, got %v", err)
	}
}

func TestHealthAndShutdown(t *testing.T) {
	shutdowns := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/healthz":
			_, _ = fmt.Fprint(w, `{"ok":true,"busy":2}`)
		case "/shutdown":
			if r.Header.Get(SecretHeader) != "s3cret" {
				w.WriteHeader(http.StatusUnauthorized)
				return
			}
			shutdowns++
			w.WriteHeader(http.StatusOK)
		}
	}))
	defer srv.Close()
	c := NewClient()
	h, err := c.Health(context.Background(), addr(srv))
	if err != nil || !h.OK || h.Busy != 2 {
		t.Fatalf("health: %+v %v", h, err)
	}
	if err := c.Shutdown(context.Background(), addr(srv), "s3cret"); err != nil || shutdowns != 1 {
		t.Fatalf("shutdown: %v", err)
	}
	if err := c.Shutdown(context.Background(), addr(srv), "nope"); !errors.Is(err, ErrUnauthorized) {
		t.Fatalf("want ErrUnauthorized, got %v", err)
	}
}

func TestHealthFailsOnNon200(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer srv.Close()
	if _, err := NewClient().Health(context.Background(), addr(srv)); err == nil {
		t.Fatal("expected an error")
	}
}

func TestHealthReportsClaudeFields(t *testing.T) {
	body := `{"ok":true,"busy":0,"runtime":"claude_code","claude_connected":true,"claude_login_pending":true}`
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = fmt.Fprint(w, body) }))
	defer srv.Close()
	h, err := NewClient().Health(context.Background(), addr(srv))
	if err != nil || h.Runtime != "claude_code" || h.ClaudeConnected == nil || !*h.ClaudeConnected || !h.ClaudeLoginPending {
		t.Fatalf("health: %+v %v", h, err)
	}
	body = `{"ok":true,"busy":0}` // older image: status unknown
	h, _ = NewClient().Health(context.Background(), addr(srv))
	if h.ClaudeConnected != nil || h.ClaudeLoginPending {
		t.Fatalf("older bridge: %+v", h)
	}
}

func TestClaudeSignIn(t *testing.T) {
	const code = "the-code-from-anthropic"
	var gotCode string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get(SecretHeader) != "s3cret" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		switch r.URL.Path {
		case "/claude/login/start":
			_, _ = fmt.Fprint(w, `{"login_id":"L1","authorize_url":"https://claude.ai/oauth/authorize?x=1","expires_at":1700000000000}`)
		case "/claude/login/complete":
			var b struct {
				LoginID string `json:"login_id"`
				Code    string `json:"code"`
			}
			_ = json.NewDecoder(r.Body).Decode(&b)
			switch {
			case b.LoginID != "L1":
				w.WriteHeader(http.StatusConflict)
			case b.Code == "":
				w.WriteHeader(http.StatusBadRequest)
			default:
				gotCode = b.Code
				_, _ = fmt.Fprint(w, `{"connected":true,"message":"Claude plan connected"}`)
			}
		case "/claude/logout":
			_, _ = fmt.Fprint(w, `{"ok":true,"claude_connected":false}`)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer srv.Close()
	c := NewClient()
	ctx := context.Background()
	l, err := c.ClaudeLoginStart(ctx, addr(srv), "s3cret")
	if err != nil || l.LoginID != "L1" || l.ExpiresAt != 1700000000000 || !strings.HasPrefix(l.AuthorizeURL, "https://claude.ai/") {
		t.Fatalf("start: %+v %v", l, err)
	}
	res, err := c.ClaudeLoginComplete(ctx, addr(srv), "s3cret", "L1", code)
	if err != nil || !res.Connected || gotCode != code {
		t.Fatalf("complete: %+v %v", res, err)
	}
	if _, err := c.ClaudeLoginComplete(ctx, addr(srv), "s3cret", "L2", code); !errors.Is(err, ErrLoginNotFound) {
		t.Fatalf("unknown login: %v", err)
	} else if strings.Contains(err.Error(), code) {
		t.Fatal("errors must not carry the code")
	}
	if _, err := c.ClaudeLoginComplete(ctx, addr(srv), "s3cret", "L1", ""); !errors.Is(err, ErrBadRequest) {
		t.Fatalf("bad request: %v", err)
	}
	if _, err := c.ClaudeLoginStart(ctx, addr(srv), "wrong"); !errors.Is(err, ErrUnauthorized) {
		t.Fatalf("secret: %v", err)
	}
	if connected, err := c.ClaudeLogout(ctx, addr(srv), "s3cret"); err != nil || connected {
		t.Fatalf("logout: %v %v", connected, err)
	}
	// An older image without the endpoints.
	old := httptest.NewServer(http.NotFoundHandler())
	defer old.Close()
	if _, err := c.ClaudeLoginStart(ctx, addr(old), "s3cret"); !errors.Is(err, ErrClaudeUnavailable) {
		t.Fatalf("old image: %v", err)
	}
}
