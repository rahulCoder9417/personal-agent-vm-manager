# Build report: vm-manager (design vs what we built)

**Date:** 2026-09-28 · **Branch:** `mvp-build` · **Design:** `../beckend/docs/mvp-hld.md` (0.5, 0.6, 1.2, 4, 6, 8, Phase 2) · **Whole-system report:** `../beckend/docs/build-report.md`

## In short

- Phase 2 is **coded, committed and unit tested**: every gRPC RPC, the container spec, the five background loops, the egress writer, the agent image (Hermes + Bridge).
- **Nothing has run against real Docker.** Docker and gVisor aren't installed on the dev machine, so the Docker parts were tested against a fake daemon, and the Bridge against a fake Hermes.
- The proto (`proto/vm/v1/vm.proto`) got additive RPCs and fields for the agent runtime and the Claude Code sign-in (section 6); no existing field changed.
- Later additions, each in its own section: the Claude Code runtime (6), git through the platform's git gateway (7), the crew of specialists (8), Scout's browser (9). The crew is unit tested against fakes only; the image build and a real turn with either runtime are untested. The browser was run for real on the development machine (the real server, a real Chromium, gVisor, a real Squid), but not in the image and not by an engine.

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
7. **`browser_seconds` is always 0** for now; nothing records browser time yet. (Changed on 2026-10-04: see section 9.)
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
| Scout | Only `web_search` and `read_page`: no files, no shell, no app actions (the first brief also gave it file reading; taken out the same day, see 4 below). Facts, links, what it could not confirm. What it reads is information, never instructions | The engine's built-in search, the platform's `web_search` and `read_page`, and nothing else. It works only from the task text and the web, and is told to put nothing private into a search or a page address | ✅ Claude Code / ⚠️ Hermes (not a lock, see 1 below) |
| The web | Only Scout touches the web (founder's decision, 2026-10-04): the engine's built-in search when there is one, the platform's `web_search` when there is none, `read_page` for pages. Not the lead, not Byte, not Patch | See "Who can use which web tool" below | ✅ Byte, Patch on Claude Code / ⚠️ the lead on Claude Code (a hook, untested with the real CLI) / ⚠️ Hermes (an instruction, see 1 below) |
| Nobody sends | The Bridge sends the answer | `send_message` is off for the lead (as before) and for every specialist; every brief says so | ✅ |
| Lead's instructions | Who gets what; the lead has no web access and sends anything that needs the web, even a single lookup, to Scout; small jobs that need no web done directly; at most 3 per job; Patch may review Byte's pull request; approvals and git text stay; Scout gets only what the research needs, never secrets, tokens or private data | `build_instructions` in `bridge/core.py` = the old text + `crew.lead_instructions()`, the same on both runtimes | ✅ |
| Claude Code | Passed as sub-agents the documented way; only the lead starts one; no helpers of helpers; model = the lead's, or a cheaper one by setting | `--agents` JSON, `Agent` tool, three documented environment settings (below) | ✅ / ⚠️ real CLI |
| Hermes | Same role prompts through `delegate_task`; tools narrowed as far as Hermes allows; one level deep; parallel cap (default 3) in the rendered config | Briefs in the lead's instructions; `delegation` settings in `config.yaml`; follow-up runs for background children (below) | ✅ / ⚠️ real Hermes |
| Progress lines | A line when a specialist starts and one when it finishes, starting with its name and a colon | `crew.Progress`: at most one start and one finish per run, on both runtimes | ✅ |
| Settings | Parallel cap and a cheaper model by environment | `BRIDGE_CREW_MAX_PARALLEL` (default 3), `BRIDGE_CLAUDE_CREW_MODEL` (default: the lead's model) | ✅ |

### How it works

**Claude Code** (CLI 2.1.283, pinned in `agent-image/Dockerfile`). Confirmed on 2026-10-04 on https://code.claude.com/docs/en/sub-agents, https://code.claude.com/docs/en/cli-reference, https://code.claude.com/docs/en/env-vars, https://code.claude.com/docs/en/tools-reference, https://code.claude.com/docs/en/headless, https://code.claude.com/docs/en/errors, https://code.claude.com/docs/en/agent-sdk/typescript, and for the web rule https://code.claude.com/docs/en/permissions, https://code.claude.com/docs/en/hooks and https://code.claude.com/docs/en/settings:

- **Flag:** `--agents '<JSON>'`, one key per sub-agent. Keys we use: `description`, `prompt`, `tools`, `disallowedTools`, `model`. The CLI validates the value on a new session and exits with code 1 if it is wrong.
- **Tool that starts one:** `Agent`, with `subagent_type`, `description` and `prompt`. `Task` is its old name and still an alias. Starting a sub-agent needs no permission, so `--allowedTools` does not have to name it; we list `Agent` anyway, as the documented examples do.
- **A sub-agent's tools:** `tools` is an allow list of the CLI's tool names. An MCP tool is `mcp__<server>__<tool>` (`mcp__platform__web_search`); `mcp__<server>` means every tool of that server. `disallowedTools` is applied first. A sub-agent shares the lead's MCP connections, so no `mcpServers` is needed and `--strict-mcp-config` is unchanged.
- **What each gets** (`tools`): Byte `Read, Edit, Write, NotebookEdit, Glob, Grep, Bash, mcp__platform`. Patch `Read, Glob, Grep, Bash, mcp__platform`. Scout `WebSearch, mcp__platform__web_search, mcp__platform__read_page` and nothing else (the built-in `WebSearch` was added on 2026-10-04 so a user on a Claude plan needs no search provider key; its query goes to the model provider, and `read_page` does not open its results). `WebFetch` is denied for all three. All three: `model: inherit`.
- **What each is denied** (`disallowedTools`): `Agent`, `mcp__platform__send_message` and `WebFetch` for all three, plus every built-in file, edit, shell and web-search tool the member's list leaves out. Scout: `Read, Edit, Write, NotebookEdit, Glob, Grep, Bash`. Patch: `Edit, Write, NotebookEdit, WebSearch`. Byte: `WebSearch`. Byte and Patch also: `mcp__platform__web_search` and `mcp__platform__read_page`, because their `tools` names the whole Platform MCP. The docs say a `tools` list alone gives "only the listed tools", so for the built-in tools this is a second lock, not the first: it holds however the allow list resolves.
- **A tool list that matches nothing:** per the docs the CLI refuses to start that sub-agent ("would be spawned with zero tools") and the lead gets the error. That is Scout's case until the platform has `web_search` and `read_page`.
- **Only the lead starts one:** no `Agent` in a specialist's tools, plus `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=1`. `CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS=1` removes Explore, Plan and general-purpose. `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` is the parallel cap.
- **Unchanged:** `--strict-mcp-config`, `--permission-mode dontAsk`, `--permission-prompts none`, `--setting-sources user`, the stripped credentials. The binary is not modified and no sign-in method is touched.

**Who can use which web tool** (founder's decision, 2026-10-04: only Scout).

| | Claude Code | Hermes |
|---|---|---|
| Scout: search | The CLI's built-in `WebSearch`. It runs on the user's Claude plan and costs the platform nothing. `mcp__platform__web_search` is in its list too; its brief says to use it only when there is no built-in search | The platform's `web_search`: a paid feature of the Max plan, backed by a search provider. It says so when it is not available, and Scout reports that |
| Scout: pages | `mcp__platform__read_page` | The platform's `read_page` |
| The lead | `--allowedTools`: `Bash, Read, Edit, Write, Glob, Grep, NotebookEdit, TodoWrite, WebSearch, mcp__platform, mcp__user_<id>…, Agent`. `--disallowedTools`: `mcp__platform__send_message, WebFetch`. `WebSearch` and the two platform web tools are there for Scout; a hook refuses the lead's own calls to them | The whole tool list, as before. Told it has no web access. Not a lock |
| Byte | `tools`: `Read, Edit, Write, NotebookEdit, Glob, Grep, Bash, mcp__platform`. `disallowedTools`: `Agent, mcp__platform__send_message, mcp__platform__web_search, mcp__platform__read_page, WebFetch, WebSearch` | The lead's tools. Told in its brief it has no web tool. Not a lock |
| Patch | `tools`: `Read, Glob, Grep, Bash, mcp__platform`. `disallowedTools`: `Agent, mcp__platform__send_message, mcp__platform__web_search, mcp__platform__read_page, WebFetch, Edit, Write, NotebookEdit, WebSearch` | The lead's tools. Told in its brief it has no web tool. Not a lock |
| Scout | `tools`: `WebSearch, mcp__platform__web_search, mcp__platform__read_page`. `disallowedTools`: `Agent, mcp__platform__send_message, WebFetch, Read, Edit, Write, NotebookEdit, Glob, Grep, Bash` | The lead's tools. Told in its brief which tools are its own. Not a lock |

What the Claude Code docs say, and what follows from it:

- **Deny wins over allow.** "Rules are evaluated in order: deny, then ask, then allow. The first match in that order determines the outcome, and rule specificity doesn't change the order" (permissions page). So `mcp__platform__web_search` in a deny list beats `mcp__platform` in an allow list. A bare tool name in a deny list "removes the tool from Claude's context entirely".
- **A deny on the session also applies to its sub-agents.** Sub-agents "inherit the built-in tools and MCP tools available in the main conversation" and a tool that is not available to them "is never granted, even when listed in `tools`" (sub-agents page, "Available tools"; tools reference, "Agent tool behavior"). A deny rule in the settings "applies to the main conversation and to subagents". So denying `WebSearch`, `web_search` or `read_page` with `--disallowedTools` would take them from Scout too. They are not denied there. `WebFetch` is: nobody needs it.
- **The allow list is shared too.** "Claude Code checks the subagent's own tool calls against your permission rules as it runs" (tools reference). In `dontAsk` mode a call that no allow rule covers is refused. So `WebSearch` stays in `--allowedTools`: without it Scout could not search. `WebFetch` is taken out.
- **The documented way to tell the lead from a sub-agent is a hook.** A `PreToolUse` hook from the settings runs before a matching tool call in the main conversation and inside sub-agents. Its input has `agent_id` "only when the hook fires inside a subagent call. Use this to distinguish subagent hook calls from main-thread calls" (hooks page, "Common input fields"). Exit code 2 blocks the call, even when an allow rule matches, and the model sees the hook's stderr as the reason.
- **What the Bridge does:** it writes `/data/.platform/claude-settings.json` (0600, every turn) and passes it with `--settings`. The file has one `PreToolUse` hook with the matcher `WebSearch|mcp__platform__web_search|mcp__platform__read_page` (a list of exact names) and the command `<python> -I /opt/bridge/bridge/lead_gate.py <the three names>`, timeout 10 s. `bridge/lead_gate.py` exits 2 with "hand this to Scout" when the call has no `agent_id` (and no `agent_type`, which the docs give a sub-agent's call as well), and 0 otherwise. It uses only the standard library and runs on its own.
- **What this lock is and is not.** The lead still sees the three tools in its list; calling one fails. If the CLI cannot run the hook, the hook times out or its input cannot be read, the call goes through (per the docs, only exit code 2 blocks). That was chosen on purpose: a broken hook must not take the web from Scout. In that case the lead's instruction is all that is left, as on Hermes.

**Hermes** (commit `35272ce28b29bae90e9ecceff9def17ae791c012`). Confirmed in `cli-config.yaml.example`, `hermes_cli/config_defaults.py`, `tools/delegate_tool.py`, `tools/delegate_tool_config.py`, `tools/delegate_tool_toolsets.py`, `tools/delegate_tool_dispatch.py`, `gateway/platforms/api_server_runs.py` and `gateway/readiness.py`:

- **Settings written to `config.yaml`:** `delegation.max_spawn_depth: 1`, `delegation.orchestrator_enabled: false`, `delegation.max_concurrent_children: 3` (from `BRIDGE_CREW_MAX_PARALLEL`), `delegation.subagent_auto_approve: false`.
- **Hand-over:** `delegate_task` takes `tasks: [{goal, context}]`. The lead's instructions carry the three briefs; the lead starts a task's goal with the member's name and its context with the brief.
- **Follow-up runs:** a run started with `X-Hermes-Session-Key` dispatches the children in the background (`"status": "dispatched"`), ends, and stores their reports in the session for the next turn. The Bridge waits for `readiness.checks.background_queues` in `GET /health/detailed` to show no active delegations and no queued completions, twice in a row, then runs a follow-up turn in the same session. At most 3 follow-ups, inside the turn's time limit.

**Progress lines.** Claude Code: the lead's `Agent` call (start), then that call's result, or the `task_notification` event for a background specialist (finish). Hermes: `subagent.start` and `subagent.complete`. Endings are `done`, `could not finish`, `stopped`, or `finished` when the runtime does not say how it ended.

### Where we filled in or changed the design

1. **On Hermes a specialist's tool list is an instruction, not a lock.** At the pinned commit `delegate_task` has no tool list per task (`toolsets=None  # always inherit the parent's toolsets`). A child gets the lead's tools minus `delegate_task`, `clarify`, `memory`, `send_message` and `cronjob_manage`. So Scout on Hermes is told it has no files and only `web_search` and `read_page`, but could still reach files, the shell and app actions. The web rule is the same: the Hermes lead, Byte and Patch are told they have no web tool, but `web_search` and `read_page` stay in their list (the Hermes config can hide a platform tool only for everyone, which would take it from Scout too). On Claude Code the list is enforced by the CLI. **This is the risk that remains:** on Hermes a page Scout reads could still talk it into reading a file and sending it out. The platform is adding a rule that `read_page` only fetches addresses already seen in the user's messages, in search results or in pages already read, so a made-up address can't carry data out on either runtime.
2. **`max_concurrent_children` is not a hard cap on Hermes.** It limits the tasks in one `delegate_task` call and the calls running in the background. A call over the limit runs inside the lead's turn. The lead is told to put a step's specialists in one call and to use at most 3 per job.
3. **"At most 3 per job" is an instruction** on both runtimes; "at most 3 at a time" is the setting.
4. **Scout has no file tools.** The first version gave it `Read`, `Glob` and `Grep`. `Read` is allowed for the whole session, so Scout could have read any file the container user can (the platform secrets file, the CLI's credentials) and `read_page` fetches any public address: a page could have made it send a secret out. Its list is now the two web tools only. What Scout needs to know, the lead puts in the task, and the lead is told to pass only what the research needs.
5. **Specialists get the platform tools, not the user's own MCP servers.** The lead keeps those.
6. **Patch never approves a pull request,** only comments. The brief asked for no merges and no pushes; approving is one step from merging.
7. **Every specialist that starts gets a finish line.** If the runtime never reports the end, the line is `finished` (or `stopped` when a Claude Code run times out or crashes).
8. **The built-in sub-agent types are switched off** on Claude Code, so the crew is what the lead can start. A call without `subagent_type` fails with the list of types, and the lead can retry with one.
9. **No `--system-prompt-snapshot off`.** The CLI reuses a conversation's first system prompt on `--resume` until it is compacted, so a chat that began on an older image gets the crew instructions late. The sub-agents are available at once. Left as is rather than add an untested flag.
10. **The two settings are read from the Bridge's environment,** which the vm-manager does not pass through. They are set with an `ENV` line in the agent image, like `BRIDGE_CLAUDE_MODEL`.
11. **The lead can still read files.** On Claude Code it can no longer call `read_page` or search the web (13). On Hermes it still can, against its instructions; the platform's rule on which addresses `read_page` fetches (see 1) is what covers that.
12. **Flaky Bridge test fixed** (`test_deliver_streams_and_sends_answer` read `busy()` before the run thread dropped the turn).
13. **The lead's web tools are refused by a hook, not denied.** The plan was to put `WebSearch`, `mcp__platform__web_search` and `mcp__platform__read_page` into the lead's `--disallowedTools` and take `WebSearch` out of `--allowedTools`. The docs say both lists belong to the whole session, sub-agents included, so that would have left Scout without a search. Only `WebFetch` is handled that way. For the three tools Scout needs, the lead's own calls are refused by a `PreToolUse` hook (see "Who can use which web tool").
14. **Byte and Patch are told they have no web tool,** and their tool line says "the platform tools (not web_search or read_page)". On Claude Code the deny list enforces it; on Hermes the sentence is all there is.
15. **The user's own MCP servers stay with the lead.** If a user connects a search server of their own, the lead can use it. The rule covers the built-in tools and the platform's.

### Tests (real runs, 2026-10-04)

- `cd agent-image && python3 -m unittest discover -s tests -t .`: **98 tests, OK** (57 before the crew, 88 before the web rule).
- `go vet ./...`: clean. `go test -count=1 ./...`: all 9 packages with tests pass. No Go file changed.
- New Bridge tests cover: the three definitions and their tool lists (Scout: the two web tools only); the denied tools of each; the role prompts; the lead's instructions; the `--agents` value, `Agent` in the allow list, the three environment settings and the unchanged flags on the command line; the model and parallel settings; the `delegation` section of the Hermes config; the briefs in the Hermes instructions; follow-up runs (background children, children that finish inside the run, unknown background state, the deadline, the limit of 3); progress lines for a foreground specialist, a background one, one that fails, one whose end is never reported, one stopped by a timeout, and sub-agents that are not crew members.
- The web rule: only Scout has a web tool group; the exact `--allowedTools` and `--disallowedTools` of the session; the denied tools of Byte and Patch; nothing Scout uses is denied for the session; the settings file (path, mode 0600, the matcher, the command); `lead_gate.py` run as the hook runs it (the lead's call to each of the three tools is refused with exit code 2 and the reason; a sub-agent's call passes; other tools pass; input it cannot read passes); a whole turn against the fake CLI, which runs the `PreToolUse` hooks of `--settings` through `sh -c` the way the docs describe them (the lead's three calls refused, its other platform tool fine, Scout's three calls fine); the lead's instructions and the three briefs, also as the Hermes run receives them; the Hermes config still hides only `send_message`.

### Not verified yet

| What | How to check |
|---|---|
| The agent image builds with the crew code | `docker build -t personal-agent/agent:dev agent-image/` |
| CLI 2.1.283 accepts our `--agents` value, and the lead actually hands work over | A real Claude Code turn that needs research or a code change; look for `Scout: ...` lines |
| A specialist's tool list as the CLI resolves it (Scout with the two web tools and nothing else; `mcp__platform` for Byte and Patch; `Glob` / `Grep` present for them) | The `tools` of the sub-agent in the session transcript, or ask Scout to read a file or run a command |
| Scout on Hermes keeps to its brief (no files, no shell, no app actions), which nothing enforces there | A real Hermes turn with a test page that tells Scout to read a file; and the platform's `read_page` address rule once it exists |
| The CLI runs the `PreToolUse` hook from the `--settings` file in `-p` mode, gives it `agent_id` for a sub-agent's call and none for the lead's, and blocks the call on exit code 2. The fake CLI in the tests does this because the docs say so; the real one was not run | A real Claude Code turn: ask for something that needs a search. The lead's own `WebSearch` must fail with "hand this to Scout", and `Scout: ...` must follow and find results |
| Scout's built-in `WebSearch` works for a sub-agent in `dontAsk` mode with `WebSearch` in the session's allow list, and still works with the hook in place | The same turn: a `using WebSearch` line between `Scout: ...` and `Scout: done`, and links in the answer |
| `--disallowedTools` with two values, and `WebFetch` gone for the lead and every specialist | The same turn's `system/init` event lists the session's tools |
| Byte and Patch have every platform tool except `send_message`, `web_search` and `read_page` | The `tools` of the sub-agent in the session transcript, or ask Byte to look something up on the web |
| The Hermes lead, Byte and Patch keep off `web_search` and `read_page`, which nothing enforces there | A real Hermes turn that needs a search: the `subagent.start` goal must begin with `Scout:` and no `using web_search` line may come before it |
| `web_search` says that it is a Max-plan feature when the user's plan does not have it | The backend's tool; not part of this repo |
| A specialist cannot start a sub-agent, and the built-in types are gone | Ask the lead for an Explore agent; ask Byte to delegate |
| The stream-json events the progress lines are read from (`Agent` tool_use, `tool_use_result.status`, `task_started`, `task_notification`), and that the last `result` is the answer after background sub-agents | A real turn with `--output-format stream-json` kept |
| Hermes takes the `delegation` keys and offers `delegate_task` on the API server | A real Hermes turn; the rendered `/data/hermes/config.yaml` |
| The Hermes lead copies the brief into `context` and names the member in `goal` | The `subagent.start` events of a real run |
| Background delegation on the Runs API: `"status": "dispatched"`, the reports stored in the session, `/health/detailed` going quiet, the follow-up turn seeing the reports | A real Hermes turn that hands work over |
| `web_search` and `read_page` exist on the Platform MCP under those names | The backend's W2b-1 package; until then Scout has no tool and, per the docs, the CLI refuses to start it |
| `read_page` only fetches addresses already seen in the user's messages, search results or pages already read | Built in the backend for Hermes turns; on a Claude Code turn it opens any public address. Scout's brief says which since 2026-10-08 (section 9, "The four findings") |
| A long specialist job inside the 15-minute turn limit | A real coding job with Byte |

## 9. Scout's browser, first version

**Date:** 2026-10-04 · **Branch:** `mvp-build` · **Decision:** the founder's, of 2026-10-04, which replaces the earlier plan of a hosted browser driven by six platform tools (`browser_open` … `browser_login`): the browser runs **inside the user's own container**, it is a feature of the **Max plan** only, it is charged at 2 credits per minute of browser time, and **only Scout drives it**. A small first version: no sign-ins to sites.

**What ran and what did not.** There is no Docker on the development machine, so the agent image was not built and no engine drove the browser. Unlike sections 6 to 8, the parts could be run for real there: the real Playwright MCP server (0.0.83) and the Chromium it pins, started by the Bridge's own command and settings behind the guard; in a box with a read-only root, uid 10001 and only `/data` writable; under gVisor itself (the server's `runsc` release, rootless); and through a real Squid loaded with the rule the vm-manager generates. The details are under "Tests". What is left is in "Not verified yet".

**Read for this work (2026-10-04).** Playwright MCP: the README as shipped in the `@playwright/mcp` 0.0.83 package and at https://github.com/microsoft/playwright-mcp (its flags, the configuration file, the tool list, "User profile", "Security", "Docker"), the server's own `--help`, its tool list as the running server gives it, and its Dockerfile at the tag v0.0.83. Claude Code: https://code.claude.com/docs/en/mcp for the form of a stdio server in the MCP config, and the pages named in section 8 for the session's permission rules and the hook. Hermes at the pinned commit: `cli-config.yaml.example` (`mcp_servers`), `tools/mcp_tool.py`, `tools/mcp_tool_transport.py` and `tools/mcp_tool_config.py` (how a stdio server is started and with which environment), `toolsets.py` (the built-in `browser` toolset).

### Asked for vs built

| # | Asked for | Built | Status |
|---|---|---|---|
| 1 | A per-user switch: stored per sandbox, returned in `Sandbox`, written into `platform.json` | `sandboxes.browser_enabled` (migration 0003), `CreateSandboxRequest.browser_enabled = 8`, `Sandbox.browser_enabled = 10`, `SetBrowser`. `platform.json` gets `"browser_enabled": true` at the wake | ✅ |
| 2 | Chromium headless in the image under its constraints, versions pinned | A build stage on `node:24.21.0-trixie-slim`; `@playwright/mcp` 0.0.83 by lockfile; Chrome for Testing 155.0.8059.12 through `playwright-core install --no-shell chromium`; libraries and fonts through `install-deps chromium`; the build runs the browser's self-check | ✅ written / ⚠️ never built |
| 3 | A way for the engines to drive it, a maintained server preferred | Microsoft's Playwright MCP, over stdio, behind a guard of ours, in Claude Code's MCP config and in the Hermes config | ✅ / ⚠️ not started by an engine yet |
| 4 | Only Scout drives it; one list of Scout's web tools | The 14 browser tools are in the one list (`crew.WEB_GROUPS`): Scout's `tools`, the deny lists of Byte and Patch and the lead's gate come from it. On Hermes an instruction | ✅ Claude Code / ⚠️ Hermes (not a lock) |
| 5 | Instructions for Scout and the lead | In `bridge/crew.py`; Scout's brief has the browser part only when the browser is turned on, and says it has none otherwise | ✅ |
| 6 | A higher memory limit for a browser container | `SANDBOX_BROWSER_MEMORY_MB` (2048). Also `SANDBOX_BROWSER_PIDS_LIMIT` (1024), which was not asked for | ✅ |
| 7 | That container, and only it, reaches the public web on 80 and 443; internal destinations stay refused | A second form of the per-user Squid file (`egress.Rule{OpenWeb}`), written for the container that was started | ✅ / ⚠️ not in the backend's Squid yet |
| 8 | Browser time from start to close, reported with the turn, stored per user, returned once | `BrowserMeter` in the Bridge; `browser_seconds` + `browser_report_id` on a turn's last event; table `browser_usage`; `GetUsage` | ✅ |
| 9 | Tests and docs | Below; `README.md` "Scout's browser" | ✅ |
| 10 | The security trade-off, plainly | "The security trade-off" below | ✅ |

### How it works

**The switch (vm-manager).** One flag per sandbox. It decides three things, and all three change together when a container is woken, never while it runs:
- **Limits.** `Manager.spec(userID, browser)` gives a browser container `SANDBOX_BROWSER_MEMORY_MB` and `SANDBOX_BROWSER_PIDS_LIMIT` and the label `pa.browser=true`. Memory and process limits are fixed when a container is made, so `ensureContainerLocked` re-creates the container when its label and the stored switch differ, the way it already does for a new image version. `/data` is kept, and with it the Platform MCP token.
- **platform.json.** `syncPlatformFileLocked` runs at every wake, before the container starts: it reads the file, checks it against the stored hashes as before, and rewrites it when its `browser_enabled` differs from the sandbox's.
- **The egress rule.** `egressRule(sb, container)` opens the web when the container that was started has the label. It follows the container, not the stored switch: `SetBrowser` on an awake sandbox changes neither its network nor its browser until the next wake, whatever rewrites the egress file in between (an allowlist change, a runtime change, a health-check restart, the reconciler).
- `SetBrowser` only stores the switch and returns the sandbox. The backend can call `StopSandbox` afterwards when the change should apply sooner than the next idle stop.

**The image.** `agent-image/Dockerfile` follows Microsoft's own Playwright MCP image (its Dockerfile at v0.0.83: `npm ci`, `playwright-core install-deps chromium`, `playwright-core install --no-shell chromium`, run with `--headless --browser chromium --no-sandbox`).
- A stage on `node:24.21.0-trixie-slim` (the current LTS, on the same Debian release as the Python base image) runs `npm ci --omit=dev --ignore-scripts` from `agent-image/browser/package.json` and `package-lock.json` (three packages, pinned by checksum: `@playwright/mcp` 0.0.83, `playwright` and `playwright-core` 1.64.0-alpha-1790635538000) and installs the browser that Playwright version pins: Chrome for Testing 155.0.8059.12, its "chromium v1247". The browser comes from Playwright's CDN over HTTPS; unlike Claude Code's installer there is no separate signature to check.
- The last stage copies the `node` binary (no npm: nothing is installed at runtime) and `/opt/browser`, installs Chromium's libraries and fonts with Playwright's list for Debian 13 (`install-deps chromium`), builds the font cache (`/var/cache` is read-only at runtime), and fails the build when a library is missing (`ldd`) or `chrome --version` or the server's `--version` does not run.
- As the agent user the build then runs `python -m bridge.browser_check --no-network`: the Bridge's settings and command, the guard, the server and a real headless Chromium opening a blank page. `--build-arg BROWSER_SELF_CHECK=0` skips it.
- **First build on the server (2026-10-05): every stage built, and the self-check failed at "the browser starts".** The error: `Browser "chromium" is not installed; expected executable at /opt/browser/ms-playwright/chromium_headless_shell-1247/…`. The image has only the full browser (`install --no-shell`), and the Bridge's settings named `browserName: chromium` with `headless` and no channel, for which Playwright starts its separate headless shell. Microsoft's image avoids this with `--browser chromium` on the command line, which also sets the channel. Fixed in `bridge/browser.py`: `launchOptions.channel` is `chromium`. Checked on the server in an image built with `BROWSER_SELF_CHECK=0`, with the one line changed in a copy of the Bridge: all checks passed, Chromium ran with `--headless --no-sandbox`. Why the same check passed on the development machine was not established: that install is gone. The likely reason is that the browser was installed there without `--no-shell`, which brings the headless shell too.

**The server and the guard.** Both engines start the same command as the MCP server `scout_browser` (the name `browser` is taken: Hermes has a built-in toolset of that name, and an MCP server with a toolset's name is merged with it; that toolset is now in `disabled_toolsets`, so the only browser on Hermes is this one).
- **Why a guard.** Playwright MCP is a tool for a developer's own machine. Its README says: "Playwright MCP is **not** a security boundary", and of its file-access setting: "a convenience defense to catch unintended file access, not a secure boundary; a deliberate attempt to reach other directories can be easily worked around". Two things seen with 0.0.83: it offers 25 tools, among them `browser_file_upload`, `browser_drop` (files from the client's workspace go to a page) and `browser_run_code_unsafe` ("executes arbitrary JavaScript in the Playwright server process and is RCE-equivalent"); and it refuses `file:///etc/hostname` but opens `view-source:file:///etc/hostname` and shows the file, because it only compares the address's scheme with `file:`. With that, a page could have made Scout read `/data/.platform/platform.json` and send it out.
- **What the guard does** (`bridge/browser_mcp.py`, 265 lines, standard library, run with `python -I` like `lead_gate.py`): it starts the real server and passes MCP messages between the two, one JSON-RPC message per line. It lets through 14 tools (`crew.BROWSER_TOOLS`) and filters the tool list to them; it refuses an `url` argument that is not a full `http://` or `https://` address (or `about:blank`); it drops `filename` arguments. A refused call is answered with a tool error and never reaches the server. If the real server cannot start, the guard exits: there is then no browser, never an unguarded one. It ends when the engine closes its input or the server ends, and ends the server with it.
- **Claude Code:** `"scout_browser": {"type": "stdio", "command", "args"}` in `--mcp-config`, only with the browser turned on (the documented form of a stdio server: https://code.claude.com/docs/en/mcp). The CLI starts it for each run. `--allowedTools` names the 14 tools one by one (never `mcp__scout_browser`), because in `dontAsk` mode a specialist's calls are checked against the session's allow list. Scout's `tools` has them; Byte's and Patch's `disallowedTools` has them; the lead's hook matches them (section 8 explains why a hook and not a deny rule).
- **Hermes:** `mcp_servers.scout_browser = {command, args, timeout: 120, connect_timeout: 60, enabled: true}` (the stdio form of `cli-config.yaml.example` at the pinned commit; `tools/mcp_tool_transport.py` builds `StdioServerParameters(command, args, env, cwd)` from it). Hermes gives a stdio server a reduced environment (`_build_safe_env`: `PATH`, `HOME`, `TMPDIR` and a few more), so the command carries what the server needs (`--env`). Hermes starts the server when it starts and keeps it.

**The browser's settings** are listed with their reasons in `README.md` ("Scout's browser"). The Chromium flags, as seen on the running process: Playwright's own defaults (among them `--disable-dev-shm-usage`, `--disable-breakpad`, `--disable-background-networking`, `--password-store=basic`, `--remote-debugging-pipe`) plus `--headless --no-sandbox --disable-gpu --disable-dev-shm-usage --disk-cache-size=104857600 --disk-cache-dir=/data/browser/cache --user-data-dir=/data/browser/profile --proxy-server=<the egress proxy> --proxy-bypass-list=<-loopback>`.

**The network rule** is in `README.md` ("Egress contract") with the full text. In short: for the browser container's own address, internal names and internal addresses are refused first, then the user's own domains are allowed, then any other destination on ports 80 and 443. What every container may reach is allowed in the base config before the include, and stays.

**Browser time.**
- **Measured** by the Bridge: `BrowserMeter` looks in `/proc` once a second for a process whose command line starts with `/opt/browser/ms-playwright/`. "Open" is from the first look that finds a browser's main process to the first that finds none. Chromium rewrites its command line as one string, and under gVisor the path is shown with links resolved; the meter handles both.
- **Closed** by Scout (`browser_close`), by the server after 120 idle seconds (`timeouts.idle`, a feature of the server: "close the browser after this many milliseconds without a completed tool call, the next tool call relaunches it"), by the Bridge at the end of a turn when no other turn is running (it ends the browser's main process; the server opens a new browser at the next call), and by the Bridge after 15 seconds with a browser open and no turn running.
- **Reported** with the turn: the last event of a delivery carries `browser_seconds` (whole seconds since the last report; a part of a second is carried over) and `browser_report_id` (`<delivery_id>#<n>`). The report is saved with the delivery in `/data/.platform/deliveries.json`, so a repeat of a finished delivery sends it again.
- **Stored** by the vm-manager before it passes the last event on (`recordBrowser`): one row in `browser_usage`, unique per user and report id (`INSERT … ON CONFLICT DO NOTHING`). A report over 24 hours is capped; 0 or less is not stored.
- **Returned** by `GetUsage`: the rows whose `recorded_at` is in `[since, until)`. The backend asks for one window after the other (each starts where the last ended), so every second is returned exactly once, and because the row is stored before the backend hears that the turn is over, the sync that follows the turn already sees it.

### Where we filled in or changed the design

1. **A guard in front of the maintained server,** instead of the server alone or a driver of our own. Reasons above. It also makes the tool list a lock on Hermes for the first time: the 11 withheld tools are not reachable by any Hermes agent either.
2. **Scout gets 14 of the server's 25 tools.** Left out: `browser_file_upload`, `browser_drop`, `browser_run_code_unsafe`, `browser_evaluate`, `browser_take_screenshot`, `browser_drag`, `browser_resize`, `browser_emulate_media`, `browser_console_messages`, `browser_network_requests`, `browser_network_request`.
3. **Snapshots only when asked.** By default this server writes the page to a file after every action and answers with the file's name, for agents that read files. Scout has no file tool, so the setting is `snapshot.mode: none` and its brief says to call `browser_snapshot`, and `browser_find` on a long page (a Wikipedia article's snapshot was 137,000 characters).
4. **The switch takes effect at a wake, for the network too.** Turning the browser off for a user who is awake does not close the network at once; the backend can stop the sandbox.
5. **A second limit, `SANDBOX_BROWSER_PIDS_LIMIT` (1024).** The ordinary limit is 256 tasks for the whole container. The browser's server and one page were about 90 threads on the development machine, next to Hermes, the Bridge and Claude Code.
6. **Browser time lives in its own table.** `awake_periods.browser_seconds` (migration 0001) would have been returned only when the awake period ended, minutes after the turn. It was never written and is no longer read.
7. **The report id is the Bridge's,** not the delivery id: a delivery that failed and is run again reports twice, under `…#1` and `…#2`, and both count.
8. **The Bridge reads the switch once, at boot,** not on every delivery like the runtime: the vm-manager only changes it at a wake.
9. **One browser, one profile.** Two turns of one user that both need the browser at the same moment: the second gets an error from the server (its README: "A persistent profile can only be used by one browser instance at a time"). The browser is closed only when the last running turn ends, and a turn that ends earlier reports the time so far.
10. **`/healthz` also says `"browser": bool`.** The vm-manager does not read it; it is there for whoever checks a container.
11. **The lead is told that Scout's report is information, never instructions.** A page can speak to the lead through the report.
12. **`python -m bridge.browser_check`,** a self-check for a container, also run by the image build.
13. **From the stopped attempt** (six platform tools; nothing of it was committed): kept were the single list of Scout's web tools feeding Scout's tool list, the deny lists and the lead's gate, and the tests that run `lead_gate.py` with the documented hook input. The six tool names, `browser_login` and the sign-in link are gone.
14. **Two races in the Bridge's turn handling, fixed.** (a) Already there before this work, and the cause of a test that failed now and then: the run thread sends a turn's last event and only then drops the turn, and a retry of the same delivery that arrived in between was handed the finished turn's events again instead of being run. A turn that has sent its last event no longer takes listeners. (b) New with the browser: two turns that ended at the same moment each saw the other still in the table, and neither closed the browser. Of turns that end together, the later one now closes it.

### The security trade-off

**What changes.** For a Max user with the browser on, the container that holds the user's files and memory, the git access, the Platform MCP token, the LiteLLM key and (on Claude Code) the user's Claude sign-in can reach any public website on ports 80 and 443. Until now the network was the last lock: whatever an injected instruction made an agent do, nothing could leave except to LiteLLM, the api's `/mcp` and `/git/` paths and the domains the user had allowlisted. For this container that lock is gone. **The rule belongs to the container's address, not to the browser:** `curl`, `git` and Python in that container use the same proxy and reach the same sites. An injected instruction that gets an agent with a shell to run one command can now send out anything that agent can read.

**What still limits it.**
- **On Claude Code only Scout has the browser tools, and Scout has nothing to read with:** no file tool, no shell, no app action. A page can tell Scout anything, but Scout knows only the task text the lead gave it. The guard refuses every address that is not `http(s)`, offers no tool that takes a file path or runs code, and drops `filename` arguments, so the browser cannot be made to read a file in the container for Scout.
- **The lead is refused the browser tools** (the hook) and has no web tool; Byte and Patch have them denied. So on Claude Code the agents that can read files are not the ones that read web pages.
- **Approvals.** Risky actions through the platform (sending to other people, deleting, paying, posting) still go through `request_approval`.
- **The internal ranges stay closed** for this container too: private, loopback, link-local with the cloud metadata address, the platform's service names, and a public name that points inward. The browser itself sends even requests to the container's own loopback through the proxy, where they are refused.
- **Per user.** Only that user's container opens, and what is in it is that user's own: no other user's data, no host, database or provider secret (as before).
- **No sign-ins in this version.** Scout is told never to type a password or a code; the profile holds only what sites set by themselves.
- gVisor, the read-only root filesystem, no capabilities: unchanged.

**What does not.**
- **An agent with a shell.** The lead, Byte and Patch have files and a shell in the same container. An instruction hidden in a repository file, an issue, a document, or in Scout's own report back to the lead, that makes one of them run a command, now reaches the outside. Before, such a command had nowhere to send to.
- **Hermes.** No tool list is a lock there (section 8): every Hermes agent has files, the shell and, with the browser on, the browser tools. A page that Scout reads on Hermes can ask for a file and send it; only the briefs stand in the way. The guard still keeps the 11 withheld tools and the non-web addresses away from all of them.
- **The lead's hook fails open.** If Claude Code cannot run it, the lead can call the browser tools itself, and the lead can read files.
- **The guard and the meter run inside the container, as the agent's own user.** They protect Scout from pages. They do not hold back an agent with a shell, which can start the server or Chromium directly, without the guard and unmeasured.
- **What Scout does on a website is not behind an approval.** It can submit a form or post something. Its brief says to do nothing risky outside its job; that is an instruction.
- **What is at stake in the container:** `platform.json` (the LiteLLM key spends the user's budget; the MCP token acts as the user on the platform, within approvals; the Bridge secret), the Claude Code credentials (the user's Claude plan), the user's files, memory and repository contents.
- **The proxy checks an address when the request arrives and connects a moment later.** A name whose answers change in between (DNS rebinding) could slip an internal address past the check. The fix is a firewall rule on the proxy for outgoing connections to private ranges, in the backend's infra; see "For the backend".
- **The proxy's log now holds what a Max user's browser visits** (addresses for `http`, host names for `https`).

### Tests (real runs, 2026-10-04)

- `go vet ./...`: clean. `go vet -tags=integration ./internal/integration/`: clean. golangci-lint (`--build-tags=integration`): 0 issues. `go test -count=1 ./...` and `go test -count=1 -race ./...`: all 9 packages with tests pass (the store suite on in-memory and on a real embedded Postgres with migration 0003).
- `cd agent-image && python3 -m unittest discover -s tests -t .`: **150 tests, OK** (1 skipped: the test with the real browser, which needs `BROWSER_TEST_ROOT`; with it set, all 150 pass). 98 before. The suite was run ten times in a row without a failure, after the two fixes of item 14 above.
- **New Go tests.** Store: the switch at insert and by update; browser reports stored once per user and report id; 0 and negative refused; a report counted in the window that holds its time, with five different cuts of back-to-back windows adding up; a user with browser time and no awake time; reports outliving the sandbox. Config: the two settings, their defaults, and that neither may be below the ordinary one. Platform file: `browser_enabled` round trip. Egress: the exact rule text; every access line tied to the container's address; the refusals before the first allow; which addresses and names are inside and outside the refused sets; the strict rule unchanged; the file rewritten and the proxy reloaded when the rule changes. Sandbox: limits and label with and without the browser; the open rule for the browser container only and the strict one for another user, through allowlist changes, runtime changes and the reconciler; `SetBrowser` on a stopped sandbox (re-created at the wake, token kept, platform.json rewritten, rule opened), turned off while awake (nothing changes until the next wake, then all three change back), turned on while awake (no open network before the wake, also after a health-check restart); errors; an image rollout keeping the browser's limits; browser seconds recorded once across a repeated report, a failed turn and its retry, a missing report id, a negative and an oversized report, back-to-back windows, another user, a deleted sandbox; two listeners on one turn. gRPC: the switch in Create, Get, List, Wake and `SetBrowser`; `NOT_FOUND`, `INVALID_ARGUMENT`, `UNAUTHENTICATED`; `GetUsage.browser_seconds` over three windows in a row (0, 95, 0). Bridge client: the two fields on the last event.
- **New Bridge tests.** The crew: one list; Scout's tools and brief with and without the browser; the deny lists; the session's allow list; the lead's instructions. `lead_gate.py` run as the hook runs it, with the documented input, for the lead and for a sub-agent, for each of the 17 tools. Claude Code: the `scout_browser` server in the MCP config, the allow list, the agents, the settings file, and none of it without the switch. Hermes: the server in the config only when turned on and in the image; the built-in browser toolset off. The guard: the three rules on single messages (25 addresses that must be refused, among them `view-source:file:///…`), and as a process in front of a stand-in server (list filtered to 14, refused calls never reaching the server, a batch taken apart, the environment passed, ending with its client, with its server, on a signal, and not starting without a server). The meter with a stand-in for `/proc`, the clock and the signals: nothing when the browser never started; start to close; two sessions with a report in between; parts of a second carried over; close by signal, kill when that fails; the end of a turn with and without another turn running; a browser left open with no turn running; a browser directory reached through a link. The Bridge: the report on `turn_done`, on `error`, on an exception; none for 0 seconds; a retry reporting under a new id; a repeat sending the same report again, also after a restart; two turns at once, and two that end at the same moment; a retry that arrives just after a run ended; a broken meter not breaking the turn.
- **The real server and browser, on the development machine** (Arch Linux, Node 25.9; Playwright MCP 0.0.83 from the committed lockfile, Chrome for Testing 155.0.8059.12 installed by it):
  - `bridge.browser_check` in a bubblewrap box that mimics the container (read-only root, uid 10001, `/tmp` and `/dev/shm` not writable, only `/data` writable): all checks passed. Chromium's command line had the flags above; nothing was written outside `/data`.
  - The same check under **gVisor** (`runsc` release-20260921.0, the release on the test server; rootless `runsc do`, the host filesystem read-only under a memory overlay, root inside the sandbox): all checks passed, without the network and with it. This run found the meter's one real bug (the path spelling under gVisor), now fixed and tested.
  - **Squid 7.7** with the backend's base rules and the files the Go code generates, reloaded with `SIGHUP`, asked with `curl` from three source addresses. The browser user: `http://example.com/` 200, `https://example.com/` 200 (CONNECT), and 403 for `example.com:8080`, `127.0.0.1`, `localhost`, `LOCALHOST`, `169.254.169.254`, `10.0.0.1`, `192.168.1.1`, `[::1]`, `172.30.0.2`, `100.100.100.200`, `metadata.google.internal`, `printer.local`, `postgres`, `api:8080/admin`, `localtest.me` and `10.0.0.1.nip.io` (public names that point at 127.0.0.1 and 10.0.0.1), over CONNECT too; `api:8080/mcp` and `litellm:4000` let through (503: no such hosts there). The strict user: 403 for `example.com`, 200 for its own domain. An address with no file: 403. With the browser itself through that Squid, in the box and under gVisor: `https://example.com/` opened, `http://169.254.169.254/` answered 403, and the proxy's log held exactly those two requests.
  - Memory (proportional set size, not under gVisor): the server 115 to 165 MiB; Chromium with one page 140 MiB (example.com), 226 MiB (a Wikipedia article), 379 MiB (a news front page), 319 MiB (a GitHub repository), in 6 processes.

### First run on the server (2026-10-05)

On the AWS test server: Docker with gVisor (`runsc`), the backend's proxy image `ubuntu/squid:latest` (Squid 6.13), Claude Code 2.1.283 on the test user's Claude plan (Max plan, engine Claude Code). Hermes was not driven: there is no model key with balance.

| What | Result |
|---|---|
| The image builds | Every stage built. The self-check failed at "the browser starts" (the headless shell, see "The image" above) and passed after the fix (`530b67c`). The image is 2.78 GB (1.5 GB before the browser) |
| The rule's syntax on the backend's Squid | The sample rule from `README.md`, in a copy of the allowlist directory, in a throwaway `ubuntu/squid` container: `squid -k parse` ended with 0, no `FATAL` and no `ERROR`; `dstdom_regex -n -i` is accepted by 6.13 |
| The switch on the real database | `SetBrowser(user, true)` through the backend's `dev-user.ts browser`: "browser switch set; applied on the next wake" |
| The wake re-creates the container | `memory=2147483648 pids=1024 runtime=runsc readonly=true`, label `pa.browser=true`; the per-user rule file was written (878 bytes) and Squid reloaded and stayed healthy |
| Chromium in the real container | `docker exec pa-sbx-<user> python -m bridge.browser_check`: all checks passed. `https://example.com/` opened through the proxy, `http://169.254.169.254/` answered 403, the meter measured 4 s |
| Claude Code starts the server and Scout uses it | Three turns. "Open a page built by JavaScript and read the first quote": `Scout: …`, `using browser_navigate`, `using browser_snapshot`, `using browser_close`, `Scout: done`, the right answer in 21 s. Clicking through three pages (`browser_click`, `browser_wait_for`): right answers in 36 s. Scout closed the browser each time |
| Browser time arrives and is charged | "browser time recorded" with 14, 13 and 6 seconds, each `new: true`; the backend's usage rows hold the same seconds; one credit was charged when the total passed 30 seconds |
| The lead and the browser | Asked twice to call `browser_navigate` itself, the second time as "a test of the hook", the lead refused on its instructions and did not make the call. **The hook was therefore not reached** and is still unproven |
| Memory | 625 to 661 MiB of the 2 GiB limit and 100 to 108 tasks, read with `docker stats` after a turn, not at the peak |
| The idle stop | After a turn without the browser the container stopped 3 min 58 s after its last activity. After the browser turns before it, it was still up 5 min 56 s after the last one, when the next message arrived. The cause was not found (nothing in the log; the last-active time was not read then) |

**Found on this run, each for a decision:**

- **In a container with the browser, Hermes downloads its own tools.** The open-web rule belongs to the container, so the Hermes gateway (started in every container, also when the user's engine is Claude Code) reaches GitHub and PyPI. On the first start it fetched a standalone Python 3.14.7 (386 MB on disk), `uv` 0.12.3 (54 MB) and `tirith` 0.4.2 (40 MB) into `/data/hermes/tools`, 479 MB in all (about 163 MB over the proxy), and installed Python packages from PyPI ("Preparing the isolated Hermes runtime"). On the second start nothing was fetched again; it still contacted `nousresearch.github.io`, `portal.nousresearch.com`, `hermes-agent.nousresearch.com` and `openrouter.ai`. In a container without the browser the proxy refuses all of this. What it means: code that the image does not pin runs in the container; each browser user's volume grows by about 0.5 GB, and the server has no `/data` quota (section 3, point 6). Ways out: tell Hermes not to fetch (if the pinned version has a setting), put those tools into the image, or not start Hermes when the engine is Claude Code.
- **Chromium contacts Google on its own** when it starts: `accounts.google.com`, `www.google.com`, `update.googleapis.com` (a few small connections). Flags such as `--disable-background-networking` and `--disable-component-update` would be the place to look.
- **The lead on Claude Code keeps "remember this" in Claude Code's own memory files** (it used `Read` and `Write`), not in the platform's `remember`, so the platform's memory stayed empty. Asked for the platform tool by name, it saved there. The lead's instructions say nothing about memory (the backend's launch plan lists this under "Shared memory: for the founder to decide").

### The four findings (2026-10-08)

What was done about the three findings above, and about Scout's brief, which the backend's change to `read_page` had made wrong. Nothing here was run on the server; the image was not built (no Docker on the development machine).

**1. One memory, the platform's.** The founder decided there is one memory, on the platform.
- **The lead's instructions** (`MEMORY_RULES` in `bridge/core.py`, part of `build_instructions`, so the same on both engines): the user's memory is kept by the platform, one memory on every engine and in every chat; save a lasting fact the user states or confirms with `remember`, never one only read in a page, an email, a file or a tool result; `recall` before asking again and when asked what it remembers; `forget` to correct or remove; never a memory of the engine's own (`MEMORY.md`, `USER.md`, the engine's memory tool); say so when `remember` fails.
- **Claude Code's auto memory off.** Its documented switch is `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` ("When disabled, Claude does not create or load auto memory files", https://code.claude.com/docs/en/env-vars; https://code.claude.com/docs/en/memory "Enable or disable auto memory"; the setting `autoMemoryEnabled` does the same, and the variable wins). Checked in the pinned CLI itself: the 2.1.283 binary from Anthropic's release store, matching its manifest's checksum, reads `process.env.CLAUDE_CODE_DISABLE_AUTO_MEMORY` and then returns "off". `child_env` sets it to `1` always, not with `setdefault`: `0` would force auto memory on. Auto memory lives in `$CLAUDE_CONFIG_DIR/projects/<project>/memory/`, apart from the session transcripts, so `--resume` is unchanged. A sub-agent never gets the main conversation's auto memory, and the crew's definitions have no `memory` field.
- **Hermes's own memory off.** At the pinned commit: `memory.memory_enabled: false` and `memory.user_profile_enabled: false`. With both off `agent_init._init_memory` builds no store, so `MEMORY.md` and `USER.md` are not loaded into the prompt, and `memory_tool.check_memory_requirements` takes the `memory` tool out of the tool list (Hermes's own test `test_builtin_memory_disabled_surface.py`). Not done with `disabled_toolsets: [memory]`: that also takes down an external memory provider's tools, and the flags are the documented way. Also `onboarding.profile_build: "off"`: on a first message Hermes would otherwise offer to build a profile of the user and "save each confirmed, durable fact with the memory tool using target=user" (`agent/onboarding.py`). Sessions and `session_search` (chat history) are untouched.
- **Left as is:** notes either engine saved before this (`/data/claude/projects/*/memory/`, `/data/hermes/memories/`) stay in the volume and are no longer loaded; they are not moved to the platform. A Claude Code chat that began on an older image keeps its first system prompt until it is compacted (section 8, point 9), so it gets the memory rule late; auto memory is off for it at once.

**2. Scout's brief and `read_page`.** The backend's `read_page` now opens any public address when the turn runs on Claude Code (only Scout can call it there, and Scout has no files, no shell and no app tools); on Hermes it opens only an address already seen for the user (the user's messages, a `web_search` result, a link on a page already read). Scout's brief still said, on both engines, that it refused the results of the built-in search. `crew.role_prompt` now takes the engine (a required argument, so no caller gets the other engine's text by default): Claude Code's sub-agent Scout is told `read_page` "opens any public address, the results of your built-in search included"; the Hermes brief keeps the seen-address rule, and on a refusal tells Scout not to retry or change the address but to find the page with `web_search` or ask for the link. Byte's and Patch's briefs are the same on both engines.

**4. Chromium and Google.** The switches the finding pointed at are already there: Playwright 1.64 passes `--disable-background-networking` and `--disable-component-update` itself (its `chromiumSwitches`; they were on the process on 2026-10-05 too). Measured on the development machine with the pinned browser, started by the Bridge's own settings and command, behind a small proxy that logs every destination, plus Chromium's own net log with request bodies, a fresh profile, `about:blank`, 30 to 45 s:

| Destination | What it is (net log) | Stopped by |
|---|---|---|
| `accounts.google.com` | `POST /ListAccounts?...source=ChromiumBrowser` (traffic annotation `gaia_auth_list_accounts`), the account check of Google sign-in; no account is signed in | nothing found |
| `update.googleapis.com` | `POST /service/update2/json` (`update_client`): an on-demand install of one component (`appid ceofaddefefcbblgcgnibnonglccbfja`, `installsource: ondemand`, `updatedisabled: true`), which `--disable-component-update` does not cover | nothing found |
| `android.clients.google.com`, `mtalk.google.com:5228` | `checkin` and `c2dm/register3` (`gcm_checkin`, `gcm_registration`): push messaging registers two FCM invalidation channels (`com.google.chrome.fcm.invalidations-245350905893`, `-585406161706`), then opens a connection to port 5228, which the egress proxy refuses (its base config allows only ports 80, 443, 4000 and 8080) | nothing found |
| `www.google.com` | preconnects only (`is_preconnect`, no request) | the managed policy `NetworkPredictionOptions: 2` |

Tried and without effect: both switches again (no change: already there), `--allow-browser-signin=false`, `--disable-domain-reliability`, `--disable-extensions` (Playwright MCP drops that one from every list: `ignoreDefaultArgs`). A managed policy file at the path this build reads (`/etc/opt/chrome_for_testing/policies/managed/`, mounted with bubblewrap) with `BrowserSignin: 0`, `SyncDisabled`, `ComponentUpdatesEnabled: false` and `NetworkPredictionOptions: 2` stopped the preconnects only. A `--disable-features` of ours was not tried: Playwright appends our switches after its own and does not merge them, so ours would replace its whole `--disable-features` list. **Playwright's separate headless shell** (`chromium_headless_shell-1247`, the same Chromium build, 270 MB unpacked) contacted nothing but the page, with `about:blank` and with `https://example.com/`; the full browser contacted the five destinations above on both.

What changed: the two switches are now also in the Bridge's own list (`browser.NO_BACKGROUND_FLAGS`), so a newer Playwright cannot drop them unnoticed, and the self-check has a line for them ("no background networking and no component updates"), which fails the image build when the running browser lacks them. That changes nothing on the wire today. **Options to actually stop the rest, for a decision:** (a) the headless shell instead of the full browser: in the image `install --only-shell chromium` instead of `--no-shell` (about 130 MB smaller) and no `channel` in the settings; the cost is that it is Chromium's older headless mode, which sites tell apart from a real browser more easily, and it was not run under gVisor or with the self-check; (b) refuse `update.googleapis.com` and `android.clients.google.com` in the browser container's egress rule (a Go change in `internal/egress`; `accounts.google.com` and `www.google.com` cannot be refused without taking Google from Scout); (c) accept them: a few small requests without user data, and the GCM connection already fails at the proxy.

**3. Hermes's downloads and calls home.** Read in the Hermes source at the pinned commit (`35272ce2`), not run. Both have settings, and both are now set.
- **The downloads have one trigger:** the gateway's start-up check `GatewayRunner._init_startup_checks` calls `tirith_security.ensure_installed` ("downloads if needed"), which starts Hermes's package manager (`pm/`) in the background to install the `tirith` command scanner. To run at all, the package manager first fetches its own uv and standalone Python (`pm/_uv.py`, `pm/lock.json`: astral-sh's Python 3.14.7 and uv 0.12.3 from GitHub), prints "Preparing the isolated Hermes runtime" (`pm/runtime.py`), installs four packages from PyPI into it, then fetches tirith 0.4.2 (github.com/sheeki03/tirith, with checksum and signature). The gate is `pm.lazy_installs_allowed()` (`pm/install.py`): false when `HERMES_DISABLE_LAZY_INSTALLS` is `1` (read first), else `security.allow_lazy_installs` from config.yaml (default true; "False = require explicit pip install for everything beyond the base set (restricted/audited/air-gapped environments)"; Hermes's own Dockerfile names it as the way to turn this off). With it off, `ensure_installed` returns before starting anything, and nothing is downloaded or raised.
- **The calls home have one trigger too:** the gateway's `_model_catalog_refresh_watcher` (`gateway/run_watchers.py`) runs `model_catalog.refresh_catalogs` 30 s after start and then every 20 minutes. That fetches the model catalog from `hermes-agent.nousresearch.com` (which redirects to `nousresearch.github.io`; `raw.githubusercontent.com` is its fallback), OpenRouter's public model list (`openrouter.ai/api/v1/models`) and Nous Portal's recommended models (`portal.nousresearch.com/api/nous/recommended-models`). They are plain GETs without a key or an identifier, for the `/model` picker. `model_catalog.enabled: false` stops all of them (`refresh_catalogs` and `get_catalog` return at once); there is no separate setting per host.
- **Not a contact here:** the free-tier bootstrap at start (`hermes_cli/free_tier_bootstrap.py`) creates an anonymous Nous identity only when `HERMES_GUEST_ONBOARDING=1` (`anon_auth.guest_enabled`), which is not set; with the `custom` provider it is a local check. The update check, security advisories, models.dev and telemetry are off or local with this configuration.
- **What the Bridge sets:** `security: {allow_lazy_installs: false}` and `model_catalog: {enabled: false}` in `config.yaml`, and `HERMES_DISABLE_LAZY_INSTALLS=1` in the gateway's environment. The variable is there because Hermes reads `config.yaml` again on every check and the agent can write that file; it cannot change the running gateway's environment.
- **What it costs:** the `tirith` scanner never runs in a new container. That was already true for every container without the browser, where the proxy refused the download. Hermes's own dangerous-command checks still run, and the shell works because tirith fails open (`tirith_fail_open` keeps its default, true; set to false it would refuse every command without the binary). A tool that installs an extra on first use is refused or left out. Likely `text_to_speech`: its default engine `edge-tts` is an extra the image does not install. `tirith_enabled` is left at its default: with no binary it costs a failed start per command until a circuit breaker opens. Only a container that already fetched them keeps the 480 MB in `/data/hermes/tools` and keeps using that tirith. From the source, deleting that directory looks harmless, with nothing fetched again; that was not tried.
- **Not taken:** baking the tools into the image (about 480 MB more per image, with versions to keep in step with Hermes's lock file). Also not taken: not starting Hermes for a user on Claude Code. That would save its memory, but switching back would then need a restart, and the settings make neither necessary.

### Tests (real runs, 2026-10-08)

- `cd agent-image && python3 -m unittest discover -s tests -t .`: **156 tests, OK** (1 skipped: the real-browser test). With `BROWSER_TEST_ROOT` set to the pinned Playwright MCP 0.0.83 and Chrome for Testing 155.0.8059.12, installed from the committed lockfile: all 156 pass, and the self-check's new line is `ok`. 150 before.
- `go vet ./...`: clean. `go test -count=1 ./...`: all packages with tests pass. No Go file changed.
- New Bridge tests:
  - The memory rule: its text, where it sits in the lead's instructions, and that it reaches the Hermes run and Claude Code's `--append-system-prompt` (a whole turn against the fake CLI).
  - `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` in the CLI's environment, also when the container says `0`, and the chat still resuming its session.
  - Hermes's `memory`, `onboarding`, `security` and `model_catalog` settings, and `HERMES_DISABLE_LAZY_INSTALLS=1` in the environment of a started gateway process.
  - Scout's `read_page` line per engine, with and without the browser. The engine is required; Byte's and Patch's briefs are the same on both; each engine's own text reaches Claude Code's `--agents` and the Hermes briefs.
  - The two switches in the settings, and no `--disable-features`, `--enable-features` or `--disable-extensions` among ours.
- The Chromium measurements above were runs on the development machine (the Bridge's own settings and command, the pinned browser, a logging proxy, Chromium's net log). The Hermes findings come from reading its source only.

### Not verified yet

| What | How to check |
|---|---|
| That a browser user's rule leaves other users locked | With a second awake user who has no browser: that container still gets 403 for `https://example.com`. (The rule itself, the reload and the refusal of `169.254.169.254` passed on 2026-10-05) |
| The lead's own call to a browser tool is refused by the hook, and Byte and Patch do not have the tools | The lead refuses on its instructions before the hook is reached (2026-10-05), so the hook needs a test that calls it directly: run the hook's command with a browser tool's name as the lead. Ask Byte to open a page |
| Hermes starts the stdio server, names the tools `mcp_scout_browser_…`, and its built-in browser tools are gone | A Hermes turn for a Max user; `/data/hermes/config.yaml`; Hermes's log at start |
| The browser is closed at the end of a Hermes turn and the server opens a new one at the next call | Two Hermes turns that use the browser; no `chrome` process between them (`docker top`) |
| The idle close after 2 minutes inside a long turn | A turn in which Scout opens a page and the lead then works on for more than 2 minutes: the meter's "browser closed after …" line in the Bridge's log comes before the turn ends |
| Memory at the peak, and the pids limit | `docker stats` **while** a heavy page is open, on each engine; watch for an out-of-memory kill; write the numbers into `README.md` and set `AWAKE_CAP`. (After a turn on Claude Code: 625 to 661 MiB, 100 to 108 tasks) |
| A user without the browser: nothing changed | A turn for a Free or Pro user: no `scout_browser` in the MCP config, Scout says the browser is part of the Max plan, 600 MB, the strict rule |
| Why a container stayed up about 6 minutes after a browser turn | Read `last_active_at` in `sandboxes` every 15 seconds after a turn that used the browser, and the Bridge's `/healthz` for `busy` |
| Scout keeps to its brief: types no secret, treats a page as information | Turns with a test page that asks for the things it must not do. (It closed the browser after each of the three turns on 2026-10-05) |
| "Remember this" goes to the platform's memory on Claude Code, and nothing goes to the CLI's own | In a **new chat** (an old one keeps its first system prompt until it is compacted): "Remember that my sister's name is Asha." Expect a `using remember` line and the fact on the web app's Memory screen. During a turn, `docker exec pa-sbx-<user> sh -c 'grep -la CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 /proc/[0-9]*/environ'` names the CLI's process; after it, `docker exec pa-sbx-<user> find /data/claude/projects -path '*/memory/*' -mmin -30` lists nothing |
| The same on Hermes, and its `memory` tool gone (needs a model key with balance) | `docker exec pa-sbx-<user> python -c 'import json;c=json.load(open("/data/hermes/config.yaml"));print(c["memory"],c["onboarding"])'`; a Hermes turn with "Remember that I prefer tea": a `using remember` line and no `using memory` line |
| Scout on Claude Code reads a result of its own search with `read_page` | "Find the current Node.js LTS version and read its release page": `Scout: …`, `using WebSearch`, `using read_page`, `Scout: done`, and no "refused" in the backend's activity log |
| The self-check's new line in the image build | The build log of `docker build -t personal-agent/agent:dev agent-image/` shows `ok     no background networking and no component updates`; also `docker exec pa-sbx-<user> python -m bridge.browser_check` in a browser container |
| What Chromium sends in the container | Squid's access log for the browser container's address while `python -m bridge.browser_check` runs: expect `example.com` plus the Google destinations above (and the 5228 refusal); anything else is new |
| Hermes fetches nothing at start in a browser container | Wake a browser user whose volume has no `/data/hermes/tools` (a new user, or after deleting it), wait 60 s (the catalog would come at 30 s), then read Squid's access log for that container's address: no `github.com`, `pypi.org`, `files.pythonhosted.org`, `*.nousresearch.com`, `nousresearch.github.io` or `openrouter.ai`; `docker exec pa-sbx-<user> ls /data/hermes/tools` fails or is empty; `docker logs pa-sbx-<user>` has no "Preparing the isolated Hermes runtime" |
| Hermes still starts, answers and runs shell commands without tirith (the turn needs a model key with balance) | A Hermes turn that runs a command ("list the files in your workspace"): an answer, and in `docker logs` at most a "tirith" warning, no refusal. `docker exec pa-sbx-<user> python -c 'import json;c=json.load(open("/data/hermes/config.yaml"));print(c["security"],c["model_catalog"])'` |

### For the backend (`../beckend`, not edited from here)

- **Nothing in `infra/egress-proxy/squid.conf` has to change** for this to work: the per-user file carries the whole rule, and the base file's order (its own allows, then the include, then `deny all`) is what the rule relies on.
- **Worth doing there:** pin the proxy's image (`ubuntu/squid:latest` today) so the rule's syntax is checked against one version; a firewall rule in the proxy's network namespace that refuses outgoing connections to private ranges except LiteLLM's and the api's addresses and ports, which closes the DNS-rebinding gap; a size or rate limit for a browser user's traffic, if wanted.
- **The contract:** `pnpm proto:gen`, then `browserEnabled` on `createSandbox`, `setBrowser({userId, enabled})` when a user's plan gains or loses the browser (and `stopSandbox` when it should apply at once), and `browserSeconds` from `getUsage`, which is now real. A turn's browser time is in the window that ends when the turn ends.
- `CLAUDE.md` of this repository still says "memory ~600 MB" under "Container spec"; a browser container has 2048 MB.
