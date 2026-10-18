// Package sandbox is the vm-manager's core: the per-user container state
// machine (create, wake, deliver, stop, delete), the capacity guard and the
// background loops (idle reaper, health checker, reconciler).
package sandbox

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/url"
	"regexp"
	"strconv"
	"sync"
	"time"

	"github.com/personal-agent/vm-manager/internal/bridge"
	"github.com/personal-agent/vm-manager/internal/clock"
	"github.com/personal-agent/vm-manager/internal/config"
	"github.com/personal-agent/vm-manager/internal/docker"
	"github.com/personal-agent/vm-manager/internal/egress"
	"github.com/personal-agent/vm-manager/internal/ids"
	"github.com/personal-agent/vm-manager/internal/platformfile"
	"github.com/personal-agent/vm-manager/internal/store"
)

// Errors returned to the gRPC layer (mapped to status codes there).
var (
	ErrNotFound    = errors.New("sandbox not found")          // NOT_FOUND
	ErrInvalid     = errors.New("invalid argument")           // INVALID_ARGUMENT
	ErrCapacity    = errors.New("awake cap reached")          // RESOURCE_EXHAUSTED (retryable)
	ErrBusy        = errors.New("sandbox is in a turn")       // FAILED_PRECONDITION
	ErrUnavailable = errors.New("sandbox could not be woken") // UNAVAILABLE
	// ErrDiskFull: the server disk is over the guard percent, so no sleeping container is woken.
	// RESOURCE_EXHAUSTED (the proto has no better code).
	ErrDiskFull = errors.New("server disk is nearly full")
	// ErrStorageLimit: the user's folder is over its size limit, so the container is not woken.
	// RESOURCE_EXHAUSTED (the proto has no better code).
	ErrStorageLimit = errors.New("user storage limit reached")
	// ErrLoginsFull: CLAUDE_LOGINS_MAX Claude sign-ins already wait on the server, each holding an
	// awake place. RESOURCE_EXHAUSTED (retryable: one ends within CLAUDE_LOGIN_TTL).
	ErrLoginsFull = errors.New("too many Claude sign-ins are waiting")
	// ErrLoginNotFound: no pending Claude sign-in with that id (unknown, expired, already used,
	// or the sandbox stopped since). FAILED_PRECONDITION: start a new sign-in.
	ErrLoginNotFound = errors.New("claude sign-in not found or expired")
	// ErrAgentUnavailable: the Bridge (or Claude Code in it) could not be reached. UNAVAILABLE.
	ErrAgentUnavailable = errors.New("the agent could not be reached")
)

func invalid(format string, args ...any) error {
	return fmt.Errorf("%w: %s", ErrInvalid, fmt.Sprintf(format, args...))
}

// Priorities (same numbers as the proto enum). Lower is evicted first.
const (
	PriorityLow    int32 = 1
	PriorityNormal int32 = 2
	PriorityHigh   int32 = 3
)

// Docker labels on every managed container / volume.
const (
	LabelManaged      = "pa.managed"
	LabelUserID       = "pa.user_id"
	LabelImageVersion = "pa.image_version"
	// LabelBrowser is "true" on a container created with Scout's browser turned on (more memory,
	// more processes). A container without the label has no browser.
	LabelBrowser = "pa.browser"
)

// BridgeAPI is what the manager needs from the Bridge client (faked in tests).
type BridgeAPI interface {
	Health(ctx context.Context, addr string) (bridge.Health, error)
	Shutdown(ctx context.Context, addr, secret string) error
	Deliver(ctx context.Context, addr, secret string, req bridge.DeliverRequest) (bridge.EventStream, error)
	ClaudeLoginStart(ctx context.Context, addr, secret string) (bridge.ClaudeLogin, error)
	ClaudeLoginComplete(ctx context.Context, addr, secret, loginID, code string) (bridge.ClaudeLoginResult, error)
	ClaudeLogout(ctx context.Context, addr, secret string) (bool, error)
}

