package store

import (
	"context"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	embeddedpostgres "github.com/fergusstrange/embedded-postgres"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/personal-agent/vm-manager/migrations"
)

// TestPostgresStore runs the Store suite against real Postgres.
// It uses VM_TEST_DATABASE_URL when set; otherwise it starts embedded Postgres
// (binaries downloaded on first run and cached). Set VM_TEST_SKIP_POSTGRES=1 to skip.
func TestPostgresStore(t *testing.T) {
	if testing.Short() || os.Getenv("VM_TEST_SKIP_POSTGRES") == "1" {
		t.Skip("postgres tests skipped")
	}
	ctx := context.Background()
	baseURL := os.Getenv("VM_TEST_DATABASE_URL")
	if baseURL == "" {
		port := freePort(t)
		cache := os.Getenv("VM_TEST_PG_CACHE")
		if cache == "" {
			home, _ := os.UserCacheDir()
			cache = filepath.Join(home, "vm-manager-embedded-postgres")
		}
		runtime := t.TempDir()
		pg := embeddedpostgres.NewDatabase(embeddedpostgres.DefaultConfig().
			Port(uint32(port)).
			Database("vm").
			RuntimePath(runtime).
			DataPath(filepath.Join(runtime, "data")).
			BinariesPath(filepath.Join(runtime, "bin")).
			CachePath(cache).
			StartTimeout(90 * time.Second).
			Logger(nil))
		if err := pg.Start(); err != nil {
			t.Skipf("embedded postgres unavailable (%v); the in-memory suite still covers the Store contract", err)
		}
		t.Cleanup(func() { _ = pg.Stop() })
		baseURL = fmt.Sprintf("postgres://postgres:postgres@localhost:%d/vm?sslmode=disable", port)
	}

	admin, err := pgxpool.New(ctx, baseURL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(admin.Close)

	var n atomic.Int32
	runSuite(t, func(t *testing.T) Store {
		// A fresh schema per sub-test keeps them independent.
		schema := fmt.Sprintf("t_%d_%d", time.Now().UnixNano(), n.Add(1))
		if _, err := admin.Exec(ctx, "CREATE SCHEMA "+schema); err != nil {
			t.Fatal(err)
		}
		cfg, _ := pgxpool.ParseConfig(baseURL)
		cfg.ConnConfig.RuntimeParams["search_path"] = schema
		pool, err := pgxpool.NewWithConfig(ctx, cfg)
		if err != nil {
			t.Fatal(err)
		}
		if err := Migrate(ctx, pool, migrations.FS); err != nil {
			t.Fatal(err)
		}
		// Running migrations twice is a no-op.
		if err := Migrate(ctx, pool, migrations.FS); err != nil {
			t.Fatal(err)
		}
		s := &Postgres{pool: pool}
		t.Cleanup(s.Close)
		return s
	})
}

func freePort(t *testing.T) int {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = l.Close() }()
	return l.Addr().(*net.TCPAddr).Port
}
