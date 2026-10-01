# Architecture

## Layering

```text
┌──────────────────────────────────────────────────────────────┐
│ CLI (argparse)          REST API / bot / UI (future)         │
└───────────────────────────────┬──────────────────────────────┘
                                │  thin adapters, no business logic
┌───────────────────────────────▼──────────────────────────────┐
│ Service layer                                                │
│  ConfigService · RouterService · TunnelService               │
│  NginxService · HealthService · DeployService                │
└───────┬───────────────┬──────────────┬───────────────┬────────┘
        │               │              │               │
┌───────▼──────┐ ┌──────▼───────┐ ┌────▼────────┐ ┌────▼─────────┐
│ Config       │ │ Model        │ │ Nginx       │ │ Deploy       │
│ schema       │ │ matchers     │ │ modules     │ │ deployer     │
│ loader       │ │ topology     │ │ installer   │ │ backup       │
│ validator    │ │ resolver     │ │ manager     │ │ rollback     │
│              │ │              │ │ generator   │ │ health       │
└──────────────┘ └──────────────┘ └─────────────┘ └──────────────┘
        │                                              │
        └────► Nginx fragments (data plane) ◄──────────┘
```

Python is a **control plane only**: it configures, validates, monitors and
orchestrates. Nginx carries production traffic.

## Extensibility

Variable parts are adapters behind registries in `pg_router/plugins`:

| Extension point | Interface | Built-ins |
|---|---|---|
| `TunnelProvider` | where Nginx reaches a tunnel | `direct`, `reverse`, `gost`, `ssh`, `wireguard`, `tcp-relay`, `unix-socket`, `custom` |
| `CertificateProvider` | resolve chain/key material | `existing`, `acme`, `custom` |
| Health probes | how a target is probed | `tcp`, `http` |

Adding a provider is `registry.register(MyProvider())` — the routing core never
changes. Providers describe *how to reach* an endpoint; they never run tunnel
software or proxy traffic.

## Reference resolution and loops

`RouterConfig.build_index()` builds `kind:id → object` maps for O(1) lookup
(no O(n²) scans). The validator then:

1. resolves every reference (`route.listener`, `backend.tunnel`,
   `failover.primary/backups`, `chain` hops, `certificate`, `node`),
2. checks transport-layer/listener-mode compatibility and TLS modes,
3. rejects ambiguous locations and duplicate SNI values,
4. runs loop detection:
   * repeated chain hops,
   * failover cycles,
   * fallback cycles,
   * tunnel egress→ingress cycles (a tunnel whose target lands on another
     tunnel's ingress, and back),
   * unresolvable-backend cycles.

Cycle detection uses iterative DFS with WHITE/GRAY/BLACK colouring so deep
graphs cannot blow the stack.

## Deployment pipeline

```text
load → validate → resolve → generate
   │                    (read-only: outside the deploy lock)
   ▼
acquire deploy lock ──► busy? → refuse with the holder's name
   │
   ▼
required-module check
   │
   ▼
backup current fragments   ──► snapshot (sequence ordered)
   │
   ▼
stage fragments + nginx -t (isolated prefix)
   │  FAIL → restore backup → report
   ▼  PASS
atomic swap (temp file + os.replace)
   │  stale fragments not in the new set are removed
   ▼
nginx -t on the live tree
   │  FAIL → restore backup → report
   ▼  PASS
systemctl reload nginx
   │  FAIL → restore backup → report
   ▼
record state (last-deploy.json) → release lock
```

Snapshots are named `NNNNNN-<timestamp>`. Sequence numbers — not wall-clock —
determine ordering, because clocks can jump backwards under NTP.

The deploy lock is a file lock in `state/deploy.lock` plus an in-process
registry: OS advisory locks are per-process, so the registry is what makes two
`DeployLock` instances in one interpreter exclude each other. The lock file
records `pid=<pid> op=<apply|rollback> at=<time>` so a blocked operator can see
what is running; it is released on process exit, so a crash never wedges the
tool.

`apply --dry-run` runs the same generate + staging test, then prints a unified
diff of the live fragments against the new ones and exits without writing. The
diff is line-based and dependency-free, and identical input yields identical
output, so "no diff" is trustworthy as "no change".

## Generated files

All under `/etc/nginx/pg-router/` (application-owned):

| File | Contents |
|---|---|
| `maps.conf` | The **single** shared `$connection_upgrade` map (HTTP context only) |
| `upstreams.conf` | One upstream per route; failover members as `backup` servers; a blackhole upstream for `unknown_policy: reject` |
| `http.conf` | One `server` per host group, one `location` per route |
| `stream.conf` | One `server` per stream listener with `ssl_preread on` and map-driven `proxy_pass`, plus the SNI/ALPN maps |
| `state/last-deploy.json` | Deployment record (shown by `pg-router status`) |
| `state/deploy.lock` | Advisory lock serializing `apply`/`rollback` |
| `backups/NNNNNN-.../` | Rollback snapshots |

The single shared map is deliberate: the original shell script wrote one map
block per site file, so enabling a second domain produced
`duplicate "$connection_upgrade" variable` and broke nginx entirely.

The SNI/ALPN maps live *inside* `stream.conf`, not in `maps.conf`, because
they read `$ssl_preread_server_name` / `$ssl_preread_alpn_protocols` —
variables that exist only within a `stream {}` block. nginx loads each fragment
in one context, so putting them in the shared file made the staging test
reference stream-only variables from `http {}` and fail with
`unknown "ssl_preread_server_name" variable`.

Correspondingly, `ensure_managed_include` wires both `upstreams.conf` and
`stream.conf` into the `stream {}` block of `/etc/nginx/nginx.conf`: a
stream-only host's `stream.conf` proxies to upstreams defined in
`upstreams.conf`, and without that include nginx resolves none of them.

## Health and failover

`HealthManager` probes backends, tunnel endpoints and inbounds (TCP connect or
HTTP status). Probing is **passive by default**: a failing probe only reports.
It moves traffic only when the backend's health check sets `failover: true`, and
only after `rise`/`fall` consecutive results, so a flapping link cannot churn
the configuration. When a member is marked down, the generator emits `down` in
its upstream and nginx sends traffic to the `backup` members.

## Idempotency

* Generation is a pure function of the configuration (no timestamps, no random
  values), so identical input yields byte-identical output.
* Upstreams are deduplicated by endpoint set: two routes pointing at the same
  targets share one upstream.
* `install` installs only missing packages/modules and creates only missing
  directories; `nginx.conf` includes are added once and skipped thereafter.
* `apply` swaps only fragments whose content changed and removes fragments the
  new configuration no longer needs, so a rollback never leaves a stale
  `stream.conf` referencing deleted upstreams.

## Capability contract

Every matcher type has exactly one authoritative entry in
`model/capabilities.py`: whether it is *enforced* (compiled into nginx config),
*simulation-only* (matched by `pg-router routes test` but not expressible in
nginx), or unsupported. The validator, the generator and the CLI all read that
one table, so a matcher can never be silently accepted and then dropped from
the generated config — that gap was the root cause of the ALPN, compound and
`source_cidr` bugs. An unenforced matcher in a route fails validation with a
message naming the matcher, unless the route opts in explicitly with
`unenforced_matchers: allow` (useful for simulation-only testing).
