# personal-agent vm-manager

The only service that controls Docker. It creates, wakes, stops and deletes each
user's sandboxed agent container (Hermes Agent + Bridge), delivers messages into
it, records awake time for billing, and writes each container's egress allowlist.
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
`PLATFORM_MCP_URL` (http://api:8080/mcp). Tunables: `AWAKE_CAP`, `IDLE_TIMEOUT`,
`SANDBOX_MEMORY_MB`, `SANDBOX_CPUS`, `SANDBOX_PIDS_LIMIT`, `DELIVER_TIMEOUT`, `AGENT_IMAGE`,
`AGENT_IMAGE_VERSION`, ...

Deployment: mount the Docker socket (only here), the allowlist dir shared with the
egress proxy, and a backup dir at `SNAPSHOT_ROOT`. Put it on the platform and sandbox
networks; never publish port 9090.

## gRPC

Every call needs metadata `x-internal-secret: <VM_INTERNAL_SECRET>` (else
`UNAUTHENTICATED`). Errors: `NOT_FOUND` (no sandbox), `INVALID_ARGUMENT`,
`RESOURCE_EXHAUSTED` (awake cap reached, nothing evictable: retry later),
`FAILED_PRECONDITION` (polite stop during a turn), `UNAVAILABLE` (container would not
start; marked broken). User ids must match `[A-Za-z0-9][A-Za-z0-9_-]{0,127}`.

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

## Lifecycle

States: `stopped -> waking -> awake <-> busy`, and `broken`.

- **Create**: volume `pa-data-<user>` + container `pa-sbx-<user>` (stopped) + secrets. Idempotent.
- **Wake**: capacity guard, re-create if the image version changed or the container is
  missing (keeps /data), start (retry once, then re-create), wait for `GET /healthz`,
  write the egress file, open an awake period.
- **Deliver**: wake if needed, `POST /deliver`, relay events; `busy` on `accepted`,
  back to `awake` + last-active on `turn_done` / `error`. Timeout (15 min or
  `timeout_seconds`) -> force stop + `ERROR` with `platform_fault=true`. The turn is
  tracked to the end even if the gRPC caller disconnects.
- **Stop**: polite = `POST /shutdown` (Hermes saves to /data, up to 20 s) then `docker
  stop`; force = kill. Removes the egress file, closes the awake period.
- **Delete**: container, volume, egress file, row. Awake periods are kept for billing.

Loops: idle reaper (1 min; not busy, nothing in flight, Bridge reports no turn, idle >=
3 min), health checker (30 s; 2 failed checks -> restart; more than 3 restarts without a
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
| `GET /healthz` | `200 {"ok":true,"busy":N}` when Hermes answers, else `503`. `busy` = turns in progress |
| `POST /deliver` | Body `{"delivery_id","kind","session_key","channel","text","attachments":[{"id","name","mime_type","url"}],"approval_id","approved","metadata":{}}`, `kind` = `message` / `approval_answer` / `automation` / `app_event`. `200` with `Content-Type: application/x-ndjson`, one line per event: `{"type":"accepted\|working\|turn_done\|error","message":"...","platform_fault":bool}`, ending with `turn_done` or `error`. `401` bad secret, `400` bad body |
| `POST /shutdown` | Stop taking work, stop Hermes (it saves to /data), `200` when done (< 20 s) |

Dedupe by `delivery_id`: a repeat of a finished delivery returns `accepted` +
`turn_done` without running again; a repeat while it runs attaches to the same turn; a
retry after a failed send only re-sends the saved answer. The record lives in
`/data/.platform/deliveries.json`, so it survives restarts. Failed runs are re-run on retry.

The Bridge never returns reply text. It sends the final answer itself by calling the
Platform MCP tool `send_message` `{"text","session_key"}` with the container's bearer
token. Hermes sees the other Platform MCP tools but not `send_message`.

## Agent image

`agent-image/`: Hermes Agent (Nous Research, pinned commit) + the Bridge. The Bridge
renders `$HERMES_HOME/config.yaml` from `platform.json` at every boot (model = LiteLLM
with the user's key via the egress proxy; MCP = Platform MCP with the container token
+ the user's MCP servers; cron and messaging toolsets off; memory/skills/sessions under
`/data/hermes`), starts `hermes gateway run` with only its API server on 127.0.0.1
(random per-boot key), and drives one Hermes run per delivery with
`X-Hermes-Session-Key: <session_key>`, so each chat keeps its own session. Hermes's own
approval prompts are denied by policy (`BRIDGE_HERMES_APPROVAL`); risky real-world
actions go through the platform's `request_approval` tool.

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
non-empty. Domains are validated (hostnames with at least two labels, no IPs;
`*.x.com` / `.x.com` = x.com and subdomains; max 200) so user input can't inject
Squid config.

## Measured RAM per container

**Not measured yet.** Docker and gVisor are not available on the development machine,
so no container has run. Measure one awake container after a real turn:

```bash
docker stats --no-stream pa-sbx-<user_id>
```

(or run `TestCreateWakeDeliverHello` with `-tags=integration`; it logs the number).
Write the result here and set `AWAKE_CAP` from it (server RAM for sandboxes / RAM per container).

## Not verified yet

Unit tests use a fake Docker daemon and a fake Bridge. Not yet run for real: the
Docker SDK calls against a daemon, gVisor, `docker cp` into a read-only-rootfs
container's volume, the Squid reload, the agent image build, and Hermes itself
(config keys, `gateway run`, the Runs API) inside the read-only container.
