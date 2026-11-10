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

### Running the local stack with Gemini 4 Argon

To run the local stack against Gemini 4 Argon with the local sandbox provider, set the following in `deploy/.env`:

```bash
DEFAULT_LLM_PROVIDER=google
GOOGLE_API_KEY=...
DEFAULT_MODEL=google:gemini-4-argon
DEFAULT_SANDBOX_PROVIDER=local
```

Then start the stack with `make platform-up`.

## Host processes (deps in Docker only)

```bash
make platform-deps          # postgres + nats
export DATABASE_URL=postgresql+asyncpg://alephat:alephat@localhost:5432/alephat
export NATS_URL=nats://localhost:4222
make platform-api           # terminal 1
make platform-webhook       # terminal 2
make platform-harness       # terminal 3 — can start later; drains backlog
make platform-slack-renderer # terminal 4 — rich Slack messages, loopback :8082
```

## Rich Slack messages

The platform uses `@json-render/chat` and the Chat SDK Slack adapter in `slack-renderer/`.
The stateless renderer has no Slack or model credentials and never fetches image URLs.
Python keeps the outbound Socket Mode connection, agent runtime, Slack credentials, and authorization.
Compose runs the renderer as an internal service; Kubernetes runs it as a loopback-only harness sidecar.
Do not expose the renderer publicly.
Set the same optional `SLACK_RENDERER_TOKEN` on its server and harness client when using a remote renderer.

Enable Socket Mode and save the `app_mention` and `message.im` bot event subscriptions in Slack's app settings.
Use `SLACK_APP_TOKEN` (`connections:write`), `SLACK_BOT_TOKEN` (`chat:write`, `reactions:write`, `app_mentions:read`, `im:history`, and `files:write`), and `SLACK_BOT_USER_ID` in `deploy/.env`.
Enable Interactivity in the Slack app for buttons; Socket Mode needs no public request URL.
Invite the bot to private channels before mentioning it.

Ordinary Markdown is parsed into Slack-formatted sections, tables, and images.
Replies update in place while the model streams.
The agent can call `render_slack_card` for the entire chat catalog: Card, Section, Text, Fields/Field, Table, Chart, Divider, Image, Actions, Button, and LinkButton.
The renderer also resolves the chat runtime's state bindings, visibility conditions, and repeats.
Chart types are bar, line, area, and pie.
Slack's size limits still apply; the adapter falls back to text for oversized tables/charts, and long messages split into multiple replies.
The renderer is not a browser: arbitrary React, 3D, live browser state, and web-only widgets cannot run inside Slack.

`upload_slack_file` uploads sandbox artifacts directly to the current thread using Slack's external upload flow, without publishing a public URL.
It permits files up to 10 MiB whose resolved path is inside the sandbox workspace.
Use it for private screenshots, PDFs, or diagrams rendered in the sandbox.
Public image embeds require an HTTPS URL.
If rendering fails, the harness attempts a plain-text reply and logs the delivery error.

### Button authorization

Buttons are signed, expire after 24 hours, and are bound to their task, run, and Slack conversation.
The webhook checks organization membership and task ownership (or administrator access); duplicate clicks are idempotent.
Map a Slack identity to an existing platform `users.user_id` explicitly:

```dotenv
SLACK_USER_MAP='{"T0123456789:U0123456789":"00000000-0000-4000-8000-000000000002"}'
```

Unmapped people receive an authorization message rather than inheriting the default developer account's permissions.
New Slack tasks also resolve their creator through this mapping; production rejects unlinked senders, while `API_AUTH_DISABLED=true` retains the local default-account behavior for messages only.
The reserved action IDs `approve`, `reject`, and `cancel` call the same domain operations as the task API.
Other signed buttons deliver their label/value as human guidance, creating a new task for a completed conversation.
Link buttons open URLs without task mutations.
Keep `SLACK_USER_MAP` operator-managed; it grants identity access, not a model-controlled preference.

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
