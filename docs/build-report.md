# Build report: vm-manager (design vs what we built)

**Date:** 2026-09-28 · **Branch:** `mvp-build` · **Design:** `../beckend/docs/mvp-hld.md` (0.5, 0.6, 1.2, 4, 6, 8, Phase 2) · **Whole-system report:** `../beckend/docs/build-report.md`

## In short

- Phase 2 is **coded, committed and unit tested**: every gRPC RPC, the container spec, the five background loops, the egress writer, the agent image (Hermes + Bridge).
- **Nothing has run against real Docker.** Docker and gVisor aren't installed on the dev machine, so the Docker parts were tested against a fake daemon, and the Bridge against a fake Hermes.
- The proto (`proto/vm/v1/vm.proto`) got additive RPCs and fields for the agent runtime and the Claude Code sign-in (section 6); no existing field changed.

## 1. Initial design vs what we built

| Area | Design (HLD) | What we built | Status |
|---|---|---|---|
| Role | The only service with Docker access (privilege boundary) | Docker socket is mounted only here; nothing else in the stack talks to Docker | ✅ |
| Contract | gRPC, generated with buf from `vm.proto` | `gen/vm/v1` (Go), committed; backend client generated from the same file | ✅ |
| RPCs | CreateSandbox, WakeSandbox, Deliver (stream), Touch, StopSandbox, DeleteSandbox, GetSandbox, ListSandboxes, GetUsage, SetEgressAllowlist, SnapshotVolume | All 11 in `internal/grpcserver` | ✅ |
| Auth | Shared secret in metadata; platform network only | `x-internal-secret` (32+ chars, constant-time compare) on unary + streaming; port 9090 never published | ✅ |
| Data | `sandboxes` table, owned only by this service | `sandboxes` + `awake_periods` in its own database (`migrations/0001_sandboxes.sql`, run at startup) | ✅ |
| Create | Container from current image + /data volume + MCP token + Bridge secret (hashes only) + Hermes config + allowlist; left stopped | Volume `pa-data-<user>`, container `pa-sbx-<user>`, secrets written to `/data/.platform/platform.json`; idempotent | ✅ / ⚠️ real Docker |
| Container spec | gVisor, ~600 MB, 1 CPU, pids + disk limit, read-only, only /data writable, no privileges, sandbox network, own secrets | runsc, 600 MB no swap, 1 CPU, pids 256, read-only rootfs, cap-drop ALL, no-new-privileges, uid 10001, sandbox network, `--init` | ⚠️ (no disk quota, see 3) |
| Wake | Start if needed, wait for the Bridge | Capacity guard → re-create on image change → start (retry, then re-create) → wait for `/healthz` → egress file → open awake period | ✅ / ⚠️ |
| Deliver | POST to Bridge, relay progress, turn done → not busy + last active; 15 min timeout → force stop + error | As designed; `platform_fault=true` on timeout so the turn isn't charged; the turn is tracked even if the caller disconnects | ✅ |
| Idle reaper | Every 1 min: not in a turn **and** quiet ≥ 3 min → polite stop (~20 s) | As designed; also asks the Bridge whether a turn is running | ✅ |
| Capacity guard | On wake, over the awake cap → stop longest-idle not-busy | Lowest plan priority first, then longest idle; none evictable → `RESOURCE_EXHAUSTED` (backend retries) | ✅ |
| Health checker | Every 30 s; not answering → restart; repeated → broken + alert | 2 failed checks → restart; > 3 restarts → stop + `broken` + `alert=true` log | ✅ |
| Reconciler | Startup + every 5 min: Docker state = table | Fixes running/stopped drift, stale waking/busy, orphans, duplicates, leftover egress files; never deletes volumes | ✅ |
| Image rollout | Stopped containers re-created on next wake, keep /data | As designed (`AGENT_IMAGE_VERSION`) | ✅ |
| Awake periods | Recorded for credits | Opened on wake, closed on every stop; `GetUsage` clips to the asked window | ✅ |
| Egress | Allowlist written per user | One Squid file per awake user + SIGHUP to the proxy (coalesced); domains validated so they can't inject config | ✅ / ⚠️ real Squid |
| Snapshots | Archive /data for backup | `tar.gz` via the Docker archive API into `SNAPSHOT_ROOT` | ✅ / ⚠️ |
| Agent image | Hermes + Bridge plugged in as a messaging platform; HTTP + secret; reports turn done | Hermes (pinned commit) + stdlib-Python Bridge: `/healthz`, `/deliver` (NDJSON), `/shutdown`; dedupe by `delivery_id`; one Hermes session per chat | ✅ Bridge / ⚠️ Hermes |
| RAM measured in README | Required for the awake cap | Not measured | ❌ |

