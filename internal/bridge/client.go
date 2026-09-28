// Package bridge is the vm-manager's HTTP client for the Bridge running inside
// each sandbox container (see README "Bridge contract").
package bridge

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"time"
)

// Header carrying the per-container Bridge secret.
const SecretHeader = "X-Bridge-Secret"

// Event types in the NDJSON stream.
const (
	EventAccepted = "accepted"
	EventWorking  = "working"
	EventTurnDone = "turn_done"
	EventError    = "error"
)

// ErrUnauthorized means the Bridge rejected the secret.
var ErrUnauthorized = errors.New("bridge: unauthorized")

// Attachment is a file the container can fetch through the egress proxy.
type Attachment struct {
	ID       string `json:"id"`
	Name     string `json:"name"`
	MimeType string `json:"mime_type"`
	URL      string `json:"url"`
}

// DeliverRequest is the body of POST /deliver.
type DeliverRequest struct {
	DeliveryID  string          `json:"delivery_id"`
	Kind        string          `json:"kind"` // message | approval_answer | automation | app_event
	SessionKey  string          `json:"session_key"`
	Channel     string          `json:"channel"`
	Text        string          `json:"text"`
	Attachments []Attachment    `json:"attachments"`
	ApprovalID  string          `json:"approval_id"`
	Approved    bool            `json:"approved"`
	Metadata    json.RawMessage `json:"metadata"`
}

// Event is one NDJSON line of the /deliver response.
type Event struct {
	Type          string `json:"type"`
	Message       string `json:"message"`
	PlatformFault bool   `json:"platform_fault"`
}

// Health is the /healthz answer.
type Health struct {
	OK   bool
	Busy int // turns in progress, as reported by the Bridge
}

// Client talks to Bridges on the sandbox network. Never through a proxy.
type Client struct {
	http *http.Client
}

// NewClient builds a Bridge client.
func NewClient() *Client {
	tr := &http.Transport{
		Proxy:                 nil, // direct: the Bridge is on the sandbox network
		DialContext:           (&net.Dialer{Timeout: 5 * time.Second}).DialContext,
		ResponseHeaderTimeout: 60 * time.Second,
		MaxIdleConnsPerHost:   2,
		IdleConnTimeout:       30 * time.Second,
	}
	return &Client{http: &http.Client{Transport: tr}}
}

func url(addr, path string) string { return "http://" + addr + path }

// Health calls GET /healthz (no secret). addr is "ip:port".
func (c *Client) Health(ctx context.Context, addr string) (Health, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url(addr, "/healthz"), nil)
	if err != nil {
		return Health{}, err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return Health{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
		return Health{}, fmt.Errorf("bridge healthz: status %d", resp.StatusCode)
	}
	var body struct {
		Busy int `json:"busy"`
	}
	_ = json.NewDecoder(io.LimitReader(resp.Body, 4096)).Decode(&body) // body is optional
	return Health{OK: true, Busy: body.Busy}, nil
}

// Shutdown calls POST /shutdown and waits (bounded by ctx) for Hermes to save to /data.
func (c *Client) Shutdown(ctx context.Context, addr, secret string) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url(addr, "/shutdown"), nil)
	if err != nil {
		return err
	}
	req.Header.Set(SecretHeader, secret)
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
	switch resp.StatusCode {
	case http.StatusOK:
		return nil
	case http.StatusUnauthorized, http.StatusForbidden:
		return ErrUnauthorized
	default:
		return fmt.Errorf("bridge shutdown: status %d", resp.StatusCode)
	}
}

// EventStream yields Deliver events until io.EOF.
type EventStream interface {
	Next() (Event, error)
	Close() error
}

// Deliver calls POST /deliver and returns the NDJSON event stream.
func (c *Client) Deliver(ctx context.Context, addr, secret string, body DeliverRequest) (EventStream, error) {
	if body.Attachments == nil {
		body.Attachments = []Attachment{}
	}
	if len(body.Metadata) == 0 {
		body.Metadata = json.RawMessage("{}")
	}
	payload, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url(addr, "/deliver"), bytes.NewReader(payload))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/x-ndjson")
	req.Header.Set(SecretHeader, secret)
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusOK {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
		resp.Body.Close()
		if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden {
			return nil, ErrUnauthorized
		}
		return nil, fmt.Errorf("bridge deliver: status %d", resp.StatusCode)
	}
	sc := bufio.NewScanner(resp.Body)
	sc.Buffer(make([]byte, 64*1024), 1024*1024)
	return &Stream{body: resp.Body, sc: sc}, nil
}

// Stream reads NDJSON events.
type Stream struct {
	body io.ReadCloser
	sc   *bufio.Scanner
}

// Next returns the next event, or io.EOF when the Bridge closed the stream.
// Blank lines are skipped; a malformed line is an error.
func (s *Stream) Next() (Event, error) {
	for s.sc.Scan() {
		line := bytes.TrimSpace(s.sc.Bytes())
		if len(line) == 0 {
			continue
		}
		var ev Event
		if err := json.Unmarshal(line, &ev); err != nil {
			return Event{}, fmt.Errorf("bridge: bad event line: %w", err)
		}
		return ev, nil
	}
	if err := s.sc.Err(); err != nil {
		return Event{}, err
	}
	return Event{}, io.EOF
}

// Close closes the response body.
func (s *Stream) Close() error { return s.body.Close() }
