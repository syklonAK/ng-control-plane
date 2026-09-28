# Configuration reference

YAML is the primary format (JSON works too). Everything below is configurable;
**nothing in the toolchain is hard-coded** — ports, domains, SNIs, paths,
transports, providers and topology are all data.

## Top level

```yaml
version: 1
node:        {id, roles, address, region, labels}
nodes:       []          # optional fleet declaration (enables node-ref validation)
defaults:    {timeouts, tls, proxy_headers, health_check, ...}
certificates: [...]
listeners:   [...]
backends:    [...]
tunnels:     [...]
chains:      [...]
routes:      [...]
pasarguard:  {inbounds: [...]}
```

## Node

```yaml
node:
  id: edge-01
  roles: [edge, relay]   # edge | relay | hybrid | backend | gateway (multiple allowed)
```

Roles are logical labels; behaviour is derived from the listeners and routes
you configure, not from the role string.

## Listeners

```yaml
listeners:
  - id: public-https
    address: 0.0.0.0          # IPv4, IPv6 ("::"), localhost, or any interface IP
    port: 443                 # any port
    mode: http                # http | stream
    protocol: tcp             # tcp | udp
    default_server: false
    unknown_policy: reject    # stream only: reject | default
    listen:                   # extra binds for this listener
      - {address: 127.0.0.1, port: 9443}
    tls:
      mode: terminate         # terminate | passthrough | disabled
      certificate: wildcard   # certificate id (terminate only)
      protocols: TLSv1.2 TLSv1.3
      verify_upstream: false
```

Rules the validator enforces:

* `passthrough` requires `mode: stream` (it needs `ssl_preread`).
* `terminate` on `mode: stream` is not generated — use an `http` listener.
* `terminate` requires a resolvable certificate.

## Matchers

Single condition:

```yaml
match: {type: path_prefix, value: /nl}
match: {type: sni, values: [nl.example.com, nl2.example.com]}
```

Compound (`all` / `any` / `not`, nestable):

```yaml
match:
  all:
    - {type: host, value: example.com}
    - {type: path_prefix, value: /nl}
    - not: {type: source_cidr, value: 10.0.0.0/8}
```

| Type | Value | Notes |
|---|---|---|
| `path` | `/nl` | exact path |
| `path_prefix` | `/nl` | component-wise prefix (`/nl`, `/nl/10000`, not `/nlonline`) |
| `path_regex` | `^/n\\d+/` | PCRE |
| `host` | `example.com` or `*.example.com` | wildcard subdomain supported |
| `host_regex` | `^.*\\.example\\.com$` | |
| `sni` | hostname | stream listeners only |
| `alpn` | `h2` / `http/1.1` / `h3` / `grpc` | stream only |
| `port` / `destination_port` | integer | |
| `protocol` | `tcp` / `udp` / `tls` | |
| `transport` | a transport type | |
| `source_ip` / `source_cidr` | IP / CIDR | |

Matcher values are validated on load — a malformed hostname, CIDR or regex is
rejected before anything is generated. Path matchers cannot be used on stream
listeners; SNI/ALPN matchers cannot be used on HTTP listeners.

## Transports

```yaml
transport:
  type: ws                    # ws | httpupgrade | xhttp | splithttp | http | http2 | grpc | tcp | tls
  path: /nl                   # path hint (http-layer transports)
  service: my.Service         # grpc only
  headers: {X-Tag: nl}        # extra proxy_set_header values
  read_timeout: 1w            # inherits from defaults when unset
  send_timeout: 1w
  connect_timeout: 10s
  buffer_enabled: false       # streaming transports default to false
  upstream_tls: false         # grpc/https backends over TLS
  upstream_sni: pg.internal
```

`ws`/`httpupgrade` emit Upgrade/Connection headers; `xhttp`/`splithttp` disable
buffering and enable chunked transfer; `grpc` emits `grpc_pass` with HTTP/2.

## Backends

```yaml
backends:
  - {id: pg-nl, type: local, host: 127.0.0.1, port: 62050}
  - {id: pg-remote, type: remote, host: 10.10.0.20, port: 62050}
  - {id: pg-tun, type: tunnel, tunnel: nl-reverse}
  - {id: pg-sock, type: unix_socket, socket: /tmp/pg.sock}
  - id: pg-resilient
    type: failover
    primary: pg-tun
    backups: [pg-remote, pg-nl]   # emitted as nginx `backup` servers
    max_fails: 3
    fail_timeout: 10s
```