## 2. What's in the repo

| Path | What |
|---|---|
| `cmd/vm-manager/` | main: config, store, Docker client, loops, gRPC server, graceful stop |
| `internal/config` | env config, validated at startup |
| `internal/store` | pgx store + in-memory store for tests |
| `internal/docker` | Engine interface, Docker SDK client, fake daemon |
| `internal/sandbox` | state machine, capacity guard, Deliver relay, background loops |
| `internal/grpcserver` | the VmManager service + auth interceptors |
| `internal/bridge` | HTTP client for the Bridge |
| `internal/egress` | Squid allowlist files + proxy reload |
| `internal/platformfile` | the container's `platform.json` (secrets + config) |
| `internal/integration` | real Docker/gVisor tests (build tag `integration`) |
| `agent-image/` | Dockerfile, Bridge (`bridge/*.py`), Bridge tests |
| `Dockerfile` | the vm-manager image (distroless) |

## 3. Where we changed or filled in the design

1. **Secrets are in a file on the user's volume**, not in env. A container re-created for a new image keeps the same MCP token, so the hash the backend stores never changes.
2. **The Hermes config is rendered by the Bridge at each boot** from `platform.json`, so it always matches the Hermes version in the image.
3. **The egress file is removed on every stop**, not only on delete, because a stopped container's IP can be reused by another user's container.
4. **SetEgressAllowlist replaces both lists.** MCP server hosts aren't added automatically; the backend sends them in `domains` (it does).
5. **A polite stop during a turn is refused** (`FAILED_PRECONDITION`); stuck turns use `force`.
6. **No `/data` disk quota** with Docker's default `local` volume driver. Set `SANDBOX_VOLUME_DRIVER` / `SANDBOX_VOLUME_OPTS` on the server.
7. **`browser_seconds` is always 0** for now; nothing records browser time yet.
8. **Hermes's own approval prompts are denied**; risky actions go through the platform's `request_approval` tool.
9. **The Bridge sends the final answer itself** through the Platform MCP `send_message` (hidden from Hermes), so the Deliver stream never carries reply text.
10. **User ids** must match `[A-Za-z0-9][A-Za-z0-9_-]{0,127}`; an unspecified priority counts as LOW.

## 4. Tests (real runs on the dev machine)

- `go vet ./...` and `go test ./...`: all packages pass; `go test -race` passes; golangci-lint: 0 issues.
- Store tests run on a real Postgres (embedded binaries).
- Bridge: 27 Python tests pass (`python3 -m unittest`).
- `go test -tags=integration` compiles but has **never run** (needs Docker + runsc).

## 5. Not verified yet

| What | How to check |
|---|---|
| Docker SDK calls, gVisor, read-only rootfs + `/data` writes | `go test -tags=integration -v ./internal/integration/` |
| Create → wake → deliver "hello" → `turn_done` | same (`TestCreateWakeDeliverHello`) |
| Idle stop after 3 min and wake with memory intact | same |
| Awake cap respected | same |
| Container reaches only allowlisted domains | same + the backend's `infra/tests/egress-test.sh` |
| Hermes config keys, `gateway run` and its run API inside the image | build `agent-image`, send a real message |
| RAM per container | `docker stats --no-stream pa-sbx-<user_id>` → write it in `README.md`, set `AWAKE_CAP` |

## 6. Claude Code runtime (user's Claude plan)

**Date:** 2026-09-30 · **Branch:** `claude-plan` · **Rules:** https://code.claude.com/docs/en/legal-and-compliance

A second agent runtime: the official Claude Code CLI, running in the user's own sandbox on the user's own Claude subscription. Hermes stays the default.

### Design vs built

