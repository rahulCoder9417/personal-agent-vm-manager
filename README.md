# personal-agent vm-manager

The only service that controls Docker. It creates, wakes, stops and deletes each
user's sandboxed agent container (Hermes Agent + Claude Code + Bridge), delivers messages
into it, records awake time for billing, and writes each container's egress allowlist.

Two agent runtimes, chosen per user (`SetAgentRuntime`):
- **hermes** (default): Hermes Agent on the platform's models through LiteLLM.
- **claude_code**: the official, unmodified Claude Code CLI on the **user's own Claude plan**
  (they sign in with Anthropic; usage bills their plan). See "Claude Code runtime" below.
The backend calls it over gRPC (`proto/vm/v1/vm.proto`, the source of truth).

Design: `../beckend/docs/mvp-hld.md` sections 0.5, 0.6, 1.2, 4, 6, 8, 9 (Phase 2).
What was built vs the design: `docs/build-report.md`.

## Layout

```
proto/vm/v1/vm.proto     gRPC contract            gen/vm/v1/        generated Go (committed)
cmd/vm-manager/          main                     migrations/       SQL, embedded, run at startup
internal/config          env config               internal/store    sandboxes + awake_periods (pgx; in-memory for tests)
internal/docker          Engine interface, SDK client, fake
internal/sandbox         state machine, capacity guard, Deliver, loops
internal/grpcserver      the VmManager service + auth
internal/bridge          HTTP client for the Bridge
internal/egress          Squid allowlist files + proxy reload
internal/platformfile    /data/.platform/platform.json (the container's secrets)
internal/integration     Docker/gVisor tests (build tag `integration`)
agent-image/             Dockerfile + Bridge (Python) + its tests
```

## Commands

```bash
go run github.com/bufbuild/buf/cmd/buf@latest generate   # regenerate gen/ (needs protoc-gen-go + protoc-gen-go-grpc on PATH)
go vet ./... && go test ./...                             # unit tests (fake Docker; store tests use embedded Postgres)
go test -race ./...
go run github.com/golangci/golangci-lint/v2/cmd/golangci-lint@latest run ./...
go test -tags=integration -v -timeout 30m ./internal/integration/   # needs Docker + runsc + the infra stack
docker build -t personal-agent/vm-manager:dev .
docker build -t personal-agent/agent:dev agent-image/
cd agent-image && python3 -m unittest discover -s tests -t .       # Bridge tests
```

Store tests download Postgres binaries once (embedded-postgres). Set
`VM_TEST_DATABASE_URL` to use your own Postgres, or `VM_TEST_SKIP_POSTGRES=1` to skip.

## Configuration

