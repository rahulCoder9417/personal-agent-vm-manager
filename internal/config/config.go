// Package config loads and validates the vm-manager settings from env vars.
// Everything is validated at startup; a bad value stops the process.
package config

import (
	"errors"
	"fmt"
	"log/slog"
	"net/url"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/personal-agent/vm-manager/internal/egress"
)

// DefaultClaudeEgressDomains are the hosts the official Claude Code CLI needs for the model
// API and for claude.ai sign-in / OAuth token exchange and refresh
// (code.claude.com/docs/en/network-config#network-access-requirements).
const DefaultClaudeEgressDomains = "api.anthropic.com,claude.ai,claude.com,platform.claude.com"

// Config holds every setting. Field comments name the env var.
type Config struct {
	// Service
	GRPCAddr       string     // GRPC_ADDR (default ":9090"); bind it to the platform network only.
	InternalSecret string     // VM_INTERNAL_SECRET (required, >= 32 chars)
	DatabaseURL    string     // DATABASE_URL (required)
	LogLevel       slog.Level // LOG_LEVEL (debug|info|warn|error, default info)

	// Agent image
	AgentImage        string // AGENT_IMAGE (default "personal-agent/agent:dev")
	AgentImageVersion string // AGENT_IMAGE_VERSION (default = AGENT_IMAGE); change it to roll out a new image.

	// Container spec
	SandboxNetwork string            // SANDBOX_NETWORK (default "personal-agent_sandbox")
	Runtime        string            // SANDBOX_RUNTIME (default "runsc" = gVisor)
	MemoryBytes    int64             // SANDBOX_MEMORY_MB (default 600)
	NanoCPUs       int64             // SANDBOX_CPUS (default 1.0)
	PidsLimit      int64             // SANDBOX_PIDS_LIMIT (default 256)
	VolumeDriver   string            // SANDBOX_VOLUME_DRIVER (default "local")
	VolumeOpts     map[string]string // SANDBOX_VOLUME_OPTS ("k=v,k=v"; e.g. a size quota for a driver that supports it)
	AgentUID       int               // SANDBOX_AGENT_UID (default 10001): owner of files written into /data
	BridgePort     int               // BRIDGE_PORT (default 8765)

	// Disk limits (soft: checked when a sleeping container is about to wake).
	UserDiskLimitBytes int64   // SANDBOX_DISK_LIMIT_MB (default 2048; 0 turns the per-user limit off)
	DiskGuardPercent   float64 // DISK_GUARD_PERCENT (default 90; 0 turns the server-wide guard off)
	DiskGuardPath      string  // DISK_GUARD_PATH (default "/"): the folder whose disk is measured

	// Scout's browser (a Max-plan feature): a container with the browser turned on gets these
	// limits instead of MemoryBytes and PidsLimit. A headless Chromium needs far more memory and
	// far more processes and threads than the agent alone.
	BrowserMemoryBytes int64 // SANDBOX_BROWSER_MEMORY_MB (default 2048)
	BrowserPidsLimit   int64 // SANDBOX_BROWSER_PIDS_LIMIT (default 1024)

	// Where the container talks to (all through the egress proxy)
	LiteLLMBaseURL string // LITELLM_BASE_URL (default "http://litellm:4000")
	LiteLLMModel   string // LITELLM_MODEL (default "agent-default"): model name Hermes asks LiteLLM for
	// LITELLM_CREW_MODEL (default "agent-crew"): the model name Hermes's crew (Byte, Patch, Scout) asks
	// LiteLLM for, so LiteLLM can tell the crew's calls from the lead's (the crew thinks at medium effort,
	// the lead at low). Set it to LITELLM_MODEL to have the crew call the lead's model, as before.
	LiteLLMCrewModel string
	PlatformMCPURL   string // PLATFORM_MCP_URL (default "http://api:8080/mcp")
	EgressProxyURL   string // EGRESS_PROXY_URL (default "http://172.30.0.2:3128"): HTTP(S)_PROXY in containers

	// Egress allowlists (Squid)
	EgressAllowlistDir      string        // EGRESS_ALLOWLIST_DIR (default "/allowlists")
	EgressProxyContainer    string        // EGRESS_PROXY_CONTAINER (default "egress-proxy"): gets SIGHUP after a change
	EgressReloadMinInterval time.Duration // EGRESS_RELOAD_MIN_INTERVAL (default 1s): coalesces SIGHUPs

	// Claude Code runtime (the user's own Claude plan)
	// CLAUDE_EGRESS_DOMAINS (comma-separated, default DefaultClaudeEgressDomains): added to a user's
	// egress file while their runtime is claude_code or a Claude sign-in / sign-out is in progress.
	ClaudeEgressDomains []string
	ClaudeLoginTTL      time.Duration // CLAUDE_LOGIN_TTL (default 10m): a pending sign-in keeps the sandbox awake this long

	// Capacity and loops
	AwakeCap                    int           // AWAKE_CAP (default 20)
	IdleTimeout                 time.Duration // IDLE_TIMEOUT (default 3m)
	ReaperInterval              time.Duration // REAPER_INTERVAL (default 1m)
	HealthInterval              time.Duration // HEALTH_INTERVAL (default 30s)
	HealthFailuresBeforeRestart int           // HEALTH_FAILURES_BEFORE_RESTART (default 2)
	HealthMaxRestarts           int           // HEALTH_MAX_RESTARTS (default 3): then the sandbox is marked broken
	ReconcileInterval           time.Duration // RECONCILE_INTERVAL (default 5m)
	DeliverTimeout              time.Duration // DELIVER_TIMEOUT (default 15m)
	ShutdownGrace               time.Duration // SHUTDOWN_GRACE (default 20s): polite stop budget
	WakeTimeout                 time.Duration // WAKE_TIMEOUT (default 90s): wait for the Bridge to be healthy

	// Backups
	SnapshotRoot string // SNAPSHOT_ROOT (default "/backups"): SnapshotVolume destinations must be inside it
	// SNAPSHOT_KEEP_DAYS (default 7): archives older than this are deleted. Keep it equal to the
	// backend's BACKUP_KEEP_DAYS, which deletes the database dumps after the same time.
	SnapshotKeepDays      int
	SnapshotPruneInterval time.Duration // SNAPSHOT_PRUNE_INTERVAL (default 1h)
}