PasarGuard is one kind of backend consumer, not an assumption — `pasarguard:
inbounds` entries simply resolve to backend targets.

## Tunnels

```yaml
tunnels:
  # Remote node dials in; Nginx reaches the tunnel through a local listener.
  - id: nl-reverse
    mode: reverse
    provider: custom          # direct | reverse | gost | ssh | wireguard | tcp-relay | unix-socket | custom
    listener: {address: 127.0.0.1, port: 41001}
    remote: {node: nl-node}   # logical far-side node
    target: {host: 127.0.0.1, port: 62050}

  # This node reaches the remote endpoint directly.
  - id: fr-direct
    mode: direct
    provider: custom
    remote: {node: fr-node, host: 10.0.0.20, port: 40001}
    target: {host: 127.0.0.1, port: 62051}

  # Out-of-band tunnel described entirely by options.
  - id: mesh
    mode: direct
    provider: custom
    remote: {host: 10.0.0.20, port: 40001}
    options: {endpoint_host: 10.144.144.1, endpoint_port: 41010}
```

`reverse` resolves to its local listener; `direct` resolves to its remote
endpoint. That is the only place tunnel direction matters for routing — the
engine never knows or cares which software implements the tunnel.

## Multi-hop chains

```yaml
chains:
  - id: nl-chain
    chain:
      - {type: listener, id: public-https}
      - {type: route, id: nl-route}
      - {type: tunnel, id: edge-to-relay-b}
      - {type: tunnel, id: relay-b-to-relay-c}
      - {type: backend, id: central-pg}
```

Hops must be unique within a chain, and tunnels in a multi-hop chain must be
contiguous. The validator also rejects tunnel loops (a tunnel whose target
lands on another tunnel's ingress and back again).

## Routes

```yaml
routes:
  - id: nl-ws
    listener: public-https
    transport: {type: ws}
    match:
      all:
        - {type: host, value: example.com}
        - {type: path_prefix, value: /nl}
    backend: pg-nl
    chain: nl-chain           # optional multi-hop
    fallback: nl-backup       # optional fallback route
    health_check:
      enabled: true
      type: tcp
      interval: 10s
      timeout: 3s
      fall: 3
      rise: 2
      failover: true          # required for health state to move traffic
    enabled: true
    priority: 0
```

## Certificates

```yaml
certificates:
  - id: wildcard
    provider: existing                      # files already on disk (default)
    chain: /etc/letsencrypt/live/example.com/fullchain.pem
    key: /etc/letsencrypt/live/example.com/privkey.pem

  - id: acme-new
    provider: acme                          # issued via certbot at deploy time
    domains: [api.example.com]
    email: ops@example.com

  - id: hook
    provider: custom                        # material produced out-of-band
    chain: /opt/certs/fullchain.pem
    key: /opt/certs/privkey.pem
```

Paths are validated syntactically on load; existence is enforced by `nginx -t`
at deploy time, so a config can be written before the certificate exists.

## Defaults and inheritance

```yaml
defaults:
  connect_timeout: 10s
  read_timeout: 1w
  send_timeout: 1w
  buffer_enabled: true
  client_max_body_size: 0
  tls_protocols: TLSv1.2 TLSv1.3
  proxy_headers:
    X-Pool: edge-01
  health_check:
    enabled: true
    interval: 10s
    timeout: 3s
```

Timeouts and buffering are `None` at the transport level until merged with
`defaults`, so a route inherits by omission and overrides explicitly.

## Environment substitution

```yaml
backends:
  - {id: pg-nl, type: remote, host: ${PG_NL_HOST}, port: ${PG_NL_PORT:-62050}}
```

`${VAR}` fails loudly when unset; `${VAR:-default}` falls back. Substitution
happens before schema parsing, so validated values are never undermined.

## Generated fragment example

For `examples/edge-mixed.yaml`:

```nginx
# maps.conf
map $http_upgrade $connection_upgrade { default upgrade; '' close; }
map $ssl_preread_server_name $pg_sni_public_stream {
    default pg_blackhole;
    fr.example.com pg_fr_tcp;
}

# http.conf
server {
    listen 0.0.0.0:443 ssl;
    server_name example.com;
    ssl_certificate /etc/letsencrypt/live/example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/example.com/privkey.pem;
    location /nl {
        proxy_pass http://pg_nl_ws;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        ...
    }
}

# stream.conf
server {
    listen 0.0.0.0:8443;
    ssl_preread on;
    proxy_pass $pg_sni_public_stream;
}
```
