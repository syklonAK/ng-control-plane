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
                             │
                             ▼
                     nginx -t on the live tree
                             │  FAIL → restore backup → report
                             ▼  PASS
                     systemctl reload nginx
                             │  FAIL → restore backup → report
                             ▼
                     record state (last-deploy.json)
```

Snapshots are named `NNNNNN-<timestamp>`. Sequence numbers — not wall-clock —
determine ordering, because clocks can jump backwards under NTP.

## Generated files

All under `/etc/nginx/pg-router/` (application-owned):

| File | Contents |
|---|---|
| `maps.conf` | The **single** shared `$connection_upgrade` map + stream SNI/ALPN maps |
| `upstreams.conf` | One upstream per route; failover members as `backup` servers; a blackhole upstream for `unknown_policy: reject` |
| `http.conf` | One `server` per host group, one `location` per route |
| `stream.conf` | One `server` per stream listener with `ssl_preread on` and map-driven `proxy_pass` |
| `state/last-deploy.json` | Deployment record |
| `backups/NNNNNN-.../` | Rollback snapshots |

The single shared map is deliberate: the original shell script wrote one map
block per site file, so enabling a second domain produced
`duplicate "$connection_upgrade" variable` and broke nginx entirely.

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
