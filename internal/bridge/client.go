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
	// EventTextDelta carries the whole draft of the answer so far in Message. The Bridge sends it
	// only when the delivery's metadata has "stream_text": true.
	EventTextDelta = "text_delta"
)

// ErrUnauthorized means the Bridge rejected the secret.
var ErrUnauthorized = errors.New("bridge: unauthorized")

// ErrLoginNotFound means the Bridge has no pending Claude sign-in with that id (unknown,
// expired or already used).
var ErrLoginNotFound = errors.New("bridge: unknown or expired claude sign-in")

// ErrBadRequest means the Bridge rejected the request body (e.g. a code that is not one line).
var ErrBadRequest = errors.New("bridge: bad request")

// ErrClaudeUnavailable means Claude Code could not do it (not installed in this image, or the
// CLI did not start the sign-in).
var ErrClaudeUnavailable = errors.New("bridge: claude code unavailable")

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
	// BrowserSeconds, on the last event of a turn (turn_done or error): how long Scout's browser
	// was open since the Bridge's previous report, in whole seconds. 0 or missing when the
	// browser never started. BrowserReportID names the report, so the same one is counted once.
	BrowserSeconds  int64  `json:"browser_seconds,omitempty"`
	BrowserReportID string `json:"browser_report_id,omitempty"`
	// Timings, on the last event of a turn: the Bridge's own stage times (a JSON object of
	// milliseconds since it took the delivery; README "Stage times"). Passed on as it is.
	Timings json.RawMessage `json:"timings,omitempty"`
}

// Health is the /healthz answer.
type Health struct {
	OK      bool
	Busy    int    // turns in progress, as reported by the Bridge
	Runtime string // runtime the Bridge will use for the next delivery ("" from older images)
	// ClaudeConnected is Claude Code's sign-in status (from `claude auth status`); nil when
	// the Bridge did not report it (older image).
	ClaudeConnected *bool
	// ClaudeLoginPending is true while a Claude sign-in waits for its code.
	ClaudeLoginPending bool
}

// ClaudeLogin is a started Claude Code sign-in.
type ClaudeLogin struct {
	LoginID      string `json:"login_id"`
	AuthorizeURL string `json:"authorize_url"`
	ExpiresAt    int64  `json:"expires_at"` // unix ms
}

// ClaudeLoginResult is the outcome of handing the code to the waiting sign-in.
type ClaudeLoginResult struct {
	Connected bool   `json:"connected"`
	Message   string `json:"message"`
}

// Client talks to Bridges on the sandbox network. Never through a proxy.
type Client struct {
	http *http.Client
	slow *http.Client // Claude sign-in calls wait on the CLI (up to ~2 min)
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
	slowTr := tr.Clone()
	slowTr.ResponseHeaderTimeout = 3 * time.Minute
	return &Client{http: &http.Client{Transport: tr}, slow: &http.Client{Transport: slowTr}}
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
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
		return Health{}, fmt.Errorf("bridge healthz: status %d", resp.StatusCode)
	}
	var body struct {
		Busy               int    `json:"busy"`
		Runtime            string `json:"runtime"`
		ClaudeConnected    *bool  `json:"claude_connected"`
		ClaudeLoginPending bool   `json:"claude_login_pending"`
	}
	_ = json.NewDecoder(io.LimitReader(resp.Body, 4096)).Decode(&body) // body is optional
	return Health{OK: true, Busy: body.Busy, Runtime: body.Runtime, ClaudeConnected: body.ClaudeConnected,
		ClaudeLoginPending: body.ClaudeLoginPending}, nil
}

// postJSON posts body (nil = empty object) with the secret and decodes a 200 answer into out.
// Errors carry only the status, never the request body (it may hold a sign-in code).
func (c *Client) postJSON(ctx context.Context, hc *http.Client, addr, path, secret string, body, out any) error {
	if body == nil {
		body = struct{}{}
	}
	payload, err := json.Marshal(body)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url(addr, path), bytes.NewReader(payload))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set(SecretHeader, secret)
	resp, err := hc.Do(req)
	if err != nil {
		return fmt.Errorf("bridge %s: %w", path, err)
	}
	defer func() { _ = resp.Body.Close() }()
	switch resp.StatusCode {
	case http.StatusOK:
		if out == nil {
			_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
			return nil
		}
		if err := json.NewDecoder(io.LimitReader(resp.Body, 64*1024)).Decode(out); err != nil {
			return fmt.Errorf("bridge %s: bad answer: %w", path, err)
		}
		return nil
	case http.StatusUnauthorized, http.StatusForbidden:
		return ErrUnauthorized
	case http.StatusConflict:
		return ErrLoginNotFound
	case http.StatusBadRequest:
		return ErrBadRequest
	case http.StatusNotFound, http.StatusBadGateway, http.StatusServiceUnavailable:
		return fmt.Errorf("%w (status %d)", ErrClaudeUnavailable, resp.StatusCode)
	default:
		return fmt.Errorf("bridge %s: status %d", path, resp.StatusCode)
	}
}

// ClaudeLoginStart calls POST /claude/login/start: the Bridge starts the official
// `claude auth login` and returns Anthropic's authorize URL.
func (c *Client) ClaudeLoginStart(ctx context.Context, addr, secret string) (ClaudeLogin, error) {
	var out ClaudeLogin
	if err := c.postJSON(ctx, c.slow, addr, "/claude/login/start", secret, nil, &out); err != nil {
		return ClaudeLogin{}, err
	}
	if out.LoginID == "" || out.AuthorizeURL == "" {
		return ClaudeLogin{}, fmt.Errorf("%w: incomplete answer", ErrClaudeUnavailable)
	}
	return out, nil
}

// ClaudeLoginComplete calls POST /claude/login/complete: the Bridge writes the code to the
// waiting CLI and reports whether Claude Code is now signed in. The code is never logged.
func (c *Client) ClaudeLoginComplete(ctx context.Context, addr, secret, loginID, code string) (ClaudeLoginResult, error) {
	var out ClaudeLoginResult
	body := struct {
		LoginID string `json:"login_id"`
		Code    string `json:"code"`
	}{loginID, code}
	err := c.postJSON(ctx, c.slow, addr, "/claude/login/complete", secret, body, &out)
	return out, err
}

// ClaudeLogout calls POST /claude/logout (the official `claude auth logout`) and returns
// the sign-in status afterwards.
func (c *Client) ClaudeLogout(ctx context.Context, addr, secret string) (bool, error) {
	var out struct {
		ClaudeConnected bool `json:"claude_connected"`
	}
	err := c.postJSON(ctx, c.slow, addr, "/claude/logout", secret, nil, &out)
	return out.ClaudeConnected, err
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
	defer func() { _ = resp.Body.Close() }()
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
		_ = resp.Body.Close()
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
