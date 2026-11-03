# Multi-service platform

Implementation of [`SERVICE_CONTRACTS.md`](./SERVICE_CONTRACTS.md).

## Services

| Service | Module | Image / Dockerfile | Port | Role |
|---|---|---|---|---|
| **API** | `alephat_platform.api.app:app` | `deploy/Dockerfile.api` | 8080 | Tasks, approvals, MCP CRUD, SSE, **outbox → NATS** |
| **Webhook** | `alephat_platform.webhook.app:app` | `deploy/Dockerfile.webhook` | 8081 | GitHub/Slack ingress → Postgres + outbox |
| **Harness** | `alephat_platform.harness.worker` | `deploy/Dockerfile.harness` | — | **NATS consumer**; lease, LiteLLM, sandboxes, MCP, Postgres checkpoints |
| **UI** | `ui/` | `deploy/Dockerfile.ui` | 3000 | `/platform` task console; same-origin proxy to API |

### Does the harness run separately?

**Yes.**
The harness is a pure worker:

1. API/webhook write tasks + outbox rows in Postgres.
1. Outbox publisher (in API and webhook) publishes `task.enqueue` to **NATS JetStream `TASKS`**.
1. Harness **pull-subscribes** to that stream, claims a task (Postgres lease), runs the agent, checkpoints, completes/parks.

You can start/stop/scale harness without restarting API or webhooks.
If harness is down, jobs stay in JetStream / `queued` until a worker appears.
No KEDA is required locally — use Compose scale:

```bash
docker-compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --scale harness=3
# or: make platform-up  then  scale as above
```

## Dependencies

| Dep | Compose service | Notes |
|---|---|---|
| PostgreSQL | `postgres` | `migrate` job applies the idempotent schema before services start |
| NATS JetStream | `nats` | `-js` enabled; monitor on `:8222` |
| LLM providers | env on harness | Direct OpenAI/Anthropic/Fireworks/Google (**default**) |
| LiteLLM | `litellm` (profile `with-litellm`) | **Optional** proxy when `LITELLM_ENABLED=true` |

Sandboxes: `agent_sandbox` (default), `local`, `daytona`, `opensandbox`.
Real Daytona handles, host-local sandboxes (`local`), and the Kubernetes Agent Sandbox backend (`agent_sandbox`, using Kata `kata-qemu` or `gvisor`) run through `deepagents.create_deep_agent`.
The Agent Sandbox backend confines file operations to `/app/repo` and implements shell execution plus upload/download over the Python runtime service.
Stub sandboxes are fail-closed by default and can only be enabled explicitly with `ALLOW_STUB_SANDBOXES=true` for local control-plane/UI testing.

## Docker Compose (recommended local)

Files:

- `deploy/docker-compose.yml` — postgres, nats, migration, api, webhook, harness, UI (+ optional litellm)
- `deploy/Dockerfile.api` / `Dockerfile.webhook` / `Dockerfile.harness` / `Dockerfile.ui`
- `deploy/.env.example` — template (committed)
- `deploy/.env` — local values (gitignored; created from example)

```bash
# First time
cp deploy/.env.example deploy/.env
# Optional: set OPENAI_API_KEY / DAYTONA_API_KEY in deploy/.env

# Build images and start stack
make platform-up
# equivalent:
# docker compose -f deploy/docker-compose.yml --env-file deploy/.env up --build -d

make platform-ps
make platform-logs

# Stop
make platform-down
```

> **Troubleshooting:** An exit code `137` while building the UI image usually means the Docker VM ran out of memory.
> Allocate at least 6 GB of memory to Docker before retrying (with Colima: `colima stop && colima start --memory 6`, then re-run `make platform-up`).

Smoke test:

```bash
curl -s -X POST http://localhost:8080/v1/tasks \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-1' \
  -d '{"title":"Hello","prompt":"DONE: smoke test","agent_type":"coding"}'
```

Watch harness pick up the job (`make platform-logs`).
Task status:

```bash
curl -s http://localhost:8080/v1/tasks | jq .
```

