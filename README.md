# personal-agent vm-manager

The only service that controls Docker. It creates, wakes, stops and deletes each
user's sandboxed agent container (Hermes Agent + Claude Code + Bridge, and Scout's browser for
users who have it), delivers messages into it, records awake time and browser time for billing,
and writes each container's egress allowlist.

Two agent runtimes, chosen per user (`SetAgentRuntime`):
- **hermes** (default): Hermes Agent on the platform's models through LiteLLM.
- **claude_code**: the official, unmodified Claude Code CLI on the **user's own Claude plan**
  (they sign in with Anthropic; usage bills their plan). See "Claude Code runtime" below.
The backend calls it over gRPC (`proto/vm/v1/vm.proto`, the source of truth).

Design: `../beckend/docs/hld-mvp.md`, and `../beckend/docs/backend-report.md` Part D ("Containers: what the backend asks for" to "Scout's browser").
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
agent-image/             Dockerfile + Bridge (Python) + its tests + browser/ (the pinned browser server)
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
`api.anthropic.com,claude.ai,claude.com,platform.claude.com`), `CLAUDE_LOGIN_TTL` (10m). Scout's
browser: `SANDBOX_BROWSER_MEMORY_MB` (2048) and `SANDBOX_BROWSER_PIDS_LIMIT` (1024), the limits of a
container with the browser turned on (see "Scout's browser"). Tunables: `AWAKE_CAP`, `IDLE_TIMEOUT`,
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
  min(until, now); `until_unix_ms = 0` means now. `browser_seconds` is the browser time the
  containers reported in the window: each report counts in the window that holds the moment
  the vm-manager stored it (its own clock), so windows asked one after the other return every
  second exactly once. A user with browser time and no awake time in the window still gets a row.
- `CreateSandbox.browser_enabled` / `SetBrowser` / `Sandbox.browser_enabled`: the switch for
  Scout's browser (a Max-plan feature). `Sandbox.browser_enabled` is the stored switch.
  `SetBrowser` only stores it and answers at once; the container gets it on its **next wake**
  (see "Scout's browser"). A sandbox that is awake keeps what it was woken with until it stops;
  call `StopSandbox` after `SetBrowser` to apply the change sooner. `NOT_FOUND` for an unknown user.
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
- **Wake**: capacity guard, re-create if the image version changed, the container is
  missing, or the browser switch changed since the container was made (keeps /data), write
  the browser switch into platform.json, start (retry once, then re-create), wait for
  `GET /healthz`, write the egress file, open an awake period.
- **Runtime**: stored in the table and in platform.json; survives re-creation (it is on /data).
- **Deliver**: wake if needed, `POST /deliver`, relay events; `busy` on `accepted`,
  back to `awake` + last-active on `turn_done` / `error`. Timeout (15 min or
  `timeout_seconds`) -> force stop + `ERROR` with `platform_fault=true`. The turn is
  tracked to the end even if the gRPC caller disconnects.
- **Stop**: polite = `POST /shutdown` (Hermes saves to /data, up to 20 s) then `docker
  stop`; force = kill. Removes the egress file, closes the awake period.
- **Delete**: container, volume, egress file, row. Awake periods are kept for billing.

Loops: idle reaper (1 min; not busy, nothing in flight, no pending Claude sign-in, Bridge
reports no turn and no sign-in, idle >= 3 min; it waits up to 10 s for the user's lock, which a health check
holds at the same moments, and logs "reaper: sandbox busy with another operation" when it still has to leave a
sandbox to the next round), health checker (30 s; 2 failed checks -> restart; more than 3 restarts without a
good check -> stop + `broken` + an `alert=true` log line), reconciler (startup + 5 min;
running/not-running drift, stale `waking`/`busy`, orphan and duplicate containers,
leftover egress files; never deletes volumes). Capacity guard: evict awake, not-busy
containers, lowest priority first, then longest idle.

## Container spec

