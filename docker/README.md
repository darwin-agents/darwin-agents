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

All `docker compose` commands run from this `docker/` directory (that is
where `docker-compose.yml` lives, and Compose auto-loads `./.env` from
there).

```bash
cd docker

# 1. (Optional) Seed provider API keys so LiteLLM works.
#    .env.example lives at the repo root — the single source of truth.
cp ../.env.example .env
$EDITOR .env

# 2. (Optional) Enable bearer auth on the supervisor.
cp tokens.example.json tokens.json
# Then uncomment the tokens.json mount + --auth-tokens lines in
# docker-compose.yml.

# 3. Start the stack (from repo root: make docker-up does steps 1–3).
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

The supervisor supports two backends, both restart-survivable:

- **SQLite (default):** `/data/state.db` inside the container, backed by
  the `supervisor-data` Docker volume.
- **Postgres:** uncomment the `--db=postgresql://...` line in
  `docker-compose.yml` to point the supervisor at the bundled
  `postgres` service. The default credentials are dev-only —
  override via `docker/.env`.

Audit log + tokens + billing + lineage + escalation tickets + monitor
alerts persist in either mode.

```bash
# SQLite: inspect the live DB.
docker exec -it darwin-supervisor sqlite3 /data/state.db \
  "SELECT seq, ts, actor, action, darwin_id FROM audit_entries \
   ORDER BY seq DESC LIMIT 10;"

# Postgres: query through the postgres container.
docker exec -it darwin-postgres psql -U darwin -d darwin -c \
  "SELECT seq, ts, actor, action, darwin_id FROM audit_entries \
   ORDER BY seq DESC LIMIT 10;"
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

## CI / published images

GitHub Actions workflows live under `.github/workflows/`:

| Workflow    | Trigger                                          | What it does                                           |
| ----------- | ------------------------------------------------ | ------------------------------------------------------ |
| `test.yml`  | push/PR to main + `release/**`                   | Fast tier (`pytest -m "not slow"`).                    |
| `slow.yml`  | push to main, weekly cron, manual                | 90-day cross-engine sim (`pytest -m slow`).            |
| `lint.yml`  | push/PR to main + `release/**`                   | `ruff check` + `ruff format --check` (non-blocking).   |
| `build.yml` | push to main, semver tags, manual                | Multi-arch (amd64+arm64) image build + push to GHCR.   |

Pull a published image and run it the same way `docker compose` does:

```bash
# Latest from main
docker pull ghcr.io/<org>/<repo>/darwin-platform:latest

# Pinned to a specific commit (the safe choice for production)
docker pull ghcr.io/<org>/<repo>/darwin-platform:sha-abc1234

# Run with persistence + auth tokens
docker run --rm -d \
  -p 8000:8000 \
  -v $(pwd)/data:/data \
  -v $(pwd)/tokens.json:/run/secrets/tokens.json:ro \
  ghcr.io/<org>/<repo>/darwin-platform:sha-abc1234 \
  python -m darwin_platform serve \
    --host 0.0.0.0 --port 8000 \
    --db /data/state.db \
    --auth-tokens /run/secrets/tokens.json
```

Replace `<org>/<repo>` with your GitHub repo path (the build workflow
substitutes the live value via `${{ github.repository }}`). For
production deploys, **pin a specific `sha-`tag** rather than `latest` —
that gives you reproducible rollouts with one container image per
commit.

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
