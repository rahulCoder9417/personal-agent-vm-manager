// Package egress writes one Squid allowlist file per awake user and asks the
// egress proxy to reload.
//
// Contract with the backend's infra: the base squid.conf contains
// `include /etc/squid/allowlists/*.conf` (EGRESS_ALLOWLIST_DIR is mounted there)
// and already lets the whole sandbox subnet reach LiteLLM and the api /mcp.
// Each <user_id>.conf only adds that user's own domains for that container's IP.
package egress

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/personal-agent/vm-manager/internal/ids"
)

// MaxDomains is the most domains one user may allowlist.
const MaxDomains = 200

// placeholder keeps the include glob matching at least one file.
const placeholder = "_vm-manager.conf"

var labelRe = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$`)

// NormalizeDomains validates and canonicalises allowlist entries into Squid
// dstdomain syntax. "*.example.com" and ".example.com" mean the domain and all
// subdomains; "example.com" means exactly that host. Entries covered by a
// wildcard are dropped (Squid complains about overlapping dstdomain entries).
// Anything that is not a plain public-looking hostname is rejected, because
// the value ends up in the proxy config.
func NormalizeDomains(in []string) ([]string, error) {
	if len(in) > MaxDomains {
		return nil, fmt.Errorf("too many domains (%d > %d)", len(in), MaxDomains)
	}
	set := map[string]bool{}
	for _, raw := range in {
		d := strings.ToLower(strings.TrimSpace(raw))
		d = strings.TrimSuffix(d, ".")
		wild := false
		switch {
		case strings.HasPrefix(d, "*."):
			wild, d = true, d[2:]
		case strings.HasPrefix(d, "."):
			wild, d = true, d[1:]
		}
		if err := validHost(d); err != nil {
			return nil, fmt.Errorf("invalid domain %q: %w", raw, err)
		}
		if wild {
			set["."+d] = true
		} else {
			set[d] = true
		}
	}
	out := []string{}
	for d := range set {
		host := strings.TrimPrefix(d, ".")
		covered := false
		// Drop d if a wildcard for itself (when d is exact) or a parent domain exists.
		labels := strings.Split(host, ".")
		for i := range labels {
			parent := "." + strings.Join(labels[i:], ".")
			if parent == d {
				continue
			}
			if set[parent] {
				covered = true
				break
			}
		}
		if !covered {
			out = append(out, d)
		}
	}
	sort.Strings(out)
	return out, nil
}

func validHost(d string) error {
	if d == "" || len(d) > 253 {
		return errors.New("empty or too long")
	}
	if net.ParseIP(d) != nil {
		return errors.New("IP addresses are not allowed")
	}
	labels := strings.Split(d, ".")
	if len(labels) < 2 {
		return errors.New("must be a fully qualified domain (at least two labels)")
	}
	for _, l := range labels {
		if !labelRe.MatchString(l) {
			return fmt.Errorf("bad label %q", l)
		}
	}
	last := labels[len(labels)-1]
	if strings.Trim(last, "0123456789") == "" {
		return errors.New("numeric top-level label")
	}
	return nil
}

// Render builds the Squid snippet for one user's container.
// domains must already be normalised.
func Render(userID, ip string, domains []string) (string, error) {
	addr := net.ParseIP(ip)
	if addr == nil {
		return "", fmt.Errorf("invalid container ip %q", ip)
	}
	mask := "/32"
	if addr.To4() == nil {
		mask = "/128"
	}
	safe := "u_" + ids.Short(userID)
	var b strings.Builder
	fmt.Fprintf(&b, "# managed by vm-manager for user %s; do not edit\n", userID)
	fmt.Fprintf(&b, "acl %s_src src %s%s\n", safe, addr.String(), mask)
	if len(domains) > 0 {
		fmt.Fprintf(&b, "acl %s_dst dstdomain %s\n", safe, strings.Join(domains, " "))
		fmt.Fprintf(&b, "http_access allow %s_src %s_dst\n", safe, safe)
	}
	return b.String(), nil
}

// Signaler sends a signal to a container (the egress proxy).
type Signaler interface {
	Signal(ctx context.Context, container, signal string) error
}

// Writer manages the allowlist directory and proxy reloads.
type Writer struct {
	dir         string
	proxy       string
	sig         Signaler
	minInterval time.Duration
	log         *slog.Logger

	mu     sync.Mutex // serialises file writes
	reload chan struct{}
}

// NewWriter builds a Writer. Call Run in a goroutine to process reloads.
func NewWriter(dir, proxyContainer string, sig Signaler, minInterval time.Duration, log *slog.Logger) *Writer {
	return &Writer{dir: dir, proxy: proxyContainer, sig: sig, minInterval: minInterval, log: log, reload: make(chan struct{}, 1)}
}

// Init creates the directory and the placeholder file.
func (w *Writer) Init() error {
	if err := os.MkdirAll(w.dir, 0o755); err != nil {
		return fmt.Errorf("egress dir: %w", err)
	}
	_, err := w.writeIfChanged(placeholder, "# keeps the include glob non-empty; managed by vm-manager\n")
	return err
}

func (w *Writer) path(userID string) string { return filepath.Join(w.dir, userID+".conf") }

// Write writes the user's file (atomically, only when the content changed) and
// requests a proxy reload when it did.
func (w *Writer) Write(userID, ip string, domains []string) error {
	if !ids.ValidUserID(userID) {
		return fmt.Errorf("invalid user id")
	}
	content, err := Render(userID, ip, domains)
	if err != nil {
		return err
	}
	changed, err := w.writeIfChanged(userID+".conf", content)
	if err != nil {
		return err
	}
	if changed {
		w.RequestReload()
	}
	return nil
}

// Remove deletes the user's file (if any) and requests a reload when it existed.
// Called when a container stops (its IP may be reused by another user's
// container) and on DeleteSandbox.
func (w *Writer) Remove(userID string) error {
	if !ids.ValidUserID(userID) {
		return fmt.Errorf("invalid user id")
	}
	w.mu.Lock()
	err := os.Remove(w.path(userID))
	w.mu.Unlock()
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("remove allowlist: %w", err)
	}
	w.RequestReload()
	return nil
}

// Users lists user ids that currently have a file.
func (w *Writer) Users() ([]string, error) {
	entries, err := os.ReadDir(w.dir)
	if err != nil {
		return nil, err
	}
	out := []string{}
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || name == placeholder || !strings.HasSuffix(name, ".conf") {
			continue
		}
		id := strings.TrimSuffix(name, ".conf")
		if ids.ValidUserID(id) {
			out = append(out, id)
		}
	}
	sort.Strings(out)
	return out, nil
}

func (w *Writer) writeIfChanged(name, content string) (bool, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	p := filepath.Join(w.dir, name)
	if old, err := os.ReadFile(p); err == nil && string(old) == content {
		return false, nil
	}
	tmp, err := os.CreateTemp(w.dir, ".tmp-"+name+"-*")
	if err != nil {
		return false, fmt.Errorf("write allowlist: %w", err)
	}
	defer func() { _ = os.Remove(tmp.Name()) }() // no-op after the rename
	if _, err := tmp.WriteString(content); err != nil {
		_ = tmp.Close()
		return false, fmt.Errorf("write allowlist: %w", err)
	}
	if err := tmp.Chmod(0o644); err != nil { // squid runs as its own user and must read it
		_ = tmp.Close()
		return false, err
	}
	if err := tmp.Close(); err != nil {
		return false, err
	}
	if err := os.Rename(tmp.Name(), p); err != nil {
		return false, fmt.Errorf("write allowlist: %w", err)
	}
	return true, nil
}

// RequestReload asks for a proxy reconfigure. Bursts coalesce into one SIGHUP.
func (w *Writer) RequestReload() {
	select {
	case w.reload <- struct{}{}:
	default:
	}
}

// ReloadNow sends SIGHUP to the proxy container synchronously.
func (w *Writer) ReloadNow(ctx context.Context) error {
	sctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	return w.sig.Signal(sctx, w.proxy, "SIGHUP")
}

// Run processes reload requests until ctx ends, at most one SIGHUP per minInterval.
func (w *Writer) Run(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case <-w.reload:
			if err := w.ReloadNow(ctx); err != nil {
				w.log.Error("egress proxy reload failed", "proxy", w.proxy, "err", err, "alert", true)
			} else {
				w.log.Debug("egress proxy reloaded", "proxy", w.proxy)
			}
			if w.minInterval > 0 {
				select {
				case <-ctx.Done():
					return
				case <-time.After(w.minInterval):
				}
			}
		}
	}
}
