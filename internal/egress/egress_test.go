package egress

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/personal-agent/vm-manager/internal/ids"
)

func TestNormalizeDomains(t *testing.T) {
	got, err := NormalizeDomains([]string{"Example.com", "*.example.com", "api.example.com", "other.org.", "mcp.other.org", "a.b.c.io"})
	if err != nil {
		t.Fatal(err)
	}
	want := []string{".example.com", "a.b.c.io", "mcp.other.org", "other.org"}
	if !slices.Equal(got, want) {
		t.Fatalf("got %v want %v", got, want)
	}
	empty, err := NormalizeDomains(nil)
	if err != nil || len(empty) != 0 {
		t.Fatalf("empty: %v %v", empty, err)
	}
}

func TestNormalizeDomainsRejectsUnsafe(t *testing.T) {
	bad := []string{
		"", "localhost", "postgres", ".com", "*.com", "10.0.0.1", "::1", "1.2.3.4.5",
		"exa mple.com", "example.com\nhttp_access allow all", "ex_ample.com", "-bad.com", "a..b.com",
		"example.com/path", "http://example.com", strings.Repeat("a", 64) + ".com",
	}
	for _, d := range bad {
		if _, err := NormalizeDomains([]string{d}); err == nil {
			t.Errorf("%q should be rejected", d)
		}
	}
	many := make([]string, MaxDomains+1)
	for i := range many {
		many[i] = "x.com"
	}
	if _, err := NormalizeDomains(many); err == nil {
		t.Error("too many domains should be rejected")
	}
}

func TestRender(t *testing.T) {
	safe := "u_" + ids.Short("user_1")
	got, err := Render("user_1", "172.30.1.9", Rule{Domains: []string{".example.com", "other.org"}})
	if err != nil {
		t.Fatal(err)
	}
	want := "# managed by vm-manager for user user_1; do not edit\n" +
		"acl " + safe + "_src src 172.30.1.9/32\n" +
		"acl " + safe + "_dst dstdomain .example.com other.org\n" +
		"http_access allow " + safe + "_src " + safe + "_dst\n"
	if got != want {
		t.Fatalf("got:\n%s\nwant:\n%s", got, want)
	}
	noDomains, _ := Render("user_1", "172.30.1.9", Rule{})
	if strings.Contains(noDomains, "dstdomain") || strings.Contains(noDomains, "http_access") {
		t.Fatalf("empty list must skip the dst lines:\n%s", noDomains)
	}
	if _, err := Render("user_1", "not-an-ip", Rule{}); err == nil {
		t.Fatal("bad ip must fail")
	}
}

