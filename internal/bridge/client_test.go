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
			`{"type":"accepted"}`, ``, `{"type":"working","message":"using tool"}`, `{"type":"turn_done"}`,
		} {
			fmt.Fprintln(w, line)
			fl.Flush()
		}
	}))
	defer srv.Close()

	c := NewClient()
	st, err := c.Deliver(context.Background(), addr(srv), "s3cret", DeliverRequest{DeliveryID: "d1", Kind: "message", Text: "hello"})
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	var types []string
	for {
		ev, err := st.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		types = append(types, ev.Type)
	}
	if strings.Join(types, ",") != "accepted,working,turn_done" {
		t.Fatalf("events: %v", types)
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
			fmt.Fprint(w, `{"ok":true,"busy":2}`)
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
