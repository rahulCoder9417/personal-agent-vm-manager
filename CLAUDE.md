# Personal Agent: vm-manager (Go)

The **only** service that controls Docker. It creates, wakes, stops and deletes each user's sandboxed agent container (Hermes + Bridge), delivers messages into containers, and writes their egress allowlists. The backend (`../beckend`) calls it over **gRPC**.

## Read before doing anything

The design lives in the backend repo: **`../beckend/docs/mvp-hld.md`**. The parts that matter here:
- **0.5** Hermes inside a container · **0.6** closing containers
- **1.1–1.2** why this is a separate service; the gRPC RPC list
- **4** networks · **6** the vm-manager itself · **8** the `sandboxes` table
- **9, Phase 2** what to build and the "done when…" checks

## What lives here

```
vm-manager/
├── proto/vm/v1/vm.proto   ← gRPC contract (SOURCE OF TRUTH for backend ↔ vm-manager)
├── cmd/vm-manager/        main
├── internal/              grpc server, docker, sandboxes store, loops, egress, bridge client
├── migrations/            the `sandboxes` table (owned only by this service)
└── agent-image/           Dockerfile: Hermes Agent + Bridge adapter (Python)
```

## How we work

- **Plan first** for non-trivial work: files, steps and tests. Wait for approval.
- **Contract first:** change `proto/vm/v1/vm.proto` first, regenerate, then code. Tell the user the backend must regenerate its client (`pnpm proto:gen` in `../beckend`). Never break existing RPCs; add new fields or RPCs instead.
- **Tests with every change:** unit tests for the loops and state machine (fake Docker); integration tests against real Docker with gVisor for create → wake → deliver → idle stop → wake again. Run `go vet`, the linter and tests before saying done, and report real results.
- **Small commits** after each working step.
- **Ask when the HLD is unclear**; don't invent behaviour.

## Stack

Go (latest stable) · gRPC + protobuf generated with **buf** · Docker Engine Go SDK · gVisor runtime `runsc` · Postgres via pgx · structured logging (`slog`) · config from env vars.

## Rules (never break these)

- **Container spec:**
  - runtime `runsc` (gVisor); memory ~600 MB, 1 CPU, a pids limit, a disk quota on /data
  - read-only root filesystem; only the `/data` volume writable
  - no privileges or capabilities; `no-new-privileges`
  - the **sandbox network only**
- **Secrets per container:** its own LiteLLM key, Platform MCP token, Bridge secret, user id. **Never** host, Docker, database or provider secrets. Store tokens and secrets only as hashes.
- **The only path into a container is `Deliver` → Bridge** (HTTP + the per-container secret). The only way out is the egress proxy with that container's allowlist.
- **Never stop a container in the middle of a turn.** Stop = not busy **and** idle ≥ 3 min. Always stop politely (let Hermes save to /data, ~20 s) unless the call is a forced stop.
- **Capacity guard:** at the awake cap, evict the longest-idle, not-busy container. If none can be evicted, return a retryable error (the backend's job waits).
- **Reconciler:** on startup and every 5 min, make Docker's real state match the `sandboxes` table.
- **gRPC auth:** a shared secret in metadata. Reject calls without it. Listen only on the platform network.
- **Deleting** a container also deletes its volume and egress rules. It happens only via `DeleteSandbox` (account deletion). Idle never deletes.
- **Record awake periods** (start/stop) for `GetUsage`. Billing depends on them.

## Done when (Phase 2)

- Create → Wake → Deliver("hello") streams events ending in `turn_done`.
- It auto-stops after 3 idle minutes and wakes again with memory intact.
- The awake cap is respected.
- A container can't reach anything except the egress proxy's allowlisted domains.
- One container's measured RAM is written in `README.md` (used to set the awake cap).

## Commands

- Generate (needs `protoc-gen-go` + `protoc-gen-go-grpc` on PATH, e.g. `go install google.golang.org/protobuf/cmd/protoc-gen-go@latest google.golang.org/grpc/cmd/protoc-gen-go-grpc@latest`): `go run github.com/bufbuild/buf/cmd/buf@latest generate` (fallback: `protoc -I proto --go_out=gen --go_opt=paths=source_relative --go-grpc_out=gen --go-grpc_opt=paths=source_relative proto/vm/v1/vm.proto`)
- Lint: `go run github.com/golangci/golangci-lint/v2/cmd/golangci-lint@latest run ./...` (add `--build-tags=integration` to include the integration tests)
- Vet / test: `go vet ./... && go test ./...` (race: `go test -race ./...`; store tests start embedded Postgres, or set `VM_TEST_DATABASE_URL`; skip with `VM_TEST_SKIP_POSTGRES=1`)
- Integration tests (needs Docker + runsc + the backend's infra stack + `IT_LITELLM_KEY`): `go test -tags=integration -v -timeout 30m ./internal/integration/`
- Bridge tests: `cd agent-image && python3 -m unittest discover -s tests -t .`
- Build images: `docker build -t personal-agent/vm-manager:dev .` and `docker build -t personal-agent/agent:dev agent-image/`
- Measure RAM of an awake container: `docker stats --no-stream pa-sbx-<user_id>`

## Paths

The folder lives in a path with spaces (`b2b ai saas`). Always quote paths in shell commands.