| Area | Asked for | Built | Status |
|---|---|---|---|
| Proto | `AgentRuntime`, `Sandbox.runtime/claude_connected`, `CreateSandboxRequest.runtime`, `SetAgentRuntime`, `StartClaudeLogin`, `CompleteClaudeLogin`, `ClaudeLogout` | Used as committed; `gen/` regenerated with buf. No proto additions were needed | ✅ |
| Image | Official CLI, documented install, pinned, config on /data, read-only rootfs | Native installer at 2.1.283, binary checked against Anthropic's GPG-signed manifest, unmodified; `CLAUDE_CONFIG_DIR=/data/claude`, `HOME=/data/home`, auto-update off | ✅ / ⚠️ image never built |
| Runtime switch | platform.json `runtime`, read per delivery, no restart; reported by Get/List/Create/Wake; stored in the table | `sandboxes.runtime` + `claude_connected` (migration 0002); platform.json `runtime`; Bridge re-reads it per delivery (keeps the last good copy if a write is in progress) | ✅ |
| Claude turn | `claude -p` stream-json, per-chat session, Platform MCP with the container token, workdir on /data, no prompts; same NDJSON, dedupe, `send_message` | As asked: `--session-id`/`--resume` per `session_key` (map in /data), `--mcp-config` + `--strict-mcp-config`, `dontAsk` + allow list + `--permission-prompts none`, `send_message` disallowed for the agent and sent by the Bridge | ✅ / ⚠️ real CLI |
| Error mapping | not signed in -> "Connect your Claude plan again" (fault false); limits -> clear message (false); crash/timeout -> true | As asked; also `error_max_turns` -> false, overload / unknown -> true (the turn is not charged) | ✅ |
| Sign-in over the Bridge | start / complete / logout, 10-minute expiry, one pending per container, healthz `claude_connected` via the CLI | `/claude/login/start`, `/claude/login/complete`, `/claude/logout`; `claude auth login` in a pty; status from `claude auth status`; healthz also reports `runtime` and `claude_login_pending` | ✅ / ⚠️ real CLI screen |
| vm-manager RPCs | wake on start; pending sign-in = busy for the reaper; never log the code; NOT_FOUND / FAILED_PRECONDITION / UNAVAILABLE | As asked; pending sign-in also blocks eviction by the capacity guard; the reaper also asks the Bridge (`claude_login_pending`) so a vm-manager restart doesn't stop a sign-in; interceptor logs `code=[REDACTED]` | ✅ |
| Egress | Anthropic hosts while runtime is claude_code; removed when switching back | `CLAUDE_EGRESS_DOMAINS` (api.anthropic.com, claude.ai, claude.com, platform.claude.com) merged into the user's file; also while a sign-in is pending or a sign-out runs | ✅ / ⚠️ real Squid |

### Where we filled in or changed the design

