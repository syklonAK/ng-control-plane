# pg-router

A generic, configuration-driven **control plane** for Nginx routing in front of
PasarGuard (and any other HTTP/TCP backend).

Python configures, validates, monitors and orchestrates. **Nginx handles all
production traffic.** The tool never proxies traffic itself.

## Why

The previous `ng-nginx.sh` hard-coded one topology: fixed ports, fixed domains,
one tunnel direction, and one `map` block per site file (which breaks nginx the
moment a second domain is configured). `pg-router` replaces that with a
declarative model where the topology is data:

```text
Listener → Route → Matcher → Backend → Tunnel → Remote Target → PasarGuard Inbound
```

Changing from a reverse tunnel to a direct tunnel, from `:443` to `:8443`, from
`/nl` to `/fr`, or from SNI routing to path routing requires **configuration
changes only — never source changes**.

## Install

One line (installs into `/opt/pg-router`, creates an isolated venv, links the CLI):

```bash
curl -fsSL https://raw.githubusercontent.com/syklonAK/ng-control-plane/main/install.sh | sudo bash
```

Or from a checkout:

```bash
git clone https://github.com/syklonAK/ng-control-plane.git
cd ng-control-plane
pip install .
sudo pg-router install     # installs nginx + stream/ssl_preread modules
```

`install` is idempotent: it detects the OS, distro, architecture and existing
Nginx build, installs only what is missing, and probes each module
functionally with `nginx -t` before trusting it.

## Update

```bash
sudo pg-router update
```

This pulls the latest revision from git (skipping the work when already
up to date), reinstalls dependencies, re-links the CLI, and re-validates the
existing configuration against the new version. It never touches generated
nginx fragments, snapshots, or the live config — re-deploy explicitly with
`pg-router apply`.

## Interactive menu

Running `pg-router` with no subcommand on a real terminal opens a menu:

```text
$ pg-router -c /etc/pg-router/config.yaml

pg-router 1.0.0 — interactive menu
config: /etc/pg-router/config.yaml

  1) Status — nginx, objects, snapshots
  2) Validate configuration
  3) Generate fragments (dry run)
  4) Generate fragments to a directory
 *5) Apply — validate, generate, test, deploy
 *6) Rollback to a previous snapshot
  7) Routes — list
  8) Routes — simulate a request
  9) Backends — list
  t) Tunnels
  n) Nginx
  h) Health checks and failover
  i) Initialise a starter configuration
 *u) Update pg-router from git
  0) Quit
```

Entries marked `*` ask for confirmation. The menu only builds an argument list
and hands it to the same code path the scripted commands use, so nothing the
menu can do is impossible from a shell — and scripted use is unaffected (no
terminal ⇒ usage is printed and the exit code is `2`).

## Quick start

```bash
pg-router init                       # write a starter configuration
$EDITOR pg-router.yaml
pg-router validate                   # schema + references + loop detection
pg-router generate --dry-run         # fragments + syntax check, nothing deployed
pg-router apply                      # atomic deploy: backup → test → swap → reload
pg-router status                     # nginx, objects, snapshots
pg-router rollback                   # restore previous known-good configuration
```

Or step through the same workflow from the interactive menu (see below).

## Object model

| Object | Meaning |
|---|---|
| `node` | This server's identity and logical roles (`edge`, `relay`, `hybrid`, `backend`, `gateway`) — labels, not code paths |
| `listener` | A bind address/port/mode (`http` or `stream`); as many as needed, any port |
| `route` | Binds a listener + matcher + transport + backend (+ optional chain, fallback, health policy) |
| `matcher` | `path`, `path_prefix`, `path_regex`, `host`, `host_regex`, `sni`, `alpn`, `port`, `protocol`, `transport`, `source_ip`, `source_cidr`; composable with `all` / `any` / `not` |
| `backend` | `local`, `remote`/`tcp`, `tunnel`, `unix_socket`, `custom`, or `failover` (primary + backups) |
| `tunnel` | `reverse` (remote dials in) or `direct` (we reach out), behind a provider registry |
| `chain` | Ordered multi-hop path: listener → route → tunnel → tunnel → backend |
| `certificate` | `existing` files, `acme` (Let's Encrypt) or `custom` |

Everything above is combined freely — see [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md).

## How routing is generated

The transport selects the Nginx mechanism, never the other way around:

* HTTP-aware transports (`ws`, `httpupgrade`, `xhttp`, `splithttp`, `http`,
  `http2`, `grpc`) → `http/server/location` + `proxy_pass` / `grpc_pass`
* Raw transports (`tcp`, `tls`) → `stream/server` + `ssl_preread` + SNI/ALPN maps

A route can never get an HTTP `location` for traffic that only exists as an
opaque TLS stream, and never the reverse.

## Safety properties

* **Atomic deployment** — fragments are staged and syntax-tested before any
  live file changes; live files are swapped with `os.replace` (atomic rename).
* **Automatic rollback** — if `nginx -t` or the reload fails after the swap, the
  pre-deployment snapshot is restored automatically. Nginx keeps serving the
  previous in-memory configuration until the reload succeeds, so a broken
  configuration never reaches production traffic.
* **Idempotent** — repeated `install`/`apply` with the same config yields the
  same effective state; no duplicated directives or growing files.
* **No shell injection** — every subprocess call uses an argument list with
  `shell=False`; every value that reaches a directive is validated (ports,
  IPs, hostnames, paths, header names/values) and `;`, `{`, `}` are rejected in
  header values.
* **Ownership** — only `/etc/nginx/pg-router/*` is rewritten. `nginx.conf` is
  never overwritten: a backup-guarded, `nginx -t`-verified include line is the
  only change, and it is skipped when already present.

## Architecture

```text
CLI  →  Service Layer  →  Config / Router / Tunnel / Nginx / Health services
                              ↓
                    Nginx fragments (control plane)
                              ↓
                           Nginx  ← production traffic
```

The CLI contains no business logic, so a future REST API, Telegram bot or web UI
calls the same service layer.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Examples

* [`examples/single-node.yaml`](examples/single-node.yaml) — one listener, one
  local backend (no tunnels)
* [`examples/edge-mixed.yaml`](examples/edge-mixed.yaml) — WS path routing +
  stream SNI routing on one edge
* [`examples/multi-hop-chain.yaml`](examples/multi-hop-chain.yaml) —
  edge → relay → relay → PasarGuard chain
* [`examples/failover-one-to-many.yaml`](examples/failover-one-to-many.yaml) —
  one-to-many tunnels plus a failover group

## Tests

```bash
python -m pytest            # 153 tests: schema, matchers, validators, loops,
                            # http/stream generation, tunnels, deploy/rollback,
                            # health/failover, installer, CLI, interactive menu,
                            # end-to-end
```

## Environment variables

| Variable | Purpose |
|---|---|
| `PG_ROUTER_CONFIG` | Configuration file path |
| `PG_ROUTER_DATA_DIR` | Managed fragment directory (default `/etc/nginx/pg-router`) |
| `PG_ROUTER_LOG_LEVEL` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `PG_ROUTER_MENU` | Force the interactive menu on even when stdin is not a terminal |
| `PG_ROUTER_API_BIND` | Reserved for the future API server |

Secrets never need to live in YAML: `${VAR}` and `${VAR:-default}` placeholders
are expanded from the environment before parsing.
