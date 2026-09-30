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

The installer never depends on the system `python3` version: it picks any
interpreter ≥ 3.10 already present and, when none exists, provisions one
itself, adapting to the distro and release it detects:

| System | How a modern python is obtained |
|---|---|
| Ubuntu 18.04+ (incl. EOL releases) | deadsnakes PPA — apt sources still pointing at the retired mirrors are re-pointed to `old-releases.ubuntu.com` first |
| Debian 10+ | distro package, then a source build on releases whose archives stop below 3.10 (Debian 10/11) |
| RHEL / Rocky / Alma / Fedora | `dnf`/`yum` package, then a source build (with a private OpenSSL 1.1.1 on releases that ship 1.0.2) |
| Alpine | `apk` |
| anything else | every provisioner in turn, source build included |

Requirements are only `root` and `git`.

The same command provisions nginx when it is missing. It detects what is
already loaded (`nginx -t` probes, never a config edit), then installs only the
gaps — nginx itself plus the modules this project needs:

```text
nginx not found        -> nginx + stream/ssl/http modules for the distro
nginx present, modules missing -> just the missing module packages
nginx fully provisioned -> nothing is changed (idempotent, safe to re-run)
```

It never overwrites an existing nginx configuration, only creates its own
fragment directory. If nginx cannot be installed automatically (a locked-down
host, a build outside `PATH`, or nginx living on another machine) the installer
says so and still finishes — retry with `sudo pg-router install` later, or pass
`PG_ROUTER_SKIP_NGINX=1` from the start when nginx is managed elsewhere.

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
sudo pg-router update                          # track the branch tip
sudo PG_ROUTER_REF=v1.2.0 pg-router update     # pin to an exact tag/commit
```

This pulls the latest revision from git (skipping the work when already
up to date), reinstalls dependencies, re-links the CLI, and re-validates the
existing configuration against the new version. It never touches generated
nginx fragments, snapshots, or the live config — re-deploy explicitly with
`pg-router apply`.

Set `PG_ROUTER_REF` (a tag like `v1.2.0` or a full commit sha) to install
exactly that revision instead of the live branch tip. The updater refuses to
proceed when the ref cannot be resolved, and verifies the working tree is at
the intended commit before installing — a host never silently moves to
unreviewed code. The revision it replaced is recorded in
`/opt/pg-router/.previous-version`.

## Uninstall

```bash
sudo pg-router uninstall                 # removes CLI + virtualenv, keeps config
sudo pg-router uninstall --yes --purge   # unattended, also removes config + fragments
```

The uninstaller deletes the CLI symlink and the installation directory. It asks
for confirmation on every step (or never, with `--yes`) and never removes nginx
itself, the configuration file, or generated fragments unless `--purge` is
given. The include lines in `/etc/nginx/nginx.conf` are left in place so a live
server keeps serving while you decide.

## Interactive menu

Running `pg-router` with no subcommand on a real terminal opens a menu:

```text
$ pg-router -c /etc/pg-router/config.yaml

pg-router 1.0.0 — interactive menu
config: /etc/pg-router/config.yaml

  1) Build a configuration (guided wizard)
  2) Configuration files — show / edit / copy / delete
  3) Status — nginx, objects, last deploy
  4) Validate configuration
  5) Generate fragments (dry run)
  6) Apply — validate, generate, test, deploy
  7) Preview changes before applying (apply --dry-run)
  8) Routes — list / simulate
  9) Backends, tunnels, health
  r) Rollback to a previous snapshot
  n) Nginx operations
 *u) Update pg-router from git
  0) Quit
