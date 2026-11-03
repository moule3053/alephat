# Platform rearchitecture progress

Status against [`SERVICE_CONTRACTS.md`](./SERVICE_CONTRACTS.md), updated 2026-07-25.

## Implemented

- API task lifecycle, transactional outbox, required idempotency keys, task/run/event queries, reconnecting SSE, approvals, cancel, and mid-run messages.
- PostgreSQL leases, periodic heartbeats, immediate JetStream acknowledgement after durable claim, worker message deduplication, expired-lease requeue, cross-attempt checkpoint recovery, and boundary message injection.
- Reconnecting API/webhook outbox publishers and readiness that covers both PostgreSQL and NATS.
- Bearer authentication with static service token or HS256 JWT, organization membership checks, and admin enforcement for organization settings and organization-scoped MCP records.
- Direct OpenAI, Anthropic, Fireworks, and Gemini clients plus optional LiteLLM routing, including exponential retry for throttling and transient provider failures.
- Real Daytona-backed `deepagents.create_deep_agent` execution with Postgres transcript checkpoints.
  The compact command loop is retained only for explicitly enabled local stub sandboxes.
- MCP CRUD and test API, encrypted credential snapshots, harness-side decryption, SSRF boundary, required/optional connect policy, allow/deny filters, real tool invocation, namespacing, timeouts, and tool events.
- `/platform` UI for task creation, live events, guidance, cancellation, approvals, model/sandbox defaults, bearer token storage, and MCP create/test/delete.
- UI multi-stage image, same-origin Nginx API/SSE proxy, Compose UI service, Kubernetes UI Deployment/Service, and a repeatable schema migration service/Job.
- Compose secret scoping so API, webhook, harness, and LiteLLM receive only their relevant secret classes.
- Slack request replay protection and duplicate webhook delivery handling.

## Verification completed

- `ruff check` and format check for the platform Python packages and tests.
- Platform unit tests cover retry, SSRF, MCP invocation, checkpoint/guidance replay, persistent Kubernetes storage, and compiled Deep Agent runs against initialized Daytona and Agent Sandbox backends.
- UI TypeScript type-check and ESLint for the new route/client.
- UI production compilation reached asset/server generation; the final local prerender listener is blocked by this workspace's localhost permission boundary.
- Compose interpolation validation and Kubernetes `kustomize` rendering.

## External adapters still required

- `agent_sandbox` is implemented for the upstream Python runtime service; the target cluster still needs the Sandbox CRDs/controller, runtime template, RBAC, and network policy from `deploy/k8s`.
- `opensandbox` needs the deployment's chosen OpenSandbox API/SDK contract.
- End-to-end provider runs require real LLM and sandbox credentials.
  Production stubs are disabled, so missing adapters fail visibly instead of reporting fake command success.
- Full Compose/browser smoke verification needs Docker daemon access; source validation is complete, but the current execution sandbox denied that socket and its approval service was unavailable.