1. **Sign-in method: `claude auth login`**, not `claude setup-token`. `setup-token` "prints the token to the terminal without saving it", so we would have to capture and store the user's credential, which the rules forbid. `auth login` stores the credential itself in `$CLAUDE_CONFIG_DIR/.credentials.json`, and the docs describe exactly our case: when the browser can't reach the CLI's local callback ("common in WSL2, SSH sessions, and containers"), Anthropic's page shows a code to paste at `Paste code here if prompted` (https://code.claude.com/docs/en/authentication).
2. **Anthropic's hosts are also opened while a sign-in is pending or a sign-out runs**, not only for runtime `claude_code`. The backend connects the plan first and switches the runtime after (openapi "Connected; Claude Code is now the user's agent"), so the token exchange at `platform.claude.com` has to work while the runtime is still Hermes. Sign-out revokes the token at the same host.
3. **A new `StartClaudeLogin` replaces a pending one** (the old authorize URL stops working). One attempt per sign-in: a wrong code ends it; the user starts again.
4. **`claude_connected` is last-known.** It is refreshed on wakes, every health check (30 s; the Bridge re-asks the CLI at most every 5 minutes), sign-in and sign-out, and set to false when a turn fails with "not signed in". The Bridge reports `null` until the CLI has answered, so a fresh boot never overwrites the stored value.
5. **`SetAgentRuntime(CLAUDE_CODE)` doesn't require `claude_connected`** (it's only last-known); the backend already checks it (409), and a turn without a sign-in ends with "Connect your Claude plan again".
6. **Hermes keeps running under `claude_code`**, so switching back needs no restart. Its RAM counts either way.
7. **Env hygiene.** The Bridge removes `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_PROFILE` and the cloud-provider switches from the CLI's env, so no platform credential can ever be used in place of the user's plan. Telemetry, error reports, auto-update and claude.ai connectors are off by env var.
8. **Pre-existing flaky Bridge test fixed** (`test_exception_ends_stream_with_error` read `busy()` before the run thread's `finally`).

### Rules followed

- The Claude Code binary is installed with Anthropic's installer and run as published (checksum-verified, never patched or wrapped); no built-in auth method is removed or disabled.
- Sign-in completes through Anthropic's own pages; the CLI stores its own credentials in the user's /data volume. We never read, copy, log or return them (status comes from `claude auth status`; tests use a fake CLI whose fake credential never appears in logs).
- The pasted code goes from gRPC to the Bridge to the CLI's terminal input only. It is not logged (gRPC log: `code=[REDACTED]`; the Bridge never logs bodies and drops the terminal buffer, which echoes the code) and not stored.
- Usage bills the user's own plan; the platform doesn't pay for, resell or meter it. Composio, provider and database keys never enter the container (unchanged).

### Tests (real runs, 2026-09-30)

- `go vet ./...`: clean. golangci-lint (`--build-tags=integration`): 0 issues.
- `go test ./...` and `go test -race ./...`: all packages pass (store suite on embedded Postgres, with migration 0002).
- New Go tests: runtime stored and reported (Create/Get/List/SetAgentRuntime over gRPC), platform.json runtime, egress file includes / excludes Anthropic's hosts (switch, allowlist update, reconcile, wake, stop), sign-in start / complete / wrong code / reused / unknown / expired / stopped sandbox / Bridge down, logout, reaper and capacity guard keep a sandbox with a pending sign-in, health checks record `claude_connected`, the code never appears in manager or gRPC logs.
- Bridge: 54 Python tests pass (27 before), with a fake `claude` executable: runtime switch without restart, stream-json -> NDJSON, per-session `--session-id`/`--resume` (also after a Bridge restart), lost session, dedupe, send path, not signed in, plan limit, max turns, crash, timeout, sign-in start (ANSI/OSC 8 output, code-paste URL preferred) / complete / wrong code / timeout / expiry / replace / logout over HTTP, the code never logged.

### Not verified yet

| What | How to check |
|---|---|
| The image builds; installer, manifest signature and checksum check pass for 2.1.283 | `docker build -t personal-agent/agent:dev agent-image/` |
| `claude auth login` in a pty: prints the code-paste URL on one line, accepts the pasted code, prints "Login successful", writes `/data/claude/.credentials.json` | Sign in with a real Pro/Max account through `StartClaudeLogin` / `CompleteClaudeLogin` |
| `claude auth status` exit codes; `claude auth logout` removes and revokes | Same, then `ClaudeLogout` |
| stream-json shapes and error texts of 2.1.283 (not signed in, usage limit, `api_retry`) match what the Bridge parses | A real turn, then exhaust a plan / sign out |
| Claude Code through Squid with only the four hosts; nothing else needed (e.g. feature flags, WebFetch preflight to `api.anthropic.com` is covered) | Real turn behind the egress proxy |
| The CLI under gVisor with a read-only rootfs (writes only to /data) and its RAM next to Hermes | Integration run + `docker stats` |

## 7. Git through the platform's git gateway

The agent can use normal git on the user's GitHub repositories, and no GitHub token is ever in the container. The gateway itself lives in the backend (`/git/<owner>/<repo>`, backend HLD §5.1); this repo only points git at it.

- **`bridge/platform.py`:** at every start the Bridge writes `/data/.platform/gitconfig` (mode 0600) and includes it from `~/.gitconfig`. It rewrites `https://github.com/`, `http://github.com/`, `git@github.com:` and `ssh://git@github.com/` to the gateway (`PLATFORM_MCP_URL` with `/mcp` replaced by `/git/`) and sends the container token as the `Authorization` header. It also sets a default `user.name` / `user.email`, which the agent may override in `~/.gitconfig`.
- **`bridge/core.py`:** the turn instructions tell the agent to clone into its workspace, work on a new branch, push it and open the pull request with the GitHub app action. The gateway refuses pushes to the default branch.
- **`Dockerfile`:** `GIT_TERMINAL_PROMPT=0`, so git fails instead of waiting for a password.
- **No Go or proto change.** Git reaches the api through the egress proxy like the Platform MCP does; the Squid rule is in the backend repo.

**Tests (real run, 2026-10-01):** 57 Bridge tests pass. One runs the real `git ls-remote` for each URL form against a local HTTP server and checks the request path (`/git/<owner>/<repo>/info/refs?service=git-upload-pack`) and the `Authorization` header.

**Not verified yet:** a real clone, push and pull request from a container on the server (needs a turn from a user with GitHub connected).