// Egress is what the manager needs from the allowlist writer.
type Egress interface {
	Write(userID, ip string, rule egress.Rule) error
	Remove(userID string) error
	Users() ([]string, error)
}

// Manager owns every sandbox. All Docker access goes through it.
type Manager struct {
	cfg   *config.Config
	store store.Store
	eng   docker.Engine
	br    BridgeAPI
	eg    Egress
	clk   clock.Clock
	log   *slog.Logger

	// diskUsedPercent reports how full the server disk is (tests replace it).
	diskUsedPercent func(path string) (float64, error)

	// PollInterval is how often Wake polls the Bridge health while starting.
	PollInterval time.Duration
	// reaperLockWait is how long the idle reaper waits for a user's lock that another short operation
	// (a health check, the reconciler) holds, before it leaves that sandbox to its next round.
	reaperLockWait time.Duration

	locksMu sync.Mutex
	locks   map[string]*sync.Mutex // per-user lifecycle lock

	capMu sync.Mutex // serialises capacity decisions

	mu          sync.Mutex           // guards the maps below
	secrets     map[string]string    // user -> Bridge secret (memory only, loaded from /data)
	inflight    map[string]int       // user -> Deliver calls in progress
	healthFails map[string]int       // user -> consecutive failed health checks
	restarts    map[string]int       // user -> restarts since the last good check
	logins      map[string]time.Time // user -> pending Claude sign-in expiry (keeps the sandbox awake)
	claudeOps   map[string]int       // user -> Claude sign-out calls in progress (need Anthropic's hosts)
}

// New builds a Manager.
func New(cfg *config.Config, st store.Store, eng docker.Engine, br BridgeAPI, eg Egress, clk clock.Clock, log *slog.Logger) *Manager {
	return &Manager{
		cfg: cfg, store: st, eng: eng, br: br, eg: eg, clk: clk, log: log,
		diskUsedPercent: diskUsedPercent,
		PollInterval:    250 * time.Millisecond,
		// A health check holds the lock for an inspect and one /healthz call (5 s timeout).
		reaperLockWait: 10 * time.Second,
		locks:          map[string]*sync.Mutex{},
		secrets:        map[string]string{},
		inflight:       map[string]int{},
		healthFails:    map[string]int{},
		restarts:       map[string]int{},
		logins:         map[string]time.Time{},
		claudeOps:      map[string]int{},
	}
}

func (m *Manager) userLock(userID string) *sync.Mutex {
	m.locksMu.Lock()
	defer m.locksMu.Unlock()
	l, ok := m.locks[userID]
	if !ok {
		l = &sync.Mutex{}
		m.locks[userID] = l
	}
	return l
}

func (m *Manager) inflightCount(userID string) int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.inflight[userID]
}

func (m *Manager) addInflight(userID string, d int) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.inflight[userID] += d
	if m.inflight[userID] <= 0 {
		delete(m.inflight, userID)
	}
}

func (m *Manager) forget(userID string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	delete(m.secrets, userID)
	delete(m.healthFails, userID)
	delete(m.restarts, userID)
	delete(m.logins, userID)
	delete(m.claudeOps, userID)
}

func (m *Manager) resetHealth(userID string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	delete(m.healthFails, userID)
	delete(m.restarts, userID)
}

func normPriority(p int32) int32 {
	if p < PriorityLow || p > PriorityHigh {
		return PriorityLow // unspecified: treated as the lowest (free) priority
	}
	return p
}

func validateUserID(userID string) error {
	if !ids.ValidUserID(userID) {
		return invalid("user_id must match [A-Za-z0-9][A-Za-z0-9_-]{0,127}")
	}
	return nil
}

var headerNameRe = regexp.MustCompile(`^[A-Za-z0-9!#$%&'*+.^_|~-]{1,128}$`)

// MCPServer is one of the user's own MCP servers (from the backend).
type MCPServer = platformfile.MCPServer