```

Entries marked `*` ask for confirmation. Entries with submenus (wizard,
configuration files, routes, backends, nginx) descend a level and `0` comes
back. The menu only builds an argument list and hands it to the same code path
the scripted commands use, so nothing the menu can do is impossible from a
shell — and scripted use is unaffected (no terminal ⇒ usage is printed and the
exit code is `2`).

## Configuration file management

The wizard *creates* a configuration; `config` looks after the file
afterwards — without hand-editing YAML blind or remembering long paths:

```bash
pg-router config list                     # known files, which one is active
pg-router config show                     # print the active configuration
pg-router config show /path/to/other.yaml # print a specific one
pg-router config edit                     # open it in $EDITOR, then validate
pg-router config edit --no-validate       # edit without refusing a bad result
pg-router config copy /etc/pg-router/prod.yaml /backup/prod.yaml
pg-router config use /etc/pg-router/prod.yaml   # remember it, skip -c from now on
pg-router config forget                   # stop remembering
pg-router config delete /etc/pg-router/old.yaml --yes
```

`use` makes `-c` optional: once a file is selected, plain `pg-router status`,
`validate` or `apply` all operate on it (override per command with `-c`, or
globally with `PG_ROUTER_CONFIG`).

Every mutating operation is safe by default:

- `edit` keeps a `.bak` copy before the editor runs and validates the result
  afterwards; an invalid file leaves the command failing loudly (exit `1`) and
  the previous content recoverable in the backup.
- `copy` is byte-exact, so comments and hand formatting survive, and refuses to
  overwrite an existing file.
- `delete` requires `--yes` and still writes a `.bak` backup.
- `use` refuses to select a file that does not validate.

The same operations are reachable from the interactive menu: **Configuration
files**.

## Configuration wizard

If you don't want to hand-write YAML, the wizard walks through one section at a
time with validated defaults:

```bash
pg-router wizard --preset entry    # edge server: listener → route → backend
pg-router wizard --preset exit     # backend server: tunnel → local backend
pg-router wizard --preset custom --sections listener backend route
```

Each section asks only for what it needs and refuses values the schema would
reject (bad ports, unknown transports). It works on an existing file too — run
it again to add another listener or backend, and everything you already wrote
stays untouched. The result is an ordinary configuration file, so `validate`
and `apply` work on it unchanged.

The same wizard is reachable from the interactive menu: **Build a
configuration**.

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
* **One deploy at a time** — `apply` and `rollback` take a process-wide lock
  on the managed directory, so two concurrent operations (a menu session and a
  cron job, say) cannot interleave swaps and corrupt the state. The lock is
  OS-level: it is released automatically if the holder dies, so a crashed
  deploy never wedges the tool.
* **Automatic rollback** — if `nginx -t` or the reload fails after the swap, the
  pre-deployment snapshot is restored automatically. Nginx keeps serving the
  previous in-memory configuration until the reload succeeds, so a broken
  configuration never reaches production traffic.
* **Preview before applying** — `apply --dry-run` prints a unified diff of the
  live fragments vs. the new ones and names what would change, without writing
  anything. `pg-router status` shows the last deploy record and whether a
  deploy is in flight.
* **Idempotent** — repeated `install`/`apply` with the same config yields the
  same effective state; no duplicated directives or growing files.
* **Pinned updates** — `PG_ROUTER_REF` makes an update land on an exact tag or
  commit instead of the live branch tip, and the updater verifies the tree is
  at that commit before installing.
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
python -m pytest            # 284 tests: schema, matchers, validators, loops,
                            # http/stream generation, tunnels, deploy/rollback,
                            # deploy lock, diff preview, ref pinning,
                            # health/failover, installer, CLI, interactive menu,
                            # guided wizard, update/uninstall, end-to-end
                            # (plus installer shell scenarios run under bash)
```

## Environment variables

| Variable | Purpose |
|---|---|
| `PG_ROUTER_CONFIG` | Configuration file path |
| `PG_ROUTER_DATA_DIR` | Managed fragment directory (default `/etc/nginx/pg-router`) |
| `PG_ROUTER_LOG_LEVEL` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `PG_ROUTER_MENU` | Force the interactive menu on even when stdin is not a terminal |
| `PG_ROUTER_STATE_DIR` | Where `config use` remembers the active configuration (default `~/.pg-router`) |
| `PG_ROUTER_API_BIND` | Reserved for the future API server |

Secrets never need to live in YAML: `${VAR}` and `${VAR:-default}` placeholders
are expanded from the environment before parsing.