Open the platform console at `http://localhost:3000/platform`.
Nginx serves the SPA and proxies `/v1/*` to the API service, including the unbuffered SSE event stream.
The UI supports task creation, task status/activity, mid-run guidance and cancellation, approvals, model/sandbox defaults, and MCP server create/test/delete.

Compose does not inject the complete `.env` into every service.
Provider and sandbox credentials are available only to the harness (or the optional LiteLLM container), webhook secrets only to the webhook service, and API authentication/encryption values only to the API.

### LLM: direct providers (default) vs optional LiteLLM

**Default:** harness calls providers directly.
Set at least one key in `deploy/.env`:

```bash
OPENAI_API_KEY=sk-...
# or ANTHROPIC_API_KEY / FIREWORKS_API_KEY / GOOGLE_API_KEY
DEFAULT_MODEL=openai:gpt-4o-mini
LLM_MODE=auto          # or direct
LITELLM_ENABLED=false
```

Model id formats: `openai:gpt-4o-mini`, `anthropic:claude-sonnet-4-20250514`, `fireworks:…`, `google:gemini-2.0-flash`.

**Optional LiteLLM proxy:**

```bash
# deploy/.env
LITELLM_ENABLED=true
LLM_MODE=litellm   # or auto with LITELLM_ENABLED=true
LITELLM_BASE_URL=http://litellm:4000
LITELLM_API_KEY=sk-litellm
OPENAI_API_KEY=...     # upstream keys for the proxy

docker-compose -f deploy/docker-compose.yml --env-file deploy/.env --profile with-litellm up -d
```

## Host processes (deps in Docker only)

```bash
make platform-deps          # postgres + nats
export DATABASE_URL=postgresql+asyncpg://alephat:alephat@localhost:5432/alephat
export NATS_URL=nats://localhost:4222
make platform-api           # terminal 1
make platform-webhook       # terminal 2
make platform-harness       # terminal 3 — can start later; drains backlog
```

## Package layout

```text
alephat_platform/
  common/     # enums, db, models, outbox, tasks, resolution, messaging
  api/        # FastAPI control plane
  webhook/    # FastAPI public ingress
  harness/    # worker, agents registry, sandboxes, litellm, mcp
migrations/
deploy/
  Dockerfile.api
  Dockerfile.webhook
  Dockerfile.harness
  Dockerfile.ui
  docker-compose.yml
  .env.example
  litellm_config.yaml
  gke-deploy.sh     # End-to-end GKE cluster, Kata/gVisor, ESO, LiteLLM, KEDA & Traefik deploy
  gke-teardown.sh   # GKE cluster, static IP, DNS, Artifact Registry, IAM & GSM teardown
  k8s/              # Cluster manifests, ExternalSecrets, SandboxTemplate, KEDA & Traefik
```

## Kubernetes (GKE)

Automated end-to-end GKE provisioning (choosing `kata` or `gvisor` for Agent Sandbox, installing External Secrets Operator `2.14.0` backed by Google Cloud Secret Manager, KEDA `2.21.0`, LiteLLM `1.106.6`, and optional Traefik `42.3.0` HTTPS ingress) is provided by `deploy/gke-deploy.sh` and `deploy/gke-teardown.sh`:

```bash
# Deploy with Kata or gVisor, External Secrets Operator + Google Cloud Secret Manager, and Traefik HTTPS
./deploy/gke-deploy.sh \
  --project your-gcp-project-id \
  --region us-central1 \
  --sandbox-runtime gvisor \
  --node-size e2-standard-4 \
  --models "google:gemini-4-argon,openai:gpt-4o-mini,anthropic:claude-3-5-sonnet" \
  --with-traefik

# Tear down cluster, static IP, images, IAM service accounts, and Secret Manager secrets
./deploy/gke-teardown.sh \
  --project your-gcp-project-id \
  --region us-central1 \
  --delete-images \
  --delete-iam \
  --delete-secrets
```

See the [Standalone GKE deployment guide in `README.md`](../README.md#standalone-gke-deployment-kata-or-gvisor-agent-sandbox-litellm-and-keda) for full details and Google Cloud Secret Manager rotation commands.

## Legacy monolith

The `openswe/` LangGraph app (`make dev`) remains available alongside `alephat_platform.*`.