// The rule for a user with Scout's browser: that container may reach any public web address on
// ports 80 and 443; internal names and addresses stay refused. The text below was also loaded
// into a real Squid (7.7) next to the backend's base squid.conf and probed with requests
// (docs/build-report.md, section 9).
func TestRenderOpenWeb(t *testing.T) {
	safe := "u_" + ids.Short("user_1")
	got, err := Render("user_1", "172.30.1.9", Rule{Domains: []string{".example.com", "other.org"}, OpenWeb: true})
	if err != nil {
		t.Fatal(err)
	}
	want := "# managed by vm-manager for user user_1; do not edit\n" +
		"acl " + safe + "_src src 172.30.1.9/32\n" +
		"acl " + safe + "_int_name dstdom_regex -n -i ^[^.]+$ (^|\\.)(internal|local|localhost|lan|home\\.arpa)$\n" +
		"acl " + safe + "_int_ip dst 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 " +
		"192.0.0.0/24 192.168.0.0/16 198.18.0.0/15 224.0.0.0/4 240.0.0.0/4 168.63.129.16/32 " +
		"::/128 ::1/128 64:ff9b::/96 fc00::/7 fe80::/10 ff00::/8\n" +
		"http_access deny " + safe + "_src " + safe + "_int_name\n" +
		"http_access deny " + safe + "_src " + safe + "_int_ip\n" +
		"acl " + safe + "_dst dstdomain .example.com other.org\n" +
		"http_access allow " + safe + "_src " + safe + "_dst\n" +
		"acl " + safe + "_web port 80 443\n" +
		"http_access allow " + safe + "_src " + safe + "_web\n"
	if got != want {
		t.Fatalf("got:\n%s\nwant:\n%s", got, want)
	}

	// Every access line is tied to this container's address: no other container gains anything.
	for _, line := range strings.Split(strings.TrimSpace(got), "\n") {
		if strings.HasPrefix(line, "http_access ") && !strings.Contains(line, " "+safe+"_src ") {
			t.Errorf("access line without this container's source: %q", line)
		}
	}
	// The refusals come before every allow, so neither the user's own domains nor the open web
	// can reach an internal name or address.
	lastDeny := strings.LastIndex(got, "http_access deny ")
	firstAllow := strings.Index(got, "http_access allow ")
	if lastDeny < 0 || firstAllow < 0 || lastDeny > firstAllow {
		t.Fatalf("the internal refusals must come before the first allow:\n%s", got)
	}
	// The open web is ports 80 and 443 only, and only one line opens it.
	if strings.Count(got, "http_access allow") != 2 || !strings.Contains(got, "acl "+safe+"_web port 80 443\n") {
		t.Fatalf("unexpected allow lines:\n%s", got)
	}

	// The ranges that must stay closed: private, loopback, link-local (cloud metadata), CGNAT.
	for _, cidr := range []string{"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16",
		"100.64.0.0/10", "::1/128", "fc00::/7", "fe80::/10"} {
		if !slices.Contains(InternalNets, cidr) {
			t.Errorf("%s must be refused", cidr)
		}
	}
	covered := func(ip string) bool {
		addr := net.ParseIP(ip)
		for _, cidr := range InternalNets {
			if _, n, err := net.ParseCIDR(cidr); err != nil {
				t.Fatalf("bad range %q: %v", cidr, err)
			} else if n.Contains(addr) && (addr.To4() != nil) == (n.IP.To4() != nil) {
				return true
			}
		}
		return false
	}
	for _, ip := range []string{"127.0.0.1", "10.1.2.3", "172.30.0.2", "172.17.0.1", "192.168.1.1", "169.254.169.254",
		"100.100.100.200", "0.0.0.0", "168.63.129.16", "224.0.0.1", "255.255.255.255", "::1", "fd00:ec2::254", "fe80::1"} {
		if !covered(ip) {
			t.Errorf("%s must be inside a refused range", ip)
		}
	}
	for _, ip := range []string{"93.184.216.34", "8.8.8.8", "1.1.1.1", "2606:4700::1111", "172.32.0.1", "100.128.0.1"} {
		if covered(ip) {
			t.Errorf("%s is a public address and must not be refused", ip)
		}
	}
	// The names: the platform's service names (no dot), localhost and the private-use endings.
	names := make([]*regexp.Regexp, 0, len(InternalNames))
	for _, expr := range InternalNames {
		names = append(names, regexp.MustCompile("(?i)"+expr))
	}
	internal := func(host string) bool {
		for _, re := range names {
			if re.MatchString(host) {
				return true
			}
		}
		return false
	}
	for _, host := range []string{"api", "litellm", "postgres", "redis", "vm-manager", "egress-proxy", "localhost",
		"host.docker.internal", "metadata.google.internal", "printer.local", "foo.localhost", "nas.lan", "router.home.arpa", "API"} {
		if !internal(host) {
			t.Errorf("%s must be refused by name", host)
		}
	}
	for _, host := range []string{"example.com", "www.internal.example.com", "local.example.org", "lan.party", "internal.io"} {
		if internal(host) {
			t.Errorf("%s is a public name and must not be refused by name", host)
		}
	}

	// No domains of the user's own: the refusals and the open web only.
	bare, _ := Render("user_1", "172.30.1.9", Rule{OpenWeb: true})
	if strings.Contains(bare, "dstdomain") || strings.Count(bare, "http_access allow") != 1 ||
		strings.Count(bare, "http_access deny") != 2 {
		t.Fatalf("open web without own domains:\n%s", bare)
	}
	// Without the open web the file is today's strict rule, unchanged.
	strict, _ := Render("user_1", "172.30.1.9", Rule{Domains: []string{"other.org"}})
	if strings.Contains(strict, "_web") || strings.Contains(strict, "_int_") || strings.Contains(strict, "deny") ||
		strings.Contains(strict, "port") {
		t.Fatalf("a user without the browser keeps the strict rule:\n%s", strict)
	}
	// IPv6 container address.
	v6, _ := Render("user_1", "fd00::9", Rule{OpenWeb: true})
	if !strings.Contains(v6, "_src src fd00::9/128\n") {
		t.Fatalf("ipv6 source:\n%s", v6)
	}
}