All from env vars, validated at startup (see `.env.example` for every variable and
default). Required: `DATABASE_URL`, `VM_INTERNAL_SECRET` (>= 32 chars). The ones the
backend's compose sets: `GRPC_ADDR` (:9090), `SANDBOX_NETWORK` (personal-agent_sandbox),
`EGRESS_PROXY_URL` (http://172.30.0.2:3128), `EGRESS_PROXY_CONTAINER` (egress-proxy),
`EGRESS_ALLOWLIST_DIR` (/allowlists), `LITELLM_BASE_URL` (http://litellm:4000),
`PLATFORM_MCP_URL` (http://api:8080/mcp). Claude Code: `CLAUDE_EGRESS_DOMAINS` (default
`api.anthropic.com,claude.ai,claude.com,platform.claude.com`), `CLAUDE_LOGIN_TTL` (10m). Tunables: `AWAKE_CAP`, `IDLE_TIMEOUT`,
`SANDBOX_MEMORY_MB`, `SANDBOX_CPUS`, `SANDBOX_PIDS_LIMIT`, `DELIVER_TIMEOUT`, `AGENT_IMAGE`,
`AGENT_IMAGE_VERSION`, ...

Deployment: mount the Docker socket (only here), the allowlist dir shared with the
egress proxy, and a backup dir at `SNAPSHOT_ROOT`. Put it on the platform and sandbox
networks; never publish port 9090.

## gRPC

Every call needs metadata `x-internal-secret: <VM_INTERNAL_SECRET>` (else
`UNAUTHENTICATED`). Errors: `NOT_FOUND` (no sandbox), `INVALID_ARGUMENT`,
`RESOURCE_EXHAUSTED` (awake cap reached, nothing evictable: retry later),
`FAILED_PRECONDITION` (polite stop during a turn; unknown, expired or already used Claude
sign-in), `UNAVAILABLE` (container would not start, marked broken; or the Bridge / Claude
Code could not be reached). User ids must match `[A-Za-z0-9][A-Za-z0-9_-]{0,127}`.

Notes on behaviour the proto leaves open:
- `Priority` unspecified is treated as LOW. Wake with a priority updates the stored one.
- `mcp_token_hash` is lowercase hex sha256 of the token string.
- `SetEgressAllowlist` replaces both lists. Domains apply at once if awake; MCP servers
  are written to /data and used on the next wake. MCP server hosts are not added to
  the egress allowlist automatically: send them in `domains`.
- `GetUsage`: awake seconds clipped to `[since, until)`; open periods count up to
  min(until, now); `until_unix_ms = 0` means now. `browser_seconds` is counted when a
  period ends; nothing records browser time yet, so it is 0 in the MVP.
- `SnapshotVolume`: `destination` is a directory inside `SNAPSHOT_ROOT` (absolute, or
  relative to it). Writes `<user_id>-<UTC time>.tar.gz` and returns its path and size.
  Streams /data through the Docker archive API (no helper container). Running
  containers are archived live (SQLite files may be mid-write; fine for nightly backups).
- `StopSandbox` (polite) on a busy sandbox is refused; use `force=true` for stuck turns.
- `Sandbox.runtime` / `claude_connected` are in every answer that returns a sandbox.
  `claude_connected` is the last status seen (health checks every 30 s, wakes, sign-in,
  sign-out); the Bridge gets it from `claude auth status`.
- `CreateSandbox.runtime` / `SetAgentRuntime`: `UNSPECIFIED` = hermes; unknown values are
  `INVALID_ARGUMENT`. `SetAgentRuntime` writes `runtime` into platform.json (the Bridge reads
  it on every delivery, so no restart) and updates the egress file at once if awake. It does
  not check `claude_connected`; a Claude turn without a sign-in ends with the error
  "Connect your Claude plan again".
- `StartClaudeLogin`: wakes the sandbox, starts `claude auth login` in it, returns
  `{login_id, authorize_url, expires_at_unix_ms}` (10 minutes). Starting again replaces
  the pending sign-in. Until it completes or expires, the sandbox counts as busy (idle
  reaper and capacity guard leave it alone) and can reach Anthropic's hosts.
- `CompleteClaudeLogin`: passes `code` to the waiting CLI (never logged: the gRPC log line
  says `code=[REDACTED]`), returns `{connected, message}`. One attempt per sign-in. The
  sandbox must still be awake (a stopped one lost the waiting CLI: `FAILED_PRECONDITION`).
- `ClaudeLogout`: wakes if needed and runs `claude auth logout`; `claude_connected=false`.
  It does not change the runtime (the backend switches back to Hermes).

## Lifecycle

States: `stopped -> waking -> awake <-> busy`, and `broken`.

- **Create**: volume `pa-data-<user>` + container `pa-sbx-<user>` (stopped) + secrets. Idempotent.
- **Wake**: capacity guard, re-create if the image version changed or the container is
  missing (keeps /data), start (retry once, then re-create), wait for `GET /healthz`,
  write the egress file, open an awake period.
- **Runtime**: stored in the table and in platform.json; survives re-creation (it is on /data).
- **Deliver**: wake if needed, `POST /deliver`, relay events; `busy` on `accepted`,
  back to `awake` + last-active on `turn_done` / `error`. Timeout (15 min or
  `timeout_seconds`) -> force stop + `ERROR` with `platform_fault=true`. The turn is
  tracked to the end even if the gRPC caller disconnects.
- **Stop**: polite = `POST /shutdown` (Hermes saves to /data, up to 20 s) then `docker
  stop`; force = kill. Removes the egress file, closes the awake period.
- **Delete**: container, volume, egress file, row. Awake periods are kept for billing.

Loops: idle reaper (1 min; not busy, nothing in flight, no pending Claude sign-in, Bridge
reports no turn and no sign-in, idle >= 3 min), health checker (30 s; 2 failed checks -> restart; more than 3 restarts without a
good check -> stop + `broken` + an `alert=true` log line), reconciler (startup + 5 min;
running/not-running drift, stale `waking`/`busy`, orphan and duplicate containers,
leftover egress files; never deletes volumes). Capacity guard: evict awake, not-busy
containers, lowest priority first, then longest idle.

## Container spec

gVisor (`runsc`), 600 MB (no swap), 1 CPU, pids limit 256, read-only rootfs, only the
`/data` named volume writable, `cap-drop ALL`, `no-new-privileges`, non-root uid 10001,
the sandbox network only, no published ports, `--init`, no restart policy. A disk
quota on /data needs a volume driver that supports it (`SANDBOX_VOLUME_DRIVER` /
`SANDBOX_VOLUME_OPTS`); the default `local` driver has none.

**Secrets.** Each container gets its own Platform MCP token and Bridge secret (32
random bytes, base64url) plus the user's LiteLLM key. The table stores only sha256
hashes. The plaintext lives only in the user's own volume at
`/data/.platform/platform.json` (0600, uid 10001), written with the Docker archive API.
Keeping it on the volume means a container re-created for an image rollout reuses the
same token, so the hash the backend stores never changes. The vm-manager reads the
Bridge secret back from there after its own restart (and checks it against the hash).
Env holds only non-secret settings (user id, URLs, proxy, paths). The agent can read
its own secrets file; that only affects its own sandbox.

## Bridge contract (vm-manager -> Bridge, sandbox network, port 8765)

Header `X-Bridge-Secret: <secret>` on every call except health.

| Call | Behaviour |
|---|---|
| `GET /healthz` | `200 {"ok":true,"busy":N,"runtime":"hermes\|claude_code","claude_connected":bool\|null,"claude_login_pending":bool}` when the current runtime answers (Hermes API / `claude` installed), else `503`. `busy` = turns in progress; `claude_connected` is `null` until `claude auth status` has answered once |
| `POST /deliver` | Body `{"delivery_id","kind","session_key","channel","text","attachments":[{"id","name","mime_type","url"}],"approval_id","approved","metadata":{}}`, `kind` = `message` / `approval_answer` / `automation` / `app_event`. `200` with `Content-Type: application/x-ndjson`, one line per event: `{"type":"accepted\|working\|turn_done\|error","message":"...","platform_fault":bool}`, ending with `turn_done` or `error`. A `working` message is `using <tool>`, or a crew line that starts with the member's name and a colon (`Scout: researching flight options`, `Scout: done`; see "The crew"). `401` bad secret, `400` bad body |
| `POST /shutdown` | Stop taking work, stop Hermes (it saves to /data), `200` when done (< 20 s) |
| `POST /claude/login/start` | Starts `claude auth login` (in a pty). `200 {"login_id","authorize_url","expires_at"}` (unix ms, +10 min). One pending sign-in per container; a new start replaces it. `502` no link shown, `503` Claude Code not installed |
| `POST /claude/login/complete` | Body `{"login_id","code"}`. Types the code into the waiting CLI, waits (<= 60 s) and asks `claude auth status`. `200 {"connected":bool,"message":"..."}`; `409` unknown / expired / used; `400` missing or multi-line code. Bodies are never logged |
| `POST /claude/logout` | Runs `claude auth logout`. `200 {"ok":true,"claude_connected":false}` |

Dedupe by `delivery_id`: a repeat of a finished delivery returns `accepted` +
`turn_done` without running again; a repeat while it runs attaches to the same turn; a
retry after a failed send only re-sends the saved answer. The record lives in
`/data/.platform/deliveries.json`, so it survives restarts. Failed runs are re-run on retry.

The Bridge never returns reply text. It sends the final answer itself by calling the
Platform MCP tool `send_message` `{"text","session_key"}` with the container's bearer
token. The agent (Hermes or Claude Code) sees the other Platform MCP tools but not `send_message`.

Deliver errors from the Claude Code runtime: not signed in / login expired / token revoked
-> `"Connect your Claude plan again"`, `platform_fault=false`; plan usage limit, rate limit or
spend limit -> `"Your Claude plan's usage limit was reached: <the CLI's message>"`,
`platform_fault=false`; step limit (`error_max_turns`) -> `platform_fault=false`; crash,
timeout, overload, anything else -> `platform_fault=true`.

## Agent image

`agent-image/`: Hermes Agent (Nous Research, pinned commit) + the Bridge. The Bridge
renders `$HERMES_HOME/config.yaml` from `platform.json` at every boot (model = LiteLLM
with the user's key via the egress proxy; MCP = Platform MCP with the container token
+ the user's MCP servers; cron and messaging toolsets off; memory/skills/sessions under
`/data/hermes`), starts `hermes gateway run` with only its API server on 127.0.0.1
(random per-boot key), and drives one Hermes run per delivery with
`X-Hermes-Session-Key: <session_key>`, so each chat keeps its own session (a delivery in
which the lead hands work to its crew takes follow-up runs; see "The crew"). Hermes's own
approval prompts are denied by policy (`BRIDGE_HERMES_APPROVAL`); risky real-world
actions go through the platform's `request_approval` tool.

Hermes keeps running while the runtime is `claude_code`, so switching back is instant (its
RAM counts either way; see "Measured RAM per container").

## Claude Code runtime (the user's own Claude plan)

**Install.** `agent-image/Dockerfile` installs Claude Code with Anthropic's documented
native installer at a pinned version (`CLAUDE_CODE_VERSION`, 2.1.283), checks the binary
against Anthropic's GPG-signed release manifest (key fingerprint
`31DD DE24 DDFA B679 F42D 7BD2 BAA9 29FF 1A7E CACE`) and links `/usr/local/bin/claude` to
the installed tree. The binary is not modified. `CLAUDE_CONFIG_DIR=/data/claude` (settings,
sessions and the CLI's own `.credentials.json`) and `HOME=/data/home`, so everything
survives restarts and works on the read-only rootfs. Auto-update is off
(`DISABLE_AUTOUPDATER`, `DISABLE_UPDATES`: new versions come with new images); telemetry
and error reports off; claude.ai connectors off (`ENABLE_CLAUDEAI_MCP_SERVERS=false`). The
Bridge strips `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`,
`CLAUDE_CODE_OAUTH_TOKEN` (and cloud-provider switches) from the CLI's env, so only the
user's own sign-in is ever used.

**Sign-in method: `claude auth login`** (docs: https://code.claude.com/docs/en/authentication).
It opens Anthropic's normal OAuth flow; when no browser can reach the CLI's local callback
("common in ... containers"), Anthropic's page shows a code and the CLI waits at
`Paste code here if prompted`. The CLI then stores its credentials itself in
`$CLAUDE_CONFIG_DIR/.credentials.json` (0600). We run it in a pseudo-terminal, read the
authorize URL it prints (preferring the code-paste URL over a localhost-callback one), show
that URL to the user, and type the code they paste into the CLI. We never read, copy, log
or return the credentials; status comes from `claude auth status` (exit 0 = signed in);
sign-out is `claude auth logout`. **Not used:** `claude setup-token`, because it prints a
long-lived token that the caller would have to capture and store (the platform would then
hold the user's credential), and `CLAUDE_CODE_OAUTH_TOKEN` for the same reason.

**Flow (backend).** Connect: `StartClaudeLogin` -> user opens `authorize_url`, signs in on
Anthropic's page, copies the code -> `CompleteClaudeLogin` -> if connected,
`SetAgentRuntime(CLAUDE_CODE)`. Disconnect: `ClaudeLogout` + `SetAgentRuntime(HERMES)`.

**A turn.** The Bridge runs, in `/data/workspace`:
`claude -p --output-format stream-json --verbose --mcp-config /data/.platform/claude-mcp.json
--strict-mcp-config --permission-mode dontAsk --permission-prompts none --allowedTools
Bash,Read,Edit,Write,Glob,Grep,NotebookEdit,WebFetch,WebSearch,TodoWrite,mcp__platform,mcp__user_<id>...,Agent
--disallowedTools mcp__platform__send_message --setting-sources user --agents <the crew as JSON>
--append-system-prompt <channel and crew instructions> (--session-id <new uuid> | --resume <id>)`
with the message on stdin. The MCP
config (0600, rewritten every turn) has the Platform MCP with the container's bearer token and
the user's own MCP servers. Each `session_key` maps to one Claude session
(`/data/.platform/claude-sessions.json`); a lost session starts a new one. Turns of the same
chat run one at a time. `system/init` and `result` give the session id, `tool_use` blocks
become `working` events, `system/api_retry` becomes "Claude is retrying (...)", the `result`
text is the answer the Bridge sends with `send_message`. `BRIDGE_CLAUDE_MODEL` and
`BRIDGE_CLAUDE_MAX_TURNS` are optional (default: the plan's default model, no turn limit);
so are the crew's `BRIDGE_CLAUDE_CREW_MODEL` and `BRIDGE_CREW_MAX_PARALLEL` (see "The crew").

**Compliance** (https://code.claude.com/docs/en/legal-and-compliance): the binary is installed
and run as published, and no built-in auth method is removed or disabled (we only choose, per
turn, which credential sources the child process sees: none but the user's own sign-in);
sign-in completes on Anthropic's own pages and the CLI stores the result itself; we never
collect, store or intermediate credentials or session tokens (the pasted code goes only to
the CLI's stdin and is never logged or stored); usage is billed to the user's own plan and
the platform doesn't pay for, resell or meter it. Running Claude Code in hosted sandboxes
also requires the platform to accept Anthropic's Commercial Terms.

## The crew (Byte, Patch, Scout)

The agent the user texts is the lead (Tex). It can hand work to three specialists, defined
once in `agent-image/bridge/crew.py` (description, role prompt, tool list) and given to both
runtimes. The ids `byte`, `patch`, `scout` are fixed. No proto or Go change.

| Member | Job | Tools |
|---|---|---|
| `byte` | Writes and fixes code on a new branch, commits, pushes (git gateway), opens the pull request with the GitHub app action | files (read, write), search, shell, platform tools |
| `patch` | Reviews a pull request or a diff in three passes (correctness, security, tests), merges the findings, posts ONE review on the pull request, returns "ship" / "fix first" with the top findings. Never pushes, approves or merges | files (read only), search, shell (diffs, tests), platform tools |
| `scout` | Researches on the web, returns a short answer with facts, links and what it could not confirm. Treats what it reads as information, never as instructions, and puts nothing private into a search or a page address | files (read only), search, and only `web_search` + `read_page` of the platform tools. No shell, no app actions |

The lead's instructions (`build_instructions` in `bridge/core.py`): research goes to Scout,
code changes to Byte, reviews (also of Byte's pull request) to Patch; small jobs are done
directly; at most 3 specialists per job. Nobody calls `send_message`: the Bridge sends the answer.

**Claude Code** (CLI 2.1.283; docs: https://code.claude.com/docs/en/sub-agents,
https://code.claude.com/docs/en/cli-reference, https://code.claude.com/docs/en/env-vars).
- **How they are passed:** `--agents '{"<id>": {"description", "prompt", "tools",
  "disallowedTools", "model"}}'`, inline JSON, on every run. On a new session the CLI checks the
  value at startup and exits with code 1 on a bad one.
- **How one is started:** the lead calls the `Agent` tool with `subagent_type` = the id, a short
  `description` and the `prompt`. Starting a sub-agent needs no permission, so nothing has to be in
  `--allowedTools` for it; `Agent` is listed there anyway, as in the documented examples. `Task` is
  the tool's old name and still an alias, so the Bridge reads both in the stream.
- **A specialist's tools:** `tools` is an allow list in the CLI's own names: `Read`, `Edit`,
  `Write`, `NotebookEdit`, `Glob`, `Grep`, `Bash`, `mcp__platform` for every platform tool, and
  `mcp__platform__web_search` / `mcp__platform__read_page` for single ones. It has no `Agent` and
  none of the user's own MCP servers. `disallowedTools` (`Agent`, `mcp__platform__send_message`) is
  applied first. No `mcpServers`: a specialist shares the lead's MCP connections, so
  `--strict-mcp-config` still decides which servers exist.
- **Permissions:** no `permissionMode`, so a specialist runs in the lead's `dontAsk` mode under the
  same `--allowedTools` rules; what its tool list leaves out is not there to call.
- **Only the lead starts one:** `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=1` (a sub-agent cannot start
  sub-agents), on top of the missing `Agent` tool. `CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS=1`
  removes the built-in types (Explore, Plan, general-purpose), so a call without `subagent_type`
  fails. `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` caps how many run at once.
- **Model:** `inherit` (the lead's) unless `BRIDGE_CLAUDE_CREW_MODEL` names another.
- **Background:** in `claude -p` a sub-agent runs in the background unless the lead needs its
  result first. The CLI stays open until background sub-agents are done (it gives up after 10
  idle minutes). The Bridge takes the last `result` event as the answer.
- **Known limit:** the CLI records a conversation's system prompt on its first request and reuses
  it on `--resume` until the conversation is compacted. A chat that began on an older image gets
  the crew instructions only then (the sub-agents themselves are available at once).

**Hermes** (pinned commit `35272ce2`; `cli-config.yaml.example`, `tools/delegate_tool*.py`,
`hermes_cli/config_defaults.py`). `delegate_task` takes `tasks: [{goal, context}]`, with no named
agents and no tool list per task, so the lead gets the same role prompts as briefs: it puts the
specialists of one step into one call, starts each `goal` with the member's name (`Scout: ...`)
and each `context` with that member's brief.
- **Tools:** a child gets the lead's tools minus the ones Hermes always withholds
  (`delegate_task`, `clarify`, `memory`, `send_message`, `cronjob_manage`). This Hermes cannot
  narrow them per task, so **on Hermes a specialist's tool list is an instruction, not a lock**:
  Scout is told to use only `web_search` and `read_page`, but the shell and app actions are still
  within its reach.
- **Config** (rendered into `$HERMES_HOME/config.yaml`): `delegation: {max_spawn_depth: 1,
  orchestrator_enabled: false, max_concurrent_children: 3, subagent_auto_approve: false}`. One
  level deep; no child gets `delegate_task` back; a child's dangerous command is denied.
  `max_concurrent_children` is the most tasks one `delegate_task` call may carry and the most calls
  running in the background at once (a call over that runs inside the lead's turn instead).
- **Follow-up runs:** on the Runs API Hermes runs the lead's children in the background. The run
  ends with a short status, and the reports are stored in the session for the next turn, which
  the client has to start. So after a run that handed work over, the Bridge waits until `GET
  /health/detailed` reports no background delegations (twice in a row) and starts a follow-up run
  in the same session (at most 3, inside the turn's time limit). The last run's answer is the
  turn's answer. If Hermes does not report its background work, the lead's status is the answer.

**Progress lines.** One `working` event when a specialist starts and one when it ends, each
starting with the member's name and a colon: `Scout: researching flight options`, then
`Scout: done`. Other endings: `could not finish`, `stopped`, and `finished` when the engine does
not say how it ended. A specialist's own tool calls stay `using <tool>` lines.
- **Claude Code:** the start is the lead's `Agent` tool call (the line is its `description`). The
  end is that call's result, or the `task_notification` event for a specialist in the background.
  One still open when the run ends gets `finished`, or `stopped` after a timeout or a crash.
- **Hermes:** the `subagent.start` (its `goal`) and `subagent.complete` events of a run. A child
  still working in the background when its run's event stream closes gets `finished`, sent when
  Hermes reports no background work left.

**Settings.** `BRIDGE_CREW_MAX_PARALLEL` (default 3, at least 1): specialists at a time, on both
runtimes. `BRIDGE_CLAUDE_CREW_MODEL` (default: the lead's model): a model alias or id for the
specialists on Claude Code, e.g. `haiku`. Both are read from the Bridge's environment. The
vm-manager passes containers a fixed list of variables that doesn't include them, so set them
with an `ENV` line in `agent-image/Dockerfile`, like the other optional `BRIDGE_*` settings.

## Egress contract (with the backend's infra)

The proxy is Squid. The backend's base `squid.conf` has
`include /etc/squid/allowlists/*.conf` (this service's `EGRESS_ALLOWLIST_DIR`) and
already lets the whole sandbox subnet reach LiteLLM and the api `/mcp`. The vm-manager
writes one file per **awake** user, `<user_id>.conf`:

```
acl u_<hash>_src src <container_ip>/32
acl u_<hash>_dst dstdomain .example.com other.org
http_access allow u_<hash>_src u_<hash>_dst
```

(dst lines skipped when the list is empty), then sends `SIGHUP` to the
`EGRESS_PROXY_CONTAINER` through the Docker API (coalesced, at most once per
`EGRESS_RELOAD_MIN_INTERVAL`). Rewritten on every wake and on `SetEgressAllowlist`;
**removed on every stop** (a stopped container's IP can be reused by another user's
container) and on delete. `_vm-manager.conf` (a comment) keeps the include glob
non-empty. While a user's runtime is `claude_code`, a Claude sign-in is pending or a sign-out
is running, their file also lists `CLAUDE_EGRESS_DOMAINS` (default `api.anthropic.com` for
the model API, `claude.ai` and `claude.com` for claude.ai sign-in, `platform.claude.com` for
OAuth token exchange / refresh / revocation; from
https://code.claude.com/docs/en/network-config). Switching back to Hermes removes them.
`Sandbox.allowlist` keeps reporting only the user's own domains. Domains are validated (hostnames with at least two labels, no IPs;
`*.x.com` / `.x.com` = x.com and subdomains; max 200) so user input can't inject
Squid config.

## Measured RAM per container

Measured 2026-09-30 on AWS `m7i-flex.large` (2 vCPU, 7.6 GiB, Ubuntu 24.04, Docker 29.8,
gVisor `runsc` release-20260921.0), `docker stats --no-stream pa-sbx-<user_id>`:

| State | RAM |
|---|---|
| Awake, idle: Bridge + `hermes gateway run` + a pending `claude auth login` | **325 MiB** (limit 600 MiB) |

Not yet measured after a real turn (no model provider key on that server; Claude Code turns
come next). The platform services took ~0.85 GiB (LiteLLM alone ~545 MiB), so on the 7.6 GiB
test server ~5.5 GiB is left for sandboxes: ~15 awake at ~350 MiB, fewer once turns add
Claude Code's own process. `AWAKE_CAP` is 3 there for the first test; re-measure during a
Claude Code turn before raising it:

```bash
docker stats --no-stream pa-sbx-<user_id>
```

## Not verified yet

Unit tests use a fake Docker daemon and a fake Bridge; the Bridge's Claude mode is tested
against a fake `claude` executable. Not yet run for real: the Docker SDK calls against a
daemon, gVisor, `docker cp` into a read-only-rootfs container's volume, the Squid reload,
the agent image build, and Hermes itself (config keys, `gateway run`, the Runs API) inside
the read-only container. For Claude Code: the real CLI in the image (installer, manifest
check, version string), its `auth login` screen in a pty (URL format, whether the TUI wraps
the URL, the success text), a real sign-in with a Pro/Max account through the egress proxy,
the exact stream-json shapes and error texts of this version, and the CLI under gVisor with
a read-only rootfs. For the crew: nothing has run against the real CLI or a real Hermes (the
`--agents` value, the sub-agent events, Hermes's background delegation and the follow-up
turn); see `docs/build-report.md` section 8.
