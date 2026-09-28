package egress

import (
	"context"
	"io"
	"log/slog"
	"os"
	"path/filepath"
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
	got, err := Render("user_1", "172.30.1.9", []string{".example.com", "other.org"})
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
	noDomains, _ := Render("user_1", "172.30.1.9", nil)
	if strings.Contains(noDomains, "dstdomain") || strings.Contains(noDomains, "http_access") {
		t.Fatalf("empty list must skip the dst lines:\n%s", noDomains)
	}
	if _, err := Render("user_1", "not-an-ip", nil); err == nil {
		t.Fatal("bad ip must fail")
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

	if err := w.Write("u1", "172.30.0.5", []string{"example.com"}); err != nil {
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
	if err := w.Write("u1", "172.30.0.5", []string{"example.com"}); err != nil {
		t.Fatal(err)
	}
	time.Sleep(20 * time.Millisecond)
	if sig.count() != 1 {
		t.Fatalf("unchanged file must not reload: %v", sig.calls)
	}
	users, _ := w.Users()
	if !slices.Equal(users, []string{"u1"}) {
		t.Fatalf("users: %v", users)
	}
	if err := w.Remove("u1"); err != nil {
		t.Fatal(err)
	}
	waitFor(t, func() bool { return sig.count() == 2 })
	if err := w.Remove("u1"); err != nil { // idempotent, no reload
		t.Fatal(err)
	}
	if err := w.Write("../evil", "172.30.0.5", nil); err == nil {
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