type fakeSig struct {
	mu    sync.Mutex
	calls []string
}

func (f *fakeSig) Signal(_ context.Context, c, s string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, c+":"+s)
	return nil
}

func (f *fakeSig) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.calls)
}

func TestWriterFilesAndReload(t *testing.T) {
	dir := t.TempDir()
	sig := &fakeSig{}
	w := NewWriter(dir, "egress-proxy", sig, 0, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err := w.Init(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(dir, placeholder)); err != nil {
		t.Fatal("placeholder missing")
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go w.Run(ctx)

	if err := w.Write("u1", "172.30.0.5", Rule{Domains: []string{"example.com"}}); err != nil {
		t.Fatal(err)
	}
	body, err := os.ReadFile(filepath.Join(dir, "u1.conf"))
	if err != nil || !strings.Contains(string(body), "src 172.30.0.5/32") {
		t.Fatalf("file: %q %v", body, err)
	}
	waitFor(t, func() bool { return sig.count() == 1 })
	if sig.calls[0] != "egress-proxy:SIGHUP" {
		t.Fatalf("signal: %v", sig.calls)
	}
	// Same content: no rewrite, no reload.
	if err := w.Write("u1", "172.30.0.5", Rule{Domains: []string{"example.com"}}); err != nil {
		t.Fatal(err)
	}
	time.Sleep(20 * time.Millisecond)
	if sig.count() != 1 {
		t.Fatalf("unchanged file must not reload: %v", sig.calls)
	}
	// The browser turned on for this user: the file changes and the proxy reloads.
	if err := w.Write("u1", "172.30.0.5", Rule{Domains: []string{"example.com"}, OpenWeb: true}); err != nil {
		t.Fatal(err)
	}
	body, _ = os.ReadFile(filepath.Join(dir, "u1.conf"))
	if !strings.Contains(string(body), "_web port 80 443") || !strings.Contains(string(body), "_int_ip dst ") {
		t.Fatalf("open-web file: %q", body)
	}
	waitFor(t, func() bool { return sig.count() == 2 })
	if err := w.Write("u1", "172.30.0.5", Rule{Domains: []string{"example.com"}}); err != nil {
		t.Fatal(err)
	}
	waitFor(t, func() bool { return sig.count() == 3 })
	users, _ := w.Users()
	if !slices.Equal(users, []string{"u1"}) {
		t.Fatalf("users: %v", users)
	}
	if err := w.Remove("u1"); err != nil {
		t.Fatal(err)
	}
	waitFor(t, func() bool { return sig.count() == 4 })
	if err := w.Remove("u1"); err != nil { // idempotent, no reload
		t.Fatal(err)
	}
	if err := w.Write("../evil", "172.30.0.5", Rule{}); err == nil {
		t.Fatal("path traversal user id must be rejected")
	}
}

func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal("condition not met in time")
}

func TestMergeDomains(t *testing.T) {
	user := make([]string, 0, MaxDomains)
	for i := range MaxDomains {
		user = append(user, fmt.Sprintf("d%d.example.com", i))
	}
	got, err := MergeDomains(user, []string{"api.anthropic.com", "claude.ai", "d1.example.com"})
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != MaxDomains+2 {
		t.Fatalf("merge must dedupe and not cap: %d", len(got))
	}
	got, _ = MergeDomains([]string{".anthropic.com"}, []string{"api.anthropic.com"})
	if strings.Join(got, ",") != ".anthropic.com" {
		t.Fatalf("covered entries dropped: %v", got)
	}
	if _, err := MergeDomains([]string{"10.0.0.1"}); err == nil {
		t.Fatal("IPs are still rejected")
	}
}
