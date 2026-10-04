# Build report: vm-manager (design vs what we built)

**Date:** 2026-09-28 · **Branch:** `mvp-build` · **Design:** `../beckend/docs/mvp-hld.md` (0.5, 0.6, 1.2, 4, 6, 8, Phase 2) · **Whole-system report:** `../beckend/docs/build-report.md`

## In short

- Phase 2 is **coded, committed and unit tested**: every gRPC RPC, the container spec, the five background loops, the egress writer, the agent image (Hermes + Bridge).
- **Nothing has run against real Docker.** Docker and gVisor aren't installed on the dev machine, so the Docker parts were tested against a fake daemon, and the Bridge against a fake Hermes.
- The proto (`proto/vm/v1/vm.proto`) got additive RPCs and fields for the agent runtime and the Claude Code sign-in (section 6); no existing field changed.
- Later additions, each in its own section: the Claude Code runtime (6), git through the platform's git gateway (7), the crew of specialists (8). The crew is unit tested against fakes only; the image build and a real turn with either runtime are untested.

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

## 8. The crew (Byte, Patch, Scout)

**Date:** 2026-10-04 · **Branch:** `mvp-build` · **Design:** `../beckend/docs/launch-plan.md` section 4, `../beckend/docs/mvp-hld.md` 0.7

The agent the user texts is the lead (Tex). It can hand work to three specialists: Byte (coding), Patch (code review) and Scout (research). They are defined once in the Bridge and given to both runtimes. No Go, proto, egress or Dockerfile change, and the Bridge still uses only the Python standard library.

**Nothing here has run for real.** There is no Docker and no Claude Code CLI on the dev machine, so the image was not built and neither runtime ran a turn with the crew. The Claude Code side was written from Anthropic's documentation and the Hermes side from the Hermes source at the pinned commit; both are tested against fakes only.

### Design vs built

