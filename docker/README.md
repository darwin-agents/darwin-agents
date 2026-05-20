# Local dev stack

Provider-neutral Docker Compose stack for the Darwin platform — one
command brings up the supervisor, persistence, an LLM gateway, metrics,
and tracing.

## Services

| Service      | Port  | Purpose                                                |
| ------------ | ----- | ------------------------------------------------------ |
| `supervisor` | 8000  | FastAPI Supervisor (Darwin lifecycle, billing, audit). |
| `postgres`   | 5432  | Reserved for a future Postgres persistence backend.    |
| `litellm`    | 4000  | Real LLM gateway (BerriAI LiteLLM proxy, doc 13).      |
| `prometheus` | 9090  | Scrapes `supervisor:8000/metrics` every 15s.           |
| `jaeger`     | 16686 | Trace UI (OpenTelemetry receiver on 4317/4318).        |

## Quickstart

```bash
# 1. (Optional) Seed provider API keys so LiteLLM works.
cp docker/.env.example docker/.env
$EDITOR docker/.env

# 2. (Optional) Enable bearer auth on the supervisor.
cp docker/tokens.example.json docker/tokens.json
# Then uncomment the tokens.json mount + --auth-tokens line in
# docker-compose.yml.

# 3. Start the stack.
docker compose up --build

# 4. Verify.
curl -s http://localhost:8000/healthz | jq
curl -s http://localhost:8000/metrics | head
```

In another terminal:

```bash
# Open the dashboards.
open http://localhost:9090           # Prometheus
open http://localhost:16686          # Jaeger
```

## Spawning a Darwin

```bash
# Auth disabled (default):
curl -X POST http://localhost:8000/darwins \
  -H 'Content-Type: application/json' \
  -d '{"opening_usdc": 80.0}'

# Auth enabled (you uncommented the tokens.json mount):
curl -X POST http://localhost:8000/darwins \
  -H 'Authorization: Bearer owner-dev-token' \
  -H 'Content-Type: application/json' \
  -d '{"opening_usdc": 80.0}'
```

The response carries the new `darwin_id`. From there you can:

```bash
# Status
curl http://localhost:8000/darwins/<id>

# Audit log (operator scope)
curl -H 'Authorization: Bearer ops-dev-token' \
  http://localhost:8000/platform/audit

# Fleet billing
curl -H 'Authorization: Bearer ops-dev-token' \
  http://localhost:8000/platform/billing
```

## Persistence

The supervisor writes a SQLite database at `/data/state.db` inside the
container, backed by the `supervisor-data` volume. Audit log + tokens +
billing + lineage + escalation tickets + monitor alerts survive
restarts.

```bash
# Inspect the live DB.
docker exec -it darwin-supervisor sqlite3 /data/state.db \
  "SELECT seq, ts, actor, action, darwin_id FROM audit_entries ORDER BY seq DESC LIMIT 10;"
```

## Common operations

```bash
# Stop the stack, keep volumes.
docker compose down

# Stop and wipe local state (audit chain, billing, tokens — gone).
docker compose down --volumes

# Rebuild the supervisor image after a code change.
docker compose build supervisor && docker compose up -d supervisor

# Tail the supervisor's logs.
docker compose logs -f supervisor

# Read the platform's signing public key (operators verify against this
# to confirm a manifest came from THIS supervisor).
docker exec darwin-supervisor python -m darwin_platform signing-key \
  --db /data/state.db
```

## What's not in this stack yet

- Real on-chain wallet (`Web3ChainClient`) — the genome runs against
  the fakes by default; swap in `RealWallet` for production.
- Real HashiCorp Vault — the in-memory tiered vault is fine for dev.
- Egress proxy / network policies — doc 08 §"Outbound network filter"
  is K8s-level work, deferred behind the container layer.
- TLS / external auth provider — front the supervisor with an ingress
  (Caddy / nginx / Traefik) for real exposure.

## Security notes

This stack is for **local development**. Do not expose it on a public
network:

- Default supervisor mode is auth-disabled (trusts every caller).
- The LiteLLM master key is `dev_only_master_key`.
- Postgres password is `dev_only_password`.
- The signing key is generated on supervisor startup and persisted to
  `/data/state.db`. For production, use an HSM- or KMS-backed signer.