gVisor (`runsc`), 600 MB (no swap), 1 CPU, pids limit 256, read-only rootfs, only the
`/data` named volume writable, `cap-drop ALL`, `no-new-privileges`, non-root uid 10001,
the sandbox network only, no published ports, `--init`, no restart policy. A container with
Scout's browser turned on gets 2048 MB and a pids limit of 1024 instead
(`SANDBOX_BROWSER_MEMORY_MB`, `SANDBOX_BROWSER_PIDS_LIMIT`) and the label `pa.browser=true`;
everything else is the same. A disk
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
| `GET /healthz` | `200 {"ok":true,"busy":N,"runtime":"hermes\|claude_code","claude_connected":bool\|null,"claude_login_pending":bool,"browser":bool}` when the current runtime answers (Hermes API / `claude` installed), else `503`. `busy` = turns in progress; `claude_connected` is `null` until `claude auth status` has answered once; `browser` = Scout's browser is turned on in this container |
| `POST /deliver` | Body `{"delivery_id","kind","session_key","channel","text","attachments":[{"id","name","mime_type","url"}],"approval_id","approved","metadata":{}}`, `kind` = `message` / `approval_answer` / `automation` / `app_event`. `200` with `Content-Type: application/x-ndjson`, one line per event: `{"type":"accepted\|working\|turn_done\|error","message":"...","platform_fault":bool}`, ending with `turn_done` or `error`. A `working` message is `using <tool>`, or a crew line that starts with the member's name and a colon (`Scout: researching flight options`, `Scout: done`; see "The crew"). The last event also has `"browser_seconds":N,"browser_report_id":"<delivery_id>#<n>"` when Scout's browser was open: whole seconds since the Bridge's last report, and an id under which the vm-manager stores the report once (both are left out when the browser never started). `401` bad secret, `400` bad body |
| `POST /shutdown` | Stop taking work, stop Hermes (it saves to /data), `200` when done (< 20 s) |
| `POST /claude/login/start` | Starts `claude auth login` (in a pty). `200 {"login_id","authorize_url","expires_at"}` (unix ms, +10 min). One pending sign-in per container; a new start replaces it. `502` no link shown, `503` Claude Code not installed |
| `POST /claude/login/complete` | Body `{"login_id","code"}`. Types the code into the waiting CLI, waits (<= 60 s) and asks `claude auth status`. `200 {"connected":bool,"message":"..."}`; `409` unknown / expired / used; `400` missing or multi-line code. Bodies are never logged |
| `POST /claude/logout` | Runs `claude auth logout`. `200 {"ok":true,"claude_connected":false}` |

Dedupe by `delivery_id`: a repeat of a finished delivery returns `accepted` +
`turn_done` without running again; a repeat while it runs attaches to the same turn; a
retry after a failed send only re-sends the saved answer. The record lives in
`/data/.platform/deliveries.json`, so it survives restarts. Failed runs are re-run on retry.
A repeat of a finished delivery also sends that delivery's browser report again, with the same id.

The Bridge never returns reply text. It sends the final answer itself by calling the
Platform MCP tool `send_message` `{"text","session_key"}` with the container's bearer
token. The agent (Hermes or Claude Code) sees the other Platform MCP tools but not `send_message`.

Deliver errors from the Claude Code runtime: not signed in / login expired / token revoked
-> `"Connect your Claude plan again"`, `platform_fault=false`; plan usage limit, rate limit or
spend limit -> `"Your Claude plan's usage limit was reached: <the CLI's message>"`,
`platform_fault=false`; account on hold or organisation not allowed -> `"Your Claude account can't be
used right now"`, `platform_fault=false`; Anthropic's safeguards refused the request (on the first model
and on the CLI's fallback) -> `"Claude declined this request under Anthropic's usage policy"`,
`platform_fault=false`; step limit (`error_max_turns`) -> `platform_fault=false`; crash,
timeout, overload, anything else -> `platform_fault=true`. The Bridge decides from the CLI's own error code
for a failed API call (the `error` of its assistant message: `authentication_failed`, `billing_error`,
`rate_limit`, ...), a `stop_reason` of `refusal`, then the error text.

Every failed Claude Code run is logged once in `docker logs` (`bridge.claude WARNING claude run failed: {...}`):
the delivery, the reason, `platform_fault`, the exit code, how the run ended (`subtype`, `is_error`,
`terminal_reason`, `api_error_status`, `stop_reason`), the CLI's last API error (its code, text and a refusal's
category), the last API retry and the last 10 lines of stderr. The container's secrets are replaced with
`[secret]`, and a line that quotes the user's message is left out.