func validateMCPServers(servers []MCPServer) error {
	if len(servers) > 50 {
		return invalid("too many mcp servers")
	}
	seen := map[string]bool{}
	for _, s := range servers {
		if s.ID == "" || seen[s.ID] {
			return invalid("mcp server id must be set and unique")
		}
		seen[s.ID] = true
		u, err := url.Parse(s.URL)
		if err != nil || (u.Scheme != "https" && u.Scheme != "http") || u.Host == "" || u.User != nil {
			return invalid("mcp server %s: url must be http(s) without credentials", s.ID)
		}
		if s.AuthHeaderName != "" && !headerNameRe.MatchString(s.AuthHeaderName) {
			return invalid("mcp server %s: bad auth header name", s.ID)
		}
	}
	return nil
}

// spec builds the container spec for a user. The container gets only its own,
// non-secret settings in env; its secrets are in /data/.platform/platform.json.
// browser: the container is created for Scout's browser, with the browser's memory and
// process limits and the LabelBrowser label (the limits can't change on a running container).
func (m *Manager) spec(userID string, browser bool) docker.ContainerSpec {
	c := m.cfg
	proxy := c.EgressProxyURL
	noProxy := "localhost,127.0.0.1,::1"
	labels := map[string]string{
		LabelManaged:      "true",
		LabelUserID:       userID,
		LabelImageVersion: c.AgentImageVersion,
	}
	memory, pids := c.MemoryBytes, c.PidsLimit
	if browser {
		labels[LabelBrowser] = "true"
		memory, pids = c.BrowserMemoryBytes, c.BrowserPidsLimit
	}
	return docker.ContainerSpec{
		Name:  ids.ContainerName(userID),
		Image: c.AgentImage,
		Env: []string{
			"USER_ID=" + userID,
			"HOME=/data/home",
			"HERMES_HOME=/data/hermes",
			"TMPDIR=/data/tmp",
			"PLATFORM_FILE=" + platformfile.Path,
			"BRIDGE_PORT=" + strconv.Itoa(c.BridgePort),
			"LITELLM_BASE_URL=" + c.LiteLLMBaseURL,
			"LITELLM_MODEL=" + c.LiteLLMModel,
			"LITELLM_CREW_MODEL=" + c.LiteLLMCrewModel,
			"PLATFORM_MCP_URL=" + c.PlatformMCPURL,
			"HTTP_PROXY=" + proxy, "HTTPS_PROXY=" + proxy, "http_proxy=" + proxy, "https_proxy=" + proxy,
			"NO_PROXY=" + noProxy, "no_proxy=" + noProxy,
			"PYTHONUNBUFFERED=1",
		},
		Labels:         labels,
		Runtime:        c.Runtime,
		MemoryBytes:    memory,
		NanoCPUs:       c.NanoCPUs,
		PidsLimit:      pids,
		Network:        c.SandboxNetwork,
		VolumeName:     ids.VolumeName(userID),
		VolumeTarget:   platformfile.DataDir,
		User:           fmt.Sprintf("%d:%d", c.AgentUID, c.AgentUID),
		ReadonlyRootfs: true,
		Hostname:       "agent",
	}
}

func (m *Manager) bridgeAddr(ip string) string {
	return net.JoinHostPort(ip, strconv.Itoa(m.cfg.BridgePort))
}

// detached keeps lifecycle work going even if the caller's RPC is cancelled,
// so a sandbox is never left half-started or half-stopped.
func detached(ctx context.Context, d time.Duration) (context.Context, context.CancelFunc) {
	return context.WithTimeout(context.WithoutCancel(ctx), d)
}

func (m *Manager) getSandbox(ctx context.Context, userID string) (store.Sandbox, error) {
	sb, err := m.store.Get(ctx, userID)
	if errors.Is(err, store.ErrNotFound) {
		return store.Sandbox{}, ErrNotFound
	}
	return sb, err
}
