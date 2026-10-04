package store

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"sort"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Postgres is the production Store.
type Postgres struct {
	pool *pgxpool.Pool
}

// OpenPostgres connects, pings and runs the embedded migrations.
func OpenPostgres(ctx context.Context, databaseURL string, migrations fs.FS) (*Postgres, error) {
	cfg, err := pgxpool.ParseConfig(databaseURL)
	if err != nil {
		return nil, fmt.Errorf("parse DATABASE_URL: %w", err)
	}
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("connect postgres: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("ping postgres: %w", err)
	}
	if err := Migrate(ctx, pool, migrations); err != nil {
		pool.Close()
		return nil, err
	}
	return &Postgres{pool: pool}, nil
}

// Migrate applies every *.sql file in migrations not yet recorded in schema_migrations,
// in name order, each in its own transaction. An advisory lock serialises concurrent starts.
func Migrate(ctx context.Context, pool *pgxpool.Pool, migrations fs.FS) error {
	const lockID = 7_265_730_001 // arbitrary, stable
	conn, err := pool.Acquire(ctx)
	if err != nil {
		return fmt.Errorf("migrate: acquire: %w", err)
	}
	defer conn.Release()
	if _, err := conn.Exec(ctx, `SELECT pg_advisory_lock($1)`, lockID); err != nil {
		return fmt.Errorf("migrate: lock: %w", err)
	}
	defer func() { _, _ = conn.Exec(context.Background(), `SELECT pg_advisory_unlock($1)`, lockID) }()

	if _, err := conn.Exec(ctx, `CREATE TABLE IF NOT EXISTS schema_migrations (
		version text PRIMARY KEY,
		applied_at timestamptz NOT NULL DEFAULT now())`); err != nil {
		return fmt.Errorf("migrate: create schema_migrations: %w", err)
	}
	names, err := fs.Glob(migrations, "*.sql")
	if err != nil {
		return fmt.Errorf("migrate: list: %w", err)
	}
	sort.Strings(names)
	for _, name := range names {
		var exists bool
		if err := conn.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE version = $1)`, name).Scan(&exists); err != nil {
			return fmt.Errorf("migrate: check %s: %w", name, err)
		}
		if exists {
			continue
		}
		body, err := fs.ReadFile(migrations, name)
		if err != nil {
			return fmt.Errorf("migrate: read %s: %w", name, err)
		}
		tx, err := conn.Begin(ctx)
		if err != nil {
			return fmt.Errorf("migrate: begin %s: %w", name, err)
		}
		if _, err := tx.Exec(ctx, string(body)); err != nil {
			_ = tx.Rollback(ctx)
			return fmt.Errorf("migrate: apply %s: %w", name, err)
		}
		if _, err := tx.Exec(ctx, `INSERT INTO schema_migrations (version) VALUES ($1)`, name); err != nil {
			_ = tx.Rollback(ctx)
			return fmt.Errorf("migrate: record %s: %w", name, err)
		}
		if err := tx.Commit(ctx); err != nil {
			return fmt.Errorf("migrate: commit %s: %w", name, err)
		}
	}
	return nil
}

const sandboxCols = `user_id, container_id, image_version, state, priority, last_active_at,
	mcp_token_hash, bridge_secret_hash, allowlist, created_at, updated_at, runtime, claude_connected,
	browser_enabled`

func scanSandbox(row pgx.Row) (Sandbox, error) {
	var sb Sandbox
	var state, runtime string
	var priority int16
	err := row.Scan(&sb.UserID, &sb.ContainerID, &sb.ImageVersion, &state, &priority, &sb.LastActiveAt,
		&sb.MCPTokenHash, &sb.BridgeSecretHash, &sb.Allowlist, &sb.CreatedAt, &sb.UpdatedAt, &runtime, &sb.ClaudeConnected,
		&sb.BrowserEnabled)
	if errors.Is(err, pgx.ErrNoRows) {
		return Sandbox{}, ErrNotFound
	}
	if err != nil {
		return Sandbox{}, err
	}
	sb.State = State(state)
	sb.Runtime = Runtime(runtime)
	sb.Priority = int32(priority)
	sb.LastActiveAt = sb.LastActiveAt.UTC()
	sb.CreatedAt = sb.CreatedAt.UTC()
	sb.UpdatedAt = sb.UpdatedAt.UTC()
	if sb.Allowlist == nil {
		sb.Allowlist = []string{}
	}
	return sb, nil
}

// Insert implements Store.
func (p *Postgres) Insert(ctx context.Context, sb Sandbox) error {
	allow := sb.Allowlist
	if allow == nil {
		allow = []string{}
	}
	runtime := sb.Runtime
	if runtime == "" {
		runtime = RuntimeHermes
	}
	_, err := p.pool.Exec(ctx, `INSERT INTO sandboxes (`+sandboxCols+`)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)`,
		sb.UserID, sb.ContainerID, sb.ImageVersion, string(sb.State), int16(sb.Priority), sb.LastActiveAt,
		sb.MCPTokenHash, sb.BridgeSecretHash, allow, sb.CreatedAt, sb.UpdatedAt, string(runtime), sb.ClaudeConnected,
		sb.BrowserEnabled)
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) && pgErr.Code == "23505" {
		return ErrExists
	}
	return err
}

// Get implements Store.
func (p *Postgres) Get(ctx context.Context, userID string) (Sandbox, error) {
	return scanSandbox(p.pool.QueryRow(ctx, `SELECT `+sandboxCols+` FROM sandboxes WHERE user_id = $1`, userID))
}

// List implements Store.
func (p *Postgres) List(ctx context.Context, states ...State) ([]Sandbox, error) {
	q := `SELECT ` + sandboxCols + ` FROM sandboxes`
	args := []any{}
	if len(states) > 0 {
		ss := make([]string, len(states))
		for i, s := range states {
			ss[i] = string(s)
		}
		q += ` WHERE state = ANY($1)`
		args = append(args, ss)
	}
	q += ` ORDER BY user_id`
	rows, err := p.pool.Query(ctx, q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Sandbox{}
	for rows.Next() {
		sb, err := scanSandbox(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, sb)
	}
	return out, rows.Err()
}

func (p *Postgres) exec1(ctx context.Context, q string, args ...any) error {
	tag, err := p.pool.Exec(ctx, q, args...)
	if err != nil {
		return err
	}
	if tag.RowsAffected() == 0 {
		return ErrNotFound
	}
	return nil
}

// SetState implements Store.
func (p *Postgres) SetState(ctx context.Context, userID string, s State, at time.Time) error {
	return p.exec1(ctx, `UPDATE sandboxes SET state = $2, updated_at = $3 WHERE user_id = $1`, userID, string(s), at)
}

// SetContainer implements Store.
func (p *Postgres) SetContainer(ctx context.Context, userID, containerID, imageVersion string, at time.Time) error {
	return p.exec1(ctx, `UPDATE sandboxes SET container_id = $2, image_version = $3, updated_at = $4 WHERE user_id = $1`,
		userID, containerID, imageVersion, at)
}

// Touch implements Store.
func (p *Postgres) Touch(ctx context.Context, userID string, at time.Time) error {
	return p.exec1(ctx, `UPDATE sandboxes SET last_active_at = GREATEST(last_active_at, $2), updated_at = $2 WHERE user_id = $1`,
		userID, at)
}

// SetAllowlist implements Store.
func (p *Postgres) SetAllowlist(ctx context.Context, userID string, domains []string, at time.Time) error {
	if domains == nil {
		domains = []string{}
	}
	return p.exec1(ctx, `UPDATE sandboxes SET allowlist = $2, updated_at = $3 WHERE user_id = $1`, userID, domains, at)
}

// SetPriority implements Store.
func (p *Postgres) SetPriority(ctx context.Context, userID string, priority int32, at time.Time) error {
	return p.exec1(ctx, `UPDATE sandboxes SET priority = $2, updated_at = $3 WHERE user_id = $1`, userID, int16(priority), at)
}

// SetRuntime implements Store.
func (p *Postgres) SetRuntime(ctx context.Context, userID string, runtime Runtime, at time.Time) error {
	return p.exec1(ctx, `UPDATE sandboxes SET runtime = $2, updated_at = $3 WHERE user_id = $1`, userID, string(runtime), at)
}

// SetClaudeConnected implements Store.
func (p *Postgres) SetClaudeConnected(ctx context.Context, userID string, connected bool, at time.Time) error {
	return p.exec1(ctx, `UPDATE sandboxes SET claude_connected = $2, updated_at = $3 WHERE user_id = $1`, userID, connected, at)
}

// SetBrowserEnabled implements Store.
func (p *Postgres) SetBrowserEnabled(ctx context.Context, userID string, enabled bool, at time.Time) error {
	return p.exec1(ctx, `UPDATE sandboxes SET browser_enabled = $2, updated_at = $3 WHERE user_id = $1`, userID, enabled, at)
}

// Delete implements Store.
func (p *Postgres) Delete(ctx context.Context, userID string) error {
	_, err := p.pool.Exec(ctx, `DELETE FROM sandboxes WHERE user_id = $1`, userID)
	return err
}

// StartAwake implements Store.
func (p *Postgres) StartAwake(ctx context.Context, userID string, at time.Time) error {
	_, err := p.pool.Exec(ctx, `INSERT INTO awake_periods (user_id, started_at) VALUES ($1, $2)
		ON CONFLICT (user_id) WHERE ended_at IS NULL DO NOTHING`, userID, at)
	return err
}

// EndAwake implements Store.
func (p *Postgres) EndAwake(ctx context.Context, userID string, at time.Time) error {
	_, err := p.pool.Exec(ctx, `UPDATE awake_periods SET ended_at = GREATEST(started_at, $2)
		WHERE user_id = $1 AND ended_at IS NULL`, userID, at)
	return err
}

// AddBrowserSeconds implements Store.
func (p *Postgres) AddBrowserSeconds(ctx context.Context, userID, reportID string, seconds int64, at time.Time) (bool, error) {
	if seconds <= 0 {
		return false, errors.New("store: browser seconds must be > 0")
	}
	tag, err := p.pool.Exec(ctx, `INSERT INTO browser_usage (user_id, report_id, seconds, recorded_at)
		VALUES ($1, $2, $3, $4) ON CONFLICT (user_id, report_id) DO NOTHING`, userID, reportID, seconds, at)
	if err != nil {
		return false, err
	}
	return tag.RowsAffected() == 1, nil
}

// Usage implements Store.
func (p *Postgres) Usage(ctx context.Context, userID string, since, until, now time.Time) ([]Usage, error) {
	openEnd := until
	if now.Before(openEnd) {
		openEnd = now
	}
	// Awake time from the periods that overlap the window, browser time from the reports recorded
	// inside it; a user with only one of the two still gets a row.
	user := ""
	args := []any{since, until, openEnd}
	if userID != "" {
		user = ` AND user_id = $4`
		args = append(args, userID)
	}
	q := `SELECT user_id, SUM(awake_ms)::bigint, SUM(browser)::bigint FROM (
		SELECT user_id,
			GREATEST(0, FLOOR(EXTRACT(EPOCH FROM (
				LEAST(COALESCE(ended_at, $3), $2) - GREATEST(started_at, $1))) * 1000)) AS awake_ms,
			0 AS browser
		FROM awake_periods
		WHERE started_at < $2 AND (ended_at IS NULL OR ended_at >= $1)` + user + `
		UNION ALL
		SELECT user_id, 0 AS awake_ms, seconds AS browser
		FROM browser_usage
		WHERE recorded_at >= $1 AND recorded_at < $2` + user + `
	) u GROUP BY user_id ORDER BY user_id`
	rows, err := p.pool.Query(ctx, q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Usage{}
	for rows.Next() {
		var u Usage
		var ms int64
		if err := rows.Scan(&u.UserID, &ms, &u.BrowserSeconds); err != nil {
			return nil, err
		}
		u.AwakeSeconds = ms / 1000
		out = append(out, u)
	}
	return out, rows.Err()
}

// Close implements Store.
func (p *Postgres) Close() { p.pool.Close() }