// Load reads the config through getenv (os.Getenv in main, a map in tests).
func Load(getenv func(string) string) (*Config, error) {
	p := &parser{getenv: getenv}
	c := &Config{
		GRPCAddr:       p.str("GRPC_ADDR", ":9090"),
		InternalSecret: p.str("VM_INTERNAL_SECRET", ""),
		DatabaseURL:    p.str("DATABASE_URL", ""),
		LogLevel:       p.level("LOG_LEVEL", slog.LevelInfo),

		AgentImage: p.str("AGENT_IMAGE", "personal-agent/agent:dev"),

		SandboxNetwork: p.str("SANDBOX_NETWORK", "personal-agent_sandbox"),
		Runtime:        p.str("SANDBOX_RUNTIME", "runsc"),
		MemoryBytes:    int64(p.int("SANDBOX_MEMORY_MB", 600)) * 1024 * 1024,
		NanoCPUs:       int64(p.float("SANDBOX_CPUS", 1.0) * 1e9),
		PidsLimit:      int64(p.int("SANDBOX_PIDS_LIMIT", 256)),
		VolumeDriver:   p.str("SANDBOX_VOLUME_DRIVER", "local"),
		VolumeOpts:     p.kv("SANDBOX_VOLUME_OPTS"),
		AgentUID:       p.int("SANDBOX_AGENT_UID", 10001),
		BridgePort:     p.int("BRIDGE_PORT", 8765),

		UserDiskLimitBytes: int64(p.int("SANDBOX_DISK_LIMIT_MB", 2048)) * 1024 * 1024,
		DiskGuardPercent:   p.float("DISK_GUARD_PERCENT", 90),
		DiskGuardPath:      p.str("DISK_GUARD_PATH", "/"),

		BrowserMemoryBytes: int64(p.int("SANDBOX_BROWSER_MEMORY_MB", 2048)) * 1024 * 1024,
		BrowserPidsLimit:   int64(p.int("SANDBOX_BROWSER_PIDS_LIMIT", 1024)),

		LiteLLMBaseURL:   p.str("LITELLM_BASE_URL", "http://litellm:4000"),
		LiteLLMModel:     p.str("LITELLM_MODEL", "agent-default"),
		LiteLLMCrewModel: p.str("LITELLM_CREW_MODEL", "agent-crew"),
		PlatformMCPURL:   p.str("PLATFORM_MCP_URL", "http://api:8080/mcp"),
		EgressProxyURL:   p.str("EGRESS_PROXY_URL", "http://172.30.0.2:3128"),

		EgressAllowlistDir:      p.str("EGRESS_ALLOWLIST_DIR", "/allowlists"),
		EgressProxyContainer:    p.str("EGRESS_PROXY_CONTAINER", "egress-proxy"),
		EgressReloadMinInterval: p.dur("EGRESS_RELOAD_MIN_INTERVAL", time.Second),

		ClaudeEgressDomains: p.domains("CLAUDE_EGRESS_DOMAINS", DefaultClaudeEgressDomains),
		ClaudeLoginTTL:      p.dur("CLAUDE_LOGIN_TTL", 10*time.Minute),

		AwakeCap:                    p.int("AWAKE_CAP", 20),
		IdleTimeout:                 p.dur("IDLE_TIMEOUT", 3*time.Minute),
		ReaperInterval:              p.dur("REAPER_INTERVAL", time.Minute),
		HealthInterval:              p.dur("HEALTH_INTERVAL", 30*time.Second),
		HealthFailuresBeforeRestart: p.int("HEALTH_FAILURES_BEFORE_RESTART", 2),
		HealthMaxRestarts:           p.int("HEALTH_MAX_RESTARTS", 3),
		ReconcileInterval:           p.dur("RECONCILE_INTERVAL", 5*time.Minute),
		DeliverTimeout:              p.dur("DELIVER_TIMEOUT", 15*time.Minute),
		ShutdownGrace:               p.dur("SHUTDOWN_GRACE", 20*time.Second),
		WakeTimeout:                 p.dur("WAKE_TIMEOUT", 90*time.Second),

		SnapshotRoot:          p.str("SNAPSHOT_ROOT", "/backups"),
		SnapshotKeepDays:      p.int("SNAPSHOT_KEEP_DAYS", 7),
		SnapshotPruneInterval: p.dur("SNAPSHOT_PRUNE_INTERVAL", time.Hour),
	}
	c.AgentImageVersion = p.str("AGENT_IMAGE_VERSION", c.AgentImage)

	// Validation.
	if c.UserDiskLimitBytes < 0 {
		p.fail("SANDBOX_DISK_LIMIT_MB", "must not be negative")
	}
	if c.DiskGuardPercent < 0 || c.DiskGuardPercent > 100 {
		p.fail("DISK_GUARD_PERCENT", "must be between 0 and 100")
	}
	if len(c.InternalSecret) < 32 {
		p.fail("VM_INTERNAL_SECRET", "is required and must be at least 32 characters")
	}
	if c.DatabaseURL == "" {
		p.fail("DATABASE_URL", "is required")
	}
	for name, v := range map[string]string{
		"LITELLM_BASE_URL": c.LiteLLMBaseURL,
		"PLATFORM_MCP_URL": c.PlatformMCPURL,
		"EGRESS_PROXY_URL": c.EgressProxyURL,
	} {
		if u, err := url.Parse(v); err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
			p.fail(name, "must be an http(s) URL")
		}
	}
	nonEmpty := map[string]string{
		"AGENT_IMAGE": c.AgentImage, "SANDBOX_NETWORK": c.SandboxNetwork, "SANDBOX_RUNTIME": c.Runtime,
		"LITELLM_MODEL": c.LiteLLMModel, "LITELLM_CREW_MODEL": c.LiteLLMCrewModel, "EGRESS_PROXY_CONTAINER": c.EgressProxyContainer,
		"GRPC_ADDR": c.GRPCAddr, "SANDBOX_VOLUME_DRIVER": c.VolumeDriver,
	}
	for name, v := range nonEmpty {
		if strings.TrimSpace(v) == "" {
			p.fail(name, "must not be empty")
		}
	}
	for name, v := range map[string]string{"EGRESS_ALLOWLIST_DIR": c.EgressAllowlistDir, "SNAPSHOT_ROOT": c.SnapshotRoot} {
		if !filepath.IsAbs(v) {
			p.fail(name, "must be an absolute path")
		}
	}
	if c.MemoryBytes < 128*1024*1024 {
		p.fail("SANDBOX_MEMORY_MB", "must be >= 128")
	}
	if c.NanoCPUs <= 0 {
		p.fail("SANDBOX_CPUS", "must be > 0")
	}
	if c.PidsLimit < 32 {
		p.fail("SANDBOX_PIDS_LIMIT", "must be >= 32")
	}
	if c.BrowserMemoryBytes < c.MemoryBytes {
		p.fail("SANDBOX_BROWSER_MEMORY_MB", "must not be below SANDBOX_MEMORY_MB")
	}
	if c.BrowserPidsLimit < c.PidsLimit {
		p.fail("SANDBOX_BROWSER_PIDS_LIMIT", "must not be below SANDBOX_PIDS_LIMIT")
	}
	if c.AgentUID <= 0 {
		p.fail("SANDBOX_AGENT_UID", "must be a non-root uid (> 0)")
	}
	if c.BridgePort <= 0 || c.BridgePort > 65535 {
		p.fail("BRIDGE_PORT", "must be a valid port")
	}
	if c.SnapshotKeepDays < 1 {
		p.fail("SNAPSHOT_KEEP_DAYS", "must be >= 1")
	}
	if c.SnapshotPruneInterval <= 0 {
		p.fail("SNAPSHOT_PRUNE_INTERVAL", "must be > 0")
	}
	if c.AwakeCap < 1 {
		p.fail("AWAKE_CAP", "must be >= 1")
	}
	if c.HealthFailuresBeforeRestart < 1 {
		p.fail("HEALTH_FAILURES_BEFORE_RESTART", "must be >= 1")
	}
	if c.HealthMaxRestarts < 0 {
		p.fail("HEALTH_MAX_RESTARTS", "must be >= 0")
	}
	for name, d := range map[string]time.Duration{
		"IDLE_TIMEOUT": c.IdleTimeout, "REAPER_INTERVAL": c.ReaperInterval, "HEALTH_INTERVAL": c.HealthInterval,
		"RECONCILE_INTERVAL": c.ReconcileInterval, "DELIVER_TIMEOUT": c.DeliverTimeout,
		"SHUTDOWN_GRACE": c.ShutdownGrace, "WAKE_TIMEOUT": c.WakeTimeout, "CLAUDE_LOGIN_TTL": c.ClaudeLoginTTL,
	} {
		if d <= 0 {
			p.fail(name, "must be a positive duration")
		}
	}
	if c.EgressReloadMinInterval < 0 {
		p.fail("EGRESS_RELOAD_MIN_INTERVAL", "must not be negative")
	}

	if len(p.errs) > 0 {
		return nil, errors.Join(p.errs...)
	}
	return c, nil
}