A message whose turn failed is never acted on later, on either engine (see "A turn" under "Claude Code
runtime", and "Agent image" for Hermes).

## Agent image

`agent-image/`: Hermes Agent (Nous Research, pinned commit) + the Bridge. The Bridge
renders `$HERMES_HOME/config.yaml` from `platform.json` at every boot (model = LiteLLM
with the user's key via the egress proxy; MCP = Platform MCP with the container token
+ the user's MCP servers; cron and messaging toolsets off; Hermes's own memory off (see "One
memory" below); skills and sessions under `/data/hermes`), starts `hermes gateway run` with only its API server on 127.0.0.1
(random per-boot key), and drives one Hermes run per delivery with
`X-Hermes-Session-Key: <session_key>`, so each chat keeps its own session (a delivery in
which the lead hands work to its crew takes follow-up runs; see "The crew"). Hermes saves a turn to its
session on every exit path, a failed one included, and has no documented way over its API to take a turn
back, so after a turn that reached Hermes and failed, the chat goes on in a new Hermes conversation: the key
gets a suffix (`telegram:42#1`, then `#2`, ...; `/data/.platform/hermes-conversations.json`). The failed
message is never continued; the cost is that Hermes starts that chat afresh. What carries over is the
platform's memory (`recall`) and Hermes's `session_search` over earlier conversations. Hermes's own
approval prompts are denied by policy (`BRIDGE_HERMES_APPROVAL`); risky real-world
actions go through the platform's `request_approval` tool.

Hermes keeps running while the runtime is `claude_code`, so switching back is instant (its
RAM counts either way; see "Measured RAM per container").

**Hermes fetches nothing at runtime.** `security: {allow_lazy_installs: false}` in `config.yaml` and
`HERMES_DISABLE_LAZY_INSTALLS=1` in the gateway's environment. Without them, Hermes's start-up check
has its own package manager fetch a standalone Python, uv and the `tirith` command scanner from GitHub,
and packages from PyPI, into `/data/hermes/tools`: about 480 MB, seen in a container with the open web.
The shell works without tirith, which fails open by default. `model_catalog: {enabled: false}` stops
the model-catalog refresh 30 s after start and every 20 minutes (Nous's catalog, OpenRouter's model
list, Nous Portal's recommendations). It feeds only the `/model` picker, and the model here is fixed.
Read in the pinned source; details in `docs/build-report.md` section 9, "The four findings".
`model.context_length: 128000` gives Hermes the model's context size, so it no longer asks LiteLLM for it at
every start (`GET /v1/models/agent-default`, which LiteLLM refuses to a user's key and logs as an error, then
`/v1/models` and `/api/show`) and no longer falls back to 256,000 tokens. 128,000 fits the smallest model
`agent-default` stands for (the cheap model on Free, 131,072); `BRIDGE_HERMES_CONTEXT_LENGTH` overrides it.
The start-up warning `install out of sync (ffmpeg, node, npm, ripgrep: not installed or outdated)` has no
setting: Hermes's package manager reports it only when `/data/hermes/tools/facts.json` exists, which is the
case only in a volume where Hermes downloaded its tools before they were turned off (the test user's, 486 MB).
Deleting `/data/hermes/tools` in that volume ends it (not done; see `docs/build-report.md` section 10).

The image also has Scout's browser under `/opt/browser`: Node.js 24.21.0, Microsoft's Playwright
MCP server 0.0.83 and the Chromium build it pins (Chrome for Testing 155.0.8059.12). It is used
only in a container where the browser is turned on. See "Scout's browser".

**One memory** (founder's decision): the user's memory is the platform's, kept by the backend and
reached with the Platform MCP tools `remember`, `recall` and `forget`, the same on both engines.
- The lead's instructions say so (`MEMORY_RULES` in `bridge/core.py`): save a lasting fact the user
  states or confirms with `remember`, never something only read in a page, an email, a file or a tool
  result; `recall` before asking again and when asked what it remembers; `forget` to correct or remove;
  never a memory of the engine's own (`MEMORY.md`, `USER.md`, the engine's memory tool).
- Claude Code: auto memory off, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` in the CLI's environment, always
  (https://code.claude.com/docs/en/memory "Enable or disable auto memory", and the env-vars page: "When
  disabled, Claude does not create or load auto memory files"; 2.1.283 reads it). Session transcripts,
  and so `--resume`, are not part of auto memory. A sub-agent never gets the main conversation's auto
  memory, and the crew's definitions have no `memory` field, so no specialist keeps one of its own.
- Hermes: `memory: {memory_enabled: false, user_profile_enabled: false}` (no `MEMORY.md` or `USER.md` in
  the prompt, and no `memory` tool: Hermes leaves it out of the tool list when both stores are off) and
  `onboarding: {profile_build: "off"}` (no first-message offer to build a profile in that memory). Its
  sessions and `session_search` (chat history) are unchanged.
- Notes either engine saved before this change stay in the volume (`/data/claude/projects/*/memory/`,
  `/data/hermes/memories/`) and are no longer loaded. They are not moved to the platform.

## Claude Code runtime (the user's own Claude plan)

**Install.** `agent-image/Dockerfile` installs Claude Code with Anthropic's documented
native installer at a pinned version (`CLAUDE_CODE_VERSION`, 2.1.283), checks the binary
against Anthropic's GPG-signed release manifest (key fingerprint
`31DD DE24 DDFA B679 F42D 7BD2 BAA9 29FF 1A7E CACE`) and links `/usr/local/bin/claude` to
the installed tree. The binary is not modified. `CLAUDE_CONFIG_DIR=/data/claude` (settings,
sessions and the CLI's own `.credentials.json`) and `HOME=/data/home`, so everything
survives restarts and works on the read-only rootfs. Auto-update is off
(`DISABLE_AUTOUPDATER`, `DISABLE_UPDATES`: new versions come with new images); telemetry
and error reports off; claude.ai connectors off (`ENABLE_CLAUDEAI_MCP_SERVERS=false`); auto memory
off (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`: the user's memory is the platform's, see "One memory"). The
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
Bash,Read,Edit,Write,Glob,Grep,NotebookEdit,TodoWrite,WebSearch,mcp__platform,mcp__user_<id>...,Agent
--disallowedTools mcp__platform__send_message WebFetch --setting-sources user
--settings /data/.platform/claude-settings.json --agents <the crew as JSON>
--append-system-prompt <channel and crew instructions> (--session-id <new uuid> | --resume <id> --fork-session)`
with the message on stdin. The MCP
config (0600, rewritten every turn) has the Platform MCP with the container's bearer token and
the user's own MCP servers. The settings file (0600, rewritten every turn) has one hook, which
keeps the lead off the web tools; `WebSearch` is in the allow list for Scout, not for the lead
(see "The crew"). Each `session_key` maps to one Claude session
(`/data/.platform/claude-sessions.json`); a lost session starts a new one. Turns of the same
chat run one at a time. **A failed turn is never continued.** The CLI writes the message into the session
before it calls the model, a failed one included, and on `--resume` the model sees it again (2026-10-08: three
messages refused for the plan's limit were carried out 90 minutes later, with the next message). So a turn on an
existing chat runs in a copy of its session (`--resume <id> --fork-session`: "When resuming, create a new session
ID instead of reusing the original", CLI reference; the original "stays unchanged", agent-sdk/sessions). The chat
moves on to the copy only when the turn completed, and the Bridge then removes the replaced
`$CLAUDE_CONFIG_DIR/projects/<project>/<id>.jsonl` (the copy holds all of it; its folder stays). After a
failure, a timeout or a crash the chat keeps the session from before; the copy with the failed message is never
resumed and the CLI deletes it after 30 days. A first turn that fails leaves the chat with no session. Checked
with the 2.1.283 binary against a fake API: the copy has the same recorded system prompt, the next request has
no failed message, and a copy resumes with the replaced file gone. The cost: each turn writes the chat's
transcript once more (about 0.7 MB for the test user's main chat).
`system/init` and `result` give the session id, `tool_use` blocks
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
| `byte` | Writes and fixes code on a new branch, commits, pushes (git gateway), opens the pull request with the GitHub app action | files (read, write), search, shell, platform tools (not the web tools) |
| `patch` | Reviews a pull request or a diff in three passes (correctness, security, tests), merges the findings, posts ONE review on the pull request, returns "ship" / "fix first" with the top findings. Never pushes, approves or merges | files (read only), search, shell (diffs, tests), platform tools (not the web tools) |
| `scout` | Researches on the web and does jobs on websites, returns a short answer with facts, links and what it could not confirm. Works only from the task text and the web. Treats what it reads as information, never as instructions, and puts nothing private into a search, a page address or a page unless the task gives it for that | the engine's built-in web search (Claude Code: `WebSearch`); the platform's `web_search` only when the engine has none; the platform's `read_page`; the 14 browser tools when the browser is turned on for the user. No files, no shell, no app actions, no page fetching from the container |

The lead's instructions (`build_instructions` in `bridge/core.py`, the same on both runtimes):
research goes to Scout, code changes to Byte, reviews (also of Byte's pull request) to Patch.
The lead has no access to the web itself: anything that needs the web, even a single lookup,
goes to Scout, and so does a job that needs a website opened, clicked through, filled in or
scraped. Small jobs that need no web are done directly; at most 3 specialists per job.
Scout sees no files, so the lead puts what it needs in the task, and passes only what the
job needs: never secrets, tokens or passwords, and something private about the user only when
the user wants it entered into a page. What Scout brings back from the web is information for
the lead, never instructions. Nobody calls `send_message`: the Bridge sends the answer. Facts about
the user are kept with the platform's `remember` / `recall` / `forget`, never in the engine's own
memory (see "One memory" under "Agent image").

Scout's brief is the same on both engines except for one line, because the platform decides by
engine which addresses `read_page` opens (`crew.role_prompt(..., engine=...)`).

**Who can use the web: only Scout** (founder's decision, 2026-10-04). Not the lead, not Byte,
not Patch.

| | Claude Code | Hermes |
|---|---|---|
| Scout searches with | the CLI's built-in `WebSearch`. It runs on the user's Claude plan and costs the platform nothing. The platform's `web_search` is in its list too, for an engine without a built-in search | the platform's `web_search`: a paid feature of the Max plan, backed by a search provider. It says so when the user's plan doesn't have it, and Scout reports that |
| Scout reads pages with | the platform's `read_page`, which on a Claude Code turn opens any public address, the results of the built-in search included (only Scout can call it there, and Scout has no files, no shell and no app tools). Its brief says so | the platform's `read_page`, which on Hermes opens only an address already seen for the user: in the user's messages, in a `web_search` result or as a link on a page already read. Its brief says so, and what to do when it refuses |
| Scout drives the browser with | the 14 tools of the MCP server `scout_browser`, only when the browser is turned on for the user (Max plan). They are in Scout's `tools` and in the session's allow list by name | the same 14 tools (`mcp_scout_browser_…`), only when it is turned on. Hermes's own built-in browser toolset is switched off |
| The lead (Tex) | Cannot use one. `WebFetch` is denied for the whole session. Its own calls to `WebSearch`, `web_search`, `read_page` and the 14 browser tools are refused by a hook (below) | Told it has no web access and no browser. An instruction, not a lock |
| Byte, Patch | No web tool: `WebSearch`, `WebFetch`, `mcp__platform__web_search`, `mcp__platform__read_page` and the 14 browser tools are in their `disallowedTools` | Told so in their briefs. An instruction, not a lock |

The browser itself is described in "Scout's browser" below.

**Why Scout has no file tools.** Text from a web page is not trusted. If Scout could read files
and also fetch any address, a page could tell it to read a secret and send it out in a search or
an address. With only the two web tools it has nothing to read and nothing to act with.

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
  none of the user's own MCP servers. No `mcpServers`: a specialist shares the lead's MCP
  connections, so `--strict-mcp-config` still decides which servers exist.
  - Byte: `Read, Edit, Write, NotebookEdit, Glob, Grep, Bash, mcp__platform`.
  - Patch: `Read, Glob, Grep, Bash, mcp__platform`.
  - Scout: `WebSearch, mcp__platform__web_search, mcp__platform__read_page`, and nothing else;
    with the browser turned on for the user also `mcp__scout_browser__browser_navigate` and the
    13 other browser tools, each by name.
    Scout is told to search with the built-in search, and to use `web_search` only when it has
    none. The built-in search needs no search provider key on the platform, and its query goes to
    the model provider, not to a site. `read_page` opens its results: on a Claude Code turn the
    platform lets it open any public address (until 2026-10-08 Scout was told it would not).
- **A second lock:** `disallowedTools` is applied before `tools`. It has `Agent`,
  `mcp__platform__send_message` and `WebFetch` for everyone, plus every built-in file, edit, shell
  and web-search tool the member's list leaves out: Scout `Read, Edit, Write, NotebookEdit, Glob,
  Grep, Bash`; Patch `Edit, Write, NotebookEdit, WebSearch`; Byte `WebSearch`. Byte and Patch are
  given the whole Platform MCP, so `mcp__platform__web_search` and `mcp__platform__read_page` are
  denied for them by name, and so are the 14 browser tools. The docs say an allow list alone gives "only the listed tools"; the
  deny list holds however the allow list resolves.
- **The session's two lists are shared.** The lead and the specialists are one CLI session
  (docs: https://code.claude.com/docs/en/permissions, the sub-agents page under "Available tools",
  https://code.claude.com/docs/en/tools-reference under "Agent tool behavior"). Deny wins over
  allow, whatever the order and however specific the rules are. A tool denied for the session is
  gone for every sub-agent too, even one whose `tools` names it: a sub-agent inherits "the tools
  available in the main conversation". A specialist's calls are also checked against the session's
  allow list. So `--disallowedTools` has only what nobody may have (`mcp__platform__send_message`,
  `WebFetch`), and `--allowedTools` keeps `WebSearch` and `mcp__platform`, because Scout needs them.
  With the browser turned on it also names the 14 browser tools one by one, never the whole server
  `mcp__scout_browser`: in `dontAsk` mode a tool that is not named is refused for everyone.
- **The lead is kept off Scout's web tools by a hook** (docs: https://code.claude.com/docs/en/hooks).
  `/data/.platform/claude-settings.json`, passed with `--settings`, has one `PreToolUse` hook. It
  matches `WebSearch`, `mcp__platform__web_search`, `mcp__platform__read_page` and the 14 browser
  tools (one list in `bridge/crew.py`: Scout's web tools) and runs
  `bridge/lead_gate.py` before every such call, in the lead and in a sub-agent alike. The hook's
  input has `agent_id` only for a call from inside a sub-agent. For the lead's own call the script
  exits with code 2: the CLI blocks the call and shows the lead the reason ("hand this to Scout").
  For a sub-agent's call it exits 0 and the usual rules apply. The lead still sees the tools
  in its list; calling one fails. If the hook cannot run or cannot read its input, the call goes
  through: a broken hook never takes the web from Scout, and the lead's instruction is what is left.
- **The platform's `web_search` and `read_page`** exist since the backend's crew package. Deploy
  both repos together: an agent image with an older backend leaves Scout with the built-in search
  only.
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
  the crew instructions only then (the sub-agents themselves, with their briefs, are available at
  once). The same holds for the memory rule in the lead's instructions; auto memory is off at once.

**Hermes** (pinned commit `35272ce2`; `cli-config.yaml.example`, `tools/delegate_tool*.py`,
`hermes_cli/config_defaults.py`). `delegate_task` takes `tasks: [{goal, context}]`, with no named
agents and no tool list per task, so the lead gets the same role prompts as briefs: it puts the
specialists of one step into one call, starts each `goal` with the member's name (`Scout: ...`)
and each `context` with that member's brief.
- **Tools:** a child gets the lead's tools minus the ones Hermes always withholds
  (`delegate_task`, `clarify`, `memory`, `send_message`, `cronjob_manage`). This Hermes cannot
  narrow them per task, so **on Hermes a specialist's tool list is an instruction, not a lock**:
  Scout is told it has no files and only `web_search` and `read_page`, but files, the shell and
  app actions are still within its reach. The web rule is carried the same way: the lead, Byte
  and Patch are told they have no web tool, but `web_search` and `read_page` are still in their
  list (hiding them from the lead in the Hermes config would hide them from Scout too). The
  browser tools are the same: with the browser turned on, every Hermes agent has them. This is
  the risk that remains. What the platform does about it: on a Hermes turn `read_page` fetches only
  addresses already seen in the user's messages, in `web_search` results or as links on pages already
  read, so a made-up address can't carry data out through it (on a Claude Code turn, where only Scout
  can call it, it opens any public address).
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

## Scout's browser (Max plan)

A headless browser inside the user's own container, for pages that need JavaScript, clicks or a
form. Founder's decision of 2026-10-04: the browser runs in the container (not at a hosted
provider), it is part of the Max plan, it costs 2 credits per minute of browser time, and only
Scout drives it. Signing in to sites is not part of this version. The full account, with what was
run and what was not, is in `docs/build-report.md` section 9.

**The switch.** `sandboxes.browser_enabled`, set by `CreateSandbox.browser_enabled` and
`SetBrowser`, returned as `Sandbox.browser_enabled`. On the next wake the vm-manager:
1. re-creates the container if it was made with or without the browser and the switch changed
   (the label `pa.browser`; memory and process limits are fixed when a container is made; /data
   is kept, as in an image rollout);
2. writes `"browser_enabled": true` into `/data/.platform/platform.json` (or takes it out);
3. writes the egress rule for the container it started (below).
The Bridge reads the switch once, when it boots. A running container never changes.

**What is in the image** (`/opt/browser`, read-only at runtime; `agent-image/Dockerfile`):
Node.js 24.21.0 (the one binary from `node:24.21.0-trixie-slim`), `@playwright/mcp` 0.0.83 with
`playwright` / `playwright-core` 1.64.0-alpha-1790635538000 (`npm ci` from
`agent-image/browser/package-lock.json`), and the browser that Playwright version pins: Chrome for
Testing 155.0.8059.12 (`playwright-core install --no-shell chromium`), with the system libraries
and fonts from Playwright's own list (`install-deps chromium`). This is how Microsoft's own
Playwright MCP image is built. The image grows by an estimated 0.7 GB (Chromium alone is 400 MB
unpacked); not measured, because the image has not been built yet.

**How the engines drive it.** An MCP server named `scout_browser`, over stdio, only in a container
with the browser turned on. Its command is the same for both engines (`bridge/browser.py`):

```
python -I /opt/bridge/bridge/browser_mcp.py --allow <the 14 tools> \
  --env PLAYWRIGHT_BROWSERS_PATH=/opt/browser/ms-playwright --env HOME=/data/home --env TMPDIR=/data/tmp \
  -- node /opt/browser/node_modules/@playwright/mcp/cli.js --config /data/.platform/browser-mcp.json
```

- **Claude Code:** an entry in `--mcp-config` (`"type": "stdio"`). The CLI starts the server for
  each run and it ends with the run, and the browser with it.
- **Hermes:** an entry in `mcp_servers` (`command`, `args`). Hermes starts it once and keeps it;
  the Bridge closes the browser when a turn ends.
- The server opens the browser at the first tool call, not before.

**The guard** (`bridge/browser_mcp.py`, standard library, run as a file on its own). Playwright MCP
is made for a developer's own machine; its documentation says it "is not a security boundary". So
the engines never talk to it directly. The guard passes MCP messages between engine and server with
three rules:
- **Tools:** it lists and lets through 14 tools: `browser_navigate`, `browser_navigate_back`,
  `browser_snapshot`, `browser_find`, `browser_click`, `browser_type`, `browser_fill_form`,
  `browser_select_option`, `browser_press_key`, `browser_hover`, `browser_wait_for`,
  `browser_handle_dialog`, `browser_tabs`, `browser_close`. The server's other 11 are refused:
  they read files from the container (`browser_file_upload`, `browser_drop`), run code
  (`browser_run_code_unsafe`, `browser_evaluate`), write pictures (`browser_take_screenshot`) or
  are for debugging pages.
- **Addresses:** an `url` argument must be a full `http://` or `https://` address (or
  `about:blank`). Seen with 0.0.83: the server refuses `file:///…` but opens
  `view-source:file:///…` and shows the file. The guard refuses both, and every other scheme.
- **Files:** a `filename` argument is dropped, so nothing is written into the user's workspace.
On Claude Code the tools are `mcp__scout_browser__browser_navigate` and so on; on Hermes
`mcp_scout_browser_browser_navigate` and so on.

**The browser's settings** (`/data/.platform/browser-mcp.json`, written by the Bridge):

| Setting | Why |
|---|---|
| `headless`, `chromiumSandbox: false` (Chromium gets `--headless --no-sandbox`) | gVisor is the sandbox. Chromium's own needs privileges the container does not have |
| `channel: chromium` | the full browser, the only one in the image. Without a channel Playwright starts a headless launch from its separate headless shell, which `install --no-shell` leaves out, and fails with `Browser "chromium" is not installed` |
| `--disable-dev-shm-usage` | shared memory goes to `TMPDIR` (`/data/tmp`), not to the small `/dev/shm` |
| `--disable-gpu` | no GPU in the container; one process less |
| `--disable-background-networking`, `--disable-component-update` | no background requests and no component updates of the browser's own. Playwright passes both itself; they are in the Bridge's list too, so a newer Playwright cannot drop them unnoticed, and the self-check fails when the running browser lacks them. Only plain switches are added: Playwright puts ours after its own, so a `--disable-features` of ours would replace its list, and Playwright MCP drops `--disable-extensions` from any list |
| `userDataDir /data/browser/profile`, `--disk-cache-dir=/data/browser/cache` (100 MB), `outputDir /data/browser/output` (20 MB) | the root filesystem is read-only; cookies stay in the user's own volume |
| `proxy.server` = the container's `HTTPS_PROXY` | every request goes through the egress proxy. Playwright adds `--proxy-bypass-list=<-loopback>`, so requests to the container's own loopback go to the proxy too, and are refused there |
| `timeouts.idle: 120000` | the server closes the browser after 2 minutes without a tool call and opens it again on the next one |
| `snapshot.mode: none` | a tool that acts on a page does not write the page to a file; Scout asks for it with `browser_snapshot` |
| `imageResponses: omit`, `webmcp: false`, `codegen: none` | no pictures; tools a page registers itself are not passed on; no code samples in answers |

**What Chromium still sends to Google.** With all of the above, Chrome for Testing 155 makes a few
connections of its own each time it starts, whatever page it opens (measured with the pinned browser
behind a logging proxy and in Chromium's own net log, 2026-10-08): the Google account check
(`POST accounts.google.com/ListAccounts`, no account signed in), the push-messaging (GCM) registration
(`android.clients.google.com/checkin` and `/c2dm/register3`, then `mtalk.google.com:5228`, which the
egress proxy refuses by port), an on-demand install of one component (`update.googleapis.com`) and
preconnects to `www.google.com`. No Chromium switch stopped any of them; a managed policy file stopped
only the preconnects. Playwright's separate headless shell (the same Chromium build) made none of
them. See `docs/build-report.md` section 9 for the options.

**The network.** See "Egress contract": that container, and only it, may reach any public web
address on ports 80 and 443. The rule belongs to the container's address, not to the browser:
every program in that container can use it.

**Browser time.** Measured by the Bridge (`BrowserMeter` in `bridge/browser.py`): once a second
it looks in `/proc` for a process started from `/opt/browser/ms-playwright`. The time runs from
the moment a browser's main process exists to the moment none does. The browser is closed:
by Scout (`browser_close`), by the server after 2 idle minutes, by the Bridge when the last
running turn ends, and by the Bridge after 15 seconds with a browser open and no turn running.
With the last event of a turn the Bridge reports the whole seconds since its last report
(`browser_seconds`, `browser_report_id`); the vm-manager stores the report (table
`browser_usage`, unique per user and report id) before it passes the event on, and `GetUsage`
returns it. Nothing is reported when the browser never started. Limits of this: the time is
measured inside the user's own container, by a process the agent could disturb or go around
(it has a shell), so it is good enough to charge an honest user and cannot be used to charge
another one; a turn that is force-stopped on its time limit reports nothing (that turn is not
charged anyway).

**Memory and how many containers fit.** A browser container's limit is 2048 MB instead of
600 MB: at its limit one takes the room of 3.4 ordinary ones. The awake cap counts containers,
not memory, so set `AWAKE_CAP` for the worst case you allow: on the 7.6 GiB test server about
5.5 GiB is left for sandboxes, which is 9 ordinary containers at their limit, or 2 browser
containers at theirs. Measured on the development machine (not in the image, not under gVisor;
proportional set size): the server process 115 to 165 MiB (on Hermes it stays for as long as the
container is awake; on Claude Code only during a run), Chromium with one page 140 MiB
(example.com) to 380 MiB (a news front page), in 6 processes and about 90 threads with the
server. So a browser in use adds roughly 0.3 to 0.55 GiB to a container. A container that goes
over its limit is killed and the turn fails.

**Check it in a container.** `docker exec pa-sbx-<user_id> python -m bridge.browser_check` starts
the server the way an engine does, opens a page, checks the refusals, the proxy, the close and
the meter, and prints one line per check (`--no-network` leaves out the two checks that need the
network). The image build runs it with `--no-network`. Run it while no turn uses the browser.

**Not in this version:** signing in to sites (Scout is told never to type a password or a code,
and to report that a sign-in is needed); two turns using the browser at the same time (one
profile, one browser: the second gets an error from the server); screenshots; file downloads and
uploads.

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

(dst lines skipped when the list is empty). For a container with Scout's browser the file is:

```
acl u_<hash>_src src <container_ip>/32
acl u_<hash>_int_name dstdom_regex -n -i ^[^.]+$ (^|\.)(internal|local|localhost|lan|home\.arpa)$
acl u_<hash>_int_ip dst 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.0.0.0/24 192.168.0.0/16 198.18.0.0/15 224.0.0.0/4 240.0.0.0/4 168.63.129.16/32 ::/128 ::1/128 64:ff9b::/96 fc00::/7 fe80::/10 ff00::/8
http_access deny u_<hash>_src u_<hash>_int_name
http_access deny u_<hash>_src u_<hash>_int_ip
acl u_<hash>_dst dstdomain .example.com other.org
http_access allow u_<hash>_src u_<hash>_dst
acl u_<hash>_web port 80 443
http_access allow u_<hash>_src u_<hash>_web
```

Squid takes the first `http_access` line that matches. The base config has already refused
other ports than 80, 443, 4000 and 8080 and allowed what every container may reach (LiteLLM,
the api's `/mcp` and `/git/` paths), so those still work. Then, for this container's address
only: names without a dot (the platform's service names, `localhost`) and private-use endings
are refused; so is every destination whose address, after Squid resolved the name, is private,
loopback, link-local (the cloud metadata address `169.254.169.254` is in it), shared (CGNAT),
multicast or reserved, or the IPv6 counterpart, which covers a public name that points inward;
then the user's own domains are allowed, and last any other destination on ports 80 and 443.
Every line names the container's source address, so no other container gains anything, and a
container without the browser keeps exactly the file above. The rule is written for the
container that was started (label `pa.browser`), so it opens and closes together with the
browser itself, at a wake.

The vm-manager then sends `SIGHUP` to the
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
Claude Code's own process. A container with Scout's browser has a limit of 2048 MB and can
take the room of three others (see "Scout's browser"). `AWAKE_CAP` is 3 there for the first
test; re-measure during a Claude Code turn, and with a browser open, before raising it:

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
`--agents` value, the sub-agent events, the hook that keeps the lead off the web tools, Hermes's
background delegation and the follow-up turn); see `docs/build-report.md` section 8.
For Scout's browser: the image has not been built with it, and no engine has driven it. Run on
the development machine instead, with the real Playwright MCP 0.0.83 and Chrome for Testing 155:
the Bridge's settings and command, the guard, the meter and the self-check, in a box with a
read-only root and only `/data` writable, and under gVisor itself (`runsc` release-20260921.0,
rootless `runsc do`), through a real Squid 7.7 loaded with the generated rule. Not run: the
image build on Debian with Node 24, a container with its real limits and read-only root under
Docker and gVisor, the server started by Claude Code and by Hermes, Scout actually using the
tools, the rule in the backend's Squid, memory in practice; see `docs/build-report.md` section 9.
(Part of this list has since run on the server, 2026-10-05: see that section's "First run on the
server".) The changes of 2026-10-08 (one memory, Scout's `read_page` line per engine, Hermes fetching
nothing, the browser's switches) are tested against fakes, the pinned browser and the Hermes source,
not yet in an image on the server; the checks to run are in that section's "Not verified yet". So is the
second round of 2026-10-08 (a failed turn never continued, failed runs logged, refusals not a platform fault,
the reaper's wait, Hermes's context size), also run with the real 2.1.283 binary against a fake API; the
checks are in `docs/build-report.md` section 10.