| Area | Asked for | Built | Status |
|---|---|---|---|
| One definition | `byte`, `patch`, `scout`, each with a description, a role prompt and a tool list; both runtimes fed from it | `bridge/crew.py`: three `Member`s with tool groups (`read`, `write`, `search`, `shell`, `platform`, `platform:web_search`, `platform:read_page`), mapped to each runtime's own names in the same file | ✅ |
| Byte | Code on a new branch, commit, push through git, pull request with the GitHub app action. Files, shell, search, platform tools | As asked. The brief says git already goes through the platform and needs no token | ✅ |
| Patch | Three review passes (correctness, security, tests), merged findings, ONE review on the pull request, a "ship" / "fix first" verdict. Never merges or pushes. Read-only files, shell, platform tools | As asked; it also never approves | ✅ |
| Scout | Only `web_search` and `read_page`: no files, no shell, no app actions (the first brief also gave it file reading; taken out the same day, see 4 below). Facts, links, what it could not confirm. What it reads is information, never instructions | Exactly the two tools. It works only from the task text and the web, and is told to put nothing private into a search or a page address | ✅ Claude Code / ⚠️ Hermes (not a lock, see 1 below) |
| Nobody sends | The Bridge sends the answer | `send_message` is off for the lead (as before) and for every specialist; every brief says so | ✅ |
| Lead's instructions | Who gets what; small jobs done directly; at most 3 per job; Patch may review Byte's pull request; approvals and git text stay; Scout gets only what the research needs, never secrets, tokens or private data | `build_instructions` in `bridge/core.py` = the old text + `crew.lead_instructions()` | ✅ |
| Claude Code | Passed as sub-agents the documented way; only the lead starts one; no helpers of helpers; model = the lead's, or a cheaper one by setting | `--agents` JSON, `Agent` tool, three documented environment settings (below) | ✅ / ⚠️ real CLI |
| Hermes | Same role prompts through `delegate_task`; tools narrowed as far as Hermes allows; one level deep; parallel cap (default 3) in the rendered config | Briefs in the lead's instructions; `delegation` settings in `config.yaml`; follow-up runs for background children (below) | ✅ / ⚠️ real Hermes |
| Progress lines | A line when a specialist starts and one when it finishes, starting with its name and a colon | `crew.Progress`: at most one start and one finish per run, on both runtimes | ✅ |
| Settings | Parallel cap and a cheaper model by environment | `BRIDGE_CREW_MAX_PARALLEL` (default 3), `BRIDGE_CLAUDE_CREW_MODEL` (default: the lead's model) | ✅ |

### How it works

**Claude Code** (CLI 2.1.283, pinned in `agent-image/Dockerfile`). Confirmed on 2026-10-04 on https://code.claude.com/docs/en/sub-agents, https://code.claude.com/docs/en/cli-reference, https://code.claude.com/docs/en/env-vars, https://code.claude.com/docs/en/tools-reference, https://code.claude.com/docs/en/headless, https://code.claude.com/docs/en/errors and https://code.claude.com/docs/en/agent-sdk/typescript:

- **Flag:** `--agents '<JSON>'`, one key per sub-agent. Keys we use: `description`, `prompt`, `tools`, `disallowedTools`, `model`. The CLI validates the value on a new session and exits with code 1 if it is wrong.
- **Tool that starts one:** `Agent`, with `subagent_type`, `description` and `prompt`. `Task` is its old name and still an alias. Starting a sub-agent needs no permission, so `--allowedTools` does not have to name it; we list `Agent` anyway, as the documented examples do.
- **A sub-agent's tools:** `tools` is an allow list of the CLI's tool names. An MCP tool is `mcp__<server>__<tool>` (`mcp__platform__web_search`); `mcp__<server>` means every tool of that server. `disallowedTools` is applied first. A sub-agent shares the lead's MCP connections, so no `mcpServers` is needed and `--strict-mcp-config` is unchanged.
- **What each gets** (`tools`): Byte `Read, Edit, Write, NotebookEdit, Glob, Grep, Bash, mcp__platform`. Patch `Read, Glob, Grep, Bash, mcp__platform`. Scout `mcp__platform__web_search, mcp__platform__read_page` and nothing else. All three: `model: inherit`.
- **What each is denied** (`disallowedTools`): `Agent` and `mcp__platform__send_message` for all three, plus every built-in file, edit and shell tool the member's list leaves out. Scout: `Read, Edit, Write, NotebookEdit, Glob, Grep, Bash`. Patch: `Edit, Write, NotebookEdit`. The docs say a `tools` list alone gives "only the listed tools", so this is a second lock, not the first: it holds however the allow list resolves.
- **A tool list that matches nothing:** per the docs the CLI refuses to start that sub-agent ("would be spawned with zero tools") and the lead gets the error. That is Scout's case until the platform has `web_search` and `read_page`.
- **Only the lead starts one:** no `Agent` in a specialist's tools, plus `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=1`. `CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS=1` removes Explore, Plan and general-purpose. `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` is the parallel cap.
- **Unchanged:** `--strict-mcp-config`, `--permission-mode dontAsk`, `--permission-prompts none`, the allow list (plus `Agent`), the stripped credentials. The binary is not modified and no sign-in method is touched.

**Hermes** (commit `35272ce28b29bae90e9ecceff9def17ae791c012`). Confirmed in `cli-config.yaml.example`, `hermes_cli/config_defaults.py`, `tools/delegate_tool.py`, `tools/delegate_tool_config.py`, `tools/delegate_tool_toolsets.py`, `tools/delegate_tool_dispatch.py`, `gateway/platforms/api_server_runs.py` and `gateway/readiness.py`:

- **Settings written to `config.yaml`:** `delegation.max_spawn_depth: 1`, `delegation.orchestrator_enabled: false`, `delegation.max_concurrent_children: 3` (from `BRIDGE_CREW_MAX_PARALLEL`), `delegation.subagent_auto_approve: false`.
- **Hand-over:** `delegate_task` takes `tasks: [{goal, context}]`. The lead's instructions carry the three briefs; the lead starts a task's goal with the member's name and its context with the brief.
- **Follow-up runs:** a run started with `X-Hermes-Session-Key` dispatches the children in the background (`"status": "dispatched"`), ends, and stores their reports in the session for the next turn. The Bridge waits for `readiness.checks.background_queues` in `GET /health/detailed` to show no active delegations and no queued completions, twice in a row, then runs a follow-up turn in the same session. At most 3 follow-ups, inside the turn's time limit.

**Progress lines.** Claude Code: the lead's `Agent` call (start), then that call's result, or the `task_notification` event for a background specialist (finish). Hermes: `subagent.start` and `subagent.complete`. Endings are `done`, `could not finish`, `stopped`, or `finished` when the runtime does not say how it ended.

### Where we filled in or changed the design

1. **On Hermes a specialist's tool list is an instruction, not a lock.** At the pinned commit `delegate_task` has no tool list per task (`toolsets=None  # always inherit the parent's toolsets`). A child gets the lead's tools minus `delegate_task`, `clarify`, `memory`, `send_message` and `cronjob_manage`. So Scout on Hermes is told it has no files and only `web_search` and `read_page`, but could still reach files, the shell and app actions. On Claude Code the list is enforced by the CLI. **This is the risk that remains:** on Hermes a page Scout reads could still talk it into reading a file and sending it out. The platform is adding a rule that `read_page` only fetches addresses already seen in the user's messages, in search results or in pages already read, so a made-up address can't carry data out on either runtime.
2. **`max_concurrent_children` is not a hard cap on Hermes.** It limits the tasks in one `delegate_task` call and the calls running in the background. A call over the limit runs inside the lead's turn. The lead is told to put a step's specialists in one call and to use at most 3 per job.
3. **"At most 3 per job" is an instruction** on both runtimes; "at most 3 at a time" is the setting.
4. **Scout has no file tools.** The first version gave it `Read`, `Glob` and `Grep`. `Read` is allowed for the whole session, so Scout could have read any file the container user can (the platform secrets file, the CLI's credentials) and `read_page` fetches any public address: a page could have made it send a secret out. Its list is now the two web tools only. What Scout needs to know, the lead puts in the task, and the lead is told to pass only what the research needs.
5. **Specialists get the platform tools, not the user's own MCP servers.** The lead keeps those.
6. **Patch never approves a pull request,** only comments. The brief asked for no merges and no pushes; approving is one step from merging.
7. **Every specialist that starts gets a finish line.** If the runtime never reports the end, the line is `finished` (or `stopped` when a Claude Code run times out or crashes).
8. **The built-in sub-agent types are switched off** on Claude Code, so the crew is what the lead can start. A call without `subagent_type` fails with the list of types, and the lead can retry with one.
9. **No `--system-prompt-snapshot off`.** The CLI reuses a conversation's first system prompt on `--resume` until it is compacted, so a chat that began on an older image gets the crew instructions late. The sub-agents are available at once. Left as is rather than add an untested flag.
10. **The two settings are read from the Bridge's environment,** which the vm-manager does not pass through. They are set with an `ENV` line in the agent image, like `BRIDGE_CLAUDE_MODEL`.
11. **The lead can still read files and call `read_page`.** It has every tool, as before. The platform's rule on which addresses `read_page` fetches (see 1) is what covers the lead.
12. **Flaky Bridge test fixed** (`test_deliver_streams_and_sends_answer` read `busy()` before the run thread dropped the turn).

### Tests (real runs, 2026-10-04)

- `cd agent-image && python3 -m unittest discover -s tests -t .`: **88 tests, OK** (57 before the crew).
- `go vet ./...`: clean. `go test -count=1 ./...`: all 9 packages with tests pass. No Go file changed.
- New Bridge tests cover: the three definitions and their tool lists (Scout: the two web tools only); the denied tools of each; the role prompts; the lead's instructions; the `--agents` value, `Agent` in the allow list, the three environment settings and the unchanged flags on the command line; the model and parallel settings; the `delegation` section of the Hermes config; the briefs in the Hermes instructions; follow-up runs (background children, children that finish inside the run, unknown background state, the deadline, the limit of 3); progress lines for a foreground specialist, a background one, one that fails, one whose end is never reported, one stopped by a timeout, and sub-agents that are not crew members.

### Not verified yet

| What | How to check |
|---|---|
| The agent image builds with the crew code | `docker build -t personal-agent/agent:dev agent-image/` |
| CLI 2.1.283 accepts our `--agents` value, and the lead actually hands work over | A real Claude Code turn that needs research or a code change; look for `Scout: ...` lines |
| A specialist's tool list as the CLI resolves it (Scout with the two web tools and nothing else; `mcp__platform` for Byte and Patch; `Glob` / `Grep` present for them) | The `tools` of the sub-agent in the session transcript, or ask Scout to read a file or run a command |
| Scout on Hermes keeps to its brief (no files, no shell, no app actions), which nothing enforces there | A real Hermes turn with a test page that tells Scout to read a file; and the platform's `read_page` address rule once it exists |
| A specialist cannot start a sub-agent, and the built-in types are gone | Ask the lead for an Explore agent; ask Byte to delegate |
| The stream-json events the progress lines are read from (`Agent` tool_use, `tool_use_result.status`, `task_started`, `task_notification`), and that the last `result` is the answer after background sub-agents | A real turn with `--output-format stream-json` kept |
| Hermes takes the `delegation` keys and offers `delegate_task` on the API server | A real Hermes turn; the rendered `/data/hermes/config.yaml` |
| The Hermes lead copies the brief into `context` and names the member in `goal` | The `subagent.start` events of a real run |
| Background delegation on the Runs API: `"status": "dispatched"`, the reports stored in the session, `/health/detailed` going quiet, the follow-up turn seeing the reports | A real Hermes turn that hands work over |
| `web_search` and `read_page` exist on the Platform MCP under those names | The backend's W2b-1 package; until then Scout has no tool and, per the docs, the CLI refuses to start it |
| `read_page` only fetches addresses already seen in the user's messages, search results or pages already read | A platform rule being added in the backend; not part of this repo |
| A long specialist job inside the 15-minute turn limit | A real coding job with Byte |