type parser struct {
	getenv func(string) string
	errs   []error
}

func (p *parser) fail(name, msg string) {
	p.errs = append(p.errs, fmt.Errorf("config: %s %s", name, msg))
}

func (p *parser) raw(name string) (string, bool) {
	v := strings.TrimSpace(p.getenv(name))
	return v, v != ""
}

func (p *parser) str(name, def string) string {
	if v, ok := p.raw(name); ok {
		return v
	}
	return def
}

func (p *parser) int(name string, def int) int {
	v, ok := p.raw(name)
	if !ok {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		p.fail(name, "must be an integer")
		return def
	}
	return n
}

func (p *parser) float(name string, def float64) float64 {
	v, ok := p.raw(name)
	if !ok {
		return def
	}
	f, err := strconv.ParseFloat(v, 64)
	if err != nil {
		p.fail(name, "must be a number")
		return def
	}
	return f
}

func (p *parser) dur(name string, def time.Duration) time.Duration {
	v, ok := p.raw(name)
	if !ok {
		return def
	}
	d, err := time.ParseDuration(v)
	if err != nil {
		p.fail(name, "must be a Go duration like 30s or 3m")
		return def
	}
	return d
}

func (p *parser) level(name string, def slog.Level) slog.Level {
	v, ok := p.raw(name)
	if !ok {
		return def
	}
	var l slog.Level
	if err := l.UnmarshalText([]byte(v)); err != nil {
		p.fail(name, "must be debug, info, warn or error")
		return def
	}
	return l
}

// domains parses a comma-separated egress domain list (validated like user allowlists).
func (p *parser) domains(name, def string) []string {
	v := p.str(name, def)
	var in []string
	for _, d := range strings.Split(v, ",") {
		if d = strings.TrimSpace(d); d != "" {
			in = append(in, d)
		}
	}
	out, err := egress.NormalizeDomains(in)
	if err != nil {
		p.fail(name, "must be a comma-separated list of domains: "+err.Error())
		return nil
	}
	return out
}

func (p *parser) kv(name string) map[string]string {
	out := map[string]string{}
	v, ok := p.raw(name)
	if !ok {
		return out
	}
	for _, pair := range strings.Split(v, ",") {
		pair = strings.TrimSpace(pair)
		if pair == "" {
			continue
		}
		k, val, found := strings.Cut(pair, "=")
		if !found || strings.TrimSpace(k) == "" {
			p.fail(name, "must look like key=value,key=value")
			continue
		}
		out[strings.TrimSpace(k)] = strings.TrimSpace(val)
	}
	return out
}
