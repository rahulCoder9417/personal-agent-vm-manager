// Command vm-manager is the only service that controls Docker: it creates,
// wakes, stops and deletes each user's sandboxed agent container and serves
// the vm.v1.VmManager gRPC API to the backend.
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/personal-agent/vm-manager/internal/bridge"
	"github.com/personal-agent/vm-manager/internal/clock"
	"github.com/personal-agent/vm-manager/internal/config"
	"github.com/personal-agent/vm-manager/internal/docker"
	"github.com/personal-agent/vm-manager/internal/egress"
	"github.com/personal-agent/vm-manager/internal/grpcserver"
	"github.com/personal-agent/vm-manager/internal/sandbox"
	"github.com/personal-agent/vm-manager/internal/store"
	"github.com/personal-agent/vm-manager/migrations"
)

func main() {
	if err := run(); err != nil {
		slog.New(slog.NewJSONHandler(os.Stderr, nil)).Error("vm-manager exited", "err", err)
		os.Exit(1)
	}
}

func run() error {
	cfg, err := config.Load(os.Getenv)
	if err != nil {
		return err
	}
	log := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: cfg.LogLevel}))
	slog.SetDefault(log)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	st, err := openStore(ctx, cfg, log)
	if err != nil {
		return err
	}
	defer st.Close()

	dctx, dcancel := context.WithTimeout(ctx, 30*time.Second)
	eng, err := docker.NewClient(dctx)
	dcancel()
	if err != nil {
		return err
	}
	defer eng.Close()

	eg := egress.NewWriter(cfg.EgressAllowlistDir, cfg.EgressProxyContainer, eng, cfg.EgressReloadMinInterval, log)
	if err := eg.Init(); err != nil {
		return err
	}
	go eg.Run(ctx)

	m := sandbox.New(cfg, st, eng, bridge.NewClient(), eg, clock.Real{}, log)
	log.Info("reconciling Docker with the sandboxes table")
	m.ReconcileOnce(ctx)
	go m.Run(ctx)

	lis, err := net.Listen("tcp", cfg.GRPCAddr)
	if err != nil {
		return fmt.Errorf("listen %s: %w", cfg.GRPCAddr, err)
	}
	srv := grpcserver.NewGRPCServer(m, cfg.InternalSecret, log)
	serveErr := make(chan error, 1)
	go func() { serveErr <- srv.Serve(lis) }()
	log.Info("vm-manager listening", "addr", cfg.GRPCAddr, "image", cfg.AgentImage, "image_version", cfg.AgentImageVersion,
		"awake_cap", cfg.AwakeCap, "runtime", cfg.Runtime, "network", cfg.SandboxNetwork)

	select {
	case <-ctx.Done():
	case err := <-serveErr:
		if err != nil && !errors.Is(err, net.ErrClosed) {
			return fmt.Errorf("grpc serve: %w", err)
		}
	}
	log.Info("shutting down; containers keep running and are reconciled on the next start")
	done := make(chan struct{})
	go func() { srv.GracefulStop(); close(done) }()
	select {
	case <-done:
	case <-time.After(20 * time.Second):
		srv.Stop()
	}
	return nil
}

// openStore connects to Postgres, retrying for a minute while the database starts.
func openStore(ctx context.Context, cfg *config.Config, log *slog.Logger) (store.Store, error) {
	deadline := time.Now().Add(time.Minute)
	for {
		cctx, cancel := context.WithTimeout(ctx, 10*time.Second)
		st, err := store.OpenPostgres(cctx, cfg.DatabaseURL, migrations.FS)
		cancel()
		if err == nil {
			return st, nil
		}
		if time.Now().After(deadline) || ctx.Err() != nil {
			return nil, err
		}
		log.Warn("postgres not ready; retrying", "err", err)
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(2 * time.Second):
		}
	}
}
